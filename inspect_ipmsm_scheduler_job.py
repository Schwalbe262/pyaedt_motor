"""Inspect a Slurm Scheduler job with filtered log output."""

from __future__ import annotations

import argparse
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


def get_json(scheduler_url: str, path: str, query: dict[str, str] | None = None, timeout: float = 10.0) -> Any:
    url = scheduler_url.rstrip("/") + path
    if query:
        url += "?" + parse.urlencode(query)
    with request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body)


def selected_job_fields(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "job_name",
        "status",
        "job_mode",
        "repo_url",
        "git_ref",
        "entrypoint",
        "arguments",
        "remote_path",
        "remote_job_dir",
        "stdout_path",
        "stderr_path",
        "failure_message",
        "simulation_count",
        "created_at",
        "submitted_at",
        "updated_at",
        "finished_at",
    )
    return {key: job.get(key) for key in keys if key in job}


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


def fetch_remote_file(
    scheduler_url: str,
    job_id: int,
    path: str,
    base: str,
    timeout: float,
) -> str:
    response = get_json(
        scheduler_url,
        f"/api/jobs/{job_id}/remote-file",
        query={"path": path, "base": base},
        timeout=timeout,
    )
    return unwrap_remote_file_response(response)


def inspect_job(args: argparse.Namespace) -> dict[str, Any]:
    job = get_json(args.scheduler_url, f"/api/jobs/{args.job_id}", timeout=args.timeout)
    result: dict[str, Any] = {"job": selected_job_fields(job)}
    requested_logs: list[tuple[str, str]] = []
    if args.stdout and job.get("stdout_path"):
        requested_logs.append(("stdout", str(job["stdout_path"])))
    if args.stderr and job.get("stderr_path"):
        requested_logs.append(("stderr", str(job["stderr_path"])))
    for label, path in requested_logs:
        try:
            text = fetch_remote_file(args.scheduler_url, args.job_id, path, args.base, args.timeout)
        except Exception as exc:
            result[label] = {"path": path, "error": str(exc)}
            continue
        result[label] = {"path": path, **summarize_log_text(text, args.tail_lines, args.max_interesting)}
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect a scheduler job without dumping full logs.")
    parser.add_argument("job_id", type=int)
    parser.add_argument("--scheduler-url", default=DEFAULT_SCHEDULER_URL)
    parser.add_argument("--stdout", action="store_true", help="Fetch and filter stdout_path.")
    parser.add_argument("--stderr", action="store_true", help="Fetch and filter stderr_path.")
    parser.add_argument("--base", default="remote_path", help="remote-file base parameter.")
    parser.add_argument("--tail-lines", type=int, default=20)
    parser.add_argument("--max-interesting", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(json.dumps(inspect_job(args), indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
