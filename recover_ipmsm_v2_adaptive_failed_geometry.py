"""Build one authority-bound adaptive failed-geometry recovery plan.

The command is read-only by default.  ``--execute`` atomically publishes a
fresh effective 300-row case plan and its recovery manifest.  It never edits
the original adaptive plan, adaptive manifest, continuation decision, or
terminal campaign evidence.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import socket
from typing import Any, Iterable, Mapping, Sequence

import collect_ipmsm_v2_campaign as collector
import continue_ipmsm_v2_stage2 as continuation
import generate_ipmsm_v2_adaptive_batch as adaptive
import generate_ipmsm_v2_cases as foundation
from ipmsm_optimization import GEOMETRY_VARIABLE_NAMES, OptimizationSpec, optimization_spec_from_mapping
import replace_ipmsm_v2_failed_geometry as geometry_replacement
import revise_ipmsm_v2_clean_retry_plan as clean_retry


SCHEMA_VERSION = "ipmsm_v2_adaptive_failed_geometry_recovery_v1"
EXPECTED_ROWS = 300
EXPECTED_GROUPS = 50
EXPECTED_TRAIN_GROUPS = 40
EXPECTED_CALIBRATION_GROUPS = 10
ROWS_PER_GROUP = 6
EXPECTED_SUCCESSFUL_ROWS = EXPECTED_ROWS - ROWS_PER_GROUP
EXPECTED_FAILURE_RESULTS = ROWS_PER_GROUP * 2
RETRY_SUFFIX = "_clean_retry_01"
DESIGN_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
ORIGINAL_MANIFEST_FIELDS = frozenset(
    {
        "case_plan",
        "case_plan_sha256",
        "confirmed_exclusions",
        "excluded_design_hashes",
        "excluded_design_hashes_sha256",
        "execution_contract",
        "execution_contract_sha256",
        "failed_gate_evidence",
        "fixed_audit_case_plan",
        "mode",
        "r2_history",
        "schema_version",
        "selection",
        "source_case_plans",
        "spec",
        "summary",
    }
)
RECOVERY_CONTRACT_FIELDS = frozenset(
    {
        "continuation_decision",
        "failure",
        "original",
        "output",
        "replacement",
        "scheduler_identity",
        "selection_proof",
        "terminal_campaign",
    }
)
SCHEDULER_IDENTITY_FIELDS = frozenset(
    {
        "log_dir",
        "project",
        "project_active_cap",
        "remote_cases_dir",
        "result_dir",
        "scheduler_url",
        "simulation_dir",
        "task_prefix",
    }
)


class AdaptiveRecoveryError(RuntimeError):
    """Raised when recovery evidence is incomplete, changed, or unsafe."""


@dataclass(frozen=True)
class Snapshot:
    path: Path
    payload: bytes
    sha256: str
    identity: tuple[int, int, int, int]

    def artifact(self) -> dict[str, str]:
        return {"path": str(self.path), "sha256": self.sha256}


@dataclass(frozen=True)
class OriginalAuthority:
    spec: OptimizationSpec
    spec_snapshot: Snapshot
    plan_snapshot: Snapshot
    manifest_snapshot: Snapshot
    fieldnames: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    manifest: dict[str, Any]
    excluded_design_hashes: frozenset[str]
    adaptive_evidence: Mapping[str, Any]
    evidence_snapshots: tuple[Snapshot, ...]


@dataclass(frozen=True)
class ContinuationAuthority:
    snapshot: Snapshot
    scheduler_identity: dict[str, Any]
    output_dir: Path


@dataclass(frozen=True)
class TerminalAuthority:
    summary_snapshot: Snapshot
    decision_snapshot: Snapshot
    selected_plan_snapshot: Snapshot
    successful_plan_snapshot: Snapshot
    merged_snapshot: Snapshot
    failed_design_hash: str
    failed_geometry_group_id: str
    failed_case_ids: tuple[str, ...]
    failure_results: tuple[dict[str, Any], ...]
    evidence_snapshots: tuple[Snapshot, ...]


@dataclass(frozen=True)
class CandidateSelection:
    design_hash: str
    pool_ordinal: int
    acquisition_score: float
    diversity_score: float
    final_selection_score: float
    selection_constraint: str
    signals: dict[str, float]
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ReplacementRows:
    rows: tuple[dict[str, str], ...]
    case_id_map: tuple[tuple[str, str], ...]
    replacement_geometry_group_id: str
    payload: bytes


@dataclass(frozen=True)
class RecoveryBuild:
    output_payload: bytes
    manifest: dict[str, Any]
    snapshots: tuple[Snapshot, ...]


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError as exc:
        raise AdaptiveRecoveryError(f"cannot inspect path identity {path}: {exc}") from exc
    return bool(attributes & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _snapshot(path: Path, label: str) -> Snapshot:
    resolved = path.resolve(strict=False)
    try:
        if _is_reparse_point(path) or not path.is_file():
            raise AdaptiveRecoveryError(f"{label} must be a regular non-reparse file: {path}")
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except AdaptiveRecoveryError:
        raise
    except OSError as exc:
        raise AdaptiveRecoveryError(f"cannot read {label} {path}: {exc}") from exc
    before_identity = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
    )
    after_identity = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
    )
    if before_identity != after_identity or len(payload) != after_identity[2]:
        raise AdaptiveRecoveryError(f"{label} changed while it was read: {path}")
    return Snapshot(
        path=resolved,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        identity=after_identity,
    )


def _assert_snapshot_unchanged(snapshot: Snapshot, label: str) -> None:
    current = _snapshot(snapshot.path, label)
    if current.identity != snapshot.identity or current.sha256 != snapshot.sha256:
        raise AdaptiveRecoveryError(f"{label} changed after validation: {snapshot.path}")


def _strict_json(snapshot: Snapshot, label: str) -> dict[str, Any]:
    try:
        return foundation._strict_json_bytes(snapshot.payload, label)
    except ValueError as exc:
        raise AdaptiveRecoveryError(str(exc)) from exc


def _csv_from_payload(
    payload: bytes,
    label: str,
) -> tuple[list[str], list[dict[str, str]]]:
    try:
        stream = io.StringIO(payload.decode("utf-8-sig"), newline="")
        reader = csv.DictReader(stream)
        fieldnames = list(reader.fieldnames or ())
        if not fieldnames or any(not str(name or "").strip() for name in fieldnames):
            raise AdaptiveRecoveryError(f"{label} has an invalid CSV header")
        if len(fieldnames) != len(set(fieldnames)):
            raise AdaptiveRecoveryError(f"{label} has duplicate CSV header fields")
        rows: list[dict[str, str]] = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw or any(value is None for value in raw.values()):
                raise AdaptiveRecoveryError(f"{label} row {row_number} does not match its header")
            rows.append({str(key): str(value) for key, value in raw.items()})
    except (UnicodeError, csv.Error) as exc:
        raise AdaptiveRecoveryError(f"cannot decode {label}: {exc}") from exc
    if not rows:
        raise AdaptiveRecoveryError(f"{label} has no data rows")
    return fieldnames, rows


def _artifact_from_record(value: object, label: str) -> Snapshot:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise AdaptiveRecoveryError(f"{label} must be an exact path/SHA-256 artifact")
    snapshot = _snapshot(Path(str(value.get("path") or "")), label)
    if dict(value) != snapshot.artifact():
        raise AdaptiveRecoveryError(f"{label} path or SHA-256 changed")
    return snapshot


def _bound_artifact_snapshots(value: Any, label: str) -> tuple[Snapshot, ...]:
    """Snapshot every nested path/SHA-256 record, including records with metadata."""

    records: dict[str, Snapshot] = {}

    def visit(item: Any, location: str) -> None:
        if isinstance(item, Mapping):
            path_value = item.get("path")
            sha_value = str(item.get("sha256") or "")
            if path_value and DESIGN_HASH_PATTERN.fullmatch(sha_value):
                path = Path(str(path_value)).resolve(strict=False)
                snapshot = _snapshot(path, f"{label} artifact {location}")
                if snapshot.sha256 != sha_value:
                    raise AdaptiveRecoveryError(
                        f"{label} artifact SHA-256 changed at {location}"
                    )
                key = _normalized_path(path)
                previous = records.get(key)
                if previous is not None and previous.sha256 != snapshot.sha256:
                    raise AdaptiveRecoveryError(
                        f"{label} repeats one artifact path with conflicting SHA-256"
                    )
                records[key] = snapshot
            for key, child in item.items():
                visit(child, f"{location}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{location}[{index}]")

    visit(value, "root")
    return tuple(records[key] for key in sorted(records))


def _validate_hash(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not DESIGN_HASH_PATTERN.fullmatch(result):
        raise AdaptiveRecoveryError(f"{label} must be a lowercase SHA-256 digest")
    return result


def _plan_groups(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        group = str(row.get("geometry_group_id") or "").strip()
        if not group:
            raise AdaptiveRecoveryError("case plan contains a blank geometry_group_id")
        groups.setdefault(group, []).append(row)
    return groups


def _validate_original_summary(rows: Sequence[Mapping[str, str]]) -> None:
    groups = _plan_groups(rows)
    split_groups: dict[str, set[str]] = {}
    split_rows: dict[str, int] = {}
    for row in rows:
        split = str(row.get("doe_split") or "").strip().lower()
        group = str(row["geometry_group_id"])
        split_rows[split] = split_rows.get(split, 0) + 1
        split_groups.setdefault(split, set()).add(group)
    if (
        len(rows) != EXPECTED_ROWS
        or len(groups) != EXPECTED_GROUPS
        or set(len(group_rows) for group_rows in groups.values()) != {ROWS_PER_GROUP}
        or split_rows != {"train": 240, "calibration": 60}
        or {key: len(value) for key, value in split_groups.items()}
        != {"train": EXPECTED_TRAIN_GROUPS, "calibration": EXPECTED_CALIBRATION_GROUPS}
        or any(str(row.get("repeat_of_case_id") or "").strip() for row in rows)
    ):
        raise AdaptiveRecoveryError("original adaptive plan is not the exact 300-row/50-group contract")


def validate_original_authority(
    spec_path: Path,
    original_plan: Path,
    original_manifest: Path,
) -> OriginalAuthority:
    """Reconstruct and byte-verify the original deterministic adaptive batch."""

    spec_snapshot = _snapshot(spec_path, "optimization spec")
    plan_snapshot = _snapshot(original_plan, "original adaptive plan")
    manifest_snapshot = _snapshot(original_manifest, "original adaptive manifest")
    try:
        spec = optimization_spec_from_mapping(
            foundation._strict_json_bytes(spec_snapshot.payload, "optimization spec")
        )
    except ValueError as exc:
        raise AdaptiveRecoveryError(str(exc)) from exc
    fieldnames, rows = _csv_from_payload(plan_snapshot.payload, "original adaptive plan")
    try:
        geometry_replacement._validate_source_plan(spec, fieldnames, rows)
    except ValueError as exc:
        raise AdaptiveRecoveryError(f"original adaptive plan validation failed: {exc}") from exc
    if fieldnames != foundation.fieldnames_for_rows(spec):
        raise AdaptiveRecoveryError("original adaptive plan header changed")
    _validate_original_summary(rows)

    manifest = _strict_json(manifest_snapshot, "original adaptive manifest")
    if set(manifest) != ORIGINAL_MANIFEST_FIELDS:
        raise AdaptiveRecoveryError("original adaptive manifest fields changed")
    if (
        manifest.get("schema_version") != adaptive.SCHEMA_VERSION
        or manifest.get("mode") != "write"
    ):
        raise AdaptiveRecoveryError("original adaptive manifest schema/mode changed")
    expected_plan_record = plan_snapshot.artifact()
    recorded_plan = {
        "path": str(Path(str(manifest.get("case_plan") or "")).resolve(strict=False)),
        "sha256": str(manifest.get("case_plan_sha256") or ""),
    }
    if recorded_plan != expected_plan_record:
        raise AdaptiveRecoveryError("original adaptive manifest does not bind its plan")
    if manifest.get("spec") != spec_snapshot.artifact():
        raise AdaptiveRecoveryError("original adaptive manifest does not bind the supplied spec")

    raw_sources = manifest.get("source_case_plans")
    if not isinstance(raw_sources, list) or len(raw_sources) != 2:
        raise AdaptiveRecoveryError("original adaptive manifest must bind exactly two source plans")
    try:
        source_paths = [Path(str(item["path"])) for item in raw_sources]
        source_excluded, source_records = foundation.stage3_exclusion_contract(source_paths)
        confirmed_raw = manifest.get("confirmed_exclusions")
        if not isinstance(confirmed_raw, list):
            raise ValueError("confirmed_exclusions must be a list")
        confirmed_paths = [Path(str(item["path"])) for item in confirmed_raw]
        confirmed_excluded, confirmed_records = adaptive._confirmed_exclusion_contract(
            confirmed_paths
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise AdaptiveRecoveryError(f"cannot reconstruct adaptive exclusions: {exc}") from exc
    if raw_sources != source_records or confirmed_raw != confirmed_records:
        raise AdaptiveRecoveryError("original adaptive exclusion artifacts changed")
    excluded = source_excluded | confirmed_excluded
    if (
        manifest.get("excluded_design_hashes") != len(excluded)
        or manifest.get("excluded_design_hashes_sha256")
        != foundation._canonical_sha256(sorted(excluded))
    ):
        raise AdaptiveRecoveryError("original adaptive exclusion hash contract changed")

    selection = manifest.get("selection")
    execution = manifest.get("execution_contract")
    if not isinstance(selection, Mapping) or not isinstance(execution, Mapping):
        raise AdaptiveRecoveryError("original adaptive selection/execution contract is missing")
    if set(execution) != {
        "batch_index",
        "case_plan",
        "failed_decision",
        "fixed_audit_case_plan",
        "plateau_policy",
        "r2_history",
        "seed_policy",
    }:
        raise AdaptiveRecoveryError("original adaptive execution fields changed")
    batch_index = execution.get("batch_index")
    if type(batch_index) is not int or batch_index < 1:
        raise AdaptiveRecoveryError("original adaptive batch_index is invalid")
    failed_decision = _artifact_from_record(
        execution.get("failed_decision"), "adaptive failed decision"
    )
    history_snapshot = _artifact_from_record(execution.get("r2_history"), "adaptive R2 history")
    try:
        if batch_index == 1:
            r2_history = adaptive.load_adaptive_r2_history(
                history_snapshot.path,
                failed_decision=failed_decision.path,
                batch_index=batch_index,
            )
            adaptive_evidence = foundation.load_stage3_adaptive_evidence(
                failed_decision.path,
                source_records,
            )
        else:
            r2_history, adaptive_evidence = adaptive.audit_existing_adaptive_r2_advancement(
                history_snapshot.path,
                failed_decision=failed_decision.path,
                batch_index=batch_index,
                source_case_plans=source_records,
            )
        fixed_record = execution.get("fixed_audit_case_plan")
        if not isinstance(fixed_record, Mapping):
            raise ValueError("fixed audit record is missing")
        fixed_audit = adaptive._fixed_audit_contract(
            Path(str(fixed_record.get("path") or "")), adaptive_evidence
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise AdaptiveRecoveryError(f"cannot reconstruct adaptive evidence: {exc}") from exc
    if (
        manifest.get("failed_gate_evidence") != adaptive_evidence.get("proof")
        or manifest.get("fixed_audit_case_plan") != fixed_audit
        or dict(fixed_record) != fixed_audit
        or manifest.get("r2_history") != r2_history
        or execution.get("r2_history") != r2_history["artifact"]
        or execution.get("plateau_policy") != r2_history["plateau"]
        or execution.get("case_plan") != expected_plan_record
        or manifest.get("execution_contract_sha256") != _canonical_sha256(execution)
    ):
        raise AdaptiveRecoveryError("original adaptive evidence/execution lineage changed")

    seed_policy = selection.get("seed_policy")
    if not isinstance(seed_policy, Mapping) or execution.get("seed_policy") != seed_policy:
        raise AdaptiveRecoveryError("original adaptive seed policy changed")
    full_prefix = str(selection.get("case_prefix") or "")
    suffix = f"_batch_{batch_index:04d}"
    if not full_prefix.endswith(suffix):
        raise AdaptiveRecoveryError("original adaptive case prefix changed")
    case_prefix = full_prefix[: -len(suffix)]
    try:
        regenerated_rows, regenerated_selection = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=excluded,
            adaptive_evidence=adaptive_evidence,
            batch_index=batch_index,
            case_prefix=case_prefix,
            candidate_pool_geometries=int(selection["candidate_pool_geometries"]),
            adaptation_seed_base=int(seed_policy["adaptation_seed_base"]),
            calibration_seed_base=int(seed_policy["calibration_seed_base"]),
        )
        regenerated_summary = adaptive.validate_adaptive_batch_rows(
            regenerated_rows, excluded_design_hashes=excluded
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise AdaptiveRecoveryError(f"cannot regenerate original adaptive plan: {exc}") from exc
    regenerated_payload = foundation._stage3_csv_bytes(regenerated_rows, spec)
    if regenerated_payload != plan_snapshot.payload or regenerated_selection != selection:
        raise AdaptiveRecoveryError("original adaptive plan/selection is not deterministic generator output")
    if manifest.get("summary") != regenerated_summary:
        raise AdaptiveRecoveryError("original adaptive summary changed")

    evidence_snapshots = _bound_artifact_snapshots(
        manifest, "original adaptive manifest"
    )

    for snapshot, label in (
        (spec_snapshot, "optimization spec"),
        (plan_snapshot, "original adaptive plan"),
        (manifest_snapshot, "original adaptive manifest"),
        (failed_decision, "adaptive failed decision"),
        (history_snapshot, "adaptive R2 history"),
    ):
        _assert_snapshot_unchanged(snapshot, label)
    return OriginalAuthority(
        spec=spec,
        spec_snapshot=spec_snapshot,
        plan_snapshot=plan_snapshot,
        manifest_snapshot=manifest_snapshot,
        fieldnames=tuple(fieldnames),
        rows=tuple(rows),
        manifest=manifest,
        excluded_design_hashes=frozenset(excluded),
        adaptive_evidence=adaptive_evidence,
        evidence_snapshots=evidence_snapshots,
    )


def _runner_value(argv: Sequence[str], flag: str) -> str:
    indexes = [index for index, value in enumerate(argv) if value == flag]
    if len(indexes) != 1 or indexes[0] + 1 >= len(argv):
        raise AdaptiveRecoveryError(f"continuation runner argv must contain one {flag}")
    value = str(argv[indexes[0] + 1]).strip()
    if not value or value.startswith("--"):
        raise AdaptiveRecoveryError(f"continuation runner argv has no value for {flag}")
    return value


def _safe_remote_root(value: str, label: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").rstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise AdaptiveRecoveryError(f"{label} must be a safe relative remote path")
    return normalized


def _scheduler_identity_from_argv(argv: Sequence[str]) -> dict[str, Any]:
    if not argv or argv[-1] != "--submit" or argv.count("--submit") != 1:
        raise AdaptiveRecoveryError("continuation runner argv must end with one --submit")
    try:
        project_active_cap = int(_runner_value(argv, "--project-active-cap"))
    except ValueError as exc:
        raise AdaptiveRecoveryError("continuation project active cap is invalid") from exc
    if project_active_cap != 300:
        raise AdaptiveRecoveryError("adaptive recovery requires project_active_cap=300")
    identity = {
        "scheduler_url": _runner_value(argv, "--scheduler-url").rstrip("/"),
        "project": _runner_value(argv, "--project"),
        "project_active_cap": project_active_cap,
        "task_prefix": _runner_value(argv, "--task-prefix"),
        "remote_cases_dir": _safe_remote_root(
            _runner_value(argv, "--remote-cases-dir"), "remote_cases_dir"
        ),
        "result_dir": _safe_remote_root(
            _runner_value(argv, "--result-dir"), "result_dir"
        ),
        "simulation_dir": _safe_remote_root(
            _runner_value(argv, "--simulation-dir"), "simulation_dir"
        ),
        "log_dir": _safe_remote_root(_runner_value(argv, "--log-dir"), "log_dir"),
    }
    if set(identity) != SCHEDULER_IDENTITY_FIELDS:
        raise AdaptiveRecoveryError("internal scheduler identity fields changed")
    remote_roots = [
        PurePosixPath(identity[name])
        for name in ("remote_cases_dir", "result_dir", "simulation_dir", "log_dir")
    ]
    for index, left in enumerate(remote_roots):
        for right in remote_roots[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise AdaptiveRecoveryError("scheduler remote roots must be distinct and non-nested")
    return identity


def validate_continuation_authority(
    decision_path: Path,
    original: OriginalAuthority,
) -> ContinuationAuthority:
    """Bind scheduler identity from the immutable stage2_started decision."""

    snapshot = _snapshot(decision_path, "adaptive continuation decision")
    decision = _strict_json(snapshot, "adaptive continuation decision")
    for key, expected in (
        ("schema_version", continuation.SCHEMA_VERSION),
        ("decision", "run_stage2"),
        ("mode", "execute"),
        ("status", "stage2_started"),
    ):
        if decision.get(key) != expected:
            raise AdaptiveRecoveryError(
                f"adaptive continuation decision {key} changed: {decision.get(key)!r}"
            )
    execution = decision.get("execution_contract")
    if not isinstance(execution, Mapping):
        raise AdaptiveRecoveryError("adaptive continuation execution contract is missing")
    if decision.get("contract_sha256") != continuation._contract_sha256(execution):
        raise AdaptiveRecoveryError("adaptive continuation contract SHA-256 changed")
    if Path(str(decision.get("decision_output") or "")).resolve(strict=False) != snapshot.path:
        raise AdaptiveRecoveryError("adaptive continuation decision_output path changed")
    stage2 = execution.get("stage2")
    public_stage2 = decision.get("stage2")
    if not isinstance(stage2, Mapping) or not isinstance(public_stage2, Mapping):
        raise AdaptiveRecoveryError("adaptive continuation Stage2 contract is missing")
    expected_plan = original.plan_snapshot.artifact()
    expected_manifest = original.manifest_snapshot.artifact()
    if stage2.get("case_plan") != expected_plan or stage2.get("case_manifest") != expected_manifest:
        raise AdaptiveRecoveryError("continuation execution does not bind the original adaptive pair")
    if (
        Path(str(public_stage2.get("case_plan") or "")).resolve(strict=False)
        != original.plan_snapshot.path
        or public_stage2.get("case_plan_sha256") != original.plan_snapshot.sha256
        or Path(str(public_stage2.get("case_manifest") or "")).resolve(strict=False)
        != original.manifest_snapshot.path
        or public_stage2.get("case_manifest_sha256") != original.manifest_snapshot.sha256
    ):
        raise AdaptiveRecoveryError("continuation public Stage2 binding changed")
    argv = stage2.get("runner_argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise AdaptiveRecoveryError("continuation runner argv is invalid")
    if public_stage2.get("runner_argv") != argv:
        raise AdaptiveRecoveryError("continuation runner argv projections differ")
    scheduler_identity = _scheduler_identity_from_argv(argv)
    if Path(_runner_value(argv, "--cases")).resolve(strict=False) != original.plan_snapshot.path:
        raise AdaptiveRecoveryError("continuation runner --cases changed")
    output_dir = Path(_runner_value(argv, "--output-dir")).resolve(strict=False)
    if Path(str(stage2.get("output_dir") or "")).resolve(strict=False) != output_dir:
        raise AdaptiveRecoveryError("continuation Stage2 output_dir changed")
    if Path(str(public_stage2.get("output_dir") or "")).resolve(strict=False) != output_dir:
        raise AdaptiveRecoveryError("continuation public Stage2 output_dir changed")
    # A live claim or original owner would make permanent-failure recovery race the runner.
    claim_path = snapshot.path.with_name(snapshot.path.name + ".claim")
    if _path_exists(claim_path):
        raise AdaptiveRecoveryError(f"adaptive continuation claim is still present: {claim_path}")
    owner = decision.get("resume_owner") or decision.get("owner")
    if isinstance(owner, Mapping):
        owner_host = str(owner.get("hostname") or "").strip().lower()
        owner_pid = owner.get("pid")
        if owner_host == socket.gethostname().strip().lower() and type(owner_pid) is int:
            if continuation.pid_is_running(owner_pid):
                raise AdaptiveRecoveryError("adaptive continuation owner process is still active")
    _assert_snapshot_unchanged(snapshot, "adaptive continuation decision")
    return ContinuationAuthority(
        snapshot=snapshot,
        scheduler_identity=scheduler_identity,
        output_dir=output_dir,
    )


def _require_within(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AdaptiveRecoveryError(f"{label} escaped terminal campaign output_dir") from exc


def _validate_failure_evidence(
    failure: Mapping[str, Any],
    *,
    case_id: str,
    output_dir: Path,
) -> tuple[list[dict[str, Any]], list[Snapshot]]:
    attempts = failure.get("attempts")
    evidence = failure.get("failure_evidence")
    if attempts != 2 or not isinstance(evidence, list) or len(evidence) != 2:
        raise AdaptiveRecoveryError(
            f"permanent failure {case_id!r} must bind exactly two terminal attempts"
        )
    public: list[dict[str, Any]] = []
    snapshots: list[Snapshot] = []
    seen_retries: set[int] = set()
    for item in evidence:
        if not isinstance(item, Mapping):
            raise AdaptiveRecoveryError(f"permanent failure evidence for {case_id!r} is invalid")
        retry_index = item.get("retry_index")
        task_id = item.get("task_id")
        if (
            item.get("kind") != "result_level_terminal"
            or item.get("scheduler_status") != "completed"
            or item.get("result_status") != "failed"
            or type(retry_index) is not int
            or retry_index not in {0, 1}
            or type(task_id) is not int
            or task_id < 1
        ):
            raise AdaptiveRecoveryError(
                f"permanent failure evidence for {case_id!r} is not a result-level terminal attempt"
            )
        if retry_index in seen_retries:
            raise AdaptiveRecoveryError(f"permanent failure {case_id!r} repeats retry_index")
        seen_retries.add(retry_index)
        dedupe_key = str(item.get("dedupe_key") or "").strip()
        remote_result = str(item.get("remote_result") or "").strip()
        local_path = Path(str(item.get("local_result") or "")).resolve(strict=False)
        local_sha = str(item.get("local_result_sha256") or "").strip()
        if not dedupe_key or not remote_result:
            raise AdaptiveRecoveryError(f"permanent failure {case_id!r} lacks scheduler identity")
        _require_within(local_path, output_dir / collector.FAILED_RESULTS_DIR_NAME, "failed result")
        local_snapshot = _snapshot(local_path, f"failed result {case_id} attempt {retry_index}")
        if local_snapshot.sha256 != local_sha:
            raise AdaptiveRecoveryError(f"failed result SHA-256 changed for {case_id!r}")
        snapshots.append(local_snapshot)
        public.append(
            {
                "case_id": case_id,
                "dedupe_key": dedupe_key,
                "kind": "result_level_terminal",
                "local_result": local_snapshot.artifact(),
                "remote_result": remote_result,
                "result_status": "failed",
                "retry_index": retry_index,
                "scheduler_status": "completed",
                "task_id": task_id,
            }
        )
    public.sort(key=lambda item: (int(item["retry_index"]), int(item["task_id"])))
    return public, snapshots


def validate_terminal_authority(
    summary_path: Path,
    decision_path: Path,
    original: OriginalAuthority,
    continuation_authority: ContinuationAuthority,
    *,
    failed_design_hash: str,
) -> TerminalAuthority:
    """Verify the collector's terminal 294-success/6-permanent-failure closure."""

    expected_failed_hash = _validate_hash(failed_design_hash, "failed design hash")
    summary_snapshot = _snapshot(summary_path, "terminal campaign summary")
    decision_snapshot = _snapshot(decision_path, "terminal campaign decision")
    summary = _strict_json(summary_snapshot, "terminal campaign summary")
    decision = _strict_json(decision_snapshot, "terminal campaign decision")
    expected_summary_fields = {
        "cases",
        "history_campaign_tasks",
        "history_rows",
        "merged_output",
        "output_dir",
        "permanent_failures",
        "permanently_failed_cases",
        "project",
        "schema_version",
        "selected_cases",
        "selected_plan",
        "status",
        "successful_cases",
        "successful_plan",
    }
    if set(summary) != expected_summary_fields:
        raise AdaptiveRecoveryError("terminal campaign summary fields changed")
    if (
        summary.get("schema_version") != collector.CAMPAIGN_SUMMARY_SCHEMA_VERSION
        or summary.get("status") != "completed_with_permanent_failures"
        or summary.get("selected_cases") != EXPECTED_ROWS
        or summary.get("successful_cases") != EXPECTED_SUCCESSFUL_ROWS
        or summary.get("permanently_failed_cases") != ROWS_PER_GROUP
        or summary.get("project") != continuation_authority.scheduler_identity["project"]
    ):
        raise AdaptiveRecoveryError("terminal campaign summary counts/identity changed")
    output_dir = Path(str(summary.get("output_dir") or "")).resolve(strict=False)
    if output_dir != continuation_authority.output_dir or summary_snapshot.path.parent != output_dir:
        raise AdaptiveRecoveryError("terminal campaign output_dir differs from continuation authority")
    if set(decision) != {
        "permanent_failures",
        "permanently_failed_cases",
        "schema_version",
        "selected_cases",
        "status",
        "successful_cases",
        "summary",
    }:
        raise AdaptiveRecoveryError("terminal campaign decision fields changed")
    if (
        decision.get("schema_version") != collector.CAMPAIGN_DECISION_SCHEMA_VERSION
        or decision.get("status") != summary.get("status")
        or decision.get("selected_cases") != EXPECTED_ROWS
        or decision.get("successful_cases") != EXPECTED_SUCCESSFUL_ROWS
        or decision.get("permanently_failed_cases") != ROWS_PER_GROUP
        or decision.get("permanent_failures") != summary.get("permanent_failures")
        or decision.get("summary") != summary_snapshot.artifact()
    ):
        raise AdaptiveRecoveryError("terminal campaign decision does not bind its summary")

    selected_plan_snapshot = _snapshot(
        Path(str(summary.get("selected_plan") or "")), "terminal selected plan"
    )
    successful_plan_snapshot = _snapshot(
        Path(str(summary.get("successful_plan") or "")), "terminal successful plan"
    )
    merged_snapshot = _snapshot(
        Path(str(summary.get("merged_output") or "")), "terminal merged result"
    )
    for snapshot, label in (
        (selected_plan_snapshot, "selected plan"),
        (successful_plan_snapshot, "successful plan"),
        (merged_snapshot, "merged result"),
    ):
        _require_within(snapshot.path, output_dir, label)
    selected_fields, selected_rows = _csv_from_payload(
        selected_plan_snapshot.payload, "terminal selected plan"
    )
    if selected_fields != list(original.fieldnames) or selected_rows != list(original.rows):
        raise AdaptiveRecoveryError("terminal selected plan differs from the original adaptive plan")

    raw_failures = summary.get("permanent_failures")
    if not isinstance(raw_failures, list) or len(raw_failures) != ROWS_PER_GROUP:
        raise AdaptiveRecoveryError("terminal campaign does not contain six permanent failures")
    plan_by_case = {str(row["case_id"]): row for row in original.rows}
    failure_by_case: dict[str, Mapping[str, Any]] = {}
    for raw in raw_failures:
        if not isinstance(raw, Mapping):
            raise AdaptiveRecoveryError("terminal permanent failure entry is invalid")
        case_id = str(raw.get("case_id") or "").strip()
        if not case_id or case_id in failure_by_case or case_id not in plan_by_case:
            raise AdaptiveRecoveryError("terminal permanent failure case identity changed")
        failure_by_case[case_id] = raw
    failed_case_ids = tuple(
        str(row["case_id"])
        for row in original.rows
        if str(row["case_id"]) in failure_by_case
    )
    failed_rows = [plan_by_case[case_id] for case_id in failed_case_ids]
    failed_hashes = {str(row["design_hash"]) for row in failed_rows}
    failed_groups = {str(row["geometry_group_id"]) for row in failed_rows}
    if (
        len(failed_case_ids) != ROWS_PER_GROUP
        or failed_hashes != {expected_failed_hash}
        or len(failed_groups) != 1
        or sum(str(row["design_hash"]) == expected_failed_hash for row in original.rows)
        != ROWS_PER_GROUP
    ):
        raise AdaptiveRecoveryError("permanent failures do not close one exact six-row geometry")
    failed_group = next(iter(failed_groups))

    successful_fields, successful_rows = _csv_from_payload(
        successful_plan_snapshot.payload, "terminal successful plan"
    )
    expected_successful_rows = [
        dict(row) for row in original.rows if str(row["case_id"]) not in failure_by_case
    ]
    if successful_fields != list(original.fieldnames) or successful_rows != expected_successful_rows:
        raise AdaptiveRecoveryError("terminal successful plan is not the exact retained 294 rows")
    merged_fields, merged_rows = _csv_from_payload(merged_snapshot.payload, "terminal merged result")
    if "case_id" not in merged_fields or [row["case_id"] for row in merged_rows] != [
        row["case_id"] for row in expected_successful_rows
    ]:
        raise AdaptiveRecoveryError("terminal merged result does not cover the exact retained 294 rows")

    cases = summary.get("cases")
    if not isinstance(cases, list) or len(cases) != EXPECTED_ROWS:
        raise AdaptiveRecoveryError("terminal campaign case ledger changed")
    if [str(item.get("case_id") or "") for item in cases if isinstance(item, Mapping)] != [
        str(row["case_id"]) for row in original.rows
    ]:
        raise AdaptiveRecoveryError("terminal campaign case ledger order changed")
    for item in cases:
        if not isinstance(item, Mapping):
            raise AdaptiveRecoveryError("terminal campaign case ledger entry is invalid")
        case_id = str(item.get("case_id") or "")
        expected_outcome = "permanent_failure" if case_id in failure_by_case else "success"
        if item.get("outcome") != expected_outcome:
            raise AdaptiveRecoveryError("terminal campaign case outcome changed")

    failure_results: list[dict[str, Any]] = []
    evidence_snapshots: list[Snapshot] = []
    for case_id in failed_case_ids:
        records, snapshots = _validate_failure_evidence(
            failure_by_case[case_id], case_id=case_id, output_dir=output_dir
        )
        failure_results.extend(records)
        evidence_snapshots.extend(snapshots)
    if len(failure_results) != EXPECTED_FAILURE_RESULTS or len(
        {item["local_result"]["path"] for item in failure_results}
    ) != EXPECTED_FAILURE_RESULTS:
        raise AdaptiveRecoveryError("terminal campaign must bind 12 distinct failed-result artifacts")

    snapshots = (
        summary_snapshot,
        decision_snapshot,
        selected_plan_snapshot,
        successful_plan_snapshot,
        merged_snapshot,
        *evidence_snapshots,
    )
    for item in snapshots:
        _assert_snapshot_unchanged(item, "terminal campaign evidence")
    return TerminalAuthority(
        summary_snapshot=summary_snapshot,
        decision_snapshot=decision_snapshot,
        selected_plan_snapshot=selected_plan_snapshot,
        successful_plan_snapshot=successful_plan_snapshot,
        merged_snapshot=merged_snapshot,
        failed_design_hash=expected_failed_hash,
        failed_geometry_group_id=failed_group,
        failed_case_ids=failed_case_ids,
        failure_results=tuple(failure_results),
        evidence_snapshots=tuple(evidence_snapshots),
    )


