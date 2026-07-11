"""Create a rate-limited, read-only snapshot of settled IPMSM v2 Stage 1 results."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import errno
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError

import collect_ipmsm_v2_campaign as collector
import run_ipmsm_v2_campaign as runner
import submit_ipmsm_v2_campaign as submitter
import supervise_ipmsm_v2_pipeline as supervisor


DEFAULT_REQUEST_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_FETCHES_PER_WINDOW = 10
DEFAULT_WINDOW_SECONDS = 30.0
DEFAULT_RETRY_LIMIT = 5
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 10.0
MAX_DETERMINISTIC_JITTER_SECONDS = 0.25
SNAPSHOT_MANIFEST_NAME = "snapshot_manifest.json"
SNAPSHOT_MANIFEST_SCHEMA_VERSION = "ipmsm-v2-partial-snapshot-manifest-v1"


@dataclass(frozen=True)
class SettledResult:
    task: submitter.CampaignTask
    history_task: Mapping[str, Any]
    plan_row: dict[str, Any]


@dataclass(frozen=True)
class SnapshotManifestContext:
    contract_source: Path
    contract_sha256: str
    contract_document_sha256: str
    source_case_plan: Path
    source_case_plan_sha256: str
    producer_path: Path
    producer_sha256: str
    complete_designs_available: int
    selected_designs: int
    split_design_counts: dict[str, int]
    diagnostic_scope: str


class FetchRateLimiter:
    """Enforce one-at-a-time pacing and a bounded request window."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        max_requests_per_window: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.max_requests_per_window = max_requests_per_window
        self.window_seconds = window_seconds
        self.clock = clock
        self.sleeper = sleeper
        now = self.clock()
        self._window_started = now
        self._window_requests = 0
        self._next_request_at = now

    def before_request(self, *, jitter_seconds: float = 0.0) -> None:
        now = self.clock()
        elapsed = now - self._window_started
        if elapsed >= self.window_seconds:
            self._window_started = now
            self._window_requests = 0
        elif self._window_requests >= self.max_requests_per_window:
            self.sleeper(max(0.0, self.window_seconds - elapsed))
            now = self.clock()
            self._window_started = now
            self._window_requests = 0

        now = self.clock()
        if now < self._next_request_at:
            self.sleeper(self._next_request_at - now)
        now = self.clock()
        self._window_requests += 1
        self._next_request_at = now + self.interval_seconds + max(0.0, jitter_seconds)

    def backoff(self, seconds: float) -> None:
        self.sleeper(max(0.0, seconds))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--max-designs",
        type=int,
        default=0,
        help="Evenly sample this many complete designs in plan order; 0 selects all.",
    )
    parser.add_argument(
        "--require-exact-designs",
        action="store_true",
        help="Require --max-designs complete designs instead of treating it as an upper bound.",
    )
    parser.add_argument(
        "--base-only",
        action="store_true",
        help="Exclude settled repeat rows from the selected complete designs.",
    )
    parser.add_argument(
        "--require-exact-rows",
        type=int,
        default=0,
        help="Fail before remote fetches unless this many selected rows remain; 0 disables.",
    )
    parser.add_argument(
        "--minimum-diagnostic-scope",
        choices=("provisional_minimum", "provisional_stronger"),
        help="Fail before remote fetches unless the selected split reaches this scope.",
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=DEFAULT_REQUEST_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--max-fetches-per-window",
        type=int,
        default=DEFAULT_MAX_FETCHES_PER_WINDOW,
    )
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--retry-limit", type=int, default=DEFAULT_RETRY_LIMIT)
    parser.add_argument("--backoff-seconds", type=float, default=DEFAULT_BACKOFF_SECONDS)
    parser.add_argument(
        "--max-backoff-seconds",
        type=float,
        default=DEFAULT_MAX_BACKOFF_SECONDS,
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.output_dir.exists():
        raise RuntimeError(f"--output-dir must not already exist: {args.output_dir}")
    if args.max_designs < 0:
        raise RuntimeError("--max-designs must be >= 0")
    if getattr(args, "require_exact_designs", False) and args.max_designs <= 0:
        raise RuntimeError("--require-exact-designs requires --max-designs > 0")
    if getattr(args, "require_exact_rows", 0) < 0:
        raise RuntimeError("--require-exact-rows must be >= 0")
    if not math.isfinite(args.request_interval_seconds) or args.request_interval_seconds < 0.5:
        raise RuntimeError("--request-interval-seconds must be finite and >= 0.5")
    if not 1 <= args.max_fetches_per_window <= 10:
        raise RuntimeError("--max-fetches-per-window must be between 1 and 10")
    if not math.isfinite(args.window_seconds) or args.window_seconds < 30.0:
        raise RuntimeError("--window-seconds must be finite and >= 30")
    if not 0 <= args.retry_limit <= 5:
        raise RuntimeError("--retry-limit must be between 0 and 5")
    if not math.isfinite(args.backoff_seconds) or args.backoff_seconds < 1.0:
        raise RuntimeError("--backoff-seconds must be finite and >= 1")
    if (
        not math.isfinite(args.max_backoff_seconds)
        or args.max_backoff_seconds < max(10.0, args.backoff_seconds)
    ):
        raise RuntimeError("--max-backoff-seconds must be finite and >= max(10, --backoff-seconds)")


def validate_output_dir(output_dir: Path, contract: supervisor.PipelineContract) -> None:
    target = output_dir.resolve(strict=False)
    workdir = contract.workdir.resolve(strict=False)
    if not target.is_relative_to(workdir):
        raise RuntimeError("--output-dir must stay within the pipeline workdir")
    protected_directories = (
        contract.stage1.output_dir,
        contract.stage1.model_dir,
        contract.speed.output_dir,
    )
    for protected in protected_directories:
        resolved = protected.resolve(strict=False)
        if target == resolved or target.is_relative_to(resolved) or resolved.is_relative_to(target):
            raise RuntimeError(f"--output-dir overlaps a pipeline output directory: {protected}")
    protected_files = (
        contract.stage1.result,
        contract.stage1.validation,
        contract.stage1.r2,
        contract.stage2.decision,
        contract.stage3.plan,
        contract.stage3.manifest,
        contract.stage3.decision,
        contract.optimization.decision,
        contract.speed.plan,
        contract.speed.result,
        contract.speed.rank,
        contract.speed.top,
        contract.speed.marker,
    )
    for protected in protected_files:
        resolved = protected.resolve(strict=False)
        if target == resolved or resolved.is_relative_to(target):
            raise RuntimeError(f"--output-dir contains a pipeline output artifact: {protected}")


def _parse_scheduler_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _latest_successful_task(
    task: submitter.CampaignTask,
    matches: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    successful = [
        item
        for item in matches
        if str(item.get("status") or "").strip().lower() == "completed"
        and runner._exit_code(dict(item)) == 0
        and isinstance(runner._task_id(dict(item)), int)
    ]
    if not successful:
        raise RuntimeError(f"successful campaign state has no completed task: {task.case_id}")
    latest_id = max(int(runner._task_id(dict(item))) for item in successful)
    latest = [item for item in successful if runner._task_id(dict(item)) == latest_id]
    if len(latest) != 1:
        raise RuntimeError(f"ambiguous latest completed task: {task.case_id}")
    return latest[0]


def settled_successful_results(
    *,
    state: runner.CampaignState,
    history: Sequence[dict[str, Any]],
    project: str,
    selected_rows: Sequence[dict[str, Any]],
    first_row_number: int,
    settle_seconds: float,
    now: datetime,
) -> list[SettledResult]:
    by_dedupe = runner._history_by_dedupe(history, project)
    settled: list[SettledResult] = []
    for task in state.successful:
        latest = _latest_successful_task(task, by_dedupe.get(task.dedupe_key, []))
        finished = _parse_scheduler_time(latest.get("finished_at"))
        if finished is None or (now - finished).total_seconds() < settle_seconds:
            continue
        index = task.row_number - first_row_number
        if not 0 <= index < len(selected_rows):
            raise RuntimeError(f"invalid plan row number for case_id={task.case_id!r}")
        plan_row = dict(selected_rows[index])
        if str(plan_row.get("case_id") or "").strip() != task.case_id:
            raise RuntimeError(f"plan identity changed for case_id={task.case_id!r}")
        settled.append(SettledResult(task=task, history_task=latest, plan_row=plan_row))
    return settled


def _evenly_spaced(values: Sequence[str], limit: int) -> list[str]:
    if limit <= 0 or limit >= len(values):
        return list(values)
    if limit == 1:
        return [values[len(values) // 2]]
    indices = [round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)]
    return [values[index] for index in indices]


def select_complete_designs(
    *,
    tasks: Sequence[submitter.CampaignTask],
    selected_rows: Sequence[dict[str, Any]],
    settled: Sequence[SettledResult],
    max_designs: int,
) -> tuple[list[SettledResult], list[str], list[str]]:
    if len(tasks) != len(selected_rows):
        raise RuntimeError("campaign tasks and selected plan rows differ in length")
    required_base_rows_by_group: dict[str, set[int]] = defaultdict(set)
    group_order: list[str] = []
    seen_groups: set[str] = set()
    for task, row in zip(tasks, selected_rows, strict=True):
        group = str(row.get("geometry_group_id") or "").strip()
        if not group:
            raise RuntimeError(f"blank geometry_group_id for case_id={task.case_id!r}")
        if group not in seen_groups:
            seen_groups.add(group)
            group_order.append(group)
        if not str(row.get("repeat_of_case_id") or "").strip():
            required_base_rows_by_group[group].add(task.row_number)

    settled_by_group: dict[str, dict[int, SettledResult]] = defaultdict(dict)
    for item in settled:
        group = str(item.plan_row.get("geometry_group_id") or "").strip()
        settled_by_group[group][item.task.row_number] = item
    complete_groups = [
        group
        for group in group_order
        if required_base_rows_by_group[group]
        and required_base_rows_by_group[group] <= set(settled_by_group.get(group, {}))
    ]
    chosen_groups = _evenly_spaced(complete_groups, max_designs)
    chosen_set = set(chosen_groups)
    chosen = sorted(
        (item for item in settled if str(item.plan_row.get("geometry_group_id") or "").strip() in chosen_set),
        key=lambda item: item.task.row_number,
    )
    return chosen, complete_groups, chosen_groups


def split_counts(items: Sequence[SettledResult]) -> dict[str, int]:
    by_group: dict[str, set[str]] = defaultdict(set)
    for item in items:
        group = str(item.plan_row.get("geometry_group_id") or "").strip()
        by_group[group].add(str(item.plan_row.get("doe_split") or "").strip().lower())
    counts: Counter[str] = Counter()
    for group, values in by_group.items():
        if len(values) != 1 or next(iter(values)) not in {"train", "calibration", "test"}:
            raise RuntimeError(f"geometry group has invalid split identity: {group}")
        counts[next(iter(values))] += 1
    return {name: counts.get(name, 0) for name in ("train", "calibration", "test")}


def diagnostic_scope(design_count: int, counts: Mapping[str, int]) -> str:
    if (
        design_count >= 80
        and counts.get("train", 0) >= 40
        and counts.get("calibration", 0) >= 15
        and counts.get("test", 0) >= 15
    ):
        return "provisional_stronger"
    if (
        design_count >= 60
        and counts.get("train", 0) >= 30
        and counts.get("calibration", 0) >= 10
        and counts.get("test", 0) >= 10
    ):
        return "provisional_minimum"
    return "physics_only"


def enforce_selection_gate(
    *,
    selected_designs: int,
    max_designs: int,
    require_exact_designs: bool,
    selected_scope: str,
    minimum_diagnostic_scope: str | None,
    selected_rows: int | None = None,
    required_rows: int = 0,
) -> None:
    """Validate a checkpoint selection before any remote result is fetched."""

    if require_exact_designs and selected_designs != max_designs:
        raise RuntimeError(f"selected designs={selected_designs}, required={max_designs}")
    if required_rows > 0 and selected_rows != required_rows:
        raise RuntimeError(f"selected rows={selected_rows}, required={required_rows}")
    scope_rank = {
        "physics_only": 0,
        "provisional_minimum": 1,
        "provisional_stronger": 2,
    }
    if selected_scope not in scope_rank:
        raise RuntimeError(f"unsupported diagnostic_scope={selected_scope!r}")
    if minimum_diagnostic_scope is not None:
        if minimum_diagnostic_scope not in scope_rank:
            raise RuntimeError(
                f"unsupported minimum diagnostic scope={minimum_diagnostic_scope!r}"
            )
        if scope_rank[selected_scope] < scope_rank[minimum_diagnostic_scope]:
            raise RuntimeError(
                f"diagnostic_scope={selected_scope!r} is below "
                f"{minimum_diagnostic_scope!r}"
            )


def retry_after_seconds(error: HTTPError, *, now_epoch: float | None = None) -> float | None:
    value = error.headers.get("Retry-After") if error.headers is not None else None
    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = parsed.timestamp() - (time.time() if now_epoch is None else now_epoch)
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _deterministic_fraction(task_id: int, request_index: int, attempt: int) -> float:
    return ((task_id * 31 + request_index * 17 + attempt * 13) % 101) / 100.0


def fetch_with_policy(
    fetch: Callable[[], str],
    *,
    task_id: int,
    request_index: int,
    limiter: FetchRateLimiter,
    retry_limit: int,
    backoff_seconds: float,
    max_backoff_seconds: float,
) -> str:
    attempt = 0
    while True:
        fraction = _deterministic_fraction(task_id, request_index, attempt)
        limiter.before_request(jitter_seconds=fraction * MAX_DETERMINISTIC_JITTER_SECONDS)
        try:
            return fetch()
        except HTTPError as exc:
            if exc.code != 429 or attempt >= retry_limit:
                raise
            retry_after = retry_after_seconds(exc)
            if retry_after is None:
                base = min(max_backoff_seconds, backoff_seconds * (2**attempt))
                delay = min(max_backoff_seconds, base * (0.8 + 0.4 * fraction))
            else:
                delay = min(max_backoff_seconds, retry_after)
            limiter.backoff(delay)
            attempt += 1


def fetch_selected_results(
    selected: Sequence[SettledResult],
    *,
    campaign_args: argparse.Namespace,
    limiter: FetchRateLimiter,
    retry_limit: int,
    backoff_seconds: float,
    max_backoff_seconds: float,
    remote_fetch: Callable[..., str] = collector.fetch_task_remote_file,
) -> tuple[list[tuple[submitter.CampaignTask, str]], list[dict[str, str]]]:
    collected: list[tuple[submitter.CampaignTask, str]] = []
    result_rows: list[dict[str, str]] = []
    for request_index, item in enumerate(selected):
        task_id = runner._task_id(dict(item.history_task))
        if not isinstance(task_id, int):
            raise RuntimeError(f"invalid task id for case_id={item.task.case_id!r}")

        def fetch() -> str:
            return remote_fetch(
                campaign_args.scheduler_url,
                task_id,
                item.task.result_csv,
                "remote_cwd",
                campaign_args.timeout,
            )

        text = fetch_with_policy(
            fetch,
            task_id=task_id,
            request_index=request_index,
            limiter=limiter,
            retry_limit=retry_limit,
            backoff_seconds=backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )
        expected_hash = str(item.plan_row.get("design_hash") or "").strip()
        _, result_row = collector._one_remote_result(text, item.task.case_id, expected_hash)
        collector.validate_result_matches_plan(item.plan_row, result_row)
        collected.append((item.task, text))
        result_rows.append(result_row)
    collector.validate_homogeneous_fingerprints(result_rows)
    return collected, result_rows


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish one staged directory without replacing a peer."""

    if os.name == "nt":
        os.rename(source, destination)
        return
    if not sys.platform.startswith("linux"):
        raise OSError("atomic no-replace directory publication is unsupported on this platform")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError("renameat2 is unavailable for atomic directory publication")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), str(destination))
    raise OSError(error_number, os.strerror(error_number), str(destination))


def _plan_manifest_counts(rows: Sequence[Mapping[str, Any]]) -> tuple[int, dict[str, int]]:
    splits_by_group: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        group = str(row.get("geometry_group_id") or "").strip()
        split = str(row.get("doe_split") or "").strip().lower()
        if not group or split not in {"train", "calibration", "test"}:
            raise RuntimeError("snapshot manifest plan has an invalid geometry/split identity")
        splits_by_group[group].add(split)
    if any(len(values) != 1 for values in splits_by_group.values()):
        raise RuntimeError("snapshot manifest geometry crosses split partitions")
    counts: Counter[str] = Counter(next(iter(values)) for values in splits_by_group.values())
    return len(splits_by_group), {
        name: counts.get(name, 0) for name in ("train", "calibration", "test")
    }


def build_snapshot_manifest(
    *,
    context: SnapshotManifestContext,
    plan_path: Path,
    merged_path: Path,
    selected_rows: Sequence[Mapping[str, Any]],
    merged_rows: Sequence[Mapping[str, Any]],
    result_files: Sequence[Path],
) -> dict[str, Any]:
    selected_designs, split_counts = _plan_manifest_counts(selected_rows)
    repeat_rows = sum(
        bool(str(row.get("repeat_of_case_id") or "").strip()) for row in selected_rows
    )
    if selected_designs != context.selected_designs:
        raise RuntimeError("snapshot manifest selected-design count changed")
    if split_counts != context.split_design_counts:
        raise RuntimeError("snapshot manifest split-design counts changed")
    if context.complete_designs_available < selected_designs:
        raise RuntimeError("snapshot manifest complete-design count is below its selection")
    if diagnostic_scope(selected_designs, split_counts) != context.diagnostic_scope:
        raise RuntimeError("snapshot manifest diagnostic scope changed")
    if len(selected_rows) != len(merged_rows) or len(merged_rows) != len(result_files):
        raise RuntimeError("snapshot manifest row/result counts differ")
    return {
        "artifacts": {
            "merged_results": {
                "path": "merged_results.csv",
                "sha256": supervisor._file_sha256(merged_path),
            },
            "selected_plan": {
                "path": collector.SELECTED_PLAN_NAME,
                "sha256": supervisor._file_sha256(plan_path),
            },
        },
        "contract": {
            "canonical_sha256": context.contract_sha256,
            "document_path": str(context.contract_source.resolve(strict=False)),
            "document_sha256": context.contract_document_sha256,
            "source_case_plan_path": str(context.source_case_plan.resolve(strict=False)),
            "source_case_plan_sha256": context.source_case_plan_sha256,
        },
        "counts": {
            "complete_designs_available": context.complete_designs_available,
            "repeat_rows": repeat_rows,
            "result_files": len(result_files),
            "result_rows": len(merged_rows),
            "selected_designs": selected_designs,
            "selected_rows": len(selected_rows),
            "split_design_counts": split_counts,
        },
        "diagnostic_scope": context.diagnostic_scope,
        "official_gate_eligible": False,
        "producer": {
            "path": str(context.producer_path.resolve(strict=False)),
            "sha256": context.producer_sha256,
        },
        "schema_version": SNAPSHOT_MANIFEST_SCHEMA_VERSION,
    }


def _write_snapshot_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def stage_and_commit_snapshot(
    output_dir: Path,
    selected_rows: list[dict[str, Any]],
    collected: list[tuple[submitter.CampaignTask, str]],
    *,
    manifest_context: SnapshotManifestContext,
) -> tuple[Path, Path, list[Path], Path]:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        plan_path = stage_dir / collector.SELECTED_PLAN_NAME
        results_dir = stage_dir / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        collector._write_plan(plan_path, selected_rows)
        staged_results: list[Path] = []
        for task, text in collected:
            result_path = results_dir / f"{task.safe_case_id}.csv"
            result_path.write_text(text.lstrip("\ufeff"), encoding="utf-8")
            staged_results.append(result_path)
        headers, rows = collector.merge_complete_results(plan_path, staged_results)
        merged_path = stage_dir / "merged_results.csv"
        collector.write_csv(merged_path, headers, rows)
        manifest = build_snapshot_manifest(
            context=manifest_context,
            plan_path=plan_path,
            merged_path=merged_path,
            selected_rows=selected_rows,
            merged_rows=rows,
            result_files=staged_results,
        )
        _write_snapshot_manifest(stage_dir / SNAPSHOT_MANIFEST_NAME, manifest)
        _rename_directory_no_replace(stage_dir, output_dir)
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    final_plan = output_dir / collector.SELECTED_PLAN_NAME
    final_merged = output_dir / "merged_results.csv"
    final_results = [output_dir / "results" / f"{task.safe_case_id}.csv" for task, _ in collected]
    final_manifest = output_dir / SNAPSHOT_MANIFEST_NAME
    return final_plan, final_merged, final_results, final_manifest


def _campaign_args(contract: supervisor.PipelineContract) -> argparse.Namespace:
    argv = list(contract.stage1.campaign_argv)
    if len(argv) < 3 or Path(argv[1]).name.lower() != "run_ipmsm_v2_campaign.py":
        raise RuntimeError("Stage1 campaign argv does not use run_ipmsm_v2_campaign.py")
    args = runner.build_parser().parse_args(argv[2:])
    args.cases = contract.stage1.case_plan
    runner.validate_args(args)
    return args


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_args(args)
    contract = supervisor.load_contract(args.contract)
    supervisor.audit_immutable_inputs(contract)
    contract_document_sha256 = supervisor._file_sha256(contract.source)
    source_case_plan_sha256 = supervisor._file_sha256(contract.stage1.case_plan)
    producer_path = Path(__file__).resolve(strict=True)
    producer_sha256 = supervisor._file_sha256(producer_path)
    validate_output_dir(args.output_dir, contract)
    campaign_args = _campaign_args(contract)
    validated_rows = submitter.load_and_validate_cases(
        campaign_args.cases,
        campaign_args.max_plan_cases,
        False,
    )
    selected_rows = submitter.select_case_rows(
        validated_rows,
        campaign_args.case_start_index,
        campaign_args.case_limit,
    )
    tasks = submitter.build_campaign_tasks(
        campaign_args,
        selected_rows,
        first_row_number=campaign_args.case_start_index,
    )
    snapshot = runner.read_scheduler_snapshot(campaign_args)
    state = runner.classify_campaign_state(
        tasks,
        snapshot.history,
        campaign_args.project,
        {},
        campaign_args.terminal_retry_limit,
    )
    settled = settled_successful_results(
        state=state,
        history=snapshot.history,
        project=campaign_args.project,
        selected_rows=selected_rows,
        first_row_number=campaign_args.case_start_index,
        settle_seconds=campaign_args.completed_result_settle_seconds,
        now=datetime.now(timezone.utc),
    )
    chosen, complete_groups, chosen_groups = select_complete_designs(
        tasks=tasks,
        selected_rows=selected_rows,
        settled=settled,
        max_designs=args.max_designs,
    )
    if args.base_only:
        chosen = [
            item
            for item in chosen
            if not str(item.plan_row.get("repeat_of_case_id") or "").strip()
        ]
    if not chosen:
        raise RuntimeError("no complete settled Stage1 designs are available")
    chosen_counts = split_counts(chosen)
    chosen_scope = diagnostic_scope(len(chosen_groups), chosen_counts)
    enforce_selection_gate(
        selected_designs=len(chosen_groups),
        max_designs=args.max_designs,
        require_exact_designs=args.require_exact_designs,
        selected_scope=chosen_scope,
        minimum_diagnostic_scope=args.minimum_diagnostic_scope,
        selected_rows=len(chosen),
        required_rows=args.require_exact_rows,
    )
    limiter = FetchRateLimiter(
        interval_seconds=args.request_interval_seconds,
        max_requests_per_window=args.max_fetches_per_window,
        window_seconds=args.window_seconds,
    )
    collected, _ = fetch_selected_results(
        chosen,
        campaign_args=campaign_args,
        limiter=limiter,
        retry_limit=args.retry_limit,
        backoff_seconds=args.backoff_seconds,
        max_backoff_seconds=args.max_backoff_seconds,
    )
    rebound = supervisor.load_contract(contract.source)
    supervisor.audit_immutable_inputs(rebound)
    if (
        rebound.contract_sha256 != contract.contract_sha256
        or supervisor._file_sha256(rebound.source) != contract_document_sha256
        or supervisor._file_sha256(rebound.stage1.case_plan) != source_case_plan_sha256
        or supervisor._file_sha256(producer_path) != producer_sha256
    ):
        raise RuntimeError("snapshot contract/source plan changed before publication")
    manifest_context = SnapshotManifestContext(
        contract_source=contract.source,
        contract_sha256=contract.contract_sha256,
        contract_document_sha256=contract_document_sha256,
        source_case_plan=contract.stage1.case_plan,
        source_case_plan_sha256=source_case_plan_sha256,
        producer_path=producer_path,
        producer_sha256=producer_sha256,
        complete_designs_available=len(complete_groups),
        selected_designs=len(chosen_groups),
        split_design_counts=chosen_counts,
        diagnostic_scope=chosen_scope,
    )
    plan_path, merged_path, result_paths, manifest_path = stage_and_commit_snapshot(
        args.output_dir,
        [item.plan_row for item in chosen],
        collected,
        manifest_context=manifest_context,
    )
    output = {
        "active": len(state.active),
        "complete_designs_available": len(complete_groups),
        "contract_sha256": contract.contract_sha256,
        "diagnostic_scope": chosen_scope,
        "missing": len(state.missing),
        "official_gate_eligible": False,
        "output_dir": str(args.output_dir),
        "result_rows": len(result_paths),
        "retryable": len(state.retryable),
        "scheduler_successful": len(state.successful),
        "selected_designs": len(chosen_groups),
        "selected_plan": str(plan_path),
        "split_design_counts": chosen_counts,
        "status": "ok",
        "merged_output": str(merged_path),
        "snapshot_manifest": str(manifest_path),
        "snapshot_manifest_sha256": supervisor._file_sha256(manifest_path),
    }
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
