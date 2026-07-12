"""Fail-closed Stage1 gate and optional Stage2 continuation for IPMSM v2.

The CLI is dry-run by default.  ``--execute`` is the only mode that may launch
the Stage2 campaign or write outputs.  Stage1 runner/watcher PID files are
mandatory so a live watcher cannot race this continuation.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
from typing import Any, Iterable, Mapping
import uuid

from atomic_publish import publish_no_replace
import merge_ipmsm_v2_results as merger
import run_ipmsm_v2_campaign as campaign_runner
import train_ipmsm_lightgbm as trainer
import validate_ipmsm_v2_dataset as dataset_validator


SCHEMA_VERSION = "ipmsm_v2_stage2_continuation_v1"
DEFAULT_R2_THRESHOLD = 0.95
DEFAULT_STAGE1_ROWS = 700
DEFAULT_STAGE1_GROUPS = 112
DEFAULT_STAGE1_REPEATS = 28
DEFAULT_COMBINED_ROWS = 1000
DEFAULT_COMBINED_GROUPS = 160
DEFAULT_COMBINED_REPEATS = 40
DEFAULT_ENSEMBLE_SIZE = 5
DEFAULT_CONFORMAL_COVERAGE = 0.95
PRIMARY_TARGETS = tuple(trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS)
FINGERPRINT_COLUMNS = tuple(trainer.V2_FINGERPRINT_COLUMNS)
STANDARD_FINGERPRINTS = {
    "input_dataset_schema_version": "ipmsm_v2",
    "input_quality_profile": "reference_ultra",
    "input_beta_convention": "dq_current_advance_v2",
    "input_model_extent": "full_360",
}
WINDOWS_TRANSIENT_CREATE_ERRORS = frozenset({5, 32, 33})
ATOMIC_CREATE_STAGING_ATTEMPTS = 3


class ContinuationGateError(RuntimeError):
    """Raised when continuation evidence is missing, invalid, or unsafe."""


@dataclass(frozen=True)
class GateResult:
    decision: str
    validation: dict[str, Any]
    primary_test_r2: dict[str, float]
    primary_failures: tuple[str, ...]
    voltage_test_r2: float
    voltage_failed: bool
    fingerprints: dict[str, str]

    @property
    def passed(self) -> bool:
        return not self.primary_failures and not self.voltage_failed

    def summary(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "fingerprints": self.fingerprints,
            "primary_failures": list(self.primary_failures),
            "primary_test_r2": self.primary_test_r2,
            "validation": self.validation,
            "voltage_failed": self.voltage_failed,
            "voltage_test_r2": self.voltage_test_r2,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage1-runner-pid-file", type=Path, required=True)
    parser.add_argument("--stage1-watcher-pid-file", type=Path, required=True)
    parser.add_argument("--stage1-case-plan", type=Path, required=True)
    parser.add_argument("--stage1-result", type=Path, required=True)
    parser.add_argument("--stage1-validation", type=Path, required=True)
    parser.add_argument("--stage1-metadata", type=Path, required=True)
    parser.add_argument("--stage1-r2", type=Path, required=True)
    parser.add_argument("--stage2-case-plan", type=Path, required=True)
    parser.add_argument("--stage2-output-dir", type=Path, required=True)
    parser.add_argument("--combined-output-dir", type=Path, required=True)
    parser.add_argument("--decision-output", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--scheduler-url", default=campaign_runner.submit_campaign.DEFAULT_SCHEDULER_URL)
    parser.add_argument("--project-active-cap", type=int, default=100)
    parser.add_argument("--stage2-task-prefix", default="ipmsm-v2-foundation-s2")
    parser.add_argument("--stage2-remote-cases-dir", default="remote/ipmsm_v2_foundation_s2")
    parser.add_argument("--stage2-result-dir", default="simul_log/ipmsm_v2_foundation_s2")
    parser.add_argument("--stage2-simulation-dir", default="simulation/ipmsm_v2_foundation_s2")
    parser.add_argument(
        "--stage2-log-dir",
        default="simul_log_scheduler/ipmsm_v2_foundation_s2_logs",
    )
    parser.add_argument("--beta-summary", type=Path, required=True)
    parser.add_argument("--beta-case-plan", type=Path, required=True)
    parser.add_argument("--beta-results", type=Path, required=True)
    parser.add_argument("--beta-calibration-manifest", type=Path, required=True)
    parser.add_argument("--r2-threshold", type=float, default=DEFAULT_R2_THRESHOLD)
    parser.add_argument("--expected-stage1-rows", type=int, default=DEFAULT_STAGE1_ROWS)
    parser.add_argument("--expected-stage1-groups", type=int, default=DEFAULT_STAGE1_GROUPS)
    parser.add_argument("--expected-stage1-repeats", type=int, default=DEFAULT_STAGE1_REPEATS)
    parser.add_argument("--expected-combined-rows", type=int, default=DEFAULT_COMBINED_ROWS)
    parser.add_argument("--expected-combined-groups", type=int, default=DEFAULT_COMBINED_GROUPS)
    parser.add_argument("--expected-combined-repeats", type=int, default=DEFAULT_COMBINED_REPEATS)
    parser.add_argument("--poll-interval-seconds", type=float, default=30.0)
    parser.add_argument("--overall-timeout-seconds", type=float, default=604_800.0)
    parser.add_argument("--terminal-retry-limit", type=int, default=1)
    parser.add_argument("--ensemble-size", type=int, default=DEFAULT_ENSEMBLE_SIZE)
    parser.add_argument("--conformal-coverage", type=float, default=DEFAULT_CONFORMAL_COVERAGE)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Claim the decision artifact, submit Stage2 when required, merge, validate, and train.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Audit a matching stage2_started decision, or resume it when combined with --execute.",
    )
    return parser


def _read_csv(path: Path, label: str) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise ContinuationGateError(f"{label} is missing: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = list(reader.fieldnames or ())
            if not fields or len(fields) != len(set(fields)):
                raise ContinuationGateError(f"{label} has a missing or duplicate CSV header")
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContinuationGateError(f"cannot read {label} {path}: {exc}") from exc
    if any(None in row for row in rows):
        raise ContinuationGateError(f"{label} has fields beyond its CSV header")
    return fields, rows


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ContinuationGateError(f"{label} is missing: {path}")
    try:
        decoded = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContinuationGateError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ContinuationGateError(f"{label} must contain one JSON object")
    return decoded


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ContinuationGateError(f"{label} must be an integer")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ContinuationGateError(f"{label} must be an integer") from exc
    return number


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ContinuationGateError(f"{label} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContinuationGateError(f"{label} must be finite") from exc
    if not math.isfinite(number):
        raise ContinuationGateError(f"{label} must be finite")
    return number


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ContinuationGateError(f"{label} must be a boolean")
    return value


def _validate_validation_summary(
    path: Path,
    *,
    expected_rows: int,
    expected_groups: int,
    expected_repeats: int,
) -> dict[str, Any]:
    _, rows = _read_csv(path, "dataset validation summary")
    if len(rows) != 1:
        raise ContinuationGateError("dataset validation summary must contain exactly one row")
    row = rows[0]
    expected_counts = {
        "rows": expected_rows,
        "ok_rows": expected_rows,
        "unique_case_ids": expected_rows,
        "unique_geometry_groups": expected_groups,
        "repeat_pairs": expected_repeats,
        "failures": 0,
    }
    mismatches = [
        f"{key}={_integer(row.get(key), key)} expected={expected}"
        for key, expected in expected_counts.items()
        if _integer(row.get(key), key) != expected
    ]
    status = str(row.get("status") or "").strip().lower()
    issues = str(row.get("issues") or "").strip()
    if status != "pass":
        mismatches.append(f"status={status!r} expected='pass'")
    if issues:
        mismatches.append(f"issues={issues!r} expected blank")
    if mismatches:
        raise ContinuationGateError(
            "Stage1 coverage/physics/repeat validation failed; Stage2 is forbidden: "
            + "; ".join(mismatches)
        )
    return {**expected_counts, "issues": "", "status": "pass"}


def _validate_training_quality(metadata: Mapping[str, Any], expected_rows: int) -> None:
    if metadata.get("training_schema") != "ipmsm_v2":
        raise ContinuationGateError("metadata.training_schema must be 'ipmsm_v2'")
    quality = metadata.get("training_quality")
    if not isinstance(quality, Mapping):
        raise ContinuationGateError("metadata.training_quality must be an object")
    expected_quality = {
        "raw_rows": expected_rows,
        "rows_after_dedup": expected_rows,
        "dropped_duplicate_case_id_rows": 0,
        "status_rejected_rows": 0,
        "nonfinite_input_rows": 0,
        "nonfinite_output_rows": 0,
        "physical_sanity_rejected_rows": 0,
        "invalid_training_rows": 0,
        "valid_rows_before_outliers": expected_rows,
        "removed_output_outliers": 0,
        "valid_rows": expected_rows,
    }
    failures = [
        f"{key}={_integer(quality.get(key), 'metadata.training_quality.' + key)} expected={value}"
        for key, value in expected_quality.items()
        if _integer(quality.get(key), "metadata.training_quality." + key) != value
    ]
    for key in ("raw_rows", "valid_rows", "removed_output_outliers"):
        expected = expected_rows if key != "removed_output_outliers" else 0
        if _integer(metadata.get(key), "metadata." + key) != expected:
            failures.append(f"metadata.{key} must be {expected}")
    if failures:
        raise ContinuationGateError(
            "training coverage/physics quality is invalid; Stage2 is forbidden: "
            + "; ".join(failures)
        )


def _validate_model_configuration(
    metadata: Mapping[str, Any],
    *,
    expected_groups: int,
    expected_ensemble_size: int,
    expected_conformal_coverage: float,
    expected_audit_case_plan: Path | None = None,
) -> None:
    failures: list[str] = []
    if _integer(metadata.get("ensemble_size"), "metadata.ensemble_size") != expected_ensemble_size:
        failures.append(f"ensemble_size must be {expected_ensemble_size}")
    coverage = _finite(metadata.get("conformal_coverage"), "metadata.conformal_coverage")
    if not math.isclose(
        coverage,
        expected_conformal_coverage,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        failures.append(f"conformal_coverage must be {expected_conformal_coverage}")
    if metadata.get("split_strategy") != "preassigned_geometry_group":
        failures.append("split_strategy must be preassigned_geometry_group")
    if _boolean(
        metadata.get("conformal_calibration_isolated"),
        "metadata.conformal_calibration_isolated",
    ) is not True:
        failures.append("conformal calibration must be isolated")
    if metadata.get("feature_bounds_source") != "train":
        failures.append("feature_bounds_source must be train")
    if metadata.get("fingerprint_columns") != list(FINGERPRINT_COLUMNS):
        failures.append("fingerprint_columns must be the exact v2 fingerprint list")

    if expected_audit_case_plan is not None:
        geometry_column = str(metadata.get("geometry_group_column") or "").strip()
        if geometry_column not in trainer.V2_GEOMETRY_ID_COLUMNS:
            failures.append("geometry_group_column is invalid for audit evaluation")
        else:
            try:
                _, expected_evaluation = trainer.load_v2_audit_case_plan(
                    expected_audit_case_plan,
                    geometry_column=geometry_column,
                )
            except (OSError, ValueError) as exc:
                raise ContinuationGateError(
                    f"cannot validate combined audit case plan: {exc}"
                ) from exc
            recorded_evaluation = metadata.get("test_evaluation")
            if not isinstance(recorded_evaluation, Mapping):
                failures.append("test_evaluation must be an object for combined training")
            elif set(recorded_evaluation) != set(expected_evaluation):
                failures.append("test_evaluation fields do not match the audit contract")
            else:
                mismatches = [
                    key
                    for key, expected in expected_evaluation.items()
                    if recorded_evaluation.get(key) != expected
                ]
                if mismatches:
                    failures.append(
                        "test_evaluation does not match the Stage2 audit plan: "
                        + ", ".join(mismatches)
                    )

    split_counts = metadata.get("split_group_counts")
    if not isinstance(split_counts, Mapping) or set(split_counts) != {"train", "calibration", "test"}:
        failures.append("split_group_counts must contain train/calibration/test")
    else:
        parsed_counts = {
            name: _integer(split_counts[name], f"metadata.split_group_counts.{name}")
            for name in ("train", "calibration", "test")
        }
        if any(value <= 0 for value in parsed_counts.values()):
            failures.append("every split must contain at least one geometry group")
        if sum(parsed_counts.values()) != expected_groups:
            failures.append(f"split_group_counts must sum to {expected_groups}")

    model_targets = (
        *trainer.V2_PRIMITIVE_OUTPUT_COLUMNS,
        *trainer.V2_AUXILIARY_OUTPUT_COLUMNS,
    )
    raw_model_paths = metadata.get("model_paths")
    if not isinstance(raw_model_paths, Mapping) or set(raw_model_paths) != set(model_targets):
        failures.append("model_paths must contain every primitive and auxiliary model exactly once")
    else:
        resolved_model_paths: list[Path] = []
        for target in model_targets:
            raw_path = str(raw_model_paths[target] or "").strip()
            candidate = Path(raw_path)
            if not raw_path or not candidate.is_file():
                failures.append(f"model artifact is missing for {target}")
            elif candidate.stat().st_size <= 0:
                failures.append(f"model artifact is empty for {target}")
            else:
                resolved_model_paths.append(candidate.resolve(strict=True))
        if len(set(resolved_model_paths)) != len(resolved_model_paths):
            failures.append("model_paths must identify distinct model artifacts")
    auxiliary_paths = metadata.get("auxiliary_model_paths")
    if not isinstance(auxiliary_paths, Mapping) or set(auxiliary_paths) != set(
        trainer.V2_AUXILIARY_OUTPUT_COLUMNS
    ):
        failures.append("auxiliary_model_paths is incomplete")
    elif isinstance(raw_model_paths, Mapping):
        for target in trainer.V2_AUXILIARY_OUTPUT_COLUMNS:
            if auxiliary_paths[target] != raw_model_paths.get(target):
                failures.append(f"auxiliary model path disagrees for {target}")
    if failures:
        raise ContinuationGateError(
            "training model configuration/artifacts are incomplete; Stage2 is forbidden: "
            + "; ".join(failures)
        )


def _validate_fingerprints(metadata: Mapping[str, Any]) -> dict[str, str]:
    raw = metadata.get("fingerprints")
    if not isinstance(raw, Mapping) or set(raw) != set(FINGERPRINT_COLUMNS):
        raise ContinuationGateError(
            "metadata.fingerprints must contain the exact v2 fingerprint columns"
        )
    fingerprints = {key: str(raw[key] or "").strip() for key in FINGERPRINT_COLUMNS}
    blank = [key for key, value in fingerprints.items() if not value]
    if blank:
        raise ContinuationGateError(f"metadata fingerprints are blank: {blank}")
    mismatches = [
        f"{key}={fingerprints[key]!r}"
        for key, expected in STANDARD_FINGERPRINTS.items()
        if fingerprints[key] != expected
    ]
    if mismatches:
        raise ContinuationGateError(
            "metadata standard fingerprints are invalid: " + "; ".join(mismatches)
        )
    return fingerprints


def _validate_primary_r2_csv(
    path: Path,
    *,
    threshold: float,
) -> dict[str, float]:
    _, rows = _read_csv(path, "primary R2 verification")
    by_target: dict[str, float] = {}
    for row in rows:
        target = str(row.get("target") or "").strip()
        if not target or target in by_target:
            raise ContinuationGateError("primary R2 verification has blank or duplicate targets")
        if str(row.get("split") or "").strip().lower() != "test":
            raise ContinuationGateError(f"primary R2 target {target!r} is not a test row")
        value = _finite(row.get("R2"), f"primary R2 {target}")
        recorded_threshold = _finite(
            row.get("R2_threshold"),
            f"primary R2 threshold {target}",
        )
        if not math.isclose(recorded_threshold, threshold, rel_tol=0.0, abs_tol=1e-12):
            raise ContinuationGateError(
                f"primary R2 target {target!r} uses threshold {recorded_threshold}, not {threshold}"
            )
        expected_status = "pass" if value >= threshold else "fail"
        recorded_status = str(row.get("status") or "").strip().lower()
        if recorded_status not in {"pass", "fail"}:
            raise ContinuationGateError(f"primary R2 status is invalid for target {target!r}")
        # The trainer writes compact CSV floats but derives status from the
        # unrounded metric retained in metadata.  At the exact boundary either
        # status is therefore admissible here; evaluate_gate cross-checks the
        # higher-precision metadata value and its aggregate pass/fail fields.
        if recorded_status != expected_status and not math.isclose(
            value,
            threshold,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ContinuationGateError(f"primary R2 status is inconsistent for target {target!r}")
        by_target[target] = value
    if set(by_target) != set(PRIMARY_TARGETS):
        missing = sorted(set(PRIMARY_TARGETS) - set(by_target))
        extra = sorted(set(by_target) - set(PRIMARY_TARGETS))
        raise ContinuationGateError(
            f"primary R2 target coverage is incomplete: missing={missing}, extra={extra}"
        )
    return {target: by_target[target] for target in PRIMARY_TARGETS}


def evaluate_gate(
    validation_path: Path,
    metadata_path: Path,
    r2_path: Path,
    *,
    expected_rows: int,
    expected_groups: int,
    expected_repeats: int,
    threshold: float,
    expected_ensemble_size: int = DEFAULT_ENSEMBLE_SIZE,
    expected_conformal_coverage: float = DEFAULT_CONFORMAL_COVERAGE,
    expected_audit_case_plan: Path | None = None,
) -> GateResult:
    validation = _validate_validation_summary(
        validation_path,
        expected_rows=expected_rows,
        expected_groups=expected_groups,
        expected_repeats=expected_repeats,
    )
    metadata = _read_json(metadata_path, "training metadata")
    _validate_training_quality(metadata, expected_rows)
    _validate_model_configuration(
        metadata,
        expected_groups=expected_groups,
        expected_ensemble_size=expected_ensemble_size,
        expected_conformal_coverage=expected_conformal_coverage,
        expected_audit_case_plan=expected_audit_case_plan,
    )
    fingerprints = _validate_fingerprints(metadata)
    primary_csv = _validate_primary_r2_csv(r2_path, threshold=threshold)
    metadata_threshold = _finite(metadata.get("r2_threshold"), "metadata.r2_threshold")
    if not math.isclose(metadata_threshold, threshold, rel_tol=0.0, abs_tol=1e-12):
        raise ContinuationGateError("metadata.r2_threshold does not match the continuation threshold")
    if _boolean(
        metadata.get("primary_test_r2_gate_complete"),
        "metadata.primary_test_r2_gate_complete",
    ) is not True:
        raise ContinuationGateError("primary R2 gate is incomplete; Stage2 is forbidden")
    raw_primary = metadata.get("primary_test_r2")
    if not isinstance(raw_primary, Mapping) or set(raw_primary) != set(PRIMARY_TARGETS):
        raise ContinuationGateError("metadata.primary_test_r2 has incomplete target coverage")
    primary: dict[str, float] = {}
    for target in PRIMARY_TARGETS:
        value = _finite(raw_primary[target], f"metadata.primary_test_r2.{target}")
        if not math.isclose(value, primary_csv[target], rel_tol=1e-9, abs_tol=1e-12):
            raise ContinuationGateError(
                f"metadata and verification CSV disagree for primary target {target!r}"
            )
        primary[target] = value
    primary_failures = tuple(target for target, value in primary.items() if value < threshold)
    primary_passed = not primary_failures
    if _boolean(
        metadata.get("primary_test_r2_gate_passed"),
        "metadata.primary_test_r2_gate_passed",
    ) is not primary_passed:
        raise ContinuationGateError("metadata primary R2 pass flag is inconsistent")
    if _integer(
        metadata.get("primary_test_r2_failures"),
        "metadata.primary_test_r2_failures",
    ) != len(primary_failures):
        raise ContinuationGateError("metadata primary R2 failure count is inconsistent")

    voltage_threshold = _finite(
        metadata.get("voltage_r2_threshold"),
        "metadata.voltage_r2_threshold",
    )
    if not math.isclose(voltage_threshold, threshold, rel_tol=0.0, abs_tol=1e-12):
        raise ContinuationGateError("metadata.voltage_r2_threshold is inconsistent")
    if _boolean(
        metadata.get("voltage_test_r2_gate_complete"),
        "metadata.voltage_test_r2_gate_complete",
    ) is not True:
        raise ContinuationGateError("voltage R2 gate is incomplete; Stage2 is forbidden")
    voltage = _finite(metadata.get("voltage_test_r2"), "metadata.voltage_test_r2")
    voltage_failed = voltage < threshold
    if _boolean(
        metadata.get("voltage_test_r2_gate_passed"),
        "metadata.voltage_test_r2_gate_passed",
    ) is not (not voltage_failed):
        raise ContinuationGateError("metadata voltage R2 pass flag is inconsistent")
    decision = "skip_stage2" if primary_passed and not voltage_failed else "run_stage2"
    return GateResult(
        decision=decision,
        validation=validation,
        primary_test_r2=primary,
        primary_failures=primary_failures,
        voltage_test_r2=voltage,
        voltage_failed=voltage_failed,
        fingerprints=fingerprints,
    )


def _read_pid(path: Path, label: str) -> int:
    if not path.is_file():
        raise ContinuationGateError(f"{label} PID file is missing: {path}")
    try:
        value = int(path.read_text(encoding="utf-8-sig").strip())
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContinuationGateError(f"{label} PID file is invalid: {path}") from exc
    if value <= 0:
        raise ContinuationGateError(f"{label} PID must be positive")
    return value


def pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:  # ERROR_ACCESS_DENIED still proves that the PID exists.
                return True
            if error == 87:  # ERROR_INVALID_PARAMETER means there is no such PID.
                return False
            raise OSError(error, f"OpenProcess failed for PID {pid}")
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                error = ctypes.get_last_error()
                raise OSError(error, f"GetExitCodeProcess failed for PID {pid}")
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def active_stage1_processes(args: argparse.Namespace) -> list[dict[str, Any]]:
    candidates = (
        ("runner", args.stage1_runner_pid_file),
        ("watcher", args.stage1_watcher_pid_file),
    )
    active: list[dict[str, Any]] = []
    for role, path in candidates:
        pid = _read_pid(path, f"Stage1 {role}")
        if pid_is_running(pid):
            active.append({"pid": pid, "role": role})
    return active


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage2_runner_argv(args: argparse.Namespace, *, submit: bool) -> list[str]:
    argv = [
        "--cases",
        str(args.stage2_case_plan),
        "--project",
        args.project,
        "--scheduler-url",
        args.scheduler_url,
        "--project-active-cap",
        str(args.project_active_cap),
        "--task-prefix",
        args.stage2_task_prefix,
        "--remote-cases-dir",
        args.stage2_remote_cases_dir,
        "--result-dir",
        args.stage2_result_dir,
        "--simulation-dir",
        args.stage2_simulation_dir,
        "--log-dir",
        args.stage2_log_dir,
        "--output-dir",
        str(args.stage2_output_dir),
        "--merged-output",
        "merged_results.csv",
        "--overall-timeout-seconds",
        str(args.overall_timeout_seconds),
        "--poll-interval-seconds",
        str(args.poll_interval_seconds),
        "--terminal-retry-limit",
        str(args.terminal_retry_limit),
        "--beta-summary",
        str(args.beta_summary),
        "--beta-case-plan",
        str(args.beta_case_plan),
        "--beta-results",
        str(args.beta_results),
        "--beta-calibration-manifest",
        str(args.beta_calibration_manifest),
    ]
    if submit:
        argv.append("--submit")
    return argv


def _combined_paths(
    args: argparse.Namespace,
    output_dir: Path | None = None,
) -> dict[str, Path]:
    root = output_dir or args.combined_output_dir
    return {
        "merged": root / "merged_results.csv",
        "validation": root / "validation.csv",
        "model_dir": root / "models",
        "r2": root / "r2_gate.csv",
    }


def _combined_staging_dir(args: argparse.Namespace) -> Path:
    return args.combined_output_dir.with_name(args.combined_output_dir.name + ".staging")


def _assert_path_fresh(path: Path, label: str) -> None:
    if path.exists():
        raise ContinuationGateError(f"{label} must not already exist: {path}")


def validate_args(args: argparse.Namespace) -> None:
    if not str(args.project or "").strip():
        raise ContinuationGateError("--project must not be blank")
    if not 1 <= args.project_active_cap <= 100:
        raise ContinuationGateError("--project-active-cap must be between 1 and 100")
    if not math.isfinite(args.r2_threshold) or not 0.0 < args.r2_threshold <= 1.0:
        raise ContinuationGateError("--r2-threshold must be finite and between 0 and 1")
    if not math.isfinite(args.poll_interval_seconds) or args.poll_interval_seconds <= 0.0:
        raise ContinuationGateError("--poll-interval-seconds must be finite and > 0")
    if not math.isfinite(args.overall_timeout_seconds) or args.overall_timeout_seconds <= 0.0:
        raise ContinuationGateError("--overall-timeout-seconds must be finite and > 0")
    if args.terminal_retry_limit < 0:
        raise ContinuationGateError("--terminal-retry-limit must be >= 0")
    positive_counts = (
        args.expected_stage1_rows,
        args.expected_stage1_groups,
        args.expected_stage1_repeats,
        args.expected_combined_rows,
        args.expected_combined_groups,
        args.expected_combined_repeats,
        args.ensemble_size,
    )
    if any(value < 1 for value in positive_counts):
        raise ContinuationGateError("expected counts and --ensemble-size must be positive")
    if args.expected_combined_groups <= args.expected_stage1_groups:
        raise ContinuationGateError("combined groups must exceed Stage1 groups")
    if args.expected_combined_repeats < args.expected_stage1_repeats:
        raise ContinuationGateError("combined repeats must not be less than Stage1 repeats")
    if not 0.0 < args.conformal_coverage < 1.0:
        raise ContinuationGateError("--conformal-coverage must be between 0 and 1")
    stage1_count = _case_plan_count(args.stage1_case_plan)
    stage2_count = _case_plan_count(args.stage2_case_plan)
    if stage1_count != args.expected_stage1_rows:
        raise ContinuationGateError(
            f"Stage1 case-plan rows={stage1_count}, expected={args.expected_stage1_rows}"
        )
    if args.expected_combined_rows != args.expected_stage1_rows + stage2_count:
        raise ContinuationGateError(
            "combined expected rows must equal Stage1 rows plus the Stage2 case-plan rows"
        )
    for path, label in (
        (args.stage1_case_plan, "Stage1 case plan"),
        (args.stage2_case_plan, "Stage2 case plan"),
        (args.beta_summary, "beta summary"),
        (args.beta_case_plan, "beta case plan"),
        (args.beta_results, "beta results"),
        (args.beta_calibration_manifest, "beta calibration manifest"),
    ):
        if not path.is_file():
            raise ContinuationGateError(f"{label} is missing: {path}")
    _assert_nonoverlapping_case_plans(args.stage1_case_plan, args.stage2_case_plan)
    staging = _combined_staging_dir(args)
    if _paths_overlap(args.stage2_output_dir, args.combined_output_dir):
        raise ContinuationGateError("Stage2 and combined output directories must not overlap")
    if _paths_overlap(args.stage2_output_dir, staging):
        raise ContinuationGateError("Stage2 output and combined staging directories must not overlap")
    if _path_within(args.decision_output, args.stage2_output_dir) or _path_within(
        args.decision_output,
        args.combined_output_dir,
    ) or _path_within(args.decision_output, staging):
        raise ContinuationGateError("decision output must be outside result output directories")
    claim = _claim_path(args.decision_output)
    if args.resume:
        if not args.decision_output.is_file():
            raise ContinuationGateError(
                f"--resume requires an existing decision output: {args.decision_output}"
            )
    else:
        _assert_path_fresh(args.decision_output, "decision output")
        _assert_path_fresh(claim, "decision claim")


def _case_plan_ids(path: Path) -> list[str]:
    _, rows = _read_csv(path, f"case plan {path}")
    return merger.unique_case_ids(rows, source=str(path))


def _case_plan_count(path: Path) -> int:
    return len(_case_plan_ids(path))


def _validate_result_coverage(
    case_plan: Path | Iterable[Path],
    result: Path,
    label: str,
) -> tuple[list[str], list[dict[str, str]]]:
    try:
        return merger.merge_complete_results(case_plan, [result])
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise ContinuationGateError(f"{label} does not exactly cover its case plan: {exc}") from exc


def _validate_result_evidence(
    case_plan: Path | Iterable[Path],
    result: Path,
    gate: GateResult,
    label: str,
) -> None:
    headers, rows = _validate_result_coverage(case_plan, result, label)
    summary = dataset_validator.validate_rows(rows, fieldnames=headers)
    actual = summary.as_row()
    expected = gate.validation
    mismatches = [
        f"{key}={actual.get(key)!r} expected={expected.get(key)!r}"
        for key in (
            "rows",
            "ok_rows",
            "unique_case_ids",
            "unique_geometry_groups",
            "repeat_pairs",
            "failures",
            "status",
            "issues",
        )
        if actual.get(key) != expected.get(key)
    ]
    for column, expected_value in gate.fingerprints.items():
        values = summary.fingerprint_values.get(column)
        if values is None:
            values = {
                str(row.get(column) or "").strip()
                for row in rows
                if str(row.get(column) or "").strip()
            }
        if values != {expected_value}:
            mismatches.append(
                f"{column}={sorted(values)!r} expected={[expected_value]!r}"
            )
    if mismatches:
        raise ContinuationGateError(
            f"{label} is not bound to its validation/model evidence: " + "; ".join(mismatches)
        )


def _assert_nonoverlapping_case_plans(stage1: Path, stage2: Path) -> None:
    stage1_ids = set(_case_plan_ids(stage1))
    overlap = [case_id for case_id in _case_plan_ids(stage2) if case_id in stage1_ids]
    if overlap:
        raise ContinuationGateError(f"Stage1 and Stage2 case plans overlap: {overlap[:3]}")
    _, stage1_rows = _read_csv(stage1, "Stage1 case plan")
    _, stage2_rows = _read_csv(stage2, "Stage2 case plan")
    stage1_hashes = {str(row.get("design_hash") or "").strip() for row in stage1_rows}
    stage2_hashes = {str(row.get("design_hash") or "").strip() for row in stage2_rows}
    if "" in stage1_hashes or "" in stage2_hashes:
        raise ContinuationGateError("Stage1 and Stage2 plans require nonblank design_hash values")
    design_overlap = sorted(stage1_hashes & stage2_hashes)
    if design_overlap:
        raise ContinuationGateError(
            f"Stage1 and Stage2 plans overlap by design_hash: {design_overlap[:3]}"
        )


def _claim_path(decision_output: Path) -> Path:
    return decision_output.with_name(decision_output.name + ".claim")


def _recovery_claim_path(decision_output: Path) -> Path:
    claim = _claim_path(decision_output)
    return claim.with_name(claim.name + ".recover")


def _path_within(path: Path, directory: Path) -> bool:
    return path.resolve(strict=False).is_relative_to(directory.resolve(strict=False))


def _paths_overlap(first: Path, second: Path) -> bool:
    return _path_within(first, second) or _path_within(second, first)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _json_text(value)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"


def _atomic_create_json(path: Path, value: Mapping[str, Any], label: str) -> None:
    """Publish a complete JSON file without replacing an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(ATOMIC_CREATE_STAGING_ATTEMPTS):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".create.tmp",
            dir=path.parent,
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(_json_text(value))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                publish_no_replace(temporary, path)
            except FileExistsError as exc:
                raise ContinuationGateError(f"{label} already exists: {path}") from exc
            except OSError as exc:
                if path.exists():
                    raise ContinuationGateError(f"{label} already exists: {path}") from exc
                transient = getattr(exc, "winerror", None) in WINDOWS_TRANSIENT_CREATE_ERRORS
                if transient and attempt + 1 < ATOMIC_CREATE_STAGING_ATTEMPTS:
                    continue
                raise ContinuationGateError(
                    f"cannot atomically create {label} {path}: {exc}"
                ) from exc
            return
        finally:
            temporary.unlink(missing_ok=True)