def _reconstruct_pool_candidates(
    original: OriginalAuthority,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, int],
]:
    """Rebuild all scored Sobol-pool geometries from the sealed adaptive evidence."""

    selection = original.manifest["selection"]
    adaptation = selection["adaptation"]
    seed_policy = selection["seed_policy"]
    pool_rows = foundation.generate_foundation_rows(
        original.spec,
        geometry_count=int(selection["candidate_pool_geometries"]),
        samples_per_operating_point=foundation.STAGE3_SAMPLES_PER_OPERATING_POINT,
        repeat_count=0,
        seed=int(seed_policy["adaptation_seed"]),
        quality_profile="reference_ultra",
        case_prefix=f"{selection['case_prefix']}_adaptive_pool",
        excluded_design_hashes=original.excluded_design_hashes,
    )
    groups = foundation._rows_by_design_hash(pool_rows)
    ordinal_by_hash = {
        design_hash: ordinal for ordinal, design_hash in enumerate(groups, start=1)
    }
    evidence = original.adaptive_evidence
    input_columns = tuple(evidence["input_columns"])
    bounds = list(evidence["bounds"])
    matrix = [foundation._resolve_ordered_feature_values(row, input_columns) for row in pool_rows]
    normalized = [foundation._normalized_features(row, bounds) for row in matrix]
    direct_predictions = foundation._prediction_members_by_target(evidence["models"], matrix)
    output_name_map = evidence["output_name_map"]
    signal_targets = tuple(evidence["signal_targets"])
    target_scales = evidence["target_scales"]
    audit_features = evidence["audit_features"]
    audit_residuals = evidence["audit_residuals"]
    indexes_by_hash: dict[str, list[int]] = {}
    for index, row in enumerate(pool_rows):
        indexes_by_hash.setdefault(str(row["design_hash"]), []).append(index)

    raw_signals: dict[str, dict[str, float]] = {}
    for design_hash, indexes in indexes_by_hash.items():
        residual_values: list[float] = []
        uncertainty_values: list[float] = []
        invalid_values: list[float] = []
        domain_values: list[float] = []
        for row_index in indexes:
            residual_values.append(
                foundation._project_audit_residual(
                    normalized[row_index], audit_features, audit_residuals
                )
            )
            target_uncertainty: list[float] = []
            target_invalid: list[float] = []
            for target in signal_targets:
                members, invalid_fraction = foundation._target_member_signal(
                    target,
                    row_index,
                    direct_predictions,
                    output_name_map,
                    input_columns,
                    matrix[row_index],
                )
                target_uncertainty.append(
                    (foundation._population_std(members) / target_scales[target])
                    if members
                    else 0.0
                )
                target_invalid.append(invalid_fraction)
            uncertainty_values.append(max(target_uncertainty))
            invalid_values.append(max(target_invalid))
            domain_values.append(foundation._domain_distance(normalized[row_index]))
        raw_signals[design_hash] = {
            "domain_distance_signal": max(domain_values),
            "invalid_derived_prediction_signal": max(invalid_values),
            "residual_signal": max(residual_values),
            "uncertainty_signal": max(uncertainty_values),
        }

    residual_ranks = foundation._rank_signals(
        {key: value["residual_signal"] for key, value in raw_signals.items()}
    )
    uncertainty_ranks = foundation._rank_signals(
        {key: value["uncertainty_signal"] for key, value in raw_signals.items()}
    )
    invalid_ranks = foundation._rank_signals(
        {
            key: value["invalid_derived_prediction_signal"]
            for key, value in raw_signals.items()
        }
    )
    domain_ranks = foundation._rank_signals(
        {key: value["domain_distance_signal"] for key, value in raw_signals.items()}
    )
    candidates: dict[str, dict[str, Any]] = {}
    for design_hash, signals in raw_signals.items():
        uncertainty_component = max(
            uncertainty_ranks[design_hash], invalid_ranks[design_hash]
        )
        acquisition_score = (
            foundation.STAGE3_RESIDUAL_WEIGHT * residual_ranks[design_hash]
            + foundation.STAGE3_UNCERTAINTY_WEIGHT * uncertainty_component
            + foundation.STAGE3_DOMAIN_DISTANCE_WEIGHT * domain_ranks[design_hash]
        )
        candidates[design_hash] = {
            **signals,
            "acquisition_score": acquisition_score,
            "geometry_vector": foundation._geometry_vector(
                original.spec, groups[design_hash][0]
            ),
            "uncertainty_component_rank": uncertainty_component,
        }

    pool_contract = [
        {
            "design_hash": design_hash,
            "operating_controls": [
                {
                    "base_rpm": row["base_rpm"],
                    "beta_dq_deg": row["beta_dq_deg"],
                    "i_peak_a": row["i_peak_a"],
                }
                for row in group_rows
            ],
        }
        for design_hash, group_rows in groups.items()
    ]
    signal_contract = [
        {"design_hash": design_hash, **raw_signals[design_hash]}
        for design_hash in sorted(raw_signals)
    ]
    sealed_pool = adaptation["candidate_pool"]
    if (
        foundation._canonical_sha256(pool_contract) != sealed_pool.get("pool_sha256")
        or foundation._canonical_sha256(signal_contract)
        != sealed_pool.get("signals_sha256")
        or len(groups) != sealed_pool.get("geometry_count")
    ):
        raise AdaptiveRecoveryError("reconstructed adaptive candidate pool evidence changed")
    return groups, candidates, ordinal_by_hash


