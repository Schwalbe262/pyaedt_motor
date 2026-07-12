"""Task-Scheduler entrypoint for the durable IPMSM pipeline supervisor.

It redirects the complete process tree to bounded-purpose runtime logs and
publishes a non-authoritative PID marker for the read-only dashboard.  Pipeline
state and safety still come exclusively from ``supervise_ipmsm_v2_pipeline``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import traceback
from typing import TextIO

import supervise_ipmsm_v2_pipeline as supervisor


LEGACY_UNAUTHORIZED_DOWNSTREAM_ACTIONS = frozenset(
    {
        "run_stage1_validation",
        "run_stage1_training",
        "run_stage2_fresh",
        "run_stage2_resume",
        "merge_stage12_plan",
        "generate_stage3_plan",
        "run_stage3_fresh",
        "run_stage3_resume",
        "run_optimization_fresh",
        "run_optimization_resume",
        "run_speed_plan",
        "run_speed_campaign",
        "run_speed_rank",
        "commit_speed_completion",
    }
)


def _execute_with_authorization_guard(
    executor: object,
    contract: object,
    snapshot: object,
) -> object:
    """Keep the durable v3 task from crossing the v4 human-authorization gate."""

    action = getattr(snapshot, "next_action", None)
    if action in LEGACY_UNAUTHORIZED_DOWNSTREAM_ACTIONS:
        raise supervisor.PipelineStateError(
            "legacy automatic post-campaign execution is disabled; "
            "activate the audited v4 supervisor before continuing"
        )
    return executor(contract, snapshot)  # type: ignore[operator]


def _atomic_pid_marker(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(f"{os.getpid()}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _remove_own_marker(path: Path) -> None:
    try:
        if path.read_text(encoding="ascii").strip() == str(os.getpid()):
            path.unlink()
    except (FileNotFoundError, OSError, UnicodeError):
        pass


def _open_runtime_logs(stdout_path: Path, stderr_path: Path) -> tuple[TextIO, TextIO]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_stream = stdout_path.open("a", encoding="utf-8", buffering=1)
    stderr_stream = stderr_path.open("a", encoding="utf-8", buffering=1)
    return stdout_stream, stderr_stream


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--stdout-log", type=Path, required=True)
    parser.add_argument("--stderr-log", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    contract = args.contract.resolve(strict=True)
    pid_file = args.pid_file.resolve(strict=False)
    stdout_log = args.stdout_log.resolve(strict=False)
    stderr_log = args.stderr_log.resolve(strict=False)
    _atomic_pid_marker(pid_file)
    streams: tuple[TextIO, TextIO] | None = None
    original_stdout, original_stderr = sys.stdout, sys.stderr
    original_subprocess_run = supervisor.subprocess.run
    original_execute_action = supervisor.execute_action
    try:
        streams = _open_runtime_logs(stdout_log, stderr_log)
        sys.stdout, sys.stderr = streams

        def run_with_runtime_logs(*run_args: object, **run_kwargs: object) -> object:
            if not run_kwargs.get("capture_output"):
                run_kwargs.setdefault("stdout", sys.stdout)
                run_kwargs.setdefault("stderr", sys.stderr)
            return original_subprocess_run(*run_args, **run_kwargs)

        supervisor.subprocess.run = run_with_runtime_logs  # type: ignore[assignment]

        def execute_with_authorization_guard(contract: object, snapshot: object) -> object:
            return _execute_with_authorization_guard(
                original_execute_action,
                contract,
                snapshot,
            )

        supervisor.execute_action = execute_with_authorization_guard  # type: ignore[assignment]
        print(
            "pipeline_supervisor_start "
            f"time={datetime.now(timezone.utc).isoformat()} pid={os.getpid()} contract={contract.name}",
            flush=True,
        )
        try:
            return supervisor.main(["--contract", str(contract), "--execute"])
        except (supervisor.PipelineContractError, supervisor.PipelineStateError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr, flush=True)
            return 2
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return 1
    finally:
        _remove_own_marker(pid_file)
        supervisor.subprocess.run = original_subprocess_run
        supervisor.execute_action = original_execute_action
        sys.stdout, sys.stderr = original_stdout, original_stderr
        if streams is not None:
            for stream in streams:
                try:
                    stream.close()
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
