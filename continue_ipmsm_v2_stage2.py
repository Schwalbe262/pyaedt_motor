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
import importlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
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
ADAPTIVE_CASE_MANIFEST_SCHEMA_VERSION = "ipmsm_v2_adaptive_enrichment_batch_v1"
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
    parser.add_argument(
        "--stage2-case-manifest",
        type=Path,
        help=(
            "Hash-bound adaptive batch manifest. Required when the training audit "
            "case plan differs from --stage2-case-plan."
        ),
    )
    parser.add_argument(
        "--training-audit-case-plan",
        type=Path,
        help=(
            "Fixed case plan whose preassigned test rows remain the audit cohort. "
            "Defaults to --stage2-case-plan for backward compatibility."
        ),
    )
    parser.add_argument("--stage2-output-dir", type=Path, required=True)
    parser.add_argument(
        "--precollected-stage2-completion",
        type=Path,
        help=(
            "Verified v4r9 acquisition completion used to adopt an already complete "
            "Stage2 output without resubmitting its cases."
        ),
    )
    parser.add_argument("--combined-output-dir", type=Path, required=True)
    parser.add_argument("--decision-output", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--scheduler-url", default=campaign_runner.submit_campaign.DEFAULT_SCHEDULER_URL)
    parser.add_argument("--project-active-cap", type=int, default=50)
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
    if not 1 <= args.project_active_cap <= 300:
        raise ContinuationGateError("--project-active-cap must be between 1 and 300")
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
        (_training_audit_case_plan(args), "training audit case plan"),
        (args.beta_summary, "beta summary"),
        (args.beta_case_plan, "beta case plan"),
        (args.beta_results, "beta results"),
        (args.beta_calibration_manifest, "beta calibration manifest"),
    ):
        if not path.is_file():
            raise ContinuationGateError(f"{label} is missing: {path}")
    _assert_nonoverlapping_case_plans(args.stage1_case_plan, args.stage2_case_plan)
    _validate_training_audit_coverage(args)
    _validate_stage2_case_manifest(args)
    _precollected_stage2_contract(args)
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


def _training_audit_case_plan(args: argparse.Namespace) -> Path:
    return args.training_audit_case_plan or args.stage2_case_plan


def _validate_training_audit_coverage(args: argparse.Namespace) -> None:
    audit_plan = _training_audit_case_plan(args)
    fields, rows = _read_csv(audit_plan, "training audit case plan")
    missing = sorted({"case_id", "doe_split"} - set(fields))
    if missing:
        raise ContinuationGateError(
            f"training audit case plan is missing columns: {missing}"
        )
    test_ids = [
        str(row.get("case_id") or "").strip()
        for row in rows
        if str(row.get("doe_split") or "").strip().lower() == "test"
    ]
    if not test_ids or "" in test_ids or len(test_ids) != len(set(test_ids)):
        raise ContinuationGateError(
            "training audit case plan must contain unique nonblank preassigned test case IDs"
        )
    combined_ids = set(
        _case_plan_ids(args.stage1_case_plan) + _case_plan_ids(args.stage2_case_plan)
    )
    missing_ids = sorted(set(test_ids) - combined_ids)
    if missing_ids:
        raise ContinuationGateError(
            "training audit test rows are absent from the combined case plans: "
            + ", ".join(missing_ids[:3])
        )