def _choose_next_candidate(
    candidates: Mapping[str, Mapping[str, Any]],
    *,
    retained_design_hashes: Sequence[str],
    banned_design_hashes: Iterable[str],
    required_invalid_derived: int,
) -> tuple[str, float, float, str]:
    """Choose one deterministic greedy continuation against the retained designs."""

    retained = list(retained_design_hashes)
    if not retained or len(retained) != len(set(retained)):
        raise AdaptiveRecoveryError("retained adaptive designs must be unique and nonempty")
    banned = set(banned_design_hashes) | set(retained)
    retained_invalid = sum(
        float(candidates[item]["invalid_derived_prediction_signal"]) > 0.0
        for item in retained
    )
    require_invalid = retained_invalid < required_invalid_derived
    scored: list[tuple[float, float, str, float]] = []
    for design_hash, candidate in candidates.items():
        if design_hash in banned:
            continue
        if require_invalid and float(candidate["invalid_derived_prediction_signal"]) <= 0.0:
            continue
        diversity = min(
            foundation._feature_distance(
                candidate["geometry_vector"], candidates[item]["geometry_vector"]
            )
            for item in retained
        )
        acquisition = float(candidate["acquisition_score"])
        final_score = (
            (1.0 - foundation.STAGE3_DIVERSITY_WEIGHT) * acquisition
            + foundation.STAGE3_DIVERSITY_WEIGHT * diversity
        )
        scored.append((final_score, acquisition, design_hash, diversity))
    if not scored:
        raise AdaptiveRecoveryError("no unselected adaptive candidate satisfies recovery coverage")
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    final_score, _acquisition, design_hash, diversity = scored[0]
    return (
        design_hash,
        diversity,
        final_score,
        "invalid_derived_minimum_coverage" if require_invalid else "adaptive_score",
    )