def _new_owner(mode: str) -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "invocation_id": uuid.uuid4().hex,
        "mode": mode,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def _acquire_claim(
    args: argparse.Namespace,
    *,
    owner: Mapping[str, Any],
    decision_sha256: str | None,
    contract_sha256: str,
    original_owner: Mapping[str, Any],
) -> Path:
    claim = _claim_path(args.decision_output)
    claim_payload = {
        "decision_output": str(args.decision_output.resolve(strict=False)),
        "decision_sha256": decision_sha256 or "",
        "contract_sha256": contract_sha256,
        "original_owner": dict(original_owner),
        "owner": dict(owner),
        "schema_version": SCHEMA_VERSION,
    }
    _atomic_create_json(claim, claim_payload, "continuation claim")
    return claim


def _start_decision(
    args: argparse.Namespace,
    payload: Mapping[str, Any],
    owner: Mapping[str, Any],
) -> Path:
    contract_sha256 = str(payload.get("contract_sha256") or "").strip()
    if not contract_sha256:
        raise ContinuationGateError("decision payload has no immutable contract hash")
    expected_decision_hash = hashlib.sha256(_json_text(payload).encode("utf-8")).hexdigest()
    claim = _acquire_claim(
        args,
        owner=owner,
        decision_sha256=expected_decision_hash,
        contract_sha256=contract_sha256,
        original_owner=owner,
    )
    decision_created = False
    try:
        _atomic_create_json(args.decision_output, payload, "decision output")
        decision_created = True
        if _sha256(args.decision_output) != expected_decision_hash:
            raise ContinuationGateError("created decision hash does not match its claim")
    except Exception:
        if not decision_created:
            _release_claim(claim, owner)
        raise
    return claim


