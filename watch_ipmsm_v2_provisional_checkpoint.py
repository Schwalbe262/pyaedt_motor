"""Build one isolated, non-production Stage1 60-design learning checkpoint.

Inspection is read-only by default.  ``--execute`` polls the read-only
scheduler view until an exact settled 60-design split is ready, snapshots only
those base rows through the existing rate-limited collector, validates and
trains a no-tuning ensemble, and publishes a final decision/manifest pair.
The resulting model directory is deliberately guarded against production
surrogate loading even when its diagnostic R2 values happen to exceed 0.95.
"""

from __future__ import annotations

import argparse
import copy
import csv
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

from atomic_publish import publish_no_replace
import collect_ipmsm_v2_campaign as collector
import continue_ipmsm_v2_stage2 as continuation
import ipmsm_surrogate_bundle as surrogate_bundle
import snapshot_ipmsm_v2_partial_results as snapshotter
import submit_ipmsm_v2_campaign as submitter
import supervise_ipmsm_v2_pipeline as supervisor


SCHEMA_VERSION = "ipmsm-v2-provisional-checkpoint-v1"
MANIFEST_SCHEMA_VERSION = "ipmsm-v2-provisional-checkpoint-manifest-v1"
TARGET_DESIGNS = 60
ROWS_PER_DESIGN = 6
EXPECTED_ROWS = TARGET_DESIGNS * ROWS_PER_DESIGN
EXPECTED_REPEATS = 0
MIN_SPLIT_COUNTS = {"train": 30, "calibration": 10, "test": 10}
MIN_DIAGNOSTIC_SCOPE = "provisional_minimum"
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_OVERALL_TIMEOUT_SECONDS = 6.0 * 60.0 * 60.0
MAX_OVERALL_TIMEOUT_SECONDS = 24.0 * 60.0 * 60.0
CHILD_TIMEOUT_SECONDS = 4.0 * 60.0 * 60.0


class CheckpointError(RuntimeError):
    """The provisional checkpoint cannot safely continue."""


@dataclass(frozen=True)
class CheckpointPaths:
    root: Path
    snapshot: Path
    selected_plan: Path
    merged: Path
    snapshot_manifest: Path
    validation: Path
    models: Path
    raw_metadata: Path
    guarded_metadata: Path
    r2: Path
    decision: Path
    manifest: Path
    model_staging: Path
    validation_staging: Path
    lock: Path
    pid: Path


@dataclass(frozen=True)
class Readiness:
    ready: bool
    active: int
    scheduler_successful: int
    complete_designs_available: int
    selected_designs: int
    selected_rows: int
    repeat_rows: int
    split_design_counts: dict[str, int]
    diagnostic_scope: str
    missing: int
    retryable: int

    def as_mapping(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "complete_designs_available": self.complete_designs_available,
            "diagnostic_scope": self.diagnostic_scope,
            "missing": self.missing,
            "ready": self.ready,
            "repeat_rows": self.repeat_rows,
            "retryable": self.retryable,
            "scheduler_successful": self.scheduler_successful,
            "selected_designs": self.selected_designs,
            "selected_rows": self.selected_rows,
            "split_design_counts": dict(self.split_design_counts),
        }


@dataclass(frozen=True)
class BoundContract:
    contract: supervisor.PipelineContract
    document_sha256: str
    helper_sha256: dict[str, str] = field(default_factory=dict)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--overall-timeout-seconds",
        type=float,
        default=DEFAULT_OVERALL_TIMEOUT_SECONDS,
    )
    return parser


def _sha256(path: Path) -> str:
    return supervisor._file_sha256(path)


def _resolve_from_workdir(path: Path, workdir: Path) -> Path:
    candidate = path if path.is_absolute() else workdir / path
    return candidate.resolve(strict=False)


def make_paths(root: Path, pid_file: Path | None = None) -> CheckpointPaths:
    root = root.resolve(strict=False)
    sibling = root.parent
    pid = (
        pid_file.resolve(strict=False)
        if pid_file is not None
        else sibling / f".{root.name}.checkpoint.pid.json"
    )
    return CheckpointPaths(
        root=root,
        snapshot=root / "snapshot",
        selected_plan=root / "snapshot" / collector.SELECTED_PLAN_NAME,
        merged=root / "snapshot" / "merged_results.csv",
        snapshot_manifest=(
            root / "snapshot" / snapshotter.SNAPSHOT_MANIFEST_NAME
        ),
        validation=root / "validation.csv",
        models=root / "models",
        raw_metadata=root / "models" / "training_metadata.json",
        guarded_metadata=root / "models" / "metadata.json",
        r2=root / "models" / "r2.csv",
        decision=root / "decision.json",
        manifest=root / "manifest.json",
        model_staging=root / ".models.staging",
        validation_staging=root / ".validation.csv.staging",
        lock=sibling / f".{root.name}.checkpoint.lock",
        pid=pid,
    )


def validate_cli(args: argparse.Namespace) -> None:
    if args.resume and not args.execute:
        raise CheckpointError("--resume requires --execute")
    if not math.isfinite(args.poll_interval_seconds) or not (
        1.0 <= args.poll_interval_seconds <= 600.0
    ):
        raise CheckpointError("--poll-interval-seconds must be finite and between 1 and 600")
    if not math.isfinite(args.overall_timeout_seconds) or not (
        args.poll_interval_seconds <= args.overall_timeout_seconds <= MAX_OVERALL_TIMEOUT_SECONDS
    ):
        raise CheckpointError(
            "--overall-timeout-seconds must be finite, at least one poll interval, and <= 86400"
        )


