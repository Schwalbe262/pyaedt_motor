"""Continuously refill, monitor, and collect an IPMSM v2 scheduler campaign."""

from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Mapping

import calibrate_ipmsm_beta as beta_calibration
import collect_ipmsm_v2_campaign as collector
import submit_ipmsm_v2_campaign as submit_campaign


ACTIVE_STATUSES = frozenset({"queued", "attaching", "running"})
TERMINAL_RETRY_STATUSES = frozenset({"failed", "cancelled"})
KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_RETRY_STATUSES | {"completed"}
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_OVERALL_TIMEOUT_SECONDS = 604_800.0
DEFAULT_TERMINAL_RETRY_LIMIT = 1
DEFAULT_COMPLETED_RESULT_SETTLE_SECONDS = 300.0
STATUS_HEARTBEAT_POLLS = 10
DEFAULT_ALLOWED_QUALITY_PROFILES = ("reference_ultra",)
BETA_PATH_ARGUMENTS = (
    "beta_summary",
    "beta_case_plan",
    "beta_results",
    "beta_calibration_manifest",
)


@dataclass(frozen=True)
class PendingSubmission:
    task_id: int | str | None
    prior_match_count: int


@dataclass(frozen=True)
class ResultLevelFailure:
    case_id: str
    retry_index: int
    task_id: int
    dedupe_key: str
    remote_result: str
    raw_result_text: str
    result_error: str

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": "result_level_terminal",
            "retry_index": self.retry_index,
            "task_id": self.task_id,
            "dedupe_key": self.dedupe_key,
            "scheduler_status": "completed",
            "result_status": "failed",
            "remote_result": self.remote_result,
            "result_error": self.result_error,
            "_raw_result_text": self.raw_result_text,
        }


@dataclass(frozen=True)
class CampaignState:
    successful: tuple[submit_campaign.CampaignTask, ...]
    active: tuple[submit_campaign.CampaignTask, ...]
    missing: tuple[submit_campaign.CampaignTask, ...]
    retryable: tuple[submit_campaign.CampaignTask, ...]
    pending: tuple[submit_campaign.CampaignTask, ...]
    permanently_failed: tuple[dict[str, Any], ...] = ()
    recovered_failures: tuple[dict[str, Any], ...] = ()

    @property
    def candidates(self) -> tuple[submit_campaign.CampaignTask, ...]:
        return tuple(sorted((*self.missing, *self.retryable), key=lambda task: task.row_number))


@dataclass(frozen=True)
class SchedulerSnapshot:
    history: list[dict[str, Any]]
    campaign_history_tasks: int
    project_total_count: int
    server_project_cap: int
    project_active_count: int


def build_parser() -> argparse.ArgumentParser:
    parser = submit_campaign.build_parser()
    parser.description = __doc__
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--overall-timeout-seconds",
        type=float,
        default=DEFAULT_OVERALL_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--terminal-retry-limit",
        type=int,
        default=DEFAULT_TERMINAL_RETRY_LIMIT,
        help="Maximum failed/cancelled attempts that may each be followed by a retry.",
    )
    parser.add_argument(
        "--completed-result-settle-seconds",
        type=float,
        default=DEFAULT_COMPLETED_RESULT_SETTLE_SECONDS,
        help="Wait this long after scheduler completion before trusting an append-only result CSV.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--merged-output",
        type=Path,
        default=collector.DEFAULT_MERGED_OUTPUT,
    )
    parser.add_argument("--beta-summary", type=Path)
    parser.add_argument("--beta-case-plan", type=Path)
    parser.add_argument("--beta-results", type=Path)
    parser.add_argument("--beta-calibration-manifest", type=Path)
    parser.add_argument(
        "--allowed-quality-profile",
        action="append",
        dest="allowed_quality_profiles",
        help=(
            "Exact foundation quality profile to allow; repeat for an audited multi-profile "
            "experiment. Defaults to reference_ultra only."
        ),
    )
    return parser


def _collector_argv(args: argparse.Namespace) -> list[str]:
    return [
        "--cases",
        str(args.cases),
        "--project",
        args.project,
        "--scheduler-url",
        args.scheduler_url,
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
        "--scheduler-timeout",
        str(args.timeout),
        "--terminal-retry-limit",
        str(args.terminal_retry_limit),
        "--output-dir",
        str(args.output_dir),
        "--merged-output",
        str(args.merged_output),
    ]


