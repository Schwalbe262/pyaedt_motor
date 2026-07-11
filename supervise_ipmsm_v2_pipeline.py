"""Durable, artifact-driven supervisor for the IPMSM v2 foundation pipeline.

The immutable JSON contract contains exact child argv arrays and is protected
by its own canonical SHA-256.  Inspection is read-only by default.  ``--execute``
serially invokes children without a shell and re-inspects committed artifacts
after every transition.  Existing partial artifacts are never removed or
overwritten; unsupported partial states fail closed.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence

from atomic_publish import publish_no_replace
import continue_ipmsm_v2_stage2 as stage2_continuation
from merge_ipmsm_v2_results import merge_complete_results


CONTRACT_SCHEMA_VERSION = "ipmsm-v2-pipeline-contract-v1"
REPORT_SCHEMA_VERSION = "ipmsm-v2-pipeline-supervisor-v1"
SPEED_MARKER_SCHEMA_VERSION = "ipmsm-v2-speed-completion-v1"
STAGE2_DECISION_SCHEMA_VERSION = "ipmsm_v2_stage2_continuation_v1"
OPTIMIZATION_DECISION_SCHEMA_VERSION = "ipmsm_v2_optimization_continuation_v1"
MERGE_MANIFEST_SCHEMA_VERSION = "ipmsm-v2-case-plan-merge-v1"
STAGE3_MANIFEST_SCHEMA_VERSION = "ipmsm_v2_stage3_fallback_plan_v2"
UPSTREAM_PLACEHOLDER = "{upstream_decision}"
MAX_TRANSITIONS = 16


class PipelineContractError(ValueError):
    """The immutable launch contract is malformed or no longer exact."""


class PipelineStateError(RuntimeError):
    """Committed artifacts are incomplete, inconsistent, or unsafe to use."""


@dataclass(frozen=True)
class Artifact:
    path: Path
    sha256: str


@dataclass(frozen=True)
class ExternalPidFile:
    role: str
    path: Path


@dataclass(frozen=True)
class Stage1Contract:
    case_plan: Path
    output_dir: Path
    result: Path
    validation: Path
    model_dir: Path
    metadata: Path
    r2: Path
    expected_rows: int
    expected_groups: int
    expected_repeats: int
    r2_threshold: float
    ensemble_size: int
    conformal_coverage: float
    campaign_argv: tuple[str, ...]
    validation_argv: tuple[str, ...]
    training_argv: tuple[str, ...]


@dataclass(frozen=True)
class ContinuationContract:
    decision: Path
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Stage3Contract:
    prior_plan: Path
    prior_manifest: Path
    plan: Path
    manifest: Path
    decision: Path
    expected_rows: int
    merge_argv: tuple[str, ...]
    generate_argv: tuple[str, ...]
    continuation_argv: tuple[str, ...]


@dataclass(frozen=True)
class OptimizationContract:
    decision: Path
    argv_template: tuple[str, ...]


@dataclass(frozen=True)
class SpeedContract:
    plan: Path
    output_dir: Path
    result: Path
    rank: Path
    top: Path
    marker: Path
    expected_rows: int
    minimum_top_profiles: int
    plan_argv: tuple[str, ...]
    campaign_argv: tuple[str, ...]
    rank_argv: tuple[str, ...]


@dataclass(frozen=True)
class PipelineContract:
    source: Path
    workdir: Path
    lock_path: Path
    contract_sha256: str
    immutable_inputs: tuple[Artifact, ...]
    external_pid_files: tuple[ExternalPidFile, ...]
    stage1: Stage1Contract
    stage2: ContinuationContract
    stage3: Stage3Contract
    optimization: OptimizationContract
    speed: SpeedContract


@dataclass(frozen=True)
class PipelineSnapshot:
    next_action: str
    branch: str
    upstream_decision: Path | None = None
    terminal: bool = False
    exit_code: int = 0
    detail: Mapping[str, Any] | None = None

    def report(self, contract: PipelineContract, *, mode: str, transitions: int = 0) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "contract": str(contract.source),
            "contract_sha256": contract.contract_sha256,
            "detail": dict(self.detail or {}),
            "mode": mode,
            "next_action": self.next_action,
            "schema_version": REPORT_SCHEMA_VERSION,
            "terminal": self.terminal,
            "transitions": transitions,
            "upstream_decision": (
                str(self.upstream_decision) if self.upstream_decision is not None else ""
            ),
            "writes_performed": transitions if mode == "execute" else 0,
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PipelineContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PipelineContractError(f"non-finite JSON constant: {value}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineStateError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineStateError(f"{label} must be a JSON object: {path}")
    return value


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise PipelineContractError(f"{label} fields mismatch: missing={missing} extra={extra}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineContractError(f"{label} must be an object")
    return value


def _path(value: Any, workdir: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PipelineContractError(f"{label} must be a nonblank path string")
    path = Path(value)
    return path if path.is_absolute() else workdir / path


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise PipelineContractError(f"{label} must be a positive integer")
    return value


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise PipelineContractError(f"{label} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PipelineContractError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise PipelineContractError(f"{label} must be finite")
    return result


def _argv(value: Any, label: str, expected_script: str, *, placeholder: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) < 2:
        raise PipelineContractError(f"{label} must be an argv array with executable and script")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise PipelineContractError(f"{label} contains a blank or non-string argument")
    argv = tuple(value)
    executable = Path(argv[0]).name.lower()
    if "python" not in executable and "pypy" not in executable:
        raise PipelineContractError(f"{label} must use a Python executable")
    scripts = [item for item in argv[1:] if Path(item).name.lower() == expected_script.lower()]
    if len(scripts) != 1:
        raise PipelineContractError(f"{label} must invoke {expected_script}")
    count = argv.count(UPSTREAM_PLACEHOLDER)
    if count != (1 if placeholder else 0):
        raise PipelineContractError(
            f"{label} must contain {1 if placeholder else 0} upstream placeholder(s); got {count}"
        )
    return argv


def _flag_value(argv: Sequence[str], flag: str, label: str) -> str:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise PipelineContractError(f"{label} must contain exactly one {flag} value")
    return argv[positions[0] + 1]


def _require_flag(argv: Sequence[str], flag: str, label: str) -> None:
    if argv.count(flag) != 1:
        raise PipelineContractError(f"{label} must contain exactly one {flag}")


def _require_flag_path(
    argv: Sequence[str],
    flag: str,
    expected: Path,
    workdir: Path,
    label: str,
) -> None:
    actual = _path(_flag_value(argv, flag, label), workdir, f"{label} {flag}")
    if actual.resolve(strict=False) != expected.resolve(strict=False):
        raise PipelineContractError(f"{label} {flag} does not match its artifact path")


def load_contract(path: str | Path) -> PipelineContract:
    source = Path(path)
    raw = _read_json(source, "pipeline contract")
    _expect_keys(raw, {"schema_version", "contract_sha256", "pipeline"}, "contract")
    if raw["schema_version"] != CONTRACT_SCHEMA_VERSION:
        raise PipelineContractError("unsupported pipeline contract schema_version")
    pipeline = _mapping(raw["pipeline"], "pipeline")
    expected_hash = _canonical_sha256(
        {"schema_version": CONTRACT_SCHEMA_VERSION, "pipeline": pipeline}
    )
    if raw["contract_sha256"] != expected_hash:
        raise PipelineContractError("pipeline contract_sha256 mismatch")
    required_pipeline_fields = {
        "workdir", "lock_path", "immutable_inputs", "stage1", "stage2", "stage3", "optimization", "speed"
    }
    optional_pipeline_fields = {"external_pid_files"}
    actual_pipeline_fields = set(pipeline)
    if not required_pipeline_fields <= actual_pipeline_fields or not actual_pipeline_fields <= (
        required_pipeline_fields | optional_pipeline_fields
    ):
        raise PipelineContractError(
            "pipeline fields mismatch: "
            f"missing={sorted(required_pipeline_fields - actual_pipeline_fields)} "
            f"extra={sorted(actual_pipeline_fields - required_pipeline_fields - optional_pipeline_fields)}"
        )
    source_parent = source.resolve(strict=False).parent
    workdir = _path(pipeline["workdir"], source_parent, "pipeline.workdir").resolve(strict=False)
    lock_path = _path(pipeline["lock_path"], workdir, "pipeline.lock_path")

    inputs_raw = pipeline["immutable_inputs"]
    if not isinstance(inputs_raw, list) or not inputs_raw:
        raise PipelineContractError("pipeline.immutable_inputs must be a nonempty array")
    inputs: list[Artifact] = []
    seen_inputs: set[Path] = set()
    for index, item in enumerate(inputs_raw):
        value = _mapping(item, f"immutable_inputs[{index}]")
        _expect_keys(value, {"path", "sha256"}, f"immutable_inputs[{index}]")
        artifact_path = _path(value["path"], workdir, f"immutable_inputs[{index}].path")
        digest = value["sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise PipelineContractError(f"immutable_inputs[{index}].sha256 is invalid")
        resolved = artifact_path.resolve(strict=False)
        if resolved in seen_inputs:
            raise PipelineContractError(f"duplicate immutable input path: {artifact_path}")
        seen_inputs.add(resolved)
        inputs.append(Artifact(artifact_path, digest.lower()))

    external_pid_files: list[ExternalPidFile] = []
    seen_pid_roles: set[str] = set()
    seen_pid_paths: set[Path] = set()
    pid_files_raw = pipeline.get("external_pid_files", [])
    if not isinstance(pid_files_raw, list):
        raise PipelineContractError("pipeline.external_pid_files must be an array")
    for index, item in enumerate(pid_files_raw):
        value = _mapping(item, f"external_pid_files[{index}]")
        _expect_keys(value, {"role", "path"}, f"external_pid_files[{index}]")
        role = value["role"]
        if not isinstance(role, str) or not role.strip():
            raise PipelineContractError(f"external_pid_files[{index}].role must be nonblank")
        pid_path = _path(value["path"], workdir, f"external_pid_files[{index}].path")
        resolved_pid_path = pid_path.resolve(strict=False)
        if role in seen_pid_roles or resolved_pid_path in seen_pid_paths:
            raise PipelineContractError("external PID roles and paths must be unique")
        seen_pid_roles.add(role)
        seen_pid_paths.add(resolved_pid_path)
        external_pid_files.append(ExternalPidFile(role, pid_path))

    s1 = _mapping(pipeline["stage1"], "stage1")
    _expect_keys(
        s1,
        {
            "case_plan", "output_dir", "result", "validation", "model_dir", "metadata", "r2",
            "expected_rows", "expected_groups", "expected_repeats", "r2_threshold", "ensemble_size",
            "conformal_coverage", "campaign_argv", "validation_argv", "training_argv",
        },
        "stage1",
    )
    stage1 = Stage1Contract(
        case_plan=_path(s1["case_plan"], workdir, "stage1.case_plan"),
        output_dir=_path(s1["output_dir"], workdir, "stage1.output_dir"),
        result=_path(s1["result"], workdir, "stage1.result"),
        validation=_path(s1["validation"], workdir, "stage1.validation"),
        model_dir=_path(s1["model_dir"], workdir, "stage1.model_dir"),
        metadata=_path(s1["metadata"], workdir, "stage1.metadata"),
        r2=_path(s1["r2"], workdir, "stage1.r2"),
        expected_rows=_positive_int(s1["expected_rows"], "stage1.expected_rows"),
        expected_groups=_positive_int(s1["expected_groups"], "stage1.expected_groups"),
        expected_repeats=_positive_int(s1["expected_repeats"], "stage1.expected_repeats"),
        r2_threshold=_finite_float(s1["r2_threshold"], "stage1.r2_threshold"),
        ensemble_size=_positive_int(s1["ensemble_size"], "stage1.ensemble_size"),
        conformal_coverage=_finite_float(s1["conformal_coverage"], "stage1.conformal_coverage"),
        campaign_argv=_argv(s1["campaign_argv"], "stage1.campaign_argv", "run_ipmsm_v2_campaign.py"),
        validation_argv=_argv(s1["validation_argv"], "stage1.validation_argv", "validate_ipmsm_v2_dataset.py"),
        training_argv=_argv(s1["training_argv"], "stage1.training_argv", "train_ipmsm_lightgbm.py"),
    )

    def continuation(value: Any, label: str) -> ContinuationContract:
        item = _mapping(value, label)
        _expect_keys(item, {"decision", "argv"}, label)
        return ContinuationContract(
            decision=_path(item["decision"], workdir, f"{label}.decision"),
            argv=_argv(item["argv"], f"{label}.argv", "continue_ipmsm_v2_stage2.py"),
        )

    stage2 = continuation(pipeline["stage2"], "stage2")
    s3 = _mapping(pipeline["stage3"], "stage3")
    _expect_keys(
        s3,
        {
            "prior_plan", "prior_manifest", "plan", "manifest", "decision", "expected_rows",
            "merge_argv", "generate_argv", "continuation_argv",
        },
        "stage3",
    )
    stage3 = Stage3Contract(
        prior_plan=_path(s3["prior_plan"], workdir, "stage3.prior_plan"),
        prior_manifest=_path(s3["prior_manifest"], workdir, "stage3.prior_manifest"),
        plan=_path(s3["plan"], workdir, "stage3.plan"),
        manifest=_path(s3["manifest"], workdir, "stage3.manifest"),
        decision=_path(s3["decision"], workdir, "stage3.decision"),
        expected_rows=_positive_int(s3["expected_rows"], "stage3.expected_rows"),
        merge_argv=_argv(s3["merge_argv"], "stage3.merge_argv", "merge_ipmsm_v2_case_plans.py"),
        generate_argv=_argv(s3["generate_argv"], "stage3.generate_argv", "generate_ipmsm_v2_cases.py"),
        continuation_argv=_argv(
            s3["continuation_argv"], "stage3.continuation_argv", "continue_ipmsm_v2_stage2.py"
        ),
    )

    opt = _mapping(pipeline["optimization"], "optimization")
    _expect_keys(opt, {"decision", "argv_template"}, "optimization")
    optimization = OptimizationContract(
        decision=_path(opt["decision"], workdir, "optimization.decision"),
        argv_template=_argv(
            opt["argv_template"],
            "optimization.argv_template",
            "continue_ipmsm_v2_optimization.py",
            placeholder=True,
        ),
    )

    speed_raw = _mapping(pipeline["speed"], "speed")
    _expect_keys(
        speed_raw,
        {
            "plan", "output_dir", "result", "rank", "top", "marker", "expected_rows",
            "minimum_top_profiles", "plan_argv", "campaign_argv", "rank_argv",
        },
        "speed",
    )
    speed = SpeedContract(
        plan=_path(speed_raw["plan"], workdir, "speed.plan"),
        output_dir=_path(speed_raw["output_dir"], workdir, "speed.output_dir"),
        result=_path(speed_raw["result"], workdir, "speed.result"),
        rank=_path(speed_raw["rank"], workdir, "speed.rank"),
        top=_path(speed_raw["top"], workdir, "speed.top"),
        marker=_path(speed_raw["marker"], workdir, "speed.marker"),
        expected_rows=_positive_int(speed_raw["expected_rows"], "speed.expected_rows"),
        minimum_top_profiles=_positive_int(
            speed_raw["minimum_top_profiles"], "speed.minimum_top_profiles"
        ),
        plan_argv=_argv(
            speed_raw["plan_argv"], "speed.plan_argv", "generate_ipmsm_second_pass_cases.py"
        ),
        campaign_argv=_argv(
            speed_raw["campaign_argv"], "speed.campaign_argv", "run_ipmsm_v2_campaign.py"
        ),
        rank_argv=_argv(
            speed_raw["rank_argv"], "speed.rank_argv", "rank_ipmsm_second_pass_profiles.py"
        ),
    )

    _require_flag_path(stage1.campaign_argv, "--cases", stage1.case_plan, workdir, "Stage1 campaign")
    _require_flag_path(stage1.campaign_argv, "--output-dir", stage1.output_dir, workdir, "Stage1 campaign")
    _require_flag(stage1.campaign_argv, "--submit", "Stage1 campaign")
    merged_name = Path(
        _flag_value(stage1.campaign_argv, "--merged-output", "Stage1 campaign")
        if "--merged-output" in stage1.campaign_argv
        else "merged_results.csv"
    )
    if merged_name.is_absolute() or ".." in merged_name.parts or (
        stage1.output_dir / merged_name
    ).resolve(strict=False) != stage1.result.resolve(strict=False):
        raise PipelineContractError("Stage1 campaign merged output does not match stage1.result")
    _require_flag_path(stage1.validation_argv, "--data", stage1.result, workdir, "Stage1 validation")
    _require_flag_path(
        stage1.validation_argv, "--summary", stage1.validation, workdir, "Stage1 validation"
    )
    _require_flag(stage1.training_argv, "--v2", "Stage1 training")
    _require_flag_path(stage1.training_argv, "--data", stage1.result, workdir, "Stage1 training")
    _require_flag_path(
        stage1.training_argv, "--model-dir", stage1.model_dir, workdir, "Stage1 training"
    )
    _require_flag_path(
        stage1.training_argv,
        "--verification-output",
        stage1.r2,
        workdir,
        "Stage1 training",
    )
    _require_flag_path(stage2.argv, "--decision-output", stage2.decision, workdir, "Stage2")
    _require_flag_path(stage3.merge_argv, "--output", stage3.prior_plan, workdir, "Stage12 merge")
    _require_flag_path(
        stage3.merge_argv,
        "--manifest-output",
        stage3.prior_manifest,
        workdir,
        "Stage12 merge",
    )
    _require_flag_path(stage3.generate_argv, "--output", stage3.plan, workdir, "Stage3 generation")
    _require_flag_path(
        stage3.generate_argv,
        "--stage3-manifest-output",
        stage3.manifest,
        workdir,
        "Stage3 generation",
    )
    _require_flag_path(
        stage3.generate_argv,
        "--stage2-failed-decision",
        stage2.decision,
        workdir,
        "Stage3 generation",
    )
    _require_flag_path(
        stage3.continuation_argv,
        "--decision-output",
        stage3.decision,
        workdir,
        "Stage3 continuation",
    )
    if _flag_value(optimization.argv_template, "--stage2-decision", "optimization") != UPSTREAM_PLACEHOLDER:
        raise PipelineContractError("optimization upstream placeholder must be the --stage2-decision value")
    _require_flag_path(
        optimization.argv_template,
        "--decision-output",
        optimization.decision,
        workdir,
        "optimization",
    )
    _require_flag_path(speed.plan_argv, "--output", speed.plan, workdir, "speed plan")
    _require_flag_path(speed.campaign_argv, "--cases", speed.plan, workdir, "speed campaign")
    _require_flag_path(
        speed.campaign_argv, "--output-dir", speed.output_dir, workdir, "speed campaign"
    )
    _require_flag(speed.campaign_argv, "--submit", "speed campaign")
    speed_merged = Path(
        _flag_value(speed.campaign_argv, "--merged-output", "speed campaign")
        if "--merged-output" in speed.campaign_argv
        else "merged_results.csv"
    )
    if speed_merged.is_absolute() or ".." in speed_merged.parts or (
        speed.output_dir / speed_merged
    ).resolve(strict=False) != speed.result.resolve(strict=False):
        raise PipelineContractError("speed campaign merged output does not match speed.result")
    _require_flag_path(speed.rank_argv, "--strict-speed-plan", speed.plan, workdir, "speed rank")
    _require_flag_path(
        speed.rank_argv,
        "--strict-candidate-results",
        speed.result,
        workdir,
        "speed rank",
    )
    _require_flag_path(speed.rank_argv, "--output", speed.rank, workdir, "speed rank")
    _require_flag_path(
        speed.rank_argv, "--top-profiles-output", speed.top, workdir, "speed rank"
    )

    outputs = [
        stage1.output_dir, stage1.validation, stage1.model_dir, stage1.r2, stage2.decision,
        stage3.prior_plan, stage3.prior_manifest, stage3.plan, stage3.manifest, stage3.decision,
        optimization.decision, speed.plan, speed.output_dir, speed.rank, speed.top, speed.marker,
    ]
    resolved_outputs = [item.resolve(strict=False) for item in outputs]
    if len(resolved_outputs) != len(set(resolved_outputs)):
        raise PipelineContractError("pipeline output paths must be distinct")
    if set(resolved_outputs) & seen_pid_paths:
        raise PipelineContractError("external PID files must be distinct from pipeline outputs")
    if lock_path.resolve(strict=False) in set(resolved_outputs) | seen_inputs | seen_pid_paths:
        raise PipelineContractError("pipeline lock_path must be distinct from inputs and outputs")
    if not 0.0 < stage1.r2_threshold <= 1.0 or not 0.0 < stage1.conformal_coverage < 1.0:
        raise PipelineContractError("Stage1 R2 threshold/coverage are outside their valid ranges")
    return PipelineContract(
        source=source.resolve(strict=False),
        workdir=workdir,
        lock_path=lock_path,
        contract_sha256=expected_hash,
        immutable_inputs=tuple(inputs),
        external_pid_files=tuple(external_pid_files),
        stage1=stage1,
        stage2=stage2,
        stage3=stage3,
        optimization=optimization,
        speed=speed,
    )


def audit_immutable_inputs(contract: PipelineContract) -> None:
    for artifact in contract.immutable_inputs:
        if not artifact.path.is_file():
            raise PipelineStateError(f"immutable input is missing: {artifact.path}")
        if _file_sha256(artifact.path) != artifact.sha256:
            raise PipelineStateError(f"immutable input hash changed: {artifact.path}")


def _boot_time_epoch() -> float | None:
    """Return the current boot epoch when the host exposes a stable uptime clock."""

    if os.name == "nt":
        try:
            import ctypes

            get_tick_count = ctypes.windll.kernel32.GetTickCount64
            get_tick_count.restype = ctypes.c_ulonglong
            return time.time() - float(get_tick_count()) / 1000.0
        except (AttributeError, OSError, ValueError):
            return None
    try:
        with Path("/proc/stat").open("r", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def audit_external_pid_files(contract: PipelineContract) -> list[dict[str, Any]]:
    boot_time = _boot_time_epoch()
    active: list[dict[str, Any]] = []
    for item in contract.external_pid_files:
        if not os.path.lexists(item.path):
            continue
        if not item.path.is_file():
            raise PipelineStateError(f"external PID path is not a file: {item.path}")
        try:
            modified = item.path.stat().st_mtime
            raw = item.path.read_text(encoding="utf-8-sig").strip()
            pid = int(raw)
        except (OSError, UnicodeError, ValueError) as exc:
            raise PipelineStateError(f"external PID file is invalid: {item.path}") from exc
        if pid <= 0:
            raise PipelineStateError(f"external PID must be positive: {item.path}")
        # A file from an earlier boot cannot identify a process in this boot,
        # even if Windows has already reused the same numeric PID.
        if boot_time is not None and modified < boot_time - 2.0:
            continue
        try:
            running = stage2_continuation.pid_is_running(pid)
        except OSError as exc:
            raise PipelineStateError(f"cannot inspect external PID={pid}: {item.path}: {exc}") from exc
        if running:
            active.append({"pid": pid, "pid_file": str(item.path), "role": item.role})
    return active


def _audit_csv_coverage(plan: Path, result: Path, expected_rows: int, label: str) -> None:
    try:
        _, rows = merge_complete_results(plan, [result])
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise PipelineStateError(f"{label} coverage audit failed: {exc}") from exc
    if len(rows) != expected_rows:
        raise PipelineStateError(f"{label} rows={len(rows)}, expected={expected_rows}")


def _audit_validation_summary(path: Path, stage1: Stage1Contract) -> None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PipelineStateError(f"cannot audit Stage1 validation: {exc}") from exc
    if len(rows) != 1:
        raise PipelineStateError("Stage1 validation must contain exactly one row")
    row = rows[0]
    expected = {
        "rows": stage1.expected_rows,
        "ok_rows": stage1.expected_rows,
        "unique_case_ids": stage1.expected_rows,
        "unique_geometry_groups": stage1.expected_groups,
        "repeat_pairs": stage1.expected_repeats,
        "failures": 0,
    }
    try:
        mismatches = [key for key, value in expected.items() if int(row.get(key, "")) != value]
    except (TypeError, ValueError) as exc:
        raise PipelineStateError("Stage1 validation contains a non-integer count") from exc
    if row.get("status") != "pass" or mismatches:
        raise PipelineStateError(f"Stage1 validation did not pass exact counts: {mismatches}")


def _audit_stage1_training(stage1: Stage1Contract) -> stage2_continuation.GateResult:
    _audit_validation_summary(stage1.validation, stage1)
    try:
        return stage2_continuation.evaluate_gate(
            stage1.validation,
            stage1.metadata,
            stage1.r2,
            expected_rows=stage1.expected_rows,
            expected_groups=stage1.expected_groups,
            expected_repeats=stage1.expected_repeats,
            threshold=stage1.r2_threshold,
            expected_ensemble_size=stage1.ensemble_size,
            expected_conformal_coverage=stage1.conformal_coverage,
        )
    except Exception as exc:
        raise PipelineStateError(f"Stage1 surrogate audit failed: {exc}") from exc


def _audit_nested_artifacts(value: Any, workdir: Path) -> None:
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            path = _path(value["path"], workdir, "decision artifact path")
            digest = value["sha256"]
            if not isinstance(digest, str) or len(digest) != 64:
                raise PipelineStateError(f"decision artifact has invalid sha256: {path}")
            if not path.is_file() or _file_sha256(path) != digest.lower():
                raise PipelineStateError(f"decision artifact is missing or changed: {path}")
        for child in value.values():
            _audit_nested_artifacts(child, workdir)
    elif isinstance(value, list):
        for child in value:
            _audit_nested_artifacts(child, workdir)


def audit_decision(
    path: Path,
    *,
    schema_version: str,
    allowed_statuses: set[str],
    workdir: Path,
) -> dict[str, Any]:
    decision = _read_json(path, "continuation decision")
    if decision.get("schema_version") != schema_version or decision.get("mode") != "execute":
        raise PipelineStateError(f"unsupported or provisional decision: {path}")
    status = str(decision.get("status") or "")
    if status not in allowed_statuses:
        raise PipelineStateError(f"unsupported decision status={status!r}: {path}")
    recorded = _path(decision.get("decision_output"), workdir, "decision_output").resolve(strict=False)
    if recorded != path.resolve(strict=False):
        raise PipelineStateError(f"decision_output path changed: {path}")
    execution_contract = decision.get("execution_contract")
    if not isinstance(execution_contract, dict):
        raise PipelineStateError(f"decision execution_contract is missing: {path}")
    if decision.get("contract_sha256") != _canonical_sha256(execution_contract):
        raise PipelineStateError(f"decision contract_sha256 is invalid: {path}")
    if status in {"complete", "combined_r2_failed"}:
        _audit_nested_artifacts(decision, workdir)
    return decision


def _audit_pair_presence(left: Path, right: Path, label: str) -> bool:
    left_exists = left.is_file()
    right_exists = right.is_file()
    if left_exists != right_exists:
        raise PipelineStateError(f"unsupported partial {label}: {left} / {right}")
    return left_exists


def _audit_merge_pair(stage3: Stage3Contract, workdir: Path) -> None:
    manifest = _read_json(stage3.prior_manifest, "Stage12 merge manifest")
    if manifest.get("schema_version") != MERGE_MANIFEST_SCHEMA_VERSION or manifest.get("mode") != "execute":
        raise PipelineStateError("Stage12 merge manifest is not an executed v1 artifact")
    output = manifest.get("output")
    if not isinstance(output, dict):
        raise PipelineStateError("Stage12 merge manifest lacks output evidence")
    recorded = _path(output.get("path"), workdir, "Stage12 output path").resolve(strict=False)
    if recorded != stage3.prior_plan.resolve(strict=False):
        raise PipelineStateError("Stage12 merge output path changed")
    if output.get("sha256") != _file_sha256(stage3.prior_plan):
        raise PipelineStateError("Stage12 merged plan hash changed")
    if int(output.get("rows", -1)) <= 0 or int(output.get("design_hashes", -1)) <= 0:
        raise PipelineStateError("Stage12 merge manifest has invalid counts")


def _audit_stage3_pair(stage3: Stage3Contract, workdir: Path) -> None:
    manifest = _read_json(stage3.manifest, "Stage3 plan manifest")
    if manifest.get("schema_version") != STAGE3_MANIFEST_SCHEMA_VERSION or manifest.get("mode") != "write":
        raise PipelineStateError("Stage3 manifest is not an executed fallback artifact")
    recorded = _path(manifest.get("case_plan"), workdir, "Stage3 case plan").resolve(strict=False)
    if recorded != stage3.plan.resolve(strict=False):
        raise PipelineStateError("Stage3 case-plan path changed")
    if manifest.get("case_plan_sha256") != _file_sha256(stage3.plan):
        raise PipelineStateError("Stage3 case-plan hash changed")
    summary = manifest.get("summary")
    if not isinstance(summary, dict) or int(summary.get("rows", -1)) != stage3.expected_rows:
        raise PipelineStateError("Stage3 manifest row count changed")


def _expand_optimization_argv(contract: PipelineContract, upstream: Path) -> tuple[str, ...]:
    return tuple(str(upstream) if item == UPSTREAM_PLACEHOLDER else item for item in contract.optimization.argv_template)


def _read_top_profiles(path: Path, minimum: int) -> list[str]:
    try:
        values = [item.strip() for item in path.read_text(encoding="utf-8-sig").strip().split(",")]
    except (OSError, UnicodeError) as exc:
        raise PipelineStateError(f"cannot read speed top-profiles output: {exc}") from exc
    if len(values) < minimum or any(not item for item in values) or len(values) != len(set(values)):
        raise PipelineStateError("speed top-profiles output is incomplete or duplicated")
    return values


def _speed_artifacts(speed: SpeedContract) -> dict[str, Path]:
    return {"plan": speed.plan, "result": speed.result, "rank": speed.rank, "top": speed.top}


def _audit_speed_marker(contract: PipelineContract) -> None:
    marker = _read_json(contract.speed.marker, "speed completion marker")
    if marker.get("schema_version") != SPEED_MARKER_SCHEMA_VERSION:
        raise PipelineStateError("speed completion marker schema is unsupported")
    if marker.get("contract_sha256") != contract.contract_sha256:
        raise PipelineStateError("speed completion marker contract hash changed")
    artifacts = marker.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(_speed_artifacts(contract.speed)):
        raise PipelineStateError("speed completion marker artifact set changed")
    for name, path in _speed_artifacts(contract.speed).items():
        item = artifacts.get(name)
        if not isinstance(item, dict) or item.get("path") != str(path.resolve(strict=False)):
            raise PipelineStateError(f"speed marker path changed: {name}")
        if not path.is_file() or item.get("sha256") != _file_sha256(path):
            raise PipelineStateError(f"speed marker hash changed: {name}")


def inspect_pipeline(contract: PipelineContract) -> PipelineSnapshot:
    audit_immutable_inputs(contract)
    active_external = audit_external_pid_files(contract)
    if active_external:
        return PipelineSnapshot(
            "wait_external_process",
            "external_live_chain",
            detail={"active_external_processes": active_external},
        )
    s1 = contract.stage1
    campaign_exists = s1.output_dir.is_dir()
    result_exists = s1.result.is_file()
    if not campaign_exists and not result_exists:
        return PipelineSnapshot("run_stage1_campaign", "stage1")
    if not campaign_exists or not result_exists:
        raise PipelineStateError("unsupported partial Stage1 campaign output")
    _audit_csv_coverage(s1.case_plan, s1.result, s1.expected_rows, "Stage1")

    validation_exists = s1.validation.is_file()
    model_exists = s1.model_dir.is_dir()
    metadata_exists = s1.metadata.is_file()
    r2_exists = s1.r2.is_file()
    if not any((validation_exists, model_exists, metadata_exists, r2_exists)):
        return PipelineSnapshot("run_stage1_validation", "stage1")
    if validation_exists and not any((model_exists, metadata_exists, r2_exists)):
        _audit_validation_summary(s1.validation, s1)
        return PipelineSnapshot("run_stage1_training", "stage1")
    if not all((validation_exists, model_exists, metadata_exists, r2_exists)):
        raise PipelineStateError("unsupported partial Stage1 training output")
    stage1_gate = _audit_stage1_training(s1)
    gate_detail = {
        "decision": stage1_gate.decision,
        "min_primary_test_r2": min(stage1_gate.primary_test_r2.values()),
    }

    if not contract.stage2.decision.is_file():
        return PipelineSnapshot("run_stage2_fresh", "stage2", detail=gate_detail)
    stage2_decision = audit_decision(
        contract.stage2.decision,
        schema_version=STAGE2_DECISION_SCHEMA_VERSION,
        allowed_statuses={"stage2_started", "complete", "combined_r2_failed"},
        workdir=contract.workdir,
    )
    stage2_status = str(stage2_decision["status"])
    if stage2_status == "stage2_started":
        return PipelineSnapshot("run_stage2_resume", "stage2", detail=gate_detail)
    upstream: Path
    branch: str
    if stage2_status == "complete":
        upstream = contract.stage2.decision
        branch = "stage2_complete"
    else:
        prior_exists = _audit_pair_presence(
            contract.stage3.prior_plan, contract.stage3.prior_manifest, "Stage12 merge pair"
        )
        if not prior_exists:
            return PipelineSnapshot("merge_stage12_plan", "stage3")
        _audit_merge_pair(contract.stage3, contract.workdir)
        stage3_pair_exists = _audit_pair_presence(
            contract.stage3.plan, contract.stage3.manifest, "Stage3 plan pair"
        )
        if not stage3_pair_exists:
            return PipelineSnapshot("generate_stage3_plan", "stage3")
        _audit_stage3_pair(contract.stage3, contract.workdir)
        if not contract.stage3.decision.is_file():
            return PipelineSnapshot("run_stage3_fresh", "stage3")
        stage3_decision = audit_decision(
            contract.stage3.decision,
            schema_version=STAGE2_DECISION_SCHEMA_VERSION,
            allowed_statuses={"stage2_started", "complete", "combined_r2_failed"},
            workdir=contract.workdir,
        )
        stage3_status = str(stage3_decision["status"])
        if stage3_status == "stage2_started":
            return PipelineSnapshot("run_stage3_resume", "stage3")
        if stage3_status == "combined_r2_failed":
            return PipelineSnapshot(
                "blocked_stage3_r2_failed",
                "stage3_r2_failed",
                terminal=True,
                exit_code=1,
            )
        upstream = contract.stage3.decision
        branch = "stage3_complete"

    opt = contract.optimization
    if not opt.decision.is_file():
        return PipelineSnapshot("run_optimization_fresh", branch, upstream_decision=upstream)
    optimization_decision = audit_decision(
        opt.decision,
        schema_version=OPTIMIZATION_DECISION_SCHEMA_VERSION,
        allowed_statuses={"optimization_started", "pareto_fea_started", "complete", "failed"},
        workdir=contract.workdir,
    )
    optimization_status = str(optimization_decision["status"])
    if optimization_status in {"optimization_started", "pareto_fea_started"}:
        return PipelineSnapshot("run_optimization_resume", branch, upstream_decision=upstream)
    if optimization_status != "complete":
        raise PipelineStateError(f"optimization is not resumable: status={optimization_status}")

    speed = contract.speed
    if speed.marker.is_file():
        _audit_speed_marker(contract)
        return PipelineSnapshot("complete", branch, upstream_decision=upstream, terminal=True)
    plan_exists = speed.plan.is_file()
    output_exists = speed.output_dir.is_dir()
    result_exists = speed.result.is_file()
    rank_exists = speed.rank.is_file()
    top_exists = speed.top.is_file()
    if not any((plan_exists, output_exists, result_exists, rank_exists, top_exists)):
        return PipelineSnapshot("run_speed_plan", branch, upstream_decision=upstream)
    if not plan_exists:
        raise PipelineStateError("speed downstream artifact exists without its case plan")
    try:
        with speed.plan.open("r", encoding="utf-8-sig", newline="") as stream:
            plan_rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PipelineStateError(f"cannot audit speed plan: {exc}") from exc
    case_ids = [str(row.get("case_id") or "").strip() for row in plan_rows]
    if len(plan_rows) != speed.expected_rows or "" in case_ids or len(case_ids) != len(set(case_ids)):
        raise PipelineStateError("speed plan row/case-id contract failed")
    if not output_exists and not result_exists and not rank_exists and not top_exists:
        return PipelineSnapshot("run_speed_campaign", branch, upstream_decision=upstream)
    if output_exists != result_exists:
        raise PipelineStateError("unsupported partial speed campaign output")
    if not output_exists:
        raise PipelineStateError("speed rank output exists before campaign completion")
    _audit_csv_coverage(speed.plan, speed.result, speed.expected_rows, "speed campaign")
    if not rank_exists and not top_exists:
        return PipelineSnapshot("run_speed_rank", branch, upstream_decision=upstream)
    if rank_exists != top_exists:
        raise PipelineStateError("unsupported partial speed rank output")
    if speed.rank.stat().st_size <= 0:
        raise PipelineStateError("speed rank output is empty")
    _read_top_profiles(speed.top, speed.minimum_top_profiles)
    return PipelineSnapshot("commit_speed_completion", branch, upstream_decision=upstream)


def _last_json(stdout: str, label: str) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise PipelineStateError(f"{label} dry-run produced no JSON")
    try:
        value = json.loads(lines[-1], object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise PipelineStateError(f"{label} dry-run did not end with JSON") from exc
    if not isinstance(value, dict):
        raise PipelineStateError(f"{label} dry-run JSON must be an object")
    return value


def run_child(
    argv: Sequence[str],
    *,
    workdir: Path,
    label: str,
    allowed_returncodes: set[int] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(argv),
        cwd=workdir,
        shell=False,
        check=False,
        capture_output=capture_output,
        text=True,
    )
    allowed = allowed_returncodes or {0}
    if completed.returncode not in allowed:
        stderr = completed.stderr or ""
        tail = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "")
        raise PipelineStateError(
            f"{label} returned {completed.returncode}" + (f": {tail[:400]}" if tail else "")
        )
    return completed


def _run_dry_then_execute(
    argv: Sequence[str],
    *,
    workdir: Path,
    label: str,
    execute_suffix: Sequence[str],
    resume: bool = False,
    allowed_execute_returncodes: set[int] | None = None,
    expected_dry_statuses: set[str] | None = None,
    require_execute_matches_dry: bool = False,
) -> None:
    dry_argv = [*argv, *( ["--resume"] if resume else [] )]
    dry = run_child(
        dry_argv,
        workdir=workdir,
        label=f"{label} dry-run",
        capture_output=True,
    )
    proof = _last_json(dry.stdout, label)
    expected_statuses = expected_dry_statuses or ({"stage2_started"} if resume else {"planned"})
    if proof.get("status") not in expected_statuses and not (
        label in {"Stage12 merge", "Stage3 generation"} and proof.get("mode") == "dry-run"
    ):
        raise PipelineStateError(f"{label} dry-run returned unexpected state: {proof.get('status')!r}")
    execute_argv = [*dry_argv, *execute_suffix]
    executed = run_child(
        execute_argv,
        workdir=workdir,
        label=f"{label} execute",
        allowed_returncodes=allowed_execute_returncodes,
        capture_output=require_execute_matches_dry,
    )
    if require_execute_matches_dry:
        committed = _last_json(executed.stdout, f"{label} execute")
        dry_contract = dict(proof)
        committed_contract = dict(committed)
        if dry_contract.pop("mode", None) != "dry-run":
            raise PipelineStateError(f"{label} dry-run did not report mode=dry-run")
        if committed_contract.pop("mode", None) != "write":
            raise PipelineStateError(f"{label} execute did not report mode=write")
        if committed_contract != dry_contract:
            raise PipelineStateError(f"{label} execute changed the dry-run artifact contract")


def _write_speed_marker(contract: PipelineContract) -> None:
    speed = contract.speed
    payload = {
        "artifacts": {
            name: {"path": str(path.resolve(strict=False)), "sha256": _file_sha256(path)}
            for name, path in _speed_artifacts(speed).items()
        },
        "contract_sha256": contract.contract_sha256,
        "schema_version": SPEED_MARKER_SCHEMA_VERSION,
    }
    speed.marker.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{speed.marker.name}.", suffix=".tmp", dir=speed.marker.parent
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        publish_no_replace(staged, speed.marker)
    finally:
        staged.unlink(missing_ok=True)


def execute_action(contract: PipelineContract, snapshot: PipelineSnapshot) -> None:
    action = snapshot.next_action
    workdir = contract.workdir
    if action == "run_stage1_campaign":
        run_child(contract.stage1.campaign_argv, workdir=workdir, label="Stage1 campaign")
    elif action == "run_stage1_validation":
        run_child(contract.stage1.validation_argv, workdir=workdir, label="Stage1 validation")
    elif action == "run_stage1_training":
        # The trainer intentionally returns one when the strict R2 gate fails;
        # the complete artifacts from that outcome are the input to Stage2.
        run_child(
            contract.stage1.training_argv,
            workdir=workdir,
            label="Stage1 training",
            allowed_returncodes={0, 1},
        )
    elif action == "run_stage2_fresh":
        _run_dry_then_execute(
            contract.stage2.argv,
            workdir=workdir,
            label="Stage2 continuation",
            execute_suffix=["--execute"],
            allowed_execute_returncodes={0, 1},
        )
    elif action == "run_stage2_resume":
        _run_dry_then_execute(
            contract.stage2.argv,
            workdir=workdir,
            label="Stage2 continuation",
            execute_suffix=["--execute"],
            resume=True,
            allowed_execute_returncodes={0, 1},
        )
    elif action == "merge_stage12_plan":
        _run_dry_then_execute(
            contract.stage3.merge_argv,
            workdir=workdir,
            label="Stage12 merge",
            execute_suffix=["--execute"],
        )
    elif action == "generate_stage3_plan":
        _run_dry_then_execute(
            contract.stage3.generate_argv,
            workdir=workdir,
            label="Stage3 generation",
            execute_suffix=["--write-stage3"],
            require_execute_matches_dry=True,
        )
    elif action in {"run_stage3_fresh", "run_stage3_resume"}:
        _run_dry_then_execute(
            contract.stage3.continuation_argv,
            workdir=workdir,
            label="Stage3 continuation",
            execute_suffix=["--execute"],
            resume=action.endswith("resume"),
            allowed_execute_returncodes={0, 1},
        )
    elif action in {"run_optimization_fresh", "run_optimization_resume"}:
        if snapshot.upstream_decision is None:
            raise PipelineStateError("optimization action lacks an upstream decision")
        _run_dry_then_execute(
            _expand_optimization_argv(contract, snapshot.upstream_decision),
            workdir=workdir,
            label="optimization continuation",
            execute_suffix=["--execute"],
            resume=action.endswith("resume"),
            expected_dry_statuses=(
                {"optimization_started", "pareto_fea_started"}
                if action.endswith("resume")
                else {"planned"}
            ),
        )
    elif action == "run_speed_plan":
        run_child(contract.speed.plan_argv, workdir=workdir, label="speed plan")
    elif action == "run_speed_campaign":
        run_child(contract.speed.campaign_argv, workdir=workdir, label="speed campaign")
    elif action == "run_speed_rank":
        run_child(contract.speed.rank_argv, workdir=workdir, label="speed rank")
    elif action == "commit_speed_completion":
        _write_speed_marker(contract)
    else:
        raise PipelineStateError(f"action is not executable: {action}")


class ExecutionLock:
    """A process-held lock that is automatically released after a hard restart."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: Any = None

    def __enter__(self) -> "ExecutionLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise PipelineStateError(f"another pipeline supervisor holds the lock: {self.path}") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-transitions", type=int, default=MAX_TRANSITIONS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.max_transitions <= MAX_TRANSITIONS:
        raise PipelineContractError(f"--max-transitions must be between 1 and {MAX_TRANSITIONS}")
    contract = load_contract(args.contract)
    if not args.execute:
        snapshot = inspect_pipeline(contract)
        print(json.dumps(snapshot.report(contract, mode="dry-run"), ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return snapshot.exit_code

    transitions = 0
    preflight = inspect_pipeline(contract)
    if preflight.next_action == "wait_external_process":
        print(
            json.dumps(
                preflight.report(contract, mode="execute", transitions=0),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    with ExecutionLock(contract.lock_path):
        while True:
            snapshot = inspect_pipeline(contract)
            if snapshot.next_action == "wait_external_process":
                print(
                    json.dumps(
                        snapshot.report(contract, mode="execute", transitions=transitions),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                return 0
            if snapshot.terminal:
                print(
                    json.dumps(
                        snapshot.report(contract, mode="execute", transitions=transitions),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                return snapshot.exit_code
            if transitions >= args.max_transitions:
                raise PipelineStateError(
                    f"transition limit reached before a terminal state: {snapshot.next_action}"
                )
            execute_action(contract, snapshot)
            transitions += 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PipelineContractError, PipelineStateError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
