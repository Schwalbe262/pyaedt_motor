"""Low-rate, resumable physics audit for the original Stage2 v4r3 tasks.

The audit is scheduler-read-only.  It reconstructs every task name and dedupe
key from the sealed Stage2 decision and case plan, then performs one bounded
``name_prefix`` lookup per planned case.  Only scheduler-completed, exit-zero
attempts have their one-row result CSV fetched.

Dry-run is the default and writes nothing.  ``--publish`` atomically updates a
compact receipt after every newly verified result so an interrupted audit can
resume without fetching that result again.  No scheduler mutation or revised
case-plan publication exists in this program.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
from urllib import error, parse, request

import atomic_publish
import collect_ipmsm_v2_campaign as collector
import rank_ipmsm_quality_profiles as profile_rank
import run_ipmsm_batch as batch
import run_ipmsm_v2_campaign as campaign_runner
import submit_ipmsm_v2_campaign as submitter


SCHEMA_VERSION = "ipmsm-stage2-v4r3-physics-audit-v1"
DEFAULT_ROOT = Path("simul_log_smoke/beta_zero_recovery_26092_26093")
DEFAULT_PLAN = DEFAULT_ROOT / "ipmsm_v2_foundation_stage2_300_cases.csv"
DEFAULT_DECISION = DEFAULT_ROOT / "foundation_stage2_decision.json"
DEFAULT_OUTPUT_DIR = Path("simul_log_smoke/v4r4/stage2_v4r3_physics_audit")
RECEIPT_NAME = "receipt.canonical.json"
REPORT_NAME = "report.canonical.json"
CHECKPOINT_DIR_NAME = "result_checkpoints"
CHECKPOINT_SCHEMA_VERSION = "ipmsm-stage2-v4r3-result-checkpoint-v1"
EXPECTED_PLAN_ROWS = 300
REMOTE_FILE_BASE = "remote_cwd"
MAX_JSON_BYTES = 2_000_000

EXPECTED_POLICY = {
    "project": "PYAEDT_MOTOR_IPMSM_V2",
    # Historical v4r3 identity: the live project cap was 100 when these tasks
    # were submitted.  A later local policy reduction must not make immutable
    # task history unauditable.
    "project_active_cap": 100,
    "task_prefix": "ipmsm-v2-foundation-s2",
    "remote_cases_dir": "remote/ipmsm_v2_foundation_s2",
    "result_dir": "simul_log/ipmsm_v2_foundation_s2",
    "simulation_dir": "simulation/ipmsm_v2_foundation_s2",
    "log_dir": "simul_log_scheduler/ipmsm_v2_foundation_s2_logs",
    "entrypoint": "subprocess_run.py",
    "env_setup": "module load ansys-electronics/v252",
    "required_capability": "conda:pyaedt2026v1",
    "env_profile": "pyaedt2026v1",
    "scheduling_profile": "fea_bursty",
    "cpus": 4,
    "memory_mb": 32_768,
    "max_workers_per_node": 0,
    "cores_per_process": 4,
    "timeout_seconds": 43_200,
    "keep_projects": False,
}
EXPECTED_TASK_PAYLOAD_POLICY = {"exclusive_node": False}

KNOWN_STATUSES = {
    "queued",
    "attaching",
    "running",
    "completed",
    "failed",
    "cancelled",
}
ACTIVE_STATUSES = {"queued", "attaching", "running"}
INFRA_TERMINAL_STATUSES = {"failed", "cancelled"}
TASK_FINGERPRINT_FIELDS = (
    "id",
    "name",
    "status",
    "state",
    "project",
    "dedupe_key",
    "remote_cwd",
    "entrypoint",
    "command",
    "env_setup",
    "required_capability",
    "env_profile",
    "scheduling_profile",
    "max_workers_per_node",
    "cpus",
    "memory_mb",
    "gpus",
    "gpu_model",
    "partition",
    "node_name",
    "priority",
    "timeout_seconds",
    "requested_account_name",
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

REQUIRED_EXECUTION_IDENTITY_FIELDS = (
    "name",
    "project",
    "dedupe_key",
    "entrypoint",
    "required_capability",
    "env_profile",
    "scheduling_profile",
    "max_workers_per_node",
    "cpus",
    "memory_mb",
    "gpus",
    "gpu_model",
    "partition",
    "node_name",
    "priority",
    "timeout_seconds",
)
OPTIONAL_EXECUTION_IDENTITY_FIELDS = (
    # Current task-list responses omit these two fields.  When a scheduler
    # exposes them, exact comparison also binds the command's
    # --cores-per-process / --keep-projects semantics and the full bootstrap.
    "command",
    "env_setup",
    "exclusive_node",
)
CHECKPOINT_NAME_RE = re.compile(
    r"^(?P<safe_case>[A-Za-z0-9_.-]+)\.task-(?P<task_id>[1-9][0-9]*)\."
    r"(?P<sha256>[0-9a-f]{64})\.canonical\.json$"
)


RawGetter = Callable[[str, float, int], bytes]
Sleep = Callable[[float], None]
Clock = Callable[[], float]


@dataclass(frozen=True)
class CampaignEvidence:
    plan_path: Path
    plan_payload: bytes
    plan_sha256: str
    fieldnames: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    decision_path: Path
    decision_payload: bytes
    decision_sha256: str
    decision: dict[str, Any]
    runner_args: argparse.Namespace
    tasks: tuple[submitter.CampaignTask, ...]
    identity: dict[str, Any]


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
    return sha256_bytes(canonical_json_bytes(value))


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _stable_read(path: Path, label: str) -> bytes:
    try:
        payload = path.read_bytes()
        if path.read_bytes() != payload:
            raise RuntimeError(f"{label} changed while it was read: {path}")
        return payload
    except OSError as exc:
        raise RuntimeError(f"cannot read {label}: {path}") from exc


def _decode_json(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{label} is not valid strict JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _decode_csv(payload: bytes, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        fieldnames = list(reader.fieldnames or ())
        rows = [dict(row) for row in reader]
    except (UnicodeError, csv.Error) as exc:
        raise RuntimeError(f"{label} is not a valid UTF-8 CSV") from exc
    if not fieldnames or any(not str(name or "").strip() for name in fieldnames):
        raise RuntimeError(f"{label} has an invalid CSV header")
    if len(fieldnames) != len(set(fieldnames)):
        raise RuntimeError(f"{label} has duplicate CSV columns")
    return fieldnames, rows


def _same_path(recorded: object, actual: Path) -> bool:
    if not str(recorded or "").strip():
        return False
    try:
        return Path(str(recorded)).resolve() == actual.resolve()
    except OSError:
        return False


def _source_hash(module: Any) -> str:
    path = Path(str(module.__file__)).resolve()
    return sha256_bytes(_stable_read(path, f"source {path.name}"))


def load_campaign_evidence(
    plan_path: Path,
    decision_path: Path,
    *,
    expected_rows: int = EXPECTED_PLAN_ROWS,
) -> CampaignEvidence:
    if expected_rows < 1:
        raise RuntimeError("expected plan rows must be >= 1")
    plan_payload = _stable_read(plan_path, "Stage2 v4r3 plan")
    fieldnames, rows = _decode_csv(plan_payload, "Stage2 v4r3 plan")
    if len(rows) != expected_rows:
        raise RuntimeError(
            f"Stage2 v4r3 plan row count mismatch: {len(rows)} != {expected_rows}"
        )
    case_ids = [str(row.get("case_id") or "").strip() for row in rows]
    if any(not case_id for case_id in case_ids) or len(case_ids) != len(set(case_ids)):
        raise RuntimeError("Stage2 v4r3 plan has blank or duplicate case IDs")
    if any(not str(row.get("design_hash") or "").strip() for row in rows):
        raise RuntimeError("Stage2 v4r3 plan has a blank design_hash")

    decision_payload = _stable_read(decision_path, "Stage2 v4r3 decision")
    decision = _decode_json(decision_payload, "Stage2 v4r3 decision")
    if decision.get("schema_version") != "ipmsm_v2_stage2_continuation_v1":
        raise RuntimeError("unexpected Stage2 decision schema")
    if decision.get("decision") != "run_stage2" or decision.get("status") != "stage2_started":
        raise RuntimeError("Stage2 decision is not the sealed stage2_started decision")
    stage2 = decision.get("stage2")
    if not isinstance(stage2, dict):
        raise RuntimeError("Stage2 decision has no stage2 object")
    plan_sha256 = sha256_bytes(plan_payload)
    if stage2.get("case_plan_sha256") != plan_sha256:
        raise RuntimeError("Stage2 decision/plan SHA-256 mismatch")
    if not _same_path(stage2.get("case_plan"), plan_path):
        raise RuntimeError("Stage2 decision points to a different case plan")
    argv = stage2.get("runner_argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise RuntimeError("Stage2 decision runner_argv is malformed")
    try:
        runner_args = campaign_runner.build_parser().parse_args(argv)
    except SystemExit as exc:
        raise RuntimeError("Stage2 decision runner_argv cannot be parsed") from exc
    if not runner_args.submit:
        raise RuntimeError("Stage2 decision runner_argv is not the submitted campaign")
    if not _same_path(runner_args.cases, plan_path):
        raise RuntimeError("Stage2 runner points to a different case plan")
    if runner_args.case_start_index != 1 or runner_args.case_limit not in {0, expected_rows}:
        raise RuntimeError("Stage2 runner does not cover the complete plan")
    for key, expected in EXPECTED_POLICY.items():
        if getattr(runner_args, key) != expected:
            raise RuntimeError(
                f"Stage2 runner policy mismatch for {key}: "
                f"{getattr(runner_args, key)!r} != {expected!r}"
            )
    if not _same_path(stage2.get("output_dir"), runner_args.output_dir):
        raise RuntimeError("Stage2 decision/output directory mismatch")

    tasks = submitter.build_campaign_tasks(
        runner_args,
        rows,
        first_row_number=runner_args.case_start_index,
    )
    if len(tasks) != expected_rows:
        raise RuntimeError("Stage2 task reconstruction did not cover the plan")
    for task in tasks:
        for key, expected in EXPECTED_TASK_PAYLOAD_POLICY.items():
            if task.payload.get(key) != expected:
                raise RuntimeError(
                    f"Stage2 reconstructed task policy mismatch for {key}: "
                    f"{task.payload.get(key)!r} != {expected!r}"
                )
    identity = {
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": plan_sha256,
        "plan_rows": expected_rows,
        "decision_path": str(decision_path.resolve()),
        "decision_sha256": sha256_bytes(decision_payload),
        "contract_sha256": str(decision.get("contract_sha256") or ""),
        "policy": {key: getattr(runner_args, key) for key in EXPECTED_POLICY},
        "task_payload_policy": EXPECTED_TASK_PAYLOAD_POLICY,
        "sources": {
            "auditor": sha256_bytes(_stable_read(Path(__file__).resolve(), "auditor source")),
            "atomic_publish": _source_hash(atomic_publish),
            "run_ipmsm_batch": _source_hash(batch),
            "collector": _source_hash(collector),
            "campaign_runner": _source_hash(campaign_runner),
            "submitter": _source_hash(submitter),
            "infra_classifier": _source_hash(profile_rank),
        },
    }
    return CampaignEvidence(
        plan_path=plan_path,
        plan_payload=plan_payload,
        plan_sha256=plan_sha256,
        fieldnames=tuple(fieldnames),
        rows=tuple(rows),
        decision_path=decision_path,
        decision_payload=decision_payload,
        decision_sha256=sha256_bytes(decision_payload),
        decision=decision,
        runner_args=runner_args,
        tasks=tuple(tasks),
        identity=identity,
    )


def _default_raw_getter(url: str, timeout: float, max_bytes: int) -> bytes:
    req = request.Request(url, method="GET", headers={"Accept": "application/json,text/csv"})
    with request.urlopen(req, timeout=timeout) as response:
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise RuntimeError(f"scheduler response exceeds {max_bytes} bytes")
    return payload


class PacedHttpReader:
    """Serialize all GETs and pace request starts; retry only HTTP 429."""

    def __init__(
        self,
        *,
        timeout: float,
        pace_seconds: float,
        backoff_seconds: float,
        max_backoff_seconds: float,
        max_429_retries: int,
        raw_getter: RawGetter = _default_raw_getter,
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        values = (timeout, pace_seconds, backoff_seconds, max_backoff_seconds)
        if any(not math.isfinite(value) for value in values) or timeout <= 0.0:
            raise RuntimeError("HTTP timing values must be finite and timeout must be > 0")
        if pace_seconds < 1.0:
            raise RuntimeError("--pace-seconds must be >= 1")
        if backoff_seconds < 1.0:
            raise RuntimeError("--backoff-seconds must be >= 1")
        if max_backoff_seconds < backoff_seconds:
            raise RuntimeError("--max-backoff-seconds must be >= --backoff-seconds")
        if max_429_retries < 0:
            raise RuntimeError("--max-429-retries must be >= 0")
        self.timeout = timeout
        self.pace_seconds = pace_seconds
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.max_429_retries = max_429_retries
        self.raw_getter = raw_getter
        self.sleep = sleep
        self.clock = clock
        self._next_start = 0.0
        self._lock = threading.Lock()
        self.request_count = 0
        self.rate_limit_retries = 0

    def _wait_for_slot(self) -> None:
        delay = self._next_start - self.clock()
        if delay > 0.0:
            self.sleep(delay)
        self._next_start = self.clock() + self.pace_seconds

    @staticmethod
    def _retry_after(exc: error.HTTPError) -> float:
        raw = exc.headers.get("Retry-After") if exc.headers is not None else None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value >= 0.0 else 0.0

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        if max_bytes < 1:
            raise RuntimeError("max_bytes must be >= 1")
        with self._lock:
            for retry_number in range(self.max_429_retries + 1):
                self._wait_for_slot()
                self.request_count += 1
                try:
                    return self.raw_getter(url, self.timeout, max_bytes)
                except error.HTTPError as exc:
                    if exc.code != 429 or retry_number >= self.max_429_retries:
                        raise RuntimeError(
                            f"scheduler GET failed with HTTP {exc.code}: {url}"
                        ) from exc
                    self.rate_limit_retries += 1
                    exponential = self.backoff_seconds * (2**retry_number)
                    delay = max(exponential, self._retry_after(exc))
                    self.sleep(min(self.max_backoff_seconds, delay))
            raise AssertionError("unreachable HTTP retry loop")

    def get_json_list(self, url: str) -> list[dict[str, Any]]:
        payload = self.get_bytes(url, max_bytes=MAX_JSON_BYTES)
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("scheduler task query returned invalid JSON") from exc
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise RuntimeError("scheduler task query did not return a list of objects")
        return value


def _task_id(task: Mapping[str, Any]) -> int:
    value = task.get("id")
    if isinstance(value, bool):
        raise RuntimeError(f"scheduler task has invalid id: {value!r}")
    try:
        task_id = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"scheduler task has invalid id: {value!r}") from exc
    if task_id <= 0:
        raise RuntimeError(f"scheduler task has invalid id: {value!r}")
    return task_id


def _exit_code(task: Mapping[str, Any]) -> int | None:
    raw = task.get("exit_code", task.get("return_code"))
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"scheduler task has invalid exit_code: {raw!r}") from exc


def task_metadata(task: Mapping[str, Any]) -> dict[str, Any]:
    return {key: task.get(key) for key in TASK_FINGERPRINT_FIELDS}


def task_fingerprint(task: Mapping[str, Any]) -> str:
    return canonical_sha256(task_metadata(task))


def _expected_task_field(expected: submitter.CampaignTask, key: str) -> Any:
    if key == "name":
        return expected.task_name
    if key == "dedupe_key":
        return expected.dedupe_key
    if key == "requested_account_name":
        return expected.payload["account_name"]
    return expected.payload[key]


def validate_task_identity(
    observed: Mapping[str, Any], expected: submitter.CampaignTask
) -> None:
    missing = [key for key in REQUIRED_EXECUTION_IDENTITY_FIELDS if key not in observed]
    if missing:
        raise RuntimeError(
            f"scheduler omitted execution identity for case_id={expected.case_id!r}: "
            + ", ".join(missing)
        )
    exact_fields = REQUIRED_EXECUTION_IDENTITY_FIELDS + tuple(
        key for key in OPTIONAL_EXECUTION_IDENTITY_FIELDS if key in observed
    )
    if "requested_account_name" in observed:
        exact_fields += ("requested_account_name",)
    mismatches = [
        key
        for key in exact_fields
        if observed.get(key) != _expected_task_field(expected, key)
    ]
    if mismatches:
        raise RuntimeError(
            f"scheduler identity mismatch for case_id={expected.case_id!r}: "
            + ", ".join(mismatches)
        )
    if not str(observed.get("remote_cwd") or "").strip():
        raise RuntimeError(f"scheduler task has no remote_cwd for case_id={expected.case_id!r}")
    status = str(observed.get("status") or "").strip().lower()
    if status not in KNOWN_STATUSES:
        raise RuntimeError(
            f"scheduler task has unknown status for case_id={expected.case_id!r}: {status!r}"
        )
    _task_id(observed)
    _exit_code(observed)


def query_exact_attempts(
    client: PacedHttpReader,
    *,
    scheduler_url: str,
    expected: submitter.CampaignTask,
    attempt_limit: int,
) -> list[dict[str, Any]]:
    if not 2 <= attempt_limit <= 100:
        raise RuntimeError("--attempt-limit must be between 2 and 100")
    query = parse.urlencode(
        {
            "project": expected.payload["project"],
            "name_prefix": expected.task_name,
            "limit": attempt_limit,
        }
    )
    url = scheduler_url.rstrip("/") + f"/api/tasks?{query}"
    candidates = client.get_json_list(url)
    if len(candidates) >= attempt_limit:
        raise RuntimeError(
            f"bounded scheduler query may be truncated for case_id={expected.case_id!r}"
        )
    collisions = [
        item
        for item in candidates
        if item.get("dedupe_key") == expected.dedupe_key
        and str(item.get("name") or "") != expected.task_name
    ]
    if collisions:
        raise RuntimeError(f"scheduler name/dedupe collision for case_id={expected.case_id!r}")
    attempts = [
        item
        for item in candidates
        if str(item.get("name") or "") == expected.task_name
    ]
    ids = [_task_id(item) for item in attempts]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"duplicate scheduler task IDs for case_id={expected.case_id!r}")
    for item in attempts:
        validate_task_identity(item, expected)
    return sorted(attempts, key=_task_id)


def select_attempt(
    attempts: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    if not attempts:
        return "unsubmitted", None
    active = [
        item
        for item in attempts
        if str(item.get("status") or "").strip().lower() in ACTIVE_STATUSES
    ]
    if len(active) > 1:
        raise RuntimeError("scheduler history has overlapping active attempts")
    if active:
        return str(active[0].get("status") or "").strip().lower(), active[0]
    successful = [
        item
        for item in attempts
        if str(item.get("status") or "").strip().lower() == "completed"
        and _exit_code(item) == 0
    ]
    if successful:
        return "scheduler_success", max(successful, key=_task_id)
    terminal = [
        item
        for item in attempts
        if str(item.get("status") or "").strip().lower() in INFRA_TERMINAL_STATUSES
        or (
            str(item.get("status") or "").strip().lower() == "completed"
            and _exit_code(item) != 0
        )
    ]
    if terminal:
        return "retryable_infrastructure", max(terminal, key=_task_id)
    raise RuntimeError("scheduler history has no classifiable attempt")


def _safe_text(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _result_url(
    scheduler_url: str,
    task_id: int,
    result_csv: str,
    max_result_bytes: int,
) -> str:
    normalized = str(result_csv or "").replace("\\", "/")
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or ".." in pure.parts:
        raise RuntimeError(f"unsafe result CSV path: {result_csv!r}")
    query = parse.urlencode(
        {
            "path": normalized,
            "base": REMOTE_FILE_BASE,
            "max_bytes": max_result_bytes,
        }
    )
    return scheduler_url.rstrip("/") + f"/api/tasks/{task_id}/remote-file?{query}"


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def power_envelope_evidence(row: Mapping[str, str]) -> dict[str, Any]:
    terms: list[float] = []
    for phase in ("a", "b", "c"):
        voltage = _finite(row.get(f"output_phase{phase}_voltage_last_rms_v"))
        current = _finite(row.get(f"output_phase{phase}_current_last_rms_a"))
        if voltage is None or current is None:
            return {"apparent_power_va": None, "mech_loss_to_apparent_ratio": None}
        terms.append(abs(voltage) * abs(current))
    apparent = sum(terms)
    mech_power = _finite(row.get("output_mech_power_last_w"))
    total_loss = _finite(row.get("output_total_loss_last_avg_w"))
    ratio = None
    if apparent > 0.0 and mech_power is not None and total_loss is not None:
        ratio = (abs(mech_power) + total_loss) / apparent
    return {
        "apparent_power_va": apparent,
        "mech_power_w": mech_power,
        "total_loss_w": total_loss,
        "mech_loss_to_apparent_ratio": ratio,
        "torque_last_avg_nm": _finite(row.get("output_torque_last_avg_nm")),
    }


def audit_result_payload(
    payload: bytes,
    *,
    expected: submitter.CampaignTask,
    plan_row: dict[str, str],
    selected_task: Mapping[str, Any],
) -> dict[str, Any]:
    fieldnames, rows = _decode_csv(payload, f"result for {expected.case_id}")
    if len(rows) != 1:
        raise RuntimeError(
            f"result CSV must have exactly one row for case_id={expected.case_id!r}"
        )
    row = rows[0]
    if str(row.get("case_id") or "").strip() != expected.case_id:
        raise RuntimeError(f"result case_id mismatch for {expected.case_id!r}")
    expected_design_hash = str(plan_row.get("design_hash") or "").strip()
    result_hashes = {
        str(row.get(column) or "").strip()
        for column in ("design_hash", "input_design_hash")
        if str(row.get(column) or "").strip()
    }
    if result_hashes != {expected_design_hash}:
        raise RuntimeError(f"result design_hash mismatch for {expected.case_id!r}")

    row_status = str(row.get("status") or "").strip().lower()
    plan_contract_error = ""
    try:
        collector.validate_result_matches_plan(plan_row, row)
    except RuntimeError as exc:
        plan_contract_error = _safe_text(exc)
    record: dict[str, Any] = {
        "case_id": expected.case_id,
        "dedupe_key": expected.dedupe_key,
        "task_name": expected.task_name,
        "selected_task_id": _task_id(selected_task),
        "task_fingerprint": task_fingerprint(selected_task),
        "result_csv": expected.result_csv,
        "result_sha256": sha256_bytes(payload),
        "result_bytes": len(payload),
        "result_columns": len(fieldnames),
        "row_status": row_status,
        "plan_contract_error": plan_contract_error or None,
    }
    if row_status != "ok":
        retryable = profile_rank.row_is_retryable_infra_failure(row)
        record.update(
            {
                "classification": (
                    "retryable_infrastructure_result" if retryable else "simulation_failure_result"
                ),
                "physics_issues": [],
                "result_error": _safe_text(
                    " ".join(
                        str(row.get(key) or "")
                        for key in ("error", "failure_reason", "exception", "message")
                    )
                ),
                "power_envelope": power_envelope_evidence(row),
            }
        )
        return record

    # The canonical collector validates the full successful-row contract before
    # the current physics gate is applied to the old result payload.
    text = payload.decode("utf-8-sig")
    _, validated = collector._one_remote_result(
        text,
        expected.case_id,
        expected_design_hash,
    )
    collector.validate_result_matches_plan(plan_row, validated)
    operation = str(plan_row.get("operation") or validated.get("input_operation") or "")
    physics_issues = batch.output_physics_issues(validated, operation=operation)
    if "apparent_power_bound" in physics_issues:
        classification = "torque_unit_suspect"
    elif physics_issues:
        classification = "physics_failed"
    else:
        classification = "physics_ok"
    record.update(
        {
            "classification": classification,
            "physics_issues": sorted(set(physics_issues)),
            "result_error": None,
            "power_envelope": power_envelope_evidence(validated),
        }
    )
    return record


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _checkpoint_payload(
    audit_identity_sha256: str,
    record: Mapping[str, Any],
) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "audit_identity_sha256": audit_identity_sha256,
            "record": record,
        }
    )


def _checkpoint_path(
    output_dir: Path,
    expected: submitter.CampaignTask,
    record: Mapping[str, Any],
) -> tuple[Path, bytes, str]:
    payload = _checkpoint_payload(
        str(record["audit_identity_sha256"]),
        record,
    )
    checkpoint_sha256 = sha256_bytes(payload)
    filename = (
        f"{expected.safe_case_id}.task-{int(record['selected_task_id'])}."
        f"{checkpoint_sha256}.canonical.json"
    )
    return output_dir / CHECKPOINT_DIR_NAME / filename, payload, checkpoint_sha256


def _publish_immutable(path: Path, payload: bytes) -> str:
    """Publish canonical bytes once; an exact existing file is idempotent."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _stable_read(path, "immutable result checkpoint") != payload:
            raise RuntimeError(f"immutable result checkpoint collision/tamper: {path}")
        return "already_present"
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".staged", dir=path.parent
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            atomic_publish.publish_no_replace(staged, path)
        except FileExistsError:
            if _stable_read(path, "raced immutable result checkpoint") != payload:
                raise RuntimeError(f"immutable result checkpoint collision/tamper: {path}")
            return "already_present"
        return "published"
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _checkpoint_reference(
    output_dir: Path,
    expected: submitter.CampaignTask,
    record: Mapping[str, Any],
) -> dict[str, str]:
    path, payload, checkpoint_sha256 = _checkpoint_path(output_dir, expected, record)
    return {
        "checkpoint": path.relative_to(output_dir).as_posix(),
        "checkpoint_sha256": checkpoint_sha256,
        "record_sha256": canonical_sha256(record),
        "payload_sha256": sha256_bytes(payload),
    }