def load_bound_contract(path: Path) -> BoundContract:
    contract = supervisor.load_contract(path)
    supervisor.audit_immutable_inputs(contract)
    helpers = {
        "snapshot": _sha256(contract.workdir / "snapshot_ipmsm_v2_partial_results.py"),
        "watcher": _sha256(Path(__file__).resolve(strict=True)),
    }
    return BoundContract(
        contract=contract,
        document_sha256=_sha256(contract.source),
        helper_sha256=helpers,
    )


def assert_contract_bound(bound: BoundContract) -> None:
    current = supervisor.load_contract(bound.contract.source)
    if current.contract_sha256 != bound.contract.contract_sha256:
        raise CheckpointError("pipeline contract identity changed during checkpoint execution")
    if _sha256(current.source) != bound.document_sha256:
        raise CheckpointError("pipeline contract document changed during checkpoint execution")
    supervisor.audit_immutable_inputs(current)
    helper_paths = {
        "snapshot": current.workdir / "snapshot_ipmsm_v2_partial_results.py",
        "watcher": Path(__file__).resolve(strict=True),
    }
    for name, digest in bound.helper_sha256.items():
        if name not in helper_paths or _sha256(helper_paths[name]) != digest:
            raise CheckpointError(f"checkpoint helper changed during execution: {name}")


def _overlaps(left: Path, right: Path) -> bool:
    a = left.resolve(strict=False)
    b = right.resolve(strict=False)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def validate_paths(paths: CheckpointPaths, contract: supervisor.PipelineContract) -> None:
    snapshotter.validate_output_dir(paths.root, contract)
    protected = [
        contract.source,
        contract.lock_path,
        contract.stage1.case_plan,
        contract.stage1.output_dir,
        contract.stage1.result,
        contract.stage1.validation,
        contract.stage1.model_dir,
        contract.stage1.r2,
        contract.stage2.decision,
        contract.stage3.prior_plan,
        contract.stage3.prior_manifest,
        contract.stage3.plan,
        contract.stage3.manifest,
        contract.stage3.decision,
        contract.optimization.decision,
        contract.speed.plan,
        contract.speed.output_dir,
        contract.speed.result,
        contract.speed.rank,
        contract.speed.top,
        contract.speed.marker,
        *(item.path for item in contract.immutable_inputs),
        *(item.path for item in contract.external_pid_files),
    ]
    for candidate in protected:
        if _overlaps(paths.root, candidate):
            raise CheckpointError(f"checkpoint output aliases protected path: {candidate}")
    internal = [
        paths.snapshot,
        paths.validation,
        paths.models,
        paths.decision,
        paths.manifest,
        paths.model_staging,
        paths.validation_staging,
        paths.lock,
        paths.pid,
    ]
    resolved = [item.resolve(strict=False) for item in internal]
    if len(resolved) != len(set(resolved)):
        raise CheckpointError("checkpoint output, staging, lock, and PID paths must be distinct")
    for process_path in (paths.lock, paths.pid):
        if _overlaps(paths.root, process_path):
            raise CheckpointError("checkpoint lock/PID path must stay outside the output directory")
        for candidate in protected:
            if _overlaps(process_path, candidate):
                raise CheckpointError(
                    f"checkpoint lock/PID path aliases protected path: {candidate}"
                )


def inspect_readiness(
    contract: supervisor.PipelineContract,
    *,
    now: Any | None = None,
) -> Readiness:
    campaign_args = snapshotter._campaign_args(contract)
    validated_rows = submitter.load_and_validate_cases(
        campaign_args.cases,
        campaign_args.max_plan_cases,
        False,
    )
    selected_rows = submitter.select_case_rows(
        validated_rows,
        campaign_args.case_start_index,
        campaign_args.case_limit,
    )
    tasks = submitter.build_campaign_tasks(
        campaign_args,
        selected_rows,
        first_row_number=campaign_args.case_start_index,
    )
    scheduler = snapshotter.runner.read_scheduler_snapshot(campaign_args)
    state = snapshotter.runner.classify_campaign_state(
        tasks,
        scheduler.history,
        campaign_args.project,
        {},
        campaign_args.terminal_retry_limit,
    )
    if now is None:
        now = snapshotter.datetime.now(snapshotter.timezone.utc)
    settled = snapshotter.settled_successful_results(
        state=state,
        history=scheduler.history,
        project=campaign_args.project,
        selected_rows=selected_rows,
        first_row_number=campaign_args.case_start_index,
        settle_seconds=campaign_args.completed_result_settle_seconds,
        now=now,
    )
    chosen, complete_groups, chosen_groups = snapshotter.select_complete_designs(
        tasks=tasks,
        selected_rows=selected_rows,
        settled=settled,
        max_designs=TARGET_DESIGNS,
    )
    base = [
        item
        for item in chosen
        if not str(item.plan_row.get("repeat_of_case_id") or "").strip()
    ]
    counts = snapshotter.split_counts(base) if base else {
        "train": 0,
        "calibration": 0,
        "test": 0,
    }
    scope = snapshotter.diagnostic_scope(len(chosen_groups), counts)
    per_group: dict[str, int] = {}
    for item in base:
        group = str(item.plan_row.get("geometry_group_id") or "").strip()
        per_group[group] = per_group.get(group, 0) + 1
    ready = bool(
        len(chosen_groups) == TARGET_DESIGNS
        and len(base) == EXPECTED_ROWS
        and all(per_group.get(group) == ROWS_PER_DESIGN for group in chosen_groups)
        and all(counts.get(name, 0) >= minimum for name, minimum in MIN_SPLIT_COUNTS.items())
        and scope in {"provisional_minimum", "provisional_stronger"}
    )
    return Readiness(
        ready=ready,
        active=len(state.active),
        scheduler_successful=len(state.successful),
        complete_designs_available=len(complete_groups),
        selected_designs=len(chosen_groups),
        selected_rows=len(base),
        repeat_rows=EXPECTED_REPEATS,
        split_design_counts=counts,
        diagnostic_scope=scope,
        missing=len(state.missing),
        retryable=len(state.retryable),
    )


