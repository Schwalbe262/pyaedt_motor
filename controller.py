"""Periodically submit IPMSM Slurm batch jobs.

Default behavior matches the older project controller pattern:

1. Cancel this user's Slurm jobs named ``ANSYS``.
2. Submit ``simulation1.sh`` 10 times with 60 seconds between submissions.
   Each subprocess runs 1000 fresh random cases unless overridden.
3. Wait 12 hours and repeat forever.
"""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import time


BASE_DIR = Path(__file__).resolve().parent


def rm_if_exists(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()


def clean_runtime_files() -> None:
    for relative in (
        "simulation",
        "error",
        "log",
        "simul_log",
        "simulation_log",
        "batch.log",
        "info.log",
        "log.csv",
        "log.txt",
        "run_debug.log",
        "simulation_num.txt",
        "simulog_num.txt",
    ):
        rm_if_exists(BASE_DIR / relative)

    for path in BASE_DIR.glob("mono_crash*"):
        rm_if_exists(path)

    (BASE_DIR / "log").mkdir(exist_ok=True)
    (BASE_DIR / "simul_log").mkdir(exist_ok=True)
    (BASE_DIR / "simulation").mkdir(exist_ok=True)


def cancel_ansys_jobs() -> None:
    username = getpass.getuser()
    print(f"Canceling existing Slurm jobs named ANSYS for user={username}.")
    subprocess.run(
        ["scancel", "-u", username, "--name=ANSYS"],
        cwd=BASE_DIR,
        check=False,
    )


def submit_job(args: argparse.Namespace, job_index: int) -> None:
    loops_per_process = args.loops_per_process
    export_values = {
        "ALL": None,
        "NUM_PROCESSES": args.processes,
        "CORES_PER_PROCESS": args.cores_per_process,
        "COUNT_PER_PROCESS": loops_per_process,
        "LOOPS_PER_PROCESS": loops_per_process,
        "TOTAL_COUNT": args.total_count,
        "RESULT_CSV": args.result_csv,
        "SIMULATION_DIR": args.simulation_dir,
        "STAGGER_SECONDS": args.stagger_seconds,
        "SBATCH_JOB_INDEX": job_index,
        "SETUP_ONLY": int(args.setup_only),
        "KEEP_PROJECTS": int(args.keep_projects),
        "PERIODIC_BOUNDARY": int(args.periodic_boundary),
    }
    if args.cases:
        export_values["CASES_CSV"] = args.cases

    export_arg = ",".join(
        key if value is None else f"{key}={value}"
        for key, value in export_values.items()
    )

    command = ["sbatch", f"--export={export_arg}", str(args.script)]
    print(f"Submitting job {job_index}/{args.jobs}: {' '.join(command)}")
    subprocess.run(command, cwd=BASE_DIR, check=True)


def parse_args() -> argparse.Namespace:
    cancel_default = os.environ.get("CANCEL_EXISTING", "1").lower() not in {"0", "false", "no"}
    parser = argparse.ArgumentParser(description="Submit simulation1.sh Slurm jobs.")
    parser.add_argument("--script", default="simulation1.sh")
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("SBATCH_JOBS", "10")))
    parser.add_argument("--interval-seconds", type=float, default=float(os.environ.get("SBATCH_INTERVAL_SECONDS", "60")))
    parser.add_argument("--repeat-every-hours", type=float, default=float(os.environ.get("SBATCH_REPEAT_EVERY_HOURS", "12")))
    parser.add_argument("--clean", action="store_true", help="Delete runtime folders/files before submitting.")
    parser.add_argument("--cancel-existing", dest="cancel_existing", action="store_true", default=cancel_default, help="Cancel this user's existing Slurm jobs named ANSYS before each submit cycle.")
    parser.add_argument("--no-cancel-existing", dest="cancel_existing", action="store_false", help="Do not cancel existing Slurm jobs before each submit cycle.")
    parser.add_argument("--processes", type=int, default=int(os.environ.get("NUM_PROCESSES", "10")))
    parser.add_argument("--cores-per-process", type=int, default=int(os.environ.get("CORES_PER_PROCESS", "4")))
    loops_default = int(os.environ.get("LOOPS_PER_PROCESS", os.environ.get("COUNT_PER_PROCESS", "1000")))
    parser.add_argument("--count-per-process", "--loops-per-process", dest="loops_per_process", type=int, default=loops_default, help="Number of fresh random simulation cases each subprocess runs.")
    parser.add_argument("--total-count", type=int, default=int(os.environ.get("TOTAL_COUNT", "0")))
    parser.add_argument("--result-csv", default=os.environ.get("RESULT_CSV", "ipmsm_simulation_results.csv"))
    parser.add_argument("--simulation-dir", default=os.environ.get("SIMULATION_DIR", "simulation"))
    parser.add_argument("--stagger-seconds", type=float, default=float(os.environ.get("STAGGER_SECONDS", "30")))
    parser.add_argument("--cases", default=os.environ.get("CASES_CSV", ""))
    parser.add_argument("--setup-only", action="store_true", default=os.environ.get("SETUP_ONLY", "") in {"1", "true", "TRUE"})
    parser.add_argument("--keep-projects", action="store_true", default=os.environ.get("KEEP_PROJECTS", "") in {"1", "true", "TRUE"})
    parser.add_argument("--periodic-boundary", action="store_true", default=os.environ.get("PERIODIC_BOUNDARY", "") in {"1", "true", "TRUE"})
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise RuntimeError("--jobs must be at least 1.")

    cycle = 1
    while True:
        print(
            f"Starting submit cycle {cycle}: jobs={args.jobs}, "
            f"interval_seconds={args.interval_seconds}, repeat_every_hours={args.repeat_every_hours}."
        )
        if args.cancel_existing:
            cancel_ansys_jobs()
        if args.clean:
            clean_runtime_files()

        for job_index in range(1, args.jobs + 1):
            submit_job(args, job_index)
            if job_index < args.jobs:
                time.sleep(args.interval_seconds)

        if args.repeat_every_hours <= 0:
            break
        sleep_seconds = args.repeat_every_hours * 3600
        print(f"Sleeping {sleep_seconds:.0f} seconds before next submit cycle.")
        time.sleep(sleep_seconds)
        cycle += 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