def _validate_stage2_case_manifest(args: argparse.Namespace) -> dict[str, str] | None:
    audit_plan = _training_audit_case_plan(args).resolve(strict=False)
    stage2_plan = args.stage2_case_plan.resolve(strict=False)
    manifest_path = args.stage2_case_manifest
    if manifest_path is None:
        if audit_plan != stage2_plan:
            raise ContinuationGateError(
                "--stage2-case-manifest is required when --training-audit-case-plan "
                "differs from --stage2-case-plan"
            )
        return None
    if not manifest_path.is_file():
        raise ContinuationGateError(
            f"Stage2 adaptive case manifest is missing: {manifest_path}"
        )
    initial_manifest_contract = _artifact_contract(manifest_path)
    manifest = _read_json(manifest_path, "Stage2 adaptive case manifest")
    failures: list[str] = []
    if manifest.get("schema_version") != ADAPTIVE_CASE_MANIFEST_SCHEMA_VERSION:
        failures.append("schema_version")
    if manifest.get("mode") != "write":
        failures.append("mode")
    expected_case_plan = _artifact_contract(args.stage2_case_plan)
    recorded_case_plan = {
        "path": str(Path(str(manifest.get("case_plan") or "")).resolve(strict=False)),
        "sha256": str(manifest.get("case_plan_sha256") or ""),
    }
    if recorded_case_plan != expected_case_plan:
        failures.append("case_plan")
    expected_audit = _artifact_contract(_training_audit_case_plan(args))
    if manifest.get("fixed_audit_case_plan") != expected_audit:
        failures.append("fixed_audit_case_plan")
    execution = manifest.get("execution_contract")
    if not isinstance(execution, Mapping):
        failures.append("execution_contract")
    else:
        expected_execution_fields = {
            "batch_index",
            "case_plan",
            "failed_decision",
            "fixed_audit_case_plan",
            "plateau_policy",
            "r2_history",
            "seed_policy",
        }
        if set(execution) != expected_execution_fields:
            failures.append("execution_contract.fields")
        if manifest.get("execution_contract_sha256") != _contract_sha256(execution):
            failures.append("execution_contract_sha256")
        if execution.get("case_plan") != expected_case_plan:
            failures.append("execution_contract.case_plan")
        if execution.get("fixed_audit_case_plan") != expected_audit:
            failures.append("execution_contract.fixed_audit_case_plan")
        plateau = execution.get("plateau_policy")
        batch_index = execution.get("batch_index")
        if (
            not isinstance(plateau, Mapping)
            or plateau.get("stop_fea") is not False
            or plateau.get("action") != "continue_adaptive_fea"
            or plateau.get("minimum_improvement") != 0.01
            or plateau.get("consecutive_batches_required") != 2
            or type(batch_index) is not int
            or batch_index < 1
            or plateau.get("completed_batches") != batch_index - 1
        ):
            failures.append("execution_contract.plateau_policy")
        for name in ("failed_decision", "r2_history"):
            record = execution.get(name)
            if not isinstance(record, Mapping) or set(record) != {"path", "sha256"}:
                failures.append(f"execution_contract.{name}")
                continue
            artifact_path = Path(str(record.get("path") or ""))
            if not artifact_path.is_file() or dict(record) != _artifact_contract(artifact_path):
                failures.append(f"execution_contract.{name}")
        seed_policy = execution.get("seed_policy")
        if not isinstance(seed_policy, Mapping) or type(batch_index) is not int:
            failures.append("execution_contract.seed_policy")
        else:
            stride = seed_policy.get("stride")
            adaptation_base = seed_policy.get("adaptation_seed_base")
            calibration_base = seed_policy.get("calibration_seed_base")
            if (
                stride != 100
                or seed_policy.get("formula") != "role_seed_base + 100 * batch_index"
                or type(adaptation_base) is not int
                or type(calibration_base) is not int
                or seed_policy.get("adaptation_seed") != adaptation_base + stride * batch_index
                or seed_policy.get("calibration_seed") != calibration_base + stride * batch_index
            ):
                failures.append("execution_contract.seed_policy")
    summary = manifest.get("summary")
    if not isinstance(summary, Mapping):
        failures.append("summary")
    else:
        if summary.get("rows") != _case_plan_count(args.stage2_case_plan):
            failures.append("summary.rows")
        if summary.get("split_groups") != {"train": 40, "calibration": 10, "test": 0}:
            failures.append("summary.split_groups")
        if summary.get("split_rows") != {"train": 240, "calibration": 60, "test": 0}:
            failures.append("summary.split_rows")
    if failures:
        raise ContinuationGateError(
            "Stage2 adaptive case manifest does not bind execution inputs: "
            + ", ".join(failures)
        )
    if _artifact_contract(manifest_path) != initial_manifest_contract:
        raise ContinuationGateError(
            "Stage2 adaptive case manifest changed during validation"
        )
    return initial_manifest_contract


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
        str(_training_audit_case_plan(args)),
    ]
    for column in FINGERPRINT_COLUMNS:
        argv.extend(("--expected-fingerprint", f"{column}={fingerprints[column]}"))
    return argv


def validate_stage2_readiness(args: argparse.Namespace) -> None:
    runner_args = campaign_runner.build_parser().parse_args(
        _stage2_runner_argv(args, submit=True)
    )
    readiness_args = runner_args
    if args.precollected_stage2_completion is not None:
        readiness_args = argparse.Namespace(**vars(runner_args))
        readiness_args.output_dir = args.stage2_output_dir.with_name(
            args.stage2_output_dir.name + ".precollected-readiness"
        )
        _assert_path_fresh(
            readiness_args.output_dir,
            "precollected Stage2 readiness output placeholder",
        )
    campaign_runner.validate_args(readiness_args)
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
        expected_audit_case_plan=_training_audit_case_plan(args),
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
        expected_audit_case_plan=_training_audit_case_plan(args),
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


