"""Rebuild the official Stage1 collection after the torque-unit incident.

The rebuild is dry-run-first.  It accepts only the published recovery bundle
and the published four-case forensic receipt, proves the original 700-row
collection internally consistent, reuses 699 result files byte-for-byte, and
remaps the verified Stage1 suspect replay into the one revised execution
identity.  ``--publish`` claims a fresh collection directory without replace
and atomically publishes a receipt only after the existing dataset validator
passes.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import ctypes
import csv
from dataclasses import dataclass
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

import atomic_publish
import audit_ipmsm_torque_unit_replay as forensic_audit
import collect_ipmsm_v2_campaign as collector
import merge_ipmsm_v2_results as merger
import prepare_ipmsm_torque_unit_recovery_plans as recovery_plans
import run_ipmsm_batch as batch_runner
import submit_ipmsm_v2_campaign as campaign_submitter
import validate_ipmsm_v2_dataset as dataset_validator


SCHEMA_VERSION = "ipmsm-v2-stage1-torque-unit-rebuild-receipt-v1"
EXPECTED_ROWS = 700
EXPECTED_PLAN_COLUMNS = 45
EXPECTED_RESULT_COLUMNS = 704
DIRECTORY_RENAME_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)
SOURCE_CASE_ID = "v2s1_0010_rated_torque_01"
REVISED_CASE_ID = SOURCE_CASE_ID + "_torqueunit_fix_v1"
REPLAY_CASE_ID = SOURCE_CASE_ID + "_torqueunit_replay_v1"

DEFAULT_RECOVERY_PLAN = recovery_plans.DEFAULT_STAGE1_OUTPUT
DEFAULT_RECOVERY_MANIFEST = recovery_plans.DEFAULT_MANIFEST_OUTPUT
DEFAULT_FORENSIC_RECEIPT = (
    forensic_audit.DEFAULT_OUTPUT_DIR / forensic_audit.RECEIPT_NAME
)
DEFAULT_ORIGINAL_COLLECTION = Path("collected/ipmsm_v2_foundation_stage1_700")
DEFAULT_STAGE1_COMPLETION = Path("simul_log_smoke/v4r3/stage1/completion.json")
DEFAULT_OUTPUT_COLLECTION = Path(
    "collected/ipmsm_v2_foundation_stage1_700_torqueunit_fix_v1"
)
DEFAULT_RECEIPT_OUTPUT = Path(
    "simul_log_smoke/v4r4/stage1_torqueunit_fix_rebuild.receipt.canonical.json"
)


class RebuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class CsvSnapshot:
    path: Path
    payload: bytes
    sha256: str
    fieldnames: tuple[str, ...]
    rows: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class RecoveryEvidence:
    plan: CsvSnapshot
    source_plan: CsvSnapshot
    manifest_path: Path
    manifest_payload: bytes
    manifest_sha256: str
    manifest: dict[str, Any]
    replacement: dict[str, Any]
    replay_plan: CsvSnapshot
    replay_manifest: dict[str, Any]


@dataclass(frozen=True)
class ForensicCase:
    record: dict[str, Any]
    result_path: Path
    result_payload: bytes
    fieldnames: tuple[str, ...]
    row: dict[str, str]
    raw_path: Path
    raw_payload: bytes


@dataclass(frozen=True)
class ForensicEvidence:
    output_dir: Path
    receipt_path: Path
    receipt_payload: bytes
    receipt_sha256: str
    receipt: dict[str, Any]
    cases: dict[str, ForensicCase]


@dataclass(frozen=True)
class OriginalResult:
    case_id: str
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class OriginalCollection:
    root: Path
    selected: CsvSnapshot
    merged: CsvSnapshot
    results: dict[str, OriginalResult]
    inventory_sha256: str
    total_result_bytes: int
    validation: dict[str, Any]


@dataclass(frozen=True)
class DirectoryClaim:
    identity: tuple[int, int]
    tree_canonical_sha256: str


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_sha(value: Any) -> str:
    return _sha256(_canonical_bytes(value)[:-1])


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _stable_bytes(path: Path, label: str) -> bytes:
    if not os.path.lexists(path) or path.is_symlink() or not path.is_file():
        raise RebuildError(f"{label} must be an existing regular file: {path}")
    try:
        first = path.read_bytes()
        second = path.read_bytes()
    except OSError as exc:
        raise RebuildError(f"cannot read {label}: {path}") from exc
    if first != second:
        raise RebuildError(f"{label} changed while it was read: {path}")
    return first


def _load_json(path: Path, label: str, *, canonical: bool = False) -> tuple[bytes, dict[str, Any]]:
    payload = _stable_bytes(path, label)
    try:
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RebuildError(f"{label} is not strict JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RebuildError(f"{label} must contain one JSON object")
    if canonical and payload != _canonical_bytes(value):
        raise RebuildError(f"{label} is not canonical JSON")
    return payload, value


def _decode_csv(
    path: Path,
    payload: bytes,
    label: str,
    *,
    expected_rows: int | None = None,
    expected_columns: int | None = None,
) -> CsvSnapshot:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        fields = tuple(reader.fieldnames or ())
        rows = tuple(dict(row) for row in reader)
    except (UnicodeError, csv.Error) as exc:
        raise RebuildError(f"{label} is not a valid UTF-8 CSV") from exc
    if not fields or len(fields) != len(set(fields)) or any(not str(x or "").strip() for x in fields):
        raise RebuildError(f"{label} has an invalid header")
    if any(set(row) != set(fields) or any(value is None for value in row.values()) for row in rows):
        raise RebuildError(f"{label} has a row outside its header")
    if expected_rows is not None and len(rows) != expected_rows:
        raise RebuildError(
            f"{label} row count changed: expected {expected_rows}, got {len(rows)}"
        )
    if expected_columns is not None and len(fields) != expected_columns:
        raise RebuildError(
            f"{label} column count changed: expected {expected_columns}, got {len(fields)}"
        )
    return CsvSnapshot(path, payload, _sha256(payload), fields, rows)


def _read_csv(
    path: Path,
    label: str,
    *,
    expected_rows: int | None = None,
    expected_columns: int | None = None,
) -> CsvSnapshot:
    return _decode_csv(
        path,
        _stable_bytes(path, label),
        label,
        expected_rows=expected_rows,
        expected_columns=expected_columns,
    )


def _path_from_record(record: Mapping[str, Any], key: str, label: str) -> Path:
    raw = record.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise RebuildError(f"{label} has no {key}")
    return Path(raw)


def _same_path(first: Path, second: Path) -> bool:
    return first.resolve(strict=False) == second.resolve(strict=False)


def load_recovery_evidence(plan_path: Path, manifest_path: Path) -> RecoveryEvidence:
    manifest_payload, manifest = _load_json(manifest_path, "published recovery manifest")
    if manifest.get("schema_version") != recovery_plans.SCHEMA_VERSION:
        raise RebuildError("published recovery manifest schema changed")
    sources = manifest.get("source_plans")
    revised = manifest.get("revised_plans")
    replay = manifest.get("sealed_replay")
    if not all(isinstance(value, dict) for value in (sources, revised, replay)):
        raise RebuildError("published recovery manifest is incomplete")
    source_stage1 = _path_from_record(sources["stage1"], "path", "Stage1 source binding")
    source_stage2 = _path_from_record(sources["stage2"], "path", "Stage2 source binding")
    revised_stage1 = _path_from_record(revised["stage1"], "path", "Stage1 revised binding")
    revised_stage2 = _path_from_record(revised["stage2"], "path", "Stage2 revised binding")
    replay_plan = _path_from_record(replay, "plan_path", "sealed replay binding")
    replay_manifest = _path_from_record(replay, "manifest_path", "sealed replay binding")
    if not _same_path(plan_path, revised_stage1):
        raise RebuildError("--recovery-plan is not the Stage1 plan bound by its manifest")

    try:
        expected_stage1, expected_stage2, expected_manifest = recovery_plans.build_recovery_bundle(
            source_stage1,
            source_stage2,
            replay_plan,
            replay_manifest,
            revised_stage1,
            revised_stage2,
            expected_replay_plan_sha256=recovery_plans.EXPECTED_REPLAY_PLAN_SHA256,
            expected_replay_manifest_sha256=recovery_plans.EXPECTED_REPLAY_MANIFEST_SHA256,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise RebuildError(f"published recovery bundle failed deterministic replay: {exc}") from exc
    actual_stage1 = _stable_bytes(plan_path, "published Stage1 recovery plan")
    actual_stage2 = _stable_bytes(revised_stage2, "published Stage2 recovery plan")
    if actual_stage1 != expected_stage1 or actual_stage2 != expected_stage2:
        raise RebuildError("published recovery plan bytes differ from deterministic replay")
    if manifest_payload != recovery_plans._manifest_bytes(expected_manifest):
        raise RebuildError("published recovery manifest differs from deterministic replay")
    replay_plan_snapshot = _read_csv(
        replay_plan,
        "sealed forensic replay plan",
        expected_rows=4,
        expected_columns=EXPECTED_PLAN_COLUMNS,
    )
    _, replay_manifest_value = _load_json(
        replay_manifest, "sealed forensic replay manifest"
    )
    plan = _decode_csv(
        plan_path,
        actual_stage1,
        "published Stage1 recovery plan",
        expected_rows=EXPECTED_ROWS,
        expected_columns=EXPECTED_PLAN_COLUMNS,
    )
    source_plan = _read_csv(
        source_stage1,
        "Stage1 source plan",
        expected_rows=EXPECTED_ROWS,
        expected_columns=EXPECTED_PLAN_COLUMNS,
    )
    replacements = [
        item
        for item in manifest.get("replacements", [])
        if isinstance(item, dict) and item.get("stage") == "stage1"
    ]
    if len(replacements) != 1:
        raise RebuildError("recovery manifest must bind exactly one Stage1 replacement")
    replacement = dict(replacements[0])
    expected_identity = {
        "source_case_id": SOURCE_CASE_ID,
        "revised_case_id": REVISED_CASE_ID,
        "replay_case_id": REPLAY_CASE_ID,
        "only_changed_fields": ["case_id"],
    }
    if any(replacement.get(key) != value for key, value in expected_identity.items()):
        raise RebuildError("recovery manifest Stage1 replacement identity changed")
    return RecoveryEvidence(
        plan=plan,
        source_plan=source_plan,
        manifest_path=manifest_path,
        manifest_payload=manifest_payload,
        manifest_sha256=_sha256(manifest_payload),
        manifest=manifest,
        replacement=replacement,
        replay_plan=replay_plan_snapshot,
        replay_manifest=replay_manifest_value,
    )


def _contained_file(path: Path, root: Path, label: str) -> Path:
    absolute = path.resolve(strict=False)
    parent = root.resolve(strict=False)
    try:
        absolute.relative_to(parent)
    except ValueError as exc:
        raise RebuildError(f"{label} escapes the forensic publication directory: {path}") from exc
    return absolute


def _compare_summary_fields(observed: Mapping[str, Any], expected: Mapping[str, Any], label: str) -> None:
    for key, value in expected.items():
        actual = observed.get(key)
        if isinstance(value, float):
            try:
                equal = math.isclose(float(actual), value, rel_tol=1e-12, abs_tol=1e-12)
            except (TypeError, ValueError):
                equal = False
        else:
            equal = actual == value
        if not equal:
            raise RebuildError(f"{label} changed field {key}")


def _positive_task_id(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise RebuildError(f"{label} has an invalid task id")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RebuildError(f"{label} has an invalid task id") from exc
    if result <= 0:
        raise RebuildError(f"{label} has an invalid task id")
    return result


def _task_exit_code(task: Mapping[str, Any], label: str) -> int | None:
    value = task.get("exit_code")
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RebuildError(f"{label} has an invalid exit_code") from exc


def _expected_dedupe(row: Mapping[str, str], policy: Mapping[str, Any]) -> tuple[str, str]:
    case_id = str(row.get("case_id") or "")
    safe_case_id = campaign_submitter.sanitize_case_id(case_id)
    args = SimpleNamespace(
        project=policy["project"],
        task_prefix=policy["task_prefix"],
        remote_cases_dir=policy["remote_cases_dir"],
        result_dir=policy["result_dir"],
    )
    return safe_case_id, campaign_submitter.campaign_dedupe_key(
        args, dict(row), safe_case_id
    )


def _validate_task_provenance(
    record: Mapping[str, Any],
    plan_row: Mapping[str, str],
    policy: Mapping[str, Any],
) -> tuple[int, list[int]]:
    case_id = str(plan_row["case_id"])
    safe_case_id, expected_dedupe = _expected_dedupe(plan_row, policy)
    if record.get("dedupe_key") != expected_dedupe:
        raise RebuildError(f"forensic dedupe identity changed: {case_id}")
    selected_id = _positive_task_id(record.get("selected_task_id"), case_id)
    selected = record.get("task")
    if not isinstance(selected, dict) or set(selected) != set(forensic_audit.TASK_HASH_FIELDS):
        raise RebuildError(f"forensic selected task metadata shape changed: {case_id}")
    if record.get("task_metadata_canonical_sha256") != _canonical_sha(selected):
        raise RebuildError(f"forensic selected task fingerprint changed: {case_id}")
    exact = {
        "id": selected_id,
        "name": f"{policy['task_prefix']}-{safe_case_id}",
        "status": "completed",
        "project": policy["project"],
        "dedupe_key": expected_dedupe,
        "required_capability": policy["required_capability"],
        "env_profile": policy["env_profile"],
        "scheduling_profile": policy["scheduling_profile"],
        "max_workers_per_node": policy["max_workers_per_node"],
    }
    if any(selected.get(key) != value for key, value in exact.items()):
        raise RebuildError(f"forensic selected task identity changed: {case_id}")
    if _task_exit_code(selected, case_id) != 0 or not str(
        selected.get("remote_cwd") or ""
    ).strip():
        raise RebuildError(f"forensic selected task is not a successful remote solve: {case_id}")
    if selected.get("entrypoint") != "simulation1.sh":
        raise RebuildError(f"forensic selected task entrypoint changed: {case_id}")

    history = record.get("attempt_history")
    if not isinstance(history, list) or not history:
        raise RebuildError(f"forensic attempt history is absent: {case_id}")
    attempt_ids: list[int] = []
    attempt_tasks: list[dict[str, Any]] = []
    for index, item in enumerate(history):
        if not isinstance(item, dict) or set(item) != {
            "task",
            "task_metadata_canonical_sha256",
            "disposition",
        }:
            raise RebuildError(f"forensic attempt record shape changed: {case_id}")
        task = item["task"]
        if not isinstance(task, dict) or set(task) != set(forensic_audit.TASK_HASH_FIELDS):
            raise RebuildError(f"forensic attempt task shape changed: {case_id}")
        if item["task_metadata_canonical_sha256"] != _canonical_sha(task):
            raise RebuildError(f"forensic attempt task fingerprint changed: {case_id}")
        attempt_id = _positive_task_id(task.get("id"), f"{case_id} attempt")
        expected_disposition = (
            "selected_evidence" if index == len(history) - 1 else "excluded_failed_attempt"
        )
        if item.get("disposition") != expected_disposition:
            raise RebuildError(f"forensic attempt disposition changed: {case_id}")
        if task.get("name") != exact["name"] or task.get("project") != policy["project"]:
            raise RebuildError(f"forensic attempt task identity changed: {case_id}")
        if task.get("dedupe_key") != expected_dedupe:
            raise RebuildError(f"forensic attempt dedupe changed: {case_id}")
        attempt_ids.append(attempt_id)
        attempt_tasks.append(dict(task))
    if attempt_ids != sorted(set(attempt_ids)) or attempt_ids[-1] != selected_id:
        raise RebuildError(f"forensic latest-attempt ordering changed: {case_id}")
    if attempt_tasks[-1] != selected:
        raise RebuildError(f"forensic selected task differs from latest attempt: {case_id}")
    for task in attempt_tasks[:-1]:
        status = str(task.get("status") or "").lower()
        if status in {"queued", "attaching", "running"}:
            raise RebuildError(f"forensic excluded attempt is still active: {case_id}")
        if status == "completed" and _task_exit_code(task, case_id) == 0:
            raise RebuildError(f"forensic excluded attempt was also successful: {case_id}")
    excluded = attempt_ids[:-1]
    if record.get("excluded_task_ids") != excluded:
        raise RebuildError(f"forensic excluded-attempt binding changed: {case_id}")
    if case_id == REPLAY_CASE_ID and not excluded:
        raise RebuildError("Stage1 suspect forensic history lost its excluded predecessor")
    return selected_id, attempt_ids


def load_forensic_evidence(
    receipt_path: Path,
    recovery: RecoveryEvidence,
) -> ForensicEvidence:
    payload, receipt = _load_json(receipt_path, "published forensic receipt", canonical=True)
    if receipt.get("schema_version") != forensic_audit.SCHEMA_VERSION or receipt.get("verified") is not True:
        raise RebuildError("published forensic receipt is not a verified v1 receipt")
    publication = receipt.get("publication")
    plan_binding = receipt.get("plan")
    raw_cases = receipt.get("cases")
    policy = receipt.get("execution_policy")
    parser = receipt.get("parser")
    scheduler = receipt.get("scheduler")
    if not isinstance(publication, dict) or not isinstance(plan_binding, dict) or not isinstance(raw_cases, list):
        raise RebuildError("published forensic receipt is incomplete")
    if policy != forensic_audit.EXPECTED_POLICY:
        raise RebuildError("forensic execution policy changed")
    if recovery.replay_manifest.get("execution_policy") != policy:
        raise RebuildError("forensic execution policy differs from the sealed replay manifest")
    if not isinstance(parser, dict) or not isinstance(scheduler, dict):
        raise RebuildError("forensic parser/scheduler provenance is absent")
    parser_path = Path(batch_runner.__file__).resolve()
    parser_payload = _stable_bytes(parser_path, "current torque parser source")
    parser_expected = {
        "path": "run_ipmsm_batch.py",
        "sha256": _sha256(parser_payload),
        "torque_unit_scale_function": "run_ipmsm_batch.unit_scale_to_base",
        "physics_gate_function": "run_ipmsm_batch.output_physics_issues",
    }
    if parser != parser_expected:
        raise RebuildError("forensic parser provenance changed")
    execution_sources = recovery.replay_manifest.get("execution_sources")
    parser_sources = [
        item
        for item in execution_sources
        if isinstance(item, dict)
        and str(item.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1]
        == "run_ipmsm_batch.py"
    ] if isinstance(execution_sources, list) else []
    if (
        len(parser_sources) != 1
        or parser_sources[0].get("sha256") != parser_expected["sha256"]
        or parser_sources[0].get("size") != len(parser_payload)
    ):
        raise RebuildError("sealed replay manifest parser source binding changed")
    scheduler_expected = {
        "url": forensic_audit.DEFAULT_SCHEDULER_URL,
        "access": "read_only_get",
        "remote_file_base": forensic_audit.REMOTE_FILE_BASE,
        "remote_file_fetches": 8,
    }
    if set(scheduler) != {
        "url",
        "access",
        "remote_file_base",
        "selected_task_ids",
        "attempt_task_ids",
        "remote_file_fetches",
    } or any(scheduler.get(key) != value for key, value in scheduler_expected.items()):
        raise RebuildError("forensic scheduler access provenance changed")
    output_dir = _path_from_record(publication, "output_dir", "forensic publication")
    bound_receipt = _path_from_record(publication, "receipt_path", "forensic publication")
    if not _same_path(bound_receipt, receipt_path):
        raise RebuildError("forensic receipt path differs from its publication binding")
    replay_binding = recovery.manifest["sealed_replay"]
    if plan_binding.get("sha256") != replay_binding.get("plan_sha256"):
        raise RebuildError("forensic receipt does not bind the sealed replay plan")
    if plan_binding.get("manifest_sha256") != replay_binding.get("manifest_sha256"):
        raise RebuildError("forensic receipt does not bind the sealed replay manifest")
    if plan_binding.get("rows") != 4 or plan_binding.get("columns") != EXPECTED_PLAN_COLUMNS:
        raise RebuildError("forensic receipt replay plan shape changed")
    if plan_binding.get("sha256") != recovery.replay_plan.sha256:
        raise RebuildError("forensic plan binding differs from replay plan bytes")

    records: dict[str, dict[str, Any]] = {}
    for value in raw_cases:
        if not isinstance(value, dict):
            raise RebuildError("forensic receipt contains a non-object case")
        case_id = str(value.get("case_id") or "")
        if not case_id or case_id in records:
            raise RebuildError("forensic receipt has blank or duplicate case identity")
        records[case_id] = value
    if set(records) != set(forensic_audit.REPLAY_CASE_IDS):
        raise RebuildError("forensic receipt does not contain the exact four replay cases")

    replay_rows = {row["case_id"]: row for row in recovery.replay_plan.rows}
    cases: dict[str, ForensicCase] = {}
    selected_task_ids: list[int] = []
    attempt_task_ids: list[int] = []
    for case_id in forensic_audit.REPLAY_CASE_IDS:
        record = records[case_id]
        plan_row = replay_rows.get(case_id)
        if plan_row is None:
            raise RebuildError(f"sealed replay plan lost case: {case_id}")
        selected_id, attempt_ids = _validate_task_provenance(record, plan_row, policy)
        selected_task_ids.append(selected_id)
        attempt_task_ids.extend(attempt_ids)
        result_record = record.get("result")
        raw_record = record.get("raw_torque")
        gate_record = record.get("apparent_power_gate")
        if not all(isinstance(value, dict) for value in (result_record, raw_record, gate_record)):
            raise RebuildError(f"forensic case is incomplete: {case_id}")
        result_path = _contained_file(
            _path_from_record(result_record, "local_path", f"{case_id} result"),
            output_dir,
            f"{case_id} result",
        )
        raw_path = _contained_file(
            _path_from_record(raw_record, "local_path", f"{case_id} raw torque"),
            output_dir,
            f"{case_id} raw torque",
        )
        result_payload = _stable_bytes(result_path, f"{case_id} forensic result")
        raw_payload = _stable_bytes(raw_path, f"{case_id} raw torque")
        if result_record.get("sha256") != _sha256(result_payload) or result_record.get("bytes") != len(result_payload):
            raise RebuildError(f"forensic result binding changed: {case_id}")
        if raw_record.get("sha256") != _sha256(raw_payload) or raw_record.get("bytes") != len(raw_payload):
            raise RebuildError(f"forensic raw-torque binding changed: {case_id}")
        result = _decode_csv(
            result_path,
            result_payload,
            f"{case_id} forensic result",
            expected_rows=1,
            expected_columns=EXPECTED_RESULT_COLUMNS,
        )
        row = dict(result.rows[0])
        if row.get("case_id") != case_id or str(row.get("status") or "").lower() != "ok":
            raise RebuildError(f"forensic result identity/status changed: {case_id}")
        if result_record.get("header_canonical_sha256") != _canonical_sha(list(result.fieldnames)):
            raise RebuildError(f"forensic result header hash changed: {case_id}")
        if result_record.get("row_canonical_sha256") != _canonical_sha(row):
            raise RebuildError(f"forensic result row hash changed: {case_id}")
        if record.get("plan_row_canonical_sha256") != _canonical_sha(plan_row):
            raise RebuildError(f"forensic plan-row fingerprint changed: {case_id}")
        if record.get("design_hash") != plan_row.get("design_hash"):
            raise RebuildError(f"forensic plan/result design binding changed: {case_id}")
        try:
            raw_summary = forensic_audit.parse_torque_raw(
                raw_payload,
                period_s=float(row["output_period_s"]),
                stop_s=float(row["output_stop_time_s"]),
            )
            gate = forensic_audit.apparent_power_gate(row)
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            raise RebuildError(f"forensic physics replay failed: {case_id}: {exc}") from exc
        _compare_summary_fields(raw_record, raw_summary, f"{case_id} raw torque")
        raw_torque = float(raw_summary["normalized_last_avg_nm"])
        try:
            result_torque = float(row["output_torque_last_avg_nm"])
            rpm = float(row["input_base_rpm"])
            result_mech_power = float(row["output_mech_power_last_w"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RebuildError(f"forensic result power fields are invalid: {case_id}") from exc
        if not math.isclose(raw_torque, result_torque, rel_tol=1e-10, abs_tol=1e-12):
            raise RebuildError(f"raw normalized torque differs from result torque: {case_id}")
        raw_mech_power = raw_torque * rpm * 2.0 * math.pi / 60.0
        if not math.isclose(
            raw_mech_power, result_mech_power, rel_tol=1e-10, abs_tol=1e-9
        ):
            raise RebuildError(f"raw-derived mechanical power differs from result: {case_id}")
        if raw_record.get("matches_result_torque") is not True or raw_record.get(
            "matches_result_mechanical_power"
        ) is not True:
            raise RebuildError(f"forensic raw/result match assertions changed: {case_id}")
        _compare_summary_fields(
            raw_record,
            {"recomputed_mechanical_power_w": raw_mech_power},
            f"{case_id} raw mechanical power",
        )
        if _canonical_sha(gate_record) != _canonical_sha(gate):
            raise RebuildError(f"forensic apparent-power gate changed: {case_id}")
        cases[case_id] = ForensicCase(
            record=dict(record),
            result_path=result_path,
            result_payload=result_payload,
            fieldnames=result.fieldnames,
            row=row,
            raw_path=raw_path,
            raw_payload=raw_payload,
        )

    if scheduler.get("selected_task_ids") != selected_task_ids:
        raise RebuildError("forensic scheduler selected-task list changed")
    if scheduler.get("attempt_task_ids") != attempt_task_ids:
        raise RebuildError("forensic scheduler attempt-task list changed")

    suspect = cases[REPLAY_CASE_ID]
    source_rows = {row["case_id"]: row for row in recovery.source_plan.rows}
    mapping = suspect.record.get("replacement_mapping_inputs")
    if not isinstance(mapping, dict):
        raise RebuildError("Stage1 suspect forensic mapping is absent")
    expected_mapping = {
        "official_case_id": SOURCE_CASE_ID,
        "official_geometry_group_id": source_rows[SOURCE_CASE_ID]["geometry_group_id"],
        "replay_case_id": REPLAY_CASE_ID,
        "source_plan_sha256": recovery.source_plan.sha256,
        "source_row_canonical_sha256": recovery.replacement[
            "source_row_canonical_sha256"
        ],
        "replay_plan_sha256": recovery.manifest["sealed_replay"]["plan_sha256"],
        "remap_performed": False,
    }
    if any(mapping.get(key) != value for key, value in expected_mapping.items()):
        raise RebuildError("Stage1 suspect forensic replacement mapping changed")
    if suspect.record.get("source_case_id") != SOURCE_CASE_ID or suspect.record.get("role") != "suspect":
        raise RebuildError("Stage1 suspect forensic role/source changed")
    return ForensicEvidence(
        output_dir=output_dir,
        receipt_path=receipt_path,
        receipt_payload=payload,
        receipt_sha256=_sha256(payload),
        receipt=receipt,
        cases=cases,
    )


def _completion_result_binding(path: Path) -> dict[str, Any]:
    payload, document = _load_json(path, "v4r3 Stage1 completion", canonical=True)
    del payload
    if set(document) != {"payload", "payload_sha256", "schema_version"}:
        raise RebuildError("v4r3 Stage1 completion envelope fields changed")
    if document.get("schema_version") != "ipmsm-v2-stage1-official-completion-v4":
        raise RebuildError("v4r3 Stage1 completion schema changed")
    body = document.get("payload")
    if not isinstance(body, dict) or document.get("payload_sha256") != _sha256(_canonical_bytes(body)):
        raise RebuildError("v4r3 Stage1 completion payload hash changed")
    binding = body.get("stage1_result")
    if not isinstance(binding, dict):
        raise RebuildError("v4r3 Stage1 completion has no result binding")
    return dict(binding)


def _case_ids(rows: Iterable[Mapping[str, str]], label: str) -> list[str]:
    ids = [str(row.get("case_id") or "").strip() for row in rows]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise RebuildError(f"{label} has blank or duplicate case IDs")
    return ids


def _csv_payload(fieldnames: Iterable[str], rows: Iterable[Mapping[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fieldnames), lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def verify_original_collection(
    root: Path,
    recovery: RecoveryEvidence,
    completion_path: Path,
) -> OriginalCollection:
    if not root.is_dir() or root.is_symlink():
        raise RebuildError(f"original Stage1 collection is absent or unsafe: {root}")
    if {item.name for item in root.iterdir()} != {"selected_cases.csv", "merged_results.csv", "results"}:
        raise RebuildError("original Stage1 collection has unexpected top-level entries")
    selected = _read_csv(
        root / "selected_cases.csv",
        "original selected_cases.csv",
        expected_rows=EXPECTED_ROWS,
        expected_columns=EXPECTED_PLAN_COLUMNS,
    )
    if selected.payload != recovery.source_plan.payload:
        raise RebuildError("original selected_cases.csv differs from the recovery source plan")
    merged = _read_csv(
        root / "merged_results.csv",
        "original merged_results.csv",
        expected_rows=EXPECTED_ROWS,
        expected_columns=EXPECTED_RESULT_COLUMNS,
    )
    completion = _completion_result_binding(completion_path)
    if completion.get("sha256") != merged.sha256 or completion.get("size") != len(merged.payload):
        raise RebuildError("original merged result differs from v4r3 completion authority")

    plan_ids = _case_ids(selected.rows, "original selected plan")
    merged_ids = _case_ids(merged.rows, "original merged result")
    if merged_ids != plan_ids:
        raise RebuildError("original merged result is not in selected-plan order")
    results_dir = root / "results"
    if not results_dir.is_dir() or results_dir.is_symlink():
        raise RebuildError("original results directory is absent or unsafe")
    expected_names = {f"{case_id}.csv" for case_id in plan_ids}
    entries = list(results_dir.iterdir())
    if {item.name for item in entries} != expected_names or any(
        item.is_symlink() or not item.is_file() for item in entries
    ):
        raise RebuildError("original per-case result inventory changed")

    plan_by_id = {row["case_id"]: row for row in selected.rows}
    merged_by_id = {row["case_id"]: row for row in merged.rows}
    records: dict[str, OriginalResult] = {}
    inventory: list[dict[str, Any]] = []
    total_bytes = 0
    for case_id in plan_ids:
        path = results_dir / f"{case_id}.csv"
        result = _read_csv(
            path,
            f"original result {case_id}",
            expected_rows=1,
            expected_columns=EXPECTED_RESULT_COLUMNS,
        )
        row = dict(result.rows[0])
        if result.fieldnames != merged.fieldnames or row != merged_by_id[case_id]:
            raise RebuildError(f"original per-case/merged row mismatch: {case_id}")
        if row.get("case_id") != case_id or str(row.get("status") or "").lower() != "ok":
            raise RebuildError(f"original result identity/status changed: {case_id}")
        plan_row = plan_by_id[case_id]
        for column in ("geometry_group_id", "design_hash", "doe_split", "operating_point_id"):
            if row.get(column) != plan_row.get(column):
                raise RebuildError(f"original result/plan {column} mismatch: {case_id}")
        try:
            collector.validate_result_matches_plan(plan_row, row)
        except RuntimeError as exc:
            raise RebuildError(str(exc)) from exc
        record = OriginalResult(case_id, path, result.sha256, len(result.payload))
        records[case_id] = record
        inventory.append(
            {"case_id": case_id, "path": path.as_posix(), "sha256": result.sha256, "bytes": len(result.payload)}
        )
        total_bytes += len(result.payload)
    if _csv_payload(merged.fieldnames, [merged_by_id[case_id] for case_id in plan_ids]) != merged.payload:
        raise RebuildError("original merged result bytes are not the deterministic plan-order merge")
    try:
        collector.validate_homogeneous_fingerprints(merged.rows)
    except RuntimeError as exc:
        raise RebuildError(str(exc)) from exc
    summary = dataset_validator.validate_rows(merged.rows, fieldnames=merged.fieldnames)
    if summary.issue_counts != {"apparent_power_bound": 1} or summary.failures != 1:
        raise RebuildError(
            "original Stage1 collection no longer has exactly the quarantined torque-unit issue"
        )
    suspect_summary = dataset_validator.validate_rows(
        [merged_by_id[SOURCE_CASE_ID]], fieldnames=merged.fieldnames
    )
    if suspect_summary.issue_counts != {"apparent_power_bound": 1}:
        raise RebuildError("the known Stage1 suspect is not the sole apparent-power violation")
    return OriginalCollection(
        root=root,
        selected=selected,
        merged=merged,
        results=records,
        inventory_sha256=_canonical_sha(inventory),
        total_result_bytes=total_bytes,
        validation=dict(summary.as_row()),
    )


def remap_forensic_suspect(
    case: ForensicCase,
    recovery: RecoveryEvidence,
    expected_header: tuple[str, ...],
) -> tuple[bytes, dict[str, str], dict[str, Any]]:
    if case.fieldnames != expected_header:
        raise RebuildError("forensic suspect result schema/order differs from Stage1")
    source_by_id = {row["case_id"]: row for row in recovery.source_plan.rows}
    revised_by_id = {row["case_id"]: row for row in recovery.plan.rows}
    source_plan_row = source_by_id[SOURCE_CASE_ID]
    revised_plan_row = revised_by_id[REVISED_CASE_ID]
    row = dict(case.row)
    row["case_id"] = REVISED_CASE_ID
    row["geometry_group_id"] = source_plan_row["geometry_group_id"]
    changed = [column for column in expected_header if row[column] != case.row[column]]
    if changed != ["case_id", "geometry_group_id"]:
        raise RebuildError("forensic suspect remap changed fields beyond identity")
    for column in ("geometry_group_id", "design_hash", "doe_split", "operating_point_id"):
        if row.get(column) != revised_plan_row.get(column):
            raise RebuildError(f"remapped suspect differs from revised plan: {column}")
    try:
        collector.validate_result_matches_plan(revised_plan_row, row)
    except RuntimeError as exc:
        raise RebuildError(str(exc)) from exc
    unchanged_pairs = [(column, case.row[column]) for column in expected_header if column not in changed]
    numeric_pairs = []
    for column in expected_header:
        try:
            float(case.row[column])
        except (TypeError, ValueError):
            continue
        numeric_pairs.append((column, case.row[column]))
    payload = _csv_payload(expected_header, [row])
    check = _decode_csv(
        Path(REVISED_CASE_ID + ".csv"),
        payload,
        "remapped forensic suspect",
        expected_rows=1,
        expected_columns=EXPECTED_RESULT_COLUMNS,
    )
    if dict(check.rows[0]) != row:
        raise RebuildError("remapped suspect serialization changed field values")
    return payload, row, {
        "source_case_id": SOURCE_CASE_ID,
        "replay_case_id": REPLAY_CASE_ID,
        "revised_case_id": REVISED_CASE_ID,
        "source_geometry_group_id": source_plan_row["geometry_group_id"],
        "replay_geometry_group_id": case.row["geometry_group_id"],
        "changed_fields": changed,
        "unchanged_field_count": len(unchanged_pairs),
        "unchanged_payload_canonical_sha256": _canonical_sha(unchanged_pairs),
        "numeric_field_count": len(numeric_pairs),
        "numeric_payload_canonical_sha256": _canonical_sha(numeric_pairs),
        "replay_row_canonical_sha256": _canonical_sha(case.row),
        "revised_row_canonical_sha256": _canonical_sha(row),
        "replay_result_sha256": case.record["result"]["sha256"],
        "revised_result_sha256": _sha256(payload),
        "selected_task_id": case.record["selected_task_id"],
    }


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise RebuildError(f"refusing to replace staged artifact: {path}") from exc


def _copy_exact(source: OriginalResult, destination: Path) -> str:
    payload = _stable_bytes(source.path, f"source result {source.case_id}")
    if _sha256(payload) != source.sha256 or len(payload) != source.size:
        raise RebuildError(f"source result changed before materialization: {source.case_id}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_exclusive(destination, payload)
    if _stable_bytes(destination, f"materialized result {source.case_id}") != payload:
        raise RebuildError(f"materialized result is not byte-identical: {source.case_id}")
    try:
        if os.path.samefile(source.path, destination):
            raise RebuildError(f"materialized result is not an independent copy: {source.case_id}")
    except OSError as exc:
        raise RebuildError(f"cannot prove independent result copy: {source.case_id}") from exc
    return "copy"


def _validator_source_binding() -> dict[str, Any]:
    path = Path(dataset_validator.__file__).resolve()
    payload = _stable_bytes(path, "dataset validator source")
    return {"path": path.as_posix(), "sha256": _sha256(payload), "bytes": len(payload)}


def _materialization_method(source: OriginalResult, destination: Path) -> str:
    try:
        if os.path.samefile(source.path, destination):
            raise RebuildError(
                f"rebuilt result aliases the original instead of copying: {source.case_id}"
            )
        return "copy"
    except OSError as exc:
        raise RebuildError(
            f"cannot compare materialized result identity: {source.case_id}"
        ) from exc


def _validation_evidence(merged_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="ipmsm-stage1-rebuild-validation-") as temporary:
        summary_path = Path(temporary) / "validation_summary.csv"
        captured = io.StringIO()
        with redirect_stdout(captured):
            validator_code = dataset_validator.main(
                ["--data", str(merged_path), "--summary", str(summary_path)]
            )
        if validator_code != 0:
            raise RebuildError("existing exact dataset validator rejected the rebuilt collection")
        summary = _read_csv(
            summary_path,
            "rebuilt validation scratch summary",
            expected_rows=1,
        )
        summary_row = dict(summary.rows[0])
        if (
            summary_row.get("status") != "pass"
            or summary_row.get("failures") != "0"
            or summary_row.get("rows") != "700"
        ):
            raise RebuildError("rebuilt validation summary is not an exact 700-row pass")
        summary_binding = {
            "published": False,
            "sha256": summary.sha256,
            "bytes": len(summary.payload),
            "row": summary_row,
        }
        validator_binding = {
            **_validator_source_binding(),
            "entrypoint": "validate_ipmsm_v2_dataset.main",
            "exit_code": validator_code,
            "stdout": captured.getvalue().strip(),
        }
    return summary_binding, validator_binding


def audit_rebuilt_collection(
    collection: Path,
    final_output: Path,
    recovery: RecoveryEvidence,
    forensic: ForensicEvidence,
    original: OriginalCollection,
    receipt_output: Path,
) -> tuple[dict[str, Any], bytes]:
    if collection.is_symlink() or not collection.is_dir():
        raise RebuildError(f"rebuilt collection is absent or unsafe: {collection}")
    if {item.name for item in collection.iterdir()} != {
        "selected_cases.csv",
        "merged_results.csv",
        "results",
    }:
        raise RebuildError("rebuilt collection must contain exactly the three collector entries")
    selected = _read_csv(
        collection / "selected_cases.csv",
        "rebuilt selected_cases.csv",
        expected_rows=EXPECTED_ROWS,
        expected_columns=EXPECTED_PLAN_COLUMNS,
    )
    if selected.payload != recovery.plan.payload:
        raise RebuildError("rebuilt selected_cases.csv differs from the recovery plan")
    merged = _read_csv(
        collection / "merged_results.csv",
        "rebuilt merged_results.csv",
        expected_rows=EXPECTED_ROWS,
        expected_columns=EXPECTED_RESULT_COLUMNS,
    )
    suspect_payload, suspect_row, remap = remap_forensic_suspect(
        forensic.cases[REPLAY_CASE_ID], recovery, original.merged.fieldnames
    )
    plan_ids = _case_ids(recovery.plan.rows, "revised Stage1 plan")
    if [row["case_id"] for row in merged.rows] != plan_ids:
        raise RebuildError("rebuilt merged result is not in revised-plan order")
    results_dir = collection / "results"
    if results_dir.is_symlink() or not results_dir.is_dir():
        raise RebuildError("rebuilt results directory is absent or unsafe")
    expected_names = {f"{case_id}.csv" for case_id in plan_ids}
    entries = list(results_dir.iterdir())
    if {item.name for item in entries} != expected_names or any(
        item.is_symlink() or not item.is_file() for item in entries
    ):
        raise RebuildError("rebuilt per-case result inventory changed")

    methods = {"copy": 0}
    inventory: list[dict[str, Any]] = []
    merged_by_id = {row["case_id"]: row for row in merged.rows}
    ordered_rows: list[dict[str, str]] = []
    for case_id in plan_ids:
        destination = results_dir / f"{case_id}.csv"
        result = _read_csv(
            destination,
            f"rebuilt result {case_id}",
            expected_rows=1,
            expected_columns=EXPECTED_RESULT_COLUMNS,
        )
        row = dict(result.rows[0])
        if result.fieldnames != original.merged.fieldnames or row != merged_by_id[case_id]:
            raise RebuildError(f"rebuilt per-case/merged row mismatch: {case_id}")
        if case_id == REVISED_CASE_ID:
            if result.payload != suspect_payload or row != suspect_row:
                raise RebuildError("rebuilt suspect differs from the verified forensic remap")
            origin = "verified_forensic_replay_remap"
        else:
            source = original.results.get(case_id)
            if source is None:
                raise RebuildError(f"revised plan lost unchanged source result: {case_id}")
            if result.sha256 != source.sha256 or len(result.payload) != source.size:
                raise RebuildError(f"rebuilt unchanged result bytes differ: {case_id}")
            method = _materialization_method(source, destination)
            methods[method] += 1
            origin = method + "_byte_identical"
        ordered_rows.append(row)
        inventory.append(
            {
                "case_id": case_id,
                "path": (final_output / "results" / destination.name).as_posix(),
                "sha256": result.sha256,
                "bytes": len(result.payload),
                "origin": origin,
            }
        )
    if sum(methods.values()) != EXPECTED_ROWS - 1:
        raise RebuildError("rebuild did not preserve exactly 699 original results")
    if _csv_payload(merged.fieldnames, ordered_rows) != merged.payload:
        raise RebuildError("rebuilt merged result bytes are not the deterministic plan-order merge")
    summary_binding, validator_binding = _validation_evidence(
        collection / "merged_results.csv"
    )

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "verified": True,
        "publication": {
            "mode": "fresh_directory_then_atomic_receipt_no_replace",
            "output_collection": final_output.as_posix(),
            "receipt_path": receipt_output.as_posix(),
        },
        "recovery": {
            "plan_path": recovery.plan.path.as_posix(),
            "plan_sha256": recovery.plan.sha256,
            "manifest_path": recovery.manifest_path.as_posix(),
            "manifest_sha256": recovery.manifest_sha256,
            "source_plan_sha256": recovery.source_plan.sha256,
        },
        "forensics": {
            "receipt_path": forensic.receipt_path.as_posix(),
            "receipt_sha256": forensic.receipt_sha256,
            "replay_result_path": forensic.cases[REPLAY_CASE_ID].result_path.as_posix(),
            "replay_result_sha256": _sha256(
                forensic.cases[REPLAY_CASE_ID].result_payload
            ),
            "raw_torque_path": forensic.cases[REPLAY_CASE_ID].raw_path.as_posix(),
            "raw_torque_sha256": _sha256(forensic.cases[REPLAY_CASE_ID].raw_payload),
        },
        "original_collection": {
            "path": original.root.as_posix(),
            "selected_cases_sha256": original.selected.sha256,
            "merged_results_sha256": original.merged.sha256,
            "result_files": len(original.results),
            "result_bytes": original.total_result_bytes,
            "result_inventory_canonical_sha256": original.inventory_sha256,
            "known_validation": original.validation,
            "excluded_case_id": SOURCE_CASE_ID,
        },
        "remap": remap,
        "rebuilt_collection": {
            "rows": EXPECTED_ROWS,
            "columns": EXPECTED_RESULT_COLUMNS,
            "selected_cases": {
                "path": (final_output / "selected_cases.csv").as_posix(),
                "sha256": recovery.plan.sha256,
                "bytes": len(recovery.plan.payload),
            },
            "merged_results": {
                "path": (final_output / "merged_results.csv").as_posix(),
                "sha256": merged.sha256,
                "bytes": len(merged.payload),
            },
            "validation_summary": summary_binding,
            "result_files": len(inventory),
            "unchanged_original_results": sum(methods.values()),
            "materialization": methods,
            "result_inventory_canonical_sha256": _canonical_sha(inventory),
        },
        "validator": validator_binding,
    }
    return receipt, _canonical_bytes(receipt)


def build_staged_collection(
    stage: Path,
    final_output: Path,
    recovery: RecoveryEvidence,
    forensic: ForensicEvidence,
    original: OriginalCollection,
    receipt_output: Path,
) -> tuple[dict[str, Any], bytes]:
    stage.mkdir(parents=False, exist_ok=False)
    selected_path = stage / "selected_cases.csv"
    results_dir = stage / "results"
    results_dir.mkdir()
    _write_exclusive(selected_path, recovery.plan.payload)
    suspect_payload, _, _ = remap_forensic_suspect(
        forensic.cases[REPLAY_CASE_ID], recovery, original.merged.fieldnames
    )
    result_paths: list[Path] = []
    for case_id in _case_ids(recovery.plan.rows, "revised Stage1 plan"):
        destination = results_dir / f"{case_id}.csv"
        if case_id == REVISED_CASE_ID:
            _write_exclusive(destination, suspect_payload)
        else:
            source = original.results.get(case_id)
            if source is None:
                raise RebuildError(f"revised plan lost unchanged source result: {case_id}")
            _copy_exact(source, destination)
        result_paths.append(destination)
    try:
        headers, rows = merger.merge_complete_results(selected_path, result_paths)
        merged_path = stage / "merged_results.csv"
        merger.write_csv(merged_path, headers, rows)
    except (OSError, ValueError) as exc:
        raise RebuildError(f"cannot build revised merged result: {exc}") from exc
    merged_readback = _read_csv(
        merged_path,
        "staged merged result readback",
        expected_rows=EXPECTED_ROWS,
        expected_columns=EXPECTED_RESULT_COLUMNS,
    )
    if merged_readback.fieldnames != tuple(headers) or merged_readback.rows != tuple(rows):
        raise RebuildError("staged merged readback differs from all 700 per-case rows")
    return audit_rebuilt_collection(
        stage,
        final_output,
        recovery,
        forensic,
        original,
        receipt_output,
    )


def _proof_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.publish-proof.json")


def _directory_identity(path: Path) -> tuple[int, int]:
    info = os.stat(path, follow_symlinks=False)
    return int(info.st_dev), int(info.st_ino)


def _directory_tree_sha256(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise RebuildError(f"collection tree root is absent or unsafe: {root}")
    records: list[dict[str, Any]] = []
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            path = current_path / name
            if path.is_symlink() or not path.is_dir():
                raise RebuildError(f"collection tree has an unsafe directory: {path}")
            records.append(
                {"path": path.relative_to(root).as_posix(), "type": "directory"}
            )
        for name in file_names:
            path = current_path / name
            payload = _stable_bytes(path, "collection tree file")
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "type": "file",
                    "sha256": _sha256(payload),
                    "bytes": len(payload),
                }
            )
    return _canonical_sha(records)


def _rollback_claimed_collection(path: Path, claim: DirectoryClaim) -> bool:
    try:
        if (
            path.is_symlink()
            or not path.is_dir()
            or _directory_identity(path) != claim.identity
            or _directory_tree_sha256(path) != claim.tree_canonical_sha256
        ):
            return False
        shutil.rmtree(path)
        return not os.path.lexists(path)
    except (OSError, RebuildError):
        return False


def _rename_directory_no_replace_once(stage: Path, output: Path) -> None:
    if os.name == "nt":
        # Windows os.rename is atomic and never replaces an existing directory.
        os.rename(stage, output)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace directory rename is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(stage.absolute()),
        at_fdcwd,
        os.fsencode(output.absolute()),
        rename_noreplace,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(output))


def _audit_directory_rename_result(
    stage: Path,
    output: Path,
    *,
    expected_identity: tuple[int, int],
    expected_tree_sha256: str,
) -> DirectoryClaim:
    if os.path.lexists(stage) or not os.path.lexists(output):
        raise RebuildError("atomic directory rename did not expose exactly one pathname")
    if output.is_symlink() or not output.is_dir():
        raise RebuildError("atomic directory rename produced an unsafe output")
    observed_identity = _directory_identity(output)
    observed_tree_sha256 = _directory_tree_sha256(output)
    if observed_identity != expected_identity or observed_tree_sha256 != expected_tree_sha256:
        raise RebuildError("atomic directory rename output differs from staged identity/content")
    return DirectoryClaim(observed_identity, observed_tree_sha256)


def _claim_collection(stage: Path, output: Path) -> DirectoryClaim:
    if os.path.lexists(output):
        raise RebuildError(f"refusing to replace existing output collection: {output}")
    if not _same_path(stage.parent, output.parent):
        raise RebuildError("collection staging and output must share one parent directory")
    if stage.is_symlink() or not stage.is_dir():
        raise RebuildError("collection staging directory is absent or unsafe")
    expected_identity = _directory_identity(stage)
    expected_tree_sha256 = _directory_tree_sha256(stage)
    delays = (*DIRECTORY_RENAME_RETRY_DELAYS_SECONDS, None)
    last_error: OSError | None = None
    for delay in delays:
        try:
            _rename_directory_no_replace_once(stage, output)
        except OSError as exc:
            last_error = exc
            stage_exists = os.path.lexists(stage)
            output_exists = os.path.lexists(output)
            if not stage_exists and output_exists:
                return _audit_directory_rename_result(
                    stage,
                    output,
                    expected_identity=expected_identity,
                    expected_tree_sha256=expected_tree_sha256,
                )
            if stage_exists and not output_exists and delay is not None:
                time.sleep(delay)
                continue
            if stage_exists and output_exists:
                raise RebuildError(
                    "directory rename collided with an existing unowned output; both were preserved"
                ) from exc
            raise RebuildError("atomic no-replace directory rename failed ambiguously") from exc
        return _audit_directory_rename_result(
            stage,
            output,
            expected_identity=expected_identity,
            expected_tree_sha256=expected_tree_sha256,
        )
    raise RebuildError("atomic no-replace directory rename retries were exhausted") from last_error


def _publish_receipt(path: Path, payload: bytes):
    if os.path.lexists(path):
        raise RebuildError(f"refusing to replace existing rebuild receipt: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".staged", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return atomic_publish.publish_no_replace(staged, path, proof_path=_proof_path(path))
    finally:
        staged.unlink(missing_ok=True)


def _result_summary(
    receipt: Mapping[str, Any],
    receipt_payload: bytes,
    receipt_output: Path,
    *,
    publish: bool,
    publication: str,
) -> dict[str, Any]:
    rebuilt = receipt["rebuilt_collection"]
    return {
        "mode": "publish" if publish else "dry-run",
        "status": "verified",
        "publication": publication,
        "rows": rebuilt["rows"],
        "unchanged": rebuilt["unchanged_original_results"],
        "remapped": 1,
        "validator_failures": rebuilt["validation_summary"]["row"]["failures"],
        "receipt_sha256": _sha256(receipt_payload),
        "receipt_path": receipt_output.as_posix(),
    }


def _ensure_external_receipt(output_collection: Path, receipt_output: Path) -> None:
    output = output_collection.resolve(strict=False)
    receipt = receipt_output.resolve(strict=False)
    try:
        receipt.relative_to(output)
    except ValueError:
        return
    raise RebuildError("rebuild receipt must be external to the exact-entry collection")


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _same_existing_object(first: Path, second: Path) -> bool:
    if not os.path.lexists(first) or not os.path.lexists(second):
        return False
    try:
        return os.path.samefile(first, second)
    except OSError as exc:
        raise RebuildError(f"cannot compare path aliases: {first} and {second}") from exc


def _guard_rebuild_scope(
    *,
    output_collection: Path,
    receipt_output: Path,
    recovery: RecoveryEvidence,
    forensics: ForensicEvidence,
    original_collection: Path,
    completion_path: Path,
) -> None:
    _ensure_external_receipt(output_collection, receipt_output)
    protected_directories = [original_collection, forensics.output_dir]
    for candidate, label in (
        (output_collection, "output collection"),
        (receipt_output, "receipt output"),
    ):
        for protected in protected_directories:
            if _within(candidate, protected) or _within(protected, candidate):
                raise RebuildError(f"{label} overlaps protected input directory: {protected}")
            if _same_existing_object(candidate, protected):
                raise RebuildError(f"{label} aliases protected input directory: {protected}")

    revised_stage2 = Path(recovery.manifest["revised_plans"]["stage2"]["path"])
    replay_manifest = Path(recovery.manifest["sealed_replay"]["manifest_path"])
    input_files = [
        recovery.plan.path,
        recovery.manifest_path,
        recovery.source_plan.path,
        recovery.replay_plan.path,
        revised_stage2,
        replay_manifest,
        forensics.receipt_path,
        completion_path,
        *(
            path
            for case in forensics.cases.values()
            for path in (case.result_path, case.raw_path)
        ),
    ]
    for input_path in input_files:
        if (
            _same_path(output_collection, input_path)
            or _within(input_path, output_collection)
            or _within(output_collection, input_path)
            or _same_existing_object(output_collection, input_path)
        ):
            raise RebuildError(f"output collection overlaps recovery/input artifact: {input_path}")
        if _same_path(receipt_output, input_path) or _same_existing_object(
            receipt_output, input_path
        ):
            raise RebuildError(f"receipt output aliases recovery/input artifact: {input_path}")


def rebuild_stage1(
    *,
    recovery_plan: Path,
    recovery_manifest: Path,
    forensic_receipt: Path,
    original_collection: Path,
    stage1_completion: Path,
    output_collection: Path,
    receipt_output: Path,
    publish: bool,
) -> dict[str, Any]:
    output_exists = os.path.lexists(output_collection)
    receipt_exists = os.path.lexists(receipt_output)
    proof_path = _proof_path(receipt_output)
    proof_exists = os.path.lexists(proof_path)
    if (receipt_exists or proof_exists) and not output_exists:
        raise RebuildError(
            "rebuild receipt/proof exists without its bound output collection"
        )
    recovery = load_recovery_evidence(recovery_plan, recovery_manifest)
    forensics = load_forensic_evidence(forensic_receipt, recovery)
    _guard_rebuild_scope(
        output_collection=output_collection,
        receipt_output=receipt_output,
        recovery=recovery,
        forensics=forensics,
        original_collection=original_collection,
        completion_path=stage1_completion,
    )
    original = verify_original_collection(original_collection, recovery, stage1_completion)

    if output_exists:
        receipt, receipt_payload = audit_rebuilt_collection(
            output_collection,
            output_collection,
            recovery,
            forensics,
            original,
            receipt_output,
        )
        if receipt_exists and _stable_bytes(
            receipt_output, "existing rebuild receipt"
        ) != receipt_payload:
            raise RebuildError("existing rebuild receipt differs from audited collection authority")
        if proof_exists and publish:
            if not atomic_publish.recover_owned_output(proof_path, receipt_output):
                raise RebuildError("cannot recover the late-success rebuild receipt proof")
            receipt_exists = os.path.lexists(receipt_output)
            proof_exists = os.path.lexists(proof_path)
            if receipt_exists or proof_exists:
                raise RebuildError("late-success receipt proof recovery left ambiguous artifacts")
        if receipt_exists:
            if _stable_bytes(receipt_output, "existing rebuild receipt") != receipt_payload:
                raise RebuildError("existing rebuild receipt differs from audited collection authority")
            publication = "existing_verified"
        elif publish:
            publication_receipt = None
            try:
                publication_receipt = _publish_receipt(receipt_output, receipt_payload)
                if _stable_bytes(receipt_output, "recovered rebuild receipt") != receipt_payload:
                    raise RebuildError("recovered rebuild receipt bytes changed")
                publication = "recovered_receipt"
            except BaseException:
                if publication_receipt is not None and not atomic_publish.rollback_owned_output(
                    publication_receipt
                ):
                    raise RebuildError(
                        "receipt recovery failed and receipt rollback was unsafe; "
                        "preexisting collection was preserved"
                    )
                raise
            finally:
                if publication_receipt is not None:
                    atomic_publish.cleanup_publish_receipt(publication_receipt)
        else:
            publication = "would_publish"
        return _result_summary(
            receipt,
            receipt_payload,
            receipt_output,
            publish=publish,
            publication=publication,
        )

    if publish:
        output_collection.parent.mkdir(parents=True, exist_ok=True)
        stage_parent: str | Path | None = output_collection.parent
    else:
        stage_parent = None
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_collection.name}.rebuild-staging-",
            dir=stage_parent,
        )
    )
    stage.rmdir()
    claimed_collection: DirectoryClaim | None = None
    publication_receipt = None
    try:
        receipt, receipt_payload = build_staged_collection(
            stage,
            output_collection,
            recovery,
            forensics,
            original,
            receipt_output,
        )
        if publish:
            claimed_collection = _claim_collection(stage, output_collection)
            claimed_receipt, claimed_payload = audit_rebuilt_collection(
                output_collection,
                output_collection,
                recovery,
                forensics,
                original,
                receipt_output,
            )
            if claimed_payload != receipt_payload or claimed_receipt != receipt:
                raise RebuildError("claimed collection differs from its staged authority")
            publication_receipt = _publish_receipt(receipt_output, receipt_payload)
            if _stable_bytes(receipt_output, "published rebuild receipt") != receipt_payload:
                raise RebuildError("published rebuild receipt bytes changed")
            publication = "published"
        else:
            publication = "would_publish"
        return _result_summary(
            receipt,
            receipt_payload,
            receipt_output,
            publish=publish,
            publication=publication,
        )
    except BaseException as exc:
        receipt_rollback_ok = True
        if publication_receipt is not None:
            receipt_rollback_ok = atomic_publish.rollback_owned_output(publication_receipt)
        collection_rollback_ok = True
        if claimed_collection is not None:
            collection_rollback_ok = _rollback_claimed_collection(
                output_collection, claimed_collection
            )
        if not receipt_rollback_ok or not collection_rollback_ok:
            raise RebuildError("rebuild failed and ownership-safe rollback was impossible") from exc
        raise
    finally:
        if publication_receipt is not None:
            atomic_publish.cleanup_publish_receipt(publication_receipt)
        shutil.rmtree(stage, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery-plan", type=Path, default=DEFAULT_RECOVERY_PLAN)
    parser.add_argument("--recovery-manifest", type=Path, default=DEFAULT_RECOVERY_MANIFEST)
    parser.add_argument("--forensic-receipt", type=Path, default=DEFAULT_FORENSIC_RECEIPT)
    parser.add_argument("--original-collection", type=Path, default=DEFAULT_ORIGINAL_COLLECTION)
    parser.add_argument("--stage1-completion", type=Path, default=DEFAULT_STAGE1_COMPLETION)
    parser.add_argument("--output-collection", type=Path, default=DEFAULT_OUTPUT_COLLECTION)
    parser.add_argument("--receipt-output", type=Path, default=DEFAULT_RECEIPT_OUTPUT)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish a fresh collection and atomic receipt; default is a no-output dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = rebuild_stage1(
        recovery_plan=args.recovery_plan,
        recovery_manifest=args.recovery_manifest,
        forensic_receipt=args.forensic_receipt,
        original_collection=args.original_collection,
        stage1_completion=args.stage1_completion,
        output_collection=args.output_collection,
        receipt_output=args.receipt_output,
        publish=args.publish,
    )
    print(
        "stage1_torqueunit_rebuild "
        + " ".join(
            f"{key}={result[key]}"
            for key in (
                "mode",
                "status",
                "publication",
                "rows",
                "unchanged",
                "remapped",
                "validator_failures",
                "receipt_sha256",
                "receipt_path",
            )
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RebuildError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
