"""Submit an IPMSM v2 case plan as independent one-case scheduler tasks."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import posixpath
from pathlib import Path, PurePosixPath
import shlex
from types import SimpleNamespace
from typing import Any, Iterable
from urllib import parse, request

from submit_ipmsm_scheduler_task import (
    ANSYS_ELECTRONICS_MODULE,
    DEFAULT_BOOTSTRAP_MAX_BYTES,
    DEFAULT_SCHEDULER_URL,
    append_env_setup,
    build_remote_cases_bootstrap,
    build_task_payload,
    get_scheduler_tasks,
    load_and_validate_cases,
    post_scheduler_task,
    project_active_task_count,
    safe_dedupe_part,
    select_case_rows,
    task_belongs_to_project,
    write_manifest,
)


DEFAULT_PROJECT_ACTIVE_CAP = 50
MAX_PROJECT_ACTIVE_CAP = 100
DEFAULT_MAX_PLAN_CASES = 10_000
DEFAULT_HISTORY_LIMIT = 10_000
MAX_HISTORY_LIMIT = 10_000
DEFAULT_SCHEDULER_TIMEOUT_SECONDS = 60.0
DEFAULT_TASK_TIMEOUT_SECONDS = 43_200
MIN_TASK_TIMEOUT_SECONDS = 43_200
DEFAULT_AEDT_POOL_URL = "http://172.16.10.37:18790"
DEFAULT_AEDT_POOL_BOOTSTRAP_TOKEN_FILE = "~/slurm_scheduler/aedt_pool_bootstrap"
STDOUT_TASK_PREVIEW = 10
SKIP_EXISTING_STATUSES = frozenset({"queued", "attaching", "running", "completed"})
RETRYABLE_TERMINAL_STATUSES = frozenset({"failed", "cancelled"})


@dataclass(frozen=True)
class CampaignTask:
    row_number: int
    case_id: str
    safe_case_id: str
    remote_cases: str
    result_csv: str
    simulation_dir: str
    task_name: str
    dedupe_key: str
    payload: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "case_id": self.case_id,
            "safe_case_id": self.safe_case_id,
            "task_name": self.task_name,
            "remote_cases": self.remote_cases,
            "result_csv": self.result_csv,
            "simulation_dir": self.simulation_dir,
            "dedupe_key": self.dedupe_key,
            "cpus": self.payload["cpus"],
            "memory_mb": self.payload["memory_mb"],
            "scheduling_profile": self.payload["scheduling_profile"],
        }

    def manifest_entry(self) -> dict[str, Any]:
        entry = self.summary()
        entry["payload"] = self.payload
        return entry


def sanitize_case_id(case_id: object) -> str:
    safe = safe_dedupe_part(case_id)
    if not safe:
        raise RuntimeError(f"case_id cannot be sanitized safely: {case_id!r}")
    return safe


def campaign_dedupe_key(args: argparse.Namespace, row: dict[str, Any], safe_case_id: str) -> str:
    canonical_row = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    identity_parts = [
        args.project,
        args.task_prefix,
        safe_case_id,
        args.remote_cases_dir,
        args.result_dir,
        canonical_row,
    ]
    if str(getattr(args, "aedt_backend", "") or "").strip().lower() == "pooled":
        identity_parts.append("aedt_backend=pooled")
    identity = "|".join(identity_parts)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    prefix = safe_dedupe_part(args.task_prefix) or "ipmsm-v2"
    return f"{prefix}-{safe_case_id}-{digest}"


def _remote_path(directory: str, name: str) -> str:
    normalized = str(directory or "").strip().replace("\\", "/").rstrip("/")
    if not normalized:
        raise RuntimeError("remote path directory must not be blank")
    path = posixpath.join(normalized, name)
    if path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise RuntimeError(f"remote campaign paths must be safe relative paths: {path!r}")
    return path


def result_cleanup_command(result_csv: str) -> str:
    """Remove only this case's stale append-only result before a retry."""
    return "rm -f -- " + " ".join(
        shlex.quote(path) for path in (result_csv, result_csv + ".lock")
    )


