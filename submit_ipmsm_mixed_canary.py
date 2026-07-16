"""Prepare or submit the operator-authorized MFT2 + IPMSM1 AEDT canary.

Dry-run is the default.  Execution is intentionally one shot: it first asks
the bootstrap-protected mixed-canary admission endpoint to reserve one empty
three-slot AEDT session, creates exactly the three scheduler task rows bound
to the returned capability dedupe keys, attests those rows, and only then
releases their remote start gates.

The mixed-canary admission is the exact placement authority.  It must never be
combined with ``/api/aedt-pool/session-reservations``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any, Iterable
import urllib.error
import urllib.request


BASE_DIR = Path(__file__).resolve().parent
AUTHORITY_PATH = BASE_DIR / "mixed_aedt_canary_authority_v1.json"
API_URL = "http://127.0.0.1:8001"
REMOTE_SCHEDULER_URL = "http://172.16.10.37:18790"
DB_PATH = Path(r"C:\Users\peets\slurm_scheduler_runtime\data\slurm_scheduler.db")
BOOTSTRAP_TOKEN_PATH = Path(
    r"C:\Users\peets\slurm_scheduler_runtime\aedt_pool_bootstrap.token"
)
SCHEDULER_SOURCE = Path(r"Y:\git\slurm_scheduler_family_recovery_260715")
ACCOUNTS_PATH = Path(r"Y:\runtime\slurm_scheduler\config\accounts.yaml")

SCHEDULER_CONTROL_PLANE_SHA = "9562c6f2f66b75954c6f3276bc30f8e2088b30b3"
SCHEDULER_CLIENT_SHA = "9150e7fa7f72fdf00fb8113e157398b410833c40"
MFT_SOLVER_OLD_SHA = "c609ee52e717c650f70f73c23ee524ad8dec5aa3"
MFT_SOLVER_SHA = "c7a0c792e2babc74ad1596a6b95b45379a6f903d"
PYAEDT_LIBRARY_SHA = "e6b9b9d20a832ff5c3f7ca97218737a0b8650781"
MOTOR_REPOSITORY = "https://github.com/Schwalbe262/pyaedt_motor.git"
LIBRARY_REPOSITORY = "https://github.com/Schwalbe262/pyaedt_library.git"
MFT_SOURCE_TASK_IDS = (41696, 41697)
MOTOR_SOURCE_TASK_ID = 34762
Q21_NAME_PREFIX = "mft-3x3-q21-260716a-"
Q21_CANARY = "mft-9way-q21-exact-three-session-barrier"
RUN_LABEL = "q22-260716"
CLIENT_ACCOUNT = "harry261"
CLIENT_NODE = "n109"
GATE_TIMEOUT_SECONDS = 1800
LOCK_TIMEOUT_SECONDS = 7200
BARRIER_TIMEOUT_SECONDS = 7200
RELEASE_WAIT_SECONDS = 7200
ADMISSION_TTL_SECONDS = 3600
HEARTBEAT_MAX_AGE_SECONDS = 90.0
LIVE_LEASE_STATES = (
    "queued",
    "offered",
    "leased",
    "attaching",
    "active",
    "releasing",
)


class MixedCanaryError(RuntimeError):
    """The mixed canary could not be prepared without weakening a gate."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalized_sha256(path: Path) -> str:
    payload = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()


def load_authority(path: Path = AUTHORITY_PATH) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise MixedCanaryError("mixed-canary authority must be a JSON object")
    recorded = str(document.get("authority_sha256") or "")
    unsigned = {key: value for key, value in document.items() if key != "authority_sha256"}
    observed = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if recorded != observed:
        raise MixedCanaryError(
            f"mixed-canary authority seal mismatch: expected={recorded}, observed={observed}"
        )
    return document


def verify_runtime_closure(
    root: Path = BASE_DIR,
    authority_path: Path = AUTHORITY_PATH,
) -> dict[str, str]:
    authority = load_authority(authority_path)
    closure = authority.get("motor_runtime_closure")
    if not isinstance(closure, dict) or not closure:
        raise MixedCanaryError("motor runtime closure is absent")
    observed: dict[str, str] = {}
    for relative, expected in sorted(closure.items()):
        target = (root / relative).resolve()
        if root.resolve() not in target.parents:
            raise MixedCanaryError(f"runtime path escaped repository: {relative}")
        if not target.is_file():
            raise MixedCanaryError(f"runtime source is missing: {relative}")
        digest = normalized_sha256(target)
        if digest != expected:
            raise MixedCanaryError(
                f"runtime source hash mismatch for {relative}: "
                f"expected={expected}, observed={digest}"
            )
        observed[relative] = digest
    if "module/aedt_automation_lock.py" not in observed:
        raise MixedCanaryError("runtime closure omitted module/aedt_automation_lock.py")
    return observed


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def parse_sql_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def rows_by_ids(
    connection: sqlite3.Connection,
    table: str,
    ids: Iterable[int],
) -> list[dict[str, Any]]:
    values = tuple(int(item) for item in ids)
    if not values:
        return []
    if table not in {"tasks", "aedt_project_leases"}:
        raise ValueError("unsupported read-only table")
    marks = ",".join("?" for _ in values)
    return [dict(row) for row in connection.execute(
        f"SELECT * FROM {table} WHERE id IN ({marks}) ORDER BY id", values
    )]


