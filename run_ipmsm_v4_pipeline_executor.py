"""Durable Task-Scheduler wrapper for the executing IPMSM v4 supervisor.

The immutable v4 supervisor remains the sole pipeline state machine.  This
wrapper only publishes a non-authoritative PID marker and redirects its own and
child-process output to persistent runtime logs.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import traceback
from typing import Any

from run_ipmsm_pipeline_supervisor import (
    _atomic_pid_marker,
    _open_runtime_logs,
    _remove_own_marker,
)
import supervise_ipmsm_v2_pipeline_v4 as supervisor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--stdout-log", type=Path, required=True)
    parser.add_argument("--stderr-log", type=Path, required=True)
    parser.add_argument("--max-transitions", type=int, default=16)
    return parser


def main(argv: list[str] | None = None, *, api: Any = supervisor) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.max_transitions <= api.MAX_TRANSITIONS:
        raise ValueError(
            f"--max-transitions must be between 1 and {api.MAX_TRANSITIONS}"
        )
    contract = args.contract.resolve(strict=True)
    pid_file = args.pid_file.resolve(strict=False)
    stdout_log = args.stdout_log.resolve(strict=False)
    stderr_log = args.stderr_log.resolve(strict=False)
    _atomic_pid_marker(pid_file)
    streams = None
    original_stdout, original_stderr = sys.stdout, sys.stderr
    original_subprocess_run = api.v3.subprocess.run
    try:
        streams = _open_runtime_logs(stdout_log, stderr_log)
        sys.stdout, sys.stderr = streams

        def run_with_runtime_logs(*run_args: object, **run_kwargs: object) -> object:
            if not run_kwargs.get("capture_output"):
                run_kwargs.setdefault("stdout", sys.stdout)
                run_kwargs.setdefault("stderr", sys.stderr)
            return original_subprocess_run(*run_args, **run_kwargs)

        api.v3.subprocess.run = run_with_runtime_logs
        print(
            "pipeline_v4_executor_start "
            f"time={datetime.now(timezone.utc).isoformat()} "
            f"pid={api.os.getpid() if hasattr(api, 'os') else 'unknown'} "
            f"contract={contract.name}",
            flush=True,
        )
        try:
            return api.main(
                [
                    "--contract",
                    str(contract),
                    "--execute",
                    "--max-transitions",
                    str(args.max_transitions),
                ]
            )
        except (api.PipelineContractError, api.PipelineStateError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr, flush=True)
            return 2
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return 1
    finally:
        _remove_own_marker(pid_file)
        api.v3.subprocess.run = original_subprocess_run
        sys.stdout, sys.stderr = original_stdout, original_stderr
        if streams is not None:
            for stream in streams:
                try:
                    stream.close()
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