def shell_expandable_home_path(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    if normalized == "~" or normalized == "$HOME":
        return '"$HOME"'
    for prefix in ("~/", "$HOME/"):
        if normalized.startswith(prefix):
            return '"$HOME"/' + shlex.quote(normalized[len(prefix) :])
    return shlex.quote(normalized)


def pooled_aedt_env_setup(args: argparse.Namespace) -> str:
    scheduler_url = str(args.aedt_pool_url or "").strip()
    token_file = str(args.aedt_pool_bootstrap_token_file or "").strip()
    if not scheduler_url:
        raise RuntimeError("--aedt-pool-url must not be blank for pooled AEDT")
    if not token_file:
        raise RuntimeError("--aedt-pool-bootstrap-token-file must not be blank for pooled AEDT")
    return "\n".join(
        (
            f"export MFT_AEDT_SCHEDULER_URL={shlex.quote(scheduler_url)}",
            "export SLURM_AEDT_POOL_BOOTSTRAP_TOKEN_FILE="
            + shell_expandable_home_path(token_file),
        )
    )


def build_campaign_task(
    args: argparse.Namespace,
    row: dict[str, Any],
    *,
    row_number: int,
) -> CampaignTask:
    case_id = str(row.get("case_id") or "").strip()
    if not case_id:
        raise RuntimeError(f"selected row {row_number} has a blank case_id")
    safe_case_id = sanitize_case_id(case_id)
    remote_cases = _remote_path(args.remote_cases_dir, f"{safe_case_id}.csv")
    result_csv = _remote_path(args.result_dir, f"{safe_case_id}.csv")
    simulation_dir = _remote_path(args.simulation_dir, safe_case_id)
    task_name = f"{safe_dedupe_part(args.task_prefix) or 'ipmsm-v2'}-{safe_case_id}"
    dedupe_key = campaign_dedupe_key(args, row, safe_case_id)
    bootstrap = build_remote_cases_bootstrap(
        remote_cases,
        [row],
        args.bootstrap_max_bytes,
    )
    env_setup = append_env_setup(args.env_setup, bootstrap)
    env_setup = append_env_setup(env_setup, result_cleanup_command(result_csv))
    if str(getattr(args, "aedt_backend", "standalone") or "").strip().lower() == "pooled":
        env_setup = append_env_setup(env_setup, pooled_aedt_env_setup(args))
    task_args = SimpleNamespace(
        entrypoint=args.entrypoint,
        remote_cases=remote_cases,
        processes=1,
        cores_per_process=args.cores_per_process,
        max_cases=1,
        allow_over_budget=False,
        stagger_seconds=0.0,
        simulation_dir=simulation_dir,
        result_csv=result_csv,
        log_dir=args.log_dir,
        log_prefix=f"{safe_case_id}_",
        analyze=True,
        periodic_boundary=False,
        keep_projects=args.keep_projects,
        task_name=task_name,
        remote_cwd="",
        project=args.project,
        env_setup=env_setup,
        required_capability=args.required_capability,
        env_profile=args.env_profile,
        account_name=args.account_name,
        partition=args.partition,
        node_name=args.node_name,
        exclusive_node=False,
        cpus=args.cpus,
        memory_mb=args.memory_mb,
        scheduling_profile=args.scheduling_profile,
        max_workers_per_node=args.max_workers_per_node,
        priority=args.priority,
        timeout_seconds=args.timeout_seconds,
        dedupe_key=dedupe_key,
        gpus=0,
        gpu_model="",
        aedt_backend=getattr(args, "aedt_backend", "standalone"),
    )
    payload = build_task_payload(task_args)
    return CampaignTask(
        row_number=row_number,
        case_id=case_id,
        safe_case_id=safe_case_id,
        remote_cases=remote_cases,
        result_csv=result_csv,
        simulation_dir=simulation_dir,
        task_name=task_name,
        dedupe_key=dedupe_key,
        payload=payload,
    )


def build_campaign_tasks(
    args: argparse.Namespace,
    rows: Iterable[dict[str, Any]],
    *,
    first_row_number: int,
) -> list[CampaignTask]:
    tasks = [
        build_campaign_task(args, row, row_number=first_row_number + offset)
        for offset, row in enumerate(rows)
    ]
    by_safe_id: dict[str, str] = {}
    for task in tasks:
        previous = by_safe_id.setdefault(task.safe_case_id, task.case_id)
        if previous != task.case_id:
            raise RuntimeError(
                f"case_id sanitization collision: {previous!r} and {task.case_id!r} -> {task.safe_case_id!r}"
            )
    path_sets = {
        "remote_cases": {task.remote_cases for task in tasks},
        "result_csv": {task.result_csv for task in tasks},
        "task_name": {task.task_name for task in tasks},
        "dedupe_key": {task.dedupe_key for task in tasks},
    }
    duplicates = [name for name, values in path_sets.items() if len(values) != len(tasks)]
    if duplicates:
        raise RuntimeError("campaign task identities are not unique: " + ", ".join(duplicates))
    return tasks


def get_scheduler_task_history(
    scheduler_url: str,
    timeout: float,
    history_limit: int,
    project: str = "",
    name_prefix: str = "",
) -> list[dict[str, Any]]:
    query_values: dict[str, Any] = {"limit": history_limit}
    project_name = str(project or "").strip()
    if project_name:
        query_values["project"] = project_name
    task_name_prefix = str(name_prefix or "").strip()
    if task_name_prefix:
        query_values["name_prefix"] = task_name_prefix
    query = parse.urlencode(query_values)
    url = scheduler_url.rstrip("/") + f"/api/tasks?{query}"
    with request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, list):
        raise RuntimeError("scheduler task history did not return a JSON list")
    return [task for task in data if isinstance(task, dict)]