def _bound_artifact_record(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ContinuationGateError(f"{label} must contain only path and sha256")
    path = Path(str(value.get("path") or ""))
    sha256 = str(value.get("sha256") or "")
    if not path.is_absolute() or len(sha256) != 64:
        raise ContinuationGateError(f"{label} has an invalid path or sha256")
    record = _artifact_contract(path)
    expected = {"path": str(path.resolve(strict=False)), "sha256": sha256}
    if record != expected:
        raise ContinuationGateError(f"{label} bytes changed")
    return record


def _v4r10_adapter_authority(
    value: object,
    acquisition: Any,
) -> dict[str, Any] | None:
    if isinstance(value, Mapping) and set(value) == {"path", "sha256"}:
        _bound_artifact_record(value, "precollected acquisition contract")
        return None
    expected_fields = {"path", "raw_sha256", "contract_sha256"}
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ContinuationGateError(
            "precollected acquisition contract fields changed"
        )
    path = Path(str(value["path"]))
    raw_sha256 = str(value["raw_sha256"])
    contract_sha256 = str(value["contract_sha256"])
    hex_digits = frozenset("0123456789abcdef")
    if (
        not path.is_absolute()
        or len(raw_sha256) != 64
        or not set(raw_sha256) <= hex_digits
        or len(contract_sha256) != 64
        or not set(contract_sha256) <= hex_digits
    ):
        raise ContinuationGateError(
            "precollected acquisition contract hashes are invalid"
        )
    raw_record = _bound_artifact_record(
        {"path": str(path), "sha256": raw_sha256},
        "precollected acquisition contract",
    )
    try:
        snapshot, document = acquisition.authority._strict_json_snapshot(
            raw_record["path"], "precollected acquisition contract"
        )
    except Exception as exc:
        raise ContinuationGateError(
            f"precollected acquisition contract cannot be inspected: {exc}"
        ) from exc
    if (
        snapshot.sha256 != raw_sha256
        or set(document) != {"schema_version", "contract_sha256", "recovery"}
        or document.get("schema_version")
        != acquisition.contract_builder.CONTRACT_SCHEMA_VERSION
    ):
        raise ContinuationGateError(
            "precollected acquisition contract raw authority changed"
        )
    unsigned = {
        "schema_version": document["schema_version"],
        "recovery": document["recovery"],
    }
    logical_sha256 = acquisition.authority.canonical_sha256(unsigned)
    if document.get("contract_sha256") != logical_sha256 or logical_sha256 != contract_sha256:
        raise ContinuationGateError(
            "precollected acquisition contract canonical authority changed"
        )

    def required_mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        item = parent.get(key)
        if not isinstance(item, Mapping):
            raise ContinuationGateError(
                f"precollected acquisition contract {key} binding disappeared"
            )
        return item

    recovery = required_mapping(document, "recovery")
    repository = required_mapping(recovery, "repository")
    sources = required_mapping(repository, "sources")
    execution = required_mapping(recovery, "execution")
    outputs = required_mapping(recovery, "outputs")
    source_root = Path(str(recovery.get("source_root") or "")).resolve(strict=True)
    runtime_root = Path(str(recovery.get("runtime_root") or "")).resolve(strict=True)
    if (
        source_root == runtime_root
        or Path(str(repository.get("source_root") or "")).resolve(strict=False)
        != source_root
    ):
        raise ContinuationGateError(
            "precollected acquisition source authority changed"
        )
    revision = str(repository.get("revision") or "")
    if len(revision) != 40 or not set(revision) <= hex_digits:
        raise ContinuationGateError(
            "precollected acquisition repository revision is invalid"
        )

    return {
        "contract_path": Path(raw_record["path"]),
        "contract_sha256": logical_sha256,
        "runtime_root": runtime_root,
        "source_root": source_root,
        "repository_revision": revision,
        "repository_sources": dict(sources),
        "completion": Path(str(outputs.get("completion") or "")).resolve(
            strict=False
        ),
        "scheduler": {
            "project": execution.get("project"),
            "project_active_cap": execution.get("project_active_cap"),
            "task_prefix": execution.get("task_prefix"),
            "url": execution.get("scheduler_url"),
        },
    }


_SEALED_COMPLETION_AUDIT = """\
import copy, json, os, sys
from pathlib import Path
source = Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(source))
import continue_ipmsm_v2_stage3_acquisition_v4r9 as acquisition
context = acquisition.load_contract(Path(sys.argv[2]))
sealed_output = context.outputs["campaign_output_dir"].resolve(strict=True)
original_validate = acquisition.campaign.collector.validate_args
def validate_completed(args):
    output = Path(args.output_dir)
    try:
        observed = output.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("completed collector output directory is unavailable") from exc
    if observed != sealed_output or not output.is_dir():
        raise RuntimeError("completed collector output differs from sealed authority")
    sentinel = output.parent / (output.name + ".v4r10-completed-validation-sentinel")
    if os.path.lexists(sentinel):
        raise RuntimeError("completed collector validation sentinel already exists")
    candidate = copy.copy(args)
    candidate.output_dir = sentinel
    original_validate(candidate)
    if os.path.lexists(sentinel):
        raise RuntimeError("collector validation unexpectedly created its sentinel")
acquisition.campaign.collector.validate_args = validate_completed
try:
    report = acquisition._verify_existing_completion(context)
finally:
    acquisition.campaign.collector.validate_args = original_validate
print(json.dumps({"context": {
    "completion": str(context.outputs["completion"]),
    "contract_sha256": context.contract_sha256,
    "project": context.project,
    "project_active_cap": context.project_active_cap,
    "repository_revision": context.repository_revision,
    "scheduler_url": context.scheduler_url,
    "source_root": str(context.source_root),
    "task_prefix": context.task_prefix,
}, "report": report}, sort_keys=True, separators=(",", ":")))
"""


def _audit_v4r10_completion(authority: Mapping[str, Any]) -> dict[str, Any]:
    command = [
        str(authority["executable"]),
        "-I",
        "-B",
        "-c",
        _SEALED_COMPLETION_AUDIT,
        str(authority["source_root"]),
        str(authority["contract_path"]),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=authority["runtime_root"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContinuationGateError(
            f"sealed acquisition completion audit could not run: {exc}"
        ) from exc
    if completed.returncode != 0 or completed.stderr:
        detail = (completed.stderr or completed.stdout).decode(
            "utf-8", errors="replace"
        )[:400]
        raise ContinuationGateError(
            "sealed acquisition completion audit failed: "
            f"returncode={completed.returncode} detail={detail}"
        )
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContinuationGateError(
            f"sealed acquisition completion audit returned invalid JSON: {exc}"
        ) from exc
    expected_context = {
        "completion": str(authority["completion"]),
        "contract_sha256": authority["contract_sha256"],
        "project": authority["scheduler"]["project"],
        "project_active_cap": authority["scheduler"]["project_active_cap"],
        "repository_revision": authority["repository_revision"],
        "scheduler_url": authority["scheduler"]["url"],
        "source_root": str(authority["source_root"]),
        "task_prefix": authority["scheduler"]["task_prefix"],
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"context", "report"}
        or payload.get("context") != expected_context
        or not isinstance(payload.get("report"), Mapping)
    ):
        raise ContinuationGateError(
            "sealed acquisition completion audit authority changed"
        )
    return dict(payload["report"])


def _successor_runner_source(
    acquisition: Any,
    sealed_revision: str,
) -> dict[str, Any]:
    source_root = Path(__file__).resolve(strict=True).parent
    acquisition_path = Path(acquisition.__file__).resolve(strict=True)
    if acquisition_path.parent != source_root:
        raise ContinuationGateError(
            "successor acquisition adapter is not co-located with the continuation"
        )

    def git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    revision_result = git("rev-parse", "HEAD")
    revision = revision_result.stdout.decode("ascii", errors="replace").strip().lower()
    if revision_result.returncode != 0 or len(revision) != 40:
        raise ContinuationGateError("successor Git revision cannot be proven")
    status_result = git("status", "--porcelain", "--untracked-files=no")
    if status_result.returncode != 0:
        raise ContinuationGateError("successor Git status cannot be proven")
    if status_result.stdout.strip():
        raise ContinuationGateError("successor Git source has tracked changes")
    if git("merge-base", "--is-ancestor", sealed_revision, revision).returncode != 0:
        raise ContinuationGateError(
            "successor Git revision does not descend from the sealed acquisition"
        )
    records: dict[str, dict[str, str]] = {}
    for name, path in {
        "acquisition": acquisition_path,
        "continuation": Path(__file__).resolve(strict=True),
    }.items():
        relative = path.relative_to(source_root).as_posix()
        committed = git("show", f"{revision}:{relative}")
        if committed.returncode != 0 or committed.stdout != path.read_bytes():
            raise ContinuationGateError(
                f"successor {name} source differs from revision {revision}"
            )
        records[name] = _artifact_contract(path)
    return {
        **records,
        "repository_revision": revision,
        "source_root": str(source_root),
    }


def _trusted_sealed_source_closure(
    acquisition: Any,
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        live_sources, snapshots = acquisition.contract_builder._source_provenance(
            authority["source_root"], authority["repository_revision"]
        )
    except Exception as exc:
        raise ContinuationGateError(
            f"sealed acquisition source closure cannot be proven: {exc}"
        ) from exc
    if live_sources != authority.get("repository_sources"):
        raise ContinuationGateError(
            "sealed acquisition live source closure differs from its contract"
        )
    try:
        trusted = {
            **dict(authority),
            "runner_path": Path(live_sources["runner"]["path"]),
            "sealed_continuation": Path(
                live_sources["stage2_continuation"]["path"]
            ),
            "executable": Path(live_sources["runner_executable"]["path"]),
            "source_snapshots": tuple(snapshots),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationGateError(
            f"sealed acquisition trusted source records are incomplete: {exc}"
        ) from exc
    return trusted


def _audit_trusted_v4r10_completion(
    acquisition: Any,
    authority: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    successor = _successor_runner_source(
        acquisition, str(authority["repository_revision"])
    )
    trusted = _trusted_sealed_source_closure(acquisition, authority)
    try:
        for snapshot in trusted["source_snapshots"]:
            acquisition.authority.assert_snapshot_unchanged(
                snapshot, snapshot.path.name
            )
    except Exception as exc:
        raise ContinuationGateError(
            f"sealed acquisition source changed before execution: {exc}"
        ) from exc
    report = _audit_v4r10_completion(trusted)
    return report, successor, trusted


def _quick_validate_precollected_stage2_contract(
    args: argparse.Namespace,
    contract: Mapping[str, Any],
) -> None:
    completion_path = args.precollected_stage2_completion
    if completion_path is None:
        raise ContinuationGateError("precollected Stage2 completion path disappeared")
    if _artifact_contract(completion_path) != contract.get("completion"):
        raise ContinuationGateError("precollected Stage2 completion bytes changed")
    completion = _read_json(completion_path, "precollected Stage2 completion")
    for key in (
        "contract",
        "effective_plan",
        "replacement_manifest",
        "repository_revision",
        "result",
        "scheduler",
        "schema_version",
        "status",
    ):
        if completion.get(key) != contract.get(key):
            raise ContinuationGateError(
                f"precollected Stage2 completion binding changed: {key}"
            )
    scheduler = contract.get("scheduler")
    if not isinstance(scheduler, Mapping):
        raise ContinuationGateError(
            "precollected Stage2 acquisition scheduler binding disappeared"
        )
    argument_scheduler = {
        "project": str(args.project),
        "project_active_cap": int(args.project_active_cap),
        "task_prefix": str(args.stage2_task_prefix),
        "url": str(args.scheduler_url),
    }
    bound_scheduler = {
        key: scheduler.get(key)
        for key in ("project", "project_active_cap", "task_prefix", "url")
    }
    if argument_scheduler != bound_scheduler:
        raise ContinuationGateError(
            "continuation scheduler identity differs from precollected acquisition"
        )
    runner_source = contract.get("runner_source")
    if not isinstance(runner_source, Mapping):
        raise ContinuationGateError(
            "precollected Stage2 runner source binding disappeared"
        )
    acquisition = importlib.import_module(
        "continue_ipmsm_v2_stage3_acquisition_v4r9"
    )

    current_continuation = Path(__file__).resolve(strict=True)
    current_acquisition = Path(acquisition.__file__).resolve(strict=True)
    adapter = _v4r10_adapter_authority(completion["contract"], acquisition)
    if adapter is None:
        source_root = Path(str(runner_source.get("source_root") or ""))
        if (
            not source_root.is_absolute()
            or current_continuation != source_root / "continue_ipmsm_v2_stage2.py"
            or current_acquisition
            != source_root / "continue_ipmsm_v2_stage3_acquisition_v4r9.py"
            or runner_source.get("repository_revision")
            != contract.get("repository_revision")
        ):
            raise ContinuationGateError(
                "precollected Stage2 runner modules moved outside the authoritative source root"
            )
        expected_sources = {
            "continuation": current_continuation,
            "acquisition": current_acquisition,
        }
    else:
        expected_fields = {
            "mode",
            "sealed_acquisition",
            "sealed_continuation",
            "sealed_repository_revision",
            "sealed_source_root",
            "successor_acquisition",
            "successor_continuation",
            "successor_repository_revision",
            "successor_source_root",
        }
        if set(runner_source) != expected_fields or runner_source.get(
            "mode"
        ) != "v4r10_sealed_successor_v1":
            raise ContinuationGateError(
                "precollected successor runner source fields changed"
            )
        if (
            runner_source["sealed_repository_revision"]
            != contract.get("repository_revision")
            or Path(runner_source["sealed_source_root"]) != adapter["source_root"]
            or Path(runner_source["successor_source_root"])
            != current_continuation.parent
        ):
            raise ContinuationGateError(
                "precollected successor repository provenance changed"
            )
        expected_sources = {
            "sealed_acquisition": adapter["source_root"]
            / "continue_ipmsm_v2_stage3_acquisition_v4r9.py",
            "sealed_continuation": adapter["source_root"]
            / "continue_ipmsm_v2_stage2.py",
            "successor_acquisition": current_acquisition,
            "successor_continuation": current_continuation,
        }
    for name, expected_path in expected_sources.items():
        record = _bound_artifact_record(
            runner_source.get(name), f"precollected {name} runner"
        )
        if Path(record["path"]) != expected_path:
            raise ContinuationGateError(
                f"precollected {name} runner path changed"
            )
    effective = completion["effective_plan"]
    result = completion["result"]
    _bound_artifact_record(
        {"path": effective["path"], "sha256": effective["sha256"]},
        "precollected effective plan",
    )
    _bound_artifact_record(
        {"path": result["path"], "sha256": result["sha256"]},
        "precollected result",
    )
    replacement_record = completion["replacement_manifest"]
    if replacement_record is not None:
        _bound_artifact_record(
            {
                "path": replacement_record["path"],
                "sha256": replacement_record["sha256"],
            },
            "precollected replacement manifest",
        )
        _bound_artifact_record(
            replacement_record["failure_evidence_manifest"],
            "precollected replacement failure evidence",
        )


def _precollected_stage2_contract(
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    completion_path = args.precollected_stage2_completion
    if completion_path is None:
        return None
    cached = getattr(args, "_precollected_stage2_contract_cache", None)
    if cached is not None:
        _quick_validate_precollected_stage2_contract(args, cached)
        return dict(cached)

    completion = _read_json(completion_path, "precollected Stage2 completion")
    try:
        acquisition = importlib.import_module(
            "continue_ipmsm_v2_stage3_acquisition_v4r9"
        )

        if (
            completion.get("schema_version") != acquisition.COMPLETION_SCHEMA_VERSION
            or completion.get("status") != "acquisition_complete"
        ):
            raise ContinuationGateError(
                "precollected Stage2 completion is not an authoritative v4r9 completion"
            )
        current_continuation = Path(__file__).resolve(strict=True)
        current_acquisition = Path(acquisition.__file__).resolve(strict=True)
        adapter = _v4r10_adapter_authority(
            completion.get("contract"), acquisition
        )
        if adapter is None:
            contract_record = _bound_artifact_record(
                completion.get("contract"), "precollected acquisition contract"
            )
            context = acquisition.load_contract(contract_record["path"])
            authoritative_completion = Path(context.outputs["completion"])
            authoritative_scheduler = {
                "project": context.project,
                "project_active_cap": context.project_active_cap,
                "task_prefix": context.task_prefix,
                "url": context.scheduler_url,
            }
            authoritative_source_root = context.source_root.resolve(strict=True)
            if (
                current_continuation
                != authoritative_source_root / "continue_ipmsm_v2_stage2.py"
                or current_acquisition
                != authoritative_source_root
                / "continue_ipmsm_v2_stage3_acquisition_v4r9.py"
            ):
                raise ContinuationGateError(
                    "precollected Stage2 runner modules are outside the exact source root"
                )
            repository_revision = context.repository_revision
            runner_source = {
                "acquisition": _artifact_contract(current_acquisition),
                "continuation": _artifact_contract(current_continuation),
                "repository_revision": repository_revision,
                "source_root": str(authoritative_source_root),
            }
        else:
            authoritative_completion = adapter["completion"]
            authoritative_scheduler = adapter["scheduler"]
            repository_revision = adapter["repository_revision"]
        if authoritative_completion.resolve(
            strict=False
        ) != completion_path.resolve(strict=False):
            raise ContinuationGateError(
                "precollected completion path differs from its acquisition contract"
            )
        argument_scheduler = {
            "project": str(args.project),
            "project_active_cap": int(args.project_active_cap),
            "task_prefix": str(args.stage2_task_prefix),
            "url": str(args.scheduler_url),
        }
        if argument_scheduler != authoritative_scheduler:
            raise ContinuationGateError(
                "continuation scheduler identity differs from precollected acquisition"
            )
        if adapter is None:
            live_report = acquisition._verify_existing_completion(context)
        else:
            live_report, successor, trusted_adapter = (
                _audit_trusted_v4r10_completion(acquisition, adapter)
            )
            runner_source = {
                "mode": "v4r10_sealed_successor_v1",
                "sealed_acquisition": _artifact_contract(
                    trusted_adapter["runner_path"]
                ),
                "sealed_continuation": _artifact_contract(
                    trusted_adapter["sealed_continuation"]
                ),
                "sealed_repository_revision": repository_revision,
                "sealed_source_root": str(adapter["source_root"]),
                "successor_acquisition": successor["acquisition"],
                "successor_continuation": successor["continuation"],
                "successor_repository_revision": successor[
                    "repository_revision"
                ],
                "successor_source_root": successor["source_root"],
            }
    except ContinuationGateError:
        raise
    except Exception as exc:
        raise ContinuationGateError(
            f"precollected Stage2 live verification failed: {exc}"
        ) from exc
    if live_report is None:
        raise ContinuationGateError("precollected Stage2 completion is not complete")
    expected_live = {
        "action": "verified_existing_completion",
        "history_tasks": live_report.get("history_tasks"),
        "mode": "execute",
        "plan_kind": live_report.get("plan_kind"),
        "schema_version": acquisition.RUN_REPORT_SCHEMA_VERSION,
        "status": "acquisition_complete",
        "successful_results": 300,
        "writes_performed": 0,
    }
    if live_report != expected_live:
        raise ContinuationGateError(
            "precollected Stage2 live verification report changed"
        )

    verified = _read_json(completion_path, "verified precollected Stage2 completion")
    if verified != completion:
        raise ContinuationGateError(
            "precollected Stage2 completion changed during live verification"
        )
    effective = verified.get("effective_plan")
    result = verified.get("result")
    replacement_record = verified.get("replacement_manifest")
    scheduler = verified.get("scheduler")
    if not isinstance(effective, Mapping) or set(effective) != {
        "geometry_groups",
        "kind",
        "path",
        "rows",
        "sha256",
    }:
        raise ContinuationGateError("precollected effective-plan record changed")
    if not isinstance(result, Mapping) or set(result) != {"path", "rows", "sha256"}:
        raise ContinuationGateError("precollected result record changed")
    if not isinstance(scheduler, Mapping) or {
        key: scheduler.get(key)
        for key in ("project", "project_active_cap", "task_prefix", "url")
    } != authoritative_scheduler:
        raise ContinuationGateError(
            "precollected completion scheduler identity changed"
        )
    if (
        effective.get("rows") != 300
        or effective.get("geometry_groups") != 50
        or effective.get("kind") not in {"original", "replacement"}
        or result.get("rows") != 300
        or live_report.get("plan_kind") != effective.get("kind")
    ):
        raise ContinuationGateError("precollected Stage2 300-row/50-group scope changed")
    effective_plan = _bound_artifact_record(
        {"path": effective["path"], "sha256": effective["sha256"]},
        "precollected effective plan",
    )
    result_record = _bound_artifact_record(
        {"path": result["path"], "sha256": result["sha256"]},
        "precollected result",
    )
    expected_result = args.stage2_output_dir / "merged_results.csv"
    if Path(effective_plan["path"]) != args.stage2_case_plan.resolve(strict=False):
        raise ContinuationGateError(
            "--stage2-case-plan is not the precollected effective plan"
        )
    if Path(result_record["path"]) != expected_result.resolve(strict=False):
        raise ContinuationGateError(
            "--stage2-output-dir does not contain the precollected result"
        )
    fields, plan_rows = _read_csv(args.stage2_case_plan, "precollected effective plan")
    if "geometry_group_id" not in fields or len(plan_rows) != 300:
        raise ContinuationGateError("precollected effective plan shape changed")
    plan_groups = {str(row.get("geometry_group_id") or "").strip() for row in plan_rows}
    if "" in plan_groups or len(plan_groups) != 50:
        raise ContinuationGateError("precollected effective plan group coverage changed")
    result_fields, result_rows = _validate_result_coverage(
        args.stage2_case_plan,
        expected_result,
        "precollected Stage2 result",
    )
    if "geometry_group_id" not in result_fields or len(result_rows) != 300:
        raise ContinuationGateError("precollected Stage2 result shape changed")
    result_groups = {
        str(row.get("geometry_group_id") or "").strip() for row in result_rows
    }
    if "" in result_groups or len(result_groups) != 50:
        raise ContinuationGateError("precollected Stage2 result group coverage changed")
    identity_fields = (
        "geometry_group_id",
        "design_hash",
        "operating_point_id",
        "doe_split",
        "repeat_of_case_id",
        "beta_calibration_id",
    )
    missing_plan_identity = sorted(set(identity_fields) - set(fields))
    missing_result_identity = sorted(set(identity_fields) - set(result_fields))
    if missing_plan_identity or missing_result_identity:
        raise ContinuationGateError(
            "precollected Stage2 identity columns changed: "
            f"plan_missing={missing_plan_identity} result_missing={missing_result_identity}"
        )
    identity_drift = [
        str(plan_row.get("case_id") or "").strip()
        for plan_row, result_row in zip(plan_rows, result_rows)
        if any(
            str(plan_row.get(column) or "").strip()
            != str(result_row.get(column) or "").strip()
            for column in identity_fields
        )
    ]
    if identity_drift:
        raise ContinuationGateError(
            "precollected Stage2 result differs from its effective-plan identity: "
            + str(identity_drift[:3])
        )
    if effective.get("kind") == "original" and replacement_record is not None:
        raise ContinuationGateError("original completion unexpectedly binds a replacement")
    if effective.get("kind") == "replacement" and not isinstance(
        replacement_record, Mapping
    ):
        raise ContinuationGateError("replacement completion lacks replacement evidence")

    bound: dict[str, Any] = {
        "completion": _artifact_contract(completion_path),
        "contract": dict(verified["contract"]),
        "effective_plan": dict(effective),
        "live_verification": expected_live,
        "replacement_manifest": (
            dict(replacement_record) if isinstance(replacement_record, Mapping) else None
        ),
        "repository_revision": verified.get("repository_revision"),
        "result": dict(result),
        "runner_source": runner_source,
        "scheduler": dict(scheduler),
        "schema_version": verified.get("schema_version"),
        "status": verified.get("status"),
    }
    setattr(args, "_precollected_stage2_contract_cache", bound)
    _quick_validate_precollected_stage2_contract(args, bound)
    return dict(bound)


def _execution_contract(args: argparse.Namespace, gate: GateResult) -> dict[str, Any]:
    case_manifest = _validate_stage2_case_manifest(args)
    precollected = _precollected_stage2_contract(args)
    stage2_contract: dict[str, Any] = {
        "case_plan": _artifact_contract(args.stage2_case_plan),
        "output_dir": str(args.stage2_output_dir.resolve(strict=False)),
        "runner_argv": _stage2_runner_argv(args, submit=True),
    }
    if case_manifest is not None:
        stage2_contract["case_manifest"] = case_manifest
    if precollected is not None:
        stage2_contract["precollected_completion"] = precollected
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
        "stage2": stage2_contract,
        "training": {
            "audit_case_plan": _artifact_contract(_training_audit_case_plan(args)),
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
    stage2_payload: dict[str, Any] = {
        "beta": contract["beta"],
        "case_plan": str(args.stage2_case_plan),
        "case_plan_sha256": _sha256(args.stage2_case_plan),
        "output_dir": str(args.stage2_output_dir),
        "runner_argv": _stage2_runner_argv(args, submit=True),
    }
    if args.stage2_case_manifest is not None:
        stage2_payload["case_manifest"] = str(args.stage2_case_manifest)
        stage2_payload["case_manifest_sha256"] = _sha256(
            args.stage2_case_manifest
        )
    if args.precollected_stage2_completion is not None:
        stage2_payload["precollected_completion"] = contract["stage2"][
            "precollected_completion"
        ]
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
        "stage2": stage2_payload,
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
    if args.precollected_stage2_completion is not None and gate.decision != "run_stage2":
        raise ContinuationGateError(
            "--precollected-stage2-completion is valid only when the gate requires Stage2"
        )

    if gate.decision == "run_stage2":
        if not args.resume:
            if args.precollected_stage2_completion is None:
                _assert_path_fresh(args.stage2_output_dir, "Stage2 output directory")
            else:
                _stage2_output_state(args)
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
            if args.precollected_stage2_completion is not None:
                raise ContinuationGateError(
                    "precollected Stage2 output disappeared; resubmission is forbidden"
                )
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