def validate_args(args: argparse.Namespace) -> None:
    submit_campaign.validate_args(args)
    normalize_allowed_quality_profiles(args.allowed_quality_profiles)
    if not math.isfinite(args.timeout) or args.timeout <= 0.0:
        raise RuntimeError("--timeout must be finite and > 0")
    if not math.isfinite(args.poll_interval_seconds) or args.poll_interval_seconds <= 0.0:
        raise RuntimeError("--poll-interval-seconds must be finite and > 0")
    if not math.isfinite(args.overall_timeout_seconds) or args.overall_timeout_seconds <= 0.0:
        raise RuntimeError("--overall-timeout-seconds must be finite and > 0")
    if args.terminal_retry_limit < 0:
        raise RuntimeError("--terminal-retry-limit must be >= 0")
    if (
        not math.isfinite(args.completed_result_settle_seconds)
        or args.completed_result_settle_seconds < 0
    ):
        raise RuntimeError("--completed-result-settle-seconds must be finite and >= 0")
    if args.write_manifest is not None:
        raise RuntimeError("--write-manifest is not supported by the campaign runner")
    beta_paths = [getattr(args, name) for name in BETA_PATH_ARGUMENTS]
    if args.submit and not all(path is not None for path in beta_paths):
        raise RuntimeError(
            "--submit requires --beta-summary, --beta-case-plan, --beta-results, "
            "and --beta-calibration-manifest"
        )
    if any(path is not None for path in beta_paths) and not all(
        path is not None for path in beta_paths
    ):
        raise RuntimeError(
            "beta prerequisite validation requires --beta-summary, --beta-case-plan, "
            "--beta-results, and --beta-calibration-manifest together"
        )
    collector_args = collector.build_parser().parse_args(_collector_argv(args))
    collector.validate_args(collector_args)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"{label} must be an existing file: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"cannot read {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain one JSON object: {path}")
    return value


def _read_csv_rows(path: Path, label: str) -> list[dict[str, str]]:
    if not path.is_file():
        raise RuntimeError(f"{label} must be an existing file: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = list(reader.fieldnames or ())
            if not fieldnames or any(not str(name or "").strip() for name in fieldnames):
                raise RuntimeError(f"{label} CSV must have a nonblank header: {path}")
            if len(set(fieldnames)) != len(fieldnames):
                raise RuntimeError(f"{label} CSV has duplicate header names: {path}")
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RuntimeError(f"cannot read {label} CSV {path}: {exc}") from exc
    if any(None in row for row in rows):
        raise RuntimeError(f"{label} CSV has fields beyond its header: {path}")
    return rows


def load_beta_prerequisite(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.beta_summary is None:
        return None
    summary = _read_json_object(args.beta_summary, "beta summary")
    case_plan_rows = _read_csv_rows(args.beta_case_plan, "beta case plan")
    result_rows = _read_csv_rows(args.beta_results, "beta results")
    manifest = _read_json_object(args.beta_calibration_manifest, "beta calibration manifest")
    try:
        return beta_calibration.validate_beta_sweep_summary(
            summary,
            case_plan_rows=case_plan_rows,
            result_rows=result_rows,
            calibration_manifest=manifest,
            require_stage_pass=True,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"strict beta prerequisite failed: {exc}") from exc


def _foundation_text(row: Mapping[str, Any], field: str, case_id: str) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise RuntimeError(f"foundation case {case_id!r} has blank {field}")
    return value


def _foundation_float(row: Mapping[str, Any], field: str, case_id: str) -> float:
    try:
        value = float(row.get(field))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"foundation case {case_id!r} has invalid {field}") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"foundation case {case_id!r} has non-finite {field}")
    return value


def normalize_allowed_quality_profiles(values: Iterable[object] | None) -> tuple[str, ...]:
    raw_values = DEFAULT_ALLOWED_QUALITY_PROFILES if values is None else tuple(values)
    normalized = tuple(str(value or "").strip() for value in raw_values)
    if any(not value for value in normalized):
        raise RuntimeError("--allowed-quality-profile values must not be blank")
    if len(set(normalized)) != len(normalized):
        raise RuntimeError("--allowed-quality-profile values must not contain duplicates")
    return normalized


def validate_foundation_rows(
    rows: Iterable[Mapping[str, Any]],
    summary: Mapping[str, Any],
    allowed_quality_profiles: Iterable[object] | None = None,
) -> None:
    allowed_profiles = normalize_allowed_quality_profiles(allowed_quality_profiles)
    calibration_id = str(summary["beta_calibration_id"])
    electrical_zero_deg = float(summary["electrical_zero_deg"])
    stage_lower, stage_upper = (float(value) for value in summary["stage_beta_bounds_deg"])
    summary_quality = str(summary["homogeneous_identities"]["quality_profile"])
    if summary_quality != "reference_ultra":
        raise RuntimeError(
            "strict beta prerequisite must use homogeneous quality_profile='reference_ultra'"
        )
    for index, row in enumerate(rows, start=1):
        case_id = str(row.get("case_id") or f"row-{index}").strip()
        required_text = {
            "dataset_schema_version": "ipmsm_v2",
            "model_extent": "full_360",
            "beta_convention": "dq_current_advance_v2",
            "beta_calibration_id": calibration_id,
        }
        for field, expected in required_text.items():
            actual = _foundation_text(row, field, case_id)
            if actual != expected:
                raise RuntimeError(
                    f"foundation case {case_id!r} {field} mismatch: "
                    f"expected={expected!r} actual={actual!r}"
                )
        quality_profile = _foundation_text(row, "quality_profile", case_id)
        if quality_profile not in allowed_profiles:
            raise RuntimeError(
                f"foundation case {case_id!r} quality_profile mismatch: "
                f"allowed={list(allowed_profiles)!r} actual={quality_profile!r}"
            )
        symmetry = _foundation_float(row, "symmetry_factor", case_id)
        if not math.isclose(symmetry, 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise RuntimeError(f"foundation case {case_id!r} must use symmetry_factor=1")
        periodic = str(row.get("use_periodic_boundary")).strip().lower()
        if periodic not in {"0", "false", "no", "off"}:
            raise RuntimeError(
                f"foundation case {case_id!r} must set use_periodic_boundary=false"
            )
        row_zero = _foundation_float(row, "electrical_zero_deg", case_id)
        if not math.isclose(
            row_zero,
            electrical_zero_deg,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RuntimeError(
                f"foundation case {case_id!r} electrical_zero_deg mismatch: "
                f"expected={electrical_zero_deg:g} actual={row_zero:g}"
            )
        beta = _foundation_float(row, "beta_dq_deg", case_id)
        if not stage_lower <= beta <= stage_upper:
            raise RuntimeError(
                f"foundation case {case_id!r} beta_dq_deg={beta:g} is outside "
                f"stage bounds [{stage_lower:g}, {stage_upper:g}]"
            )
        operation = _foundation_text(row, "operation", case_id).lower().replace("-", "_")
        if operation not in {"sin_current", "sincurrent"}:
            raise RuntimeError(
                f"foundation case {case_id!r} must use loaded sin_current operation"
            )


def _beta_gate_output(summary: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "best_beta_dq_deg": float(summary["best_beta_dq_deg"]),
        "sweep_id": str(summary["sweep_id"]),
    }


def _task_id(task: dict[str, Any]) -> int | str | None:
    value = task.get("id", task.get("task_id"))
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _exit_code(task: dict[str, Any]) -> int | None:
    raw = task.get("exit_code", task.get("return_code"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _history_by_dedupe(
    history: Iterable[dict[str, Any]],
    project: str,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for history_task in history:
        if not submit_campaign.task_belongs_to_project(history_task, project):
            continue
        dedupe_key = str(history_task.get("dedupe_key") or "").strip()
        if dedupe_key:
            result.setdefault(dedupe_key, []).append(history_task)
    return result


def reconcile_pending_submissions(
    pending: dict[str, PendingSubmission],
    history_by_dedupe: dict[str, list[dict[str, Any]]],
) -> None:
    observed: list[str] = []
    for dedupe_key, submission in pending.items():
        matches = history_by_dedupe.get(dedupe_key, [])
        task_ids = {_task_id(item) for item in matches}
        if (
            submission.task_id is not None
            and submission.task_id in task_ids
        ) or len(matches) > submission.prior_match_count:
            observed.append(dedupe_key)
    for dedupe_key in observed:
        pending.pop(dedupe_key, None)


def classify_campaign_state(
    tasks: Iterable[submit_campaign.CampaignTask],
    history: Iterable[dict[str, Any]],
    project: str,
    pending: dict[str, PendingSubmission],
    terminal_retry_limit: int,
    *,
    lineages: Mapping[str, tuple[submit_campaign.CampaignTask, ...]] | None = None,
    result_failures: Mapping[str, ResultLevelFailure] | None = None,
) -> CampaignState:
    by_dedupe = _history_by_dedupe(history, project)
    reconcile_pending_submissions(pending, by_dedupe)
    result_failures = result_failures or {}
    successful: list[submit_campaign.CampaignTask] = []
    active: list[submit_campaign.CampaignTask] = []
    missing: list[submit_campaign.CampaignTask] = []
    retryable: list[submit_campaign.CampaignTask] = []
    locally_pending: list[submit_campaign.CampaignTask] = []
    permanently_failed: list[dict[str, Any]] = []
    recovered_failures: list[dict[str, Any]] = []

    for base_task in tasks:
        attempts = (
            lineages.get(base_task.dedupe_key, (base_task,))
            if lineages is not None
            else (base_task,)
        )
        matches = [
            (attempt, item)
            for attempt in attempts
            for item in by_dedupe.get(attempt.dedupe_key, [])
        ]
        statuses = {
            str(item.get("status") or "").strip().lower() or "<blank>"
            for _, item in matches
        }
        unknown = sorted(statuses - KNOWN_STATUSES)
        if unknown:
            raise RuntimeError(
                f"ambiguous scheduler history status for case_id={base_task.case_id!r}: {unknown}"
            )
        completed = [
            (attempt, item)
            for attempt, item in matches
            if str(item.get("status") or "").strip().lower() == "completed"
        ]
        completed_success_all = [
            (attempt, item)
            for attempt, item in completed
            if _exit_code(item) == 0 and _task_id(item) is not None
        ]
        if completed and not completed_success_all:
            raise RuntimeError(
                f"completed scheduler task is not a valid success for case_id={base_task.case_id!r}"
            )
        completed_success: list[tuple[submit_campaign.CampaignTask, dict[str, Any]]] = []
        for attempt in attempts:
            lineage_successes = [
                (candidate, item)
                for candidate, item in completed_success_all
                if candidate.dedupe_key == attempt.dedupe_key
            ]
            if lineage_successes:
                completed_success.append(
                    max(lineage_successes, key=lambda pair: _task_id(pair[1]) or -1)
                )
        result_terminal = [
            (attempt, item, failure)
            for attempt, item in completed_success
            for failure in (result_failures.get(attempt.dedupe_key),)
            if failure is not None and failure.task_id == _task_id(item)
        ]
        result_terminal_ids = {_task_id(item) for _, item, _ in result_terminal}
        usable_completed = [
            (attempt, item)
            for attempt, item in completed_success
            if _task_id(item) not in result_terminal_ids
        ]
        scheduler_terminal = [
            (attempt, item)
            for attempt, item in matches
            if str(item.get("status") or "").strip().lower() in TERMINAL_RETRY_STATUSES
        ]
        failure_evidence = [
            {
                "kind": "scheduler_terminal",
                "retry_index": attempt.retry_index,
                "task_id": _task_id(item),
                "dedupe_key": attempt.dedupe_key,
                "scheduler_status": str(item.get("status") or "").strip().lower(),
                "result_status": None,
                "remote_result": attempt.result_csv,
            }
            for attempt, item in scheduler_terminal
        ]
        failure_evidence.extend(failure.evidence() for _, _, failure in result_terminal)
        failure_evidence.sort(
            key=lambda item: (
                int(item["task_id"]) if isinstance(item.get("task_id"), int) else -1,
                int(item["retry_index"]),
            )
        )
        retry_count = len(failure_evidence)

        active_matches = [
            (attempt, item)
            for attempt, item in matches
            if str(item.get("status") or "").strip().lower() in ACTIVE_STATUSES
        ]
        if active_matches:
            if retry_count > terminal_retry_limit:
                raise RuntimeError(
                    "active scheduler attempt exists after terminal retry exhaustion for "
                    f"case_id={base_task.case_id!r}"
                )
            current, _ = max(active_matches, key=lambda pair: _task_id(pair[1]) or -1)
            active.append(current)
            continue
        if usable_completed:
            current, _ = max(usable_completed, key=lambda pair: _task_id(pair[1]) or -1)
            successful.append(current)
            if failure_evidence:
                recovered_failures.append(
                    {
                        "case_id": base_task.case_id,
                        "failure_evidence": failure_evidence,
                    }
                )
            continue
        if retry_count > terminal_retry_limit:
            permanently_failed.append(
                {
                    "case_id": base_task.case_id,
                    "attempts": retry_count,
                    "failure_evidence": failure_evidence,
                }
            )
            continue

        candidate = attempts[retry_count] if retry_count < len(attempts) else base_task
        if candidate.dedupe_key in pending:
            locally_pending.append(candidate)
        elif retry_count:
            retryable.append(candidate)
        else:
            missing.append(candidate)

    return CampaignState(
        successful=tuple(successful),
        active=tuple(active),
        missing=tuple(missing),
        retryable=tuple(retryable),
        pending=tuple(locally_pending),
        permanently_failed=tuple(permanently_failed),
        recovered_failures=tuple(recovered_failures),
    )


def audit_completed_result_rows(
    args: argparse.Namespace,
    completed_tasks: Iterable[submit_campaign.CampaignTask],
    selected_rows: list[dict[str, Any]],
    history: Iterable[dict[str, Any]],
    validated_task_ids: dict[str, int],
    result_failures: dict[str, ResultLevelFailure] | None = None,
) -> tuple[str, ...]:
    """Validate each newly completed scheduler task's one-row result immediately.

    A scheduler exit code of zero only proves that the wrapper ran.  Maxwell can
    still return ``analysis=False`` and write a structured failed row, so such a
    task must never be counted as usable campaign progress.
    """

    history_by_dedupe = _history_by_dedupe(history, args.project)
    if result_failures is None:
        result_failures = {}
    pending: list[str] = []
    for task in completed_tasks:
        successful = [
            item
            for item in history_by_dedupe.get(task.dedupe_key, [])
            if str(item.get("status") or "").strip().lower() == "completed"
            and _exit_code(item) == 0
            and isinstance(_task_id(item), int)
        ]
        if not successful:
            continue
        latest_id = max(int(_task_id(item)) for item in successful)
        latest = [item for item in successful if _task_id(item) == latest_id]
        if len(latest) != 1:
            raise RuntimeError(
                f"completed result audit found an ambiguous latest task for case_id={task.case_id!r}"
            )
        known_failure = result_failures.get(task.dedupe_key)
        if validated_task_ids.get(task.dedupe_key) == latest_id or (
            known_failure is not None and known_failure.task_id == latest_id
        ):
            continue
        finished_raw = str(latest[0].get("finished_at") or "").strip()
        if not finished_raw:
            pending.append(f"{task.case_id}:settling:missing_finished_at")
            continue
        try:
            finished_at = datetime.fromisoformat(finished_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError(
                f"completed result audit has invalid finished_at for case_id={task.case_id!r}: "
                f"{finished_raw!r}"
            ) from exc
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - finished_at.astimezone(timezone.utc)).total_seconds()
        if age_seconds < args.completed_result_settle_seconds:
            pending.append(
                f"{task.case_id}:settling:{max(0.0, args.completed_result_settle_seconds - age_seconds):.1f}s"
            )
            continue
        plan_index = task.row_number - args.case_start_index
        if not 0 <= plan_index < len(selected_rows):
            raise RuntimeError(
                f"completed result audit plan index is invalid for case_id={task.case_id!r}"
            )
        plan_row = selected_rows[plan_index]
        if str(plan_row.get("case_id") or "").strip() != task.case_id:
            raise RuntimeError(
                f"completed result audit plan identity changed for case_id={task.case_id!r}"
            )
        try:
            text = collector.fetch_task_remote_file(
                args.scheduler_url,
                latest_id,
                task.result_csv,
                "remote_cwd",
                args.timeout,
            )
        except Exception as exc:
            message = str(exc).replace("\r", " ").replace("\n", " ")[:160]
            pending.append(f"{task.case_id}:{type(exc).__name__}:{message}")
            continue
        expected_design_hash = str(plan_row.get("design_hash") or "").strip()
        _, result_row = collector._one_remote_result(
            text,
            task.case_id,
            expected_design_hash,
            allow_failed=True,
        )
        collector.validate_result_matches_plan(plan_row, result_row)
        result_status = str(result_row.get("status") or "").strip().lower()
        if result_status == "failed":
            validated_task_ids.pop(task.dedupe_key, None)
            result_failures[task.dedupe_key] = ResultLevelFailure(
                case_id=task.case_id,
                retry_index=task.retry_index,
                task_id=latest_id,
                dedupe_key=task.dedupe_key,
                remote_result=task.result_csv,
                raw_result_text=text,
                result_error=str(result_row.get("error") or "").strip()[:500],
            )
        else:
            result_failures.pop(task.dedupe_key, None)
            validated_task_ids[task.dedupe_key] = latest_id
    return tuple(pending)


def read_scheduler_snapshot(args: argparse.Namespace) -> SchedulerSnapshot:
    try:
        history = submit_campaign.get_scheduler_task_history(
            args.scheduler_url,
            args.timeout,
            args.history_limit,
            args.project,
            args.task_prefix,
        )
    except Exception as exc:
        raise RuntimeError(
            f"cannot inspect scheduler history; no POST was attempted in this polling loop: {exc}"
        ) from exc
    try:
        project_summary = submit_campaign.get_scheduler_project_summary(
            args.scheduler_url,
            args.project,
            args.timeout,
        )
    except Exception as exc:
        raise RuntimeError(
            "cannot verify scheduler project cap; "
            f"no POST was attempted in this polling loop: {exc}"
        ) from exc
    server_project_cap = submit_campaign.require_scheduler_project_cap(
        project_summary,
        args.project_active_cap,
    )
    unexpected_history = [
        task
        for task in history
        if not submit_campaign.task_belongs_to_project(task, args.project)
        or not str(task.get("name") or "").startswith(args.task_prefix)
    ]
    if unexpected_history:
        first = unexpected_history[0]
        raise RuntimeError(
            "scheduler campaign history returned a row outside the exact project/name_prefix "
            "filters; no POST was attempted in this polling loop: "
            f"unexpected_rows={len(unexpected_history)} first_task_id={first.get('id')!r} "
            f"first_project={first.get('project')!r} first_name={first.get('name')!r}"
        )
    campaign_history_tasks = len(history)
    project_total_count = int(project_summary["total_count"])
    if campaign_history_tasks >= args.history_limit:
        raise RuntimeError(
            "saturated scheduler campaign history cannot prove complete bounded coverage; "
            "no POST was attempted in this polling loop: "
            f"task_prefix={args.task_prefix!r} campaign_history_tasks={campaign_history_tasks} "
            f"history_limit={args.history_limit} project_total_count={project_total_count}"
        )
    try:
        active_tasks = submit_campaign.get_scheduler_tasks(args.scheduler_url, args.timeout)
    except Exception as exc:
        raise RuntimeError(
            f"cannot enforce project active cap; no POST was attempted in this polling loop: {exc}"
        ) from exc
    endpoint_active_count = submit_campaign.project_active_task_count(active_tasks, args.project)
    history_active_count = sum(
        1
        for task in history
        if submit_campaign.task_belongs_to_project(task, args.project)
        and str(task.get("status") or "").strip().lower() in ACTIVE_STATUSES
    )
    project_active_count = max(endpoint_active_count, history_active_count)
    if project_active_count > server_project_cap:
        raise RuntimeError(
            "scheduler project active count exceeds its configured cap; "
            "no POST was attempted in this polling loop: "
            f"active={project_active_count} cap={server_project_cap}"
        )
    return SchedulerSnapshot(
        history=history,
        campaign_history_tasks=campaign_history_tasks,
        project_total_count=project_total_count,
        server_project_cap=server_project_cap,
        project_active_count=project_active_count,
    )


def _status_signature(
    state: CampaignState,
    snapshot: SchedulerSnapshot,
    submitted: int,
    validated_results: int,
) -> tuple[int, ...]:
    return (
        len(state.successful),
        validated_results,
        len(state.active),
        len(state.pending),
        len(state.missing),
        len(state.retryable),
        len(state.permanently_failed),
        snapshot.project_active_count,
        submitted,
    )


def _emit_status(
    state: CampaignState,
    snapshot: SchedulerSnapshot,
    submitted: int,
    elapsed: float,
    validated_results: int,
) -> None:
    print(
        "run_ipmsm_v2 "
        f"scheduler_ok={len(state.successful)} result_ok={validated_results} "
        f"active={len(state.active)} "
        f"pending={len(state.pending)} missing={len(state.missing)} "
        f"retry={len(state.retryable)} permanent={len(state.permanently_failed)} "
        f"project_active={snapshot.project_active_count} "
        f"submitted={submitted} elapsed_s={elapsed:.1f}",
        file=sys.stderr,
    )


def _compact_collector_result(result: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return {
        "status": str(result.get("status") or "complete"),
        "collected_results": int(result.get("collected_results", 0)),
        "merged_output": str(result.get("merged_output") or (args.output_dir / args.merged_output)),
        "output_dir": str(result.get("output_dir") or args.output_dir),
        "summary": str(
            result.get("summary") or (args.output_dir / collector.CAMPAIGN_SUMMARY_NAME)
        ),
        "decision": str(
            result.get("decision") or (args.output_dir / collector.CAMPAIGN_DECISION_NAME)
        ),
        "permanently_failed_cases": int(result.get("permanently_failed_cases", 0)),
        "permanent_failures": list(result.get("permanent_failures") or []),
    }


def collect_completed_campaign(args: argparse.Namespace) -> dict[str, Any]:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        result_code = collector.main(_collector_argv(args))
    if result_code != 0:
        raise RuntimeError(f"collector returned nonzero status: {result_code}")
    try:
        result = json.loads(captured.getvalue())
    except json.JSONDecodeError as exc:
        raise RuntimeError("collector did not return valid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("collector did not return a JSON object")
    return _compact_collector_result(result, args)


def collect_terminal_campaign(
    args: argparse.Namespace,
    selected_rows: list[dict[str, Any]],
    state: CampaignState,
    snapshot: SchedulerSnapshot,
    validated_task_ids: Mapping[str, int],
) -> dict[str, Any]:
    history_by_dedupe = _history_by_dedupe(snapshot.history, args.project)
    resolved: list[tuple[submit_campaign.CampaignTask, dict[str, Any]]] = []
    for task in state.successful:
        task_id = validated_task_ids.get(task.dedupe_key)
        matches = [
            item
            for item in history_by_dedupe.get(task.dedupe_key, [])
            if _task_id(item) == task_id
        ]
        if task_id is None or len(matches) != 1:
            raise RuntimeError(
                f"cannot resolve validated result task for case_id={task.case_id!r}"
            )
        resolved.append((task, matches[0]))
    collector_args = collector.build_parser().parse_args(_collector_argv(args))
    collector.validate_args(collector_args)
    result = collector.collect_resolved_campaign(
        collector_args,
        selected_rows,
        resolved,
        history_rows=len(snapshot.history),
        campaign_history_tasks=snapshot.campaign_history_tasks,
        permanent_failures=[dict(item) for item in state.permanently_failed],
        successful_prior_evidence={
            str(item["case_id"]): [dict(evidence) for evidence in item["failure_evidence"]]
            for item in state.recovered_failures
        },
    )
    return _compact_collector_result(result, args)


def _dry_run_output(
    args: argparse.Namespace,
    tasks: list[submit_campaign.CampaignTask],
    state: CampaignState,
    snapshot: SchedulerSnapshot,
    beta_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    effective_active = snapshot.project_active_count + len(state.pending)
    open_slots = max(0, snapshot.server_project_cap - effective_active)
    planned = state.candidates[:open_slots]
    return {
        "active_cases": len(state.active) + len(state.pending),
        "beta_gate": _beta_gate_output(beta_summary),
        "missing_cases": len(state.missing),
        "mode": "dry-run",
        "open_slots": open_slots,
        "planned_submissions": len(planned),
        "project": args.project,
        "project_active": snapshot.project_active_count,
        "permanently_failed_cases": len(state.permanently_failed),
        "retryable_cases": len(state.retryable),
        "selected_cases": len(tasks),
        "successful_cases": len(state.successful),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_args(args)
    beta_summary = load_beta_prerequisite(args)
    validated_rows = submit_campaign.load_and_validate_cases(args.cases, args.max_plan_cases, False)
    selected_rows = submit_campaign.select_case_rows(
        validated_rows,
        args.case_start_index,
        args.case_limit,
    )
    if beta_summary is not None:
        validate_foundation_rows(
            selected_rows,
            beta_summary,
            normalize_allowed_quality_profiles(args.allowed_quality_profiles),
        )
    tasks = submit_campaign.build_campaign_tasks(
        args,
        selected_rows,
        first_row_number=args.case_start_index,
    )
    lineages = submit_campaign.build_campaign_task_lineages(
        args,
        selected_rows,
        first_row_number=args.case_start_index,
        terminal_retry_limit=args.terminal_retry_limit,
    )
    attempt_tasks = [
        attempt
        for task in tasks
        for attempt in lineages[task.dedupe_key]
    ]
    pending: dict[str, PendingSubmission] = {}
    validated_task_ids: dict[str, int] = {}
    result_failures: dict[str, ResultLevelFailure] = {}
    submitted = 0

    if not args.submit:
        snapshot = read_scheduler_snapshot(args)
        state = classify_campaign_state(
            tasks,
            snapshot.history,
            args.project,
            pending,
            args.terminal_retry_limit,
            lineages=lineages,
            result_failures=result_failures,
        )
        print(
            json.dumps(
                _dry_run_output(args, tasks, state, snapshot, beta_summary),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0

    started = time.monotonic()
    previous_signature: tuple[int, ...] | None = None
    polls = 0
    while True:
        snapshot = read_scheduler_snapshot(args)
        history_by_dedupe = _history_by_dedupe(snapshot.history, args.project)
        result_audit_pending = audit_completed_result_rows(
            args,
            attempt_tasks,
            selected_rows,
            snapshot.history,
            validated_task_ids,
            result_failures,
        )
        state = classify_campaign_state(
            tasks,
            snapshot.history,
            args.project,
            pending,
            args.terminal_retry_limit,
            lineages=lineages,
            result_failures=result_failures,
        )
        validated_successes = {
            task.dedupe_key
            for task in state.successful
            if task.dedupe_key in validated_task_ids
        }
        if not result_audit_pending and len(validated_successes) != len(state.successful):
            raise RuntimeError(
                "completed result audit coverage mismatch: "
                f"scheduler_completed={len(state.successful)} "
                f"result_validated={len(validated_successes)}"
            )
        if result_audit_pending:
            elapsed = time.monotonic() - started
            if elapsed >= args.overall_timeout_seconds:
                raise RuntimeError(
                    f"campaign timeout while waiting for {len(result_audit_pending)} completed result(s); "
                    "no output files were written"
                )
            preview = ",".join(result_audit_pending[:3])
            if len(result_audit_pending) > 3:
                preview += f",...(+{len(result_audit_pending) - 3})"
            print(
                f"wait_ipmsm_v2_result_audit pending={len(result_audit_pending)} {preview}",
                file=sys.stderr,
            )
        terminal_cases = len(state.successful) + len(state.permanently_failed)
        if terminal_cases == len(tasks) and not result_audit_pending:
            if (
                state.permanently_failed
                or state.recovered_failures
                or any(task.retry_index > 0 for task in state.successful)
            ):
                collected = collect_terminal_campaign(
                    args,
                    selected_rows,
                    state,
                    snapshot,
                    validated_task_ids,
                )
            else:
                collected = collect_completed_campaign(args)
            output = {
                **collected,
                "beta_gate": _beta_gate_output(beta_summary),
                "mode": "submit",
                "project": args.project,
                "selected_cases": len(tasks),
                "submitted": submitted,
                "successful_cases": len(state.successful),
                "permanently_failed_cases": len(state.permanently_failed),
            }
            print(
                json.dumps(
                    output,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0

        elapsed = time.monotonic() - started
        if elapsed >= args.overall_timeout_seconds:
            raise RuntimeError(
                f"campaign timeout after {elapsed:.3f}s: "
                f"successful={len(state.successful)} permanent={len(state.permanently_failed)} "
                f"selected={len(tasks)}; "
                "no output files were written"
            )

        effective_active = snapshot.project_active_count + len(state.pending)
        open_slots = max(0, snapshot.server_project_cap - effective_active)
        candidates = state.candidates[:open_slots]
        for task in candidates:
            elapsed = time.monotonic() - started
            if elapsed >= args.overall_timeout_seconds:
                raise RuntimeError(
                    f"campaign timeout after {elapsed:.3f}s and {submitted} submission(s); "
                    "no output files were written"
                )
            prior_matches = history_by_dedupe.get(task.dedupe_key, [])
            prior_ids = {_task_id(item) for item in prior_matches}
            try:
                response = submit_campaign.post_scheduler_task(
                    args.scheduler_url,
                    task.payload,
                    args.timeout,
                    "/api/tasks",
                )
            except Exception as exc:
                raise RuntimeError(
                    f"scheduler POST failed after {submitted} successful submission(s): {exc}"
                ) from exc
            response_id = _task_id(response)
            if response_id is not None and response_id in prior_ids:
                raise RuntimeError(
                    f"scheduler POST returned an existing task id for case_id={task.case_id!r}: "
                    f"task_id={response_id}"
                )
            pending[task.dedupe_key] = PendingSubmission(
                task_id=response_id,
                prior_match_count=len(prior_matches),
            )
            submitted += 1

        if candidates:
            elapsed = time.monotonic() - started
            if elapsed >= args.overall_timeout_seconds:
                raise RuntimeError(
                    f"campaign timeout after {elapsed:.3f}s and {submitted} submission(s); "
                    "no output files were written"
                )

        signature = _status_signature(
            state,
            snapshot,
            submitted,
            len(validated_task_ids),
        )
        if signature != previous_signature or polls % STATUS_HEARTBEAT_POLLS == 0:
            _emit_status(
                state,
                snapshot,
                submitted,
                elapsed,
                len(validated_task_ids),
            )
        previous_signature = signature
        polls += 1
        remaining = args.overall_timeout_seconds - elapsed
        time.sleep(min(args.poll_interval_seconds, remaining))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
