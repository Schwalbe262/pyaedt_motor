"""Prepare or submit an IPMSM scheduler task through the Slurm Scheduler API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
from typing import Any
from urllib import parse, request

from submit_ipmsm_scheduler_job import (
    DEFAULT_BOOTSTRAP_MAX_BYTES,
    DEFAULT_MAX_CASES,
    DEFAULT_SCHEDULER_URL,
    append_env_setup,
    build_remote_cases_bootstrap,
    build_stdout_output,
    build_subprocess_arguments,
    compact_non_json_response,
    get_scheduler_health,
    load_and_validate_cases,
    read_env_setup_file,
    select_case_rows,
    write_manifest,
)


def build_task_command(args: argparse.Namespace) -> str:
    return shlex.join(["python", args.entrypoint]) + " " + build_subprocess_arguments(args)


def build_task_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "name": args.task_name,
        "remote_cwd": args.remote_cwd,
        "command": build_task_command(args),
        "env_setup": args.env_setup,
        "required_capability": args.required_capability,
        "env_profile": args.env_profile,
        "account_name": args.account_name,
        "partition": args.partition,
        "node_name": args.node_name,
        "exclusive_node": args.exclusive_node,
        "cpus": args.cpus,
        "memory_mb": args.memory_mb,
        "scheduling_profile": args.scheduling_profile,
        "max_workers_per_node": args.max_workers_per_node,
        "gpus": args.gpus,
        "gpu_model": args.gpu_model,
    }


def validate_task_request(args: argparse.Namespace) -> None:
    if args.submit and args.analyze and not args.confirm_analyze:
        raise RuntimeError("scheduler analyze task submission requires --confirm-analyze with --analyze")
    if not args.remote_cwd:
        raise RuntimeError("--remote-cwd is required for scheduler task submission")
    if args.case_start_index < 1:
        raise RuntimeError("--case-start-index must be >= 1")
    if args.case_limit < 0:
        raise RuntimeError("--case-limit must be >= 0")


def post_scheduler_task(scheduler_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = scheduler_url.rstrip("/") + "/tasks"
    encoded = parse.urlencode(payload).encode("utf-8")
    req = request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return compact_non_json_response(body)


def get_scheduler_tasks(scheduler_url: str, timeout: float) -> list[dict[str, Any]]:
    url = scheduler_url.rstrip("/") + "/api/tasks"
    with request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, list):
        raise RuntimeError("/api/tasks did not return a JSON list")
    return [task for task in data if isinstance(task, dict)]


def task_id_set(tasks: list[dict[str, Any]]) -> set[int]:
    ids: set[int] = set()
    for task in tasks:
        try:
            ids.add(int(task["id"]))
        except Exception:
            continue
    return ids


def task_id(task: dict[str, Any]) -> int | None:
    try:
        return int(task["id"])
    except Exception:
        return None


def selected_submit_task_fields(task: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "name",
        "status",
        "account_name",
        "allocation_id",
        "remote_cwd",
        "remote_dir",
        "stdout_path",
        "stderr_path",
        "return_code",
        "failure_message",
        "scheduling_profile",
        "max_workers_per_node",
        "created_at",
        "started_at",
        "updated_at",
        "finished_at",
    )
    return {key: task.get(key) for key in keys if key in task}


def find_submitted_task(
    tasks: list[dict[str, Any]],
    previous_ids: set[int],
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    new_tasks = [task for task in tasks if task_id(task) not in previous_ids]
    candidates = new_tasks or tasks
    for task in candidates:
        if task.get("name") == args.task_name and task.get("remote_cwd") == args.remote_cwd:
            return task
    return candidates[0] if candidates else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run or submit an IPMSM task to the Slurm Scheduler API.")
    parser.add_argument("--scheduler-url", default=DEFAULT_SCHEDULER_URL)
    parser.add_argument("--cases", type=Path, required=True, help="Local case CSV to validate before submission.")
    parser.add_argument("--remote-cases", default="remote/cases.csv", help="Case CSV path visible from --remote-cwd.")
    parser.add_argument("--bootstrap-remote-cases", action="store_true", help="Embed the validated case CSV into env_setup.")
    parser.add_argument("--bootstrap-max-bytes", type=int, default=DEFAULT_BOOTSTRAP_MAX_BYTES)
    parser.add_argument("--case-start-index", type=int, default=1, help="1-based first validated case row to submit.")
    parser.add_argument("--case-limit", type=int, default=0, help="Maximum selected case rows to submit; 0 means all rows from --case-start-index.")
    parser.add_argument("--remote-cwd", required=True, help="Existing scheduler-accessible project directory.")
    parser.add_argument("--entrypoint", default="subprocess_run.py")
    parser.add_argument("--task-name", default="ipmsm-replay-setup-task")
    parser.add_argument("--processes", type=int, default=1)
    parser.add_argument("--cores-per-process", type=int, default=4)
    parser.add_argument("--max-cases", type=int, default=DEFAULT_MAX_CASES)
    parser.add_argument("--allow-over-budget", action="store_true")
    parser.add_argument("--stagger-seconds", type=float, default=30.0)
    parser.add_argument("--simulation-dir", default="simulation")
    parser.add_argument("--result-csv", default="ipmsm_scheduler_task_results.csv")
    parser.add_argument("--log-dir", default="simul_log_scheduler")
    parser.add_argument("--log-prefix", default="task_")
    parser.add_argument("--analyze", action="store_true", help="Run solves; default is setup-only.")
    parser.add_argument("--confirm-analyze", action="store_true", help="Allow --submit with --analyze.")
    parser.add_argument("--periodic-boundary", action="store_true")
    parser.add_argument("--keep-projects", action="store_true")
    parser.add_argument("--env-setup", default="")
    parser.add_argument("--env-setup-file", type=Path, default=None, help="Read additional scheduler env setup shell from a local file.")
    parser.add_argument("--required-capability", default="")
    parser.add_argument("--env-profile", default="")
    parser.add_argument("--account-name", default="")
    parser.add_argument("--partition", default="auto")
    parser.add_argument("--node-name", default="")
    parser.add_argument("--exclusive-node", action="store_true")
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory-mb", type=int, default=16_384)
    parser.add_argument("--scheduling-profile", choices=("standard", "fea_bursty"), default="standard")
    parser.add_argument("--max-workers-per-node", type=int, default=0)
    parser.add_argument("--gpus", type=int, default=0)
    parser.add_argument("--gpu-model", default="")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--check-health", action="store_true")
    parser.add_argument("--write-manifest", type=Path, help="Write the review JSON payload to this local path.")
    parser.add_argument("--show-env-setup", action="store_true", help="Print full env_setup in stdout instead of redacting it.")
    parser.add_argument("--submit", action="store_true", help="POST to scheduler. Omit for dry-run JSON only.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validate_task_request(args)
    validated_rows = load_and_validate_cases(args.cases, args.max_cases, args.allow_over_budget)
    rows = select_case_rows(validated_rows, args.case_start_index, args.case_limit)
    if args.env_setup_file is not None:
        args.env_setup = append_env_setup(args.env_setup, read_env_setup_file(args.env_setup_file))
    if args.bootstrap_remote_cases:
        args.env_setup = append_env_setup(args.env_setup, build_remote_cases_bootstrap(args.remote_cases, rows, args.bootstrap_max_bytes))

    payload = build_task_payload(args)
    output: dict[str, Any] = {
        "submitted": False,
        "task_endpoint": "/tasks",
        "task_name": args.task_name,
        "validated_cases": len(validated_rows),
        "case_count": len(rows),
        "case_start_index": args.case_start_index,
        "case_limit": args.case_limit,
        "payload": payload,
    }
    if args.check_health:
        output["health"] = get_scheduler_health(args.scheduler_url, args.timeout)
    if args.submit:
        pre_submit_task_ids: set[int] = set()
        try:
            pre_submit_task_ids = task_id_set(get_scheduler_tasks(args.scheduler_url, args.timeout))
        except Exception as exc:
            output["pre_submit_task_lookup_error"] = str(exc)
        output["submitted"] = True
        output["response"] = post_scheduler_task(args.scheduler_url, payload, args.timeout)
        try:
            submitted_task = find_submitted_task(
                get_scheduler_tasks(args.scheduler_url, args.timeout),
                pre_submit_task_ids,
                args,
            )
        except Exception as exc:
            output["submitted_task_lookup_error"] = str(exc)
        else:
            if submitted_task:
                output["submitted_task"] = selected_submit_task_fields(submitted_task)
    if args.write_manifest:
        write_manifest(args.write_manifest, output)
    print(json.dumps(build_stdout_output(output, args.show_env_setup), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2)
