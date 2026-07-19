"""Advance the post-1300 IPMSM v2 adaptive campaign one batch at a time.

The coordinator owns no scientific authority artifacts.  It invokes the
immutable merge, adaptive-plan, and continuation CLIs as subprocesses, audits
their outputs, and emits replaceable campaign telemetry for automation.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import socket
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

import continue_ipmsm_v2_stage2 as stage2_continuation


SCHEMA_VERSION = "ipmsm-v2-adaptive-campaign-coordinator-v1"
MERGE_SCHEMA_VERSION = "ipmsm-v2-case-plan-merge-v1"
ADAPTIVE_MANIFEST_SCHEMA_VERSION = "ipmsm_v2_adaptive_enrichment_batch_v1"
ADAPTIVE_RECOVERY_MANIFEST_SCHEMA_VERSION = (
    "ipmsm_v2_adaptive_failed_geometry_recovery_v1"
)
EXPECTED_ADAPTIVE_RECOVERY_DESIGN_HASH = (
    "3263dc3e81653d767f9bea561f958587f3f31e4740d48afecdeeb003332be55f"
)
R2_HISTORY_SCHEMA_VERSION = "ipmsm_v2_adaptive_r2_history_v1"
DECISION_SCHEMA_VERSION = "ipmsm_v2_stage2_continuation_v1"
ROWS_PER_BATCH = 300
GROUPS_PER_BATCH = 50
BASELINE_ROWS = 1300
BASELINE_GROUPS = 210
EXPECTED_REPEATS = 40
PROJECT_ACTIVE_CAP = 300
ADAPTIVE_SELECTION_VERSION = "stage3_audit_residual_adaptive_v2"
PRIMARY_TARGETS = (
    "output_torque_last_avg_nm",
    "output_torque_last_max_nm",
    "output_solidloss_last_avg_w",
    "output_coreloss_last_avg_w",
    "output_ld_last_avg_h",
    "output_lq_last_avg_h",
    "output_total_loss_last_avg_w",
    "output_efficiency_last_pct",
)


class CoordinatorError(RuntimeError):
    """Raised when campaign authority or subprocess behavior is unsafe."""


@dataclass(frozen=True)
class Artifact:
    path: Path
    sha256: str

    def record(self) -> dict[str, str]:
        return {"path": str(self.path), "sha256": self.sha256}


@dataclass(frozen=True)
class PlanInfo:
    artifact: Artifact
    headers: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    case_ids: frozenset[str]
    design_hashes: frozenset[str]
    geometry_groups: frozenset[str]


@dataclass(frozen=True)
class DecisionInfo:
    artifact: Artifact
    payload: dict[str, Any]
    status: str
    stage1_plan: Artifact
    stage2_plan: Artifact
    fixed_audit: Artifact
    case_manifest: Artifact | None
    combined_artifacts: dict[str, Artifact]


@dataclass(frozen=True)
class BatchPaths:
    index: int
    root: Path
    stage1_plan: Path
    stage1_manifest: Path
    history: Path
    adaptive_plan: Path
    adaptive_manifest: Path
    decision: Path
    stage2_output: Path
    combined_output: Path
    runner_pid: Path
    watcher_pid: Path


@dataclass(frozen=True)
class ProgressContext:
    current_batch: int
    action: str
    latest: DecisionInfo | None
    commands: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class FailedGeometryRecoveryEvidence:
    failed_design_hash: str
    failed_geometry_group_id: str
    failed_case_ids: tuple[str, ...]
    summary: Artifact
    decision: Artifact


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: Any) -> str:
    normalized = dict(value) if isinstance(value, Mapping) else value
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(payload)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CoordinatorError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CoordinatorError(f"JSON contains non-finite constant {value!r}")


def _strict_json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CoordinatorError(f"cannot decode {label}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise CoordinatorError(f"{label} must be a JSON object")
    return decoded


def _read_json(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CoordinatorError(f"cannot read {label} {path}: {exc}") from exc
    return payload, _strict_json_bytes(payload, label)


def _artifact(path: Path, label: str) -> Artifact:
    resolved = path.resolve(strict=False)
    if not resolved.is_file():
        raise CoordinatorError(f"{label} is missing: {resolved}")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise CoordinatorError(f"cannot read {label} {resolved}: {exc}") from exc
    return Artifact(resolved, _sha256(payload))


def _bound_artifact(value: object, label: str) -> Artifact:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise CoordinatorError(f"{label} must contain only path and sha256")
    raw_path = str(value.get("path") or "")
    recorded_hash = str(value.get("sha256") or "")
    path = Path(raw_path)
    if (
        not path.is_absolute()
        or path.resolve(strict=False) != path
        or len(recorded_hash) != 64
        or any(character not in "0123456789abcdef" for character in recorded_hash)
    ):
        raise CoordinatorError(f"{label} has a noncanonical path or SHA-256")
    actual = _artifact(path, label)
    if actual.sha256 != recorded_hash:
        raise CoordinatorError(f"{label} bytes changed")
    return actual


def _bound_stage3_failed_decision_proof(value: object, label: str) -> Artifact:
    """Verify the artifact projection of the exact enriched Stage3 proof."""
    expected_fields = {
        "combined_artifacts",
        "contract_sha256",
        "fixed_audit_case_plan",
        "path",
        "sha256",
        "stage2_result",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise CoordinatorError(f"{label} fields changed")
    return _bound_artifact(
        {"path": value.get("path"), "sha256": value.get("sha256")}, label
    )


def _same_artifact(actual: Artifact, expected: Artifact, label: str) -> None:
    if actual != expected:
        raise CoordinatorError(f"{label} differs from the deterministic campaign lineage")


def _read_plan(path: Path, label: str) -> PlanInfo:
    artifact = _artifact(path, label)
    try:
        text = artifact.path.read_bytes().decode("utf-8-sig")
    except UnicodeError as exc:
        raise CoordinatorError(f"{label} is not UTF-8: {artifact.path}") from exc
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = tuple(reader.fieldnames or ())
        rows = tuple(dict(row) for row in reader)
    except csv.Error as exc:
        raise CoordinatorError(f"cannot parse {label}: {exc}") from exc
    if (
        not headers
        or any(not str(header or "").strip() for header in headers)
        or len(headers) != len(set(headers))
    ):
        raise CoordinatorError(f"{label} has a missing or duplicate CSV header")
    if not {"case_id", "design_hash"}.issubset(headers):
        raise CoordinatorError(f"{label} lacks case_id or design_hash")
    case_ids: set[str] = set()
    design_hashes: set[str] = set()
    groups: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        if None in row or any(value is None for value in row.values()):
            raise CoordinatorError(f"{label} row {row_number} does not match its header")
        case_id = str(row.get("case_id") or "").strip()
        design_hash = str(row.get("design_hash") or "").strip()
        if not case_id or not design_hash or case_id in case_ids:
            raise CoordinatorError(f"{label} has blank/duplicate identity at row {row_number}")
        case_ids.add(case_id)
        design_hashes.add(design_hash)
        group = str(row.get("geometry_group_id") or "").strip()
        if group:
            groups.add(group)
    if not rows:
        raise CoordinatorError(f"{label} is empty")
    return PlanInfo(
        artifact=artifact,
        headers=headers,
        rows=rows,
        case_ids=frozenset(case_ids),
        design_hashes=frozenset(design_hashes),
        geometry_groups=frozenset(groups),
    )


def _terminal_failed_geometry_recovery_evidence(
    paths: BatchPaths,
    continuation_decision: DecisionInfo,
) -> FailedGeometryRecoveryEvidence | None:
    """Recognize only the exact bounded 294-ok/6-failed terminal campaign."""

    summary_path = paths.stage2_output / "campaign_summary.json"
    decision_path = paths.stage2_output / "campaign_decision.json"
    present = (os.path.lexists(summary_path), os.path.lexists(decision_path))
    if present == (False, False):
        return None
    if present != (True, True):
        raise CoordinatorError("terminal adaptive campaign evidence pair is partial")
    if (
        continuation_decision.status != "stage2_started"
        or continuation_decision.payload.get("resume_required") is not True
        or not str(continuation_decision.payload.get("last_error") or "").strip()
    ):
        raise CoordinatorError(
            "failed-geometry recovery requires an exact resumable stage2_started decision"
        )

    summary_artifact = _artifact(summary_path, "terminal adaptive campaign summary")
    decision_artifact = _artifact(decision_path, "terminal adaptive campaign decision")
    _, summary = _read_json(summary_artifact.path, "terminal adaptive campaign summary")
    _, campaign_decision = _read_json(
        decision_artifact.path, "terminal adaptive campaign decision"
    )
    expected_decision_fields = {
        "schema_version",
        "status",
        "selected_cases",
        "successful_cases",
        "permanently_failed_cases",
        "summary",
        "permanent_failures",
    }
    if (
        set(campaign_decision) != expected_decision_fields
        or campaign_decision.get("schema_version") != "ipmsm_v2_campaign_decision_v1"
        or campaign_decision.get("status") != "completed_with_permanent_failures"
        or campaign_decision.get("selected_cases") != ROWS_PER_BATCH
        or campaign_decision.get("successful_cases") != ROWS_PER_BATCH - 6
        or campaign_decision.get("permanently_failed_cases") != 6
        or _bound_artifact(
            campaign_decision.get("summary"), "terminal campaign summary binding"
        )
        != summary_artifact
    ):
        raise CoordinatorError("terminal adaptive campaign decision scope changed")
    if (
        summary.get("schema_version") != "ipmsm_v2_campaign_summary_v1"
        or summary.get("status") != "completed_with_permanent_failures"
        or summary.get("selected_cases") != ROWS_PER_BATCH
        or summary.get("successful_cases") != ROWS_PER_BATCH - 6
        or summary.get("permanently_failed_cases") != 6
        or summary.get("permanent_failures")
        != campaign_decision.get("permanent_failures")
    ):
        raise CoordinatorError("terminal adaptive campaign summary scope changed")

    original = _read_plan(paths.adaptive_plan, "failed adaptive source plan")
    selected = _read_plan(
        Path(str(summary.get("selected_plan") or "")),
        "terminal adaptive selected plan",
    )
    if original.headers != selected.headers or original.rows != selected.rows:
        raise CoordinatorError("terminal adaptive selected plan differs from its source plan")
    failures = campaign_decision.get("permanent_failures")
    if not isinstance(failures, list) or len(failures) != 6:
        raise CoordinatorError("terminal adaptive campaign does not contain six failures")
    rows_by_case = {str(row["case_id"]): row for row in original.rows}
    failed_case_ids: list[str] = []
    for failure in failures:
        if not isinstance(failure, Mapping) or set(failure) != {
            "case_id",
            "attempts",
            "failure_evidence",
        }:
            raise CoordinatorError("terminal adaptive permanent-failure fields changed")
        case_id = str(failure.get("case_id") or "")
        if case_id not in rows_by_case or failure.get("attempts") != 2:
            raise CoordinatorError("terminal adaptive failure identity/attempt count changed")
        evidence = failure.get("failure_evidence")
        if not isinstance(evidence, list) or len(evidence) != 2:
            raise CoordinatorError("terminal adaptive failure evidence count changed")
        retry_indices: set[int] = set()
        for item in evidence:
            expected_fields = {
                "kind",
                "retry_index",
                "task_id",
                "dedupe_key",
                "scheduler_status",
                "result_status",
                "remote_result",
                "result_error",
                "local_result",
                "local_result_sha256",
            }
            if not isinstance(item, Mapping) or set(item) != expected_fields:
                raise CoordinatorError("terminal failed-result evidence fields changed")
            retry_index = item.get("retry_index")
            if (
                item.get("kind") != "result_level_terminal"
                or item.get("scheduler_status") != "completed"
                or item.get("result_status") != "failed"
                or type(retry_index) is not int
            ):
                raise CoordinatorError("terminal failed-result evidence semantics changed")
            retry_indices.add(retry_index)
            local_result = Path(str(item.get("local_result") or "")).resolve(
                strict=False
            )
            failed_root = (paths.stage2_output / "failed_results").resolve(strict=False)
            if local_result.parent != failed_root:
                raise CoordinatorError("failed-result evidence escaped its output directory")
            evidence_artifact = _artifact(local_result, "terminal failed-result evidence")
            if evidence_artifact.sha256 != item.get("local_result_sha256"):
                raise CoordinatorError("terminal failed-result evidence bytes changed")
        if retry_indices != {0, 1}:
            raise CoordinatorError("terminal failed-result retry lineage changed")
        failed_case_ids.append(case_id)
    if len(set(failed_case_ids)) != 6:
        raise CoordinatorError("terminal adaptive failure case IDs are not unique")
    failed_rows = [rows_by_case[case_id] for case_id in failed_case_ids]
    failed_groups = {
        str(row.get("geometry_group_id") or "").strip() for row in failed_rows
    }
    failed_hashes = {str(row.get("design_hash") or "").strip() for row in failed_rows}
    if len(failed_groups) != 1 or len(failed_hashes) != 1 or "" in (
        failed_groups | failed_hashes
    ):
        raise CoordinatorError("terminal adaptive failures are not one geometry group")
    failed_group = next(iter(failed_groups))
    group_case_ids = tuple(
        str(row["case_id"])
        for row in original.rows
        if str(row.get("geometry_group_id") or "").strip() == failed_group
    )
    if len(group_case_ids) != 6 or set(group_case_ids) != set(failed_case_ids):
        raise CoordinatorError("terminal failure does not cover its full six-row geometry")
    return FailedGeometryRecoveryEvidence(
        failed_design_hash=next(iter(failed_hashes)),
        failed_geometry_group_id=failed_group,
        failed_case_ids=group_case_ids,
        summary=summary_artifact,
        decision=decision_artifact,
    )


def _render_merged(first: PlanInfo, second: PlanInfo) -> bytes:
    if first.headers != second.headers:
        raise CoordinatorError("source case-plan headers differ")
    if first.case_ids & second.case_ids:
        raise CoordinatorError("source case plans overlap by case_id")
    if first.design_hashes & second.design_hashes:
        raise CoordinatorError("source case plans overlap by design_hash")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(first.headers), extrasaction="raise")
    writer.writeheader()
    writer.writerows([*first.rows, *second.rows])
    return stream.getvalue().encode("utf-8-sig")


def _audit_merge_pair(
    output: Path,
    manifest_path: Path,
    source_paths: Sequence[Path],
    *,
    expected_rows: int,
    expected_groups: int,
) -> Artifact:
    if len(source_paths) != 2:
        raise CoordinatorError("a campaign merge requires exactly two source plans")
    first = _read_plan(source_paths[0], "first merge source plan")
    second = _read_plan(source_paths[1], "second merge source plan")
    expected_payload = _render_merged(first, second)
    output_artifact = _artifact(output, "merged Stage1 case plan")
    if output_artifact.sha256 != _sha256(expected_payload):
        raise CoordinatorError("existing merged Stage1 plan bytes differ from exact source merge")
    merged = _read_plan(output, "merged Stage1 case plan")
    if len(merged.rows) != expected_rows or len(merged.geometry_groups) != expected_groups:
        raise CoordinatorError("merged Stage1 plan row/group counts changed")
    manifest_bytes, manifest = _read_json(manifest_path, "merged Stage1 manifest")
    del manifest_bytes
    exact_fields = {
        "schema_version",
        "mode",
        "source_case_plans",
        "output",
        "manifest_output",
        "header",
        "counts",
    }
    if set(manifest) != exact_fields:
        raise CoordinatorError("merged Stage1 manifest fields changed")
    expected_sources = [
        {
            "path": str(plan.artifact.path),
            "sha256": plan.artifact.sha256,
            "rows": len(plan.rows),
            "design_hashes": len(plan.design_hashes),
        }
        for plan in (first, second)
    ]
    expected_header = {
        "columns": list(merged.headers),
        "sha256": _sha256(
            json.dumps(
                list(merged.headers), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ),
    }
    expected_counts = {
        "case_plans": 2,
        "rows": expected_rows,
        "case_ids": expected_rows,
        "design_hashes": len(merged.design_hashes),
    }
    if (
        manifest.get("schema_version") != MERGE_SCHEMA_VERSION
        or manifest.get("mode") != "execute"
        or manifest.get("source_case_plans") != expected_sources
        or manifest.get("output")
        != {
            "path": str(output_artifact.path),
            "sha256": output_artifact.sha256,
            "rows": expected_rows,
            "design_hashes": len(merged.design_hashes),
        }
        or manifest.get("manifest_output") != str(manifest_path.resolve(strict=False))
        or manifest.get("header") != expected_header
        or manifest.get("counts") != expected_counts
    ):
        raise CoordinatorError("merged Stage1 manifest does not exactly bind its sources")
    return output_artifact


def _primary_minimum(decision: Mapping[str, Any], label: str) -> float:
    combined = decision.get("combined")
    primary = combined.get("primary_test_r2") if isinstance(combined, Mapping) else None
    if not isinstance(primary, Mapping) or set(primary) != set(PRIMARY_TARGETS):
        raise CoordinatorError(f"{label} primary R2 target set changed")
    values: list[float] = []
    for target in PRIMARY_TARGETS:
        try:
            value = float(primary[target])
        except (TypeError, ValueError) as exc:
            raise CoordinatorError(f"{label} has invalid primary R2 for {target}") from exc
        if not math.isfinite(value):
            raise CoordinatorError(f"{label} has non-finite primary R2 for {target}")
        values.append(value)
    return min(values)


def _load_decision(path: Path, label: str) -> DecisionInfo:
    artifact = _artifact(path, label)
    _, decision = _read_json(artifact.path, label)
    status = str(decision.get("status") or "")
    if (
        decision.get("schema_version") != DECISION_SCHEMA_VERSION
        or decision.get("mode") != "execute"
        or decision.get("decision") != "run_stage2"
        or status not in {"stage2_started", "combined_r2_failed", "complete"}
        or Path(str(decision.get("decision_output") or "")).resolve(strict=False)
        != artifact.path
    ):
        raise CoordinatorError(f"{label} is not an exact executable continuation decision")
    execution = decision.get("execution_contract")
    if not isinstance(execution, Mapping):
        raise CoordinatorError(f"{label} lacks execution_contract")
    if decision.get("contract_sha256") != _canonical_sha256(execution):
        raise CoordinatorError(f"{label} contract SHA-256 changed")
    stage1 = execution.get("stage1")
    stage2 = execution.get("stage2")
    training = execution.get("training")
    if not all(isinstance(value, Mapping) for value in (stage1, stage2, training)):
        raise CoordinatorError(f"{label} lacks Stage1/Stage2/training bindings")
    stage1_plan = _bound_artifact(stage1.get("case_plan"), f"{label} Stage1 plan")
    stage2_plan = _bound_artifact(stage2.get("case_plan"), f"{label} Stage2 plan")
    fixed_audit = _bound_artifact(
        training.get("audit_case_plan"), f"{label} fixed audit plan"
    )
    case_manifest: Artifact | None = None
    if "case_manifest" in stage2:
        case_manifest = _bound_artifact(
            stage2.get("case_manifest"), f"{label} adaptive case manifest"
        )
        top_stage2 = decision.get("stage2")
        top_manifest = (
            {
                "path": str(
                    Path(str(top_stage2.get("case_manifest") or "")).resolve(
                        strict=False
                    )
                ),
                "sha256": str(top_stage2.get("case_manifest_sha256") or ""),
            }
            if isinstance(top_stage2, Mapping)
            else None
        )
        if top_manifest != case_manifest.record():
            raise CoordinatorError(f"{label} case-manifest bindings differ")
    combined_artifacts: dict[str, Artifact] = {}
    if status in {"combined_r2_failed", "complete"}:
        combined = decision.get("combined")
        raw_artifacts = combined.get("artifacts") if isinstance(combined, Mapping) else None
        if not isinstance(raw_artifacts, Mapping) or set(raw_artifacts) != {
            "merged",
            "validation",
            "metadata",
            "r2",
        }:
            raise CoordinatorError(f"{label} combined artifact set changed")
        combined_artifacts = {
            name: _bound_artifact(record, f"{label} combined {name}")
            for name, record in raw_artifacts.items()
        }
        _primary_minimum(decision, label)
    return DecisionInfo(
        artifact=artifact,
        payload=decision,
        status=status,
        stage1_plan=stage1_plan,
        stage2_plan=stage2_plan,
        fixed_audit=fixed_audit,
        case_manifest=case_manifest,
        combined_artifacts=combined_artifacts,
    )


def _revalidate_terminal_gate(
    decision: DecisionInfo,
    *,
    expected_rows: int,
    expected_groups: int,
) -> Any:
    if decision.status == "stage2_started":
        return None
    try:
        import continue_ipmsm_v2_stage2 as continuation

        gate = continuation.evaluate_gate(
            decision.combined_artifacts["validation"].path,
            decision.combined_artifacts["metadata"].path,
            decision.combined_artifacts["r2"].path,
            expected_rows=expected_rows,
            expected_groups=expected_groups,
            expected_repeats=EXPECTED_REPEATS,
            threshold=0.95,
            expected_ensemble_size=5,
            expected_conformal_coverage=0.95,
            expected_audit_case_plan=decision.fixed_audit.path,
        )
        continuation._validate_result_evidence(
            [decision.stage1_plan.path, decision.stage2_plan.path],
            decision.combined_artifacts["merged"].path,
            gate,
            "coordinator terminal combined result",
        )
    except Exception as exc:
        raise CoordinatorError(f"terminal combined gate revalidation failed: {exc}") from exc
    expected_status = "complete" if gate.passed else "combined_r2_failed"
    if decision.status != expected_status:
        raise CoordinatorError(
            "terminal decision status disagrees with the revalidated combined gate"
        )
    combined = decision.payload.get("combined")
    if not isinstance(combined, Mapping):
        raise CoordinatorError("terminal decision lacks combined summary")
    summary = gate.summary()
    mismatches = [key for key, expected in summary.items() if combined.get(key) != expected]
    if mismatches:
        raise CoordinatorError(
            "terminal decision summary disagrees with gate revalidation: "
            + ", ".join(mismatches)
        )
    return gate


def _audit_source_lineage(
    decision: DecisionInfo,
    source_paths: Sequence[Path],
    fixed_audit: Artifact,
    *,
    expected_rows: int,
    expected_groups: int,
) -> tuple[Artifact, Artifact]:
    expected = (
        _artifact(source_paths[0], "expected Stage1 exclusion plan"),
        _artifact(source_paths[1], "expected Stage2 exclusion plan"),
    )
    _same_artifact(decision.stage1_plan, expected[0], "failed decision Stage1 plan")
    _same_artifact(decision.stage2_plan, expected[1], "failed decision Stage2 plan")
    _same_artifact(decision.fixed_audit, fixed_audit, "failed decision fixed audit")
    first = _read_plan(expected[0].path, "failed decision Stage1 plan")
    second = _read_plan(expected[1].path, "failed decision Stage2 plan")
    _render_merged(first, second)
    if len(first.rows) + len(second.rows) != expected_rows:
        raise CoordinatorError("failed decision source plans do not cover expected rows")
    if len(first.geometry_groups | second.geometry_groups) != expected_groups:
        raise CoordinatorError("failed decision source plans do not cover expected groups")
    combined_contract = decision.payload["execution_contract"].get("combined")
    expected_contract = {
        "expected_rows": expected_rows,
        "expected_groups": expected_groups,
        "expected_repeats": EXPECTED_REPEATS,
    }
    if not isinstance(combined_contract, Mapping) or any(
        combined_contract.get(key) != value for key, value in expected_contract.items()
    ):
        raise CoordinatorError("failed decision combined shape differs from campaign policy")
    _revalidate_terminal_gate(
        decision,
        expected_rows=expected_rows,
        expected_groups=expected_groups,
    )
    return expected


def _evaluate_plateau(values: Sequence[float]) -> dict[str, Any]:
    improvements = [current - previous for previous, current in zip(values, values[1:])]
    trailing = 0
    for improvement in reversed(improvements):
        if improvement >= 0.01:
            break
        trailing += 1
    stop = trailing >= 2
    return {
        "action": "model_physics_diagnosis" if stop else "continue_adaptive_fea",
        "completed_batches": len(values) - 1,
        "consecutive_batches_required": 2,
        "improvements": improvements,
        "minimum_improvement": 0.01,
        "stop_fea": stop,
        "trailing_below_threshold": trailing,
    }


def _audit_history(path: Path, latest: DecisionInfo, batch_index: int) -> dict[str, Any]:
    payload, history = _read_json(path, "adaptive R2 history")
    if set(history) != {"records", "schema_version"} or history.get(
        "schema_version"
    ) != R2_HISTORY_SCHEMA_VERSION:
        raise CoordinatorError("adaptive R2 history schema/fields changed")
    records = history.get("records")
    if not isinstance(records, list) or len(records) != batch_index:
        raise CoordinatorError("adaptive R2 history length differs from batch index")
    normalized: list[dict[str, Any]] = []
    values: list[float] = []
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping) or set(raw) != {
            "batch_index",
            "decision",
            "min_primary_r2",
        } or raw.get("batch_index") != index:
            raise CoordinatorError("adaptive R2 history records are not contiguous/exact")
        decision_artifact = _bound_artifact(
            raw.get("decision"), f"adaptive R2 history decision {index}"
        )
        decision = _load_decision(
            decision_artifact.path, f"adaptive R2 history decision {index}"
        )
        if decision.status != "combined_r2_failed":
            raise CoordinatorError("adaptive R2 history contains a nonfailed decision")
        minimum = _primary_minimum(decision.payload, "adaptive R2 history decision")
        try:
            recorded = float(raw.get("min_primary_r2"))
        except (TypeError, ValueError) as exc:
            raise CoordinatorError("adaptive R2 history minimum is invalid") from exc
        if not math.isclose(recorded, minimum, rel_tol=1e-12, abs_tol=1e-15):
            raise CoordinatorError("adaptive R2 history minimum changed")
        normalized.append(
            {
                "batch_index": index,
                "decision": decision_artifact.record(),
                "min_primary_r2": minimum,
            }
        )
        values.append(minimum)
    if normalized[-1]["decision"] != latest.artifact.record():
        raise CoordinatorError("adaptive R2 history does not end at the latest decision")
    expected_payload = (
        json.dumps(
            {"schema_version": R2_HISTORY_SCHEMA_VERSION, "records": normalized},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    if payload != expected_payload:
        raise CoordinatorError("adaptive R2 history bytes are not canonical")
    return {
        "artifact": Artifact(path.resolve(strict=False), _sha256(payload)).record(),
        "plateau": _evaluate_plateau(values),
        "records": normalized,
    }


def _audit_adaptive_rows_and_selection(
    plan: PlanInfo,
    selection: object,
    *,
    spec: Any,
    adaptive_evidence: Mapping[str, Any],
    excluded_design_hashes: set[str],
    batch_index: int,
    candidate_pool_geometries: int,
    adaptation_seed_base: int,
    calibration_seed_base: int,
) -> bytes:
    try:
        import generate_ipmsm_v2_adaptive_batch as adaptive_generator

        adaptive_generator.validate_adaptive_batch_rows(
            [dict(row) for row in plan.rows],
            excluded_design_hashes=excluded_design_hashes,
        )
        expected_rows, expected_selection = adaptive_generator.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=excluded_design_hashes,
            adaptive_evidence=adaptive_evidence,
            batch_index=batch_index,
            case_prefix=f"v2-adaptive-b{batch_index:04d}",
            candidate_pool_geometries=candidate_pool_geometries,
            adaptation_seed_base=adaptation_seed_base,
            calibration_seed_base=calibration_seed_base,
        )
        expected_plan_bytes = adaptive_generator.foundation._stage3_csv_bytes(
            expected_rows, spec
        )
        expected_text = expected_plan_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(expected_text, newline=""))
        expected_headers = tuple(reader.fieldnames or ())
        expected_csv_rows = tuple(dict(row) for row in reader)
    except Exception as exc:
        raise CoordinatorError(
            f"adaptive deterministic regeneration failed: {exc}"
        ) from exc
    if plan.headers != expected_headers or plan.rows != expected_csv_rows:
        raise CoordinatorError(
            "adaptive CSV rows differ from deterministic generator output"
        )
    if selection != expected_selection:
        raise CoordinatorError(
            "adaptive selection differs from deterministic generator output"
        )
    return expected_plan_bytes


def _audit_adaptive_pair(
    args: argparse.Namespace,
    paths: BatchPaths,
    sources: Sequence[Artifact],
    latest: DecisionInfo,
    fixed_audit: Artifact,
) -> Artifact:
    proof_paths = (
        paths.adaptive_plan.with_name(f".{paths.adaptive_plan.name}.publish-proof.json"),
        paths.adaptive_manifest.with_name(
            f".{paths.adaptive_manifest.name}.publish-proof.json"
        ),
        paths.history.with_name(f".{paths.history.name}.publish-proof.json"),
    )
    if any(os.path.lexists(path) for path in proof_paths):
        raise CoordinatorError("adaptive publication has an unresolved proof artifact")
    plan = _read_plan(paths.adaptive_plan, "adaptive batch plan")
    if len(plan.rows) != ROWS_PER_BATCH or len(plan.geometry_groups) != GROUPS_PER_BATCH:
        raise CoordinatorError("adaptive batch plan is not 300 rows / 50 groups")
    split_rows: dict[str, int] = {}
    split_groups: dict[str, set[str]] = {}
    for row in plan.rows:
        split = str(row.get("doe_split") or "").strip().lower()
        group = str(row.get("geometry_group_id") or "").strip()
        split_rows[split] = split_rows.get(split, 0) + 1
        split_groups.setdefault(split, set()).add(group)
    if split_rows != {"train": 240, "calibration": 60} or {
        key: len(value) for key, value in split_groups.items()
    } != {"train": 40, "calibration": 10}:
        raise CoordinatorError("adaptive batch split changed")
    try:
        import generate_ipmsm_v2_adaptive_batch as adaptive_generator
    except Exception as exc:
        raise CoordinatorError(f"cannot import adaptive generator authority: {exc}") from exc
    _, manifest = _read_json(paths.adaptive_manifest, "adaptive batch manifest")
    if manifest.get("schema_version") == ADAPTIVE_RECOVERY_MANIFEST_SCHEMA_VERSION:
        return _audit_recovery_adaptive_pair(
            args, paths, sources, latest, fixed_audit, plan, manifest
        )
    expected_manifest_fields = {
        "case_plan",
        "case_plan_sha256",
        "confirmed_exclusions",
        "excluded_design_hashes",
        "excluded_design_hashes_sha256",
        "failed_gate_evidence",
        "fixed_audit_case_plan",
        "execution_contract",
        "execution_contract_sha256",
        "mode",
        "schema_version",
        "selection",
        "r2_history",
        "source_case_plans",
        "spec",
        "summary",
    }
    if set(manifest) != expected_manifest_fields:
        raise CoordinatorError("adaptive batch manifest fields changed")
    if manifest.get("schema_version") != ADAPTIVE_MANIFEST_SCHEMA_VERSION or manifest.get(
        "mode"
    ) != "write":
        raise CoordinatorError("adaptive batch manifest schema/mode changed")
    plan_record = {
        "path": str(plan.artifact.path),
        "sha256": plan.artifact.sha256,
    }
    if {
        "path": str(Path(str(manifest.get("case_plan") or "")).resolve(strict=False)),
        "sha256": str(manifest.get("case_plan_sha256") or ""),
    } != plan_record:
        raise CoordinatorError("adaptive manifest does not bind its case plan")
    raw_sources = manifest.get("source_case_plans")
    if not isinstance(raw_sources, list) or len(raw_sources) != 2:
        raise CoordinatorError("adaptive manifest must bind exactly two exclusion plans")
    try:
        source_excluded, expected_source_records = (
            adaptive_generator.foundation.stage3_exclusion_contract(
                [source.path for source in sources]
            )
        )
    except Exception as exc:
        raise CoordinatorError(f"adaptive source exclusion contract failed: {exc}") from exc
    projected_sources = [
        _bound_artifact(
            {"path": record.get("path"), "sha256": record.get("sha256")},
            f"adaptive source plan {index}",
        )
        for index, record in enumerate(expected_source_records)
    ]
    if projected_sources != list(sources) or raw_sources != expected_source_records:
        raise CoordinatorError("adaptive source-plan authority changed")

    failed_gate = manifest.get("failed_gate_evidence")
    if not isinstance(failed_gate, Mapping):
        raise CoordinatorError("adaptive manifest lacks failed-gate evidence")
    # The Stage3 evidence proof embeds the full sealed decision object.  Verify
    # its artifact projection here; the complete proof is reconstructed and
    # compared byte-for-byte below before the generated plan is accepted.
    failed_decision = _bound_stage3_failed_decision_proof(
        failed_gate.get("decision"), "adaptive failed-gate decision"
    )
    failed_audit = _bound_artifact(
        failed_gate.get("stage2_audit_case_plan"), "adaptive failed-gate audit"
    )
    if failed_decision != latest.artifact or failed_audit != fixed_audit:
        raise CoordinatorError("adaptive failed-gate evidence changed")

    try:
        confirmed_hashes, confirmed_records = (
            adaptive_generator._confirmed_exclusion_contract(
                args.confirmed_exclusion_csv
            )
        )
    except Exception as exc:
        raise CoordinatorError(f"adaptive confirmed-exclusion contract failed: {exc}") from exc
    excluded = source_excluded | confirmed_hashes
    if (
        manifest.get("confirmed_exclusions") != confirmed_records
        or manifest.get("excluded_design_hashes") != len(excluded)
        or manifest.get("excluded_design_hashes_sha256")
        != _canonical_sha256(sorted(excluded))
    ):
        raise CoordinatorError("adaptive confirmed-exclusion contract changed")

    spec_artifact = _artifact(args.spec, "adaptive optimization spec")
    if manifest.get("spec") != spec_artifact.record():
        raise CoordinatorError("adaptive optimization spec binding changed")
    try:
        spec_bytes = spec_artifact.path.read_bytes()
        if _sha256(spec_bytes) != spec_artifact.sha256:
            raise CoordinatorError("adaptive optimization spec changed during audit")
        optimization_spec = adaptive_generator.optimization_spec_from_mapping(
            adaptive_generator.foundation._strict_json_bytes(
                spec_bytes, "adaptive optimization spec"
            )
        )
        if paths.index == 1:
            adaptive_evidence = (
                adaptive_generator.foundation.load_stage3_adaptive_evidence(
                    latest.artifact.path, expected_source_records
                )
            )
        else:
            predecessor_audit = adaptive_generator._audit_adaptive_r2_predecessor(
                failed_decision=latest.artifact.path,
                batch_index=paths.index,
                source_case_plans=expected_source_records,
                expected_previous_history=_batch_paths(
                    args, paths.index - 1
                ).history,
            )
            adaptive_evidence = predecessor_audit.adaptive_evidence
            if paths.history.read_bytes() != predecessor_audit.payload:
                raise CoordinatorError(
                    "adaptive R2 history is not the exact predecessor-history extension"
                )
    except CoordinatorError:
        raise
    except Exception as exc:
        raise CoordinatorError(
            f"cannot reconstruct adaptive generation evidence: {exc}"
        ) from exc
    if failed_gate != adaptive_evidence.get("proof"):
        raise CoordinatorError("adaptive failed-gate evidence proof changed")
    expected_plan_bytes = _audit_adaptive_rows_and_selection(
        plan,
        manifest.get("selection"),
        spec=optimization_spec,
        adaptive_evidence=adaptive_evidence,
        excluded_design_hashes=excluded,
        batch_index=paths.index,
        candidate_pool_geometries=args.candidate_pool_geometries,
        adaptation_seed_base=args.adaptation_seed_base,
        calibration_seed_base=args.calibration_seed_base,
    )
    try:
        actual_plan_bytes = plan.artifact.path.read_bytes()
    except OSError as exc:
        raise CoordinatorError(f"cannot reread adaptive batch plan: {exc}") from exc
    if actual_plan_bytes != expected_plan_bytes:
        raise CoordinatorError(
            "adaptive CSV bytes differ from deterministic generator output"
        )
    execution = manifest.get("execution_contract")
    if not isinstance(execution, Mapping) or set(execution) != {
        "batch_index",
        "case_plan",
        "failed_decision",
        "fixed_audit_case_plan",
        "plateau_policy",
        "r2_history",
        "seed_policy",
    }:
        raise CoordinatorError("adaptive execution contract fields changed")
    history = _audit_history(paths.history, latest, paths.index)
    expected_seed_policy = {
        "adaptation_seed": args.adaptation_seed_base + 100 * paths.index,
        "adaptation_seed_base": args.adaptation_seed_base,
        "calibration_seed": args.calibration_seed_base + 100 * paths.index,
        "calibration_seed_base": args.calibration_seed_base,
        "formula": "role_seed_base + 100 * batch_index",
        "stride": 100,
    }
    selection = manifest.get("selection")
    if (
        not isinstance(selection, Mapping)
        or selection.get("candidate_pool_geometries")
        != args.candidate_pool_geometries
        or selection.get("seed_policy") != expected_seed_policy
        or execution.get("seed_policy") != expected_seed_policy
    ):
        raise CoordinatorError("adaptive selection/seed policy changed")
    if (
        manifest.get("execution_contract_sha256") != _canonical_sha256(execution)
        or execution.get("batch_index") != paths.index
        or execution.get("case_plan") != plan_record
        or execution.get("failed_decision") != latest.artifact.record()
        or execution.get("fixed_audit_case_plan") != fixed_audit.record()
        or execution.get("r2_history") != history["artifact"]
        or execution.get("plateau_policy") != history["plateau"]
        or manifest.get("fixed_audit_case_plan") != fixed_audit.record()
        or manifest.get("r2_history") != history
    ):
        raise CoordinatorError("adaptive manifest execution lineage changed")
    if manifest.get("summary") != {
        "cross_split_design_overlap": 0,
        "geometry_groups": 50,
        "prior_or_confirmed_design_overlap": 0,
        "repeats": 0,
        "rows": 300,
        "split_groups": {"train": 40, "calibration": 10, "test": 0},
        "split_rows": {"train": 240, "calibration": 60, "test": 0},
    }:
        raise CoordinatorError("adaptive manifest summary changed")
    return plan.artifact


def _audit_recovery_adaptive_pair(
    args: argparse.Namespace,
    paths: BatchPaths,
    sources: Sequence[Artifact],
    latest: DecisionInfo,
    fixed_audit: Artifact,
    plan: PlanInfo,
    manifest: Mapping[str, Any],
) -> Artifact:
    original_paths = _batch_paths(args, paths.index)
    expected_recovery_paths = _recovery_batch_paths(original_paths)
    if paths != expected_recovery_paths:
        raise CoordinatorError("adaptive recovery paths are not the canonical fresh successor")
    original_state = _pair_state(
        original_paths.adaptive_plan,
        original_paths.adaptive_manifest,
        "original adaptive recovery source pair",
    )
    if original_state != "complete" or not original_paths.decision.is_file():
        raise CoordinatorError("adaptive recovery lacks its immutable original authority")
    original_decision = _load_decision(
        original_paths.decision, "failed adaptive continuation decision"
    )
    evidence = _terminal_failed_geometry_recovery_evidence(
        original_paths, original_decision
    )
    if evidence is None:
        raise CoordinatorError("adaptive recovery lacks terminal permanent-failure evidence")
    _audit_adaptive_pair(args, original_paths, sources, latest, fixed_audit)
    recovery_manifest_artifact = _artifact(
        paths.adaptive_manifest, "adaptive recovery manifest replay snapshot"
    )
    try:
        import recover_ipmsm_v2_adaptive_failed_geometry as adaptive_recovery

        replay = adaptive_recovery.build_recovery(
            spec_path=args.spec,
            original_plan=original_paths.adaptive_plan,
            original_manifest=original_paths.adaptive_manifest,
            continuation_decision=original_paths.decision,
            campaign_summary=evidence.summary.path,
            campaign_decision=evidence.decision.path,
            failed_design_hash=evidence.failed_design_hash,
            expected_replacement_design_hash=(
                EXPECTED_ADAPTIVE_RECOVERY_DESIGN_HASH
            ),
            output=paths.adaptive_plan,
            manifest_output=paths.adaptive_manifest,
            mode="execute",
        )
        expected_manifest_payload = (
            json.dumps(
                dict(replay.manifest),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if paths.adaptive_plan.read_bytes() != replay.output_payload:
            raise CoordinatorError(
                "adaptive recovery CSV bytes differ from exact publisher replay"
            )
        if paths.adaptive_manifest.read_bytes() != expected_manifest_payload:
            raise CoordinatorError(
                "adaptive recovery manifest bytes differ from exact publisher replay"
            )
        for snapshot in replay.snapshots:
            adaptive_recovery._assert_snapshot_unchanged(
                snapshot, "adaptive recovery replay input"
            )
    except CoordinatorError:
        raise
    except Exception as exc:
        raise CoordinatorError(
            f"adaptive recovery publisher replay failed: {exc}"
        ) from exc
    _same_artifact(
        _artifact(paths.adaptive_plan, "adaptive recovery plan replay rehash"),
        plan.artifact,
        "adaptive recovery plan replay",
    )
    _same_artifact(
        _artifact(paths.adaptive_manifest, "adaptive recovery manifest replay rehash"),
        recovery_manifest_artifact,
        "adaptive recovery manifest replay",
    )
    if set(manifest) != {
        "schema_version",
        "mode",
        "status",
        "contract",
        "contract_sha256",
        "checks",
    }:
        raise CoordinatorError("adaptive recovery manifest fields changed")
    contract = manifest.get("contract")
    original = contract.get("original") if isinstance(contract, Mapping) else None
    continuation_record = (
        contract.get("continuation_decision") if isinstance(contract, Mapping) else None
    )
    terminal = contract.get("terminal_campaign") if isinstance(contract, Mapping) else None
    output = contract.get("output") if isinstance(contract, Mapping) else None
    if not all(isinstance(value, Mapping) for value in (original, terminal, output)):
        raise CoordinatorError("adaptive recovery contract lineage is incomplete")
    original_plan = original.get("plan")
    original_manifest = original.get("manifest")
    output_plan = output.get("plan")
    if (
        not isinstance(original_plan, Mapping)
        or not isinstance(original_manifest, Mapping)
        or not isinstance(output_plan, Mapping)
        or _bound_artifact(
            {"path": original_plan.get("path"), "sha256": original_plan.get("sha256")},
            "adaptive recovery original plan",
        )
        != _artifact(original_paths.adaptive_plan, "adaptive recovery original plan")
        or _bound_artifact(
            {
                "path": original_manifest.get("path"),
                "sha256": original_manifest.get("sha256"),
            },
            "adaptive recovery original manifest",
        )
        != _artifact(
            original_paths.adaptive_manifest, "adaptive recovery original manifest"
        )
        or _bound_artifact(
            {"path": output_plan.get("path"), "sha256": output_plan.get("sha256")},
            "adaptive recovery output plan",
        )
        != plan.artifact
        or Path(str(output.get("manifest_path") or "")).resolve(strict=False)
        != paths.adaptive_manifest.resolve(strict=False)
        or _bound_artifact(
            continuation_record, "adaptive recovery continuation decision"
        )
        != original_decision.artifact
    ):
        raise CoordinatorError("adaptive recovery artifact lineage changed")
    for name, expected in (
        ("summary", evidence.summary),
        ("decision", evidence.decision),
    ):
        if _bound_artifact(
            terminal.get(name), f"adaptive recovery terminal {name}"
        ) != expected:
            raise CoordinatorError("adaptive recovery terminal evidence changed")
    try:
        continuation = _continuation_argv(
            args, paths, latest, resume=False, recovery=True
        )
        continuation_args = stage2_continuation.build_parser().parse_args(
            continuation[2:-1]
        )
        bound_manifest = stage2_continuation._validate_stage2_case_manifest(
            continuation_args
        )
    except Exception as exc:
        raise CoordinatorError(f"adaptive recovery manifest validation failed: {exc}") from exc
    if bound_manifest != _artifact(paths.adaptive_manifest, "adaptive recovery manifest").record():
        raise CoordinatorError("adaptive recovery manifest binding changed")
    return plan.artifact


def _pair_state(first: Path, second: Path, label: str) -> str:
    present = (os.path.lexists(first), os.path.lexists(second))
    if present == (False, False):
        return "absent"
    if present != (True, True):
        raise CoordinatorError(f"{label} is partial; overwrite/recovery is forbidden")
    if (
        _is_reparse_point(first)
        or _is_reparse_point(second)
        or not first.is_file()
        or not second.is_file()
    ):
        raise CoordinatorError(f"{label} contains a non-file artifact")
    return "complete"


def _batch_paths(args: argparse.Namespace, index: int) -> BatchPaths:
    prior_rows = BASELINE_ROWS + ROWS_PER_BATCH * (index - 1)
    combined_rows = prior_rows + ROWS_PER_BATCH
    root = args.campaign_root / f"adaptive_batch_{index:04d}"
    return BatchPaths(
        index=index,
        root=root,
        stage1_plan=root
        / f"ipmsm_v2_stage1_through_batch_{index - 1:04d}_{prior_rows}_cases.csv",
        stage1_manifest=root
        / f"ipmsm_v2_stage1_through_batch_{index - 1:04d}_{prior_rows}_manifest.json",
        history=root / f"adaptive_r2_history_through_batch_{index - 1:04d}.json",
        adaptive_plan=root / f"ipmsm_v2_adaptive_batch_{index:04d}_300_cases.csv",
        adaptive_manifest=root
        / f"ipmsm_v2_adaptive_batch_{index:04d}_300_manifest.json",
        decision=root / f"foundation_adaptive_batch_{index:04d}_decision.json",
        stage2_output=args.stage2_output_root
        / f"ipmsm_v2_adaptive_batch_{index:04d}_300",
        combined_output=args.combined_output_root
        / f"ipmsm_v2_foundation_through_adaptive_batch_{index:04d}_{combined_rows}",
        runner_pid=root / "completed_stage1_runner.pid",
        watcher_pid=root / "completed_stage1_watcher.pid",
    )


def _recovery_batch_paths(paths: BatchPaths) -> BatchPaths:
    """Return fresh successor paths without changing the failed batch authority."""

    return BatchPaths(
        index=paths.index,
        root=paths.root,
        stage1_plan=paths.stage1_plan,
        stage1_manifest=paths.stage1_manifest,
        history=paths.history,
        adaptive_plan=paths.root
        / f"ipmsm_v2_adaptive_batch_{paths.index:04d}_300_recovery_cases.csv",
        adaptive_manifest=paths.root
        / f"ipmsm_v2_adaptive_batch_{paths.index:04d}_300_recovery_manifest.json",
        decision=paths.root
        / f"foundation_adaptive_batch_{paths.index:04d}_recovery_decision.json",
        stage2_output=paths.stage2_output.with_name(paths.stage2_output.name + "_recovery"),
        combined_output=paths.combined_output.with_name(
            paths.combined_output.name + "_recovery"
        ),
        runner_pid=paths.runner_pid,
        watcher_pid=paths.watcher_pid,
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CoordinatorError(f"cannot inspect path identity {path}: {exc}") from exc
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _assert_safe_descendant(path: Path, root: Path, label: str) -> None:
    lexical_path = Path(os.path.abspath(path))
    lexical_root = Path(os.path.abspath(root))
    if not _is_within(lexical_path, lexical_root):
        raise CoordinatorError(f"{label} escapes its lexical output root")
    resolved_path = lexical_path.resolve(strict=False)
    resolved_root = lexical_root.resolve(strict=False)
    if resolved_root != lexical_root:
        raise CoordinatorError(f"{label} output root changed through a reparse path")
    if not _is_within(resolved_path, resolved_root):
        raise CoordinatorError(f"{label} escapes its resolved output root")
    current = lexical_root
    if os.path.lexists(current) and _is_reparse_point(current):
        raise CoordinatorError(f"{label} output root is a symlink/reparse point")
    for part in lexical_path.relative_to(lexical_root).parts:
        current /= part
        if os.path.lexists(current) and _is_reparse_point(current):
            raise CoordinatorError(f"{label} traverses a symlink/reparse point: {current}")


def _reject_reparse_tree(root: Path, label: str) -> None:
    if not os.path.lexists(root):
        return
    if _is_reparse_point(root) or not root.is_dir():
        raise CoordinatorError(f"{label} is not a regular directory")
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise CoordinatorError(f"cannot inspect {label} {current}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            if _is_reparse_point(path):
                raise CoordinatorError(f"{label} contains a symlink/reparse point: {path}")
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif not entry.is_file(follow_symlinks=False):
                    raise CoordinatorError(f"{label} contains a nonregular path: {path}")
            except OSError as exc:
                raise CoordinatorError(f"cannot inspect {label} entry {path}: {exc}") from exc


def _validate_batch_paths(args: argparse.Namespace, paths: BatchPaths) -> None:
    _assert_safe_descendant(paths.root, args.campaign_root, "adaptive batch root")
    _assert_safe_descendant(
        paths.stage2_output, args.stage2_output_root, "adaptive Stage2 output"
    )
    _assert_safe_descendant(
        paths.combined_output, args.combined_output_root, "adaptive combined output"
    )
    authority_paths = (
        paths.stage1_plan,
        paths.stage1_manifest,
        paths.history,
        paths.adaptive_plan,
        paths.adaptive_manifest,
        paths.decision,
        paths.runner_pid,
        paths.watcher_pid,
        paths.root / "logs" / "merge.log",
        paths.root / "logs" / "generator.log",
        paths.root / "logs" / "continuation.log",
        paths.root / "logs" / "recovery_publisher.log",
        paths.root / "logs" / "recovery_continuation.log",
    )
    for path in authority_paths:
        _assert_safe_descendant(path, paths.root, "adaptive batch authority/log path")
        if os.path.lexists(path) and (
            _is_reparse_point(path) or not path.is_file()
        ):
            raise CoordinatorError(
                f"adaptive batch authority/log path is not a regular file: {path}"
            )
    staging = paths.combined_output.with_name(paths.combined_output.name + ".staging")
    _assert_safe_descendant(staging, args.combined_output_root, "combined staging output")
    for root, label in (
        (paths.root, "adaptive batch tree"),
        (paths.stage2_output, "adaptive Stage2 output tree"),
        (paths.combined_output, "adaptive combined output tree"),
        (staging, "adaptive combined staging tree"),
    ):
        _reject_reparse_tree(root, label)
    expected_result_files = (
        paths.stage2_output / "merged_results.csv",
        paths.combined_output / "merged_results.csv",
        paths.combined_output / "validation.csv",
        paths.combined_output / "models" / "metadata.json",
        paths.combined_output / "r2_gate.csv",
    )
    for path in expected_result_files:
        root = (
            paths.stage2_output
            if _is_within(path, paths.stage2_output)
            else paths.combined_output
        )
        _assert_safe_descendant(path, root, "adaptive result artifact")
        if os.path.lexists(path) and (
            _is_reparse_point(path) or not path.is_file()
        ):
            raise CoordinatorError(f"adaptive result artifact is not a regular file: {path}")


def _completed_stage1_owner_pid(latest: DecisionInfo) -> int:
    if latest.status != "combined_r2_failed":
        raise CoordinatorError("completed Stage1 decision is not a failed terminal gate")
    resume_owner = latest.payload.get("resume_owner")
    owner = resume_owner if resume_owner is not None else latest.payload.get("owner")
    expected_mode = "resume" if resume_owner is not None else "execute"
    expected_fields = {"hostname", "invocation_id", "mode", "pid", "started_at"}
    if not isinstance(owner, Mapping) or set(owner) != expected_fields:
        raise CoordinatorError("completed Stage1 decision owner fields changed")
    if owner.get("hostname") != socket.gethostname() or owner.get("mode") != expected_mode:
        raise CoordinatorError("completed Stage1 decision owner identity changed")
    invocation_id = str(owner.get("invocation_id") or "")
    if len(invocation_id) != 32 or any(
        character not in "0123456789abcdef" for character in invocation_id
    ):
        raise CoordinatorError("completed Stage1 decision owner invocation ID is invalid")
    pid = owner.get("pid")
    if type(pid) is not int or pid <= 0:
        raise CoordinatorError("completed Stage1 decision owner PID is invalid")
    try:
        started_at = datetime.fromisoformat(str(owner.get("started_at") or ""))
    except ValueError as exc:
        raise CoordinatorError(
            "completed Stage1 decision owner timestamp is invalid"
        ) from exc
    if started_at.tzinfo is None:
        raise CoordinatorError("completed Stage1 decision owner timestamp is naive")
    if stage2_continuation.pid_is_running(pid):
        raise CoordinatorError(f"completed Stage1 decision owner is still active: pid={pid}")
    return pid


def _audit_pid_marker(path: Path, payload: bytes, label: str) -> None:
    if _is_reparse_point(path) or not path.is_file():
        raise CoordinatorError(f"{label} is not a regular file: {path}")
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise CoordinatorError(f"cannot read {label} {path}: {exc}") from exc
    if actual != payload:
        raise CoordinatorError(f"{label} differs from the completed decision owner")


def _publish_pid_marker(path: Path, payload: bytes, label: str) -> None:
    if os.path.lexists(path):
        _audit_pid_marker(path, payload, label)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staged, path)
        except FileExistsError:
            _audit_pid_marker(path, payload, label)
    finally:
        staged.unlink(missing_ok=True)
    _audit_pid_marker(path, payload, label)


def _ensure_completed_stage1_pid_markers(
    paths: BatchPaths, latest: DecisionInfo
) -> None:
    payload = f"{_completed_stage1_owner_pid(latest)}\n".encode("ascii")
    _publish_pid_marker(paths.runner_pid, payload, "completed Stage1 runner PID marker")
    _publish_pid_marker(paths.watcher_pid, payload, "completed Stage1 watcher PID marker")


def _script(args: argparse.Namespace, name: str) -> str:
    return str((args.source_root / name).resolve(strict=False))


def _merge_argv(args: argparse.Namespace, paths: BatchPaths, sources: Sequence[Path]) -> list[str]:
    return [
        str(args.python_executable),
        _script(args, "merge_ipmsm_v2_case_plans.py"),
        "--case-plan",
        str(sources[0]),
        "--case-plan",
        str(sources[1]),
        "--output",
        str(paths.stage1_plan),
        "--manifest-output",
        str(paths.stage1_manifest),
        "--execute",
    ]


def _generator_argv(
    args: argparse.Namespace,
    paths: BatchPaths,
    latest: DecisionInfo,
    sources: Sequence[Path],
) -> list[str]:
    argv = [
        str(args.python_executable),
        _script(args, "generate_ipmsm_v2_adaptive_batch.py"),
        "--spec",
        str(args.spec),
        "--output",
        str(paths.adaptive_plan),
        "--manifest-output",
        str(paths.adaptive_manifest),
        "--failed-decision",
        str(latest.artifact.path),
        "--fixed-audit-case-plan",
        str(args.fixed_audit_case_plan),
        "--r2-history",
        str(paths.history),
    ]
    if paths.index == 1:
        argv.append("--initialize-r2-history")
    else:
        argv.extend(
            (
                "--advance-r2-history-from",
                str(_batch_paths(args, paths.index - 1).history),
            )
        )
    for source in sources:
        argv.extend(("--exclude-case-plan", str(source)))
    for exclusion in args.confirmed_exclusion_csv:
        argv.extend(("--confirmed-exclusion-csv", str(exclusion)))
    argv.extend(
        (
            "--batch-index",
            str(paths.index),
            "--case-prefix",
            f"v2-adaptive-b{paths.index:04d}",
            "--candidate-pool-geometries",
            str(args.candidate_pool_geometries),
            "--adaptation-seed-base",
            str(args.adaptation_seed_base),
            "--calibration-seed-base",
            str(args.calibration_seed_base),
            "--write",
        )
    )
    return argv


def _recovery_argv(
    args: argparse.Namespace,
    original: BatchPaths,
    recovery: BatchPaths,
    failed_design_hash: str,
) -> list[str]:
    return [
        str(args.python_executable),
        _script(args, "recover_ipmsm_v2_adaptive_failed_geometry.py"),
        "--spec",
        str(args.spec),
        "--original-plan",
        str(original.adaptive_plan),
        "--original-manifest",
        str(original.adaptive_manifest),
        "--continuation-decision",
        str(original.decision),
        "--campaign-summary",
        str(original.stage2_output / "campaign_summary.json"),
        "--campaign-decision",
        str(original.stage2_output / "campaign_decision.json"),
        "--failed-design-hash",
        failed_design_hash,
        "--expected-replacement-design-hash",
        EXPECTED_ADAPTIVE_RECOVERY_DESIGN_HASH,
        "--output",
        str(recovery.adaptive_plan),
        "--manifest-output",
        str(recovery.adaptive_manifest),
        "--execute",
    ]


def _remote_child(root: str, index: int) -> str:
    return str(PurePosixPath(root) / f"batch_{index:04d}")


def _continuation_argv(
    args: argparse.Namespace,
    paths: BatchPaths,
    latest: DecisionInfo,
    *,
    resume: bool,
    recovery: bool = False,
) -> list[str]:
    prior_rows = BASELINE_ROWS + ROWS_PER_BATCH * (paths.index - 1)
    prior_groups = BASELINE_GROUPS + GROUPS_PER_BATCH * (paths.index - 1)
    combined_rows = prior_rows + ROWS_PER_BATCH
    combined_groups = prior_groups + GROUPS_PER_BATCH
    artifacts = latest.combined_artifacts
    argv = [
        str(args.python_executable),
        _script(args, "continue_ipmsm_v2_stage2.py"),
        "--stage1-runner-pid-file",
        str(paths.runner_pid),
        "--stage1-watcher-pid-file",
        str(paths.watcher_pid),
        "--stage1-case-plan",
        str(paths.stage1_plan),
        "--stage1-result",
        str(artifacts["merged"].path),
        "--stage1-validation",
        str(artifacts["validation"].path),
        "--stage1-metadata",
        str(artifacts["metadata"].path),
        "--stage1-r2",
        str(artifacts["r2"].path),
        "--stage2-case-plan",
        str(paths.adaptive_plan),
        "--stage2-case-manifest",
        str(paths.adaptive_manifest),
        "--training-audit-case-plan",
        str(args.fixed_audit_case_plan),
        "--stage2-output-dir",
        str(paths.stage2_output),
        "--combined-output-dir",
        str(paths.combined_output),
        "--decision-output",
        str(paths.decision),
        "--project",
        args.project,
        "--scheduler-url",
        args.scheduler_url,
        "--project-active-cap",
        str(args.project_active_cap),
        "--stage2-task-prefix",
        f"{args.task_prefix_base}-b{paths.index:04d}",
        "--stage2-remote-cases-dir",
        _remote_child(args.remote_cases_root, paths.index),
        "--stage2-result-dir",
        _remote_child(args.result_dir_root, paths.index),
        "--stage2-simulation-dir",
        _remote_child(args.simulation_dir_root, paths.index),
        "--stage2-log-dir",
        _remote_child(args.log_dir_root, paths.index),
        "--beta-summary",
        str(args.beta_summary),
        "--beta-case-plan",
        str(args.beta_case_plan),
        "--beta-results",
        str(args.beta_results),
        "--beta-calibration-manifest",
        str(args.beta_calibration_manifest),
        "--r2-threshold",
        str(args.r2_threshold),
        "--expected-stage1-rows",
        str(prior_rows),
        "--expected-stage1-groups",
        str(prior_groups),
        "--expected-stage1-repeats",
        str(EXPECTED_REPEATS),
        "--expected-combined-rows",
        str(combined_rows),
        "--expected-combined-groups",
        str(combined_groups),
        "--expected-combined-repeats",
        str(EXPECTED_REPEATS),
        "--poll-interval-seconds",
        str(args.poll_interval_seconds),
        "--overall-timeout-seconds",
        str(args.overall_timeout_seconds),
        "--terminal-retry-limit",
        "0" if recovery else str(args.terminal_retry_limit),
        "--ensemble-size",
        str(args.ensemble_size),
        "--conformal-coverage",
        str(args.conformal_coverage),
    ]
    if resume:
        argv.append("--resume")
    argv.append("--execute")
    return argv


def _run_subprocess(
    argv: Sequence[str],
    label: str,
    allowed_codes: set[int],
    log_path: Path,
) -> int:
    if os.path.lexists(log_path.parent) and (
        _is_reparse_point(log_path.parent) or not log_path.parent.is_dir()
    ):
        raise CoordinatorError(f"child log parent is not a regular directory: {log_path.parent}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(log_path.parent):
        raise CoordinatorError(f"child log parent is a symlink/reparse point: {log_path.parent}")
    if os.path.lexists(log_path) and (log_path.is_symlink() or not log_path.is_file()):
        raise CoordinatorError(f"child log is not a regular file: {log_path}")
    with log_path.open("a+b") as output:
        header = {
            "argv": list(argv),
            "event": "child_start",
            "label": label,
            "schema_version": SCHEMA_VERSION,
        }
        output.write(
            (
                "\n"
                + json.dumps(
                    header,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        )
        output.flush()
        os.fsync(output.fileno())
        try:
            completed = subprocess.run(
                list(argv),
                check=False,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            spawn_footer = {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "event": "child_exit",
                "label": label,
                "outcome": "spawn_error",
                "returncode": None,
                "schema_version": SCHEMA_VERSION,
            }
            output.write(
                (
                    "\n"
                    + json.dumps(
                        spawn_footer,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            output.flush()
            os.fsync(output.fileno())
            raise CoordinatorError(
                f"{label} could not start; log={log_path}: {exc}"
            ) from exc
        footer = {
            "event": "child_exit",
            "label": label,
            "returncode": completed.returncode,
            "schema_version": SCHEMA_VERSION,
        }
        output.write(
            (
                "\n"
                + json.dumps(
                    footer,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        )
        output.flush()
        os.fsync(output.fileno())
        if completed.returncode not in allowed_codes:
            output.seek(0, os.SEEK_END)
            size = output.tell()
            output.seek(max(0, size - 16_384))
            tail = output.read().decode("utf-8", errors="replace")
            evidence = " ".join(tail.strip().splitlines()[-3:])
            raise CoordinatorError(
                f"{label} returned {completed.returncode}; log={log_path}: "
                f"{evidence or 'no output'}"
            )
        return completed.returncode


def _config_identity(args: argparse.Namespace) -> dict[str, Any]:
    immutable_paths = {
        "spec": args.spec,
        "initial_stage1_case_plan": args.initial_stage1_case_plan,
        "initial_stage2_case_plan": args.initial_stage2_case_plan,
        "fixed_audit_case_plan": args.fixed_audit_case_plan,
        "beta_summary": args.beta_summary,
        "beta_case_plan": args.beta_case_plan,
        "beta_results": args.beta_results,
        "beta_calibration_manifest": args.beta_calibration_manifest,
    }
    dependencies = {
        name: args.source_root / name
        for name in (
            "coordinate_ipmsm_v2_adaptive_campaign.py",
            "merge_ipmsm_v2_case_plans.py",
            "generate_ipmsm_v2_adaptive_batch.py",
            "continue_ipmsm_v2_stage2.py",
            "recover_ipmsm_v2_adaptive_failed_geometry.py",
        )
    }
    values: dict[str, Any] = {
        "source_root": str(args.source_root),
        "python_executable": str(args.python_executable),
        "campaign_root": str(args.campaign_root),
        "state_output": str(args.state_output),
        "stage2_output_root": str(args.stage2_output_root),
        "combined_output_root": str(args.combined_output_root),
        "initial_failed_decision": str(args.initial_failed_decision),
        "project": args.project,
        "scheduler_url": args.scheduler_url,
        "project_active_cap": args.project_active_cap,
        "candidate_pool_geometries": args.candidate_pool_geometries,
        "adaptation_seed_base": args.adaptation_seed_base,
        "calibration_seed_base": args.calibration_seed_base,
        "task_prefix_base": args.task_prefix_base,
        "remote_namespaces": {
            "cases": args.remote_cases_root,
            "results": args.result_dir_root,
            "simulation": args.simulation_dir_root,
            "logs": args.log_dir_root,
        },
        "r2_threshold": args.r2_threshold,
        "poll_interval_seconds": args.poll_interval_seconds,
        "overall_timeout_seconds": args.overall_timeout_seconds,
        "terminal_retry_limit": args.terminal_retry_limit,
        "ensemble_size": args.ensemble_size,
        "conformal_coverage": args.conformal_coverage,
        "immutable_artifacts": {
            name: _artifact(path, f"coordinator config {name}").record()
            for name, path in immutable_paths.items()
        },
        "confirmed_exclusions": [
            _artifact(path, "coordinator confirmed exclusion").record()
            for path in args.confirmed_exclusion_csv
        ],
        "dependencies": {
            name: _artifact(path, f"coordinator dependency {name}").record()
            for name, path in dependencies.items()
        },
    }
    return {
        "schema_version": "ipmsm-v2-adaptive-campaign-config-v1",
        "sha256": _canonical_sha256(values),
    }


def _state(
    args: argparse.Namespace,
    *,
    status: str,
    current_batch: int,
    action: str,
    latest: DecisionInfo | None,
    final: DecisionInfo | None = None,
    commands: Sequence[Sequence[str]] = (),
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "config_identity": _config_identity(args),
        "mode": "execute" if args.execute else "dry-run",
        "status": status,
        "current_batch": current_batch,
        "action": action,
        "latest_decision": (
            {**latest.artifact.record(), "status": latest.status} if latest else None
        ),
        "final_decision": final.artifact.record() if final else None,
        "planned_commands": [list(command) for command in commands],
        "error": error,
    }


def _remember_progress(
    args: argparse.Namespace,
    *,
    current_batch: int,
    action: str,
    latest: DecisionInfo | None,
    commands: Sequence[Sequence[str]] = (),
) -> None:
    setattr(
        args,
        "_coordinator_progress_context",
        ProgressContext(
            current_batch=current_batch,
            action=action,
            latest=latest,
            commands=tuple(tuple(command) for command in commands),
        ),
    )


def _atomic_write_state(path: Path, state: Mapping[str, Any]) -> None:
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
        raise CoordinatorError(f"telemetry output is not a regular file: {path}")
    if path.is_file():
        _, existing = _read_json(path, "existing coordinator telemetry")
        expected_fields = {
            "schema_version",
            "config_identity",
            "mode",
            "status",
            "current_batch",
            "action",
            "latest_decision",
            "final_decision",
            "planned_commands",
            "error",
        }
        if (
            set(existing) != expected_fields
            or existing.get("schema_version") != SCHEMA_VERSION
            or existing.get("config_identity") != state.get("config_identity")
            or existing.get("mode") != "execute"
            or existing.get("status")
            not in {
                "waiting",
                "running",
                "ready_for_optimization",
                "plateau_stopped",
                "error",
            }
        ):
            raise CoordinatorError(
                "refusing to replace foreign or config-mismatched coordinator telemetry"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            dict(state),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _emit(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    if args.execute:
        _atomic_write_state(args.state_output, state)
    print(
        json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    )
    return state


def _paths_overlap(left: Path, right: Path) -> bool:
    first = left.resolve(strict=False)
    second = right.resolve(strict=False)
    return first == second or first in second.parents or second in first.parents


def _validate_protected_input_aliases(
    paths: Mapping[str, Path],
    artifacts: Mapping[str, Artifact],
) -> None:
    aliases: dict[Path, list[str]] = {}
    for name, path in paths.items():
        aliases.setdefault(path.resolve(strict=False), []).append(name)
    resolved_items = list(aliases)
    for index, left in enumerate(resolved_items):
        for right in resolved_items[index + 1 :]:
            try:
                same_file = (
                    left.is_file()
                    and right.is_file()
                    and os.path.samefile(left, right)
                )
            except OSError as exc:
                raise CoordinatorError(
                    f"cannot inspect coordinator input file identity: {exc}"
                ) from exc
            if same_file:
                raise CoordinatorError(
                    "hard-linked coordinator input artifact paths are not permitted"
                )
    allowed_names = {"initial_stage2_case_plan", "fixed_audit_case_plan"}
    for names in aliases.values():
        if len(names) == 1:
            continue
        name_set = set(names)
        if name_set != allowed_names or len(names) != len(allowed_names):
            raise CoordinatorError(
                "coordinator input artifact paths must be distinct except for the "
                "exact Stage2/fixed-audit alias"
            )
        stage2 = artifacts.get("initial_stage2_case_plan")
        fixed_audit = artifacts.get("fixed_audit_case_plan")
        if stage2 is None or fixed_audit is None or stage2 != fixed_audit:
            raise CoordinatorError(
                "aliased Stage2/fixed-audit artifact records must be exact-equal"
            )


def _validate_path_args(args: argparse.Namespace) -> None:
    path_names = (
        "source_root",
        "campaign_root",
        "state_output",
        "spec",
        "initial_failed_decision",
        "initial_stage1_case_plan",
        "initial_stage2_case_plan",
        "fixed_audit_case_plan",
        "stage2_output_root",
        "combined_output_root",
        "beta_summary",
        "beta_case_plan",
        "beta_results",
        "beta_calibration_manifest",
    )
    paths = [getattr(args, name) for name in path_names]
    paths.extend(args.confirmed_exclusion_csv)
    if any(not path.is_absolute() for path in paths):
        raise CoordinatorError("all filesystem paths must be explicit absolute paths")
    for name in (
        "campaign_root",
        "state_output",
        "stage2_output_root",
        "combined_output_root",
    ):
        raw_output = getattr(args, name)
        if os.path.lexists(raw_output) and _is_reparse_point(raw_output):
            raise CoordinatorError(
                f"writable coordinator path is a symlink/reparse point: {raw_output}"
            )
    for name in path_names:
        setattr(args, name, getattr(args, name).resolve(strict=False))
    args.confirmed_exclusion_csv = [
        path.resolve(strict=False) for path in args.confirmed_exclusion_csv
    ]
    if not args.python_executable.is_absolute():
        raise CoordinatorError("Python executable must be an explicit absolute path")
    args.python_executable = args.python_executable.resolve(strict=False)
    immutable_input_names = (
        "spec",
        "initial_stage1_case_plan",
        "initial_stage2_case_plan",
        "fixed_audit_case_plan",
        "beta_summary",
        "beta_case_plan",
        "beta_results",
        "beta_calibration_manifest",
    )
    protected_input_artifacts = {
        name: _artifact(getattr(args, name), name.replace("_", " "))
        for name in immutable_input_names
    }
    protected_input_artifacts.update(
        {
            f"confirmed_exclusion_csv[{index}]": _artifact(
                path, "confirmed exclusion CSV"
            )
            for index, path in enumerate(args.confirmed_exclusion_csv)
        }
    )
    protected_input_paths = {
        "initial_failed_decision": args.initial_failed_decision,
        **{name: getattr(args, name) for name in immutable_input_names},
        **{
            f"confirmed_exclusion_csv[{index}]": path
            for index, path in enumerate(args.confirmed_exclusion_csv)
        },
    }
    _validate_protected_input_aliases(
        protected_input_paths, protected_input_artifacts
    )
    if not args.source_root.is_dir():
        raise CoordinatorError(f"source root is missing: {args.source_root}")
    if not args.python_executable.is_absolute() or not args.python_executable.is_file():
        raise CoordinatorError(
            f"Python executable must be an explicit file: {args.python_executable}"
        )
    runtime_source_root = Path(__file__).resolve().parent
    runtime_python = Path(sys.executable).resolve()
    if args.source_root != runtime_source_root:
        raise CoordinatorError(
            "--source-root must equal the coordinator runtime source authority"
        )
    if args.python_executable != runtime_python:
        raise CoordinatorError(
            "--python-executable must equal the coordinator runtime interpreter"
        )
    for name in (
        "coordinate_ipmsm_v2_adaptive_campaign.py",
        "merge_ipmsm_v2_case_plans.py",
        "generate_ipmsm_v2_adaptive_batch.py",
        "continue_ipmsm_v2_stage2.py",
        "recover_ipmsm_v2_adaptive_failed_geometry.py",
    ):
        _artifact(args.source_root / name, f"coordinator dependency {name}")
    dependency_paths = tuple(
        (args.source_root / name).resolve(strict=False)
        for name in (
            "coordinate_ipmsm_v2_adaptive_campaign.py",
            "merge_ipmsm_v2_case_plans.py",
            "generate_ipmsm_v2_adaptive_batch.py",
            "continue_ipmsm_v2_stage2.py",
            "recover_ipmsm_v2_adaptive_failed_geometry.py",
        )
    )
    if len(set(dependency_paths)) != len(dependency_paths):
        raise CoordinatorError("coordinator dependency paths must be distinct")
    output_roots = (
        args.campaign_root.resolve(strict=False),
        args.stage2_output_root.resolve(strict=False),
        args.combined_output_root.resolve(strict=False),
    )
    if args.state_output.resolve(strict=False).parent != output_roots[0]:
        raise CoordinatorError("telemetry output must be a direct child of campaign root")
    for root in output_roots:
        if os.path.lexists(root) and not root.is_dir():
            raise CoordinatorError(f"campaign output root is not a directory: {root}")
    for index, left in enumerate(output_roots):
        for right in output_roots[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise CoordinatorError("campaign output roots must be distinct and non-nested")
    protected_inputs = tuple(protected_input_paths.values())
    for input_path in protected_inputs:
        if _paths_overlap(input_path, args.source_root) or _paths_overlap(
            input_path, args.python_executable
        ):
            raise CoordinatorError("coordinator input overlaps source/Python namespace")
    protected_namespaces = (
        args.source_root,
        args.python_executable,
        *protected_inputs,
        *dependency_paths,
    )
    for output_root in output_roots:
        for protected in protected_namespaces:
            if _paths_overlap(output_root, protected):
                raise CoordinatorError(
                    "writable coordinator namespace overlaps source/input/dependency"
                )
    if args.state_output in protected_namespaces:
        raise CoordinatorError("telemetry output aliases a protected artifact")
    if args.project_active_cap != PROJECT_ACTIVE_CAP:
        raise CoordinatorError(
            f"post-1300 campaign requires the exact active cap of {PROJECT_ACTIVE_CAP}"
        )
    if not math.isclose(args.r2_threshold, 0.95, abs_tol=1e-15):
        raise CoordinatorError("post-1300 campaign requires the exact R2 threshold of 0.95")
    if args.candidate_pool_geometries != 1024:
        raise CoordinatorError("adaptive candidate pool must remain 1024 geometries")
    if (args.adaptation_seed_base, args.calibration_seed_base) != (730031, 730033):
        raise CoordinatorError("adaptive seed bases must remain 730031 and 730033")
    if args.terminal_retry_limit != 1 or args.ensemble_size != 5:
        raise CoordinatorError("continuation retry limit / ensemble size must remain 1 / 5")
    if not math.isclose(args.conformal_coverage, 0.95, abs_tol=1e-15):
        raise CoordinatorError("conformal coverage must remain 0.95")
    for value in (
        args.poll_interval_seconds,
        args.overall_timeout_seconds,
        args.conformal_coverage,
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise CoordinatorError("timing/coverage values must be finite and positive")
    for remote in (
        args.remote_cases_root,
        args.result_dir_root,
        args.simulation_dir_root,
        args.log_dir_root,
    ):
        pure = PurePosixPath(remote)
        if pure.is_absolute() or not remote.strip() or ".." in pure.parts:
            raise CoordinatorError("remote scheduler roots must be safe relative POSIX paths")
    remote_roots = tuple(
        PurePosixPath(value)
        for value in (
            args.remote_cases_root,
            args.result_dir_root,
            args.simulation_dir_root,
            args.log_dir_root,
        )
    )
    for index, left in enumerate(remote_roots):
        for right in remote_roots[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise CoordinatorError(
                    "remote cases/result/simulation/log namespaces must be distinct and non-nested"
                )
    setattr(args, "_coordinator_paths_validated", True)


def _terminal_state(
    args: argparse.Namespace, decision: DecisionInfo, completed_batches: int
) -> dict[str, Any] | None:
    if decision.status == "complete":
        return _state(
            args,
            status="ready_for_optimization",
            current_batch=completed_batches,
            action="activate_nsga2",
            latest=decision,
            final=decision,
        )
    return None


def _audit_batch_decision_manifest(decision: DecisionInfo, paths: BatchPaths) -> None:
    expected = _artifact(paths.adaptive_manifest, "batch adaptive manifest")
    if decision.case_manifest != expected:
        raise CoordinatorError("adaptive decision case-manifest lineage changed")


def _audit_adaptive_decision_contract(
    args: argparse.Namespace,
    paths: BatchPaths,
    predecessor: DecisionInfo,
    decision: DecisionInfo,
) -> None:
    prior_rows = BASELINE_ROWS + ROWS_PER_BATCH * (paths.index - 1)
    prior_groups = BASELINE_GROUPS + GROUPS_PER_BATCH * (paths.index - 1)
    predecessor_gate = _revalidate_terminal_gate(
        predecessor,
        expected_rows=prior_rows,
        expected_groups=prior_groups,
    )
    if predecessor_gate is None:
        raise CoordinatorError("adaptive predecessor decision is not terminal")
    try:
        import continue_ipmsm_v2_stage2 as continuation

        recovery = paths == _recovery_batch_paths(_batch_paths(args, paths.index))
        continuation_argv = _continuation_argv(
            args, paths, predecessor, resume=False, recovery=recovery
        )[2:]
        if continuation_argv[-1] != "--execute":
            raise CoordinatorError("internal continuation command lacks --execute")
        continuation_args = continuation.build_parser().parse_args(
            continuation_argv[:-1]
        )
        expected = continuation._base_payload(continuation_args, predecessor_gate)
    except Exception as exc:
        raise CoordinatorError(
            f"cannot reconstruct exact adaptive continuation contract: {exc}"
        ) from exc
    expected_stage2 = dict(expected["stage2"])
    if decision.status != "stage2_started":
        stage2_result = _artifact(
            paths.stage2_output / "merged_results.csv", "adaptive Stage2 result"
        )
        expected_stage2.update(
            {"result": str(stage2_result.path), "result_sha256": stage2_result.sha256}
        )
    mismatches = [
        key
        for key in (
            "schema_version",
            "contract_sha256",
            "decision",
            "decision_output",
            "execution_contract",
            "stage1",
        )
        if decision.payload.get(key) != expected.get(key)
    ]
    if decision.payload.get("stage2") != expected_stage2:
        mismatches.append("stage2")
    if mismatches:
        raise CoordinatorError(
            "adaptive decision differs from actual continuation parser/contract: "
            + ", ".join(mismatches)
        )
    if decision.status != "stage2_started":
        expected_combined_artifacts = {
            "merged": paths.combined_output / "merged_results.csv",
            "validation": paths.combined_output / "validation.csv",
            "metadata": paths.combined_output / "models" / "metadata.json",
            "r2": paths.combined_output / "r2_gate.csv",
        }
        for name, path in expected_combined_artifacts.items():
            if decision.combined_artifacts[name] != _artifact(
                path, f"adaptive combined {name}"
            ):
                raise CoordinatorError("adaptive combined artifact namespace changed")


def _rehash_terminal_decision(
    decision: DecisionInfo,
    paths: BatchPaths | None,
) -> DecisionInfo:
    refreshed = _load_decision(decision.artifact.path, "terminal decision closing snapshot")
    _same_artifact(refreshed.artifact, decision.artifact, "terminal decision")
    if refreshed.status != "complete":
        raise CoordinatorError("terminal closing snapshot is not complete")
    execution = refreshed.payload["execution_contract"]
    stage1 = execution["stage1"]
    stage2 = execution["stage2"]
    training = execution["training"]
    beta = execution.get("beta")
    if not all(
        isinstance(value, Mapping)
        for value in (stage1, stage2, training, beta)
    ):
        raise CoordinatorError("terminal decision closing bindings are incomplete")
    direct_records = [
        ("terminal Stage1 case plan", stage1.get("case_plan")),
        ("terminal Stage1 result", stage1.get("result")),
        ("terminal Stage1 validation", stage1.get("validation")),
        ("terminal Stage1 metadata", stage1.get("metadata")),
        ("terminal Stage1 R2", stage1.get("r2")),
        ("terminal Stage2 case plan", stage2.get("case_plan")),
        ("terminal fixed audit", training.get("audit_case_plan")),
    ]
    if "case_manifest" in stage2:
        direct_records.append(
            ("terminal Stage2 case manifest", stage2.get("case_manifest"))
        )
    direct_records.extend(
        (f"terminal beta {name}", beta.get(name))
        for name in (
            "summary",
            "case_plan",
            "results",
            "calibration_manifest",
        )
    )
    for label, record in direct_records:
        _bound_artifact(record, label)
    top_stage2 = refreshed.payload.get("stage2")
    if not isinstance(top_stage2, Mapping):
        raise CoordinatorError("terminal decision lacks top-level Stage2 evidence")
    stage2_result = _bound_artifact(
        {
            "path": top_stage2.get("result"),
            "sha256": top_stage2.get("result_sha256"),
        },
        "terminal Stage2 result",
    )
    if paths is not None:
        expected_stage2_result = _artifact(
            paths.stage2_output / "merged_results.csv", "terminal Stage2 result"
        )
        _same_artifact(stage2_result, expected_stage2_result, "terminal Stage2 result")
    for name, artifact in refreshed.combined_artifacts.items():
        _same_artifact(
            _artifact(artifact.path, f"terminal combined {name}"),
            artifact,
            f"terminal combined {name}",
        )
    _same_artifact(
        _artifact(refreshed.artifact.path, "terminal decision final rehash"),
        refreshed.artifact,
        "terminal decision final rehash",
    )
    return refreshed


def _closing_batch_audit(
    args: argparse.Namespace,
    paths: BatchPaths,
    predecessor: DecisionInfo,
    source_paths: Sequence[Path],
    fixed_audit: Artifact,
    decision: DecisionInfo,
    *,
    expected_rows: int,
    expected_groups: int,
) -> DecisionInfo:
    _validate_batch_paths(args, paths)
    source_artifacts = _audit_source_lineage(
        predecessor,
        source_paths,
        fixed_audit,
        expected_rows=expected_rows,
        expected_groups=expected_groups,
    )
    _audit_merge_pair(
        paths.stage1_plan,
        paths.stage1_manifest,
        source_paths,
        expected_rows=expected_rows,
        expected_groups=expected_groups,
    )
    _audit_adaptive_pair(args, paths, source_artifacts, predecessor, fixed_audit)
    refreshed = _load_decision(paths.decision, "terminal adaptive batch decision")
    _same_artifact(refreshed.artifact, decision.artifact, "terminal adaptive decision")
    _audit_batch_decision_manifest(refreshed, paths)
    _audit_adaptive_decision_contract(args, paths, predecessor, refreshed)
    _audit_source_lineage(
        refreshed,
        (paths.stage1_plan, paths.adaptive_plan),
        fixed_audit,
        expected_rows=expected_rows + ROWS_PER_BATCH,
        expected_groups=expected_groups + GROUPS_PER_BATCH,
    )
    _validate_batch_paths(args, paths)
    return _rehash_terminal_decision(refreshed, paths)


def _run_failed_geometry_recovery(
    args: argparse.Namespace,
    original_paths: BatchPaths,
    recovery_paths: BatchPaths,
    predecessor: DecisionInfo,
    original_decision: DecisionInfo,
    evidence: FailedGeometryRecoveryEvidence,
    source_artifacts: Sequence[Artifact],
    source_paths: Sequence[Path],
    fixed_audit: Artifact,
    *,
    expected_rows: int,
    expected_groups: int,
) -> dict[str, Any]:
    recovery_state = _pair_state(
        recovery_paths.adaptive_plan,
        recovery_paths.adaptive_manifest,
        "adaptive failed-geometry recovery pair",
    )
    publisher = _recovery_argv(
        args, original_paths, recovery_paths, evidence.failed_design_hash
    )
    continuation = _continuation_argv(
        args, recovery_paths, predecessor, resume=False, recovery=True
    )
    commands = ([publisher] if recovery_state == "absent" else []) + [continuation]
    _remember_progress(
        args,
        current_batch=original_paths.index,
        action="recover_failed_adaptive_geometry",
        latest=original_decision,
        commands=commands,
    )
    if not args.execute:
        return _emit(
            args,
            _state(
                args,
                status="waiting",
                current_batch=original_paths.index,
                action="recover_failed_adaptive_geometry",
                latest=original_decision,
                commands=commands,
            ),
        )
    _validate_batch_paths(args, original_paths)
    _validate_batch_paths(args, recovery_paths)
    if recovery_state == "absent":
        _run_subprocess(
            publisher,
            "adaptive failed-geometry recovery publisher",
            {0},
            original_paths.root / "logs" / "recovery_publisher.log",
        )
    if (
        _pair_state(
            recovery_paths.adaptive_plan,
            recovery_paths.adaptive_manifest,
            "adaptive failed-geometry recovery pair",
        )
        != "complete"
    ):
        raise CoordinatorError("adaptive recovery publisher did not create a complete pair")
    _audit_adaptive_pair(
        args,
        recovery_paths,
        source_artifacts,
        predecessor,
        fixed_audit,
    )
    if recovery_paths.decision.exists():
        raise CoordinatorError("adaptive recovery decision appeared before its continuation")
    _ensure_completed_stage1_pid_markers(recovery_paths, predecessor)
    _atomic_write_state(
        args.state_output,
        _state(
            args,
            status="running",
            current_batch=original_paths.index,
            action="run_failed_geometry_recovery_continuation",
            latest=original_decision,
            commands=(continuation,),
        ),
    )
    _validate_batch_paths(args, recovery_paths)
    continuation_code = _run_subprocess(
        continuation,
        "adaptive failed-geometry recovery continuation",
        {0, 1},
        original_paths.root / "logs" / "recovery_continuation.log",
    )
    final = _load_decision(
        recovery_paths.decision,
        f"adaptive batch {original_paths.index} recovery decision",
    )
    _audit_batch_decision_manifest(final, recovery_paths)
    _audit_adaptive_decision_contract(
        args, recovery_paths, predecessor, final
    )
    _audit_source_lineage(
        final,
        (recovery_paths.stage1_plan, recovery_paths.adaptive_plan),
        fixed_audit,
        expected_rows=expected_rows + ROWS_PER_BATCH,
        expected_groups=expected_groups + GROUPS_PER_BATCH,
    )
    if (continuation_code == 0) != (final.status == "complete"):
        raise CoordinatorError(
            "recovery continuation exit code and terminal decision disagree"
        )
    if final.status == "complete":
        final = _closing_batch_audit(
            args,
            recovery_paths,
            predecessor,
            source_paths,
            fixed_audit,
            final,
            expected_rows=expected_rows,
            expected_groups=expected_groups,
        )
    terminal = _terminal_state(args, final, original_paths.index)
    if terminal is not None:
        return _emit(args, terminal)
    if final.status != "combined_r2_failed":
        raise CoordinatorError("recovery continuation did not reach a terminal decision")
    return _emit(
        args,
        _state(
            args,
            status="waiting",
            current_batch=original_paths.index,
            action="prepare_next_adaptive_batch",
            latest=final,
        ),
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_path_args(args)
    _remember_progress(
        args,
        current_batch=0,
        action="audit_initial_campaign_inputs",
        latest=None,
    )
    fixed_audit = _artifact(args.fixed_audit_case_plan, "fixed Stage3 audit plan")
    if not args.initial_failed_decision.is_file():
        return _emit(
            args,
            _state(
                args,
                status="waiting",
                current_batch=0,
                action="wait_for_initial_1300_decision",
                latest=None,
            ),
        )
    latest = _load_decision(args.initial_failed_decision, "initial 1300 decision")
    _remember_progress(
        args,
        current_batch=0,
        action="audit_initial_1300_decision",
        latest=latest,
    )
    initial_sources = (
        args.initial_stage1_case_plan.resolve(strict=False),
        args.initial_stage2_case_plan.resolve(strict=False),
    )
    _audit_source_lineage(
        latest,
        initial_sources,
        fixed_audit,
        expected_rows=BASELINE_ROWS,
        expected_groups=BASELINE_GROUPS,
    )
    if latest.status == "complete":
        latest = _rehash_terminal_decision(latest, None)
    terminal = _terminal_state(args, latest, 0)
    if terminal is not None:
        return _emit(args, terminal)
    if latest.status == "stage2_started":
        return _emit(
            args,
            _state(
                args,
                status="waiting",
                current_batch=0,
                action="wait_for_initial_1300_continuation",
                latest=latest,
            ),
        )

    sources = initial_sources
    for batch_index in range(1, 1001):
        paths = _batch_paths(args, batch_index)
        _remember_progress(
            args,
            current_batch=batch_index,
            action="audit_adaptive_batch",
            latest=latest,
        )
        _validate_batch_paths(args, paths)
        expected_rows = BASELINE_ROWS + ROWS_PER_BATCH * (batch_index - 1)
        expected_groups = BASELINE_GROUPS + GROUPS_PER_BATCH * (batch_index - 1)
        source_artifacts = _audit_source_lineage(
            latest,
            sources,
            fixed_audit,
            expected_rows=expected_rows,
            expected_groups=expected_groups,
        )

        merge_state = _pair_state(
            paths.stage1_plan, paths.stage1_manifest, "merged Stage1 pair"
        )
        adaptive_state = _pair_state(
            paths.adaptive_plan, paths.adaptive_manifest, "adaptive batch pair"
        )
        if adaptive_state == "complete" and merge_state != "complete":
            raise CoordinatorError("adaptive pair exists without its merged Stage1 authority")
        merge_command = _merge_argv(args, paths, sources)
        generator_command = _generator_argv(args, paths, latest, sources)
        if merge_state == "complete":
            _audit_merge_pair(
                paths.stage1_plan,
                paths.stage1_manifest,
                sources,
                expected_rows=expected_rows,
                expected_groups=expected_groups,
            )
        if adaptive_state == "complete":
            _audit_adaptive_pair(args, paths, source_artifacts, latest, fixed_audit)

        recovery_paths = _recovery_batch_paths(paths)
        recovery_state = _pair_state(
            recovery_paths.adaptive_plan,
            recovery_paths.adaptive_manifest,
            "adaptive failed-geometry recovery pair",
        )
        recovery_decision_exists = os.path.lexists(recovery_paths.decision)
        if recovery_decision_exists and recovery_state != "complete":
            raise CoordinatorError(
                "adaptive recovery decision exists without its complete recovery pair"
            )
        decision_paths = paths
        decision_is_recovery = False
        if paths.decision.exists():
            if merge_state != "complete" or adaptive_state != "complete":
                raise CoordinatorError(
                    "batch decision exists without complete immutable inputs"
                )
            original_decision = _load_decision(
                paths.decision, f"adaptive batch {batch_index} decision"
            )
            recovery_evidence = None
            if original_decision.status == "stage2_started":
                recovery_evidence = _terminal_failed_geometry_recovery_evidence(
                    paths, original_decision
                )
            if recovery_evidence is not None:
                _audit_batch_decision_manifest(original_decision, paths)
                _audit_adaptive_decision_contract(
                    args, paths, latest, original_decision
                )
                _audit_source_lineage(
                    original_decision,
                    (paths.stage1_plan, paths.adaptive_plan),
                    fixed_audit,
                    expected_rows=expected_rows + ROWS_PER_BATCH,
                    expected_groups=expected_groups + GROUPS_PER_BATCH,
                )
                if recovery_state == "complete":
                    _audit_adaptive_pair(
                        args,
                        recovery_paths,
                        source_artifacts,
                        latest,
                        fixed_audit,
                    )
                if not recovery_decision_exists:
                    return _run_failed_geometry_recovery(
                        args,
                        paths,
                        recovery_paths,
                        latest,
                        original_decision,
                        recovery_evidence,
                        source_artifacts,
                        sources,
                        fixed_audit,
                        expected_rows=expected_rows,
                        expected_groups=expected_groups,
                    )
                decision_paths = recovery_paths
                decision_is_recovery = True
            elif recovery_state != "absent" or recovery_decision_exists:
                raise CoordinatorError(
                    "adaptive recovery artifacts lack exact terminal failure authority"
                )
        elif recovery_state != "absent" or recovery_decision_exists:
            raise CoordinatorError(
                "adaptive recovery artifacts exist without the original failed decision"
            )

        if decision_paths.decision.exists():
            if merge_state != "complete" or adaptive_state != "complete":
                raise CoordinatorError("batch decision exists without complete immutable inputs")
            decision = _load_decision(
                decision_paths.decision,
                f"adaptive batch {batch_index}"
                + (" recovery" if decision_is_recovery else "")
                + " decision",
            )
            _audit_batch_decision_manifest(decision, decision_paths)
            _audit_adaptive_decision_contract(
                args, decision_paths, latest, decision
            )
            _audit_source_lineage(
                decision,
                (decision_paths.stage1_plan, decision_paths.adaptive_plan),
                fixed_audit,
                expected_rows=expected_rows + ROWS_PER_BATCH,
                expected_groups=expected_groups + GROUPS_PER_BATCH,
            )
            if decision.status == "complete":
                _remember_progress(
                    args,
                    current_batch=batch_index,
                    action="close_terminal_adaptive_authority",
                    latest=latest,
                )
                decision = _closing_batch_audit(
                    args,
                    decision_paths,
                    latest,
                    sources,
                    fixed_audit,
                    decision,
                    expected_rows=expected_rows,
                    expected_groups=expected_groups,
                )
            terminal = _terminal_state(args, decision, batch_index)
            if terminal is not None:
                return _emit(args, terminal)
            if decision.status == "combined_r2_failed":
                latest = decision
                sources = (
                    decision_paths.stage1_plan.resolve(strict=False),
                    decision_paths.adaptive_plan.resolve(strict=False),
                )
                continue
            continuation = _continuation_argv(
                args,
                decision_paths,
                latest,
                resume=True,
                recovery=decision_is_recovery,
            )
            _remember_progress(
                args,
                current_batch=batch_index,
                action="resume_adaptive_continuation",
                latest=decision,
                commands=(continuation,),
            )
            if not args.execute:
                return _emit(
                    args,
                    _state(
                        args,
                        status="running",
                        current_batch=batch_index,
                        action="resume_adaptive_continuation",
                        latest=decision,
                        commands=(continuation,),
                    ),
                )
            _validate_batch_paths(args, decision_paths)
            _ensure_completed_stage1_pid_markers(decision_paths, latest)
            _atomic_write_state(
                args.state_output,
                _state(
                    args,
                    status="running",
                    current_batch=batch_index,
                    action="resume_adaptive_continuation",
                    latest=decision,
                    commands=(continuation,),
                ),
            )
            _validate_batch_paths(args, decision_paths)
            continuation_code = _run_subprocess(
                continuation,
                (
                    "adaptive failed-geometry recovery continuation resume"
                    if decision_is_recovery
                    else "adaptive continuation resume"
                ),
                {0, 1},
                decision_paths.root
                / "logs"
                / (
                    "recovery_continuation.log"
                    if decision_is_recovery
                    else "continuation.log"
                ),
            )
            final = _load_decision(
                decision_paths.decision,
                f"adaptive batch {batch_index}"
                + (" recovery" if decision_is_recovery else "")
                + " decision",
            )
            _audit_batch_decision_manifest(final, decision_paths)
            _audit_adaptive_decision_contract(
                args, decision_paths, latest, final
            )
            _audit_source_lineage(
                final,
                (decision_paths.stage1_plan, decision_paths.adaptive_plan),
                fixed_audit,
                expected_rows=expected_rows + ROWS_PER_BATCH,
                expected_groups=expected_groups + GROUPS_PER_BATCH,
            )
            if (continuation_code == 0) != (final.status == "complete"):
                raise CoordinatorError(
                    "continuation resume exit code and terminal decision disagree"
                )
            if final.status == "complete":
                _remember_progress(
                    args,
                    current_batch=batch_index,
                    action="close_terminal_adaptive_authority",
                    latest=decision,
                )
                final = _closing_batch_audit(
                    args,
                    decision_paths,
                    latest,
                    sources,
                    fixed_audit,
                    final,
                    expected_rows=expected_rows,
                    expected_groups=expected_groups,
                )
            terminal = _terminal_state(args, final, batch_index)
            if terminal is not None:
                return _emit(args, terminal)
            if final.status != "combined_r2_failed":
                raise CoordinatorError("continuation resume did not reach a terminal decision")
            return _emit(
                args,
                _state(
                    args,
                    status="waiting",
                    current_batch=batch_index,
                    action="prepare_next_adaptive_batch",
                    latest=final,
                ),
            )

        commands: list[list[str]] = []
        if not args.execute and adaptive_state == "absent" and paths.history.is_file():
            history = _audit_history(paths.history, latest, batch_index)
            if history["plateau"]["stop_fea"] is True:
                return _emit(
                    args,
                    _state(
                        args,
                        status="plateau_stopped",
                        current_batch=batch_index - 1,
                        action="diagnose_model_or_physics",
                        latest=latest,
                    ),
                )
        if merge_state == "absent":
            commands.append(merge_command)
        if adaptive_state == "absent":
            commands.append(generator_command)
        continuation = _continuation_argv(args, paths, latest, resume=False)
        commands.append(continuation)
        if not args.execute:
            return _emit(
                args,
                _state(
                    args,
                    status="waiting",
                    current_batch=batch_index - 1,
                    action="prepare_adaptive_batch",
                    latest=latest,
                    commands=commands,
                ),
            )

        if merge_state == "absent":
            _remember_progress(
                args,
                current_batch=batch_index,
                action="merge_adaptive_stage1_authority",
                latest=latest,
                commands=(merge_command,),
            )
            _validate_batch_paths(args, paths)
            _run_subprocess(
                merge_command,
                "Stage1 case-plan merge",
                {0},
                paths.root / "logs" / "merge.log",
            )
            _audit_merge_pair(
                paths.stage1_plan,
                paths.stage1_manifest,
                sources,
                expected_rows=expected_rows,
                expected_groups=expected_groups,
            )
        if adaptive_state == "absent":
            _remember_progress(
                args,
                current_batch=batch_index,
                action="generate_adaptive_batch",
                latest=latest,
                commands=(generator_command,),
            )
            _validate_batch_paths(args, paths)
            generator_code = _run_subprocess(
                generator_command,
                "adaptive batch generation",
                {0, 1},
                paths.root / "logs" / "generator.log",
            )
            if paths.history.is_file():
                history = _audit_history(paths.history, latest, batch_index)
                if history["plateau"]["stop_fea"] is True:
                    if _pair_state(
                        paths.adaptive_plan,
                        paths.adaptive_manifest,
                        "plateau adaptive pair",
                    ) != "absent":
                        raise CoordinatorError("plateau advancement published a forbidden FEA plan")
                    return _emit(
                        args,
                        _state(
                            args,
                            status="plateau_stopped",
                            current_batch=batch_index - 1,
                            action="diagnose_model_or_physics",
                            latest=latest,
                        ),
                    )
            if generator_code != 0:
                raise CoordinatorError("adaptive generator failed without an exact plateau")
            _audit_adaptive_pair(args, paths, source_artifacts, latest, fixed_audit)
        _validate_batch_paths(args, paths)
        _ensure_completed_stage1_pid_markers(paths, latest)
        _atomic_write_state(
            args.state_output,
            _state(
                args,
                status="running",
                current_batch=batch_index,
                action="run_adaptive_continuation",
                latest=latest,
                commands=(continuation,),
            ),
        )
        _remember_progress(
            args,
            current_batch=batch_index,
            action="run_adaptive_continuation",
            latest=latest,
            commands=(continuation,),
        )
        _validate_batch_paths(args, paths)
        continuation_code = _run_subprocess(
            continuation,
            "adaptive continuation",
            {0, 1},
            paths.root / "logs" / "continuation.log",
        )
        final = _load_decision(paths.decision, f"adaptive batch {batch_index} decision")
        _audit_batch_decision_manifest(final, paths)
        _audit_adaptive_decision_contract(args, paths, latest, final)
        _audit_source_lineage(
            final,
            (paths.stage1_plan, paths.adaptive_plan),
            fixed_audit,
            expected_rows=expected_rows + ROWS_PER_BATCH,
            expected_groups=expected_groups + GROUPS_PER_BATCH,
        )
        if (continuation_code == 0) != (final.status == "complete"):
            raise CoordinatorError(
                "continuation exit code and terminal decision disagree"
            )
        if final.status == "complete":
            _remember_progress(
                args,
                current_batch=batch_index,
                action="close_terminal_adaptive_authority",
                latest=latest,
            )
            final = _closing_batch_audit(
                args,
                paths,
                latest,
                sources,
                fixed_audit,
                final,
                expected_rows=expected_rows,
                expected_groups=expected_groups,
            )
        terminal = _terminal_state(args, final, batch_index)
        if terminal is not None:
            return _emit(args, terminal)
        if final.status != "combined_r2_failed":
            raise CoordinatorError("continuation did not reach a terminal decision")
        return _emit(
            args,
            _state(
                args,
                status="waiting",
                current_batch=batch_index,
                action="prepare_next_adaptive_batch",
                latest=final,
            ),
        )
    raise CoordinatorError("campaign exceeded the 1000-batch corruption guard")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--state-output", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--initial-failed-decision", type=Path, required=True)
    parser.add_argument("--initial-stage1-case-plan", type=Path, required=True)
    parser.add_argument("--initial-stage2-case-plan", type=Path, required=True)
    parser.add_argument("--fixed-audit-case-plan", type=Path, required=True)
    parser.add_argument("--stage2-output-root", type=Path, required=True)
    parser.add_argument("--combined-output-root", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--scheduler-url", required=True)
    parser.add_argument("--beta-summary", type=Path, required=True)
    parser.add_argument("--beta-case-plan", type=Path, required=True)
    parser.add_argument("--beta-results", type=Path, required=True)
    parser.add_argument("--beta-calibration-manifest", type=Path, required=True)
    parser.add_argument(
        "--confirmed-exclusion-csv", type=Path, action="append", default=[]
    )
    parser.add_argument("--python-executable", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--project-active-cap", type=int, default=PROJECT_ACTIVE_CAP
    )
    parser.add_argument("--candidate-pool-geometries", type=int, default=1024)
    parser.add_argument("--adaptation-seed-base", type=int, default=730031)
    parser.add_argument("--calibration-seed-base", type=int, default=730033)
    parser.add_argument("--task-prefix-base", default="ipmsm-v2-adaptive")
    parser.add_argument("--remote-cases-root", default="remote/ipmsm_v2_adaptive")
    parser.add_argument("--result-dir-root", default="simul_log/ipmsm_v2_adaptive")
    parser.add_argument("--simulation-dir-root", default="simulation/ipmsm_v2_adaptive")
    parser.add_argument("--log-dir-root", default="simul_log_scheduler/ipmsm_v2_adaptive")
    parser.add_argument("--r2-threshold", type=float, default=0.95)
    parser.add_argument("--poll-interval-seconds", type=float, default=30.0)
    parser.add_argument("--overall-timeout-seconds", type=float, default=604800.0)
    parser.add_argument("--terminal-retry-limit", type=int, default=1)
    parser.add_argument("--ensemble-size", type=int, default=5)
    parser.add_argument("--conformal-coverage", type=float, default=0.95)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
        return 0
    except Exception as exc:
        if args.execute and getattr(args, "_coordinator_paths_validated", False):
            try:
                context = getattr(args, "_coordinator_progress_context", None)
                if not isinstance(context, ProgressContext):
                    context = ProgressContext(
                        current_batch=0,
                        action="operator_intervention_required",
                        latest=None,
                    )
                error_state = _state(
                    args,
                    status="error",
                    current_batch=context.current_batch,
                    action=f"{context.action}_failed",
                    latest=context.latest,
                    commands=context.commands,
                    error=str(exc),
                )
                _atomic_write_state(args.state_output, error_state)
                print(
                    json.dumps(
                        error_state,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    )
                )
            except Exception as state_exc:
                print(f"ERROR: cannot invalidate telemetry: {state_exc}", file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