def _claim_is_owned(claim: Path, owner: Mapping[str, Any]) -> bool:
    try:
        value = _read_json(claim, "continuation claim")
    except ContinuationGateError:
        return False
    return value.get("schema_version") == SCHEMA_VERSION and value.get("owner") == dict(owner)


def _require_claim_owned(claim: Path, owner: Mapping[str, Any]) -> None:
    if not _claim_is_owned(claim, owner):
        raise ContinuationGateError("continuation claim ownership was lost; decision update is forbidden")


def _release_claim(claim: Path, owner: Mapping[str, Any]) -> None:
    if _claim_is_owned(claim, owner):
        claim.unlink(missing_ok=True)


def _training_argv(
    args: argparse.Namespace,
    merged_path: Path,
    model_dir: Path,
    r2_path: Path,
    fingerprints: Mapping[str, str],
) -> list[str]:
    argv = [
        "--v2",
        "--data",
        str(merged_path),
        "--model-dir",
        str(model_dir),
        "--verification-output",
        str(r2_path),
        "--r2-threshold",
        str(args.r2_threshold),
        "--fail-on-threshold",
        "--ensemble-size",
        str(args.ensemble_size),
        "--conformal-coverage",
        str(args.conformal_coverage),
        "--max-invalid-training-rows",
        "0",
        "--max-removed-output-outlier-rows",
        "0",
        "--v2-audit-case-plan",
        str(args.stage2_case_plan),
    ]
    for column in FINGERPRINT_COLUMNS:
        argv.extend(("--expected-fingerprint", f"{column}={fingerprints[column]}"))
    return argv