def _replace_flag_value(argv: Sequence[str], flag: str, value: Path | str) -> list[str]:
    result = list(argv)
    indices = [index for index, item in enumerate(result) if item == flag]
    if len(indices) != 1 or indices[0] + 1 >= len(result):
        raise CheckpointError(f"immutable argv must contain exactly one {flag}")
    index = indices[0]
    if result[index + 1].startswith("--"):
        raise CheckpointError(f"immutable argv has no value for {flag}")
    result[index + 1] = str(value)
    return result


def build_snapshot_argv(
    contract: supervisor.PipelineContract,
    paths: CheckpointPaths,
) -> list[str]:
    python = contract.stage1.validation_argv[0]
    return [
        python,
        "snapshot_ipmsm_v2_partial_results.py",
        "--contract",
        str(contract.source),
        "--output-dir",
        str(paths.snapshot),
        "--max-designs",
        str(TARGET_DESIGNS),
        "--require-exact-designs",
        "--base-only",
        "--require-exact-rows",
        str(EXPECTED_ROWS),
        "--minimum-diagnostic-scope",
        MIN_DIAGNOSTIC_SCOPE,
    ]


def build_validation_argv(
    contract: supervisor.PipelineContract,
    paths: CheckpointPaths,
) -> list[str]:
    argv = _replace_flag_value(contract.stage1.validation_argv, "--data", paths.merged)
    return _replace_flag_value(argv, "--summary", paths.validation_staging)


