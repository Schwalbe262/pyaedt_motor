"""Prepare or submit an IPMSM scheduler job through the local Slurm Scheduler API."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import posixpath
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any
from urllib import parse, request

import subprocess_run


DEFAULT_SCHEDULER_URL = "http://localhost:8000"
DEFAULT_MAX_CASES = 200
DEFAULT_BOOTSTRAP_MAX_BYTES = 50_000
HTML_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def git_value(args: list[str], default: str = "") -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).resolve().parent,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return default
    return result.stdout.strip() or default


def default_git_ref() -> str:
    branch = git_value(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch and branch != "HEAD":
        return branch
    return git_value(["rev-parse", "HEAD"], default="main")


def default_repo_url() -> str:
    return git_value(["config", "--get", "remote.origin.url"])


def load_and_validate_cases(cases_path: Path, max_cases: int, allow_over_budget: bool) -> list[dict[str, Any]]:
    rows = subprocess_run.read_cases(cases_path)
    subprocess_run.validate_explicit_case_plan(rows, max_cases=max_cases, allow_over_budget=allow_over_budget)
    return rows


def build_subprocess_arguments(args: argparse.Namespace) -> str:
    command = [
        "--cases",
        args.remote_cases or str(args.cases),
        "--processes",
        str(args.processes),
        "--cores-per-process",
        str(args.cores_per_process),
        "--max-cases",
        str(args.max_cases),
        "--stagger-seconds",
        str(args.stagger_seconds),
        "--simulation-dir",
        args.simulation_dir,
        "--result-csv",
        args.result_csv,
        "--log-dir",
        args.log_dir,
        "--log-prefix",
        args.log_prefix,
    ]
    command.append("--analyze" if args.analyze else "--setup-only")
    if args.allow_over_budget:
        command.append("--allow-over-budget")
    if args.periodic_boundary:
        command.append("--periodic-boundary")
    if args.keep_projects:
        command.append("--keep-projects")
    return shlex.join(command)


def build_cases_csv_text(rows: list[dict[str, Any]]) -> str:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def build_remote_cases_bootstrap(remote_cases: str, rows: list[dict[str, Any]], max_bytes: int) -> str:
    csv_text = build_cases_csv_text(rows)
    size = len(csv_text.encode("utf-8"))
    if size > max_bytes:
        raise RuntimeError(f"remote case CSV bootstrap is {size} bytes, exceeding --bootstrap-max-bytes={max_bytes}")
    quoted_path = shlex.quote(remote_cases)
    quoted_dir = shlex.quote(posixpath.dirname(remote_cases) or ".")
    return "\n".join(
        [
            f"mkdir -p {quoted_dir}",
            f"cat > {quoted_path} <<'IPMSM_CASES_CSV'",
            csv_text.rstrip("\n"),
            "IPMSM_CASES_CSV",
        ]
    )


def append_env_setup(existing: str, extra: str) -> str:
    if not existing:
        return extra
    return existing.rstrip() + "\n" + extra


def read_env_setup_file(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def build_remote_entrypoint_validation(entrypoint: str) -> str:
    required_paths = list(dict.fromkeys([entrypoint, "run_ipmsm_batch.py"]))
    lines = []
    for path in required_paths:
        quoted_path = shlex.quote(path)
        lines.append(
            f"test -f {quoted_path} || "
            f'{{ echo "ERROR: required scheduler file missing: {path}" >&2; exit 2; }}'
        )
    return "\n".join(lines)


def build_remote_probe(remote_probe_output: str, entrypoint: str) -> str:
    quoted_output = shlex.quote(remote_probe_output)
    parent = posixpath.dirname(remote_probe_output)
    setup_lines = []
    if parent and parent != ".":
        setup_lines.append(f"mkdir -p {shlex.quote(parent)}")
    setup_lines.extend(
        [
            "{",
            "echo SCHEDULER_REMOTE_PROBE=1",
            "printf 'PWD='; pwd",
            "echo HOME=${HOME:-}",
            "echo USER=${USER:-}",
            "python --version || true",
            f"test -f {shlex.quote(entrypoint)} && echo entrypoint_ok={entrypoint} || echo entrypoint_missing={entrypoint}",
            "test -f run_ipmsm_batch.py && echo run_ipmsm_batch_ok=1 || echo run_ipmsm_batch_missing=1",
            "test -d remote && echo remote_dir_ok=1 || echo remote_dir_missing=1",
            "ls -ld . || true",
            f"}} > {quoted_output} 2>&1",
        ]
    )
    return "\n".join(setup_lines)


def build_job_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "job_mode": args.job_mode,
        "repo_url": args.repo_url,
        "git_ref": args.git_ref,
        "entrypoint": args.entrypoint,
        "arguments": build_subprocess_arguments(args),
        "env_setup": args.env_setup,
        "required_capability": args.required_capability,
        "env_profile": args.env_profile,
        "account_name": args.account_name,
        "partition": args.partition,
        "time_limit": args.time_limit,
        "cpus": args.cpus,
        "memory": args.memory,
        "gpus": args.gpus,
        "gpu_model": args.gpu_model,
        "node_name": args.node_name,
        "exclusive_node": args.exclusive_node,
        "job_name": args.job_name,
        "remote_path": args.remote_path,
        "total_simulations": args.total_simulations,
        "simulations_per_job": args.simulations_per_job,
        "cpus_per_simulation": args.cpus_per_simulation,
        "mem_per_simulation_gb": args.mem_per_simulation_gb,
        "max_workers_per_job": args.max_workers_per_job,
        "max_new_jobs": args.max_new_jobs,
        "oversubscribe_factor": args.oversubscribe_factor,
        "load_target": args.load_target,
        "ramp_interval_seconds": args.ramp_interval_seconds,
    }


def validate_scheduler_request(args: argparse.Namespace) -> None:
    if args.job_mode == "python_git" and not args.repo_url:
        raise RuntimeError("--repo-url is required for --job-mode python_git")
    if args.job_mode in {"packed_srun", "dynamic_packed_srun"} and not args.remote_path:
        raise RuntimeError(f"--remote-path is required for --job-mode {args.job_mode}")
    if args.submit and args.analyze and not args.confirm_analyze:
        raise RuntimeError("scheduler solve submission requires --confirm-analyze with --analyze")
    if args.submit and not args.remote_cases and args.cases.is_absolute():
        raise RuntimeError("absolute local --cases requires --remote-cases for scheduler submission")
    if args.bootstrap_remote_cases and not (args.remote_cases or not args.cases.is_absolute()):
        raise RuntimeError("--bootstrap-remote-cases with an absolute --cases path requires --remote-cases")


def compact_non_json_response(body: str) -> dict[str, Any]:
    stripped = body.strip()
    prefix = stripped[:300].lower()
    response_format = "html" if prefix.startswith("<!doctype") or "<html" in prefix else "text"
    summary: dict[str, Any] = {
        "response_format": response_format,
        "response_chars": len(body),
        "response_bytes": len(body.encode("utf-8")),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
    }
    title_match = HTML_TITLE_PATTERN.search(body)
    if title_match:
        summary["title"] = " ".join(title_match.group(1).split())
    if response_format != "html" and stripped:
        summary["snippet"] = " ".join(stripped.split())[:240]
    return summary


def post_scheduler_job(scheduler_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = scheduler_url.rstrip("/") + "/jobs"
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


def get_scheduler_health(scheduler_url: str, timeout: float) -> dict[str, Any]:
    url = scheduler_url.rstrip("/") + "/api/health"
    with request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw_response": body}


def get_scheduler_jobs(scheduler_url: str, timeout: float) -> list[dict[str, Any]]:
    url = scheduler_url.rstrip("/") + "/api/jobs"
    with request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, list):
        raise RuntimeError("/api/jobs did not return a JSON list")
    return [job for job in data if isinstance(job, dict)]


def job_id_set(jobs: list[dict[str, Any]]) -> set[int]:
    ids: set[int] = set()
    for job in jobs:
        try:
            ids.add(int(job["id"]))
        except Exception:
            continue
    return ids


def job_id(job: dict[str, Any]) -> int | None:
    try:
        return int(job["id"])
    except Exception:
        return None


def selected_submit_job_fields(job: dict[str, Any]) -> dict[str, Any]:
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
        "remote_path",
        "remote_job_dir",
        "stdout_path",
        "stderr_path",
        "failure_message",
        "created_at",
        "submitted_at",
        "updated_at",
        "finished_at",
    )
    return {key: job.get(key) for key in keys if key in job}


def find_submitted_job(
    jobs: list[dict[str, Any]],
    previous_ids: set[int],
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    submitted_jobs = find_submitted_jobs(jobs, previous_ids, args)
    return submitted_jobs[0] if submitted_jobs else None


def find_submitted_jobs(
    jobs: list[dict[str, Any]],
    previous_ids: set[int],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    new_jobs = [job for job in jobs if job_id(job) not in previous_ids]
    candidates = new_jobs or jobs
    if args.job_mode == "dynamic_packed_srun":
        prefix = f"{args.job_name}-"
        matches = [
            job
            for job in candidates
            if str(job.get("job_name") or "").startswith(prefix)
            and job.get("job_mode") == "packed_srun"
            and job.get("entrypoint") == args.entrypoint
            and job.get("remote_path") == args.remote_path
        ]
        return sorted(matches, key=lambda job: job_id(job) or 0)
    for job in candidates:
        if (
            job.get("job_name") == args.job_name
            and job.get("job_mode") == args.job_mode
            and job.get("entrypoint") == args.entrypoint
            and job.get("git_ref") == args.git_ref
        ):
            return [job]
    return candidates[:1]


def write_manifest(path: Path, output: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(output, indent=2, sort_keys=True) + "\n")


def redacted_env_setup(value: str) -> str:
    if not value:
        return value
    encoded = value.encode("utf-8")
    line_count = value.count("\n") + 1
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    return f"<redacted env_setup bytes={len(encoded)} lines={line_count} sha256={digest}>"


def build_stdout_output(output: dict[str, Any], show_env_setup: bool) -> dict[str, Any]:
    if show_env_setup:
        return output
    stdout_output = json.loads(json.dumps(output))
    payload = stdout_output.get("payload")
    if isinstance(payload, dict) and payload.get("env_setup"):
        payload["env_setup"] = redacted_env_setup(str(payload["env_setup"]))
        stdout_output["output_redactions"] = {
            "payload.env_setup": "redacted in stdout; use --show-env-setup or --write-manifest for the full script"
        }
    return stdout_output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run or submit an IPMSM job to the Slurm Scheduler API.")
    parser.add_argument("--scheduler-url", default=DEFAULT_SCHEDULER_URL)
    parser.add_argument("--cases", type=Path, required=True, help="Local case CSV to validate before submission.")
    parser.add_argument("--remote-cases", default="", help="Case CSV path visible to the scheduler job; defaults to --cases.")
    parser.add_argument("--bootstrap-remote-cases", action="store_true", help="Embed the validated case CSV into env_setup for remote job startup.")
    parser.add_argument("--bootstrap-max-bytes", type=int, default=DEFAULT_BOOTSTRAP_MAX_BYTES)
    parser.add_argument("--repo-url", default=default_repo_url())
    parser.add_argument("--git-ref", default=default_git_ref())
    parser.add_argument("--job-mode", choices=("python_git", "packed_srun", "dynamic_packed_srun"), default="python_git")
    parser.add_argument("--remote-path", default="", help="Scheduler-accessible working directory for packed_srun mode.")
    parser.add_argument("--entrypoint", default="subprocess_run.py")
    parser.add_argument("--job-name", default="ipmsm-replay-setup")
    parser.add_argument("--processes", type=int, default=1)
    parser.add_argument("--cores-per-process", type=int, default=4)
    parser.add_argument("--max-cases", type=int, default=DEFAULT_MAX_CASES)
    parser.add_argument("--allow-over-budget", action="store_true")
    parser.add_argument("--stagger-seconds", type=float, default=30.0)
    parser.add_argument("--simulation-dir", default="simulation")
    parser.add_argument("--result-csv", default="ipmsm_scheduler_results.csv")
    parser.add_argument("--log-dir", default="simul_log_scheduler")
    parser.add_argument("--log-prefix", default="scheduler_")
    parser.add_argument("--analyze", action="store_true", help="Run solves; default is setup-only.")
    parser.add_argument("--confirm-analyze", action="store_true", help="Allow --submit with --analyze.")
    parser.add_argument("--periodic-boundary", action="store_true")
    parser.add_argument("--keep-projects", action="store_true")
    parser.add_argument("--env-setup", default="")
    parser.add_argument("--env-setup-file", type=Path, default=None, help="Read additional scheduler env setup shell from a local file.")
    parser.add_argument("--remote-probe-output", default="", help="Write scheduler working-tree diagnostics to this remote file before validation.")
    parser.add_argument("--validate-remote-entrypoint", action="store_true", help="Check expected project files in the scheduler working tree before running.")
    parser.add_argument("--required-capability", default="")
    parser.add_argument("--env-profile", default="")
    parser.add_argument("--account-name", default="", help="Exact scheduler account constraint, or comma-separated ordered candidates.")
    parser.add_argument("--partition", default="auto")
    parser.add_argument("--time-limit", default="01:00:00")
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory", default="16G")
    parser.add_argument("--gpus", type=int, default=0)
    parser.add_argument("--gpu-model", default="")
    parser.add_argument("--node-name", default="")
    parser.add_argument("--exclusive-node", action="store_true")
    parser.add_argument("--total-simulations", type=int, default=0, help="Scheduler simulation count; 0 uses validated case rows.")
    parser.add_argument("--simulations-per-job", type=int, default=1)
    parser.add_argument("--cpus-per-simulation", type=int, default=4)
    parser.add_argument("--mem-per-simulation-gb", type=float, default=4.0)
    parser.add_argument("--max-workers-per-job", type=int, default=1)
    parser.add_argument("--max-new-jobs", type=int, default=1)
    parser.add_argument("--oversubscribe-factor", type=float, default=1.0)
    parser.add_argument("--load-target", type=float, default=0.75)
    parser.add_argument("--ramp-interval-seconds", type=int, default=900)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--check-health", action="store_true")
    parser.add_argument("--write-manifest", type=Path, help="Write the review JSON payload to this local path.")
    parser.add_argument("--show-env-setup", action="store_true", help="Print full env_setup in stdout instead of redacting it.")
    parser.add_argument("--submit", action="store_true", help="POST to scheduler. Omit for dry-run JSON only.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validate_scheduler_request(args)
    rows = load_and_validate_cases(args.cases, args.max_cases, args.allow_over_budget)
    if args.total_simulations <= 0:
        args.total_simulations = len(rows)
    if args.env_setup_file is not None:
        args.env_setup = append_env_setup(args.env_setup, read_env_setup_file(args.env_setup_file))
    if args.remote_probe_output:
        args.env_setup = append_env_setup(args.env_setup, build_remote_probe(args.remote_probe_output, args.entrypoint))
    if args.validate_remote_entrypoint:
        args.env_setup = append_env_setup(args.env_setup, build_remote_entrypoint_validation(args.entrypoint))
    if args.bootstrap_remote_cases:
        remote_cases = args.remote_cases or str(args.cases)
        args.env_setup = append_env_setup(
            args.env_setup,
            build_remote_cases_bootstrap(remote_cases, rows, args.bootstrap_max_bytes),
        )
    payload = build_job_payload(args)
    output: dict[str, Any] = {
        "scheduler_url": args.scheduler_url,
        "submit": args.submit,
        "validated_cases": len(rows),
        "payload": payload,
    }
    if args.check_health:
        output["health"] = get_scheduler_health(args.scheduler_url, args.timeout)
    if args.submit:
        pre_submit_job_ids: set[int] = set()
        try:
            pre_submit_job_ids = job_id_set(get_scheduler_jobs(args.scheduler_url, args.timeout))
        except Exception as exc:
            output["pre_submit_job_lookup_error"] = str(exc)
        output["response"] = post_scheduler_job(args.scheduler_url, payload, args.timeout)
        try:
            submitted_jobs = find_submitted_jobs(
                get_scheduler_jobs(args.scheduler_url, args.timeout),
                pre_submit_job_ids,
                args,
            )
            if submitted_jobs:
                output["submitted_job"] = selected_submit_job_fields(submitted_jobs[0])
                if len(submitted_jobs) > 1 or args.job_mode == "dynamic_packed_srun":
                    output["submitted_jobs"] = [selected_submit_job_fields(job) for job in submitted_jobs]
        except Exception as exc:
            output["submitted_job_lookup_error"] = str(exc)
    if args.write_manifest:
        output["manifest_path"] = str(args.write_manifest)
        write_manifest(args.write_manifest, output)
    print(json.dumps(build_stdout_output(output, args.show_env_setup), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