def validate_stage2_readiness(args: argparse.Namespace) -> None:
    runner_args = campaign_runner.build_parser().parse_args(
        _stage2_runner_argv(args, submit=True)
    )
    campaign_runner.validate_args(runner_args)
    beta_summary = campaign_runner.load_beta_prerequisite(runner_args)
    if beta_summary is None:
        raise ContinuationGateError("Stage2 runner has no strict beta prerequisite")
    validated_rows = campaign_runner.submit_campaign.load_and_validate_cases(
        runner_args.cases,
        runner_args.max_plan_cases,
        False,
    )
    selected_rows = campaign_runner.submit_campaign.select_case_rows(
        validated_rows,
        runner_args.case_start_index,
        runner_args.case_limit,
    )
    if len(selected_rows) != _case_plan_count(args.stage2_case_plan):
        raise ContinuationGateError("Stage2 runner selection does not cover its exact case plan")
    campaign_runner.validate_foundation_rows(selected_rows, beta_summary)


def run_combined_pipeline(
    args: argparse.Namespace,
    stage1_gate: GateResult,
    output_dir: Path | None = None,
) -> GateResult:
    stage2_result = args.stage2_output_dir / "merged_results.csv"
    if not stage2_result.is_file():
        raise ContinuationGateError(f"Stage2 runner did not produce merged results: {stage2_result}")
    root = output_dir or args.combined_output_dir
    _assert_path_fresh(root, "combined output directory")
    paths = _combined_paths(args, root)
    root.mkdir(parents=True)
    headers, rows = merger.merge_complete_results(
        [args.stage1_case_plan, args.stage2_case_plan],
        [args.stage1_result, stage2_result],
    )
    merger.write_csv(paths["merged"], headers, rows)
    validation_code = dataset_validator.main(
        ["--data", str(paths["merged"]), "--summary", str(paths["validation"])]
    )
    if validation_code != 0:
        raise ContinuationGateError("combined dataset validation failed")
    training_argv = _training_argv(
        args,
        paths["merged"],
        paths["model_dir"],
        paths["r2"],
        stage1_gate.fingerprints,
    )
    training_code = trainer.main(training_argv)
    if training_code not in (0, 1):
        raise ContinuationGateError(f"combined training returned unexpected code {training_code}")
    combined_gate = evaluate_gate(
        paths["validation"],
        paths["model_dir"] / "metadata.json",
        paths["r2"],
        expected_rows=args.expected_combined_rows,
        expected_groups=args.expected_combined_groups,
        expected_repeats=args.expected_combined_repeats,
        threshold=args.r2_threshold,
        expected_ensemble_size=args.ensemble_size,
        expected_conformal_coverage=args.conformal_coverage,
        expected_audit_case_plan=args.stage2_case_plan,
    )
    if (training_code == 0) != combined_gate.passed:
        raise ContinuationGateError("combined training exit code and R2 metadata disagree")
    if root.resolve(strict=False) == _combined_staging_dir(args).resolve(strict=False):
        _relocate_staged_model_metadata(args, root)
    return combined_gate


