"""Launch multiple IPMSM PyAEDT batch workers as subprocesses.

This script keeps the older project pattern:

1. The ANSYS automation script itself runs N simulations.
2. This launcher starts several automation scripts in parallel.

For this project, ``run_ipmsm_batch.py`` is the automation script.  Each
subprocess should run it with ``--workers 1`` so AEDT parallelism is controlled
by this file instead of nested multiprocessing.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any


BASE_DIR = Path(__file__).resolve().parent


def safe_name_part(value: Any) -> str:
    """Return a filesystem/CSV friendly identifier component."""
    text = str(value).strip()
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text)


def default_log_prefix() -> str:
    parts = []
    submit_index = os.environ.get("SBATCH_JOB_INDEX")
    slurm_job_id = os.environ.get("SLURM_JOB_ID")
    if submit_index:
        parts.append(f"submit{safe_name_part(submit_index)}")
    if slurm_job_id:
        parts.append(f"job{safe_name_part(slurm_job_id)}")
    return "_".join(parts) + "_" if parts else ""


def normalize_log_prefix(prefix: str) -> str:
    if not prefix:
        return ""
    normalized = safe_name_part(prefix)
    return normalized if normalized.endswith("_") else normalized + "_"


def int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return int(value)


def default_cores_per_process() -> int:
    return int_env("CORES_PER_PROCESS", 4)


def default_process_count(cores_per_process: int) -> int:
    if "NUM_PROCESSES" in os.environ:
        return int(os.environ["NUM_PROCESSES"])
    if platform.system() == "Windows":
        return 2
    slurm_cpus = int_env("SLURM_CPUS_PER_TASK", 40)
    return max(1, min(10, slurm_cpus // max(1, cores_per_process)))


def read_cases(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_cases(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def split_counts(total_count: int, processes: int) -> list[int]:
    base = total_count // processes
    remainder = total_count % processes
    return [base + (1 if index < remainder else 0) for index in range(processes)]


def split_cases(rows: list[dict[str, Any]], processes: int) -> list[list[dict[str, Any]]]:
    chunks = [[] for _ in range(processes)]
    for index, row in enumerate(rows):
        chunks[index % processes].append(row)
    return chunks


def generated_cases(log_prefix: str, process_index: int, count: int) -> list[dict[str, Any]]:
    case_prefix = normalize_log_prefix(
        log_prefix + f"p{process_index:03d}"
    ).rstrip("_")
    return [
        {"case_id": f"{case_prefix}_case_{case_index:04d}"}
        for case_index in range(1, count + 1)
    ]


def build_command(args: argparse.Namespace, process_index: int, count: int | None, cases_path: Path | None) -> list[str]:
    command = [
        args.python,
        str(args.script),
        "--workers",
        "1",
        "--cores",
        str(args.cores_per_process),
        "--simulation-dir",
        str(args.simulation_dir),
        "--result-csv",
        str(args.result_csv),
        "--symmetry-factor",
        str(args.symmetry_factor),
    ]

    if args.analyze:
        command.append("--analyze")
    else:
        command.append("--setup-only")

    command.append("--non-graphical" if args.non_graphical else "--graphical")
    command.append("--cleanup-linux" if args.cleanup_linux else "--keep-projects")

    if args.periodic_boundary:
        command.append("--periodic-boundary")

    if cases_path is not None:
        command.extend(["--cases", str(cases_path)])
    elif count is not None:
        command.extend(["--count", str(count)])
    else:
        raise RuntimeError(f"Process {process_index} has neither cases nor count.")

    return command


def parse_args() -> argparse.Namespace:
    cores_default = default_cores_per_process()
    processes_default = default_process_count(cores_default)

    parser = argparse.ArgumentParser(description="Run run_ipmsm_batch.py in parallel subprocesses.")
    parser.add_argument("--script", type=Path, default=BASE_DIR / "run_ipmsm_batch.py")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--processes", type=int, default=processes_default)
    parser.add_argument("--cores-per-process", type=int, default=cores_default)
    parser.add_argument("--count-per-process", type=int, default=int_env("COUNT_PER_PROCESS", 1))
    parser.add_argument("--total-count", type=int, default=int_env("TOTAL_COUNT", 0))
    parser.add_argument("--cases", type=Path, default=Path(os.environ["CASES_CSV"]) if os.environ.get("CASES_CSV") else None)
    parser.add_argument("--simulation-dir", type=Path, default=BASE_DIR / "simulation")
    parser.add_argument("--result-csv", type=Path, default=BASE_DIR / "ipmsm_simulation_results.csv")
    parser.add_argument("--log-dir", type=Path, default=BASE_DIR / "simul_log")
    parser.add_argument("--log-prefix", default=os.environ.get("LOG_PREFIX", ""))
    parser.add_argument("--stagger-seconds", type=float, default=float(os.environ.get("STAGGER_SECONDS", "30")))
    parser.add_argument("--symmetry-factor", type=int, default=int_env("SYMMETRY_FACTOR", 4))
    parser.add_argument("--periodic-boundary", action="store_true", default=os.environ.get("PERIODIC_BOUNDARY", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--analyze", dest="analyze", action="store_true", default=os.environ.get("SETUP_ONLY", "").lower() not in {"1", "true", "yes"})
    parser.add_argument("--setup-only", dest="analyze", action="store_false")
    parser.add_argument("--non-graphical", action="store_true", default=(platform.system() != "Windows"))
    parser.add_argument("--graphical", dest="non_graphical", action="store_false")
    parser.add_argument("--cleanup-linux", action="store_true", default=(platform.system() != "Windows"))
    parser.add_argument("--keep-projects", dest="cleanup_linux", action="store_false")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.script = args.script.resolve()
    args.simulation_dir = args.simulation_dir.resolve()
    args.result_csv = args.result_csv.resolve()
    args.log_dir = args.log_dir.resolve()
    args.log_prefix = normalize_log_prefix(args.log_prefix or default_log_prefix())

    if args.processes < 1:
        raise RuntimeError("--processes must be at least 1.")
    if not args.script.exists():
        raise FileNotFoundError(args.script)

    args.log_dir.mkdir(parents=True, exist_ok=True)
    args.simulation_dir.mkdir(parents=True, exist_ok=True)

    process_inputs: list[tuple[int | None, Path | None]] = []
    if args.cases:
        rows = read_cases(args.cases)
        chunks = split_cases(rows, args.processes)
        for index, chunk in enumerate(chunks, start=1):
            if not chunk:
                process_inputs.append((0, None))
                continue
            split_path = args.log_dir / f"{args.log_prefix}cases_process_{index:03d}.csv"
            write_cases(split_path, chunk)
            process_inputs.append((None, split_path))
    else:
        total_count = args.total_count if args.total_count > 0 else args.processes * args.count_per_process
        for index, count in enumerate(split_counts(total_count, args.processes), start=1):
            if count <= 0:
                process_inputs.append((0, None))
                continue
            generated_path = args.log_dir / f"{args.log_prefix}generated_cases_process_{index:03d}.csv"
            write_cases(generated_path, generated_cases(args.log_prefix, index, count))
            process_inputs.append((None, generated_path))

    processes: list[tuple[int, subprocess.Popen[Any], Any]] = []
    for process_index, (count, cases_path) in enumerate(process_inputs, start=1):
        if count == 0 and cases_path is None:
            continue
        log_path = args.log_dir / f"{args.log_prefix}process_{process_index:03d}.log"
        command = build_command(args, process_index, count, cases_path)
        log_file = log_path.open("w", encoding="utf-8", buffering=1)
        log_file.write("COMMAND: " + " ".join(command) + "\n\n")
        log_file.flush()

        env = os.environ.copy()
        env["IPMSM_PROCESS_INDEX"] = str(process_index)
        process = subprocess.Popen(command, cwd=BASE_DIR, stdout=log_file, stderr=subprocess.STDOUT, env=env)
        processes.append((process_index, process, log_file))
        print(f"Started process {process_index}: pid={process.pid}, log={log_path}")
        time.sleep(args.stagger_seconds)

    failed = 0
    for process_index, process, log_file in processes:
        return_code = process.wait()
        log_file.write(f"\nProcess {process_index} finished with return code {return_code}\n")
        log_file.close()
        print(f"Finished process {process_index}: return_code={return_code}")
        if return_code != 0:
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
