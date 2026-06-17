"""Synchronize IPMSM scheduler replay evidence and refill open FEA slots."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Iterable

from inspect_ipmsm_scheduler_job import fetch_task_remote_file
from submit_ipmsm_scheduler_task import ANSYS_ELECTRONICS_MODULE
from summarize_ipmsm_partial_replay import summarize_partial_replay, write_summary


DEFAULT_NODES = ("n107", "n108", "n109", "n110", "n114", "n115")
NONTERMINAL_STATUSES = {"queued", "attaching", "running"}


@dataclass(frozen=True)
class TaskRow:
    id: int
    name: str
    status: str
    node_name: str
    remote_cwd: str


def case_number_from_task_name(name: str) -> int | None:
    match = re.search(r"fea-(\d{3})-", name)
    return int(match.group(1)) if match else None


def case_number_from_probe_name(batch: int, name: str) -> int | None:
    pattern = rf"batch{batch}_fea_task_(\d{{3}})_[^_]+_b{batch}_\1_results_probe\.csv$"
    match = re.search(pattern, name)
    return int(match.group(1)) if match else None


def node_for_case(case_number: int, nodes: tuple[str, ...] = DEFAULT_NODES) -> str:
    if case_number < 1:
        raise ValueError("case_number must be >= 1")
    if not nodes:
        raise ValueError("nodes must not be empty")
    return nodes[(case_number - 1) % len(nodes)]


def status_counts(tasks: Iterable[TaskRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    return dict(sorted(counts.items()))


def nonterminal_count(tasks: Iterable[TaskRow]) -> int:
    return sum(1 for task in tasks if task.status in NONTERMINAL_STATUSES)


def max_case_number(tasks: Iterable[TaskRow]) -> int:
    maximum = 0
    for task in tasks:
        case_number = case_number_from_task_name(task.name)
        if case_number is not None:
            maximum = max(maximum, case_number)
    return maximum


def local_probe_cases(root: Path, batch: int) -> set[int]:
    cases: set[int] = set()
    for path in root.glob(f"batch{batch}_fea_task_*_results_probe.csv"):
        case_number = case_number_from_probe_name(batch, path.name)
        if case_number is not None:
            cases.add(case_number)
    return cases


def missing_completed_tasks(tasks: Iterable[TaskRow], local_cases: set[int]) -> list[tuple[int, TaskRow]]:
    missing: list[tuple[int, TaskRow]] = []
    for task in tasks:
        if task.status != "completed":
            continue
        case_number = case_number_from_task_name(task.name)
        if case_number is not None and case_number not in local_cases:
            missing.append((case_number, task))
    return sorted(missing, key=lambda item: item[0])


def planned_refill_cases(current_max_case: int, active_count: int, active_cap: int, batch_case_limit: int) -> list[int]:
    if active_count >= active_cap:
        return []
    open_slots = active_cap - active_count
    start = current_max_case + 1
    stop = min(batch_case_limit, current_max_case + open_slots)
    return list(range(start, stop + 1))


def remote_result_path(batch: int, case_number: int, node_name: str) -> str:
    tag = f"batch{batch}_fea_task_{case_number:03d}_{node_name}_b{batch}_{case_number:03d}"
    return f"simul_log_scheduler/{tag}_results.csv"


def local_probe_path(root: Path, batch: int, case_number: int, node_name: str) -> Path:
    return root / f"batch{batch}_fea_task_{case_number:03d}_{node_name}_b{batch}_{case_number:03d}_results_probe.csv"


def normalize_header(fieldnames: list[str] | None) -> list[str]:
    return [field.lstrip("\ufeff") if index == 0 else field for index, field in enumerate(fieldnames or [])]


def read_single_result_row(path: Path, expected_header: list[str] | None) -> tuple[list[str], dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        header = normalize_header(reader.fieldnames)
        if expected_header is not None and header != expected_header:
            raise ValueError(f"header mismatch after BOM normalization: {path}")
        rows = []
        for raw in reader:
            rows.append({(key.lstrip("\ufeff") if key else key): value for key, value in raw.items()})
    if len(rows) != 1:
        raise ValueError(f"expected one result row in {path}, found {len(rows)}")
    return header, rows[0]


def build_selected_results(root: Path, batch: int, output: Path) -> dict[str, object]:
    probe_paths = []
    for path in root.glob(f"batch{batch}_fea_task_*_results_probe.csv"):
        case_number = case_number_from_probe_name(batch, path.name)
        if case_number is not None:
            probe_paths.append((case_number, path))
    probe_paths.sort()

    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for _, path in probe_paths:
        header, row = read_single_result_row(path, header)
        rows.append(row)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=header or [])
        writer.writeheader()
        writer.writerows(rows)

    failed_cases = failed_case_numbers(rows)
    return {
        "output": str(output),
        "rows": len(rows),
        "ok": len(rows) - len(failed_cases),
        "failed": len(failed_cases),
        "failed_cases": failed_cases,
    }


def failed_case_numbers(rows: Iterable[dict[str, str]]) -> list[int]:
    failed: list[int] = []
    for selected_row, row in enumerate(rows, start=1):
        if (row.get("status") or "").strip().lower() == "ok":
            continue
        match = re.search(r"replay\d+_mtf_(\d{4})_", row.get("case_id", ""))
        failed.append(int(match.group(1)) if match else selected_row)
    return failed


def read_tasks_from_db(db_path: Path, name_glob: str) -> list[TaskRow]:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=1) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            select id,name,status,node_name,remote_cwd
            from tasks
            where name like ?
            order by id
            """,
            (name_glob,),
        ).fetchall()
    return [
        TaskRow(
            id=int(row["id"]),
            name=str(row["name"] or ""),
            status=str(row["status"] or ""),
            node_name=str(row["node_name"] or ""),
            remote_cwd=str(row["remote_cwd"] or ""),
        )
        for row in rows
    ]


