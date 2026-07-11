"""Collect and validate completed one-case IPMSM v2 scheduler results."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any, Iterable

from inspect_ipmsm_scheduler_job import fetch_task_remote_file
from merge_ipmsm_v2_results import merge_complete_results, write_csv
import submit_ipmsm_v2_campaign as submit_campaign


ACTIVE_STATUSES = frozenset({"queued", "attaching", "running"})
SCHEMA_VERSION = "ipmsm_v2"
BETA_CONVENTION = "dq_current_advance_v2"
DEFAULT_SCHEDULER_TIMEOUT_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_WAIT_TIMEOUT_SECONDS = 43_200.0
WAIT_STATUS_PREVIEW = 5
DEFAULT_MERGED_OUTPUT = Path("merged_results.csv")
SELECTED_PLAN_NAME = "selected_cases.csv"
REQUIRED_FINGERPRINT_COLUMNS = (
    "input_quality_profile",
    "input_setup_fingerprint",
    "input_material_fingerprint",
    "input_aedt_version",
)
PLAN_INPUT_COLUMNS = (
    "initial_position_deg",
    "stack_length_mm",
    "phase_resistance_ohm",
    "vdc_v",
    "electrical_zero_deg",
    "beta_dq_deg",
    "base_rpm",
    "i_peak_a",
    "operation",
    "quality_profile",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--scheduler-url", default=submit_campaign.DEFAULT_SCHEDULER_URL)
    parser.add_argument("--task-prefix", default="ipmsm-v2")
    parser.add_argument("--remote-cases-dir", default="remote/ipmsm_v2_campaign_cases")
    parser.add_argument("--result-dir", default="simul_log_scheduler/ipmsm_v2_campaign_results")
    parser.add_argument("--simulation-dir", default="simulation/ipmsm_v2_campaign")
    parser.add_argument("--log-dir", default="simul_log_scheduler/ipmsm_v2_campaign_logs")
    parser.add_argument("--start", "--case-start-index", dest="case_start_index", type=int, default=1)
    parser.add_argument("--limit", "--case-limit", dest="case_limit", type=int, default=0)
    parser.add_argument("--max-plan-cases", type=int, default=submit_campaign.DEFAULT_MAX_PLAN_CASES)
    parser.add_argument("--history-limit", type=int, default=submit_campaign.DEFAULT_HISTORY_LIMIT)
    parser.add_argument("--scheduler-timeout", type=float, default=DEFAULT_SCHEDULER_TIMEOUT_SECONDS)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--wait-timeout-seconds", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--merged-output", type=Path, default=DEFAULT_MERGED_OUTPUT)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not str(args.project or "").strip():
        raise RuntimeError("--project must not be blank")
    if not 1 <= args.history_limit <= submit_campaign.MAX_HISTORY_LIMIT:
        raise RuntimeError(
            f"--history-limit must be between 1 and {submit_campaign.MAX_HISTORY_LIMIT}"
        )
    if not math.isfinite(args.scheduler_timeout) or args.scheduler_timeout <= 0.0:
        raise RuntimeError("--scheduler-timeout must be finite and > 0")
    if not math.isfinite(args.poll_interval_seconds) or args.poll_interval_seconds <= 0.0:
        raise RuntimeError("--poll-interval-seconds must be finite and > 0")
    if not math.isfinite(args.wait_timeout_seconds) or args.wait_timeout_seconds <= 0.0:
        raise RuntimeError("--wait-timeout-seconds must be finite and > 0")
    if args.output_dir.exists():
        raise RuntimeError(f"--output-dir must not already exist: {args.output_dir}")
    if args.merged_output.is_absolute() or ".." in args.merged_output.parts:
        raise RuntimeError("--merged-output must be a relative path within --output-dir")
    if args.merged_output == Path(SELECTED_PLAN_NAME) or (
        args.merged_output.parts and args.merged_output.parts[0] == "results"
    ):
        raise RuntimeError("--merged-output conflicts with reserved collector paths")


def build_identity_args(args: argparse.Namespace) -> argparse.Namespace:
    identity = submit_campaign.build_parser().parse_args(
        [
            "--cases",
            str(args.cases),
            "--project",
            args.project,
            "--task-prefix",
            args.task_prefix,
            "--remote-cases-dir",
            args.remote_cases_dir,
            "--result-dir",
            args.result_dir,
            "--simulation-dir",
            args.simulation_dir,
            "--log-dir",
            args.log_dir,
            "--start",
            str(args.case_start_index),
            "--limit",
            str(args.case_limit),
            "--max-plan-cases",
            str(args.max_plan_cases),
            "--history-limit",
            str(args.history_limit),
            "--timeout",
            str(args.scheduler_timeout),
        ]
    )
    submit_campaign.validate_args(identity)
    return identity


def _task_id(task: dict[str, Any]) -> int | None:
    try:
        value = int(task["id"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _exit_code(task: dict[str, Any]) -> int | None:
    raw = task.get("exit_code", task.get("return_code"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def inspect_history_task_states(
    tasks: Iterable[submit_campaign.CampaignTask],
    history: Iterable[dict[str, Any]],
    project: str,
) -> tuple[list[tuple[submit_campaign.CampaignTask, dict[str, Any]]], list[dict[str, Any]]]:
    by_dedupe: dict[str, list[dict[str, Any]]] = {}
    for history_task in history:
        if not submit_campaign.task_belongs_to_project(history_task, project):
            continue
        dedupe_key = str(history_task.get("dedupe_key") or "").strip()
        if dedupe_key:
            by_dedupe.setdefault(dedupe_key, []).append(history_task)

    resolved: list[tuple[submit_campaign.CampaignTask, dict[str, Any]]] = []
    active_records: list[dict[str, Any]] = []
    for task in tasks:
        matches = by_dedupe.get(task.dedupe_key, [])
        if not matches:
            raise RuntimeError(f"missing scheduler task for case_id={task.case_id!r}")
        unknown = [
            item
            for item in matches
            if str(item.get("status") or "").strip().lower()
            not in ({"completed", "failed", "cancelled"} | ACTIVE_STATUSES)
        ]
        if unknown:
            statuses = sorted(
                {str(item.get("status") or "").strip().lower() or "<blank>" for item in unknown}
            )
            raise RuntimeError(f"ambiguous scheduler status for case_id={task.case_id!r}: {statuses}")
        active = [
            item
            for item in matches
            if str(item.get("status") or "").strip().lower() in ACTIVE_STATUSES
        ]
        if active:
            latest = max(active, key=lambda item: _task_id(item) or -1)
            active_records.append(
                {
                    "case_id": task.case_id,
                    "status": str(latest.get("status") or "").strip().lower(),
                    "task_id": _task_id(latest),
                }
            )
            continue
        successful = [
            item
            for item in matches
            if str(item.get("status") or "").strip().lower() == "completed"
            and _exit_code(item) == 0
            and _task_id(item) is not None
        ]
        if not successful:
            raise RuntimeError(f"no successful completed task for case_id={task.case_id!r}")
        latest_id = max(_task_id(item) or -1 for item in successful)
        latest = [item for item in successful if _task_id(item) == latest_id]
        if len(latest) != 1:
            raise RuntimeError(f"ambiguous latest successful task for case_id={task.case_id!r}")
        resolved.append((task, latest[0]))
    return resolved, active_records


def resolve_successful_history_tasks(
    tasks: Iterable[submit_campaign.CampaignTask],
    history: Iterable[dict[str, Any]],
    project: str,
) -> list[tuple[submit_campaign.CampaignTask, dict[str, Any]]]:
    resolved, active = inspect_history_task_states(tasks, history, project)
    if active:
        statuses = sorted({str(item["status"]) for item in active})
        raise RuntimeError(f"active scheduler task for case_id={active[0]['case_id']!r}: {statuses}")
    return resolved


def _false_like(value: object) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _one_remote_result(text: str, expected_case_id: str, expected_design_hash: str) -> tuple[list[str], dict[str, str]]:
    stream = io.StringIO(text.lstrip("\ufeff"))
    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        raise RuntimeError(f"remote result has no CSV header for case_id={expected_case_id!r}")
    rows = [dict(row) for row in reader]
    if len(rows) != 1:
        raise RuntimeError(
            f"remote result must contain exactly one row for case_id={expected_case_id!r}; rows={len(rows)}"
        )
    row = rows[0]
    failures: list[str] = []
    if str(row.get("case_id") or "").strip() != expected_case_id:
        failures.append(f"case_id={row.get('case_id')!r}")
    if str(row.get("status") or "").strip().lower() != "ok":
        failures.append(f"status={row.get('status')!r}")
    if "missing_required_outputs" not in row or str(row.get("missing_required_outputs") or "").strip():
        failures.append("missing_required_outputs")
    if str(row.get("input_dataset_schema_version") or "").strip() != SCHEMA_VERSION:
        failures.append("input_dataset_schema_version")
    if str(row.get("input_model_extent") or "").strip() != "full_360":
        failures.append("input_model_extent")
    try:
        symmetry = float(row.get("input_symmetry_factor", "nan"))
    except (TypeError, ValueError):
        symmetry = math.nan
    if not math.isclose(symmetry, 1.0, abs_tol=1e-12):
        failures.append("input_symmetry_factor")
    if "input_use_periodic_boundary" not in row or not _false_like(
        row.get("input_use_periodic_boundary")
    ):
        failures.append("input_use_periodic_boundary")
    if str(row.get("input_beta_convention") or "").strip() != BETA_CONVENTION:
        failures.append("input_beta_convention")
    for column in REQUIRED_FINGERPRINT_COLUMNS:
        if not str(row.get(column) or "").strip():
            failures.append(column)
    result_hashes = {
        str(row.get(column) or "").strip()
        for column in ("design_hash", "input_design_hash")
        if str(row.get(column) or "").strip()
    }
    if not expected_design_hash or result_hashes != {expected_design_hash}:
        failures.append("design_hash")
    if failures:
        raise RuntimeError(
            f"remote result contract failed for case_id={expected_case_id!r}: " + ", ".join(failures)
        )
    return list(reader.fieldnames), row


def _equivalent_value(expected: object, actual: object) -> bool:
    expected_text = "" if expected is None else str(expected).strip()
    actual_text = "" if actual is None else str(actual).strip()
    if expected_text.lower() in {"true", "false"} or actual_text.lower() in {"true", "false"}:
        return expected_text.lower() == actual_text.lower()
    try:
        expected_number = float(expected_text)
        actual_number = float(actual_text)
    except (TypeError, ValueError):
        return expected_text == actual_text
    return math.isfinite(expected_number) and math.isfinite(actual_number) and math.isclose(
        expected_number,
        actual_number,
        rel_tol=1e-10,
        abs_tol=1e-12,
    )


def validate_result_matches_plan(plan_row: dict[str, Any], result_row: dict[str, str]) -> None:
    mismatches: list[str] = []
    for column in PLAN_INPUT_COLUMNS:
        expected = plan_row.get(column)
        if expected is None or not str(expected).strip():
            continue
        result_column = f"input_{column}"
        if result_column not in result_row or not _equivalent_value(expected, result_row[result_column]):
            mismatches.append(result_column)
    if mismatches:
        raise RuntimeError(
            f"remote result does not match case plan for case_id={plan_row.get('case_id')!r}: "
            + ", ".join(mismatches)
        )


def validate_homogeneous_fingerprints(rows: Iterable[dict[str, str]]) -> None:
    rows_list = list(rows)
    for column in REQUIRED_FINGERPRINT_COLUMNS:
        values = {str(row.get(column) or "").strip() for row in rows_list}
        if len(values) != 1 or "" in values:
            raise RuntimeError(f"collected results mix or omit {column}: {sorted(values)!r}")


def _write_plan(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for column in row:
            if column not in fieldnames:
                fieldnames.append(column)
    write_csv(path, fieldnames, rows)


def _stage_and_commit(
    output_dir: Path,
    merged_output: Path,
    selected_rows: list[dict[str, Any]],
    collected: list[tuple[submit_campaign.CampaignTask, str]],
) -> tuple[Path, Path, list[Path]]:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        plan_path = stage_dir / SELECTED_PLAN_NAME
        results_dir = stage_dir / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        _write_plan(plan_path, selected_rows)
        result_paths: list[Path] = []
        for task, text in collected:
            result_path = results_dir / f"{task.safe_case_id}.csv"
            result_path.write_text(text.lstrip("\ufeff"), encoding="utf-8")
            result_paths.append(result_path)
        headers, rows = merge_complete_results(plan_path, result_paths)
        staged_merged = stage_dir / merged_output
        write_csv(staged_merged, headers, rows)
        os.replace(stage_dir, output_dir)
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    final_plan = output_dir / SELECTED_PLAN_NAME
    final_merged = output_dir / merged_output
    final_results = [output_dir / "results" / f"{task.safe_case_id}.csv" for task, _ in collected]
    return final_plan, final_merged, final_results


def read_history_snapshot(
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], int, int]:
    try:
        history = submit_campaign.get_scheduler_task_history(
            args.scheduler_url,
            args.scheduler_timeout,
            args.history_limit,
        )
    except Exception as exc:
        raise RuntimeError(f"cannot inspect scheduler task history; no files were written: {exc}") from exc
    try:
        project_summary = submit_campaign.get_scheduler_project_summary(
            args.scheduler_url,
            args.project,
            args.scheduler_timeout,
        )
    except Exception as exc:
        raise RuntimeError(f"cannot verify project history coverage; no files were written: {exc}") from exc
    history_project_tasks = sum(
        1 for task in history if submit_campaign.task_belongs_to_project(task, args.project)
    )
    project_total_count = int(project_summary["total_count"])
    if history_project_tasks != project_total_count:
        raise RuntimeError(
            "scheduler history coverage is incomplete; no files were written: "
            f"history_project_tasks={history_project_tasks} project_total_count={project_total_count} "
            f"history_rows={len(history)} history_limit={args.history_limit}"
        )
    return history, history_project_tasks, project_total_count


def wait_for_successful_tasks(
    args: argparse.Namespace,
    campaign_tasks: list[submit_campaign.CampaignTask],
) -> tuple[
    list[tuple[submit_campaign.CampaignTask, dict[str, Any]]],
    list[dict[str, Any]],
    int,
    int,
]:
    started = time.monotonic()
    previous_signature: tuple[tuple[str, str, int | None], ...] | None = None
    polls = 0
    while True:
        history, history_project_tasks, project_total_count = read_history_snapshot(args)
        resolved, active = inspect_history_task_states(campaign_tasks, history, args.project)
        if not active:
            return resolved, history, history_project_tasks, project_total_count
        if not args.wait:
            statuses = sorted({str(item["status"]) for item in active})
            raise RuntimeError(
                f"active scheduler task for case_id={active[0]['case_id']!r}: {statuses}; "
                "pass --wait to poll"
            )
        elapsed = time.monotonic() - started
        if elapsed >= args.wait_timeout_seconds:
            raise RuntimeError(
                f"wait timeout after {elapsed:.3f}s with {len(active)} active task(s); "
                "no files were written"
            )
        signature = tuple(
            sorted((str(item["case_id"]), str(item["status"]), item["task_id"]) for item in active)
        )
        if signature != previous_signature or polls % 10 == 0:
            preview = signature[:WAIT_STATUS_PREVIEW]
            compact = ",".join(f"{case_id}:{status}" for case_id, status, _ in preview)
            if len(signature) > len(preview):
                compact += f",...(+{len(signature) - len(preview)})"
            print(
                f"wait_ipmsm_v2 active={len(active)} elapsed_s={elapsed:.1f} {compact}",
                file=sys.stderr,
            )
        previous_signature = signature
        polls += 1
        remaining = args.wait_timeout_seconds - elapsed
        time.sleep(min(args.poll_interval_seconds, remaining))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_args(args)
    identity_args = build_identity_args(args)
    validated_rows = submit_campaign.load_and_validate_cases(args.cases, args.max_plan_cases, False)
    selected_rows = submit_campaign.select_case_rows(
        validated_rows,
        args.case_start_index,
        args.case_limit,
    )
    campaign_tasks = submit_campaign.build_campaign_tasks(
        identity_args,
        selected_rows,
        first_row_number=args.case_start_index,
    )

    resolved, history, history_project_tasks, project_total_count = wait_for_successful_tasks(
        args,
        campaign_tasks,
    )
    collected: list[tuple[submit_campaign.CampaignTask, str]] = []
    collected_rows: list[dict[str, str]] = []
    summaries: list[dict[str, Any]] = []
    for task, history_task in resolved:
        task_id = _task_id(history_task)
        assert task_id is not None
        text = fetch_task_remote_file(
            args.scheduler_url,
            task_id,
            task.result_csv,
            "remote_cwd",
            args.scheduler_timeout,
        )
        plan_row = selected_rows[task.row_number - args.case_start_index]
        expected_design_hash = str(plan_row.get("design_hash") or "").strip()
        _, result_row = _one_remote_result(text, task.case_id, expected_design_hash)
        validate_result_matches_plan(plan_row, result_row)
        collected_rows.append(result_row)
        collected.append((task, text))
        summaries.append(
            {
                "case_id": task.case_id,
                "task_id": task_id,
                "task_status": str(history_task.get("status") or "").strip().lower(),
                "remote_result": task.result_csv,
                "local_result": str(args.output_dir / "results" / f"{task.safe_case_id}.csv"),
            }
        )

    validate_homogeneous_fingerprints(collected_rows)
    plan_path, merged_path, result_paths = _stage_and_commit(
        args.output_dir,
        args.merged_output,
        selected_rows,
        collected,
    )
    output = {
        "project": args.project,
        "selected_cases": len(campaign_tasks),
        "successful_tasks": len(resolved),
        "collected_results": len(result_paths),
        "history_rows": len(history),
        "history_project_tasks": history_project_tasks,
        "project_total_count": project_total_count,
        "selected_plan": str(plan_path),
        "merged_output": str(merged_path),
        "output_dir": str(args.output_dir),
        "tasks": summaries,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2)