def _relocate_staged_model_metadata(args: argparse.Namespace, staging: Path) -> None:
    metadata_path = staging / "models" / "metadata.json"
    metadata = _read_json(metadata_path, "staged combined training metadata")
    staging_root = staging.resolve(strict=False)
    final_root = args.combined_output_dir.resolve(strict=False)

    def relocate(raw: object, label: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        resolved = Path(text).resolve(strict=False)
        if not resolved.is_relative_to(staging_root):
            raise ContinuationGateError(f"{label} is outside the combined staging directory")
        return str(final_root / resolved.relative_to(staging_root))

    for field in ("model_paths", "auxiliary_model_paths"):
        raw_paths = metadata.get(field)
        if not isinstance(raw_paths, Mapping):
            raise ContinuationGateError(f"staged metadata.{field} must be an object")
        metadata[field] = {
            str(target): relocate(path, f"metadata.{field}.{target}")
            for target, path in raw_paths.items()
        }
    for field in ("metrics_path", "auxiliary_metrics_path", "tuning_trials_path"):
        if field in metadata:
            metadata[field] = relocate(metadata.get(field), f"metadata.{field}")
    for field in ("model_artifacts", "training_artifacts"):
        if field not in metadata:
            continue
        raw_artifacts = metadata.get(field)
        if not isinstance(raw_artifacts, Mapping):
            raise ContinuationGateError(f"staged metadata.{field} must be an object")
        relocated: dict[str, dict[str, Any]] = {}
        for name, raw_record in raw_artifacts.items():
            if not isinstance(raw_record, Mapping):
                raise ContinuationGateError(
                    f"staged metadata.{field}.{name} must be an object"
                )
            record = dict(raw_record)
            record["path"] = relocate(
                record.get("path"),
                f"metadata.{field}.{name}.path",
            )
            relocated[str(name)] = record
        metadata[field] = relocated
    _atomic_write_json(metadata_path, metadata)


def _load_combined_gate(args: argparse.Namespace, stage1_gate: GateResult) -> GateResult:
    paths = _combined_paths(args)
    required = (
        paths["merged"],
        paths["validation"],
        paths["model_dir"] / "metadata.json",
        paths["r2"],
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ContinuationGateError(
            "combined output directory is partial; missing artifacts: " + ", ".join(missing)
        )
    gate = evaluate_gate(
        paths["validation"],
        paths["model_dir"] / "metadata.json",
        paths["r2"],
        expected_rows=args.expected_combined_rows,
        expected_groups=args.expected_combined_groups,
        expected_repeats=args.expected_combined_repeats,
        threshold=args.r2_threshold,
        expected_ensemble_size=args.ensemble_size,
        expected_conformal_coverage=args.conformal_coverage,
        expected_audit_case_plan=args.stage2_case_plan,
    )
    if gate.fingerprints != stage1_gate.fingerprints:
        raise ContinuationGateError("combined fingerprints do not match Stage1")
    _validate_result_evidence(
        [args.stage1_case_plan, args.stage2_case_plan],
        paths["merged"],
        gate,
        "combined merged result",
    )
    return gate


def _publish_staged_combined(args: argparse.Namespace, staging: Path) -> None:
    _assert_path_fresh(args.combined_output_dir, "combined output directory")
    try:
        staging.rename(args.combined_output_dir)
    except OSError as exc:
        if args.combined_output_dir.exists():
            raise ContinuationGateError(
                f"combined output appeared before atomic publish: {args.combined_output_dir}"
            ) from exc
        raise ContinuationGateError(f"cannot atomically publish combined output: {exc}") from exc


def _artifact_contract(path: Path) -> dict[str, str]:
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": _sha256(path),
    }


def _execution_contract(args: argparse.Namespace, gate: GateResult) -> dict[str, Any]:
    return {
        "beta": {
            "calibration_manifest": _artifact_contract(args.beta_calibration_manifest),
            "case_plan": _artifact_contract(args.beta_case_plan),
            "results": _artifact_contract(args.beta_results),
            "summary": _artifact_contract(args.beta_summary),
        },
        "combined": {
            "expected_groups": args.expected_combined_groups,
            "expected_repeats": args.expected_combined_repeats,
            "expected_rows": args.expected_combined_rows,
            "output_dir": str(args.combined_output_dir.resolve(strict=False)),
            "staging_dir": str(_combined_staging_dir(args).resolve(strict=False)),
        },
        "stage1": {
            "case_plan": _artifact_contract(args.stage1_case_plan),
            "expected_groups": args.expected_stage1_groups,
            "expected_repeats": args.expected_stage1_repeats,
            "expected_rows": args.expected_stage1_rows,
            "fingerprints": gate.fingerprints,
            "metadata": _artifact_contract(args.stage1_metadata),
            "r2": _artifact_contract(args.stage1_r2),
            "result": _artifact_contract(args.stage1_result),
            "validation": _artifact_contract(args.stage1_validation),
        },
        "stage2": {
            "case_plan": _artifact_contract(args.stage2_case_plan),
            "output_dir": str(args.stage2_output_dir.resolve(strict=False)),
            "runner_argv": _stage2_runner_argv(args, submit=True),
        },
        "training": {
            "audit_case_plan": _artifact_contract(args.stage2_case_plan),
            "conformal_coverage": args.conformal_coverage,
            "ensemble_size": args.ensemble_size,
            "r2_threshold": args.r2_threshold,
            "test_evaluation_scope": trainer.V2_TEST_EVALUATION_SCOPE_AUDIT_CASE_PLAN,
        },
    }


def _contract_sha256(contract: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(contract),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _base_payload(args: argparse.Namespace, gate: GateResult) -> dict[str, Any]:
    contract = _execution_contract(args, gate)
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_sha256": _contract_sha256(contract),
        "decision": gate.decision,
        "decision_output": str(args.decision_output),
        "execution_contract": contract,
        "stage1": {
            **gate.summary(),
            "case_plan": str(args.stage1_case_plan),
            "case_plan_sha256": _sha256(args.stage1_case_plan),
            "metadata": str(args.stage1_metadata),
            "metadata_sha256": _sha256(args.stage1_metadata),
            "r2": str(args.stage1_r2),
            "r2_sha256": _sha256(args.stage1_r2),
            "result": str(args.stage1_result),
            "result_sha256": _sha256(args.stage1_result),
            "validation_path": str(args.stage1_validation),
            "validation_sha256": _sha256(args.stage1_validation),
        },
        "stage2": {
            "beta": contract["beta"],
            "case_plan": str(args.stage2_case_plan),
            "case_plan_sha256": _sha256(args.stage2_case_plan),
            "output_dir": str(args.stage2_output_dir),
            "runner_argv": _stage2_runner_argv(args, submit=True),
        },
    }


def _validate_resume_decision(
    args: argparse.Namespace,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    prior = _read_json(args.decision_output, "resume decision")
    required_values = {
        "schema_version": SCHEMA_VERSION,
        "decision": "run_stage2",
        "status": "stage2_started",
        "mode": "execute",
    }
    mismatches = [
        f"{key}={prior.get(key)!r} expected={value!r}"
        for key, value in required_values.items()
        if prior.get(key) != value
    ]
    created_at = str(prior.get("created_at") or "").strip()
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        created = None
    if created is None or created.tzinfo is None:
        mismatches.append("created_at must be a timezone-aware ISO timestamp")
    try:
        prior_decision_path = Path(str(prior.get("decision_output") or "")).resolve(strict=False)
    except (OSError, ValueError):
        prior_decision_path = Path()
    if prior_decision_path != args.decision_output.resolve(strict=False):
        mismatches.append("decision_output path changed")
    expected_contract = expected.get("execution_contract")
    if not isinstance(expected_contract, Mapping):
        raise ContinuationGateError("internal resume contract is missing")
    if prior.get("execution_contract") != expected_contract:
        mismatches.append("execution_contract changed")
    expected_contract_hash = _contract_sha256(expected_contract)
    if prior.get("contract_sha256") != expected_contract_hash:
        mismatches.append("contract_sha256 changed")
    if prior.get("stage1") != expected.get("stage1"):
        mismatches.append("Stage1 evidence changed")
    if prior.get("stage2") != expected.get("stage2"):
        mismatches.append("Stage2 evidence/runner argv changed")
    owner = prior.get("owner")
    if not isinstance(owner, Mapping):
        mismatches.append("original owner is missing")
    if mismatches:
        raise ContinuationGateError("resume decision is not an exact stage2_started match: " + "; ".join(mismatches))
    return prior


def _require_owner_inactive(owner: Mapping[str, Any], label: str) -> None:
    hostname = str(owner.get("hostname") or "").strip()
    if hostname != socket.gethostname():
        raise ContinuationGateError(
            f"{label} hostname mismatch: recorded={hostname!r} current={socket.gethostname()!r}"
        )
    pid = _integer(owner.get("pid"), f"{label}.pid")
    if pid <= 0:
        raise ContinuationGateError(f"{label}.pid must be positive")
    if pid_is_running(pid):
        raise ContinuationGateError(f"{label} is still active: pid={pid}")


def _require_prior_owner_inactive(prior: Mapping[str, Any]) -> None:
    owner = prior["owner"]
    if not isinstance(owner, Mapping):
        raise ContinuationGateError("resume decision original owner is invalid")
    _require_owner_inactive(owner, "original Stage2 continuation owner")


def _validate_stale_claim(
    args: argparse.Namespace,
    prior: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    claim = _claim_path(args.decision_output)
    value = _read_json(claim, "stale continuation claim")
    expected_keys = {
        "contract_sha256",
        "decision_output",
        "decision_sha256",
        "original_owner",
        "owner",
        "schema_version",
    }
    mismatches: list[str] = []
    if set(value) != expected_keys:
        mismatches.append("claim fields are not exact")
    if value.get("schema_version") != SCHEMA_VERSION:
        mismatches.append("claim schema_version changed")
    if value.get("decision_output") != str(args.decision_output.resolve(strict=False)):
        mismatches.append("claim decision_output changed")
    decision_hash = _sha256(args.decision_output)
    if value.get("decision_sha256") != decision_hash:
        mismatches.append("claim decision hash does not match the prior decision")
    if value.get("contract_sha256") != prior.get("contract_sha256"):
        mismatches.append("claim contract hash does not match the prior decision")
    if value.get("original_owner") != prior.get("owner"):
        mismatches.append("claim original owner does not match the prior decision")
    owner = value.get("owner")
    if not isinstance(owner, Mapping):
        mismatches.append("claim owner is invalid")
    if mismatches:
        raise ContinuationGateError("stale continuation claim is not recoverable: " + "; ".join(mismatches))
    _require_owner_inactive(owner, "stale continuation claim owner")
    return value, _sha256(claim)


def _acquire_recovery_lock(
    args: argparse.Namespace,
    owner: Mapping[str, Any],
    decision_sha256: str,
) -> Path:
    recovery = _recovery_claim_path(args.decision_output)
    _atomic_create_json(
        recovery,
        {
            "decision_output": str(args.decision_output.resolve(strict=False)),
            "decision_sha256": decision_sha256,
            "owner": dict(owner),
            "schema_version": SCHEMA_VERSION,
        },
        "stale-claim recovery lock",
    )
    return recovery


def _acquire_resume_claim(
    args: argparse.Namespace,
    prior: Mapping[str, Any],
    owner: Mapping[str, Any],
) -> Path:
    claim = _claim_path(args.decision_output)
    recovery = _recovery_claim_path(args.decision_output)
    if recovery.exists():
        raise ContinuationGateError(
            f"stale-claim recovery is already in progress: {recovery}"
        )
    decision_hash = _sha256(args.decision_output)
    contract_sha256 = str(prior.get("contract_sha256") or "").strip()
    original_owner = prior.get("owner")
    if not contract_sha256 or not isinstance(original_owner, Mapping):
        raise ContinuationGateError("resume decision claim evidence is incomplete")
    recovery_lock: Path | None = None
    if claim.exists():
        recovery_lock = _acquire_recovery_lock(args, owner, decision_hash)
        try:
            _require_prior_owner_inactive(prior)
            _, stale_claim_hash = _validate_stale_claim(args, prior)
            if _sha256(args.decision_output) != decision_hash or _read_json(
                args.decision_output,
                "resume decision",
            ) != prior:
                raise ContinuationGateError("decision changed during stale-claim recovery")
            if _sha256(claim) != stale_claim_hash:
                raise ContinuationGateError("stale claim changed during recovery")
            claim.unlink()
            claim = _acquire_claim(
                args,
                owner=owner,
                decision_sha256=decision_hash,
                contract_sha256=contract_sha256,
                original_owner=original_owner,
            )
        finally:
            _release_claim(recovery_lock, owner)
    else:
        claim = _acquire_claim(
            args,
            owner=owner,
            decision_sha256=decision_hash,
            contract_sha256=contract_sha256,
            original_owner=original_owner,
        )
    try:
        if _sha256(args.decision_output) != decision_hash:
            raise ContinuationGateError("decision output changed while acquiring resume claim")
        reread = _read_json(args.decision_output, "resume decision")
        if reread != prior:
            raise ContinuationGateError("decision content changed while acquiring resume claim")
    except Exception:
        _release_claim(claim, owner)
        raise
    return claim


def _assert_contract_unchanged(
    args: argparse.Namespace,
    gate: GateResult,
    expected_contract: Mapping[str, Any],
) -> None:
    current = _execution_contract(args, gate)
    if current != expected_contract:
        raise ContinuationGateError(
            "immutable Stage1/Stage2/beta artifacts or execution settings changed after decision"
        )


def _stage2_output_state(args: argparse.Namespace) -> str:
    if not args.stage2_output_dir.exists():
        return "absent"
    if not args.stage2_output_dir.is_dir():
        raise ContinuationGateError(
            f"Stage2 output path exists but is not a directory: {args.stage2_output_dir}"
        )
    result = args.stage2_output_dir / "merged_results.csv"
    if not result.is_file():
        raise ContinuationGateError(
            f"Stage2 output directory is partial; merged result is missing: {result}"
        )
    _validate_result_coverage(args.stage2_case_plan, result, "Stage2 merged result")
    return "complete"


def _combined_output_state(args: argparse.Namespace) -> str:
    staging = _combined_staging_dir(args)
    if staging.exists():
        raise ContinuationGateError(
            f"combined staging directory already exists and may be partial: {staging}"
        )
    if not args.combined_output_dir.exists():
        return "absent"
    if not args.combined_output_dir.is_dir():
        raise ContinuationGateError(
            f"combined output path exists but is not a directory: {args.combined_output_dir}"
        )
    return "complete"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_args(args)
    active = active_stage1_processes(args)
    if active:
        output = {
            "active_stage1_processes": active,
            "decision": "wait_for_stage1",
            "mode": (
                "resume-execute"
                if args.resume and args.execute
                else "resume-dry-run"
                if args.resume
                else "execute"
                if args.execute
                else "dry-run"
            ),
            "schema_version": SCHEMA_VERSION,
        }
        if args.execute:
            raise ContinuationGateError(
                f"Stage1 runner/watcher is still active; continuation is forbidden: {active}"
            )
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return 0

    gate = evaluate_gate(
        args.stage1_validation,
        args.stage1_metadata,
        args.stage1_r2,
        expected_rows=args.expected_stage1_rows,
        expected_groups=args.expected_stage1_groups,
        expected_repeats=args.expected_stage1_repeats,
        threshold=args.r2_threshold,
        expected_ensemble_size=args.ensemble_size,
        expected_conformal_coverage=args.conformal_coverage,
    )
    if not args.stage1_result.is_file():
        raise ContinuationGateError(f"Stage1 merged result is missing: {args.stage1_result}")
    _validate_result_evidence(
        args.stage1_case_plan,
        args.stage1_result,
        gate,
        "Stage1 merged result",
    )
    expected_payload = _base_payload(args, gate)

    if args.resume and gate.decision != "run_stage2":
        raise ContinuationGateError("--resume is valid only while the exact gate still requires Stage2")

    if gate.decision == "run_stage2":
        if not args.resume:
            _assert_path_fresh(args.stage2_output_dir, "Stage2 output directory")
            _assert_path_fresh(args.combined_output_dir, "combined output directory")
            _assert_path_fresh(_combined_staging_dir(args), "combined staging directory")
        validate_stage2_readiness(args)
    if not args.resume:
        payload = expected_payload
        payload["mode"] = "execute" if args.execute else "dry-run"
        payload["status"] = "planned"
        if not args.execute:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            return 0
        owner = _new_owner("execute")
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
        payload["owner"] = owner
        if gate.decision == "skip_stage2":
            payload["status"] = "complete"
            claim = _start_decision(args, payload, owner)
            _release_claim(claim, owner)
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            return 0
        payload["status"] = "stage2_started"
        claim = _start_decision(args, payload, owner)
    else:
        prior = _validate_resume_decision(args, expected_payload)
        _require_prior_owner_inactive(prior)
        recovery_path = _recovery_claim_path(args.decision_output)
        if recovery_path.exists():
            raise ContinuationGateError(
                f"stale-claim recovery is already in progress: {recovery_path}"
            )
        stale_claim_present = _claim_path(args.decision_output).exists()
        if stale_claim_present:
            _validate_stale_claim(args, prior)
        stage2_state = _stage2_output_state(args)
        combined_state = _combined_output_state(args)
        if combined_state == "complete" and stage2_state != "complete":
            raise ContinuationGateError("combined output exists without a complete Stage2 output")
        if not args.execute:
            audit = dict(prior)
            audit["mode"] = "resume-dry-run"
            audit["resume_action"] = {
                "claim": "recover_stale" if stale_claim_present else "acquire",
                "combined": "finalize_existing" if combined_state == "complete" else "build",
                "stage2": "skip_runner" if stage2_state == "complete" else "run",
            }
            print(json.dumps(audit, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            return 0
        owner = _new_owner("resume")
        claim = _acquire_resume_claim(args, prior, owner)
        payload = dict(prior)
        payload["resume_owner"] = owner
        payload["resumed_at"] = datetime.now(timezone.utc).isoformat()

    try:
        contract = payload["execution_contract"]
        _assert_contract_unchanged(args, gate, contract)
        stage2_state = _stage2_output_state(args)
        if stage2_state == "absent":
            runner_code = campaign_runner.main(list(contract["stage2"]["runner_argv"]))
            if runner_code != 0:
                raise ContinuationGateError(f"Stage2 runner returned nonzero code {runner_code}")
            if _stage2_output_state(args) != "complete":
                raise ContinuationGateError("Stage2 runner finished without a complete exact result")
        _assert_contract_unchanged(args, gate, contract)

        combined_state = _combined_output_state(args)
        if combined_state == "complete":
            combined_gate = _load_combined_gate(args, gate)
        else:
            staging = _combined_staging_dir(args)
            run_combined_pipeline(args, gate, staging)
            _assert_contract_unchanged(args, gate, contract)
            _publish_staged_combined(args, staging)
            combined_gate = _load_combined_gate(args, gate)

        combined_paths = _combined_paths(args)
        stage2_result = args.stage2_output_dir / "merged_results.csv"
        payload["stage2"]["result"] = str(stage2_result)
        payload["stage2"]["result_sha256"] = _sha256(stage2_result)
        payload["combined"] = {
            **combined_gate.summary(),
            "artifacts": {
                name: {"path": str(path), "sha256": _sha256(path)}
                for name, path in (
                    ("merged", combined_paths["merged"]),
                    ("validation", combined_paths["validation"]),
                    ("metadata", combined_paths["model_dir"] / "metadata.json"),
                    ("r2", combined_paths["r2"]),
                )
            },
            "output_dir": str(args.combined_output_dir),
        }
        payload["status"] = "complete" if combined_gate.passed else "combined_r2_failed"
        payload.pop("last_error", None)
        payload.pop("last_attempt_at", None)
        payload.pop("resume_required", None)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        _require_claim_owned(claim, owner)
        _atomic_write_json(args.decision_output, payload)
    except Exception as exc:
        payload.pop("error", None)
        payload["last_error"] = str(exc)
        payload["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        payload["resume_required"] = True
        payload["status"] = "stage2_started"
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        if _claim_is_owned(claim, owner):
            _atomic_write_json(args.decision_output, payload)
        raise
    finally:
        _release_claim(claim, owner)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if payload["status"] == "complete" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