def fetch_missing_results(
    *,
    scheduler_url: str,
    root: Path,
    batch: int,
    missing: Iterable[tuple[int, TaskRow]],
    timeout: float,
) -> list[dict[str, object]]:
    fetched = []
    for case_number, task in missing:
        remote_path = remote_result_path(batch, case_number, task.node_name)
        local_path = local_probe_path(root, batch, case_number, task.node_name)
        text = fetch_task_remote_file(scheduler_url, task.id, remote_path, "remote_cwd", timeout)
        local_path.write_text(text, encoding="utf-8-sig")
        fetched.append(
            {
                "case": case_number,
                "task_id": task.id,
                "node": task.node_name,
                "bytes": len(text),
                "path": str(local_path),
            }
        )
    return fetched


def build_refill_argv(args: argparse.Namespace, case_number: int, node_name: str) -> list[str]:
    tag = f"batch{args.refill_batch}_fea_task_{case_number:03d}_{node_name}_b{args.refill_batch}_{case_number:03d}"
    argv = [
        sys.executable,
        "submit_ipmsm_scheduler_task.py",
        "--scheduler-url",
        args.scheduler_url,
        "--cases",
        str(args.refill_cases),
        "--case-start-index",
        str(case_number),
        "--case-limit",
        "1",
        "--remote-cwd",
        args.remote_cwd,
        "--task-name",
        f"ipmsm-batch{args.refill_batch}-fea-{case_number:03d}-{node_name}_b{args.refill_batch}_{case_number:03d}",
        "--processes",
        "1",
        "--cores-per-process",
        "4",
        "--max-cases",
        str(args.refill_case_limit),
        "--simulation-dir",
        f"simul_log_scheduler/{tag}",
        "--result-csv",
        f"simul_log_scheduler/{tag}_results.csv",
        "--log-dir",
        f"simul_log_scheduler/{tag}",
        "--log-prefix",
        tag,
        "--analyze",
        "--confirm-analyze",
        "--env-setup",
        ANSYS_ELECTRONICS_MODULE,
        "--required-capability",
        args.required_capability,
        "--env-profile",
        args.env_profile,
        "--account-name",
        args.account_name,
        "--node-name",
        node_name,
        "--cpus",
        str(args.cpus),
        "--memory-mb",
        str(args.memory_mb),
        "--scheduling-profile",
        "fea_bursty",
        "--max-workers-per-node",
        str(args.max_workers_per_node),
        "--timeout",
        str(args.timeout),
    ]
    return argv