def build_training_argv(
    contract: supervisor.PipelineContract,
    paths: CheckpointPaths,
) -> list[str]:
    argv = _replace_flag_value(contract.stage1.training_argv, "--data", paths.merged)
    argv = _replace_flag_value(argv, "--model-dir", paths.model_staging)
    argv = _replace_flag_value(argv, "--verification-output", paths.model_staging / "r2.csv")
    argv = [item for item in argv if item not in {"--fail-on-threshold", "--enable-tuning", "--disable-tuning"}]
    if "--v2" not in argv:
        raise CheckpointError("Stage1 training argv lost --v2")
    if "--v2-audit-case-plan" in argv:
        argv = _replace_flag_value(argv, "--v2-audit-case-plan", paths.selected_plan)
    else:
        argv.extend(["--v2-audit-case-plan", str(paths.selected_plan)])
    argv.append("--disable-tuning")
    parsed = __import__("train_ipmsm_lightgbm").build_parser().parse_args(argv[2:])
    if not parsed.v2 or parsed.enable_tuning or parsed.fail_on_threshold:
        raise CheckpointError("provisional training safety flags are inconsistent")
    if parsed.ensemble_size != contract.stage1.ensemble_size or parsed.ensemble_size != 5:
        raise CheckpointError("provisional training must preserve ensemble_size=5")
    if not math.isclose(
        parsed.conformal_coverage,
        contract.stage1.conformal_coverage,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise CheckpointError("provisional training changed conformal coverage")
    if parsed.max_invalid_training_rows != 0 or parsed.max_removed_output_outlier_rows != 0:
        raise CheckpointError("provisional training must preserve zero-invalid quality gates")
    return argv


def _last_json(stdout: str, label: str) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise CheckpointError(f"{label} returned no JSON evidence")
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise CheckpointError(f"{label} did not end with JSON evidence") from exc
    if not isinstance(value, dict):
        raise CheckpointError(f"{label} JSON evidence must be an object")
    return value


def run_child(argv: Sequence[str], *, workdir: Path, label: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=workdir,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckpointError(
            f"{label} exceeded the bounded child timeout of {CHILD_TIMEOUT_SECONDS:.0f}s"
        ) from exc
    if completed.returncode != 0:
        stderr = next(
            (line.strip() for line in reversed(completed.stderr.splitlines()) if line.strip()),
            "",
        )
        raise CheckpointError(
            f"{label} returned {completed.returncode}" + (f": {stderr[:400]}" if stderr else "")
        )
    return completed


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CheckpointError(f"cannot audit CSV {path}: {exc}") from exc
    if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise CheckpointError(f"CSV has a missing or duplicate header: {path}")
    return list(reader.fieldnames), rows


def _expect_json_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise CheckpointError(f"{label} keys differ: missing={missing}, extra={extra}")


def _json_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CheckpointError(f"{label} must be an object")
    return dict(value)


def _exact_manifest_int(value: Any, expected: int, label: str) -> None:
    if type(value) is not int or value != expected:
        raise CheckpointError(f"{label} must be exactly {expected}")


def audit_snapshot_manifest(
    paths: CheckpointPaths,
    bound: BoundContract,
) -> dict[str, Any]:
    manifest = _read_json(paths.snapshot_manifest, "checkpoint snapshot manifest")
    _expect_json_keys(
        manifest,
        {
            "artifacts",
            "contract",
            "counts",
            "diagnostic_scope",
            "official_gate_eligible",
            "producer",
            "schema_version",
        },
        "snapshot manifest",
    )
    if manifest["schema_version"] != snapshotter.SNAPSHOT_MANIFEST_SCHEMA_VERSION:
        raise CheckpointError("snapshot manifest schema_version changed")
    if manifest["official_gate_eligible"] is not False:
        raise CheckpointError("snapshot manifest must remain ineligible for the official gate")

    producer = _json_mapping(manifest["producer"], "snapshot manifest.producer")
    _expect_json_keys(producer, {"path", "sha256"}, "snapshot manifest.producer")
    expected_producer_path = (
        bound.contract.workdir / "snapshot_ipmsm_v2_partial_results.py"
    ).resolve(strict=False)
    expected_producer_sha256 = bound.helper_sha256.get("snapshot")
    if (
        expected_producer_sha256 is None
        or Path(str(producer["path"])).resolve(strict=False) != expected_producer_path
        or producer["sha256"] != expected_producer_sha256
        or _sha256(expected_producer_path) != expected_producer_sha256
    ):
        raise CheckpointError("snapshot manifest producer binding changed")

    contract = _json_mapping(manifest["contract"], "snapshot manifest.contract")
    _expect_json_keys(
        contract,
        {
            "canonical_sha256",
            "document_path",
            "document_sha256",
            "source_case_plan_path",
            "source_case_plan_sha256",
        },
        "snapshot manifest.contract",
    )
    expected_contract_path = bound.contract.source.resolve(strict=False)
    expected_plan_path = bound.contract.stage1.case_plan.resolve(strict=False)
    try:
        recorded_contract_path = Path(str(contract["document_path"])).resolve(strict=False)
        recorded_plan_path = Path(str(contract["source_case_plan_path"])).resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise CheckpointError("snapshot manifest contains an invalid bound path") from exc
    if (
        contract["canonical_sha256"] != bound.contract.contract_sha256
        or contract["document_sha256"] != bound.document_sha256
        or recorded_contract_path != expected_contract_path
        or contract["source_case_plan_sha256"] != _sha256(expected_plan_path)
        or recorded_plan_path != expected_plan_path
    ):
        raise CheckpointError("snapshot manifest contract/source-plan binding changed")

    artifacts = _json_mapping(manifest["artifacts"], "snapshot manifest.artifacts")
    _expect_json_keys(
        artifacts,
        {"merged_results", "selected_plan"},
        "snapshot manifest.artifacts",
    )
    expected_artifacts = {
        "merged_results": ("merged_results.csv", paths.merged),
        "selected_plan": (collector.SELECTED_PLAN_NAME, paths.selected_plan),
    }
    for name, (relative_path, actual_path) in expected_artifacts.items():
        record = _json_mapping(artifacts[name], f"snapshot manifest.artifacts.{name}")
        _expect_json_keys(record, {"path", "sha256"}, f"snapshot manifest.artifacts.{name}")
        if record["path"] != relative_path or record["sha256"] != _sha256(actual_path):
            raise CheckpointError(f"snapshot manifest artifact binding changed: {name}")

    counts = _json_mapping(manifest["counts"], "snapshot manifest.counts")
    _expect_json_keys(
        counts,
        {
            "complete_designs_available",
            "repeat_rows",
            "result_files",
            "result_rows",
            "selected_designs",
            "selected_rows",
            "split_design_counts",
        },
        "snapshot manifest.counts",
    )
    for name, expected in {
        "repeat_rows": EXPECTED_REPEATS,
        "result_files": EXPECTED_ROWS,
        "result_rows": EXPECTED_ROWS,
        "selected_designs": TARGET_DESIGNS,
        "selected_rows": EXPECTED_ROWS,
    }.items():
        _exact_manifest_int(counts[name], expected, f"snapshot manifest.counts.{name}")
    available = counts["complete_designs_available"]
    if type(available) is not int or available < TARGET_DESIGNS:
        raise CheckpointError("snapshot manifest complete-design count is below 60")
    split_counts = _json_mapping(
        counts["split_design_counts"],
        "snapshot manifest.counts.split_design_counts",
    )
    _expect_json_keys(
        split_counts,
        set(MIN_SPLIT_COUNTS),
        "snapshot manifest.counts.split_design_counts",
    )
    if (
        any(type(split_counts[name]) is not int for name in MIN_SPLIT_COUNTS)
        or sum(split_counts.values()) != TARGET_DESIGNS
        or any(split_counts[name] < minimum for name, minimum in MIN_SPLIT_COUNTS.items())
    ):
        raise CheckpointError("snapshot manifest split counts are below the provisional contract")
    scope = manifest["diagnostic_scope"]
    if scope not in {"provisional_minimum", "provisional_stronger"}:
        raise CheckpointError("snapshot manifest diagnostic scope is below provisional minimum")
    return manifest


def audit_snapshot(paths: CheckpointPaths, bound: BoundContract) -> Readiness:
    assert_contract_bound(bound)
    if not paths.snapshot.is_dir():
        raise CheckpointError("checkpoint snapshot directory is missing")
    manifest = audit_snapshot_manifest(paths, bound)
    _, plan_rows = _read_csv(paths.selected_plan)
    _, merged_rows = _read_csv(paths.merged)
    if len(plan_rows) != EXPECTED_ROWS or len(merged_rows) != EXPECTED_ROWS:
        raise CheckpointError("checkpoint snapshot must contain exactly 360 plan/result rows")
    plan_ids = [str(row.get("case_id") or "").strip() for row in plan_rows]
    result_ids = [str(row.get("case_id") or "").strip() for row in merged_rows]
    if "" in plan_ids or len(set(plan_ids)) != EXPECTED_ROWS or result_ids != plan_ids:
        raise CheckpointError("checkpoint snapshot case identity/order changed")
    if any(str(row.get("repeat_of_case_id") or "").strip() for row in plan_rows):
        raise CheckpointError("checkpoint snapshot must contain base rows only")
    if any(str(row.get("status") or "").strip().lower() != "ok" for row in merged_rows):
        raise CheckpointError("checkpoint snapshot contains a non-ok result")
    groups: dict[str, list[dict[str, str]]] = {}
    for row in plan_rows:
        group = str(row.get("geometry_group_id") or "").strip()
        groups.setdefault(group, []).append(row)
    if "" in groups or len(groups) != TARGET_DESIGNS:
        raise CheckpointError("checkpoint snapshot must contain exactly 60 geometry groups")
    if any(len(rows) != ROWS_PER_DESIGN for rows in groups.values()):
        raise CheckpointError("checkpoint snapshot must contain six base rows per design")
    counts: dict[str, int] = {name: 0 for name in MIN_SPLIT_COUNTS}
    for group, rows in groups.items():
        values = {str(row.get("doe_split") or "").strip().lower() for row in rows}
        if len(values) != 1 or next(iter(values)) not in counts:
            raise CheckpointError(f"checkpoint geometry has invalid split identity: {group}")
        counts[next(iter(values))] += 1
    scope = snapshotter.diagnostic_scope(len(groups), counts)
    if any(counts[name] < minimum for name, minimum in MIN_SPLIT_COUNTS.items()):
        raise CheckpointError("checkpoint snapshot split is below provisional minimum")
    result_files = sorted((paths.snapshot / "results").glob("*.csv"))
    if len(result_files) != EXPECTED_ROWS:
        raise CheckpointError("checkpoint snapshot must contain exactly 360 individual results")
    try:
        _, reconstructed = collector.merge_complete_results(paths.selected_plan, result_files)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise CheckpointError(f"cannot reconstruct checkpoint snapshot: {exc}") from exc
    if reconstructed != merged_rows:
        raise CheckpointError("checkpoint merged result differs from its individual result files")
    collector.validate_homogeneous_fingerprints(merged_rows)
    manifest_counts = _json_mapping(manifest["counts"], "snapshot manifest.counts")
    if (
        manifest_counts["split_design_counts"] != counts
        or manifest["diagnostic_scope"] != scope
    ):
        raise CheckpointError("snapshot manifest counts/scope differ from selected plan")
    return Readiness(
        ready=True,
        active=0,
        scheduler_successful=0,
        complete_designs_available=manifest_counts["complete_designs_available"],
        selected_designs=TARGET_DESIGNS,
        selected_rows=EXPECTED_ROWS,
        repeat_rows=EXPECTED_REPEATS,
        split_design_counts=counts,
        diagnostic_scope=scope,
        missing=0,
        retryable=0,
    )


def audit_validation(path: Path) -> None:
    _, rows = _read_csv(path)
    if len(rows) != 1:
        raise CheckpointError("checkpoint validation must contain exactly one row")
    row = rows[0]
    expected = {
        "rows": EXPECTED_ROWS,
        "ok_rows": EXPECTED_ROWS,
        "unique_case_ids": EXPECTED_ROWS,
        "unique_geometry_groups": TARGET_DESIGNS,
        "repeat_pairs": EXPECTED_REPEATS,
        "failures": 0,
    }
    try:
        mismatches = [key for key, value in expected.items() if int(row.get(key, "")) != value]
    except (TypeError, ValueError) as exc:
        raise CheckpointError("checkpoint validation contains a non-integer count") from exc
    if row.get("status") != "pass" or mismatches:
        raise CheckpointError(f"checkpoint validation exact counts failed: {mismatches}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key!r}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise CheckpointError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CheckpointError(f"{label} must be a JSON object")
    return value


def _write_json_no_replace(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        publish_no_replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def guard_training_metadata(
    staging: Path,
    *,
    contract_sha256: str,
    selected_plan_sha256: str,
    merged_results_sha256: str,
) -> None:
    generated = staging / "metadata.json"
    raw = staging / "training_metadata.json"
    if raw.exists() or not generated.is_file():
        raise CheckpointError("trainer metadata publication state is unsafe")
    publish_no_replace(generated, raw)
    generated.unlink(missing_ok=True)
    raw_bytes_hash = _sha256(raw)
    raw_value = _read_json(raw, "raw training metadata")
    guarded = dict(raw_value)
    guarded["provisional"] = True
    guarded["official_gate_eligible"] = False
    guarded["provisional_source_contract_sha256"] = contract_sha256
    guarded["provisional_selected_plan_sha256"] = selected_plan_sha256
    guarded["provisional_merged_results_sha256"] = merged_results_sha256
    guarded["provisional_training_metadata"] = {
        "path": "training_metadata.json",
        "sha256": raw_bytes_hash,
    }
    guarded["provisional_actual_gate_flags"] = {
        "primary_test_r2_gate_passed": raw_value.get("primary_test_r2_gate_passed"),
        "voltage_test_r2_gate_passed": raw_value.get("voltage_test_r2_gate_passed"),
    }
    guarded["primary_test_r2_gate_passed"] = False
    guarded["voltage_test_r2_gate_passed"] = False
    _write_json_no_replace(generated, guarded)
    if _sha256(raw) != raw_bytes_hash:
        raise CheckpointError("raw training metadata changed while building its guard")


def _audit_artifact_records(metadata: Mapping[str, Any], model_dir: Path) -> None:
    for field in ("model_artifacts", "training_artifacts"):
        raw_records = metadata.get(field)
        if not isinstance(raw_records, Mapping) or not raw_records:
            raise CheckpointError(f"raw metadata.{field} must be a nonempty object")
        for name, value in raw_records.items():
            if not isinstance(value, Mapping):
                raise CheckpointError(f"raw metadata.{field}.{name} must be an object")
            recorded_path = str(value.get("path") or "").strip()
            digest = str(value.get("sha256") or "").strip().lower()
            artifact = model_dir / Path(recorded_path).name
            if len(digest) != 64 or not artifact.is_file() or _sha256(artifact) != digest:
                raise CheckpointError(f"raw metadata artifact changed: {field}.{name}")


def _relocated_metadata_for_audit(
    metadata: Mapping[str, Any],
    model_dir: Path,
) -> dict[str, Any]:
    """Relocate only an ephemeral audit view; the trainer metadata stays byte-exact."""

    relocated = copy.deepcopy(dict(metadata))
    for field in ("model_paths", "auxiliary_model_paths"):
        values = relocated.get(field)
        if not isinstance(values, Mapping):
            raise CheckpointError(f"raw metadata.{field} must be an object")
        relocated[field] = {
            str(target): str(model_dir / Path(str(path)).name)
            for target, path in values.items()
        }
    return relocated


def _evaluate_raw_gate(
    metadata: Mapping[str, Any],
    paths: CheckpointPaths,
    contract: supervisor.PipelineContract,
) -> continuation.GateResult:
    relocated = _relocated_metadata_for_audit(metadata, paths.models)
    descriptor, name = tempfile.mkstemp(prefix="ipmsm-v2-provisional-audit-", suffix=".json")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump(relocated, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.write("\n")
        return continuation.evaluate_gate(
            paths.validation,
            temporary,
            paths.r2,
            expected_rows=EXPECTED_ROWS,
            expected_groups=TARGET_DESIGNS,
            expected_repeats=EXPECTED_REPEATS,
            threshold=contract.stage1.r2_threshold,
            expected_ensemble_size=contract.stage1.ensemble_size,
            expected_conformal_coverage=contract.stage1.conformal_coverage,
            expected_audit_case_plan=paths.selected_plan,
        )
    finally:
        temporary.unlink(missing_ok=True)


def audit_models(
    paths: CheckpointPaths,
    contract: supervisor.PipelineContract,
) -> continuation.GateResult:
    raw = _read_json(paths.raw_metadata, "raw training metadata")
    guarded = _read_json(paths.guarded_metadata, "guarded training metadata")
    if guarded.get("provisional") is not True or guarded.get("official_gate_eligible") is not False:
        raise CheckpointError("guarded metadata lacks the provisional production guard")
    if guarded.get("provisional_source_contract_sha256") != contract.contract_sha256:
        raise CheckpointError("guarded metadata contract binding changed")
    if guarded.get("provisional_selected_plan_sha256") != _sha256(paths.selected_plan):
        raise CheckpointError("guarded metadata selected-plan binding changed")
    if guarded.get("provisional_merged_results_sha256") != _sha256(paths.merged):
        raise CheckpointError("guarded metadata merged-result binding changed")
    raw_record = guarded.get("provisional_training_metadata")
    if not isinstance(raw_record, Mapping):
        raise CheckpointError("guarded metadata lacks raw metadata provenance")
    if (
        raw_record.get("path") != "training_metadata.json"
        or raw_record.get("sha256") != _sha256(paths.raw_metadata)
    ):
        raise CheckpointError("guarded metadata raw provenance changed")
    if (
        guarded.get("primary_test_r2_gate_passed") is not False
        or guarded.get("voltage_test_r2_gate_passed") is not False
    ):
        raise CheckpointError("guarded metadata could be accepted as a production model")
    if raw.get("enable_tuning") is not False or int(raw.get("ensemble_size", 0)) != 5:
        raise CheckpointError("raw provisional training configuration changed")
    _audit_artifact_records(raw, paths.models)
    try:
        gate = _evaluate_raw_gate(raw, paths, contract)
    except Exception as exc:
        raise CheckpointError(f"raw provisional training audit failed: {exc}") from exc
    try:
        surrogate_bundle.load_surrogate_bundle(paths.models)
    except surrogate_bundle.SurrogateBundleError as exc:
        if "primary_test_r2_gate_passed must be true" not in str(exc):
            raise CheckpointError(f"guarded bundle rejected for the wrong reason: {exc}") from exc
    else:
        raise CheckpointError("guarded provisional bundle was accepted by the production loader")
    return gate


def artifact_map(paths: CheckpointPaths) -> dict[str, dict[str, str]]:
    selected = {
        "merged_results": paths.merged,
        "snapshot_manifest": paths.snapshot_manifest,
        "selected_plan": paths.selected_plan,
        "validation": paths.validation,
        "raw_training_metadata": paths.raw_metadata,
        "guarded_metadata": paths.guarded_metadata,
        "r2": paths.r2,
    }
    artifacts = {
        name: {"path": str(path), "sha256": _sha256(path)}
        for name, path in selected.items()
    }
    raw = _read_json(paths.raw_metadata, "raw training metadata")
    for field in ("model_artifacts", "training_artifacts"):
        records = raw.get(field)
        if not isinstance(records, Mapping):
            raise CheckpointError(f"raw metadata.{field} must be an object")
        for name, record in records.items():
            if not isinstance(record, Mapping):
                raise CheckpointError(f"raw metadata.{field}.{name} must be an object")
            artifact = paths.models / Path(str(record.get("path") or "")).name
            key = f"{field}.{name}"
            artifacts[key] = {"path": str(artifact), "sha256": _sha256(artifact)}
    return dict(sorted(artifacts.items()))


def build_decision(
    bound: BoundContract,
    paths: CheckpointPaths,
    readiness: Readiness,
    gate: continuation.GateResult,
) -> dict[str, Any]:
    return {
        "artifacts": artifact_map(paths),
        "contract_document_sha256": bound.document_sha256,
        "contract_sha256": bound.contract.contract_sha256,
        "diagnostic_scope": readiness.diagnostic_scope,
        "official_gate_eligible": False,
        "provisional": True,
        "helper_sha256": dict(sorted(bound.helper_sha256.items())),
        "recommended_action": "continue_stage1",
        "result": gate.summary(),
        "schema_version": SCHEMA_VERSION,
        "selected_designs": TARGET_DESIGNS,
        "selected_rows": EXPECTED_ROWS,
        "split_design_counts": dict(readiness.split_design_counts),
        "status": "diagnostic_complete",
    }


def build_manifest(
    bound: BoundContract,
    paths: CheckpointPaths,
    decision: Mapping[str, Any],
    commands: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    return {
        "artifacts": artifact_map(paths),
        "commands": {name: list(argv) for name, argv in commands.items()},
        "contract": {
            "canonical_sha256": bound.contract.contract_sha256,
            "document_sha256": bound.document_sha256,
            "path": str(bound.contract.source),
        },
        "decision": {"path": str(paths.decision), "sha256": _sha256(paths.decision)},
        "official_gate_eligible": False,
        "provisional": True,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "scripts": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in {
                "snapshot": bound.contract.workdir / "snapshot_ipmsm_v2_partial_results.py",
                "training": bound.contract.workdir / "train_ipmsm_lightgbm.py",
                "validation": bound.contract.workdir / "validate_ipmsm_v2_dataset.py",
                "watcher": Path(__file__).resolve(strict=True),
            }.items()
        },
        "status": "complete",
    }


def _json_equal(path: Path, expected: Mapping[str, Any], label: str) -> None:
    actual = _read_json(path, label)
    if actual != expected:
        raise CheckpointError(f"existing {label} does not match current audited artifacts")


def _emit(event: str, **fields: Any) -> None:
    print(
        json.dumps({"event": event, **fields}, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        flush=True,
    )


def _top_level_state(paths: CheckpointPaths) -> set[str]:
    if not paths.root.exists():
        return set()
    if not paths.root.is_dir():
        raise CheckpointError("--output-dir exists but is not a directory")
    names = {item.name for item in paths.root.iterdir()}
    allowed = {
        paths.snapshot.name,
        paths.validation.name,
        paths.models.name,
        paths.decision.name,
        paths.manifest.name,
    }
    extras = sorted(names - allowed)
    if extras:
        raise CheckpointError(f"checkpoint output contains unknown/partial artifacts: {extras}")
    return names


def _publish_validation(
    bound: BoundContract,
    paths: CheckpointPaths,
    argv: Sequence[str],
) -> None:
    if paths.validation_staging.exists():
        raise CheckpointError("validation staging artifact already exists")
    try:
        assert_contract_bound(bound)
        run_child(argv, workdir=bound.contract.workdir, label="checkpoint validation")
        audit_validation(paths.validation_staging)
        publish_no_replace(paths.validation_staging, paths.validation)
    finally:
        paths.validation_staging.unlink(missing_ok=True)


def _publish_models(
    bound: BoundContract,
    paths: CheckpointPaths,
    argv: Sequence[str],
) -> None:
    if paths.model_staging.exists():
        raise CheckpointError("model staging directory already exists")
    published = False
    try:
        assert_contract_bound(bound)
        run_child(argv, workdir=bound.contract.workdir, label="checkpoint training")
        guard_training_metadata(
            paths.model_staging,
            contract_sha256=bound.contract.contract_sha256,
            selected_plan_sha256=_sha256(paths.selected_plan),
            merged_results_sha256=_sha256(paths.merged),
        )
        # Audit with staging paths before the fresh directory publication.
        staging_paths = CheckpointPaths(
            **{
                **paths.__dict__,
                "models": paths.model_staging,
                "raw_metadata": paths.model_staging / "training_metadata.json",
                "guarded_metadata": paths.model_staging / "metadata.json",
                "r2": paths.model_staging / "r2.csv",
            }
        )
        audit_models(staging_paths, bound.contract)
        snapshotter._rename_directory_no_replace(paths.model_staging, paths.models)
        published = True
    finally:
        if not published and paths.model_staging.exists():
            shutil.rmtree(paths.model_staging, ignore_errors=True)


def _pid_record(path: Path) -> tuple[dict[str, Any], bool]:
    value = _read_json(path, "checkpoint PID marker")
    try:
        pid = int(value.get("pid"))
    except (TypeError, ValueError) as exc:
        raise CheckpointError("checkpoint PID marker has an invalid pid") from exc
    return value, continuation.pid_is_running(pid)


class PidMarker:
    def __init__(self, path: Path, bound: BoundContract, output: Path, *, resume: bool) -> None:
        self.path = path
        self.bound = bound
        self.output = output
        self.resume = resume
        self.owned = False

    def __enter__(self) -> "PidMarker":
        if self.path.exists():
            value, running = _pid_record(self.path)
            expected = {
                "contract_sha256": self.bound.contract.contract_sha256,
                "output_dir": str(self.output),
                "schema_version": "ipmsm-v2-provisional-checkpoint-pid-v1",
            }
            if any(value.get(key) != expected_value for key, expected_value in expected.items()):
                raise CheckpointError("checkpoint PID marker belongs to another execution contract")
            if running:
                raise CheckpointError(f"another checkpoint process is active: {self.path}")
            if not self.resume:
                raise CheckpointError(f"stale checkpoint PID marker requires --resume: {self.path}")
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "contract_sha256": self.bound.contract.contract_sha256,
            "output_dir": str(self.output),
            "pid": os.getpid(),
            "schema_version": "ipmsm-v2-provisional-checkpoint-pid-v1",
        }
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.owned = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self.owned:
            return
        try:
            value = _read_json(self.path, "checkpoint PID marker")
            if value.get("pid") == os.getpid():
                self.path.unlink(missing_ok=True)
        except CheckpointError:
            pass


def report(
    bound: BoundContract,
    paths: CheckpointPaths,
    readiness: Readiness,
    commands: Mapping[str, Sequence[str]],
    *,
    mode: str,
    status: str,
) -> dict[str, Any]:
    return {
        "commands": {name: list(argv) for name, argv in commands.items()},
        "contract": str(bound.contract.source),
        "contract_sha256": bound.contract.contract_sha256,
        "mode": mode,
        "official_gate_eligible": False,
        "output_dir": str(paths.root),
        "readiness": readiness.as_mapping(),
        "status": status,
        "writes_performed": 0 if mode == "dry-run" else None,
    }


def execute(
    bound: BoundContract,
    paths: CheckpointPaths,
    args: argparse.Namespace,
    commands: dict[str, list[str]],
) -> dict[str, Any]:
    root_preexisted = paths.root.exists()
    existing = _top_level_state(paths)
    if paths.manifest.name in existing:
        if paths.decision.name not in existing:
            raise CheckpointError("completion manifest exists without its decision")
        readiness = audit_snapshot(paths, bound)
        audit_validation(paths.validation)
        gate = audit_models(paths, bound.contract)
        decision = build_decision(bound, paths, readiness, gate)
        _json_equal(paths.decision, decision, "checkpoint decision")
        manifest = build_manifest(bound, paths, decision, commands)
        _json_equal(paths.manifest, manifest, "checkpoint manifest")
        return report(bound, paths, readiness, commands, mode="execute", status="already_complete")
    if root_preexisted and not args.resume:
        raise CheckpointError("incomplete checkpoint output requires --resume")
    if args.resume and not root_preexisted:
        raise CheckpointError("--resume requires an existing audited checkpoint prefix")

    with supervisor.ExecutionLock(paths.lock):
        with PidMarker(paths.pid, bound, paths.root, resume=args.resume):
            existing = _top_level_state(paths)
            allowed_prefixes = [
                set(),
                {paths.snapshot.name},
                {paths.snapshot.name, paths.validation.name},
                {paths.snapshot.name, paths.validation.name, paths.models.name},
                {
                    paths.snapshot.name,
                    paths.validation.name,
                    paths.models.name,
                    paths.decision.name,
                },
            ]
            if existing not in allowed_prefixes:
                raise CheckpointError("checkpoint partial state is not a supported audited prefix")
            if paths.snapshot.name not in existing:
                started = time.monotonic()
                while True:
                    assert_contract_bound(bound)
                    readiness = inspect_readiness(bound.contract)
                    _emit("readiness", **readiness.as_mapping())
                    if readiness.ready:
                        break
                    elapsed = time.monotonic() - started
                    if elapsed + args.poll_interval_seconds > args.overall_timeout_seconds:
                        raise CheckpointError("bounded readiness poll timed out before 60 designs")
                    time.sleep(args.poll_interval_seconds)
                completed = run_child(
                    commands["snapshot"],
                    workdir=bound.contract.workdir,
                    label="checkpoint snapshot",
                )
                evidence = _last_json(completed.stdout, "checkpoint snapshot")
                if (
                    evidence.get("contract_sha256") != bound.contract.contract_sha256
                    or Path(str(evidence.get("snapshot_manifest"))).resolve(strict=False)
                    != paths.snapshot_manifest.resolve(strict=False)
                    or evidence.get("snapshot_manifest_sha256")
                    != _sha256(paths.snapshot_manifest)
                    or evidence.get("selected_designs") != TARGET_DESIGNS
                    or evidence.get("result_rows") != EXPECTED_ROWS
                    or evidence.get("official_gate_eligible") is not False
                    or evidence.get("diagnostic_scope") not in {
                        "provisional_minimum",
                        "provisional_stronger",
                    }
                ):
                    raise CheckpointError("snapshot evidence violates the provisional contract")
                _emit("snapshot_complete", selected_designs=TARGET_DESIGNS, result_rows=EXPECTED_ROWS)
            readiness = audit_snapshot(paths, bound)

            if not paths.validation.exists():
                _publish_validation(bound, paths, commands["validation"])
                _emit("validation_complete", rows=EXPECTED_ROWS)
            audit_validation(paths.validation)

            if not paths.models.exists():
                _publish_models(bound, paths, commands["training"])
                _emit("training_complete", ensemble_size=5, tuning=False)
            gate = audit_models(paths, bound.contract)
            assert_contract_bound(bound)
            decision = build_decision(bound, paths, readiness, gate)
            if paths.decision.exists():
                _json_equal(paths.decision, decision, "checkpoint decision")
            else:
                _write_json_no_replace(paths.decision, decision)
            manifest = build_manifest(bound, paths, decision, commands)
            if paths.manifest.exists():
                _json_equal(paths.manifest, manifest, "checkpoint manifest")
            else:
                _write_json_no_replace(paths.manifest, manifest)
            _json_equal(paths.decision, decision, "checkpoint decision")
            _json_equal(paths.manifest, manifest, "checkpoint manifest")
            return report(bound, paths, readiness, commands, mode="execute", status="complete")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_cli(args)
    bound = load_bound_contract(args.contract)
    root = _resolve_from_workdir(args.output_dir, bound.contract.workdir)
    pid_file = (
        _resolve_from_workdir(args.pid_file, bound.contract.workdir)
        if args.pid_file is not None
        else None
    )
    paths = make_paths(root, pid_file)
    validate_paths(paths, bound.contract)
    commands = {
        "snapshot": build_snapshot_argv(bound.contract, paths),
        "validation": build_validation_argv(bound.contract, paths),
        "training": build_training_argv(bound.contract, paths),
    }
    if not args.execute:
        readiness = inspect_readiness(bound.contract)
        print(
            json.dumps(
                report(
                    bound,
                    paths,
                    readiness,
                    commands,
                    mode="dry-run",
                    status="ready" if readiness.ready else "waiting",
                ),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    result = execute(bound, paths, args, commands)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("ERROR: checkpoint interrupted", file=sys.stderr)
        raise SystemExit(130)
    except (
        CheckpointError,
        OSError,
        supervisor.PipelineContractError,
        supervisor.PipelineStateError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