def source_tasks(connection: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    mft = rows_by_ids(connection, "tasks", MFT_SOURCE_TASK_IDS)
    motor = rows_by_ids(connection, "tasks", (MOTOR_SOURCE_TASK_ID,))
    if [int(item["id"]) for item in mft] != list(MFT_SOURCE_TASK_IDS):
        raise MixedCanaryError("exact MFT seed tasks are missing")
    if len(motor) != 1:
        raise MixedCanaryError("exact motor seed task is missing")
    return mft, motor[0]


def q21_terminal_evidence(connection: sqlite3.Connection) -> dict[str, Any]:
    tasks = [dict(row) for row in connection.execute(
        """
        SELECT id, name, status, exit_code, account_name, node_name,
               allocation_id, payload_json
        FROM tasks WHERE name LIKE ? ORDER BY id
        """,
        (f"{Q21_NAME_PREFIX}%",),
    )]
    valid_tasks: list[dict[str, Any]] = []
    for task in tasks:
        try:
            payload = json.loads(str(task.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            continue
        if payload.get("canary") == Q21_CANARY:
            valid_tasks.append({**task, "payload": payload})
    task_ids = [int(item["id"]) for item in valid_tasks]
    leases: list[dict[str, Any]] = []
    if task_ids:
        marks = ",".join("?" for _ in task_ids)
        leases = [dict(row) for row in connection.execute(
            f"""
            SELECT id, task_id, session_id, state, solve_permit_generation,
                   native_pipeline_completed_at, failure_message
            FROM aedt_project_leases
            WHERE task_id IN ({marks}) ORDER BY task_id
            """,
            tuple(task_ids),
        )]
    tasks_ok = (
        len(valid_tasks) == 9
        and len({int(item["id"]) for item in valid_tasks}) == 9
        and all(
            item["status"] == "completed"
            and item["exit_code"] is not None
            and int(item["exit_code"]) == 0
            for item in valid_tasks
        )
    )
    leases_ok = (
        len(leases) == 9
        and {int(item["task_id"]) for item in leases} == set(task_ids)
        and all(
            item["state"] == "released"
            and int(item["solve_permit_generation"] or 0) > 0
            and bool(item["native_pipeline_completed_at"])
            and not str(item["failure_message"] or "").strip()
            for item in leases
        )
    )
    sessions: dict[int, int] = {}
    for item in leases:
        session_id = int(item["session_id"] or 0)
        sessions[session_id] = sessions.get(session_id, 0) + 1
    cohorts_ok = len(sessions) == 3 and set(sessions.values()) == {3}
    clients = sorted(
        {
            (str(item["account_name"] or ""), str(item["node_name"] or ""))
            for item in valid_tasks
            if item["account_name"] and item["node_name"]
        }
    )
    return {
        "ready": bool(tasks_ok and leases_ok and cohorts_ok),
        "task_count": len(valid_tasks),
        "task_status_counts": {
            status: sum(1 for item in valid_tasks if item["status"] == status)
            for status in sorted({str(item["status"]) for item in valid_tasks})
        },
        "lease_count": len(leases),
        "session_project_counts": sessions,
        "client_locations": clients,
        "task_ids": task_ids,
    }


def candidate_sessions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    marks = ",".join("?" for _ in LIVE_LEASE_STATES)
    rows = connection.execute(
        f"""
        SELECT s.id, s.generation, s.state, s.slots_total, s.last_heartbeat_at,
               s.session_profile, s.allocation_id, s.account_name, s.node_name,
               s.endpoint, s.artifact_dir, s.last_fault_evidence_json
        FROM aedt_sessions s
        JOIN allocations a ON a.id = s.allocation_id
        WHERE s.state = 'ready'
          AND s.slots_total = 3
          AND s.solve_batch_sealed_at IS NULL
          AND s.drain_requested_at IS NULL
          AND COALESCE(s.failure_message, '') = ''
          AND a.state = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM aedt_project_leases l
              WHERE l.session_id = s.id AND l.state IN ({marks})
          )
          AND NOT EXISTS (
              SELECT 1 FROM aedt_exact_session_reservations r
              WHERE r.session_id = s.id AND r.state IN ('reserved','claimed')
          )
          AND NOT EXISTS (
              SELECT 1 FROM aedt_mixed_canary_admissions ca
              WHERE ca.session_id = s.id AND ca.state IN ('open','filled')
          )
        ORDER BY s.id
        """,
        LIVE_LEASE_STATES,
    ).fetchall()
    now = datetime.now(timezone.utc)
    expected_profile = load_authority()["session_profile"]["value"]
    result = []
    for raw in rows:
        item = dict(raw)
        age = (now - parse_sql_time(str(item["last_heartbeat_at"]))).total_seconds()
        try:
            profile = json.loads(str(item["session_profile"] or "{}"))
            fault_evidence = json.loads(str(item["last_fault_evidence_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        if (
            age <= HEARTBEAT_MAX_AGE_SECONDS
            and profile == expected_profile
            and not fault_evidence
        ):
            item["heartbeat_age_seconds"] = round(age, 3)
            result.append(item)
    return result


def latest_mixed_validation(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT id, status, mixed_mft_ipmsm_isolation_passed, created_at
        FROM aedt_pool_validations ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else {}


def local_scheduler_head() -> str:
    result = subprocess.run(
        ["git", "-C", str(SCHEDULER_SOURCE), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def health() -> dict[str, Any]:
    request = urllib.request.Request(f"{API_URL}/api/health", method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise MixedCanaryError("scheduler health returned non-object JSON")
    return result


def prepare(
    *,
    requested_session_id: int = 0,
    motor_git_ref: str = "",
) -> dict[str, Any]:
    closure = verify_runtime_closure()
    with database() as connection:
        q21 = q21_terminal_evidence(connection)
        candidates = candidate_sessions(connection)
        validation = latest_mixed_validation(connection)
        mft_sources, motor_source = source_tasks(connection)
    selected = None
    if requested_session_id:
        selected = next(
            (item for item in candidates if int(item["id"]) == requested_session_id),
            None,
        )
    elif candidates:
        selected = candidates[0]
    blockers = []
    if local_scheduler_head() != SCHEDULER_CONTROL_PLANE_SHA:
        blockers.append("local scheduler control-plane commit is not the sealed live commit")
    scheduler_health = health()
    if not scheduler_health.get("ok") or not scheduler_health.get("scheduler_ok"):
        blockers.append("scheduler health is not operational")
    if not q21["ready"]:
        blockers.append("q21 3x3 terminal/native-barrier evidence is incomplete")
    if q21["client_locations"] != [(CLIENT_ACCOUNT, CLIENT_NODE)]:
        blockers.append(
            "q21 client location differs from the independently verified scheduler package"
        )
    if bool(validation.get("mixed_mft_ipmsm_isolation_passed")):
        blockers.append("mixed isolation is already validated; one-shot canary admission is closed")
    if requested_session_id and selected is None:
        blockers.append(f"requested session {requested_session_id} is not eligible")
    if selected is None:
        blockers.append("no empty healthy active-allocation three-slot session is eligible")
    payload_preflight: list[dict[str, Any]] = []
    if not re.fullmatch(r"[0-9a-f]{40}", motor_git_ref):
        blockers.append("dry-run requires an exact 40-character --motor-git-ref")
    else:
        head = subprocess.run(
            ["git", "-C", str(BASE_DIR), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if head != motor_git_ref:
            blockers.append(
                "--motor-git-ref is not the exact deployment checkout HEAD"
            )
        dirty = subprocess.run(
            ["git", "-C", str(BASE_DIR), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if dirty:
            blockers.append("motor deployment checkout has tracked modifications")
    if selected is not None and re.fullmatch(r"[0-9a-f]{40}", motor_git_ref):
        try:
            session_id = int(selected["id"])
            for ordinal, source in enumerate(mft_sources):
                payload, token = mft_payload(
                    source,
                    dedupe_key=f"preflight-mft-{ordinal}",
                    ordinal=ordinal,
                    session_id=session_id,
                    account_name=CLIENT_ACCOUNT,
                    node_name=CLIENT_NODE,
                    motor_git_ref=motor_git_ref,
                )
                payload_preflight.append({
                    "family": "mft",
                    "name": payload["name"],
                    "gate_token": token,
                    "aedt_backend": payload["aedt_backend"],
                    "isolation_policy": "shared_if_compatible",
                })
            payload, token = motor_payload(
                motor_source,
                dedupe_key="preflight-ipmsm-0",
                session_id=session_id,
                account_name=CLIENT_ACCOUNT,
                node_name=CLIENT_NODE,
                motor_git_ref=motor_git_ref,
                profile_export=canonical_profile_export(mft_sources[0]),
            )
            payload_preflight.append({
                "family": "ipmsm",
                "name": payload["name"],
                "gate_token": token,
                "aedt_backend": payload["aedt_backend"],
                "isolation_policy": "shared_if_compatible",
            })
        except MixedCanaryError as exc:
            blockers.append(f"payload template preflight failed: {exc}")
    return {
        "schema_version": "mft-ipmsm-mixed-canary-preflight-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_ready": not blockers,
        "blockers": blockers,
        "q21": q21,
        "selected_session": selected,
        "candidate_sessions": [
            {
                key: item[key]
                for key in (
                    "id", "generation", "state", "allocation_id", "account_name",
                    "node_name", "endpoint", "heartbeat_age_seconds",
                )
            }
            for item in candidates
        ],
        "latest_validation": validation,
        "scheduler": {
            "api_url": API_URL,
            "remote_client_url": REMOTE_SCHEDULER_URL,
            "control_plane_commit": SCHEDULER_CONTROL_PLANE_SHA,
            "client_package_commit": SCHEDULER_CLIENT_SHA,
            "health": {
                key: scheduler_health.get(key)
                for key in ("ok", "scheduler_ok", "scheduler_thread_alive", "scheduler_stalled")
            },
        },
        "runtime_closure": closure,
        "motor_git_ref": motor_git_ref,
        "payload_preflight": payload_preflight,
        "placement_authority": "mixed-canary-admission",
        "exact_session_reservation_allowed": False,
    }


def request_json(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    bootstrap: bool = False,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if bootstrap:
        token = BOOTSTRAP_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if not token:
            raise MixedCanaryError("AEDT bootstrap token file is empty")
        headers["X-AEDT-Bootstrap-Token"] = token
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=json.dumps(payload or {}, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise MixedCanaryError(f"POST {path} failed HTTP {exc.code}: {detail}") from exc
    if not isinstance(result, dict):
        raise MixedCanaryError(f"POST {path} returned non-object JSON")
    return result


def replace_required(value: str, old: str, new: str, label: str) -> str:
    if old not in value:
        raise MixedCanaryError(f"{label} marker is missing: {old!r}")
    return value.replace(old, new)


def gate_token(
    family: str,
    ordinal: int,
    session_id: int,
    motor_git_ref: str,
    dedupe_key: str,
) -> str:
    # The admission capability makes a retry unique even if a previous
    # attempt left an unconsumed release file on the same session.
    material = (
        f"{RUN_LABEL}:{family}:{ordinal}:{session_id}:{motor_git_ref}:{dedupe_key}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def gate_prefix(token: str) -> str:
    return (
        'MIXED_GATE_DIR="$HOME/slurm_scheduler/mixed-canary-gates"; '
        f'MIXED_RELEASE_FILE="$MIXED_GATE_DIR/{token}.release"; '
        'mkdir -p "$MIXED_GATE_DIR"; '
        f'MIXED_GATE_DEADLINE=$(( $(date +%s) + {GATE_TIMEOUT_SECONDS} )); '
        'while [ ! -f "$MIXED_RELEASE_FILE" ]; do '
        'if [ "$(date +%s)" -ge "$MIXED_GATE_DEADLINE" ]; then '
        "printf 'mixed canary release gate timed out\\n' >&2; exit 75; fi; sleep 1; done; "
        'rm -f -- "$MIXED_RELEASE_FILE"; '
        f'export AEDT_POOL_AUTOMATION_LOCK_TIMEOUT_SECONDS="{LOCK_TIMEOUT_SECONDS}"; '
        f'export AEDT_POOL_NATIVE_PIPELINE_BARRIER_TIMEOUT_SECONDS="{BARRIER_TIMEOUT_SECONDS}"; '
    )


def pooled_env_from_mft(source: dict[str, Any]) -> str:
    env_setup = str(source.get("env_setup") or "")
    marker = 'export MFT_AEDT_BACKEND="pooled"'
    position = env_setup.find(marker)
    if position < 0:
        raise MixedCanaryError(f"MFT source task {source['id']} lacks pooled environment")
    pooled = env_setup[position:]
    pooled = replace_required(
        pooled,
        'export MFT_AEDT_ISOLATION_POLICY="family"',
        'export MFT_AEDT_ISOLATION_POLICY="shared_if_compatible"',
        f"MFT source task {source['id']} isolation policy",
    )
    return pooled.rstrip() + "\n" + "\n".join((
        f'export AEDT_POOL_AUTOMATION_LOCK_TIMEOUT_SECONDS="{LOCK_TIMEOUT_SECONDS}"',
        f'export AEDT_POOL_NATIVE_PIPELINE_BARRIER_TIMEOUT_SECONDS="{BARRIER_TIMEOUT_SECONDS}"',
        f'export MFT_AEDT_RELEASE_WAIT_SECONDS="{RELEASE_WAIT_SECONDS}"',
    ))


def canonical_profile_export(source: dict[str, Any]) -> str:
    for line in str(source.get("env_setup") or "").splitlines():
        if line.startswith("export MFT_AEDT_SESSION_PROFILE="):
            return line
    raise MixedCanaryError(f"MFT source task {source['id']} lacks canonical profile")


def mft_payload(
    source: dict[str, Any],
    *,
    dedupe_key: str,
    ordinal: int,
    session_id: int,
    account_name: str,
    node_name: str,
    motor_git_ref: str,
) -> tuple[dict[str, Any], str]:
    row_match = re.search(r"r(\d+)$", str(source.get("name") or ""))
    if not row_match:
        raise MixedCanaryError(f"cannot recover MFT source row from {source.get('name')!r}")
    row_id = row_match.group(1)
    old_campaign = f"mft_1to3_q8_cap_r{row_id}"
    new_campaign = f"mft_mixed_{RUN_LABEL}_s{session_id}_{ordinal}_r{row_id}"
    token = gate_token("mft", ordinal, session_id, motor_git_ref, dedupe_key)
    command = str(source.get("command") or "")
    command = replace_required(
        command, MFT_SOLVER_OLD_SHA, MFT_SOLVER_SHA,
        f"MFT source task {source['id']} solver commit",
    )
    command = command.replace(MFT_SOLVER_OLD_SHA[:12], MFT_SOLVER_SHA[:12])
    command = replace_required(
        command, old_campaign, new_campaign,
        f"MFT source task {source['id']} campaign",
    )
    command = replace_required(
        command,
        'export MFT_AEDT_ISOLATION_POLICY="family"',
        'export MFT_AEDT_ISOLATION_POLICY="shared_if_compatible"',
        f"MFT source task {source['id']} command isolation policy",
    )
    old_workdir = re.search(r"-t([0-9a-f]{16})", command)
    if not old_workdir:
        raise MixedCanaryError(f"MFT source task {source['id']} lacks workdir token")
    command = command.replace(
        f"-t{old_workdir.group(1)}",
        f"-t{token[:16]}",
    )
    command = gate_prefix(token) + command
    cleanup = replace_required(
        str(source.get("cleanup_globs") or ""), old_campaign, new_campaign,
        f"MFT source task {source['id']} cleanup campaign",
    )
    cleanup = cleanup.replace(MFT_SOLVER_OLD_SHA[:12], MFT_SOLVER_SHA[:12])
    cleanup = cleanup.replace(
        f"-t{old_workdir.group(1)}",
        f"-t{token[:16]}",
    )
    metadata = {
        "canary": "mixed-mft-ipmsm-2plus1-native-barrier-q22",
        "session_expected": session_id,
        "family": "mft",
        "source_task_id": int(source["id"]),
        "source_row": int(row_id),
        "solver_git_hash": MFT_SOLVER_SHA,
        "scheduler_git_hash": SCHEDULER_CONTROL_PLANE_SHA,
        "scheduler_client_git_hash": SCHEDULER_CLIENT_SHA,
        "library_git_hash": PYAEDT_LIBRARY_SHA,
        "motor_peer_git_hash": motor_git_ref,
        "stage_profile": {"matrix_on": 1, "cap_on": 1, "loss_on": 0, "thermal_on": 0},
        "placement_authority": "mixed-canary-admission",
        "exact_session_reservation": False,
        "automation_lock_timeout_seconds": LOCK_TIMEOUT_SECONDS,
        "native_pipeline_barrier_timeout_seconds": BARRIER_TIMEOUT_SECONDS,
        "release_wait_seconds": RELEASE_WAIT_SECONDS,
    }
    return ({
        "name": f"mft-mixed-{RUN_LABEL}-s{session_id}-{ordinal}-r{row_id}",
        "remote_cwd": source["remote_cwd"],
        "command": command,
        "env_setup": pooled_env_from_mft(source),
        "required_capability": source["required_capability"],
        "env_profile": source["env_profile"],
        "account_name": account_name,
        "node_name": node_name,
        "cpus": int(source["cpus"]),
        "memory_mb": int(source["memory_mb"]),
        "scheduling_profile": source["scheduling_profile"],
        "aedt_backend": "pooled",
        "gpus": int(source["gpus"]),
        "partition": source["partition"],
        "priority": 100,
        "timeout_seconds": max(43200, int(source["timeout_seconds"])),
        "dedupe_key": dedupe_key,
        "max_workers_per_node": 0,
        "cleanup_globs": cleanup,
        "project": "MFT_1MW_2026v1",
        "payload_json": metadata,
    }, token)


def motor_case_bootstrap(source: dict[str, Any]) -> str:
    env_setup = str(source.get("env_setup") or "")
    position = env_setup.find("mkdir -p remote/")
    if position < 0 or "IPMSM_CASES_CSV" not in env_setup[position:]:
        raise MixedCanaryError("motor source task lacks exact case bootstrap")
    return env_setup[position:].strip()


def motor_payload(
    source: dict[str, Any],
    *,
    dedupe_key: str,
    session_id: int,
    account_name: str,
    node_name: str,
    motor_git_ref: str,
    profile_export: str,
) -> tuple[dict[str, Any], str]:
    case_id = "v2s3_final_audit_0020_rated_power_at_max_speed_03"
    case_path = f"remote/ipmsm_v2_foundation_s3_v4r4/{case_id}.csv"
    result_path = f"simul_log/mixed_{RUN_LABEL}/{case_id}.csv"
    simulation_dir = f"simulation/mixed_{RUN_LABEL}/{case_id}"
    log_dir = f"simul_log_scheduler/mixed_{RUN_LABEL}_logs"
    token = gate_token("ipmsm", 0, session_id, motor_git_ref, dedupe_key)
    command = "\n".join((
        gate_prefix(token),
        "git_root=__SLURM_SCHEDULER_ACCOUNT_WORKSPACE__/git_tasks",
        'mkdir -p "$git_root"',
        'workdir="$git_root/task-__SLURM_SCHEDULER_TASK_ID__"',
        'rm -rf "$workdir"',
        'mkdir -p "$workdir"',
        f'git clone -q --depth 1 {LIBRARY_REPOSITORY} "$workdir/pyaedt_library"',
        f'git -C "$workdir/pyaedt_library" fetch -q origin {PYAEDT_LIBRARY_SHA}',
        f'git -C "$workdir/pyaedt_library" checkout -q --detach {PYAEDT_LIBRARY_SHA}',
        'test -z "$(git -C "$workdir/pyaedt_library" status --porcelain --untracked-files=all)"',
        f'test "$(git -C "$workdir/pyaedt_library" rev-parse HEAD)" = "{PYAEDT_LIBRARY_SHA}"',
        f'git clone -q --depth 1 {MOTOR_REPOSITORY} "$workdir/repo"',
        f'git -C "$workdir/repo" fetch -q origin {motor_git_ref}',
        f'git -C "$workdir/repo" checkout -q --detach {motor_git_ref}',
        'test -z "$(git -C "$workdir/repo" status --porcelain --untracked-files=all)"',
        f'test "$(git -C "$workdir/repo" rev-parse HEAD)" = "{motor_git_ref}"',
        'cd "$workdir/repo"',
        "python submit_ipmsm_mixed_canary.py --verify-runtime-only",
        f"printf 'MOTOR_GIT_HASH {motor_git_ref}\\n'",
        f"printf 'PYAEDT_LIBRARY_GIT_HASH {PYAEDT_LIBRARY_SHA}\\n'",
        motor_case_bootstrap(source),
        (
            "python subprocess_run.py "
            f"--cases {case_path} --processes 1 --cores-per-process 4 "
            "--max-cases 1 --stagger-seconds 0.0 "
            f"--simulation-dir {simulation_dir} --result-csv {result_path} "
            f"--log-dir {log_dir} --log-prefix mixed_{RUN_LABEL}_ --analyze"
        ),
        "simulation_rc=$?",
        'if [ "$simulation_rc" -ne 0 ]; then exit "$simulation_rc"; fi',
        (
            "python -c \"import csv,sys; "
            f"rows=list(csv.DictReader(open('{result_path}',newline=''))); "
            "r=rows[-1] if rows else {}; "
            "ok=(r.get('status','').strip().lower()=='ok' and "
            "r.get('analysis_returned_false','').strip().lower() not in "
            "{'true','1','yes'} and not r.get('missing_required_outputs','').strip() "
            "and not r.get('error','').strip() and "
            "int(r.get('pooled_native_pipeline_completed_count') or 0)==3 and "
            "int(r.get('pooled_native_pipeline_expected_count') or 0)==3); "
            "print('MOTOR_RESULT_GUARD', 'ok' if ok else 'failed', "
            "r.get('status',''), r.get('pooled_native_pipeline_completed_count',''), "
            "r.get('pooled_native_pipeline_expected_count','')); "
            "sys.exit(0 if ok else 3)\""
        ),
    ))
    env_setup = "\n".join((
        "source /etc/profile.d/lmod.sh 2>/dev/null || true",
        "module load ansys-electronics/v252 2>/dev/null || export ANSYSEM_ROOT252=/opt/ohpc/pub/Electronics/v252/Linux64",
        "export FLEXLM_TIMEOUT=3000000",
        'if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then source "$HOME/miniconda3/etc/profile.d/conda.sh"; elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then source "$HOME/anaconda3/etc/profile.d/conda.sh"; fi',
        "conda activate pyaedt2026v1",
        'export MFT_AEDT_BACKEND="pooled"',
        'export MFT_AEDT_ISOLATION_POLICY="shared_if_compatible"',
        f'export MFT_AEDT_LEASE_WAIT_SECONDS="{GATE_TIMEOUT_SECONDS}"',
        f'export MFT_AEDT_POOL_FILL_TIMEOUT_SECONDS="{GATE_TIMEOUT_SECONDS // 2}"',
        'export MFT_AEDT_POOL_WORKSPACE_ROOT="/gpfs/tmp_cpu2/mft_pool"',
        'export MFT_AEDT_WORKSPACE_PATH="/gpfs/tmp_cpu2/mft_pool/ipmsm-${SLURM_SCHED_TASK_ID}"',
        f'export MFT_AEDT_SCHEDULER_URL="{REMOTE_SCHEDULER_URL}"',
        profile_export,
        'export MFT_AEDT_SESSION_VERSION="2025.2"',
        f'export AEDT_POOL_AUTOMATION_LOCK_TIMEOUT_SECONDS="{LOCK_TIMEOUT_SECONDS}"',
        f'export AEDT_POOL_NATIVE_PIPELINE_BARRIER_TIMEOUT_SECONDS="{BARRIER_TIMEOUT_SECONDS}"',
        f'export MFT_AEDT_RELEASE_WAIT_SECONDS="{RELEASE_WAIT_SECONDS}"',
        'export SLURM_AEDT_POOL_CLIENT_TOKEN_FILE="$HOME/slurm_scheduler/aedt_pool_client"',
    ))
    metadata = {
        "canary": "mixed-mft-ipmsm-2plus1-native-barrier-q22",
        "session_expected": session_id,
        "family": "ipmsm",
        "source_task_id": MOTOR_SOURCE_TASK_ID,
        "source_case_id": case_id,
        "motor_git_hash": motor_git_ref,
        "scheduler_git_hash": SCHEDULER_CONTROL_PLANE_SHA,
        "scheduler_client_git_hash": SCHEDULER_CLIENT_SHA,
        "library_git_hash": PYAEDT_LIBRARY_SHA,
        "placement_authority": "mixed-canary-admission",
        "exact_session_reservation": False,
        "runtime_authority_sha256": load_authority()["authority_sha256"],
        "automation_lock_timeout_seconds": LOCK_TIMEOUT_SECONDS,
        "native_pipeline_barrier_timeout_seconds": BARRIER_TIMEOUT_SECONDS,
        "release_wait_seconds": RELEASE_WAIT_SECONDS,
        "result_guard": {
            "status": "ok",
            "analysis_returned_false": False,
            "missing_required_outputs": "",
            "native_pipeline_completed_count": 3,
            "native_pipeline_expected_count": 3,
        },
    }
    return ({
        "name": f"ipmsm-mixed-{RUN_LABEL}-s{session_id}-{motor_git_ref[:7]}-case0020",
        "remote_cwd": "__SLURM_SCHEDULER_ACCOUNT_WORKSPACE__",
        "command": command,
        "env_setup": env_setup,
        "required_capability": "conda:pyaedt2026v1",
        "env_profile": "pyaedt2026v1",
        "account_name": account_name,
        "node_name": node_name,
        "cpus": 4,
        "memory_mb": 32768,
        "scheduling_profile": "fea_bursty",
        "aedt_backend": "pooled",
        "gpus": 0,
        "partition": "auto",
        "priority": 100,
        "timeout_seconds": 43200,
        "dedupe_key": dedupe_key,
        "max_workers_per_node": 0,
        "cleanup_globs": "*.aedtresults",
        "project": "PYAEDT_MOTOR_IPMSM_V2",
        "entrypoint": "subprocess_run.py",
        "payload_json": metadata,
    }, token)


def cancel_tasks(task_ids: Iterable[int]) -> list[str]:
    errors = []
    for task_id in task_ids:
        try:
            request_json(f"/api/tasks/{int(task_id)}/cancel")
        except Exception as exc:
            errors.append(f"task {task_id}: {type(exc).__name__}: {exc}")
    return errors


def attest_created_tasks(task_ids: list[int], dedupe_keys: set[str]) -> list[dict[str, Any]]:
    with database() as connection:
        rows = rows_by_ids(connection, "tasks", task_ids)
    if len(rows) != 3 or {str(item["dedupe_key"]) for item in rows} != dedupe_keys:
        raise MixedCanaryError("created mixed task rows failed exact dedupe attestation")
    if any(str(item["aedt_backend"] or "") != "pooled" for item in rows):
        raise MixedCanaryError("created mixed task row lost pooled AEDT backend")
    return [
        {
            "id": int(item["id"]),
            "name": item["name"],
            "status": item["status"],
            "dedupe_key": item["dedupe_key"],
        }
        for item in rows
    ]


def release_gates(account_name: str, tokens: list[str]) -> None:
    sys.path.insert(0, str(SCHEDULER_SOURCE))
    from slurm_scheduler.config import load_accounts
    from slurm_scheduler.slurm import SSHSession

    account = next(
        (item for item in load_accounts(ACCOUNTS_PATH) if item.name == account_name),
        None,
    )
    if account is None:
        raise MixedCanaryError(f"scheduler account is missing: {account_name}")
    files = " ".join(
        f'"$HOME/slurm_scheduler/mixed-canary-gates/{item}.release"'
        for item in tokens
    )
    command = (
        'mkdir -p "$HOME/slurm_scheduler/mixed-canary-gates" && '
        f"touch {files}"
    )
    with SSHSession(account, default_timeout=60) as ssh:
        result = ssh.run(command)
    if result.exit_code != 0:
        raise MixedCanaryError(
            "failed to release mixed-canary gates: "
            f"rc={result.exit_code}, stderr={result.stderr[-500:]}"
        )


def verify_motor_ref(motor_git_ref: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", motor_git_ref):
        raise MixedCanaryError("--motor-git-ref must be an exact 40-character commit")
    head = subprocess.run(
        ["git", "-C", str(BASE_DIR), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != motor_git_ref:
        raise MixedCanaryError(
            f"execution checkout must equal --motor-git-ref: head={head}, requested={motor_git_ref}"
        )
    dirty = subprocess.run(
        ["git", "-C", str(BASE_DIR), "status", "--porcelain", "--untracked-files=no"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise MixedCanaryError("execution checkout has tracked modifications")


def execute(preflight: dict[str, Any], motor_git_ref: str) -> dict[str, Any]:
    if not preflight["execution_ready"]:
        raise MixedCanaryError(
            "mixed canary preflight is blocked: " + "; ".join(preflight["blockers"])
        )
    verify_motor_ref(motor_git_ref)
    session = dict(preflight["selected_session"])
    session_id = int(session["id"])
    q21_locations = list(preflight["q21"].get("client_locations") or [])
    if q21_locations != [(CLIENT_ACCOUNT, CLIENT_NODE)]:
        raise MixedCanaryError(
            "q21 client account/node does not match the verified scheduler package"
        )
    account_name, node_name = CLIENT_ACCOUNT, CLIENT_NODE
    admission = request_json(
        "/api/aedt-pool/mixed-canary-admissions",
        {
            "session_id": session_id,
            "mft_projects": 2,
            "ipmsm_projects": 1,
            "ttl_seconds": ADMISSION_TTL_SECONDS,
        },
        bootstrap=True,
    )
    slots = list(admission.get("slots") or [])
    mft_slots = [item for item in slots if item.get("workload_family") == "mft"]
    motor_slots = [item for item in slots if item.get("workload_family") == "ipmsm"]
    if (
        int(admission.get("session_id") or 0) != session_id
        or len(mft_slots) != 2
        or len(motor_slots) != 1
        or any(item.get("project_namespace") != "mft" for item in mft_slots)
        or motor_slots[0].get("project_namespace") != "pyaedt_motor"
    ):
        raise MixedCanaryError("mixed-canary admission returned an invalid exact 2+1 slot set")
    with database() as connection:
        mft_sources, motor_source = source_tasks(connection)
    payloads: list[dict[str, Any]] = []
    tokens: list[str] = []
    for ordinal, (source, slot) in enumerate(zip(mft_sources, mft_slots, strict=True)):
        payload, token = mft_payload(
            source,
            dedupe_key=str(slot["dedupe_key"]),
            ordinal=ordinal,
            session_id=session_id,
            account_name=account_name,
            node_name=node_name,
            motor_git_ref=motor_git_ref,
        )
        payloads.append(payload)
        tokens.append(token)
    payload, token = motor_payload(
        motor_source,
        dedupe_key=str(motor_slots[0]["dedupe_key"]),
        session_id=session_id,
        account_name=account_name,
        node_name=node_name,
        motor_git_ref=motor_git_ref,
        profile_export=canonical_profile_export(mft_sources[0]),
    )
    payloads.append(payload)
    tokens.append(token)
    if len({item["dedupe_key"] for item in payloads}) != 3 or len(set(tokens)) != 3:
        raise MixedCanaryError("mixed payload dedupe/gate tokens are not unique")

    created_ids: list[int] = []
    gates_released = False
    try:
        for payload in payloads:
            task = request_json("/api/tasks", payload)
            if task.get("deduped"):
                raise MixedCanaryError(f"mixed task unexpectedly deduped to {task.get('id')}")
            task_id = int(task.get("id") or 0)
            if task_id <= 0:
                raise MixedCanaryError("mixed task POST returned no task id")
            created_ids.append(task_id)
        task_rows = attest_created_tasks(
            created_ids,
            {str(item["dedupe_key"]) for item in payloads},
        )
        release_gates(str(account_name), tokens)
        gates_released = True
    except Exception:
        if not gates_released and created_ids:
            rollback_errors = cancel_tasks(created_ids)
            if rollback_errors:
                print("mixed canary rollback errors: " + "; ".join(rollback_errors), file=sys.stderr)
        raise
    return {
        "schema_version": "mft-ipmsm-mixed-canary-submission-v1",
        "admission_id": int(admission.get("id") or 0),
        "session_id": session_id,
        "motor_git_ref": motor_git_ref,
        "task_rows": task_rows,
        "gate_tokens": tokens,
        "gates_released": gates_released,
        "placement_authority": "mixed-canary-admission",
        "exact_session_reservation_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--motor-git-ref", default="")
    parser.add_argument("--session-id", type=int, default=0)
    parser.add_argument("--verify-runtime-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify_runtime_only:
            evidence = verify_runtime_closure()
            print(canonical_json({"runtime_closure_verified": True, "files": evidence}))
            return 0
        preflight = prepare(
            requested_session_id=args.session_id,
            motor_git_ref=args.motor_git_ref,
        )
        if not args.execute:
            print(json.dumps(preflight, indent=2, sort_keys=True))
            return 0
        if not args.motor_git_ref:
            raise MixedCanaryError("--execute requires --motor-git-ref")
        result = execute(preflight, args.motor_git_ref)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (MixedCanaryError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"mixed canary failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