def submit_refill_cases(args: argparse.Namespace, cases: Iterable[int]) -> list[dict[str, object]]:
    submitted = []
    nodes = tuple(args.nodes.split(","))
    for case_number in cases:
        node_name = node_for_case(case_number, nodes)
        tag = f"batch{args.refill_batch}_fea_task_{case_number:03d}_{node_name}_b{args.refill_batch}_{case_number:03d}"
        base_argv = build_refill_argv(args, case_number, node_name)
        dryrun_manifest = args.root / f"{tag}_dryrun_manifest.json"
        submit_manifest = args.root / f"{tag}_submit_manifest.json"
        subprocess.run(base_argv + ["--write-manifest", str(dryrun_manifest)], check=True, capture_output=True, text=True)
        if args.submit_refill:
            subprocess.run(
                base_argv + ["--write-manifest", str(submit_manifest), "--submit"],
                check=True,
                capture_output=True,
                text=True,
            )
        submitted.append(
            {
                "case": case_number,
                "node": node_name,
                "dryrun_manifest": str(dryrun_manifest),
                "submit_manifest": str(submit_manifest) if args.submit_refill else "",
                "submitted": bool(args.submit_refill),
            }
        )
    return submitted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize IPMSM scheduler replay evidence and refill slots.")
    parser.add_argument("--db", type=Path, required=True, help="Read-only scheduler SQLite DB path.")
    parser.add_argument("--scheduler-url", default="http://127.0.0.1:8000")
    parser.add_argument("--root", type=Path, default=Path("simul_log_smoke"))
    parser.add_argument("--result-batch", type=int, default=3)
    parser.add_argument("--refill-batch", type=int, default=4)
    parser.add_argument("--active-cap", type=int, default=200)
    parser.add_argument("--refill-case-limit", type=int, default=200)
    parser.add_argument("--base-training", type=Path)
    parser.add_argument("--remote-cwd", default="/home1/r1jae262/ipmsm_pyaedt_motor_work")
    parser.add_argument("--refill-cases", type=Path)
    parser.add_argument("--required-capability", default="conda:pyaedt2026v1")
    parser.add_argument("--env-profile", default="pyaedt2026v1")
    parser.add_argument("--account-name", default="r1jae262")
    parser.add_argument("--nodes", default=",".join(DEFAULT_NODES))
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory-mb", type=int, default=32768)
    parser.add_argument("--max-workers-per-node", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--fetch-missing", action="store_true")
    parser.add_argument("--write-partial", action="store_true")
    parser.add_argument("--write-refill-manifests", action="store_true")
    parser.add_argument("--submit-refill", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.write_refill_manifests or args.submit_refill) and args.refill_cases is None:
        raise SystemExit("--refill-cases is required when writing or submitting refill tasks")
    result_tasks = read_tasks_from_db(args.db, f"ipmsm-batch{args.result_batch}-fea-%")
    refill_tasks = read_tasks_from_db(args.db, f"ipmsm-batch{args.refill_batch}-fea-%")
    all_tasks = result_tasks + refill_tasks + read_tasks_from_db(args.db, "ipmsm-batch2-fea-%")
    active = nonterminal_count(all_tasks)
    missing = missing_completed_tasks(result_tasks, local_probe_cases(args.root, args.result_batch))
    refill_cases = planned_refill_cases(max_case_number(refill_tasks), active, args.active_cap, args.refill_case_limit)

    result: dict[str, object] = {
        "active_nonterminal": active,
        "result_batch": args.result_batch,
        "result_status_counts": status_counts(result_tasks),
        "refill_batch": args.refill_batch,
        "refill_status_counts": status_counts(refill_tasks),
        "refill_max_case": max_case_number(refill_tasks),
        "missing_completed_cases": [case for case, _ in missing],
        "planned_refill_cases": refill_cases,
    }
    if args.fetch_missing:
        result["fetched"] = fetch_missing_results(
            scheduler_url=args.scheduler_url,
            root=args.root,
            batch=args.result_batch,
            missing=missing,
            timeout=args.timeout,
        )
    if args.write_partial:
        completed_count = sum(1 for task in result_tasks if task.status == "completed")
        selected_output = args.root / f"batch{args.result_batch}_partial{completed_count:03d}_selected_results.csv"
        result["selected_results"] = build_selected_results(args.root, args.result_batch, selected_output)
        if args.base_training:
            summary = summarize_partial_replay(
                [path for _, path in sorted((case_number_from_probe_name(args.result_batch, path.name) or 0, path) for path in args.root.glob(f"batch{args.result_batch}_fea_task_*_results_probe.csv"))],
                base_training=args.base_training,
            )
            summary_output = args.root / f"batch{args.result_batch}_partial{completed_count:03d}_summary_vs_base.csv"
            write_summary(summary_output, summary)
            result["partial_summary"] = {"output": str(summary_output), **summary}
    if refill_cases and (args.write_refill_manifests or args.submit_refill):
        result["refill"] = submit_refill_cases(args, refill_cases)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