def _checkpoint_references(
    output_dir: Path,
    evidence: CampaignEvidence,
    audited_results: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, dict[str, dict[str, str]]]:
    expected_by_case = {task.case_id: task for task in evidence.tasks}
    references: dict[str, dict[str, dict[str, str]]] = {}
    for case_id, by_task in audited_results.items():
        expected = expected_by_case.get(case_id)
        if expected is None:
            raise RuntimeError(f"audited result has no planned case: {case_id!r}")
        references[case_id] = {
            str(task_id): _checkpoint_reference(output_dir, expected, record)
            for task_id, record in sorted(by_task.items(), key=lambda item: int(item[0]))
        }
    return references


def _validate_checkpoint_record(
    record: Mapping[str, Any],
    *,
    expected: submitter.CampaignTask,
    task_id: int,
    audit_identity_sha256: str,
) -> None:
    exact = {
        "audit_identity_sha256": audit_identity_sha256,
        "case_id": expected.case_id,
        "dedupe_key": expected.dedupe_key,
        "task_name": expected.task_name,
        "selected_task_id": task_id,
        "result_csv": expected.result_csv,
    }
    mismatches = [key for key, value in exact.items() if record.get(key) != value]
    if mismatches:
        raise RuntimeError(
            f"immutable result checkpoint identity mismatch for case_id={expected.case_id!r}: "
            + ", ".join(mismatches)
        )
    for field in ("task_fingerprint", "result_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get(field) or "")):
            raise RuntimeError(f"immutable result checkpoint has invalid {field}")
    for field in ("result_bytes", "result_columns"):
        value = record.get(field)
        if type(value) is not int or value <= 0:
            raise RuntimeError(f"immutable result checkpoint has invalid {field}")
    if record.get("classification") not in {
        "physics_ok",
        "torque_unit_suspect",
        "physics_failed",
        "retryable_infrastructure_result",
        "simulation_failure_result",
    }:
        raise RuntimeError("immutable result checkpoint has invalid classification")
    issues = record.get("physics_issues")
    if not isinstance(issues, list) or any(not isinstance(item, str) for item in issues):
        raise RuntimeError("immutable result checkpoint has malformed physics_issues")


def _load_prior_evidence(
    output_dir: Path,
    evidence: CampaignEvidence,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Rebuild reusable evidence from immutable hash-named checkpoints.

    The replaceable aggregate may reference checkpoints, but can neither create
    nor alter reusable evidence.  A valid self-bound checkpoint left just before
    an interrupted aggregate update is recovered independently.
    """

    audit_identity_sha256 = canonical_sha256(evidence.identity)
    expected_by_case = {task.case_id: task for task in evidence.tasks}
    checkpoints_dir = output_dir / CHECKPOINT_DIR_NAME
    audited: dict[str, dict[str, dict[str, Any]]] = {}
    checkpoint_index: dict[tuple[str, str], dict[str, str]] = {}
    if checkpoints_dir.exists():
        if not checkpoints_dir.is_dir():
            raise RuntimeError("result checkpoint path is not a directory")
        for path in sorted(checkpoints_dir.iterdir(), key=lambda item: item.name):
            if not path.is_file():
                raise RuntimeError(f"orphan result checkpoint entry: {path}")
            match = CHECKPOINT_NAME_RE.fullmatch(path.name)
            if not match:
                raise RuntimeError(f"orphan result checkpoint filename: {path}")
            payload = _stable_read(path, "immutable result checkpoint")
            actual_sha256 = sha256_bytes(payload)
            if actual_sha256 != match.group("sha256"):
                raise RuntimeError(f"immutable result checkpoint hash/tamper mismatch: {path}")
            document = _decode_json(payload, "immutable result checkpoint")
            if canonical_json_bytes(document) != payload:
                raise RuntimeError(f"immutable result checkpoint is not canonical: {path}")
            if document.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
                raise RuntimeError(f"immutable result checkpoint schema mismatch: {path}")
            if document.get("audit_identity_sha256") != audit_identity_sha256:
                raise RuntimeError(f"orphan result checkpoint audit identity: {path}")
            record = document.get("record")
            if not isinstance(record, dict):
                raise RuntimeError(f"immutable result checkpoint record is malformed: {path}")
            case_id = str(record.get("case_id") or "")
            expected = expected_by_case.get(case_id)
            if expected is None:
                raise RuntimeError(f"orphan result checkpoint case_id: {path}")
            task_id = int(match.group("task_id"))
            expected_path, _, expected_sha256 = _checkpoint_path(output_dir, expected, record)
            if expected_path != path or expected_sha256 != actual_sha256:
                raise RuntimeError(f"immutable result checkpoint path collision: {path}")
            _validate_checkpoint_record(
                record,
                expected=expected,
                task_id=task_id,
                audit_identity_sha256=audit_identity_sha256,
            )
            key = (case_id, str(task_id))
            if key in checkpoint_index:
                raise RuntimeError(
                    f"multiple immutable checkpoints for case_id={case_id!r} task_id={task_id}"
                )
            reference = _checkpoint_reference(output_dir, expected, record)
            checkpoint_index[key] = reference
            audited.setdefault(case_id, {})[str(task_id)] = record

    receipt_path = output_dir / RECEIPT_NAME
    if not receipt_path.exists():
        return audited
    receipt_payload = _stable_read(receipt_path, "prior Stage2 audit receipt")
    receipt = _decode_json(receipt_payload, "prior receipt")
    if canonical_json_bytes(receipt) != receipt_payload:
        raise RuntimeError("prior receipt is not canonical or was tampered")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("prior receipt schema mismatch")
    if receipt.get("audit_identity") != evidence.identity:
        raise RuntimeError("prior receipt audit identity mismatch; use a fresh output directory")
    references = receipt.get("audited_results")
    if not isinstance(references, dict):
        raise RuntimeError("prior receipt audited_results references are malformed")
    for case_id, by_task in references.items():
        if not isinstance(case_id, str) or not isinstance(by_task, dict):
            raise RuntimeError("prior receipt checkpoint index is malformed")
        for task_id, reference in by_task.items():
            if not str(task_id).isdigit() or not isinstance(reference, dict):
                raise RuntimeError("prior receipt checkpoint reference is malformed")
            actual = checkpoint_index.get((case_id, str(task_id)))
            if actual is None:
                raise RuntimeError(
                    f"prior receipt references a missing immutable checkpoint: {case_id}:{task_id}"
                )
            if reference != actual:
                raise RuntimeError(
                    f"prior receipt immutable checkpoint reference tamper: {case_id}:{task_id}"
                )
    return audited


def _summary(
    observations: list[dict[str, Any]],
    *,
    plan_rows: int,
    audited_results: Mapping[str, Mapping[str, Any]],
    task_queries: int,
    remote_fetches: int,
    reused_results: int,
    rate_limit_retries: int,
) -> dict[str, Any]:
    classifications = Counter(str(item["classification"]) for item in observations)
    active_task_count = sum(
        classifications.get(status, 0) for status in sorted(ACTIVE_STATUSES)
    )
    successful_result_pending_count = classifications.get(
        "result_fetch_or_contract_pending", 0
    )
    coverage_complete = task_queries == plan_rows and len(observations) == plan_rows
    return {
        "plan_rows": plan_rows,
        "task_identity_queries": task_queries,
        "coverage_complete": coverage_complete,
        "active_task_count": active_task_count,
        "successful_result_pending_count": successful_result_pending_count,
        "replacement_set_ready_to_seal": bool(
            coverage_complete
            and active_task_count == 0
            and successful_result_pending_count == 0
        ),
        "existing_attempt_cases": sum(bool(item.get("attempt_ids")) for item in observations),
        "existing_attempts": sum(len(item.get("attempt_ids") or ()) for item in observations),
        "audited_result_versions": sum(len(value) for value in audited_results.values()),
        "selected_results_audited": sum(
            item["classification"]
            in {
                "physics_ok",
                "torque_unit_suspect",
                "physics_failed",
                "retryable_infrastructure_result",
                "simulation_failure_result",
            }
            for item in observations
        ),
        "remote_result_fetches_this_run": remote_fetches,
        "reused_results_this_run": reused_results,
        "http_429_retries_this_run": rate_limit_retries,
        "classifications": dict(sorted(classifications.items())),
    }


def _receipt(
    evidence: CampaignEvidence,
    audited_result_references: Mapping[str, Mapping[str, Any]],
    observations: list[dict[str, Any]],
    summary: Mapping[str, Any],
    *,
    pacing: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "audit_identity": evidence.identity,
        "audit_identity_sha256": canonical_sha256(evidence.identity),
        "scheduler_access": {
            "method": "GET only",
            "max_in_flight": 1,
            **pacing,
        },
        "summary": dict(summary),
        "observations": observations,
        # This replaceable index is progress/navigation only.  Resume authority
        # comes from independently scanned immutable hash-named checkpoints.
        "audited_results": audited_result_references,
    }


def _report(receipt: Mapping[str, Any], receipt_payload: bytes) -> dict[str, Any]:
    observations = receipt["observations"]
    by_classification: dict[str, list[str]] = {}
    for item in observations:
        classification = str(item["classification"])
        if classification != "physics_ok":
            by_classification.setdefault(classification, []).append(str(item["case_id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": receipt["updated_at"],
        "status": "scanned" if receipt["summary"]["coverage_complete"] else "partial",
        "audit_identity_sha256": receipt["audit_identity_sha256"],
        "receipt_sha256": sha256_bytes(receipt_payload),
        "summary": receipt["summary"],
        "case_ids_by_non_ok_classification": {
            key: sorted(values) for key, values in sorted(by_classification.items())
        },
    }


def audit_stage2(
    *,
    plan_path: Path,
    decision_path: Path,
    output_dir: Path,
    scheduler_url: str | None,
    publish: bool,
    client: PacedHttpReader,
    attempt_limit: int,
    max_result_bytes: int,
    expected_rows: int = EXPECTED_PLAN_ROWS,
) -> dict[str, Any]:
    if max_result_bytes < 1:
        raise RuntimeError("--max-result-bytes must be >= 1")
    evidence = load_campaign_evidence(
        plan_path,
        decision_path,
        expected_rows=expected_rows,
    )
    effective_scheduler_url = str(scheduler_url or evidence.runner_args.scheduler_url).rstrip("/")
    if not effective_scheduler_url.startswith(("http://", "https://")):
        raise RuntimeError("scheduler URL must use HTTP or HTTPS")
    receipt_path = output_dir / RECEIPT_NAME
    report_path = output_dir / REPORT_NAME
    audited_results = _load_prior_evidence(output_dir, evidence)
    audit_identity_sha256 = canonical_sha256(evidence.identity)
    observations: list[dict[str, Any]] = []
    seen_task_ids: dict[int, str] = {}
    task_queries = 0
    remote_fetches = 0
    reused_results = 0
    initial_429_retries = client.rate_limit_retries
    pacing = {
        "pace_seconds": client.pace_seconds,
        "backoff_seconds": client.backoff_seconds,
        "max_backoff_seconds": client.max_backoff_seconds,
        "max_429_retries": client.max_429_retries,
        "attempt_query_limit": attempt_limit,
        "max_result_bytes": max_result_bytes,
    }

    def publish_checkpoint() -> None:
        if not publish:
            return
        summary = _summary(
            observations,
            plan_rows=expected_rows,
            audited_results=audited_results,
            task_queries=task_queries,
            remote_fetches=remote_fetches,
            reused_results=reused_results,
            rate_limit_retries=client.rate_limit_retries - initial_429_retries,
        )
        current = _receipt(
            evidence,
            _checkpoint_references(output_dir, evidence, audited_results),
            observations,
            summary,
            pacing=pacing,
        )
        _atomic_write(receipt_path, canonical_json_bytes(current))

    for plan_row, expected in zip(evidence.rows, evidence.tasks, strict=True):
        attempts = query_exact_attempts(
            client,
            scheduler_url=effective_scheduler_url,
            expected=expected,
            attempt_limit=attempt_limit,
        )
        task_queries += 1
        for attempt in attempts:
            task_id = _task_id(attempt)
            previous = seen_task_ids.setdefault(task_id, expected.case_id)
            if previous != expected.case_id:
                raise RuntimeError(
                    f"scheduler task ID {task_id} resolves to multiple planned cases"
                )
        state, selected = select_attempt(attempts)
        observation: dict[str, Any] = {
            "case_id": expected.case_id,
            "dedupe_key": expected.dedupe_key,
            "task_name": expected.task_name,
            "attempt_ids": [_task_id(item) for item in attempts],
            "selected_task_id": _task_id(selected) if selected is not None else None,
            "classification": state,
        }
        if state == "retryable_infrastructure" and selected is not None:
            observation["infrastructure"] = {
                "status": str(selected.get("status") or "").strip().lower(),
                "exit_code": _exit_code(selected),
                "failure_message": _safe_text(selected.get("failure_message")),
            }
        if state != "scheduler_success" or selected is None:
            observations.append(observation)
            continue

        task_id = _task_id(selected)
        prior = audited_results.get(expected.case_id, {}).get(str(task_id))
        if prior is not None:
            if (
                prior.get("dedupe_key") != expected.dedupe_key
                or prior.get("task_fingerprint") != task_fingerprint(selected)
                or prior.get("result_csv") != expected.result_csv
            ):
                raise RuntimeError(
                    f"prior audited result drift for case_id={expected.case_id!r} task_id={task_id}"
                )
            record = prior
            reused_results += 1
        else:
            url = _result_url(
                effective_scheduler_url,
                task_id,
                expected.result_csv,
                max_result_bytes,
            )
            try:
                payload = client.get_bytes(url, max_bytes=max_result_bytes)
                remote_fetches += 1
                if not payload:
                    raise RuntimeError("remote result CSV is empty")
                record = audit_result_payload(
                    payload,
                    expected=expected,
                    plan_row=plan_row,
                    selected_task=selected,
                )
                record["audit_identity_sha256"] = audit_identity_sha256
                _validate_checkpoint_record(
                    record,
                    expected=expected,
                    task_id=task_id,
                    audit_identity_sha256=audit_identity_sha256,
                )
            except (OSError, RuntimeError, UnicodeError, csv.Error) as exc:
                observation["classification"] = "result_fetch_or_contract_pending"
                observation["result_error"] = _safe_text(exc)
                observations.append(observation)
                continue
            if publish:
                checkpoint_path, checkpoint_payload, _ = _checkpoint_path(
                    output_dir, expected, record
                )
                _publish_immutable(checkpoint_path, checkpoint_payload)
            case_versions = audited_results.setdefault(expected.case_id, {})
            case_versions[str(task_id)] = record
            publish_checkpoint()
        observation["classification"] = record["classification"]
        observation["result_sha256"] = record["result_sha256"]
        observation["physics_issues"] = record["physics_issues"]
        observations.append(observation)

    summary = _summary(
        observations,
        plan_rows=expected_rows,
        audited_results=audited_results,
        task_queries=task_queries,
        remote_fetches=remote_fetches,
        reused_results=reused_results,
        rate_limit_retries=client.rate_limit_retries - initial_429_retries,
    )
    receipt = _receipt(
        evidence,
        _checkpoint_references(output_dir, evidence, audited_results),
        observations,
        summary,
        pacing=pacing,
    )
    receipt_payload = canonical_json_bytes(receipt)
    report = _report(receipt, receipt_payload)
    report_payload = canonical_json_bytes(report)
    if publish:
        _atomic_write(receipt_path, receipt_payload)
        _atomic_write(report_path, report_payload)
        publication = "published"
    else:
        publication = "would_publish"
    return {
        "status": report["status"],
        "mode": "publish" if publish else "dry-run",
        "publication": publication,
        "summary": summary,
        "receipt_sha256": sha256_bytes(receipt_payload),
        "report_sha256": sha256_bytes(report_payload),
        "receipt_path": receipt_path.as_posix(),
        "report_path": report_path.as_posix(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--decision", type=Path, default=DEFAULT_DECISION)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--scheduler-url",
        help="Read-only scheduler base URL; defaults to the sealed decision value.",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--pace-seconds", type=float, default=1.0)
    parser.add_argument("--backoff-seconds", type=float, default=2.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=30.0)
    parser.add_argument("--max-429-retries", type=int, default=5)
    parser.add_argument("--attempt-limit", type=int, default=20)
    parser.add_argument("--max-result-bytes", type=int, default=2_000_000)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish/update only the compact local receipt and report; default writes nothing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = PacedHttpReader(
        timeout=args.timeout,
        pace_seconds=args.pace_seconds,
        backoff_seconds=args.backoff_seconds,
        max_backoff_seconds=args.max_backoff_seconds,
        max_429_retries=args.max_429_retries,
    )
    result = audit_stage2(
        plan_path=args.plan,
        decision_path=args.decision,
        output_dir=args.output_dir,
        scheduler_url=args.scheduler_url,
        publish=args.publish,
        client=client,
        attempt_limit=args.attempt_limit,
        max_result_bytes=args.max_result_bytes,
    )
    summary = result["summary"]
    print(
        "stage2_v4r3_physics_audit "
        + " ".join(
            (
                f"mode={result['mode']}",
                f"status={result['status']}",
                f"coverage={summary['task_identity_queries']}/{summary['plan_rows']}",
                f"selected_results={summary['selected_results_audited']}",
                f"remote_fetches={summary['remote_result_fetches_this_run']}",
                f"reused={summary['reused_results_this_run']}",
                f"publication={result['publication']}",
                f"receipt={result['receipt_path']}",
            )
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
