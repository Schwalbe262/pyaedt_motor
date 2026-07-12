"""Fetch and seal the four retained AEDT torque-unit replay artifacts.

The scheduler is read-only in this workflow.  Exact plan-derived names and
dedupe keys resolve each case's latest attempt; failed predecessors are bound
and explicitly excluded.  The selected completed task's result CSV is then
fetched through the scheduler's safe ``remote-file`` API and used to discover
exactly one retained ``PPT_Torque.csv`` below the known simulation directory.

The default is a dry-run: it verifies remote evidence but writes nothing.
``--publish`` atomically publishes byte-preserving result/raw files and one
canonical receipt without replacing any existing local artifact.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, replace
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from urllib import parse, request

import atomic_publish
import collect_ipmsm_v2_campaign as collector
import run_ipmsm_batch as batch
import submit_ipmsm_v2_campaign as submitter


SCHEMA_VERSION = "ipmsm-torque-unit-forensic-receipt-v1"
DEFAULT_SCHEDULER_URL = "http://localhost:8000"
DEFAULT_PLAN = Path("simul_log_smoke/v4r4/torque_unit_replay_plan_sealed.csv")
DEFAULT_PLAN_MANIFEST = Path(
    "simul_log_smoke/v4r4/torque_unit_replay_plan_sealed.manifest.json"
)
DEFAULT_OUTPUT_DIR = Path("simul_log_smoke/v4r4/torque_unit_replay_forensics")
RECEIPT_NAME = "receipt.canonical.json"
REMOTE_FILE_BASE = "remote_cwd"
EXPECTED_RESULT_COLUMNS = 704

REPLAY_CASE_IDS = (
    "v2s1_0010_rated_torque_01_torqueunit_replay_v1",
    "v2s1_0010_rated_torque_03_torqueunit_replay_v1",
    "v2s2_0002_rated_torque_01_torqueunit_replay_v1",
    "v2s2_0002_rated_torque_03_torqueunit_replay_v1",
)

EXPECTED_POLICY = {
    "keep_projects": True,
    "task_prefix": "ipmsm-v2-torqueunit-replay-v1",
    "project": "PYAEDT_MOTOR_IPMSM_V2",
    "required_capability": "conda:pyaedt2026v1",
    "env_profile": "pyaedt2026v1",
    "scheduling_profile": "fea_bursty",
    "env_setup": "module load ansys-electronics/v252",
    "remote_cases_dir": "remote/ipmsm_v2_torqueunit_replay_v1_cases",
    "result_dir": "simul_log_scheduler/ipmsm_v2_torqueunit_replay_v1_results",
    "simulation_dir": "simulation/ipmsm_v2_torqueunit_replay_v1",
    "log_dir": "simul_log_scheduler/ipmsm_v2_torqueunit_replay_v1_logs",
    "max_workers_per_node": 1,
}

TASK_HASH_FIELDS = (
    "id",
    "name",
    "status",
    "state",
    "project",
    "dedupe_key",
    "remote_cwd",
    "remote_dir",
    "entrypoint",
    "required_capability",
    "env_profile",
    "scheduling_profile",
    "max_workers_per_node",
    "cpus",
    "memory_mb",
    "account_name",
    "allocation_id",
    "actual_node_name",
    "slurm_job_id",
    "exit_code",
    "failure_message",
    "created_at",
    "started_at",
    "finished_at",
)


TaskHistoryGetter = Callable[[str, str, float], list[dict[str, Any]]]
RemoteFetcher = Callable[[str, int, str, str, float], bytes]


@dataclass(frozen=True)
class PlanEvidence:
    path: Path
    payload: bytes
    sha256: str
    fieldnames: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    manifest_path: Path
    manifest_payload: bytes
    manifest_sha256: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ExpectedTask:
    task_id: int
    case_id: str
    safe_case_id: str
    task_name: str
    dedupe_key: str
    result_csv: str
    simulation_dir: str


ResolvedTask = tuple[ExpectedTask, dict[str, Any], tuple[dict[str, Any], ...]]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
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


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value)[:-1])


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _read_stable(path: Path, label: str) -> bytes:
    try:
        payload = path.read_bytes()
        if path.read_bytes() != payload:
            raise RuntimeError(f"{label} changed while it was read: {path}")
        return payload
    except OSError as exc:
        raise RuntimeError(f"cannot read {label}: {path}") from exc


def _decode_csv(payload: bytes, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fieldnames = list(reader.fieldnames or ())
        rows = [dict(row) for row in reader]
    except (UnicodeError, csv.Error) as exc:
        raise RuntimeError(f"{label} is not a valid UTF-8 CSV") from exc
    if not fieldnames or any(not str(name or "").strip() for name in fieldnames):
        raise RuntimeError(f"{label} has an invalid CSV header")
    if len(fieldnames) != len(set(fieldnames)):
        raise RuntimeError(f"{label} has duplicate CSV columns")
    return fieldnames, rows


def _load_manifest(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{label} is not canonicalizable JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _manifest_run_source(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    records = manifest.get("execution_sources")
    if not isinstance(records, list):
        raise RuntimeError("plan manifest has no execution_sources list")
    matching = [
        item
        for item in records
        if isinstance(item, dict)
        and str(item.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1]
        == "run_ipmsm_batch.py"
    ]
    if len(matching) != 1:
        raise RuntimeError("plan manifest must bind exactly one run_ipmsm_batch.py")
    return matching[0]


def load_plan_evidence(plan_path: Path, manifest_path: Path) -> PlanEvidence:
    plan_payload = _read_stable(plan_path, "sealed replay plan")
    manifest_payload = _read_stable(manifest_path, "sealed replay plan manifest")
    fieldnames, rows = _decode_csv(plan_payload, "sealed replay plan")
    manifest = _load_manifest(manifest_payload, "sealed replay plan manifest")
    if manifest.get("schema_version") != "ipmsm-torque-unit-replay-plan-v1":
        raise RuntimeError("sealed replay plan manifest schema_version changed")
    plan_sha256 = sha256_bytes(plan_payload)
    if str(manifest.get("plan_sha256") or "") != plan_sha256:
        raise RuntimeError("sealed replay plan hash does not match its manifest")
    if manifest.get("plan_rows") != len(rows):
        raise RuntimeError("sealed replay plan row count does not match its manifest")
    if manifest.get("plan_columns") != fieldnames:
        raise RuntimeError("sealed replay plan columns do not match its manifest")
    if len(rows) != len(REPLAY_CASE_IDS):
        raise RuntimeError(f"sealed replay plan must contain {len(REPLAY_CASE_IDS)} rows")
    ids = [str(row.get("case_id") or "").strip() for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(REPLAY_CASE_IDS):
        raise RuntimeError("sealed replay plan case IDs do not match the four pinned replay tasks")

    policy = manifest.get("execution_policy")
    if not isinstance(policy, dict):
        raise RuntimeError("sealed replay plan manifest has no execution_policy")
    policy_mismatches = [
        key for key, expected in EXPECTED_POLICY.items() if policy.get(key) != expected
    ]
    if policy_mismatches:
        raise RuntimeError(
            "sealed replay execution policy changed: " + ", ".join(policy_mismatches)
        )

    manifest_cases = manifest.get("cases")
    if not isinstance(manifest_cases, list):
        raise RuntimeError("sealed replay plan manifest has no cases list")
    by_id: dict[str, dict[str, Any]] = {}
    for record in manifest_cases:
        if not isinstance(record, dict):
            raise RuntimeError("sealed replay plan manifest contains a non-object case")
        case_id = str(record.get("replay_case_id") or "").strip()
        if not case_id or case_id in by_id:
            raise RuntimeError("sealed replay plan manifest has blank or duplicate case identity")
        by_id[case_id] = record
    if set(by_id) != set(ids):
        raise RuntimeError("sealed replay plan rows and manifest cases differ")
    for row in rows:
        case_id = str(row["case_id"]).strip()
        expected_hash = str(by_id[case_id].get("replay_row_canonical_sha256") or "")
        if canonical_sha256(row) != expected_hash:
            raise RuntimeError(f"sealed replay row hash mismatch: {case_id}")

    run_source = _manifest_run_source(manifest)
    parser_path = Path(batch.__file__).resolve()
    parser_payload = _read_stable(parser_path, "current run_ipmsm_batch.py")
    if str(run_source.get("sha256") or "") != sha256_bytes(parser_payload):
        raise RuntimeError(
            "current run_ipmsm_batch.py differs from the parser source sealed into the replay"
        )
    if run_source.get("size") != len(parser_payload):
        raise RuntimeError("sealed run_ipmsm_batch.py size does not match current parser source")
    return PlanEvidence(
        path=plan_path,
        payload=plan_payload,
        sha256=plan_sha256,
        fieldnames=tuple(fieldnames),
        rows=tuple(rows),
        manifest_path=manifest_path,
        manifest_payload=manifest_payload,
        manifest_sha256=sha256_bytes(manifest_payload),
        manifest=manifest,
    )


def expected_task_for_row(row: dict[str, str], policy: Mapping[str, Any]) -> ExpectedTask:
    case_id = str(row.get("case_id") or "").strip()
    if case_id not in REPLAY_CASE_IDS:
        raise RuntimeError(f"unexpected torque replay case ID: {case_id!r}")
    safe_case_id = submitter.sanitize_case_id(case_id)
    identity_args = SimpleNamespace(
        project=policy["project"],
        task_prefix=policy["task_prefix"],
        remote_cases_dir=policy["remote_cases_dir"],
        result_dir=policy["result_dir"],
    )
    dedupe_key = submitter.campaign_dedupe_key(identity_args, row, safe_case_id)
    return ExpectedTask(
        task_id=0,
        case_id=case_id,
        safe_case_id=safe_case_id,
        task_name=f"{policy['task_prefix']}-{safe_case_id}",
        dedupe_key=dedupe_key,
        result_csv=posixpath.join(policy["result_dir"], f"{safe_case_id}.csv"),
        simulation_dir=posixpath.join(policy["simulation_dir"], safe_case_id),
    )


def get_scheduler_task_history(
    scheduler_url: str, project: str, timeout: float
) -> list[dict[str, Any]]:
    query = parse.urlencode({"limit": 10_000, "project": project})
    url = scheduler_url.rstrip("/") + f"/api/tasks?{query}"
    with request.urlopen(url, timeout=timeout) as response:
        payload = response.read()
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("scheduler task history is not valid JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RuntimeError("scheduler task history did not return a list of objects")
    return value


def fetch_task_remote_file_bytes(
    scheduler_url: str,
    task_id: int,
    path: str,
    base: str,
    timeout: float,
) -> bytes:
    if base != REMOTE_FILE_BASE:
        raise RuntimeError(f"unsupported remote-file base: {base!r}")
    normalized = str(path or "").replace("\\", "/")
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or ".." in pure.parts:
        raise RuntimeError(f"unsafe scheduler remote-file path: {path!r}")
    query = parse.urlencode({"path": normalized, "base": base})
    url = scheduler_url.rstrip("/") + f"/api/tasks/{task_id}/remote-file?{query}"
    with request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _exit_code(task: Mapping[str, Any]) -> int | None:
    value = task.get("exit_code")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"scheduler task has invalid exit_code: {value!r}") from exc


def validate_task_identity(
    task: Mapping[str, Any], expected: ExpectedTask, policy: Mapping[str, Any]
) -> None:
    exact = {
        "id": expected.task_id,
        "name": expected.task_name,
        "project": policy["project"],
        "dedupe_key": expected.dedupe_key,
        "required_capability": policy["required_capability"],
        "env_profile": policy["env_profile"],
        "scheduling_profile": policy["scheduling_profile"],
        "max_workers_per_node": policy["max_workers_per_node"],
    }
    mismatches = [key for key, value in exact.items() if task.get(key) != value]
    if mismatches:
        raise RuntimeError(
            f"scheduler task {expected.task_id} identity mismatch: {', '.join(mismatches)}"
        )
    if not str(task.get("remote_cwd") or "").strip():
        raise RuntimeError(f"scheduler task {expected.task_id} has no remote_cwd")


def selected_task_metadata(task: Mapping[str, Any]) -> dict[str, Any]:
    return {key: task.get(key) for key in TASK_HASH_FIELDS}


def discover_torque_path(result_row: Mapping[str, str], expected: ExpectedTask) -> str:
    reported = str(result_row.get("artifact_report_PPT_Torque") or "").strip()
    if not reported:
        raise RuntimeError(f"result has no torque artifact path: {expected.case_id}")
    normalized = reported.replace("\\", "/")
    anchor = expected.simulation_dir.rstrip("/") + "/"
    positions: list[int] = []
    start = 0
    while True:
        index = normalized.find(anchor, start)
        if index < 0:
            break
        if index == 0 or normalized[index - 1] == "/":
            positions.append(index)
        start = index + 1
    if len(positions) != 1:
        raise RuntimeError(
            f"torque artifact is not uniquely below the known case simulation directory: "
            f"{expected.case_id}"
        )
    relative = normalized[positions[0] :]
    pure = PurePosixPath(relative)
    root = PurePosixPath(expected.simulation_dir)
    root_parts = root.parts
    if pure.is_absolute() or ".." in pure.parts or pure.parts[: len(root_parts)] != root_parts:
        raise RuntimeError(f"unsafe torque artifact path for {expected.case_id}")
    suffix = pure.parts[len(root_parts) :]
    expected_name = f"{expected.safe_case_id}_PPT_Torque.csv"
    if (
        len(suffix) != 3
        or re.fullmatch(r"simulation[0-9]+", suffix[0]) is None
        or suffix[1] != "exports"
        or suffix[2] != expected_name
    ):
        raise RuntimeError(
            f"unexpected retained torque artifact layout for {expected.case_id}: {relative}"
        )
    return relative


def _finite(value: object, label: str) -> float:
    result = batch.finite_float(value)
    if not math.isfinite(result):
        raise RuntimeError(f"non-finite forensic value: {label}")
    return result


def parse_torque_raw(
    payload: bytes,
    *,
    period_s: float,
    stop_s: float,
) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeError as exc:
        raise RuntimeError("raw torque report is not valid UTF-8") from exc
    lines = payload.splitlines(keepends=True)
    if not lines:
        raise RuntimeError("raw torque report is empty")
    header_bytes = lines[0]
    header_text = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if header_text.count(";") > header_text.count(",") else ","
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
        columns = list(reader.fieldnames or ())
        rows = [dict(row) for row in reader]
    except csv.Error as exc:
        raise RuntimeError("raw torque report is not a valid CSV") from exc
    if not columns or not rows:
        raise RuntimeError("raw torque report has no header or data rows")
    time_column = batch.find_column(columns, ("time",)) or columns[0]
    torque_column = batch.find_column(columns, ("torque",))
    if torque_column is None:
        raise RuntimeError("raw torque report has no torque column")
    time_unit = batch.extract_column_unit(time_column)
    torque_unit = batch.extract_column_unit(torque_column)
    torque_scale = batch.unit_scale_to_base(torque_unit, "nm")
    eps = max(period_s, stop_s, 1.0) * 1e-9
    selected: list[float] = []
    for row in rows:
        try:
            time_s = batch.parse_time_seconds(row.get(time_column), time_unit)
            torque_nm = batch.parse_report_value(row.get(torque_column), torque_unit, "nm")
        except (TypeError, ValueError) as exc:
            raise RuntimeError("raw torque report contains an invalid time/torque value") from exc
        if stop_s - period_s - eps <= time_s <= stop_s + eps:
            if not math.isfinite(torque_nm):
                raise RuntimeError("raw torque report last-cycle torque is non-finite")
            selected.append(torque_nm)
    if not selected:
        raise RuntimeError("raw torque report has no samples in the sealed last-cycle window")
    if header_bytes.endswith(b"\r\n"):
        line_ending = "crlf"
    elif header_bytes.endswith(b"\n"):
        line_ending = "lf"
    elif header_bytes.endswith(b"\r"):
        line_ending = "cr"
    else:
        line_ending = "none"
    return {
        "utf8_bom": payload.startswith(b"\xef\xbb\xbf"),
        "header_utf8": header_text.lstrip("\ufeff"),
        "header_line_sha256": sha256_bytes(header_bytes),
        "header_line_ending": line_ending,
        "delimiter": delimiter,
        "columns": len(columns),
        "rows": len(rows),
        "time_column": time_column,
        "time_unit": time_unit,
        "torque_column": torque_column,
        "torque_unit": torque_unit,
        "torque_scale_to_nm": torque_scale,
        "last_cycle_samples": len(selected),
        "normalized_last_avg_nm": sum(selected) / len(selected),
    }


def apparent_power_gate(result_row: Mapping[str, str]) -> dict[str, Any]:
    operation = str(result_row.get("input_operation") or "").strip()
    if not batch.is_current_driven_operation(operation):
        raise RuntimeError(f"forensic replay is not current-driven: {operation!r}")
    issues = batch.output_physics_issues(result_row, operation=operation)
    terms = []
    for phase in ("a", "b", "c"):
        voltage = _finite(
            result_row.get(f"output_phase{phase}_voltage_last_rms_v"),
            f"phase{phase}_voltage_last_rms_v",
        )
        current = _finite(
            result_row.get(f"output_phase{phase}_current_last_rms_a"),
            f"phase{phase}_current_last_rms_a",
        )
        terms.append(abs(voltage) * abs(current))
    apparent = sum(terms)
    mech_power = _finite(result_row.get("output_mech_power_last_w"), "mech_power_last_w")
    total_loss = _finite(result_row.get("output_total_loss_last_avg_w"), "total_loss_last_avg_w")
    numerator = abs(mech_power) + total_loss
    ratio = numerator / apparent if apparent > 0.0 else math.inf
    if issues:
        raise RuntimeError("replay result failed apparent-power gate: " + ", ".join(issues))
    if not math.isfinite(ratio) or ratio > 1.05:
        raise RuntimeError("replay apparent-power ratio exceeds the sealed 1.05 limit")
    return {
        "function": "run_ipmsm_batch.output_physics_issues",
        "operation": operation,
        "max_ratio": 1.05,
        "phase_apparent_power_terms_va": terms,
        "apparent_power_sum_va": apparent,
        "absolute_mechanical_power_plus_loss_w": numerator,
        "ratio": ratio,
        "issues": [],
        "passed": True,
    }


def _result_evidence(
    result_payload: bytes,
    plan_row: dict[str, str],
    expected: ExpectedTask,
) -> tuple[list[str], dict[str, str]]:
    try:
        text = result_payload.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError(f"remote result is not valid UTF-8: {expected.case_id}") from exc
    design_hash = str(plan_row.get("design_hash") or "").strip()
    fieldnames, result_row = collector._one_remote_result(text, expected.case_id, design_hash)
    collector.validate_result_matches_plan(plan_row, result_row)
    return fieldnames, result_row


def _case_manifest_record(manifest: Mapping[str, Any], case_id: str) -> dict[str, Any]:
    for record in manifest["cases"]:
        if str(record.get("replay_case_id") or "").strip() == case_id:
            return dict(record)
    raise RuntimeError(f"manifest case record disappeared: {case_id}")


def build_forensic_receipt(
    plan: PlanEvidence,
    tasks: list[ResolvedTask],
    *,
    scheduler_url: str,
    output_dir: Path,
    timeout: float,
    remote_fetcher: RemoteFetcher,
) -> tuple[dict[str, Any], dict[Path, bytes], int]:
    policy = plan.manifest["execution_policy"]
    rows_by_id = {str(row["case_id"]).strip(): row for row in plan.rows}
    outputs: dict[Path, bytes] = {}
    records: list[dict[str, Any]] = []
    fetch_count = 0
    parser_sha256 = sha256_bytes(_read_stable(Path(batch.__file__).resolve(), "parser source"))
    for expected, task, attempts in tasks:
        plan_row = rows_by_id[expected.case_id]
        result_payload = remote_fetcher(
            scheduler_url,
            expected.task_id,
            expected.result_csv,
            REMOTE_FILE_BASE,
            timeout,
        )
        fetch_count += 1
        result_fields, result_row = _result_evidence(result_payload, plan_row, expected)
        if len(result_fields) != EXPECTED_RESULT_COLUMNS:
            raise RuntimeError(
                f"replay result must preserve the exact {EXPECTED_RESULT_COLUMNS}-column schema: "
                f"{expected.case_id} has {len(result_fields)}"
            )
        torque_remote_path = discover_torque_path(result_row, expected)
        raw_payload = remote_fetcher(
            scheduler_url,
            expected.task_id,
            torque_remote_path,
            REMOTE_FILE_BASE,
            timeout,
        )
        fetch_count += 1
        period_s = _finite(result_row.get("output_period_s"), "output_period_s")
        stop_s = _finite(result_row.get("output_stop_time_s"), "output_stop_time_s")
        raw_summary = parse_torque_raw(raw_payload, period_s=period_s, stop_s=stop_s)
        result_torque = _finite(
            result_row.get("output_torque_last_avg_nm"), "output_torque_last_avg_nm"
        )
        raw_torque = float(raw_summary["normalized_last_avg_nm"])
        if not math.isclose(raw_torque, result_torque, rel_tol=1e-10, abs_tol=1e-12):
            raise RuntimeError(
                f"normalized raw torque does not match result for {expected.case_id}: "
                f"raw={raw_torque:.17g} result={result_torque:.17g}"
            )
        rpm = _finite(result_row.get("input_base_rpm"), "input_base_rpm")
        raw_mech_power = raw_torque * rpm * 2.0 * math.pi / 60.0
        result_mech_power = _finite(
            result_row.get("output_mech_power_last_w"), "output_mech_power_last_w"
        )
        if not math.isclose(raw_mech_power, result_mech_power, rel_tol=1e-10, abs_tol=1e-9):
            raise RuntimeError(
                f"normalized raw torque does not reproduce mechanical power: {expected.case_id}"
            )
        gate = apparent_power_gate(result_row)

        result_local = output_dir / "results" / f"{expected.safe_case_id}.csv"
        raw_local = output_dir / "raw" / expected.safe_case_id / PurePosixPath(
            torque_remote_path
        ).name
        outputs[result_local] = result_payload
        outputs[raw_local] = raw_payload
        task_metadata = selected_task_metadata(task)
        attempt_history = []
        excluded_task_ids = []
        for attempt in attempts:
            attempt_metadata = selected_task_metadata(attempt)
            attempt_id = int(attempt_metadata["id"])
            selected = attempt_id == expected.task_id
            if not selected:
                excluded_task_ids.append(attempt_id)
            attempt_history.append(
                {
                    "task": attempt_metadata,
                    "task_metadata_canonical_sha256": canonical_sha256(attempt_metadata),
                    "disposition": (
                        "selected_evidence" if selected else "excluded_failed_attempt"
                    ),
                }
            )
        manifest_record = _case_manifest_record(plan.manifest, expected.case_id)
        result_header_sha256 = canonical_sha256(result_fields)
        records.append(
            {
                "case_id": expected.case_id,
                "source_case_id": manifest_record.get("source_case_id"),
                "stage": manifest_record.get("stage"),
                "role": manifest_record.get("role"),
                "design_hash": str(plan_row.get("design_hash") or ""),
                "source_geometry_group_id": manifest_record.get(
                    "source_geometry_group_id"
                ),
                "replay_geometry_group_id": manifest_record.get(
                    "replay_geometry_group_id"
                ),
                "plan_row_canonical_sha256": canonical_sha256(plan_row),
                "replacement_mapping_inputs": {
                    "official_case_id": manifest_record.get("source_case_id"),
                    "official_geometry_group_id": manifest_record.get(
                        "source_geometry_group_id"
                    ),
                    "replay_case_id": expected.case_id,
                    "replay_geometry_group_id": manifest_record.get(
                        "replay_geometry_group_id"
                    ),
                    "source_plan": manifest_record.get("source_plan"),
                    "source_plan_sha256": manifest_record.get("source_plan_sha256"),
                    "source_plan_line": manifest_record.get("source_line"),
                    "source_row_canonical_sha256": manifest_record.get(
                        "source_row_canonical_sha256"
                    ),
                    "replay_plan_sha256": plan.sha256,
                    "replay_row_canonical_sha256": canonical_sha256(plan_row),
                    "result_schema_columns": len(result_fields),
                    "result_header_canonical_sha256": result_header_sha256,
                    "remap_performed": False,
                },
                "task": task_metadata,
                "task_metadata_canonical_sha256": canonical_sha256(task_metadata),
                "attempt_history": attempt_history,
                "selected_task_id": expected.task_id,
                "excluded_task_ids": excluded_task_ids,
                "dedupe_key": expected.dedupe_key,
                "result": {
                    "remote_path": expected.result_csv,
                    "local_path": result_local.as_posix(),
                    "sha256": sha256_bytes(result_payload),
                    "bytes": len(result_payload),
                    "columns": len(result_fields),
                    "header_canonical_sha256": result_header_sha256,
                    "row_canonical_sha256": canonical_sha256(result_row),
                    "status": str(result_row.get("status") or ""),
                    "torque_last_avg_nm": result_torque,
                    "mechanical_power_last_w": result_mech_power,
                },
                "raw_torque": {
                    "reported_absolute_path": str(
                        result_row.get("artifact_report_PPT_Torque") or ""
                    ),
                    "remote_path": torque_remote_path,
                    "local_path": raw_local.as_posix(),
                    "sha256": sha256_bytes(raw_payload),
                    "bytes": len(raw_payload),
                    **raw_summary,
                    "recomputed_mechanical_power_w": raw_mech_power,
                    "matches_result_torque": True,
                    "matches_result_mechanical_power": True,
                },
                "apparent_power_gate": gate,
            }
        )
    if fetch_count != 2 * len(REPLAY_CASE_IDS):
        raise RuntimeError("unexpected forensic remote-file fetch count")
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "scheduler": {
            "url": scheduler_url.rstrip("/"),
            "access": "read_only_get",
            "remote_file_base": REMOTE_FILE_BASE,
            "selected_task_ids": [expected.task_id for expected, _, _ in tasks],
            "attempt_task_ids": [
                int(attempt["id"])
                for _, _, attempts in tasks
                for attempt in attempts
            ],
            "remote_file_fetches": fetch_count,
        },
        "plan": {
            "path": plan.path.as_posix(),
            "sha256": plan.sha256,
            "bytes": len(plan.payload),
            "rows": len(plan.rows),
            "columns": len(plan.fieldnames),
            "manifest_path": plan.manifest_path.as_posix(),
            "manifest_sha256": plan.manifest_sha256,
            "manifest_bytes": len(plan.manifest_payload),
        },
        "execution_policy": dict(policy),
        "parser": {
            "path": "run_ipmsm_batch.py",
            "sha256": parser_sha256,
            "torque_unit_scale_function": "run_ipmsm_batch.unit_scale_to_base",
            "physics_gate_function": "run_ipmsm_batch.output_physics_issues",
        },
        "publication": {
            "mode": "no_replace",
            "output_dir": output_dir.as_posix(),
            "receipt_path": (output_dir / RECEIPT_NAME).as_posix(),
        },
        "cases": records,
        "verified": True,
    }
    return receipt, outputs, fetch_count


def _publish_exact(path: Path, payload: bytes) -> str:
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise FileExistsError(f"refusing to replace different forensic artifact: {path}")
        return "existing_verified"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".staged", dir=path.parent
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            publish_receipt = atomic_publish.publish_no_replace(staged, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise
            return "existing_verified"
        finally:
            staged.unlink(missing_ok=True)
        atomic_publish.cleanup_publish_receipt(publish_receipt)
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise RuntimeError(f"published forensic artifact bytes changed: {path}")
        return "published"
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        staged.unlink(missing_ok=True)


def publish_receipt_outputs(
    receipt: dict[str, Any], outputs: Mapping[Path, bytes], output_dir: Path
) -> str:
    statuses = []
    for path in sorted(outputs, key=lambda item: item.as_posix()):
        statuses.append(_publish_exact(path, outputs[path]))
    receipt_payload = canonical_json_bytes(receipt)
    statuses.append(_publish_exact(output_dir / RECEIPT_NAME, receipt_payload))
    return "published" if "published" in statuses else "existing_verified"


def _task_id(task: Mapping[str, Any]) -> int:
    value = task.get("id")
    if isinstance(value, bool):
        raise RuntimeError(f"scheduler task has invalid id: {value!r}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"scheduler task has invalid id: {value!r}") from exc
    if result <= 0:
        raise RuntimeError(f"scheduler task has invalid id: {value!r}")
    return result


def inspect_tasks(
    plan: PlanEvidence,
    *,
    scheduler_url: str,
    timeout: float,
    task_history_getter: TaskHistoryGetter,
) -> tuple[list[ResolvedTask], dict[str, str]]:
    policy = plan.manifest["execution_policy"]
    history = task_history_getter(scheduler_url, str(policy["project"]), timeout)
    ids = [_task_id(task) for task in history]
    if len(ids) != len(set(ids)):
        raise RuntimeError("scheduler task history contains duplicate task IDs")
    tasks: list[ResolvedTask] = []
    statuses: dict[str, str] = {}
    for row in plan.rows:
        base_expected = expected_task_for_row(row, policy)
        same_name = [
            task
            for task in history
            if str(task.get("name") or "").strip() == base_expected.task_name
        ]
        if not same_name:
            raise RuntimeError(
                f"scheduler history has no attempt for replay case {base_expected.case_id}"
            )
        wrong_identity = [
            _task_id(task)
            for task in same_name
            if task.get("project") != policy["project"]
            or task.get("dedupe_key") != base_expected.dedupe_key
        ]
        if wrong_identity:
            raise RuntimeError(
                f"scheduler attempt identity collision for {base_expected.case_id}: "
                + ", ".join(str(value) for value in wrong_identity)
            )
        attempts = tuple(sorted(same_name, key=_task_id))
        for attempt in attempts:
            validate_task_identity(
                attempt,
                replace(base_expected, task_id=_task_id(attempt)),
                policy,
            )
            status = str(attempt.get("status") or "").strip().lower()
            if status not in {
                "queued",
                "attaching",
                "running",
                "completed",
                "failed",
                "cancelled",
            }:
                raise RuntimeError(
                    f"scheduler attempt {_task_id(attempt)} has unknown status {status!r}"
                )
        latest = attempts[-1]
        latest_status = str(latest.get("status") or "").strip().lower()
        selected_expected = replace(base_expected, task_id=_task_id(latest))
        prior_success = [
            _task_id(attempt)
            for attempt in attempts[:-1]
            if str(attempt.get("status") or "").strip().lower() == "completed"
            and _exit_code(attempt) == 0
        ]
        if prior_success:
            raise RuntimeError(
                f"replay case {base_expected.case_id} has a superseded successful attempt: "
                + ", ".join(str(value) for value in prior_success)
            )
        for attempt in attempts[:-1]:
            status = str(attempt.get("status") or "").strip().lower()
            if status in {"queued", "attaching", "running"}:
                raise RuntimeError(
                    f"replay case {base_expected.case_id} has overlapping active attempts"
                )
            if status == "completed" and _exit_code(attempt) == 0:
                raise RuntimeError(
                    f"replay case {base_expected.case_id} has ambiguous successful attempts"
                )
        statuses[base_expected.case_id] = latest_status
        tasks.append((selected_expected, latest, attempts))
    return tasks, statuses


def audit_replay(
    *,
    plan_path: Path,
    manifest_path: Path,
    output_dir: Path,
    scheduler_url: str,
    timeout: float,
    publish: bool,
    task_history_getter: TaskHistoryGetter = get_scheduler_task_history,
    remote_fetcher: RemoteFetcher = fetch_task_remote_file_bytes,
) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise RuntimeError("--timeout must be finite and > 0")
    plan = load_plan_evidence(plan_path, manifest_path)
    tasks, statuses = inspect_tasks(
        plan,
        scheduler_url=scheduler_url,
        timeout=timeout,
        task_history_getter=task_history_getter,
    )
    completed = sum(status == "completed" for status in statuses.values())
    unexpected = {
        case_id: status
        for case_id, status in statuses.items()
        if status not in {"queued", "attaching", "running", "completed"}
    }
    if unexpected:
        details = ", ".join(f"{case_id}:{status or '<blank>'}" for case_id, status in unexpected.items())
        raise RuntimeError("replay task entered a non-success terminal state: " + details)
    if completed != len(tasks):
        selected_task_ids = [expected.task_id for expected, _, _ in tasks]
        excluded_task_ids = [
            _task_id(attempt)
            for expected, _, attempts in tasks
            for attempt in attempts
            if _task_id(attempt) != expected.task_id
        ]
        return {
            "status": "pending",
            "completed": completed,
            "total": len(tasks),
            "statuses": statuses,
            "remote_file_fetches": 0,
            "selected_task_ids": ",".join(str(value) for value in selected_task_ids),
            "excluded_task_ids": ",".join(str(value) for value in excluded_task_ids),
            "mode": "publish" if publish else "dry-run",
        }
    bad_exit = [
        expected.task_id
        for expected, task, _ in tasks
        if _exit_code(task) != 0
    ]
    if bad_exit:
        raise RuntimeError(
            "completed replay tasks have nonzero or missing exit codes: "
            + ", ".join(str(value) for value in bad_exit)
        )
    receipt, outputs, fetch_count = build_forensic_receipt(
        plan,
        tasks,
        scheduler_url=scheduler_url,
        output_dir=output_dir,
        timeout=timeout,
        remote_fetcher=remote_fetcher,
    )
    receipt_payload = canonical_json_bytes(receipt)
    if publish:
        publication = publish_receipt_outputs(receipt, outputs, output_dir)
    else:
        publication = "would_publish"
    return {
        "status": "verified",
        "completed": len(tasks),
        "total": len(tasks),
        "remote_file_fetches": fetch_count,
        "selected_task_ids": ",".join(
            str(expected.task_id) for expected, _, _ in tasks
        ),
        "excluded_task_ids": ",".join(
            str(_task_id(attempt))
            for expected, _, attempts in tasks
            for attempt in attempts
            if _task_id(attempt) != expected.task_id
        ),
        "mode": "publish" if publish else "dry-run",
        "publication": publication,
        "receipt_sha256": sha256_bytes(receipt_payload),
        "receipt_path": (output_dir / RECEIPT_NAME).as_posix(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--plan-manifest", type=Path, default=DEFAULT_PLAN_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--scheduler-url", default=DEFAULT_SCHEDULER_URL)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish verified local evidence; default dry-run writes nothing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = audit_replay(
        plan_path=args.plan,
        manifest_path=args.plan_manifest,
        output_dir=args.output_dir,
        scheduler_url=args.scheduler_url,
        timeout=args.timeout,
        publish=args.publish,
    )
    print(
        "torque_unit_forensics "
        + " ".join(
            f"{key}={result[key]}"
            for key in (
                "mode",
                "status",
                "completed",
                "total",
                "remote_file_fetches",
                "selected_task_ids",
                "excluded_task_ids",
                "publication",
                "receipt_sha256",
                "receipt_path",
            )
            if key in result
        )
    )
    return 3 if result["status"] == "pending" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
