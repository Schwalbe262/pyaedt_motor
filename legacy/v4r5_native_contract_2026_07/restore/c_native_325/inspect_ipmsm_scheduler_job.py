"""Inspect a Slurm Scheduler job with filtered log output."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from typing import Any
from urllib import parse, request


DEFAULT_SCHEDULER_URL = "http://localhost:8000"
INTERESTING_PATTERN = re.compile(
    r"(ERROR|FAIL|Traceback|Exception|RuntimeError|ValueError|fatal|missing|required|validation|return code|Finished)",
    re.IGNORECASE,
)
QUALITY_PROFILES = frozenset({"baseline", "mesh_fine", "time_fine", "mesh_time_fine"})


def get_json(scheduler_url: str, path: str, query: dict[str, str] | None = None, timeout: float = 10.0) -> Any:
    url = scheduler_url.rstrip("/") + path
    if query:
        url += "?" + parse.urlencode(query)
    with request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body)


def get_text_or_json(scheduler_url: str, path: str, query: dict[str, str] | None = None, timeout: float = 10.0) -> Any:
    url = scheduler_url.rstrip("/") + path
    if query:
        url += "?" + parse.urlencode(query)
    with request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def selected_job_fields(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "job_name",
        "status",
        "job_mode",
        "account_name",
        "repo_url",
        "git_ref",
        "entrypoint",
        "arguments",
        "partition",
        "node_name",
        "slurm_job_id",
        "remote_path",
        "remote_job_dir",
        "stdout_path",
        "stderr_path",
        "failure_message",
        "simulation_start",
        "simulation_count",
        "created_at",
        "submitted_at",
        "updated_at",
        "finished_at",
    )
    return {key: job.get(key) for key in keys if key in job}


def selected_task_fields(task: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "name",
        "status",
        "state",
        "account_name",
        "allocation_id",
        "assigned_allocation",
        "slurm_job_id",
        "required_capability",
        "remote_cwd",
        "remote_dir",
        "stdout_path",
        "stderr_path",
        "failure_message",
        "created_at",
        "attached_at",
        "started_at",
        "finished_at",
    )
    return {key: task.get(key) for key in keys if key in task}


def unwrap_remote_file_response(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("content", "text", "data", "body"):
            value = response.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(response, ensure_ascii=False, sort_keys=True)
    return str(response)


def tail_lines(text: str, count: int) -> list[str]:
    if count <= 0:
        return []
    return text.splitlines()[-count:]


def interesting_lines(text: str, max_lines: int) -> list[str]:
    matches = [line for line in text.splitlines() if INTERESTING_PATTERN.search(line)]
    if max_lines <= 0:
        return []
    return matches[-max_lines:]


def summarize_log_text(text: str, tail_count: int, max_interesting: int) -> dict[str, Any]:
    lines = text.splitlines()
    return {
        "line_count": len(lines),
        "interesting": interesting_lines(text, max_interesting),
        "tail": tail_lines(text, tail_count),
    }


def summarize_result_csv_text(text: str) -> dict[str, Any]:
    rows = list(csv.DictReader(io.StringIO(text)))
    status_counts: dict[str, int] = {}
    profiles_by_source: dict[str, set[str]] = {}
    ok_profiles_by_source: dict[str, set[str]] = {}
    for row in rows:
        status = str(row.get("status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        source = str(row.get("source_case_id") or row.get("input_source_case_id") or "")
        profile = str(row.get("quality_profile") or row.get("input_quality_profile") or "")
        if source and profile:
            profiles_by_source.setdefault(source, set()).add(profile)
            if status == "ok":
                ok_profiles_by_source.setdefault(source, set()).add(profile)
    complete_groups = sum(1 for profiles in ok_profiles_by_source.values() if QUALITY_PROFILES.issubset(profiles))
    summary: dict[str, Any] = {
        "row_count": len(rows),
        "status_counts": status_counts,
        "source_group_count": len(profiles_by_source),
        "complete_ok_quality_group_count": complete_groups,
    }
    if rows:
        last = rows[-1]
        summary["last_row"] = {
            key: last.get(key)
            for key in (
                "case_id",
                "source_case_id",
                "input_source_case_id",
                "quality_profile",
                "input_quality_profile",
                "status",
                "finished_at",
            )
            if key in last
        }
    return summary


def fetch_remote_file(
    scheduler_url: str,
    job_id: int,
    path: str,
    base: str,
    timeout: float,
) -> str:
    response = get_text_or_json(
        scheduler_url,
        f"/api/jobs/{job_id}/remote-file",
        query={"path": path, "base": base},
        timeout=timeout,
    )
    return unwrap_remote_file_response(response)


def fetch_task_remote_file(
    scheduler_url: str,
    task_id: int,
    path: str,
    base: str,
    timeout: float,
) -> str:
    response = get_text_or_json(
        scheduler_url,
        f"/api/tasks/{task_id}/remote-file",
        query={"path": path, "base": base},
        timeout=timeout,
    )
    return unwrap_remote_file_response(response)


def remote_file_query_path(job: dict[str, Any], path: str, base: str) -> str:
    if base != "remote_job_dir":
        return path
    remote_job_dir = str(job.get("remote_job_dir") or "").strip("/")
    normalized = path.strip("/")
    prefix = remote_job_dir + "/"
    if remote_job_dir and normalized.startswith(prefix):
        return normalized[len(prefix) :]
    return path


def add_result_csv_summary(result: dict[str, Any], fetcher: Any, path: str, args: argparse.Namespace) -> None:
    try:
        text = fetcher(args.scheduler_url, args.job_id, path, args.base, args.timeout)
    except Exception as exc:
        result["result_csv"] = {"path": path, "error": str(exc)}
        return
    result["result_csv"] = {"path": path, **summarize_result_csv_text(text)}


def inspect_job(args: argparse.Namespace) -> dict[str, Any]:
    job = get_json(args.scheduler_url, f"/api/jobs/{args.job_id}", timeout=args.timeout)
    result: dict[str, Any] = {"job": selected_job_fields(job)}
    requested_logs: list[tuple[str, str]] = []
    if args.stdout and job.get("stdout_path"):
        requested_logs.append(("stdout", str(job["stdout_path"])))
    if args.stderr and job.get("stderr_path"):
        requested_logs.append(("stderr", str(job["stderr_path"])))
    for label, path in requested_logs:
        query_path = remote_file_query_path(job, path, args.base)
        try:
            text = fetch_remote_file(args.scheduler_url, args.job_id, query_path, args.base, args.timeout)
        except Exception as exc:
            result[label] = {"path": path, "error": str(exc)}
            if query_path != path:
                result[label]["query_path"] = query_path
            continue
        result[label] = {"path": path, **summarize_log_text(text, args.tail_lines, args.max_interesting)}
        if query_path != path:
            result[label]["query_path"] = query_path
    result_csv = getattr(args, "result_csv", "")
    if result_csv:
        add_result_csv_summary(result, fetch_remote_file, result_csv, args)
    return result


def inspect_task(args: argparse.Namespace) -> dict[str, Any]:
    task = get_json(
        args.scheduler_url,
        f"/api/tasks/{args.job_id}",
        query={"include_output": "true"} if args.stdout or args.stderr else None,
        timeout=args.timeout,
    )
    result: dict[str, Any] = {"task": selected_task_fields(task)}
    for label in ("stdout", "stderr"):
        if not getattr(args, label):
            continue
        text = str(task.get(label) or "")
        result[label] = summarize_log_text(text, args.tail_lines, args.max_interesting)
    result_csv = getattr(args, "result_csv", "")
    if result_csv:
        add_result_csv_summary(result, fetch_task_remote_file, result_csv, args)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect a scheduler job or task without dumping full logs.")
    parser.add_argument("job_id", type=int)
    parser.add_argument("--task", action="store_true", help="Interpret the id as an attached task id.")
    parser.add_argument("--scheduler-url", default=DEFAULT_SCHEDULER_URL)
    parser.add_argument("--stdout", action="store_true", help="Fetch and filter stdout_path.")
    parser.add_argument("--stderr", action="store_true", help="Fetch and filter stderr_path.")
    parser.add_argument("--base", default="remote_job_dir", help="remote-file base parameter for log fetches.")
    parser.add_argument("--result-csv", default="", help="Fetch and summarize a remote result CSV without printing rows.")
    parser.add_argument("--tail-lines", type=int, default=20)
    parser.add_argument("--max-interesting", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = inspect_task(args) if args.task else inspect_job(args)
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
