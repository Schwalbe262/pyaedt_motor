"""Reconcile, recover, and collect only the sealed Stage3 v4r9 campaign."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping, Sequence

import build_ipmsm_v2_stage3_acquisition_v4r9 as contract_builder
import build_ipmsm_v2_stage3_activation_v4r6 as activation_builder
import collect_ipmsm_v2_campaign as collector
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import replace_ipmsm_v2_failed_geometry as replacement
import run_ipmsm_v2_campaign as campaign
import submit_ipmsm_v2_campaign as submit
import supervise_ipmsm_v2_pipeline as supervisor


RUN_REPORT_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r9-run-v1"
COMPLETION_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r9-completion-v1"


class Stage3RecoveryError(RuntimeError):
    """The sealed recovery/collection action is not safe."""


RESULT_FETCH_MIN_INTERVAL_SECONDS = 1.05
RESULT_FETCH_RATE_LIMIT_RETRIES = 5


@dataclass(frozen=True)
class RecoveryContext:
    path: Path
    snapshot: authority.FileSnapshot
    contract_sha256: str
    document: Mapping[str, Any]
    root: Path
    source_root: Path
    repository_revision: str
    sources: Mapping[str, Mapping[str, str]]
    prior: Mapping[str, Any]
    campaign_argv: tuple[str, ...]
    runner_dry_argv: tuple[str, ...]
    runner_execute_argv: tuple[str, ...]
    project: str
    scheduler_url: str
    task_prefix: str
    project_active_cap: int
    history_limit: int
    scheduler_timeout_seconds: float
    result_retry_limit: int
    shared_lock: Path
    plan: Path
    plan_sha256: str
    replacement: Mapping[str, Any]
    outputs: Mapping[str, Path]
    authority_snapshots: tuple[authority.FileSnapshot, ...]


@dataclass(frozen=True)
class Reconciliation:
    kind: str
    args: argparse.Namespace
    rows: tuple[dict[str, Any], ...]
    tasks: tuple[submit.CampaignTask, ...]
    lineages: Mapping[str, tuple[submit.CampaignTask, ...]]
    snapshot: campaign.SchedulerSnapshot
    state: campaign.CampaignState
    validated_task_ids: Mapping[str, int]
    validated_result_rows: Mapping[str, Mapping[str, str]]
    result_failures: Mapping[str, campaign.ResultLevelFailure]
    result_audit_pending: tuple[str, ...]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise Stage3RecoveryError(f"{label} must be an object")
    return dict(value)


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise Stage3RecoveryError(
            f"{label} fields changed: expected={sorted(expected)} actual={sorted(value)}"
        )


def _snapshot(path: Path, label: str) -> authority.FileSnapshot:
    try:
        return authority.read_single_link_snapshot(path, label)
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3RecoveryError(str(exc)) from exc


def _source_record(snapshot: authority.FileSnapshot) -> dict[str, str]:
    return {"path": str(snapshot.path), "sha256": snapshot.sha256}


def _replace_flag(argv: Sequence[str], flag: str, value: str) -> tuple[str, ...]:
    try:
        return contract_builder._set_flag(argv, flag, value)
    except contract_builder.Stage3RecoveryBuildError as exc:
        raise Stage3RecoveryError(str(exc)) from exc


def _contract_record(context: RecoveryContext) -> dict[str, str]:
    return {
        "path": str(context.path),
        "raw_sha256": context.snapshot.sha256,
        "contract_sha256": context.contract_sha256,
    }


def load_contract(path: str | Path) -> RecoveryContext:
    try:
        snapshot, document = authority._strict_json_snapshot(
            path, "Stage3 v4r9 recovery contract"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3RecoveryError(str(exc)) from exc
    _expect_keys(document, {"schema_version", "contract_sha256", "recovery"}, "contract")
    if document["schema_version"] != contract_builder.CONTRACT_SCHEMA_VERSION:
        raise Stage3RecoveryError("unsupported v4r9 recovery contract schema")
    unsigned = {
        "schema_version": document["schema_version"],
        "recovery": document["recovery"],
    }
    logical = authority.canonical_sha256(unsigned)
    if document["contract_sha256"] != logical:
        raise Stage3RecoveryError("v4r9 contract_sha256 changed")
    recovery_doc = _mapping(document["recovery"], "recovery")
    _expect_keys(
        recovery_doc,
        {
            "runtime_root",
            "source_root",
            "repository",
            "runtime_dependencies",
            "prior",
            "execution",
            "expected_initial_reconciliation",
            "plan",
            "replacement",
            "outputs",
        },
        "recovery",
    )
    root = Path(str(recovery_doc["runtime_root"])).absolute()
    if root.resolve(strict=True) != contract_builder.EXPECTED_RUNTIME_ROOT.resolve(strict=False):
        raise Stage3RecoveryError("v4r9 root is not the fixed LF325 runtime")
    source_root = Path(str(recovery_doc["source_root"])).absolute()
    if source_root.resolve(strict=True) == root.resolve(strict=True):
        raise Stage3RecoveryError("v4r9 source root overlaps the sealed LF325 runtime")
    expected_path = root / contract_builder.RELATIVE_ROOT / contract_builder.CONTRACT_FILENAME
    if snapshot.path.resolve(strict=True) != expected_path.resolve(strict=False):
        raise Stage3RecoveryError("v4r9 contract path changed")

    repository = _mapping(recovery_doc["repository"], "repository")
    _expect_keys(repository, {"source_root", "revision", "sources"}, "repository")
    if Path(str(repository["source_root"])).absolute() != source_root:
        raise Stage3RecoveryError("repository source root changed")
    revision = str(repository["revision"])
    try:
        live_sources, source_snapshots = contract_builder._source_provenance(
            source_root, revision
        )
    except contract_builder.Stage3RecoveryBuildError as exc:
        raise Stage3RecoveryError(str(exc)) from exc
    sources = _mapping(repository["sources"], "repository sources")
    if sources != live_sources:
        raise Stage3RecoveryError("v4r9 committed source closure changed")
    dependencies = _mapping(
        recovery_doc["runtime_dependencies"], "runtime dependencies"
    )
    try:
        live_dependencies = contract_builder._runtime_dependencies()
    except contract_builder.Stage3RecoveryBuildError as exc:
        raise Stage3RecoveryError(str(exc)) from exc
    if dependencies != live_dependencies:
        raise Stage3RecoveryError(
            f"v4r9 runner dependency versions changed: sealed={dependencies} live={live_dependencies}"
        )
    loaded_modules = {
        "runner": Path(__file__).resolve(strict=True),
        "builder": Path(contract_builder.__file__).resolve(strict=True),
        "activation_builder": Path(activation_builder.__file__).resolve(strict=True),
        "authority": Path(authority.__file__).resolve(strict=True),
        "campaign": Path(campaign.__file__).resolve(strict=True),
        "submit": Path(submit.__file__).resolve(strict=True),
        "collector": Path(collector.__file__).resolve(strict=True),
        "replacement": Path(replacement.__file__).resolve(strict=True),
        "supervisor": Path(supervisor.__file__).resolve(strict=True),
    }
    for name, loaded_path in loaded_modules.items():
        if loaded_path != Path(str(live_sources[name]["path"])).resolve(strict=True):
            raise Stage3RecoveryError(
                f"loaded {name} module is outside the exact-commit source root"
            )

    prior = _mapping(recovery_doc["prior"], "prior authority")
    prior_contract = Path(str(_mapping(prior["acquisition_contract"], "prior contract")["path"]))
    try:
        prior_context, prior_audit, prior_snapshots = contract_builder.v4r8_builder._audit_prior_acquisition(
            prior_contract
        )
    except contract_builder.v4r8_builder.Stage3AcquisitionBuildError as exc:
        raise Stage3RecoveryError(str(exc)) from exc
    if prior_audit["binding"] != prior:
        raise Stage3RecoveryError("v4r9 prior acquisition authority changed")

    execution = _mapping(recovery_doc["execution"], "execution")
    _expect_keys(
        execution,
        {
            "cwd",
            "pythonpath",
            "campaign_argv",
            "runner_dry_argv",
            "runner_execute_argv",
            "project",
            "scheduler_url",
            "task_prefix",
            "project_active_cap",
            "aedt_backend",
            "history_limit",
            "scheduler_timeout_seconds",
            "result_retry_limit",
            "acquisition_only",
            "may_write_decision",
            "may_enter_optimization",
        },
        "execution",
    )
    if (
        execution["cwd"] != str(root)
        or execution["pythonpath"] != [str(source_root)]
        or execution["scheduler_url"] != contract_builder.SCHEDULER_URL
        or int(execution["project_active_cap"]) != contract_builder.PROJECT_ACTIVE_CAP
        or execution["aedt_backend"] != "standalone"
        or int(execution["history_limit"]) != contract_builder.HISTORY_LIMIT
        or float(execution["scheduler_timeout_seconds"])
        != contract_builder.SCHEDULER_TIMEOUT_SECONDS
        or int(execution["result_retry_limit"]) != contract_builder.RESULT_RETRY_LIMIT
        or execution["acquisition_only"] is not True
        or execution["may_write_decision"] is not False
        or execution["may_enter_optimization"] is not False
    ):
        raise Stage3RecoveryError("v4r9 recovery authority was changed or broadened")

    plan_record = _mapping(recovery_doc["plan"], "plan")
    _expect_keys(
        plan_record,
        {"path", "sha256", "rows", "geometry_groups", "rows_per_group"},
        "plan",
    )
    plan_snapshot = _snapshot(Path(str(plan_record["path"])), "v4r9 plan")
    if _source_record(plan_snapshot) != {
        "path": str(plan_record["path"]),
        "sha256": str(plan_record["sha256"]),
    }:
        raise Stage3RecoveryError("v4r9 plan bytes changed")
    rows, _groups = contract_builder._read_plan_groups(plan_snapshot.path)
    if (
        rows != int(plan_record["rows"])
        or int(plan_record["geometry_groups"]) != contract_builder.EXPECTED_GROUPS
        or int(plan_record["rows_per_group"]) != contract_builder.ROWS_PER_GROUP
        or plan_snapshot.path != prior_context.plan
    ):
        raise Stage3RecoveryError("v4r9 plan shape or identity changed")

    expected_initial = _mapping(
        recovery_doc["expected_initial_reconciliation"], "initial reconciliation"
    )
    expected_initial_value = {
        "history_tasks": contract_builder.EXPECTED_INITIAL_HISTORY,
        "logical_cases": contract_builder.EXPECTED_ROWS,
        "successful_results": contract_builder.EXPECTED_INITIAL_OK,
        "result_level_failures": contract_builder.EXPECTED_INITIAL_RESULT_FAILURES,
    }
    if expected_initial != expected_initial_value:
        raise Stage3RecoveryError("initial reconciliation evidence changed")

    replacement_doc = _mapping(recovery_doc["replacement"], "replacement")
    _expect_keys(
        replacement_doc,
        {
            "enabled",
            "seed",
            "maximum_geometry_groups",
            "required_failed_rows",
            "optimization_spec",
            "exclude_plans",
            "plan_output",
            "manifest_output",
            "failure_evidence_dir",
            "failure_evidence_manifest",
        },
        "replacement",
    )
    expected_replacement_plan = root / contract_builder.RELATIVE_ROOT / contract_builder.REPLACEMENT_PLAN_FILENAME
    if (
        replacement_doc["enabled"] is not True
        or int(replacement_doc["seed"]) != contract_builder.REPLACEMENT_SEED
        or int(replacement_doc["maximum_geometry_groups"])
        != contract_builder.REPLACEMENT_GROUP_LIMIT
        or int(replacement_doc["required_failed_rows"]) != contract_builder.ROWS_PER_GROUP
        or Path(str(replacement_doc["plan_output"])).absolute() != expected_replacement_plan
        or Path(str(replacement_doc["manifest_output"])).absolute()
        != replacement.manifest_path_for_output(expected_replacement_plan)
        or Path(str(replacement_doc["failure_evidence_dir"])).absolute()
        != root / contract_builder.RELATIVE_ROOT / contract_builder.FAILURE_EVIDENCE_DIR_NAME
        or Path(str(replacement_doc["failure_evidence_manifest"])).absolute()
        != root / contract_builder.RELATIVE_ROOT / contract_builder.FAILURE_EVIDENCE_FILENAME
    ):
        raise Stage3RecoveryError("replacement authority changed")
    spec_record = _mapping(replacement_doc["optimization_spec"], "optimization spec")
    spec_snapshot = _snapshot(Path(str(spec_record["path"])), "optimization spec")
    if _source_record(spec_snapshot) != spec_record:
        raise Stage3RecoveryError("optimization spec bytes changed")
    exclude_records = replacement_doc["exclude_plans"]
    if not isinstance(exclude_records, list) or len(exclude_records) != 1:
        raise Stage3RecoveryError("replacement exclusion closure changed")
    exclude_snapshots: list[authority.FileSnapshot] = []
    for index, raw in enumerate(exclude_records):
        record = _mapping(raw, f"replacement exclusion {index}")
        exclusion_snapshot = _snapshot(Path(str(record["path"])), f"replacement exclusion {index}")
        if _source_record(exclusion_snapshot) != record:
            raise Stage3RecoveryError("replacement exclusion bytes changed")
        exclude_snapshots.append(exclusion_snapshot)

    campaign_argv = tuple(str(item) for item in execution["campaign_argv"])
    base_args = tuple(str(item) for item in prior_context.campaign_argv[3:])
    base_args = _replace_flag(base_args, "--cases", str(plan_snapshot.path))
    base_args = _replace_flag(base_args, "--scheduler-url", contract_builder.SCHEDULER_URL)
    base_args = _replace_flag(
        base_args, "--project-active-cap", str(contract_builder.PROJECT_ACTIVE_CAP)
    )
    base_args = _replace_flag(base_args, "--history-limit", str(contract_builder.HISTORY_LIMIT))
    base_args = _replace_flag(
        base_args, "--timeout", str(contract_builder.SCHEDULER_TIMEOUT_SECONDS)
    )
    base_args = _replace_flag(base_args, "--aedt-backend", "standalone")
    expected_campaign = (
        live_sources["runner_executable"]["path"],
        "-B",
        live_sources["campaign"]["path"],
        *base_args,
    )
    if campaign_argv != expected_campaign:
        raise Stage3RecoveryError("v4r9 campaign argv changed")
    project = str(execution["project"])
    task_prefix = str(execution["task_prefix"])
    if project != prior_context.project or task_prefix != prior_context.task_prefix:
        raise Stage3RecoveryError("v4r9 scheduler project/prefix changed")
    runner_base = (
        live_sources["runner_executable"]["path"],
        "-B",
        live_sources["runner"]["path"],
        "--contract",
        str(snapshot.path),
    )
    runner_dry = tuple(str(item) for item in execution["runner_dry_argv"])
    runner_execute = tuple(str(item) for item in execution["runner_execute_argv"])
    if runner_dry != runner_base or runner_execute != (*runner_base, "--execute"):
        raise Stage3RecoveryError("v4r9 runner argv changed")

    outputs_doc = _mapping(recovery_doc["outputs"], "outputs")
    _expect_keys(outputs_doc, {"campaign_output_dir", "merged_result", "completion"}, "outputs")
    output_dir = Path(str(outputs_doc["campaign_output_dir"])).absolute()
    merged_result = Path(str(outputs_doc["merged_result"])).absolute()
    completion = Path(str(outputs_doc["completion"])).absolute()
    expected_outputs = {
        "campaign_output_dir": prior_context.outputs["campaign_output_dir"],
        "merged_result": prior_context.outputs["merged_result"],
        "completion": root / contract_builder.RELATIVE_ROOT / contract_builder.COMPLETION_FILENAME,
    }
    outputs = {
        "campaign_output_dir": output_dir,
        "merged_result": merged_result,
        "completion": completion,
    }
    if outputs != expected_outputs:
        raise Stage3RecoveryError("v4r9 output identity changed")
    shared_lock = prior_context.shared_lock
    return RecoveryContext(
        path=snapshot.path,
        snapshot=snapshot,
        contract_sha256=logical,
        document=document,
        root=root,
        source_root=source_root,
        repository_revision=revision,
        sources=sources,
        prior=prior,
        campaign_argv=campaign_argv,
        runner_dry_argv=runner_dry,
        runner_execute_argv=runner_execute,
        project=project,
        scheduler_url=contract_builder.SCHEDULER_URL,
        task_prefix=task_prefix,
        project_active_cap=contract_builder.PROJECT_ACTIVE_CAP,
        history_limit=contract_builder.HISTORY_LIMIT,
        scheduler_timeout_seconds=contract_builder.SCHEDULER_TIMEOUT_SECONDS,
        result_retry_limit=contract_builder.RESULT_RETRY_LIMIT,
        shared_lock=shared_lock,
        plan=plan_snapshot.path,
        plan_sha256=plan_snapshot.sha256,
        replacement=replacement_doc,
        outputs=outputs,
        authority_snapshots=(
            *source_snapshots,
            *prior_snapshots,
            plan_snapshot,
            spec_snapshot,
            *exclude_snapshots,
        ),
    )


def _assert_authority(context: RecoveryContext) -> None:
    for snapshot in context.authority_snapshots:
        try:
            authority.assert_snapshot_unchanged(snapshot, snapshot.path.name)
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3RecoveryError(str(exc)) from exc
    if _snapshot(context.path, "live v4r9 contract").sha256 != context.snapshot.sha256:
        raise Stage3RecoveryError("v4r9 contract changed during execution")


def _args_for_plan(
    context: RecoveryContext,
    plan: Path,
    *,
    retry_limit: int,
) -> argparse.Namespace:
    argv = tuple(context.campaign_argv[3:])
    argv = _replace_flag(argv, "--cases", str(plan))
    argv = _replace_flag(argv, "--terminal-retry-limit", str(retry_limit))
    try:
        args = campaign.build_parser().parse_args(list(argv))
        campaign.validate_args(args)
    except (RuntimeError, SystemExit) as exc:
        raise Stage3RecoveryError(f"sealed campaign arguments are invalid: {exc}") from exc
    if (
        args.scheduler_url != contract_builder.SCHEDULER_URL
        or args.project_active_cap != contract_builder.PROJECT_ACTIVE_CAP
        or args.aedt_backend != "standalone"
        or not args.submit
    ):
        raise Stage3RecoveryError("sealed campaign policy changed")
    return args


def _task_payload_policy(task: submit.CampaignTask, context: RecoveryContext) -> None:
    payload = task.payload
    required = {
        "project": context.project,
        "scheduling_profile": "fea_bursty",
        "required_capability": "conda:pyaedt2026v1",
        "env_profile": "pyaedt2026v1",
        "dedupe_key": task.dedupe_key,
    }
    mismatches = [name for name, expected in required.items() if payload.get(name) != expected]
    if mismatches:
        raise Stage3RecoveryError(
            "retry payload violates the sealed /api/tasks policy: " + ", ".join(mismatches)
        )
    if "module load ansys-electronics/v252" not in str(payload.get("env_setup") or ""):
        raise Stage3RecoveryError("retry payload lacks the explicit Ansys Electronics module load")
    if payload.get("aedt_backend") not in (None, ""):
        raise Stage3RecoveryError("standalone retry payload unexpectedly requests pooled AEDT")


def _reconcile(
    context: RecoveryContext,
    plan: Path,
    *,
    kind: str,
    retry_limit: int,
) -> Reconciliation:
    args = _args_for_plan(context, plan, retry_limit=retry_limit)
    try:
        rows = submit.load_and_validate_cases(args.cases, args.max_plan_cases, False)
        rows = submit.select_case_rows(rows, args.case_start_index, args.case_limit)
        tasks = submit.build_campaign_tasks(
            args, rows, first_row_number=args.case_start_index
        )
        lineages = submit.build_campaign_task_lineages(
            args,
            rows,
            first_row_number=args.case_start_index,
            terminal_retry_limit=retry_limit,
        )
    except RuntimeError as exc:
        raise Stage3RecoveryError(f"cannot reconstruct sealed case identities: {exc}") from exc
    if len(rows) != contract_builder.EXPECTED_ROWS or len(tasks) != contract_builder.EXPECTED_ROWS:
        raise Stage3RecoveryError("recovery does not cover exactly 300 logical cases")
    attempt_tasks = tuple(
        attempt for task in tasks for attempt in lineages[task.dedupe_key]
    )
    for task in attempt_tasks:
        _task_payload_policy(task, context)
    try:
        scheduler_snapshot = campaign.read_scheduler_snapshot(args)
    except RuntimeError as exc:
        raise Stage3RecoveryError(str(exc)) from exc
    validated: dict[str, int] = {}
    validated_result_rows: dict[str, dict[str, str]] = {}
    result_failures: dict[str, campaign.ResultLevelFailure] = {}
    try:
        audit_pending = _audit_completed_results_bounded(
            args,
            attempt_tasks,
            rows,
            scheduler_snapshot.history,
            validated,
            validated_result_rows,
            result_failures,
        )
        state = campaign.classify_campaign_state(
            tasks,
            scheduler_snapshot.history,
            context.project,
            {},
            retry_limit,
            lineages=lineages,
            result_failures=result_failures,
        )
    except RuntimeError as exc:
        raise Stage3RecoveryError(f"cannot reconcile scheduler/result evidence: {exc}") from exc
    return Reconciliation(
        kind=kind,
        args=args,
        rows=tuple(rows),
        tasks=tuple(tasks),
        lineages=lineages,
        snapshot=scheduler_snapshot,
        state=state,
        validated_task_ids=validated,
        validated_result_rows=validated_result_rows,
        result_failures=result_failures,
        result_audit_pending=tuple(audit_pending),
    )


def _audit_completed_results_bounded(
    args: argparse.Namespace,
    completed_tasks: Sequence[submit.CampaignTask],
    selected_rows: Sequence[dict[str, Any]],
    history: Sequence[dict[str, Any]],
    validated_task_ids: dict[str, int],
    validated_result_rows: dict[str, dict[str, str]],
    result_failures: dict[str, campaign.ResultLevelFailure],
) -> tuple[str, ...]:
    """GET and validate completed result rows with a small, fixed fan-out."""

    by_dedupe = campaign._history_by_dedupe(history, args.project)
    rows_by_case = {
        str(row.get("case_id") or "").strip(): dict(row) for row in selected_rows
    }
    work: list[tuple[submit.CampaignTask, int, dict[str, Any]]] = []
    pending: list[str] = []
    for task in completed_tasks:
        successful = [
            item
            for item in by_dedupe.get(task.dedupe_key, ())
            if str(item.get("status") or "").strip().lower() == "completed"
            and campaign._exit_code(item) == 0
            and isinstance(campaign._task_id(item), int)
        ]
        if not successful:
            continue
        latest_id = max(int(campaign._task_id(item)) for item in successful)
        latest = [item for item in successful if campaign._task_id(item) == latest_id]
        if len(latest) != 1:
            raise Stage3RecoveryError(
                f"ambiguous latest completed task for case_id={task.case_id!r}"
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
            raise Stage3RecoveryError(
                f"invalid finished_at for case_id={task.case_id!r}: {finished_raw!r}"
            ) from exc
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - finished_at.astimezone(timezone.utc)).total_seconds()
        if age < args.completed_result_settle_seconds:
            pending.append(f"{task.case_id}:settling")
            continue
        plan_row = rows_by_case.get(task.case_id)
        if plan_row is None:
            raise Stage3RecoveryError(
                f"completed task is outside effective plan: {task.case_id!r}"
            )
        work.append((task, latest_id, plan_row))

    last_fetch_started = 0.0

    def fetch(item: tuple[submit.CampaignTask, int, dict[str, Any]]) -> tuple[Any, ...]:
        nonlocal last_fetch_started
        task, task_id, plan_row = item
        for retry in range(RESULT_FETCH_RATE_LIMIT_RETRIES + 1):
            wait_seconds = RESULT_FETCH_MIN_INTERVAL_SECONDS - (
                time.monotonic() - last_fetch_started
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            last_fetch_started = time.monotonic()
            try:
                text = collector.fetch_task_remote_file(
                    args.scheduler_url,
                    task_id,
                    task.result_csv,
                    "remote_cwd",
                    args.timeout,
                )
                expected_design_hash = str(plan_row.get("design_hash") or "").strip()
                _, result_row = collector._one_remote_result(
                    text,
                    task.case_id,
                    expected_design_hash,
                    allow_failed=True,
                )
                collector.validate_result_matches_plan(plan_row, result_row)
                return task, task_id, text, result_row, None
            except Exception as exc:
                if getattr(exc, "code", None) == 429 and retry < RESULT_FETCH_RATE_LIMIT_RETRIES:
                    time.sleep(max(2.0, RESULT_FETCH_MIN_INTERVAL_SECONDS * (retry + 2)))
                    continue
                message = str(exc).replace("\r", " ").replace("\n", " ")[:160]
                return task, task_id, "", {}, f"{task.case_id}:{type(exc).__name__}:{message}"
        raise AssertionError("unreachable result fetch retry state")

    fetched = [fetch(item) for item in work]
    for task, task_id, text, result_row, error in fetched:
        if error is not None:
            pending.append(error)
            continue
        if str(result_row.get("status") or "").strip().lower() == "failed":
            validated_task_ids.pop(task.dedupe_key, None)
            validated_result_rows.pop(task.dedupe_key, None)
            result_failures[task.dedupe_key] = campaign.ResultLevelFailure(
                case_id=task.case_id,
                retry_index=task.retry_index,
                task_id=task_id,
                dedupe_key=task.dedupe_key,
                remote_result=task.result_csv,
                raw_result_text=text,
                result_error=str(result_row.get("error") or "").strip()[:500],
            )
        else:
            result_failures.pop(task.dedupe_key, None)
            validated_task_ids[task.dedupe_key] = task_id
            validated_result_rows[task.dedupe_key] = dict(result_row)
    return tuple(pending)


def _has_fresh_retry_history(reconciled: Reconciliation) -> bool:
    retry_dedupes = {
        attempt.dedupe_key
        for attempts in reconciled.lineages.values()
        for attempt in attempts
        if attempt.retry_index > 0
    }
    return any(
        str(item.get("dedupe_key") or "") in retry_dedupes
        for item in reconciled.snapshot.history
    )


def _audit_initial_reconciliation(reconciled: Reconciliation) -> None:
    if reconciled.kind != "original" or _has_fresh_retry_history(reconciled):
        return
    if reconciled.result_audit_pending:
        raise Stage3RecoveryError(
            "initial GET-only result audit is incomplete: "
            f"pending={len(reconciled.result_audit_pending)} "
            f"first={reconciled.result_audit_pending[0]}"
        )
    state = reconciled.state
    observed = {
        "history_tasks": reconciled.snapshot.campaign_history_tasks,
        "logical_cases": len(reconciled.tasks),
        "successful_results": len(state.successful),
        "result_level_failures": len(reconciled.result_failures),
    }
    expected = {
        "history_tasks": contract_builder.EXPECTED_INITIAL_HISTORY,
        "logical_cases": contract_builder.EXPECTED_ROWS,
        "successful_results": contract_builder.EXPECTED_INITIAL_OK,
        "result_level_failures": contract_builder.EXPECTED_INITIAL_RESULT_FAILURES,
    }
    if observed != expected:
        raise Stage3RecoveryError(
            f"initial GET-only reconciliation changed: expected={expected} actual={observed}"
        )
    if (
        len(state.retryable) != contract_builder.EXPECTED_INITIAL_RESULT_FAILURES
        or state.missing
        or state.active
        or state.permanently_failed
        or any(task.retry_index != 1 for task in state.retryable)
    ):
        raise Stage3RecoveryError("initial six-case result retry lineage changed")


def _report(context: RecoveryContext, reconciled: Reconciliation, *, mode: str) -> dict[str, Any]:
    state = reconciled.state
    candidates = state.candidates
    effective_active = reconciled.snapshot.project_active_count + len(state.pending)
    open_slots = max(0, context.project_active_cap - effective_active)
    return {
        "schema_version": RUN_REPORT_SCHEMA_VERSION,
        "status": "planned" if mode == "dry-run" else "reconciled",
        "mode": mode,
        "plan_kind": reconciled.kind,
        "scheduler_url": context.scheduler_url,
        "project": context.project,
        "project_active_cap": context.project_active_cap,
        "history_tasks": reconciled.snapshot.campaign_history_tasks,
        "logical_cases": len(reconciled.tasks),
        "successful_results": len(state.successful),
        "result_level_failures": len(reconciled.result_failures),
        "active_cases": len(state.active) + len(state.pending),
        "retryable_cases": len(state.retryable),
        "missing_cases": len(state.missing),
        "permanently_failed_cases": len(state.permanently_failed),
        "result_audit_pending": len(reconciled.result_audit_pending),
        "open_slots": open_slots,
        "planned_submissions": min(len(candidates), open_slots),
        "planned_case_ids": [task.case_id for task in candidates[:open_slots]],
        "writes_performed": 0,
    }


def _replacement_paths(context: RecoveryContext) -> tuple[Path, Path]:
    plan = Path(str(context.replacement["plan_output"])).absolute()
    return plan, replacement.manifest_path_for_output(plan)


def _failure_evidence_paths(context: RecoveryContext) -> tuple[Path, Path]:
    return (
        Path(str(context.replacement["failure_evidence_dir"])).absolute(),
        Path(str(context.replacement["failure_evidence_manifest"])).absolute(),
    )


def _audit_failure_evidence(context: RecoveryContext) -> dict[str, Any]:
    evidence_dir, manifest_path = _failure_evidence_paths(context)
    if not evidence_dir.is_dir() or not manifest_path.is_file():
        raise Stage3RecoveryError("replacement failure evidence is missing")
    try:
        _, manifest = authority._strict_json_snapshot(
            manifest_path, "v4r9 failure evidence manifest"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3RecoveryError(str(exc)) from exc
    if (
        manifest.get("schema_version") != "ipmsm-v2-stage3-v4r9-failure-evidence-v1"
        or manifest.get("contract") != _contract_record(context)
        or int(manifest.get("failed_geometry_row_count", -1))
        != contract_builder.ROWS_PER_GROUP
    ):
        raise Stage3RecoveryError("replacement failure evidence authority changed")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) < contract_builder.ROWS_PER_GROUP:
        raise Stage3RecoveryError("replacement failure result coverage changed")
    expected_names: set[str] = set()
    for raw in entries:
        entry = _mapping(raw, "failure evidence entry")
        local_result = Path(str(entry.get("local_result") or "")).absolute()
        try:
            local_result.relative_to(evidence_dir)
        except ValueError as exc:
            raise Stage3RecoveryError("failure evidence path escaped its sealed directory") from exc
        result_snapshot = _snapshot(local_result, "preserved failed result")
        if result_snapshot.sha256 != str(entry.get("sha256") or ""):
            raise Stage3RecoveryError("preserved failed result bytes changed")
        expected_names.add(local_result.name)
    actual_names = {path.name for path in evidence_dir.iterdir() if path.is_file()}
    if actual_names != expected_names:
        raise Stage3RecoveryError("preserved failed result file set changed")
    return manifest


def _publish_failure_evidence(
    context: RecoveryContext,
    reconciled: Reconciliation,
    *,
    failed_group: str,
    failed_group_case_ids: Sequence[str],
) -> dict[str, Any]:
    evidence_dir, manifest_path = _failure_evidence_paths(context)
    if not evidence_dir.exists():
        try:
            evidence_dir.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise Stage3RecoveryError(
                f"cannot create failed-result evidence directory: {exc}"
            ) from exc
    if not evidence_dir.is_dir():
        raise Stage3RecoveryError("failed-result evidence path is not a directory")
    try:
        authority._directory_identity(evidence_dir, "failed-result evidence directory")
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3RecoveryError(str(exc)) from exc
    failures = sorted(
        reconciled.result_failures.values(),
        key=lambda item: (item.case_id, item.retry_index, item.task_id),
    )
    failed_case_set = set(failed_group_case_ids)
    failures = [failure for failure in failures if failure.case_id in failed_case_set]
    covered_cases = {failure.case_id for failure in failures}
    if covered_cases != failed_case_set:
        raise Stage3RecoveryError(
            "cannot replace geometry without preserving every failed base result row"
        )
    entries: list[dict[str, Any]] = []
    for failure in failures:
        safe_case_id = submit.sanitize_case_id(failure.case_id)
        filename = (
            f"{safe_case_id}_attempt_{failure.retry_index:02d}_task_{failure.task_id}.csv"
        )
        path = evidence_dir / filename
        payload = failure.raw_result_text.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        if path.is_file():
            if _snapshot(path, "existing failed result evidence").payload != payload:
                raise Stage3RecoveryError("existing failed result evidence differs")
        else:
            try:
                activation_builder._publish_no_replace(path, payload)
            except Exception as exc:
                raise Stage3RecoveryError(
                    f"cannot preserve failed result evidence: {exc}"
                ) from exc
        entries.append(
            {
                "case_id": failure.case_id,
                "retry_index": failure.retry_index,
                "task_id": failure.task_id,
                "dedupe_key": failure.dedupe_key,
                "remote_result": failure.remote_result,
                "result_error": failure.result_error,
                "local_result": str(path),
                "sha256": digest,
            }
        )
    manifest = {
        "schema_version": "ipmsm-v2-stage3-v4r9-failure-evidence-v1",
        "contract": _contract_record(context),
        "failed_geometry_group_id": failed_group,
        "failed_geometry_row_count": len(failed_group_case_ids),
        "failed_geometry_case_ids": list(failed_group_case_ids),
        "entries": entries,
    }
    payload = authority.canonical_json_bytes(manifest)
    if manifest_path.is_file():
        if _snapshot(manifest_path, "existing failure evidence manifest").payload != payload:
            raise Stage3RecoveryError("existing failure evidence manifest differs")
    else:
        try:
            activation_builder._publish_no_replace(manifest_path, payload)
        except Exception as exc:
            raise Stage3RecoveryError(
                f"cannot publish failure evidence manifest: {exc}"
            ) from exc
    return _audit_failure_evidence(context)


def _load_replacement_manifest(context: RecoveryContext) -> dict[str, Any] | None:
    plan, manifest_path = _replacement_paths(context)
    if not plan.exists() and not manifest_path.exists():
        return None
    if not plan.is_file() or not manifest_path.is_file():
        raise Stage3RecoveryError("replacement plan/manifest pair is partial")
    try:
        _, manifest = authority._strict_json_snapshot(
            manifest_path, "v4r9 replacement manifest"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3RecoveryError(str(exc)) from exc
    if manifest.get("schema_version") != replacement.MANIFEST_SCHEMA_VERSION:
        raise Stage3RecoveryError("replacement manifest schema changed")
    if manifest.get("mode") != "execute" or manifest.get("status") != "created":
        raise Stage3RecoveryError("replacement manifest is not an executed mapping")
    output = _mapping(manifest.get("output"), "replacement manifest output")
    plan_snapshot = _snapshot(plan, "v4r9 replacement plan")
    if output != _source_record(plan_snapshot):
        raise Stage3RecoveryError("replacement plan bytes changed")
    if (
        int(manifest.get("seed", -1)) != contract_builder.REPLACEMENT_SEED
        or int(manifest.get("row_count", -1)) != contract_builder.EXPECTED_ROWS
        or int(manifest.get("replaced_row_count", -1)) != contract_builder.ROWS_PER_GROUP
        or manifest.get("row_order_preserved") is not True
        or manifest.get("control_fields_preserved") is not True
        or manifest.get("split_repeat_relationships_preserved") is not True
    ):
        raise Stage3RecoveryError("replacement mapping evidence changed")
    contract_builder._read_plan_groups(plan)
    _audit_failure_evidence(context)
    return manifest


def _completion_replacement_record(
    context: RecoveryContext,
    replacement_manifest: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if replacement_manifest is None:
        return None
    manifest_path = _replacement_paths(context)[1]
    failure_manifest_path = _failure_evidence_paths(context)[1]
    return {
        "path": str(manifest_path),
        "sha256": _snapshot(manifest_path, "replacement manifest").sha256,
        "failed_geometry_group_id": replacement_manifest[
            "failed_geometry_group_id"
        ],
        "replacement_geometry_group_id": replacement_manifest[
            "replacement_geometry_group_id"
        ],
        "failure_evidence_manifest": _source_record(
            _snapshot(
                failure_manifest_path,
                "replacement failure evidence manifest",
            )
        ),
    }


def _assert_no_replacement_artifacts(context: RecoveryContext) -> None:
    plan_path, plan_manifest_path = _replacement_paths(context)
    evidence_dir, evidence_manifest_path = _failure_evidence_paths(context)
    unexpected = [
        path
        for path in (
            plan_path,
            plan_manifest_path,
            evidence_dir,
            evidence_manifest_path,
        )
        if os.path.lexists(path)
    ]
    if unexpected:
        raise Stage3RecoveryError(
            f"original completion has unexpected replacement artifact: {unexpected[0]}"
        )


def _expected_completion_history(kind: str) -> int:
    if kind == "original":
        return (
            contract_builder.EXPECTED_INITIAL_HISTORY
            + contract_builder.EXPECTED_INITIAL_RESULT_FAILURES
        )
    if kind == "replacement":
        return (
            contract_builder.EXPECTED_INITIAL_HISTORY
            + contract_builder.EXPECTED_INITIAL_RESULT_FAILURES
            + contract_builder.ROWS_PER_GROUP
        )
    raise Stage3RecoveryError(f"unsupported completion plan kind: {kind!r}")


def _failed_group_identity(
    reconciled: Reconciliation,
) -> tuple[str, str, tuple[str, ...]]:
    permanently_failed_ids = tuple(
        str(item.get("case_id") or "") for item in reconciled.state.permanently_failed
    )
    if (
        not permanently_failed_ids
        or len(permanently_failed_ids) > contract_builder.ROWS_PER_GROUP
        or len(set(permanently_failed_ids)) != len(permanently_failed_ids)
    ):
        raise Stage3RecoveryError("replacement failures must belong to one six-case group")
    by_case = {str(row.get("case_id") or ""): row for row in reconciled.rows}
    try:
        failed_rows = [by_case[case_id] for case_id in permanently_failed_ids]
    except KeyError as exc:
        raise Stage3RecoveryError("failed case is outside the sealed plan") from exc
    groups = {str(row.get("geometry_group_id") or "").strip() for row in failed_rows}
    hashes = {str(row.get("design_hash") or "").strip() for row in failed_rows}
    if len(groups) != 1 or len(hashes) != 1 or "" in groups or "" in hashes:
        raise Stage3RecoveryError("permanent failures do not form one coherent geometry group")
    group = next(iter(groups))
    group_case_ids = tuple(
        str(row.get("case_id") or "")
        for row in reconciled.rows
        if str(row.get("geometry_group_id") or "").strip() == group
    )
    if len(group_case_ids) != contract_builder.ROWS_PER_GROUP:
        raise Stage3RecoveryError("failed geometry does not contain exactly six sealed rows")
    return group, next(iter(hashes)), group_case_ids


def _publish_replacement(
    context: RecoveryContext,
    reconciled: Reconciliation,
) -> dict[str, Any]:
    failed_group, failed_hash, failed_group_case_ids = _failed_group_identity(reconciled)
    plan_output, manifest_output = _replacement_paths(context)
    if plan_output.exists() or manifest_output.exists():
        raise Stage3RecoveryError("replacement mapping already exists")
    spec_path = Path(str(_mapping(context.replacement["optimization_spec"], "spec")["path"]))
    exclude_paths = [
        Path(str(_mapping(item, "replacement exclusion")["path"]))
        for item in context.replacement["exclude_plans"]
    ]
    try:
        spec = replacement.load_optimization_spec(spec_path)
        fieldnames, source_rows = replacement._read_csv_exact(context.plan, "source plan")
        explicit_exclusions, exclusion_artifacts = replacement.read_excluded_design_hashes_exact(
            exclude_paths
        )
        replacement_plan = replacement.build_replacement_plan(
            spec,
            fieldnames,
            source_rows,
            failed_design_hash=failed_hash,
            seed=contract_builder.REPLACEMENT_SEED,
            excluded_design_hashes=explicit_exclusions,
        )
        manifest = replacement.build_manifest(
            replacement_plan,
            mode="execute",
            seed=contract_builder.REPLACEMENT_SEED,
            spec_path=spec_path,
            source_plan=context.plan,
            exclude_artifacts=exclusion_artifacts,
            excluded_design_hash_count=len(
                {str(row["design_hash"]).strip() for row in source_rows}
                | explicit_exclusions
            ),
            output=plan_output,
        )
        if (
            replacement_plan.failed_geometry_group_id != failed_group
            or replacement_plan.replaced_row_count != contract_builder.ROWS_PER_GROUP
            or len(replacement_plan.output_rows) != contract_builder.EXPECTED_ROWS
        ):
            raise Stage3RecoveryError("generated replacement mapping changed scope")
        _publish_failure_evidence(
            context,
            reconciled,
            failed_group=failed_group,
            failed_group_case_ids=failed_group_case_ids,
        )
        replacement._atomic_publish_pair(
            plan_output,
            replacement_plan.output_payload,
            manifest,
        )
    except Stage3RecoveryError:
        raise
    except Exception as exc:
        raise Stage3RecoveryError(f"cannot publish replacement mapping: {exc}") from exc
    return manifest


def _post_candidates(
    context: RecoveryContext,
    reconciled: Reconciliation,
) -> list[dict[str, Any]]:
    state = reconciled.state
    if reconciled.result_audit_pending:
        return []
    candidates = list(state.candidates)
    if not candidates:
        return []
    effective_active = reconciled.snapshot.project_active_count + len(state.pending)
    open_slots = max(0, context.project_active_cap - effective_active)
    if open_slots < len(candidates):
        return []
    if not 1 <= len(candidates) <= contract_builder.ROWS_PER_GROUP:
        raise Stage3RecoveryError(
            f"recovery may submit only one bounded six-case group, found {len(candidates)} candidates"
        )
    if reconciled.kind == "original":
        if any(task.retry_index != 1 for task in candidates):
            raise Stage3RecoveryError("original recovery candidates are not fresh retry identities")
    elif any(task.retry_index != 0 for task in candidates):
        raise Stage3RecoveryError("replacement candidates must be fresh base identities")
    history_by_dedupe = campaign._history_by_dedupe(
        reconciled.snapshot.history, context.project
    )
    submissions: list[dict[str, Any]] = []
    for task in candidates:
        if history_by_dedupe.get(task.dedupe_key):
            raise Stage3RecoveryError("fresh recovery dedupe identity already exists")
        _task_payload_policy(task, context)
        try:
            response = submit.post_scheduler_task(
                context.scheduler_url,
                task.payload,
                context.scheduler_timeout_seconds,
                "/api/tasks",
            )
        except Exception as exc:
            raise Stage3RecoveryError(
                f"/api/tasks POST failed after {len(submissions)} submission(s): {exc}"
            ) from exc
        task_id = campaign._task_id(response)
        if task_id is None:
            raise Stage3RecoveryError("/api/tasks response has no task ID")
        submissions.append(
            {
                "case_id": task.case_id,
                "task_id": task_id,
                "dedupe_key": task.dedupe_key,
                "retry_index": task.retry_index,
            }
        )
    return submissions


def _resolved_results(
    reconciled: Reconciliation,
) -> list[tuple[submit.CampaignTask, dict[str, Any]]]:
    by_dedupe = campaign._history_by_dedupe(
        reconciled.snapshot.history, reconciled.args.project
    )
    resolved: list[tuple[submit.CampaignTask, dict[str, Any]]] = []
    for task in reconciled.state.successful:
        task_id = reconciled.validated_task_ids.get(task.dedupe_key)
        matches = [
            item
            for item in by_dedupe.get(task.dedupe_key, ())
            if campaign._task_id(item) == task_id
        ]
        if task_id is None or len(matches) != 1:
            raise Stage3RecoveryError(
                f"validated result task cannot be resolved for {task.case_id}"
            )
        resolved.append((task, matches[0]))
    return resolved


def _collect_and_complete(
    context: RecoveryContext,
    reconciled: Reconciliation,
    replacement_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    state = reconciled.state
    if (
        reconciled.result_audit_pending
        or state.active
        or state.pending
        or state.missing
        or state.retryable
        or state.permanently_failed
        or len(state.successful) != contract_builder.EXPECTED_ROWS
        or len(reconciled.validated_task_ids) != contract_builder.EXPECTED_ROWS
    ):
        raise Stage3RecoveryError("cannot collect before all 300 results are validated ok")
    expected_history = _expected_completion_history(reconciled.kind)
    if reconciled.snapshot.campaign_history_tasks != expected_history:
        raise Stage3RecoveryError(
            "cannot complete with unexpected scheduler history coverage: "
            f"kind={reconciled.kind} expected={expected_history} "
            f"actual={reconciled.snapshot.campaign_history_tasks}"
        )
    try:
        collector_args = collector.build_parser().parse_args(
            campaign._collector_argv(reconciled.args)
        )
        collector.validate_args(collector_args)
        collected = collector.collect_resolved_campaign(
            collector_args,
            list(reconciled.rows),
            _resolved_results(reconciled),
            history_rows=len(reconciled.snapshot.history),
            campaign_history_tasks=reconciled.snapshot.campaign_history_tasks,
            permanent_failures=[],
            successful_prior_evidence={
                str(item["case_id"]): [
                    dict(evidence) for evidence in item["failure_evidence"]
                ]
                for item in state.recovered_failures
            },
        )
    except (RuntimeError, SystemExit) as exc:
        raise Stage3RecoveryError(f"sealed Stage3 collection failed: {exc}") from exc
    if int(collected.get("collected_results", -1)) != contract_builder.EXPECTED_ROWS:
        raise Stage3RecoveryError("collector did not publish exactly 300 results")
    merged_snapshot = _snapshot(context.outputs["merged_result"], "v4r9 merged result")
    effective_plan = _snapshot(reconciled.args.cases, "v4r9 effective plan")
    completion = {
        "schema_version": COMPLETION_SCHEMA_VERSION,
        "status": "acquisition_complete",
        "contract": _contract_record(context),
        "repository_revision": context.repository_revision,
        "scheduler": {
            "url": context.scheduler_url,
            "project": context.project,
            "task_prefix": context.task_prefix,
            "history_tasks": reconciled.snapshot.campaign_history_tasks,
            "project_active_cap": context.project_active_cap,
        },
        "effective_plan": {
            **_source_record(effective_plan),
            "kind": reconciled.kind,
            "rows": contract_builder.EXPECTED_ROWS,
            "geometry_groups": contract_builder.EXPECTED_GROUPS,
        },
        "replacement_manifest": _completion_replacement_record(
            context, replacement_manifest
        ),
        "result": {
            **_source_record(merged_snapshot),
            "rows": contract_builder.EXPECTED_ROWS,
        },
    }
    completion_path = context.outputs["completion"]
    payload = authority.canonical_json_bytes(completion)
    if completion_path.is_file():
        if _snapshot(completion_path, "v4r9 completion").payload != payload:
            raise Stage3RecoveryError("existing v4r9 completion differs")
        writes = 0
    else:
        try:
            writes = int(activation_builder._publish_no_replace(completion_path, payload))
        except Exception as exc:
            raise Stage3RecoveryError(f"cannot publish v4r9 completion: {exc}") from exc
    return {
        "schema_version": RUN_REPORT_SCHEMA_VERSION,
        "status": "acquisition_complete",
        "mode": "execute",
        "plan_kind": reconciled.kind,
        "successful_results": contract_builder.EXPECTED_ROWS,
        "geometry_groups": contract_builder.EXPECTED_GROUPS,
        "merged_result": str(context.outputs["merged_result"]),
        "completion": str(completion_path),
        "writes_performed": writes,
    }


def dry_run(context: RecoveryContext) -> dict[str, Any]:
    _assert_authority(context)
    replacement_manifest = _load_replacement_manifest(context)
    if replacement_manifest is None:
        reconciled = _reconcile(
            context,
            context.plan,
            kind="original",
            retry_limit=context.result_retry_limit,
        )
        _audit_initial_reconciliation(reconciled)
    else:
        reconciled = _reconcile(
            context,
            _replacement_paths(context)[0],
            kind="replacement",
            retry_limit=0,
        )
    return _report(context, reconciled, mode="dry-run")


def _verify_existing_completion(context: RecoveryContext) -> dict[str, Any] | None:
    completion = context.outputs["completion"]
    if not completion.exists():
        return None
    try:
        _, value = authority._strict_json_snapshot(completion, "v4r9 completion")
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3RecoveryError(str(exc)) from exc
    _expect_keys(
        value,
        {
            "schema_version",
            "status",
            "contract",
            "repository_revision",
            "scheduler",
            "effective_plan",
            "replacement_manifest",
            "result",
        },
        "existing completion",
    )
    if value.get("schema_version") != COMPLETION_SCHEMA_VERSION or value.get(
        "status"
    ) != "acquisition_complete":
        raise Stage3RecoveryError("existing v4r9 completion is not authoritative")
    if value.get("contract") != _contract_record(context):
        raise Stage3RecoveryError("existing completion contract binding changed")
    if value.get("repository_revision") != context.repository_revision:
        raise Stage3RecoveryError("existing completion repository revision changed")

    effective = _mapping(value.get("effective_plan"), "completion effective plan")
    _expect_keys(
        effective,
        {"path", "sha256", "kind", "rows", "geometry_groups"},
        "completion effective plan",
    )
    kind = str(effective.get("kind") or "")
    expected_history = _expected_completion_history(kind)
    if kind == "original":
        _assert_no_replacement_artifacts(context)
        replacement_manifest = None
        effective_plan_path = context.plan
        retry_limit = context.result_retry_limit
    else:
        replacement_manifest = _load_replacement_manifest(context)
        if replacement_manifest is None:
            raise Stage3RecoveryError("replacement completion lacks replacement authority")
        effective_plan_path = _replacement_paths(context)[0]
        retry_limit = 0
    live_plan = _snapshot(effective_plan_path, "completed v4r9 effective plan")
    contract_builder._read_plan_groups(live_plan.path)
    expected_effective = {
        **_source_record(live_plan),
        "kind": kind,
        "rows": contract_builder.EXPECTED_ROWS,
        "geometry_groups": contract_builder.EXPECTED_GROUPS,
    }
    if effective != expected_effective:
        raise Stage3RecoveryError("existing completion effective plan binding changed")
    if kind == "original" and live_plan.sha256 != context.plan_sha256:
        raise Stage3RecoveryError("existing completion original plan changed")
    expected_replacement = _completion_replacement_record(
        context, replacement_manifest
    )
    if value.get("replacement_manifest") != expected_replacement:
        raise Stage3RecoveryError("existing completion replacement binding changed")

    scheduler = _mapping(value.get("scheduler"), "completion scheduler")
    _expect_keys(
        scheduler,
        {"url", "project", "task_prefix", "history_tasks", "project_active_cap"},
        "completion scheduler",
    )
    expected_scheduler = {
        "url": context.scheduler_url,
        "project": context.project,
        "task_prefix": context.task_prefix,
        "history_tasks": expected_history,
        "project_active_cap": context.project_active_cap,
    }
    if scheduler != expected_scheduler:
        raise Stage3RecoveryError("existing completion scheduler binding changed")

    merged_snapshot = _snapshot(
        context.outputs["merged_result"], "completed v4r9 merged result"
    )
    result_record = _mapping(value.get("result"), "completion result")
    _expect_keys(result_record, {"path", "sha256", "rows"}, "completion result")
    if result_record != {
        **_source_record(merged_snapshot),
        "rows": contract_builder.EXPECTED_ROWS,
    }:
        raise Stage3RecoveryError("completed v4r9 merged result bytes changed")

    reconciled = _reconcile(
        context,
        effective_plan_path,
        kind=kind,
        retry_limit=retry_limit,
    )
    state = reconciled.state
    if (
        reconciled.result_audit_pending
        or state.active
        or state.pending
        or state.missing
        or state.retryable
        or state.permanently_failed
        or len(state.successful) != contract_builder.EXPECTED_ROWS
        or len(reconciled.validated_task_ids) != contract_builder.EXPECTED_ROWS
        or len(reconciled.validated_result_rows) != contract_builder.EXPECTED_ROWS
    ):
        raise Stage3RecoveryError("completed v4r9 live result provenance changed")
    if (
        reconciled.snapshot.campaign_history_tasks != expected_history
        or reconciled.snapshot.server_project_cap != context.project_active_cap
    ):
        raise Stage3RecoveryError("completed v4r9 live scheduler provenance changed")
    _headers, merged_rows = replacement._read_csv_exact(
        merged_snapshot.path, "completed merged result"
    )
    expected_merged_rows = [
        dict(reconciled.validated_result_rows[task.dedupe_key])
        for task in state.successful
    ]
    if len(merged_rows) != contract_builder.EXPECTED_ROWS or merged_rows != expected_merged_rows:
        raise Stage3RecoveryError(
            "completed merged result is not the exact ordered remote result set"
        )
    return {
        "schema_version": RUN_REPORT_SCHEMA_VERSION,
        "status": "acquisition_complete",
        "mode": "execute",
        "action": "verified_existing_completion",
        "plan_kind": kind,
        "history_tasks": expected_history,
        "successful_results": contract_builder.EXPECTED_ROWS,
        "writes_performed": 0,
    }


def execute(context: RecoveryContext) -> dict[str, Any]:
    _assert_authority(context)
    with supervisor.ExecutionLock(context.shared_lock):
        _assert_authority(context)
        completed = _verify_existing_completion(context)
        if completed is not None:
            return completed
        replacement_manifest = _load_replacement_manifest(context)
        if replacement_manifest is None:
            reconciled = _reconcile(
                context,
                context.plan,
                kind="original",
                retry_limit=context.result_retry_limit,
            )
            _audit_initial_reconciliation(reconciled)
            state = reconciled.state
            if len(state.successful) == contract_builder.EXPECTED_ROWS:
                return _collect_and_complete(context, reconciled, None)
            if state.permanently_failed and not (
                state.active
                or state.pending
                or state.missing
                or state.retryable
                or reconciled.result_audit_pending
            ):
                replacement_manifest = _publish_replacement(context, reconciled)
                reconciled = _reconcile(
                    context,
                    _replacement_paths(context)[0],
                    kind="replacement",
                    retry_limit=0,
                )
            else:
                submissions = _post_candidates(context, reconciled)
                report = _report(context, reconciled, mode="execute")
                return {
                    **report,
                    "status": "result_retries_submitted" if submissions else "result_retries_pending",
                    "submitted": len(submissions),
                    "submissions": submissions,
                    "writes_performed": len(submissions),
                }
        else:
            reconciled = _reconcile(
                context,
                _replacement_paths(context)[0],
                kind="replacement",
                retry_limit=0,
            )

        state = reconciled.state
        if state.permanently_failed:
            failed_ids = [str(item.get("case_id") or "") for item in state.permanently_failed]
            return {
                **_report(context, reconciled, mode="execute"),
                "status": "replacement_failed",
                "failed_case_ids": failed_ids,
                "submitted": 0,
                "writes_performed": 0,
            }
        if len(state.successful) == contract_builder.EXPECTED_ROWS:
            return _collect_and_complete(context, reconciled, replacement_manifest)
        submissions = _post_candidates(context, reconciled)
        report = _report(context, reconciled, mode="execute")
        return {
            **report,
            "status": "replacement_submitted" if submissions else "replacement_pending",
            "submitted": len(submissions),
            "submissions": submissions,
            "replacement_manifest": str(_replacement_paths(context)[1]),
            "writes_performed": len(submissions),
        }


def _audit_process_argv(context: RecoveryContext, execute_mode: bool) -> None:
    expected = context.runner_execute_argv if execute_mode else context.runner_dry_argv
    observed_raw = getattr(sys, "orig_argv", None)
    if not isinstance(observed_raw, list) or not observed_raw:
        observed_raw = [sys.executable, *sys.argv]
    if tuple(str(item) for item in observed_raw) != expected:
        raise Stage3RecoveryError("live runner argv differs from the sealed v4r9 contract")
    if Path.cwd().resolve(strict=True) != context.root.resolve(strict=True):
        raise Stage3RecoveryError("v4r9 runner cwd is not the sealed LF325 runtime root")
    if not sys.path or Path(sys.path[0]).resolve(strict=True) != context.source_root.resolve(
        strict=True
    ):
        raise Stage3RecoveryError("v4r9 source root is not first on sys.path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = load_contract(args.contract)
        _audit_process_argv(context, args.execute)
        report = execute(context) if args.execute else dry_run(context)
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        Stage3RecoveryError,
        contract_builder.Stage3RecoveryBuildError,
        authority.TargetLoadAuthorityError,
        OSError,
    ) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
