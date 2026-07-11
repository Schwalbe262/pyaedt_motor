"""Run one independent, diagnostic-only Stage1 model-family confirmation.

The default mode is read-only.  ``--execute`` waits for an exact audited
Stage1 result plus the complete official Stage1 validation/model/R2 audit,
then runs the separately frozen untouched-cohort confirmation.  This sidecar
never acquires the sealed pipeline lock and never writes a pipeline artifact.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

from atomic_publish import cleanup_publish_receipt, publish_no_replace
import build_ipmsm_untouched_test_plan as untouched_builder
import confirm_ipmsm_v2_model_families as confirmation
import continue_ipmsm_v2_stage2 as continuation
import diagnose_ipmsm_v2_model_families as diagnostic
import supervise_ipmsm_v2_pipeline as supervisor
import train_ipmsm_lightgbm as trainer


SCHEMA_VERSION = "ipmsm-v2-model-family-confirmation-sidecar-v1"
COMPLETION_SCHEMA_VERSION = "ipmsm-v2-model-family-confirmation-completion-v1"
PID_SCHEMA_VERSION = "ipmsm-v2-model-family-confirmation-pid-v1"
EXPECTED_STAGE1_ROWS = 700
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_OVERALL_TIMEOUT_SECONDS = 86_400.0
DEFAULT_CHILD_TIMEOUT_SECONDS = 21_600.0
MAX_POLL_INTERVAL_SECONDS = 600.0
MAX_OVERALL_TIMEOUT_SECONDS = 86_400.0
MAX_CHILD_TIMEOUT_SECONDS = 86_400.0

LOCK_NAME = "confirmation.lock.json"
REPORT_NAME = "confirmation.json"
COMPLETION_NAME = "completion.json"


class ConfirmationWatcherError(RuntimeError):
    """The independent confirmation sidecar cannot safely continue."""


@dataclass(frozen=True)
class SidecarPaths:
    root: Path
    lock_output: Path
    report: Path
    completion: Path
    pid: Path
    execution_lock: Path


@dataclass(frozen=True)
class FrozenInputs:
    baseline_metadata: Path
    frozen_selection_manifest: Path
    audit_case_plan: Path
    untouched_plan_manifest: Path
    full_case_plan: Path
    explored_case_plan: Path

    def as_mapping(self) -> dict[str, Path]:
        return {
            "baseline_metadata": self.baseline_metadata,
            "frozen_selection_manifest": self.frozen_selection_manifest,
            "audit_case_plan": self.audit_case_plan,
            "untouched_plan_manifest": self.untouched_plan_manifest,
            "full_case_plan": self.full_case_plan,
            "explored_case_plan": self.explored_case_plan,
        }


@dataclass(frozen=True)
class BoundContext:
    contract: supervisor.PipelineContract
    contract_file_sha256: str
    sources: Mapping[str, Path]
    source_sha256: Mapping[str, str]
    inputs: FrozenInputs
    input_sha256: Mapping[str, str]
    frozen_selection: Mapping[str, Any]
    untouched_contract: Mapping[str, Any]

    def identity(self) -> dict[str, Any]:
        return {
            "contract": str(self.contract.source),
            "contract_sha256": self.contract.contract_sha256,
            "contract_file_sha256": self.contract_file_sha256,
            "sources": {
                name: {"path": str(self.sources[name]), "sha256": self.source_sha256[name]}
                for name in sorted(self.sources)
            },
            "inputs": {
                name: {"path": str(path), "sha256": self.input_sha256[name]}
                for name, path in sorted(self.inputs.as_mapping().items())
            },
        }


@dataclass(frozen=True)
class Readiness:
    ready: bool
    phase: str
    detail: str
    data_sha256: str = ""
    gate_decision: str = ""
    gate_passed: bool | None = None
    validation_sha256: str = ""
    metadata_sha256: str = ""
    r2_sha256: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "phase": self.phase,
            "detail": self.detail,
            "data_sha256": self.data_sha256,
            "gate_decision": self.gate_decision,
            "gate_passed": self.gate_passed,
            "validation_sha256": self.validation_sha256,
            "metadata_sha256": self.metadata_sha256,
            "r2_sha256": self.r2_sha256,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ConfirmationWatcherError(f"cannot hash file: {path}") from exc
    return digest.hexdigest()


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw, value = confirmation.read_json_document(path)
    except (OSError, UnicodeError, ValueError, confirmation.ConfirmationError) as exc:
        raise ConfirmationWatcherError(f"cannot read {label}: {path}") from exc
    if not raw:
        raise ConfirmationWatcherError(f"{label} is empty: {path}")
    return value


def publish_json_no_replace(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staged = Path(name)
    receipt = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        receipt = publish_no_replace(staged, path)
    finally:
        if receipt is not None:
            cleanup_publish_receipt(receipt)
        staged.unlink(missing_ok=True)


def same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve(strict=False))) == os.path.normcase(
        str(second.resolve(strict=False))
    )


def overlaps(first: Path, second: Path) -> bool:
    left = first.resolve(strict=False)
    right = second.resolve(strict=False)
    return same_path(left, right) or left.is_relative_to(right) or right.is_relative_to(left)


def make_paths(root: Path, pid_file: Path | None = None) -> SidecarPaths:
    root = root.resolve(strict=False)
    sibling = root.parent
    pid = (
        pid_file.resolve(strict=False)
        if pid_file is not None
        else sibling / f".{root.name}.pid.json"
    )
    return SidecarPaths(
        root=root,
        lock_output=root / LOCK_NAME,
        report=root / REPORT_NAME,
        completion=root / COMPLETION_NAME,
        pid=pid,
        execution_lock=sibling / f".{root.name}.execution.lock",
    )


def source_paths() -> dict[str, Path]:
    return {
        "watcher": Path(__file__).resolve(),
        "confirmation": Path(confirmation.__file__).resolve(),
        "trainer": Path(trainer.__file__).resolve(),
        "diagnostic": Path(diagnostic.__file__).resolve(),
        "untouched_builder": Path(untouched_builder.__file__).resolve(),
    }


def load_bound_context(contract_path: Path) -> BoundContext:
    resolved_contract = contract_path.resolve(strict=True)
    contract_file_sha256 = sha256_file(resolved_contract)
    try:
        contract = supervisor.load_contract(resolved_contract)
        supervisor.audit_immutable_inputs(contract)
    except (supervisor.PipelineContractError, supervisor.PipelineStateError) as exc:
        raise ConfirmationWatcherError(f"pipeline contract audit failed: {exc}") from exc
    if contract.stage1.expected_rows != EXPECTED_STAGE1_ROWS:
        raise ConfirmationWatcherError(
            f"Stage1 expected_rows changed: {contract.stage1.expected_rows}"
        )

    artifact_dir = contract.source.parent
    inputs = FrozenInputs(
        baseline_metadata=(
            artifact_dir / "foundation_stage1_provisional60_v1" / "models" / "training_metadata.json"
        ).resolve(strict=False),
        frozen_selection_manifest=(
            artifact_dir / "foundation_stage1_provisional60_model_family_diagnostic_v5.selection.json"
        ).resolve(strict=False),
        audit_case_plan=(
            artifact_dir / "foundation_stage1_untouched_test8_plan_v3.csv"
        ).resolve(strict=False),
        untouched_plan_manifest=(
            artifact_dir / "foundation_stage1_untouched_test8_plan_v3.manifest.json"
        ).resolve(strict=False),
        full_case_plan=contract.stage1.case_plan.resolve(strict=False),
        explored_case_plan=(
            artifact_dir
            / "foundation_stage1_provisional60_v1"
            / "snapshot"
            / "selected_cases.csv"
        ).resolve(strict=False),
    )
    input_sha256 = {
        name: sha256_file(path) for name, path in inputs.as_mapping().items()
    }
    frozen_bytes, frozen_manifest = confirmation.read_json_document(
        inputs.frozen_selection_manifest
    )
    untouched_bytes, untouched_manifest = confirmation.read_json_document(
        inputs.untouched_plan_manifest
    )
    try:
        frozen = confirmation.validate_frozen_selection(
            frozen_manifest,
            manifest_sha256=hashlib.sha256(frozen_bytes).hexdigest(),
            expected_manifest_sha256=confirmation.FROZEN_SELECTION_MANIFEST_SHA256,
            expected_selection_sha256=confirmation.FROZEN_SELECTION_SHA256,
            baseline_metadata_sha256=input_sha256["baseline_metadata"],
        )
        untouched = confirmation.validate_untouched_contract(
            full_plan=inputs.full_case_plan,
            explored_plan=inputs.explored_case_plan,
            audit_case_plan=inputs.audit_case_plan,
            manifest=untouched_manifest,
            manifest_sha256=hashlib.sha256(untouched_bytes).hexdigest(),
            expected_manifest_sha256=confirmation.UNTOUCHED_PLAN_MANIFEST_SHA256,
            frozen_selection=frozen["selection"],
        )
    except (confirmation.ConfirmationError, diagnostic.DiagnosticError, OSError, ValueError) as exc:
        raise ConfirmationWatcherError(f"frozen confirmation input audit failed: {exc}") from exc
    if not same_path(inputs.full_case_plan, contract.stage1.case_plan):
        raise ConfirmationWatcherError("frozen full plan differs from the sealed Stage1 case plan")

    sources = source_paths()
    source_sha256 = {name: sha256_file(path) for name, path in sources.items()}
    return BoundContext(
        contract=contract,
        contract_file_sha256=contract_file_sha256,
        sources=sources,
        source_sha256=source_sha256,
        inputs=inputs,
        input_sha256=input_sha256,
        frozen_selection=frozen,
        untouched_contract=untouched,
    )


def assert_bound_context(bound: BoundContext) -> None:
    current = load_bound_context(bound.contract.source)
    if current.identity() != bound.identity():
        raise ConfirmationWatcherError(
            "contract, helper source, or frozen confirmation input changed during execution"
        )


def protected_paths(bound: BoundContext) -> list[Path]:
    contract = bound.contract
    return [
        contract.source,
        *bound.sources.values(),
        *bound.inputs.as_mapping().values(),
        contract.lock_path,
        *(item.path for item in contract.immutable_inputs),
        *(item.path for item in contract.external_pid_files),
        contract.stage1.case_plan,
        contract.stage1.output_dir,
        contract.stage1.result,
        contract.stage1.validation,
        contract.stage1.model_dir,
        contract.stage1.metadata,
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
    ]


def validate_paths(paths: SidecarPaths, bound: BoundContext) -> None:
    outputs = [
        paths.root,
        paths.lock_output,
        paths.report,
        paths.completion,
        paths.pid,
        paths.execution_lock,
    ]
    if len({os.path.normcase(str(path)) for path in outputs}) != len(outputs):
        raise ConfirmationWatcherError("sidecar output, PID, and lock paths must be distinct")
    if paths.root == paths.root.parent:
        raise ConfirmationWatcherError("output root cannot be a filesystem root")
    if paths.pid.is_relative_to(paths.root) or paths.execution_lock.is_relative_to(paths.root):
        raise ConfirmationWatcherError("PID and execution lock must stay outside output root")
    for candidate in outputs:
        for protected in protected_paths(bound):
            if overlaps(candidate, protected):
                raise ConfirmationWatcherError(
                    f"independent sidecar path overlaps sealed pipeline state: {candidate}"
                )
    if paths.root.exists() and not paths.root.is_dir():
        raise ConfirmationWatcherError("sidecar output root exists but is not a directory")


def output_state(paths: SidecarPaths) -> frozenset[str]:
    if not paths.root.exists():
        return frozenset()
    if not paths.root.is_dir():
        raise ConfirmationWatcherError("sidecar output root is not a directory")
    names: set[str] = set()
    for path in paths.root.iterdir():
        if not path.is_file():
            raise ConfirmationWatcherError(f"unexpected sidecar output entry: {path}")
        names.add(path.name)
    allowed = {
        frozenset(),
        frozenset({LOCK_NAME}),
        frozenset({LOCK_NAME, REPORT_NAME}),
        frozenset({LOCK_NAME, REPORT_NAME, COMPLETION_NAME}),
    }
    state = frozenset(names)
    if state not in allowed:
        raise ConfirmationWatcherError(
            "sidecar output is not an exact supported prefix: " + ", ".join(sorted(names))
        )
    return state


def inspect_readiness(bound: BoundContext) -> Readiness:
    assert_bound_context(bound)
    stage1 = bound.contract.stage1
    output_present = os.path.lexists(stage1.output_dir)
    result_present = os.path.lexists(stage1.result)
    output_exists = stage1.output_dir.is_dir()
    result_exists = stage1.result.is_file()
    if not output_present and not result_present:
        return Readiness(False, "stage1_results", "waiting for atomic Stage1 result publication")
    if (output_present and not output_exists) or (result_present and not result_exists):
        raise ConfirmationWatcherError("visible Stage1 campaign output has an invalid path type")
    if not output_exists or not result_exists:
        raise ConfirmationWatcherError("visible Stage1 campaign output is structurally partial")
    try:
        supervisor._audit_csv_coverage(
            stage1.case_plan,
            stage1.result,
            stage1.expected_rows,
            "Stage1",
        )
    except supervisor.PipelineStateError as exc:
        raise ConfirmationWatcherError(f"visible Stage1 result failed exact coverage: {exc}") from exc
    data_sha256 = sha256_file(stage1.result)

    validation_present = os.path.lexists(stage1.validation)
    model_dir_present = os.path.lexists(stage1.model_dir)
    metadata_present = os.path.lexists(stage1.metadata)
    r2_present = os.path.lexists(stage1.r2)
    validation_exists = stage1.validation.is_file()
    model_dir_exists = stage1.model_dir.exists()
    metadata_exists = stage1.metadata.is_file()
    r2_exists = stage1.r2.is_file()
    invalid_paths = [
        name
        for name, present, valid in (
            ("validation", validation_present, validation_exists),
            ("model_dir", model_dir_present, stage1.model_dir.is_dir()),
            ("metadata", metadata_present, metadata_exists),
            ("r2", r2_present, r2_exists),
        )
        if present and not valid
    ]
    if invalid_paths:
        raise ConfirmationWatcherError(
            "official Stage1 artifact has an invalid path type: "
            + ", ".join(invalid_paths)
        )
    downstream_visible = model_dir_exists or metadata_exists or r2_exists
    if not validation_exists:
        if downstream_visible:
            raise ConfirmationWatcherError(
                "official Stage1 model artifacts appeared before validation"
            )
        return Readiness(
            False,
            "official_validation",
            "exact Stage1 results ready; waiting for official validation",
            data_sha256,
        )
    try:
        supervisor._audit_validation_summary(stage1.validation, stage1)
    except supervisor.PipelineStateError as exc:
        raise ConfirmationWatcherError(f"official Stage1 validation is corrupt: {exc}") from exc
    if not (model_dir_exists and metadata_exists and r2_exists):
        visible = [
            name
            for name, exists in (
                ("model_dir", model_dir_exists),
                ("metadata", metadata_exists),
                ("r2", r2_exists),
            )
            if exists
        ]
        return Readiness(
            False,
            "official_training",
            "waiting for complete official Stage1 training"
            + (f"; visible={','.join(visible)}" if visible else ""),
            data_sha256,
        )
    official_before = {
        "validation_sha256": sha256_file(stage1.validation),
        "metadata_sha256": sha256_file(stage1.metadata),
        "r2_sha256": sha256_file(stage1.r2),
    }
    try:
        gate = supervisor._audit_stage1_training(stage1)
    except supervisor.PipelineStateError as exc:
        raise ConfirmationWatcherError(
            f"complete official Stage1 model/R2 artifacts failed audit: {exc}"
        ) from exc
    official_after = {
        "validation_sha256": sha256_file(stage1.validation),
        "metadata_sha256": sha256_file(stage1.metadata),
        "r2_sha256": sha256_file(stage1.r2),
    }
    if official_after != official_before:
        raise ConfirmationWatcherError(
            "official Stage1 validation/model/R2 files changed during audit"
        )
    return Readiness(
        ready=True,
        phase="ready",
        detail="exact Stage1 results and official Stage1 validation/model/R2 audit are complete",
        data_sha256=data_sha256,
        gate_decision=gate.decision,
        gate_passed=gate.passed,
        **official_after,
    )


def confirmation_namespace(
    bound: BoundContext,
    paths: SidecarPaths,
    *,
    n_jobs: int,
    resume: bool,
) -> argparse.Namespace:
    return argparse.Namespace(
        data=bound.contract.stage1.result,
        baseline_metadata=bound.inputs.baseline_metadata,
        frozen_selection_manifest=bound.inputs.frozen_selection_manifest,
        audit_case_plan=bound.inputs.audit_case_plan,
        untouched_plan_manifest=bound.inputs.untouched_plan_manifest,
        full_case_plan=bound.inputs.full_case_plan,
        explored_case_plan=bound.inputs.explored_case_plan,
        lock_output=paths.lock_output,
        output=paths.report,
        n_jobs=n_jobs,
        resume=resume,
    )


def build_confirmation_argv(
    bound: BoundContext,
    paths: SidecarPaths,
    *,
    n_jobs: int,
    resume: bool,
) -> list[str]:
    values = confirmation_namespace(bound, paths, n_jobs=n_jobs, resume=resume)
    argv = [
        sys.executable,
        str(bound.sources["confirmation"]),
        "--data",
        str(values.data),
        "--baseline-metadata",
        str(values.baseline_metadata),
        "--frozen-selection-manifest",
        str(values.frozen_selection_manifest),
        "--audit-case-plan",
        str(values.audit_case_plan),
        "--untouched-plan-manifest",
        str(values.untouched_plan_manifest),
        "--full-case-plan",
        str(values.full_case_plan),
        "--explored-case-plan",
        str(values.explored_case_plan),
        "--lock-output",
        str(values.lock_output),
        "--output",
        str(values.output),
        "--n-jobs",
        str(n_jobs),
    ]
    if resume:
        argv.append("--resume")
    return argv


def confirmation_paths_mapping(
    bound: BoundContext,
    paths: SidecarPaths,
) -> dict[str, Path]:
    values = confirmation_namespace(bound, paths, n_jobs=1, resume=True)
    return confirmation._confirmation_paths(values)


def audit_lock_only(
    bound: BoundContext,
    paths: SidecarPaths,
    readiness: Readiness,
) -> dict[str, Any]:
    if not readiness.ready:
        raise ConfirmationWatcherError("confirmation lock exists before readiness is provable")
    try:
        confirmation_paths = confirmation_paths_mapping(bound, paths)
        context = confirmation._build_confirmation_context(confirmation_paths)
        file_sha256 = confirmation._validate_exact_lock(
            paths.lock_output,
            context["lock"],
        )
    except (confirmation.ConfirmationError, diagnostic.DiagnosticError, OSError, ValueError) as exc:
        raise ConfirmationWatcherError(f"confirmation lock audit failed: {exc}") from exc
    if context["lock"]["lock"].get("data_sha256") != readiness.data_sha256:
        raise ConfirmationWatcherError("confirmation lock data SHA256 differs from Stage1")
    return {"context": context, "lock_file_sha256": file_sha256}


def audit_lock_and_report(
    bound: BoundContext,
    paths: SidecarPaths,
    readiness: Readiness,
) -> tuple[dict[str, Any], str, str]:
    audited = audit_lock_only(bound, paths, readiness)
    try:
        report = confirmation._audit_completed_report(
            confirmation_paths_mapping(bound, paths),
            audited["context"],
            lock_file_sha256=audited["lock_file_sha256"],
        )
    except (confirmation.ConfirmationError, diagnostic.DiagnosticError, OSError, ValueError) as exc:
        raise ConfirmationWatcherError(f"confirmation report audit failed: {exc}") from exc
    return report, audited["lock_file_sha256"], sha256_file(paths.report)


def completion_document(
    bound: BoundContext,
    paths: SidecarPaths,
    readiness: Readiness,
    report: Mapping[str, Any],
    lock_file_sha256: str,
    report_file_sha256: str,
) -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "schema_version": COMPLETION_SCHEMA_VERSION,
        "status": "complete",
        "diagnostic_only": True,
        "official_gate_eligible": False,
        "production_eligible": False,
        "contract": {
            "path": str(bound.contract.source),
            "contract_sha256": bound.contract.contract_sha256,
            "file_sha256": bound.contract_file_sha256,
        },
        "data": {
            "path": str(bound.contract.stage1.result),
            "sha256": readiness.data_sha256,
            "rows": bound.contract.stage1.expected_rows,
        },
        "official_stage1": {
            "validation": {
                "path": str(bound.contract.stage1.validation),
                "sha256": readiness.validation_sha256,
            },
            "metadata": {
                "path": str(bound.contract.stage1.metadata),
                "sha256": readiness.metadata_sha256,
            },
            "r2": {
                "path": str(bound.contract.stage1.r2),
                "sha256": readiness.r2_sha256,
            },
            "gate_decision": readiness.gate_decision,
            "gate_passed": readiness.gate_passed,
        },
        "sources": {
            name: {"path": str(bound.sources[name]), "sha256": bound.source_sha256[name]}
            for name in sorted(bound.sources)
        },
        "inputs": {
            name: {"path": str(path), "sha256": bound.input_sha256[name]}
            for name, path in sorted(bound.inputs.as_mapping().items())
        },
        "confirmation_lock": {
            "path": str(paths.lock_output),
            "sha256": lock_file_sha256,
        },
        "confirmation_report": {
            "path": str(paths.report),
            "sha256": report_file_sha256,
            "status": report["status"],
        },
    }
    return {**unsigned, "completion_sha256": canonical_sha256(unsigned)}


def audit_completion(
    bound: BoundContext,
    paths: SidecarPaths,
    readiness: Readiness,
) -> dict[str, Any]:
    report, lock_hash, report_hash = audit_lock_and_report(
        bound,
        paths,
        readiness,
    )
    expected = completion_document(
        bound,
        paths,
        readiness,
        report,
        lock_hash,
        report_hash,
    )
    actual = read_json(paths.completion, "completion manifest")
    try:
        raw = paths.completion.read_bytes()
    except OSError as exc:
        raise ConfirmationWatcherError("cannot read completion manifest bytes") from exc
    if raw != canonical_json_bytes(actual) or actual != expected:
        raise ConfirmationWatcherError("completion manifest differs from exact replay")
    return report


def run_child(
    argv: Sequence[str],
    *,
    workdir: Path,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            shell=False,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfirmationWatcherError(
            f"confirmation child timed out after {timeout_seconds:.0f}s"
        ) from exc
    if completed.returncode != 0:
        lines = [line.strip() for line in (completed.stderr or "").splitlines() if line.strip()]
        tail = lines[-1][:300] if lines else ""
        raise ConfirmationWatcherError(
            f"confirmation child returned {completed.returncode}" + (f": {tail}" if tail else "")
        )
    return completed


def marker_is_current_boot(path: Path) -> bool:
    boot = supervisor._boot_time_epoch()
    if boot is None:
        return True
    try:
        return path.stat().st_mtime >= boot - 2.0
    except OSError as exc:
        raise ConfirmationWatcherError(f"cannot stat PID marker: {path}") from exc


class PidMarker:
    def __init__(
        self,
        path: Path,
        bound: BoundContext,
        output_dir: Path,
        *,
        resume: bool,
    ) -> None:
        self.path = path
        self.bound = bound
        self.output_dir = output_dir
        self.resume = resume
        self.nonce = secrets.token_hex(16)
        self.owned = False

    def expected_identity(self) -> dict[str, Any]:
        return {
            "schema_version": PID_SCHEMA_VERSION,
            "contract_sha256": self.bound.contract.contract_sha256,
            "contract_file_sha256": self.bound.contract_file_sha256,
            "watcher_sha256": self.bound.source_sha256["watcher"],
            "output_dir": str(self.output_dir),
        }

    def __enter__(self) -> "PidMarker":
        if self.path.exists():
            marker = read_json(self.path, "PID marker")
            expected_fields = {
                *self.expected_identity(),
                "pid",
                "nonce",
                "boot_time_epoch",
            }
            if set(marker) != expected_fields or any(
                marker.get(key) != value for key, value in self.expected_identity().items()
            ):
                raise ConfirmationWatcherError("PID marker belongs to another sidecar identity")
            pid = marker.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                raise ConfirmationWatcherError("PID marker pid is invalid")
            current_boot = marker_is_current_boot(self.path)
            try:
                running = current_boot and continuation.pid_is_running(pid)
            except OSError as exc:
                raise ConfirmationWatcherError(f"cannot inspect PID marker owner {pid}") from exc
            if running:
                raise ConfirmationWatcherError(
                    f"another model-family confirmation watcher is active: pid={pid}"
                )
            if not self.resume:
                raise ConfirmationWatcherError("stale PID marker requires --resume")
            self.path.unlink()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **self.expected_identity(),
            "pid": os.getpid(),
            "nonce": self.nonce,
            "boot_time_epoch": supervisor._boot_time_epoch(),
        }
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        self.owned = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self.owned:
            return
        try:
            marker = read_json(self.path, "PID marker")
            if marker.get("pid") == os.getpid() and marker.get("nonce") == self.nonce:
                self.path.unlink(missing_ok=True)
        except ConfirmationWatcherError:
            pass


def result_report(
    bound: BoundContext,
    paths: SidecarPaths,
    readiness: Readiness,
    *,
    mode: str,
    status: str,
    output_prefix: frozenset[str],
    command: Sequence[str],
    confirmation_status: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "status": status,
        "diagnostic_only": True,
        "official_gate_eligible": False,
        "production_eligible": False,
        "contract_sha256": bound.contract.contract_sha256,
        "output_dir": str(paths.root),
        "output_prefix": sorted(output_prefix),
        "readiness": readiness.as_mapping(),
        "confirmation_status": confirmation_status,
        "command": list(command),
    }


def inspect_sidecar(
    bound: BoundContext,
    paths: SidecarPaths,
    *,
    n_jobs: int,
) -> dict[str, Any]:
    state = output_state(paths)
    readiness = inspect_readiness(bound)
    command = build_confirmation_argv(
        bound,
        paths,
        n_jobs=n_jobs,
        resume=state == frozenset({LOCK_NAME}),
    )
    if state == frozenset({LOCK_NAME, REPORT_NAME, COMPLETION_NAME}):
        if not readiness.ready:
            raise ConfirmationWatcherError("complete sidecar no longer has audited readiness")
        report = audit_completion(bound, paths, readiness)
        return result_report(
            bound,
            paths,
            readiness,
            mode="dry-run",
            status="already_complete",
            output_prefix=state,
            command=command,
            confirmation_status=str(report["status"]),
        )
    if state == frozenset({LOCK_NAME, REPORT_NAME}):
        if not readiness.ready:
            raise ConfirmationWatcherError("confirmation report exists before audited readiness")
        report, _, _ = audit_lock_and_report(bound, paths, readiness)
        return result_report(
            bound,
            paths,
            readiness,
            mode="dry-run",
            status="completion_pending",
            output_prefix=state,
            command=command,
            confirmation_status=str(report["status"]),
        )
    if state == frozenset({LOCK_NAME}):
        if not readiness.ready:
            raise ConfirmationWatcherError("confirmation lock exists before audited readiness")
        audit_lock_only(bound, paths, readiness)
        return result_report(
            bound,
            paths,
            readiness,
            mode="dry-run",
            status="resume_required",
            output_prefix=state,
            command=command,
        )
    return result_report(
        bound,
        paths,
        readiness,
        mode="dry-run",
        status="ready" if readiness.ready else "waiting",
        output_prefix=state,
        command=command,
    )


def execute_sidecar(
    bound: BoundContext,
    paths: SidecarPaths,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started = time.monotonic()
    with supervisor.ExecutionLock(paths.execution_lock):
        with PidMarker(paths.pid, bound, paths.root, resume=args.resume):
            while True:
                state = output_state(paths)
                readiness = inspect_readiness(bound)
                if state == frozenset({LOCK_NAME, REPORT_NAME, COMPLETION_NAME}):
                    report = audit_completion(bound, paths, readiness)
                    return result_report(
                        bound,
                        paths,
                        readiness,
                        mode="execute",
                        status="already_complete",
                        output_prefix=state,
                        command=(),
                        confirmation_status=str(report["status"]),
                    )
                if state and not args.resume:
                    raise ConfirmationWatcherError(
                        "incomplete sidecar output requires --resume"
                    )
                if readiness.ready:
                    break
                elapsed = time.monotonic() - started
                if elapsed + args.poll_interval_seconds > args.overall_timeout_seconds:
                    raise ConfirmationWatcherError(
                        f"bounded readiness poll timed out in phase={readiness.phase}"
                    )
                print(
                    json.dumps(
                        {
                            "event": "waiting",
                            "phase": readiness.phase,
                            "detail": readiness.detail,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    flush=True,
                )
                time.sleep(args.poll_interval_seconds)

            assert_bound_context(bound)
            readiness = inspect_readiness(bound)
            state = output_state(paths)
            if state == frozenset({LOCK_NAME, REPORT_NAME}):
                report, lock_hash, report_hash = audit_lock_and_report(
                    bound,
                    paths,
                    readiness,
                )
            elif state in {frozenset(), frozenset({LOCK_NAME})}:
                resume_child = state == frozenset({LOCK_NAME})
                if resume_child:
                    audit_lock_only(bound, paths, readiness)
                pre_child_readiness = readiness.as_mapping()
                command = build_confirmation_argv(
                    bound,
                    paths,
                    n_jobs=args.n_jobs,
                    resume=resume_child,
                )
                run_child(
                    command,
                    workdir=bound.contract.workdir,
                    timeout_seconds=args.child_timeout_seconds,
                )
                assert_bound_context(bound)
                readiness = inspect_readiness(bound)
                if readiness.as_mapping() != pre_child_readiness:
                    raise ConfirmationWatcherError(
                        "official Stage1 readiness identity changed while confirmation ran"
                    )
                if output_state(paths) != frozenset({LOCK_NAME, REPORT_NAME}):
                    raise ConfirmationWatcherError(
                        "confirmation child did not publish the exact lock/report pair"
                    )
                report, lock_hash, report_hash = audit_lock_and_report(
                    bound,
                    paths,
                    readiness,
                )
            else:
                raise ConfirmationWatcherError("unsupported sidecar state before confirmation")

            pre_publish_readiness = readiness.as_mapping()
            assert_bound_context(bound)
            readiness = inspect_readiness(bound)
            if readiness.as_mapping() != pre_publish_readiness:
                raise ConfirmationWatcherError(
                    "official Stage1 readiness identity changed before completion publication"
                )
            report, lock_hash, report_hash = audit_lock_and_report(
                bound,
                paths,
                readiness,
            )
            assert_bound_context(bound)
            readiness = inspect_readiness(bound)
            if readiness.as_mapping() != pre_publish_readiness:
                raise ConfirmationWatcherError(
                    "official Stage1 readiness identity changed during completion audit"
                )
            completion = completion_document(
                bound,
                paths,
                readiness,
                report,
                lock_hash,
                report_hash,
            )
            publish_json_no_replace(paths.completion, completion)
            if output_state(paths) != frozenset({LOCK_NAME, REPORT_NAME, COMPLETION_NAME}):
                raise ConfirmationWatcherError("completion manifest publication is incomplete")
            audit_completion(bound, paths, readiness)
            return result_report(
                bound,
                paths,
                readiness,
                mode="execute",
                status="complete",
                output_prefix=output_state(paths),
                command=(),
                confirmation_status=str(report["status"]),
            )


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
    parser.add_argument(
        "--child-timeout-seconds",
        type=float,
        default=DEFAULT_CHILD_TIMEOUT_SECONDS,
    )
    parser.add_argument("--n-jobs", type=int, required=True)
    return parser


def validate_cli(args: argparse.Namespace) -> None:
    if not math.isfinite(args.poll_interval_seconds) or not (
        1.0 <= args.poll_interval_seconds <= MAX_POLL_INTERVAL_SECONDS
    ):
        raise ConfirmationWatcherError(
            f"--poll-interval-seconds must be between 1 and {MAX_POLL_INTERVAL_SECONDS:g}"
        )
    if not math.isfinite(args.overall_timeout_seconds) or not (
        args.poll_interval_seconds
        <= args.overall_timeout_seconds
        <= MAX_OVERALL_TIMEOUT_SECONDS
    ):
        raise ConfirmationWatcherError(
            f"--overall-timeout-seconds must be bounded by the poll interval and {MAX_OVERALL_TIMEOUT_SECONDS:g}"
        )
    if not math.isfinite(args.child_timeout_seconds) or not (
        60.0 <= args.child_timeout_seconds <= MAX_CHILD_TIMEOUT_SECONDS
    ):
        raise ConfirmationWatcherError(
            f"--child-timeout-seconds must be between 60 and {MAX_CHILD_TIMEOUT_SECONDS:g}"
        )
    if isinstance(args.n_jobs, bool) or args.n_jobs == 0 or args.n_jobs < -1:
        raise ConfirmationWatcherError("--n-jobs must be -1 or a positive integer")
    if args.resume and not args.execute:
        # Read-only resume inspection is meaningful and creates no paths.
        return


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_cli(args)
        bound = load_bound_context(args.contract)
        paths = make_paths(args.output_dir, args.pid_file)
        validate_paths(paths, bound)
        result = (
            execute_sidecar(bound, paths, args)
            if args.execute
            else inspect_sidecar(bound, paths, n_jobs=args.n_jobs)
        )
    except (
        ConfirmationWatcherError,
        confirmation.ConfirmationError,
        diagnostic.DiagnosticError,
        supervisor.PipelineContractError,
        supervisor.PipelineStateError,
        OSError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("ERROR: model-family confirmation watcher interrupted", file=sys.stderr)
        raise SystemExit(130)