def get_scheduler_project_summary(
    scheduler_url: str,
    project: str,
    timeout: float,
) -> dict[str, Any]:
    encoded_project = parse.quote(project, safe="")
    url = scheduler_url.rstrip("/") + f"/api/projects/{encoded_project}"
    with request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise RuntimeError("scheduler project lookup did not return a JSON object")
    try:
        total_count = int(data["total_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("scheduler project lookup has no valid total_count") from exc
    if total_count < 0:
        raise RuntimeError("scheduler project total_count must be nonnegative")
    return {**data, "total_count": total_count}


def require_scheduler_project_cap(project_summary: dict[str, Any], expected_cap: int) -> int:
    raw = project_summary.get("max_active_tasks")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RuntimeError("scheduler project does not expose a valid integer max_active_tasks")
    server_cap = raw
    if server_cap != expected_cap:
        raise RuntimeError(
            "scheduler project active cap does not match campaign policy: "
            f"server={server_cap} requested={expected_cap}"
        )
    return server_cap


def _existing_record(task: CampaignTask, history_task: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": task.case_id,
        "dedupe_key": task.dedupe_key,
        "status": str(history_task.get("status") or "").strip().lower(),
        "task_id": _task_id(history_task),
    }


def _history_task_order(history_task: dict[str, Any]) -> int:
    value = _task_id(history_task)
    return value if isinstance(value, int) else -1


def classify_campaign_history(
    tasks: Iterable[CampaignTask],
    history: Iterable[dict[str, Any]],
    project: str,
) -> tuple[list[CampaignTask], list[dict[str, Any]], list[dict[str, Any]]]:
    by_dedupe: dict[str, list[dict[str, Any]]] = {}
    for history_task in history:
        if not task_belongs_to_project(history_task, project):
            continue
        dedupe_key = str(history_task.get("dedupe_key") or "").strip()
        if dedupe_key:
            by_dedupe.setdefault(dedupe_key, []).append(history_task)

    eligible: list[CampaignTask] = []
    skipped_existing: list[dict[str, Any]] = []
    retryable_terminal: list[dict[str, Any]] = []
    for task in tasks:
        matches = by_dedupe.get(task.dedupe_key, [])
        if not matches:
            eligible.append(task)
            continue
        ordered = sorted(matches, key=_history_task_order, reverse=True)
        protected = [
            item
            for item in ordered
            if str(item.get("status") or "").strip().lower() in SKIP_EXISTING_STATUSES
        ]
        if protected:
            skipped_existing.append(_existing_record(task, protected[0]))
            continue
        retryable = [
            item
            for item in ordered
            if str(item.get("status") or "").strip().lower() in RETRYABLE_TERMINAL_STATUSES
        ]
        unknown_statuses = sorted(
            {
                str(item.get("status") or "").strip().lower() or "<blank>"
                for item in ordered
            }
            - RETRYABLE_TERMINAL_STATUSES
        )
        if unknown_statuses:
            raise RuntimeError(
                f"ambiguous scheduler history status for case_id={task.case_id!r}: {unknown_statuses}"
            )
        if not retryable:
            raise RuntimeError(f"scheduler history match has no usable status for case_id={task.case_id!r}")
        retryable_terminal.append(_existing_record(task, retryable[0]))
        eligible.append(task)
    return eligible, skipped_existing, retryable_terminal


def validate_args(args: argparse.Namespace) -> None:
    if not str(args.project or "").strip():
        raise RuntimeError("--project must not be blank")
    if not str(args.task_prefix or "").strip():
        raise RuntimeError("--task-prefix must not be blank")
    if args.project_active_cap < 1:
        raise RuntimeError("--project-active-cap must be >= 1")
    if args.project_active_cap > MAX_PROJECT_ACTIVE_CAP:
        raise RuntimeError(f"--project-active-cap must be <= {MAX_PROJECT_ACTIVE_CAP}")
    if args.cpus < 1 or args.cores_per_process < 1:
        raise RuntimeError("--cpus and --cores-per-process must be >= 1")
    if args.memory_mb < 1:
        raise RuntimeError("--memory-mb must be >= 1")
    if args.timeout_seconds < MIN_TASK_TIMEOUT_SECONDS:
        raise RuntimeError(f"--timeout-seconds must be >= {MIN_TASK_TIMEOUT_SECONDS}")
    if args.max_plan_cases < 1:
        raise RuntimeError("--max-plan-cases must be >= 1")
    if not 1 <= args.history_limit <= MAX_HISTORY_LIMIT:
        raise RuntimeError(f"--history-limit must be between 1 and {MAX_HISTORY_LIMIT}")
    if ANSYS_ELECTRONICS_MODULE not in args.env_setup:
        raise RuntimeError(f"--env-setup must include {ANSYS_ELECTRONICS_MODULE!r}")
    if args.scheduling_profile != "fea_bursty":
        raise RuntimeError("IPMSM v2 FEA campaigns require --scheduling-profile fea_bursty")
    if args.required_capability != "conda:pyaedt2026v1":
        raise RuntimeError("IPMSM v2 FEA campaigns require --required-capability conda:pyaedt2026v1")
    if args.env_profile != "pyaedt2026v1":
        raise RuntimeError("IPMSM v2 FEA campaigns require --env-profile pyaedt2026v1")
    if args.aedt_backend == "pooled":
        pooled_aedt_env_setup(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run or submit each selected IPMSM v2 case as an independent scheduler task."
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--scheduler-url", default=DEFAULT_SCHEDULER_URL)
    parser.add_argument("--project-active-cap", type=int, default=DEFAULT_PROJECT_ACTIVE_CAP)
    parser.add_argument("--start", "--case-start-index", dest="case_start_index", type=int, default=1)
    parser.add_argument("--limit", "--case-limit", dest="case_limit", type=int, default=0)
    parser.add_argument("--max-plan-cases", type=int, default=DEFAULT_MAX_PLAN_CASES)
    parser.add_argument("--history-limit", type=int, default=DEFAULT_HISTORY_LIMIT)
    parser.add_argument("--task-prefix", default="ipmsm-v2")
    parser.add_argument("--remote-cases-dir", default="remote/ipmsm_v2_campaign_cases")
    parser.add_argument("--result-dir", default="simul_log_scheduler/ipmsm_v2_campaign_results")
    parser.add_argument("--simulation-dir", default="simulation/ipmsm_v2_campaign")
    parser.add_argument("--log-dir", default="simul_log_scheduler/ipmsm_v2_campaign_logs")
    parser.add_argument("--entrypoint", default="subprocess_run.py")
    parser.add_argument("--env-setup", default=ANSYS_ELECTRONICS_MODULE)
    parser.add_argument("--required-capability", default="conda:pyaedt2026v1")
    parser.add_argument("--env-profile", default="pyaedt2026v1")
    parser.add_argument("--account-name", default="")
    parser.add_argument("--partition", default="auto")
    parser.add_argument("--node-name", default="")
    parser.add_argument("--cores-per-process", type=int, default=4)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory-mb", type=int, default=32_768)
    parser.add_argument("--scheduling-profile", choices=("standard", "fea_bursty"), default="fea_bursty")
    parser.add_argument("--max-workers-per-node", type=int, default=0)
    parser.add_argument("--priority", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TASK_TIMEOUT_SECONDS)
    parser.add_argument("--bootstrap-max-bytes", type=int, default=DEFAULT_BOOTSTRAP_MAX_BYTES)
    parser.add_argument("--keep-projects", action="store_true")
    parser.add_argument("--aedt-backend", choices=("standalone", "pooled"), default="standalone")
    parser.add_argument("--aedt-pool-url", default=DEFAULT_AEDT_POOL_URL)
    parser.add_argument(
        "--aedt-pool-bootstrap-token-file",
        default=DEFAULT_AEDT_POOL_BOOTSTRAP_TOKEN_FILE,
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_SCHEDULER_TIMEOUT_SECONDS)
    parser.add_argument("--write-manifest", type=Path)
    parser.add_argument("--submit", action="store_true")
    return parser


def _task_id(response: dict[str, Any]) -> int | str | None:
    value = response.get("id", response.get("task_id"))
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_args(args)
    validated_rows = load_and_validate_cases(args.cases, args.max_plan_cases, False)
    selected_rows = select_case_rows(validated_rows, args.case_start_index, args.case_limit)
    tasks = build_campaign_tasks(args, selected_rows, first_row_number=args.case_start_index)

    try:
        history = get_scheduler_task_history(
            args.scheduler_url,
            args.timeout,
            args.history_limit,
            args.project,
        )
    except Exception as exc:
        raise RuntimeError(
            f"cannot inspect scheduler task history; no task was submitted: project={args.project!r}: {exc}"
        ) from exc
    try:
        project_summary = get_scheduler_project_summary(args.scheduler_url, args.project, args.timeout)
    except Exception as exc:
        raise RuntimeError(
            f"cannot verify scheduler project history coverage; no task was submitted: "
            f"project={args.project!r}: {exc}"
        ) from exc
    server_project_cap = require_scheduler_project_cap(
        project_summary,
        args.project_active_cap,
    )
    history_project_tasks = sum(1 for task in history if task_belongs_to_project(task, args.project))
    project_total_count = int(project_summary["total_count"])
    history_saturated = len(history) >= args.history_limit
    history_coverage_complete = history_project_tasks == project_total_count
    if not history_coverage_complete:
        qualifier = "saturated " if history_saturated else ""
        raise RuntimeError(
            f"{qualifier}scheduler history coverage is incomplete; no task was submitted: "
            f"project={args.project!r} history_project_tasks={history_project_tasks} "
            f"project_total_count={project_total_count} history_rows={len(history)} "
            f"history_limit={args.history_limit}"
        )

    eligible_tasks, skipped_existing, retryable_terminal = classify_campaign_history(
        tasks,
        history,
        args.project,
    )

    try:
        scheduler_tasks = get_scheduler_tasks(args.scheduler_url, args.timeout)
    except Exception as exc:
        raise RuntimeError(
            f"cannot enforce project active cap; no task was submitted: project={args.project!r}: {exc}"
        ) from exc
    active_initial = project_active_task_count(scheduler_tasks, args.project)
    open_slots = max(0, args.project_active_cap - active_initial)
    planned_tasks = eligible_tasks[:open_slots]
    deferred_tasks = eligible_tasks[open_slots:]
    if args.submit and eligible_tasks and not planned_tasks:
        raise RuntimeError(
            "project active cap reached before POST: "
            f"project={args.project!r} active={active_initial} cap={args.project_active_cap}"
        )

    submissions: list[dict[str, Any]] = []
    if args.submit:
        for task in planned_tasks:
            response = post_scheduler_task(
                args.scheduler_url,
                task.payload,
                args.timeout,
                "/api/tasks",
            )
            submissions.append(
                {
                    "case_id": task.case_id,
                    "task_name": task.task_name,
                    "dedupe_key": task.dedupe_key,
                    "task_id": _task_id(response),
                    "response": response,
                }
            )

    output: dict[str, Any] = {
        "mode": "submit" if args.submit else "dry-run",
        "submitted": len(submissions),
        "project": args.project,
        "project_active_cap": args.project_active_cap,
        "project_server_active_cap": server_project_cap,
        "project_active_initial": active_initial,
        "open_slots_initial": open_slots,
        "history_limit": args.history_limit,
        "history_rows": len(history),
        "history_saturated": history_saturated,
        "history_project_tasks": history_project_tasks,
        "project_total_count": project_total_count,
        "history_coverage_complete": history_coverage_complete,
        "validated_cases": len(validated_rows),
        "selected_cases": len(tasks),
        "eligible_cases": len(eligible_tasks),
        "planned_tasks": len(planned_tasks),
        "deferred_tasks": len(deferred_tasks),
        "skipped_existing": skipped_existing,
        "retryable_terminal": retryable_terminal,
        "selection": {"start": args.case_start_index, "limit": args.case_limit},
        "resources": {
            "cpus": args.cpus,
            "memory_mb": args.memory_mb,
            "cores_per_process": args.cores_per_process,
            "scheduling_profile": args.scheduling_profile,
            "required_capability": args.required_capability,
            "env_profile": args.env_profile,
        },
        "task_preview": [task.summary() for task in planned_tasks[:STDOUT_TASK_PREVIEW]],
        "task_preview_truncated": len(planned_tasks) > STDOUT_TASK_PREVIEW,
        "deferred_case_ids": [task.case_id for task in deferred_tasks[:STDOUT_TASK_PREVIEW]],
        "deferred_preview_truncated": len(deferred_tasks) > STDOUT_TASK_PREVIEW,
        "submissions": submissions,
        "manifest": str(args.write_manifest) if args.write_manifest else "",
    }
    if args.write_manifest:
        manifest = dict(output)
        manifest["tasks"] = [task.manifest_entry() for task in tasks]
        write_manifest(args.write_manifest, manifest)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2)