def select_replacement_candidate(
    original: OriginalAuthority,
    terminal: TerminalAuthority,
    *,
    expected_design_hash: str | None = None,
) -> CandidateSelection:
    groups, candidates, ordinal_by_hash = _reconstruct_pool_candidates(original)
    adaptation = original.manifest["selection"]["adaptation"]
    selected_records = adaptation.get("selected")
    if not isinstance(selected_records, list) or len(selected_records) != EXPECTED_TRAIN_GROUPS:
        raise AdaptiveRecoveryError("original adaptive selected-candidate ledger changed")
    selected = [str(item.get("design_hash") or "") for item in selected_records]
    if selected.count(terminal.failed_design_hash) != 1:
        raise AdaptiveRecoveryError("failed design is not one exact selected train geometry")
    failed_record = selected_records[selected.index(terminal.failed_design_hash)]
    if float(failed_record.get("invalid_derived_prediction_signal", math.nan)) > 0.0:
        # Supported when coverage remains satisfiable, but keep the explicit branch visible.
        pass
    retained = [item for item in selected if item != terminal.failed_design_hash]
    calibration_hashes = {
        str(value) for value in original.manifest["selection"]["calibration"]["design_hashes"]
    }
    original_hashes = {str(row["design_hash"]) for row in original.rows}
    required_invalid = int(
        adaptation["candidate_pool"][
            "required_invalid_derived_prediction_geometry_count"
        ]
    )
    candidate_hash, diversity, final_score, constraint = _choose_next_candidate(
        candidates,
        retained_design_hashes=retained,
        banned_design_hashes=(
            set(original.excluded_design_hashes)
            | original_hashes
            | calibration_hashes
            | {terminal.failed_design_hash}
        ),
        required_invalid_derived=required_invalid,
    )
    if expected_design_hash is not None and candidate_hash != _validate_hash(
        expected_design_hash, "expected replacement design hash"
    ):
        raise AdaptiveRecoveryError(
            "deterministic replacement candidate differs from --expected-replacement-design-hash"
        )
    if (
        candidate_hash in original.excluded_design_hashes
        or candidate_hash in original_hashes
        or candidate_hash in calibration_hashes
    ):
        raise AdaptiveRecoveryError("replacement candidate overlaps prior/current adaptive designs")
    candidate_rows = groups.get(candidate_hash)
    if candidate_rows is None or len(candidate_rows) != ROWS_PER_GROUP:
        raise AdaptiveRecoveryError("replacement candidate does not contain six pool rows")
    candidate = candidates[candidate_hash]
    return CandidateSelection(
        design_hash=candidate_hash,
        pool_ordinal=ordinal_by_hash[candidate_hash],
        acquisition_score=float(candidate["acquisition_score"]),
        diversity_score=diversity,
        final_selection_score=final_score,
        selection_constraint=constraint,
        signals={
            name: float(candidate[name])
            for name in (
                "domain_distance_signal",
                "invalid_derived_prediction_signal",
                "residual_signal",
                "uncertainty_component_rank",
                "uncertainty_signal",
            )
        },
        rows=tuple(dict(row) for row in candidate_rows),
    )


def _candidate_rows_by_point(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        point = str(row.get("operating_point_id") or "").strip()
        if not point:
            raise AdaptiveRecoveryError("replacement candidate has a blank operating point")
        result.setdefault(point, []).append(row)
    return result


def build_replacement_rows(
    original: OriginalAuthority,
    terminal: TerminalAuthority,
    candidate: CandidateSelection,
) -> ReplacementRows:
    """Replace only the failed six rows with the candidate's own six controls."""

    failed_set = set(terminal.failed_case_ids)
    existing_ids = {str(row["case_id"]) for row in original.rows}
    case_id_map = {
        case_id: f"{case_id}{RETRY_SUFFIX}" for case_id in terminal.failed_case_ids
    }
    if len(set(case_id_map.values())) != ROWS_PER_GROUP or set(case_id_map.values()) & existing_ids:
        raise AdaptiveRecoveryError("clean-retry case IDs collide with the original plan")
    old_group = terminal.failed_geometry_group_id
    if not old_group.endswith(terminal.failed_design_hash[:12]):
        raise AdaptiveRecoveryError("failed geometry_group_id does not bind its design hash")
    new_group = old_group[: -12] + candidate.design_hash[:12]
    if new_group in _plan_groups(original.rows):
        raise AdaptiveRecoveryError("replacement geometry_group_id collides with the original plan")

    candidate_by_point = _candidate_rows_by_point(candidate.rows)
    candidate_indexes = {point: 0 for point in candidate_by_point}
    output_rows: list[dict[str, str]] = []
    changed = 0
    for source in original.rows:
        source_case_id = str(source["case_id"])
        if source_case_id not in failed_set:
            output_rows.append(dict(source))
            continue
        point = str(source["operating_point_id"])
        point_rows = candidate_by_point.get(point, [])
        point_index = candidate_indexes.get(point, 0)
        if point_index >= len(point_rows):
            raise AdaptiveRecoveryError(
                f"replacement candidate lacks controls for operating point {point!r}"
            )
        candidate_row = point_rows[point_index]
        candidate_indexes[point] = point_index + 1
        output = {field: str(candidate_row[field]) for field in original.fieldnames}
        output["case_id"] = case_id_map[source_case_id]
        output["geometry_group_id"] = new_group
        output["doe_split"] = str(source["doe_split"])
        repeat_of = str(source.get("repeat_of_case_id") or "").strip()
        output["repeat_of_case_id"] = case_id_map.get(repeat_of, repeat_of)
        output_rows.append(output)
        changed += 1
    if changed != ROWS_PER_GROUP or any(
        candidate_indexes[point] != len(rows) for point, rows in candidate_by_point.items()
    ):
        raise AdaptiveRecoveryError("replacement did not consume one exact six-row candidate group")

    for before, after in zip(original.rows, output_rows, strict=True):
        if str(before["case_id"]) not in failed_set and after != before:
            raise AdaptiveRecoveryError("replacement changed a retained row field")
    try:
        geometry_replacement._validate_source_plan(
            original.spec, original.fieldnames, output_rows
        )
        summary = adaptive.validate_adaptive_batch_rows(
            output_rows, excluded_design_hashes=original.excluded_design_hashes
        )
    except ValueError as exc:
        raise AdaptiveRecoveryError(f"effective adaptive plan validation failed: {exc}") from exc
    if summary != original.manifest["summary"]:
        raise AdaptiveRecoveryError("effective adaptive plan changed the 300-row/50-group summary")
    original_hashes = {str(row["design_hash"]) for row in original.rows}
    output_hashes = {str(row["design_hash"]) for row in output_rows}
    if (
        terminal.failed_design_hash in output_hashes
        or candidate.design_hash not in output_hashes
        or len(output_hashes) != len(original_hashes)
        or output_hashes & set(original.excluded_design_hashes)
        or candidate.design_hash in original_hashes
    ):
        raise AdaptiveRecoveryError("effective adaptive design-hash coverage changed unexpectedly")
    payload = foundation._stage3_csv_bytes(output_rows, original.spec)
    parsed_fields, parsed_rows = _csv_from_payload(payload, "effective adaptive plan")
    if parsed_fields != list(original.fieldnames) or parsed_rows != output_rows:
        raise AdaptiveRecoveryError("effective adaptive plan bytes do not preserve row fields")
    return ReplacementRows(
        rows=tuple(output_rows),
        case_id_map=tuple((case_id, case_id_map[case_id]) for case_id in terminal.failed_case_ids),
        replacement_geometry_group_id=new_group,
        payload=payload,
    )


def _replacement_operating_controls(
    replacement_rows: ReplacementRows,
    case_ids: Sequence[str],
) -> list[dict[str, Any]]:
    by_case = {str(row["case_id"]): row for row in replacement_rows.rows}
    controls: list[dict[str, Any]] = []
    for case_id in case_ids:
        row = by_case[case_id]
        controls.append(
            {
                "base_rpm": float(row["base_rpm"]),
                "beta_dq_deg": float(row["beta_dq_deg"]),
                "case_id": case_id,
                "i_peak_a": float(row["i_peak_a"]),
                "operating_point_id": str(row["operating_point_id"]),
            }
        )
    return controls


def _replacement_geometry(candidate: CandidateSelection) -> dict[str, float]:
    first = candidate.rows[0]
    return {
        **{name: float(first[name]) for name in GEOMETRY_VARIABLE_NAMES},
        "phase_resistance_ohm": float(first["phase_resistance_ohm"]),
        "stack_length_mm": float(first["stack_length_mm"]),
    }


def build_recovery_manifest(
    original: OriginalAuthority,
    continuation_authority: ContinuationAuthority,
    terminal: TerminalAuthority,
    candidate: CandidateSelection,
    replacement_rows: ReplacementRows,
    *,
    output: Path,
    manifest_output: Path,
    mode: str,
) -> dict[str, Any]:
    if mode not in {"dry-run", "execute"}:
        raise AdaptiveRecoveryError(f"unsupported recovery mode: {mode}")
    output_record = {
        "path": str(output.resolve(strict=False)),
        "sha256": hashlib.sha256(replacement_rows.payload).hexdigest(),
        "rows": EXPECTED_ROWS,
        "geometry_groups": EXPECTED_GROUPS,
    }
    selected_hashes = [
        str(item["design_hash"])
        for item in original.manifest["selection"]["adaptation"]["selected"]
    ]
    retained_hashes = [
        design_hash
        for design_hash in selected_hashes
        if design_hash != terminal.failed_design_hash
    ]
    replacement_case_ids = [target for _source, target in replacement_rows.case_id_map]
    candidate_pool = dict(
        original.manifest["selection"]["adaptation"]["candidate_pool"]
    )
    scoring = dict(original.manifest["selection"]["adaptation"]["scoring"])
    selection_proof = {
        "candidate_pool": candidate_pool,
        "failed_selected_rank": selected_hashes.index(terminal.failed_design_hash) + 1,
        "original_selected_design_hashes_sha256": foundation._canonical_sha256(
            selected_hashes
        ),
        "replacement_greedy_rank_after_retained": 1,
        "retained_design_hashes_sha256": foundation._canonical_sha256(retained_hashes),
        "retained_geometry_count": len(retained_hashes),
        "scoring": scoring,
        "seed_policy": dict(original.manifest["selection"]["seed_policy"]),
        "overlap": {
            "calibration": 0,
            "current_adaptive": 0,
            "prior_or_confirmed": 0,
        },
    }
    terminal_campaign = {
        "decision": terminal.decision_snapshot.artifact(),
        "failure_results": [dict(item) for item in terminal.failure_results],
        "merged_output": terminal.merged_snapshot.artifact(),
        "output_dir": str(continuation_authority.output_dir),
        "permanently_failed_cases": ROWS_PER_GROUP,
        "selected_cases": EXPECTED_ROWS,
        "selected_plan": terminal.selected_plan_snapshot.artifact(),
        "successful_cases": EXPECTED_SUCCESSFUL_ROWS,
        "successful_plan": terminal.successful_plan_snapshot.artifact(),
        "summary": terminal.summary_snapshot.artifact(),
    }
    contract = {
        "original": {
            "manifest": {
                **original.manifest_snapshot.artifact(),
                "execution_contract_sha256": original.manifest[
                    "execution_contract_sha256"
                ],
                "schema_version": original.manifest["schema_version"],
            },
            "plan": {
                **original.plan_snapshot.artifact(),
                "geometry_groups": EXPECTED_GROUPS,
                "rows": EXPECTED_ROWS,
            },
        },
        "continuation_decision": continuation_authority.snapshot.artifact(),
        "terminal_campaign": terminal_campaign,
        "scheduler_identity": dict(continuation_authority.scheduler_identity),
        "failure": {
            "attempts_per_case": 2,
            "case_ids": list(terminal.failed_case_ids),
            "design_hash": terminal.failed_design_hash,
            "geometry_group_id": terminal.failed_geometry_group_id,
            "rows": ROWS_PER_GROUP,
        },
        "replacement": {
            "acquisition_score": candidate.acquisition_score,
            "candidate_pool_ordinal": candidate.pool_ordinal,
            "case_id_map": [
                {"replacement": replacement, "source": source}
                for source, replacement in replacement_rows.case_id_map
            ],
            "case_ids": replacement_case_ids,
            "design_hash": candidate.design_hash,
            "diversity_score": candidate.diversity_score,
            "final_selection_score": candidate.final_selection_score,
            "geometry": _replacement_geometry(candidate),
            "geometry_group_id": replacement_rows.replacement_geometry_group_id,
            "operating_controls": _replacement_operating_controls(
                replacement_rows, replacement_case_ids
            ),
            "rows": ROWS_PER_GROUP,
            "selection_constraint": candidate.selection_constraint,
            "signals": dict(candidate.signals),
        },
        "selection_proof": selection_proof,
        "output": {
            "manifest_path": str(manifest_output.resolve(strict=False)),
            "plan": output_record,
        },
    }
    if set(contract) != RECOVERY_CONTRACT_FIELDS:
        raise AdaptiveRecoveryError("internal recovery contract fields changed")
    checks = {
        "candidate_own_controls_preserved": True,
        "candidate_pool_proof_reconstructed": True,
        "case_id_mapping_is_fresh": True,
        "failure_evidence_sha256_bound": True,
        "original_adaptive_generator_reproduced": True,
        "original_artifacts_immutable": True,
        "prior_current_calibration_overlap_zero": True,
        "replaced_rows": ROWS_PER_GROUP,
        "retained_rows_byte_field_identical": EXPECTED_SUCCESSFUL_ROWS,
        "scheduler_identity_bound": True,
        "terminal_campaign_authority_verified": True,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "status": "validated" if mode == "dry-run" else "created",
        "contract": contract,
        "contract_sha256": _canonical_sha256(contract),
        "checks": checks,
    }


def build_recovery(
    *,
    spec_path: Path,
    original_plan: Path,
    original_manifest: Path,
    continuation_decision: Path,
    campaign_summary: Path,
    campaign_decision: Path,
    failed_design_hash: str,
    expected_replacement_design_hash: str | None,
    output: Path,
    manifest_output: Path,
    mode: str,
) -> RecoveryBuild:
    original = validate_original_authority(spec_path, original_plan, original_manifest)
    continuation_authority = validate_continuation_authority(
        continuation_decision, original
    )
    terminal = validate_terminal_authority(
        campaign_summary,
        campaign_decision,
        original,
        continuation_authority,
        failed_design_hash=failed_design_hash,
    )
    candidate = select_replacement_candidate(
        original,
        terminal,
        expected_design_hash=expected_replacement_design_hash,
    )
    replacement_rows = build_replacement_rows(original, terminal, candidate)
    manifest = build_recovery_manifest(
        original,
        continuation_authority,
        terminal,
        candidate,
        replacement_rows,
        output=output,
        manifest_output=manifest_output,
        mode=mode,
    )
    snapshots = (
        original.spec_snapshot,
        original.plan_snapshot,
        original.manifest_snapshot,
        *original.evidence_snapshots,
        continuation_authority.snapshot,
        terminal.summary_snapshot,
        terminal.decision_snapshot,
        terminal.selected_plan_snapshot,
        terminal.successful_plan_snapshot,
        terminal.merged_snapshot,
        *terminal.evidence_snapshots,
    )
    unique: dict[str, Snapshot] = {}
    for snapshot in snapshots:
        key = _normalized_path(snapshot.path)
        previous = unique.get(key)
        if previous is not None and previous.sha256 != snapshot.sha256:
            raise AdaptiveRecoveryError("one recovery input path has conflicting snapshots")
        unique[key] = snapshot
    return RecoveryBuild(
        output_payload=replacement_rows.payload,
        manifest=manifest,
        snapshots=tuple(unique[key] for key in sorted(unique)),
    )


def _validate_input_output_paths(args: argparse.Namespace) -> None:
    inputs = (
        args.spec,
        args.original_plan,
        args.original_manifest,
        args.continuation_decision,
        args.campaign_summary,
        args.campaign_decision,
    )
    transaction_paths = (
        args.output,
        args.manifest_output,
        clean_retry.publish_proof_path(args.output),
        clean_retry.publish_proof_path(args.manifest_output),
    )
    normalized_transactions = {_normalized_path(path) for path in transaction_paths}
    if len(normalized_transactions) != len(transaction_paths):
        raise AdaptiveRecoveryError("recovery output, manifest, and proof paths must be distinct")
    for path in inputs:
        if _normalized_path(path) in normalized_transactions:
            raise AdaptiveRecoveryError(f"recovery output aliases an immutable input: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--original-plan", type=Path, required=True)
    parser.add_argument("--original-manifest", type=Path, required=True)
    parser.add_argument("--continuation-decision", type=Path, required=True)
    parser.add_argument("--campaign-summary", type=Path, required=True)
    parser.add_argument("--campaign-decision", type=Path, required=True)
    parser.add_argument("--failed-design-hash", required=True)
    parser.add_argument("--expected-replacement-design-hash")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Atomically publish the fresh recovery plan/manifest pair.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _validate_input_output_paths(args)
    try:
        clean_retry.prepare_fresh_pair(
            args.output, args.manifest_output, execute=args.execute
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise AdaptiveRecoveryError(str(exc)) from exc
    build = build_recovery(
        spec_path=args.spec,
        original_plan=args.original_plan,
        original_manifest=args.original_manifest,
        continuation_decision=args.continuation_decision,
        campaign_summary=args.campaign_summary,
        campaign_decision=args.campaign_decision,
        failed_design_hash=args.failed_design_hash,
        expected_replacement_design_hash=args.expected_replacement_design_hash,
        output=args.output,
        manifest_output=args.manifest_output,
        mode="execute" if args.execute else "dry-run",
    )
    for snapshot in build.snapshots:
        _assert_snapshot_unchanged(snapshot, "recovery input")
    if args.execute:
        try:
            clean_retry.publish_pair(
                args.output,
                build.output_payload,
                args.manifest_output,
                build.manifest,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise AdaptiveRecoveryError(str(exc)) from exc
    print(
        json.dumps(
            build.manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
