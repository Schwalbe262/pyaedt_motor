"""Run only the sealed v4r7 Stage3 acquisition/collection maintenance path."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

import build_ipmsm_v2_stage3_acquisition_v4r7 as contract_builder
import build_ipmsm_v2_stage3_activation_v4r6 as prior_builder
import collect_ipmsm_v2_campaign as sealed_collector
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import continue_ipmsm_v2_stage2 as stage2_continuation
import continue_ipmsm_v2_stage3_v4r6 as prior_runner
import merge_ipmsm_v2_results as sealed_merger
import run_ipmsm_v2_campaign as sealed_campaign
import submit_ipmsm_v2_campaign as sealed_submit
import supervise_ipmsm_v2_pipeline as v3
from urllib import parse as url_parse
from urllib import request as url_request


RUN_REPORT_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r7-run-v1"
COMPLETION_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r7-completion-v1"


class Stage3AcquisitionError(RuntimeError):
    """The sealed acquisition-only maintenance path is not safe to run."""


@dataclass(frozen=True)
class AcquisitionContext:
    path: Path
    snapshot: authority.FileSnapshot
    contract_sha256: str
    document: Mapping[str, Any]
    root: Path
    prior_context: Any
    prior: Mapping[str, Any]
    sources: Mapping[str, Any]
    campaign_argv: tuple[str, ...]
    runner_dry_argv: tuple[str, ...]
    runner_execute_argv: tuple[str, ...]
    environment: Mapping[str, str]
    project: str
    scheduler_url: str
    task_prefix: str
    project_active_cap: int
    history_limit: int
    scheduler_timeout_seconds: float
    expected_rows: int
    shared_lock: Path
    plan: Path
    outputs: Mapping[str, Path]
    decision_snapshot: authority.FileSnapshot
    authority_snapshots: tuple[authority.FileSnapshot, ...]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise Stage3AcquisitionError(f"{label} must be an object")
    return dict(value)


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise Stage3AcquisitionError(
            f"{label} fields changed: expected={sorted(expected)} actual={sorted(value)}"
        )


def _path(value: Any, label: str) -> Path:
    try:
        path = authority._require_c_local(Path(str(value)).absolute(), label)
        authority._audit_parent_chain(path, label)
        return path
    except (OSError, authority.TargetLoadAuthorityError) as exc:
        raise Stage3AcquisitionError(str(exc)) from exc


def _source_snapshot(
    raw: Any,
    label: str,
    *,
    require_single_link: bool = True,
) -> authority.FileSnapshot:
    record = _mapping(raw, label)
    _expect_keys(record, {"path", "sha256"}, label)
    path = _path(record["path"], f"{label}.path")
    try:
        snapshot = authority.read_single_link_snapshot(
            path,
            label,
            require_single_link=require_single_link,
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc
    if {"path": str(snapshot.path), "sha256": snapshot.sha256} != dict(record):
        raise Stage3AcquisitionError(f"{label} bytes changed")
    return snapshot


def _contract_record(context: AcquisitionContext) -> dict[str, str]:
    return {
        "path": str(context.path),
        "raw_sha256": context.snapshot.sha256,
        "contract_sha256": context.contract_sha256,
    }


def _audit_loaded_sources(
    sources: Mapping[str, Any],
    snapshots: Mapping[str, authority.FileSnapshot],
) -> None:
    expected_loaded = {
        "builder": Path(contract_builder.__file__).resolve(strict=True),
        "runner": Path(__file__).resolve(strict=True),
        "authority": Path(authority.__file__).resolve(strict=True),
        "runner_executable": Path(sys.executable).resolve(strict=True),
    }
    for name, loaded in expected_loaded.items():
        if snapshots[name].path != loaded:
            raise Stage3AcquisitionError(f"loaded {name} differs from its source pin")
    inherited = _mapping(sources["inherited"], "acquisition.sources.inherited")
    loaded_inherited = {
        "prior_activation_builder": Path(prior_builder.__file__).resolve(strict=True),
        "prior_activation_runner": Path(prior_runner.__file__).resolve(strict=True),
        "prior_activation_authority": Path(authority.__file__).resolve(strict=True),
        "prior_parent_optimization_source_continue_ipmsm_v2_stage2": Path(
            stage2_continuation.__file__
        ).resolve(strict=True),
        "prior_parent_optimization_source_collect_ipmsm_v2_campaign": Path(
            sealed_collector.__file__
        ).resolve(strict=True),
        "prior_parent_optimization_source_merge_ipmsm_v2_results": Path(
            sealed_merger.__file__
        ).resolve(strict=True),
        "prior_parent_optimization_source_submit_ipmsm_v2_campaign": Path(
            sealed_submit.__file__
        ).resolve(strict=True),
        "prior_parent_optimization_source_run_ipmsm_v2_campaign": Path(
            sealed_campaign.__file__
        ).resolve(strict=True),
        "prior_parent_supervisor_v3": Path(v3.__file__).resolve(strict=True),
    }
    for name, loaded in loaded_inherited.items():
        record = inherited.get(name)
        if not isinstance(record, Mapping) or Path(str(record.get("path"))).resolve(strict=True) != loaded:
            raise Stage3AcquisitionError(f"loaded inherited source differs: {name}")


def load_contract(path: str | Path) -> AcquisitionContext:
    try:
        snapshot, document = authority._strict_json_snapshot(
            path, "Stage3 v4r7 acquisition contract"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc
    _expect_keys(
        document,
        {"schema_version", "contract_sha256", "acquisition"},
        "acquisition contract",
    )
    if document["schema_version"] != contract_builder.CONTRACT_SCHEMA_VERSION:
        raise Stage3AcquisitionError("unsupported acquisition contract schema_version")
    unsigned = {
        "schema_version": document["schema_version"],
        "acquisition": document["acquisition"],
    }
    logical = authority.canonical_sha256(unsigned)
    if document["contract_sha256"] != logical:
        raise Stage3AcquisitionError("acquisition contract_sha256 changed")
    acquisition = _mapping(document["acquisition"], "acquisition")
    _expect_keys(
        acquisition,
        {"root", "build_config", "prior", "sources", "execution", "outputs", "plan"},
        "acquisition",
    )
    root = _path(acquisition["root"], "acquisition root")
    if root.resolve(strict=True) != contract_builder.EXPECTED_RUNTIME_ROOT.resolve(strict=False):
        raise Stage3AcquisitionError("acquisition root is not the fixed LF325 runtime")
    expected_contract = root / contract_builder.RELATIVE_ROOT / contract_builder.CONTRACT_FILENAME
    if snapshot.path.resolve(strict=True) != expected_contract.resolve(strict=False):
        raise Stage3AcquisitionError("acquisition contract path changed")

    config_record = _mapping(acquisition["build_config"], "acquisition.build_config")
    _expect_keys(config_record, {"path", "sha256"}, "acquisition.build_config")
    try:
        config_snapshot, config, resolved = contract_builder._load_config(config_record["path"])
    except contract_builder.Stage3AcquisitionBuildError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc
    if {"path": str(config_snapshot.path), "sha256": config_snapshot.sha256} != config_record:
        raise Stage3AcquisitionError("acquisition build config binding changed")
    if Path(str(config["output_contract"])).absolute() != snapshot.path:
        raise Stage3AcquisitionError("acquisition build config output changed")

    sources = _mapping(acquisition["sources"], "acquisition.sources")
    _expect_keys(
        sources,
        {
            "campaign",
            "submit",
            "collector",
            "builder",
            "runner",
            "authority",
            "runner_executable",
            "inherited",
        },
        "acquisition.sources",
    )
    source_snapshots = {
        name: _source_snapshot(
            sources[name],
            f"acquisition source {name}",
            require_single_link=name != "runner_executable",
        )
        for name in (
            "campaign",
            "submit",
            "collector",
            "builder",
            "runner",
            "authority",
            "runner_executable",
        )
    }
    inherited = _mapping(sources["inherited"], "acquisition.sources.inherited")
    inherited_snapshots = {
        name: _source_snapshot(
            record,
            f"inherited source {name}",
            require_single_link=not name.endswith("runner_executable"),
        )
        for name, record in sorted(inherited.items())
    }
    _audit_loaded_sources(sources, source_snapshots)
    source_root = root / contract_builder.SOURCE_RELATIVE_ROOT
    if any(source_snapshots[name].path.parent != source_root for name in ("campaign", "submit", "collector")):
        raise Stage3AcquisitionError("patched source directory changed")
    source_entries = sorted(item.name for item in source_root.iterdir())
    if source_entries != sorted(
        (
            contract_builder.CAMPAIGN_FILENAME,
            contract_builder.SUBMIT_FILENAME,
            contract_builder.COLLECTOR_FILENAME,
        )
    ):
        raise Stage3AcquisitionError("patched source directory gained an import shadow")

    prior_record = _mapping(acquisition["prior"], "acquisition.prior")
    try:
        prior_context, prior_audit, prior_snapshots = contract_builder._audit_prior_activation(
            Path(prior_record["activation_contract"]["path"])
        )
    except (KeyError, TypeError, contract_builder.Stage3AcquisitionBuildError) as exc:
        raise Stage3AcquisitionError(f"prior v4r6 activation replay failed: {exc}") from exc
    if prior_audit["binding"] != prior_record:
        raise Stage3AcquisitionError("prior v4r6 acquisition authority changed")
    if resolved["prior_snapshot"].path != prior_context.snapshot.path:
        raise Stage3AcquisitionError("build config points to another prior activation")

    execution = _mapping(acquisition["execution"], "acquisition.execution")
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
            "history_limit",
            "scheduler_timeout_seconds",
            "expected_rows",
            "shared_lock",
            "acquisition_only",
            "may_write_decision",
            "may_enter_optimization",
        },
        "acquisition.execution",
    )
    if execution["cwd"] != str(root):
        raise Stage3AcquisitionError("acquisition cwd changed")
    if execution["pythonpath"] != [str(source_root), str(root)]:
        raise Stage3AcquisitionError("acquisition PYTHONPATH changed")
    if (
        execution["acquisition_only"] is not True
        or execution["may_write_decision"] is not False
        or execution["may_enter_optimization"] is not False
    ):
        raise Stage3AcquisitionError("acquisition-only authority was broadened")
    if int(execution["project_active_cap"]) != contract_builder.PROJECT_ACTIVE_CAP:
        raise Stage3AcquisitionError("acquisition project cap changed")
    if int(execution["history_limit"]) != contract_builder.HISTORY_LIMIT:
        raise Stage3AcquisitionError("acquisition history limit changed")
    timeout = float(execution["scheduler_timeout_seconds"])
    if timeout < contract_builder.SCHEDULER_TIMEOUT_SECONDS:
        raise Stage3AcquisitionError("acquisition scheduler timeout is below 300 seconds")
    if int(execution["expected_rows"]) != contract_builder.EXPECTED_ROWS:
        raise Stage3AcquisitionError("acquisition expected row count changed")

    decision = prior_audit["decision"]
    base_argv = tuple(str(item) for item in decision["stage2"]["runner_argv"])
    expected_campaign = (
        str(source_snapshots["runner_executable"].path),
        "-B",
        str(source_snapshots["campaign"].path),
        *base_argv,
        "--history-limit",
        str(contract_builder.HISTORY_LIMIT),
        "--timeout",
        str(contract_builder.SCHEDULER_TIMEOUT_SECONDS),
    )
    campaign_argv = tuple(str(item) for item in execution["campaign_argv"])
    if campaign_argv != expected_campaign:
        raise Stage3AcquisitionError("acquisition campaign argv changed")
    runner_base = (
        str(source_snapshots["runner_executable"].path),
        "-B",
        str(source_snapshots["runner"].path),
        "--contract",
        str(snapshot.path),
    )
    runner_dry_argv = tuple(str(item) for item in execution["runner_dry_argv"])
    runner_execute_argv = tuple(str(item) for item in execution["runner_execute_argv"])
    if runner_dry_argv != runner_base or runner_execute_argv != (*runner_base, "--execute"):
        raise Stage3AcquisitionError("acquisition runner argv changed")
    project = str(execution["project"])
    scheduler_url = str(execution["scheduler_url"])
    task_prefix = str(execution["task_prefix"])
    if (
        project != prior_context.scheduler["project"]
        or scheduler_url != prior_context.scheduler["scheduler_url"]
        or task_prefix != prior_context.scheduler["task_prefix"]
    ):
        raise Stage3AcquisitionError("acquisition scheduler identity changed")
    shared_lock = _path(execution["shared_lock"], "acquisition shared lock")
    if shared_lock != prior_context.shared_lock:
        raise Stage3AcquisitionError("acquisition shared lock changed")

    plan_record = _mapping(acquisition["plan"], "acquisition.plan")
    _expect_keys(plan_record, {"path", "sha256", "rows"}, "acquisition.plan")
    plan_snapshot = _source_snapshot(
        {"path": plan_record["path"], "sha256": plan_record["sha256"]},
        "acquisition plan",
    )
    if plan_snapshot.path != prior_context.outputs["plan"] or int(plan_record["rows"]) != contract_builder.EXPECTED_ROWS:
        raise Stage3AcquisitionError("acquisition plan binding changed")

    outputs_raw = _mapping(acquisition["outputs"], "acquisition.outputs")
    _expect_keys(
        outputs_raw,
        {"campaign_output_dir", "merged_result", "completion"},
        "acquisition.outputs",
    )
    output_dir_from_contract = _path(
        outputs_raw["campaign_output_dir"], "acquisition output campaign_output_dir"
    )
    completion_from_contract = _path(
        outputs_raw["completion"], "acquisition output completion"
    )
    merged_from_contract = Path(str(outputs_raw["merged_result"])).absolute()
    try:
        merged_from_contract = authority._require_c_local(
            merged_from_contract, "acquisition output merged_result"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc
    outputs = {
        "campaign_output_dir": output_dir_from_contract,
        "merged_result": merged_from_contract,
        "completion": completion_from_contract,
    }
    raw_output_dir = Path(str(decision["stage2"]["output_dir"]))
    output_dir = _path(
        raw_output_dir if raw_output_dir.is_absolute() else root / raw_output_dir,
        "decision Stage3 output_dir",
    )
    merged_name_index = base_argv.index("--merged-output") + 1
    expected_outputs = {
        "campaign_output_dir": output_dir,
        "merged_result": output_dir / base_argv[merged_name_index],
        "completion": root / contract_builder.RELATIVE_ROOT / contract_builder.COMPLETION_FILENAME,
    }
    if outputs != expected_outputs:
        raise Stage3AcquisitionError("acquisition output paths changed")

    decision_snapshot = _source_snapshot(
        {
            "path": prior_record["decision"]["path"],
            "sha256": prior_record["decision"]["sha256"],
        },
        "sealed acquisition decision",
    )
    return AcquisitionContext(
        path=snapshot.path,
        snapshot=snapshot,
        contract_sha256=logical,
        document=document,
        root=root,
        prior_context=prior_context,
        prior=prior_record,
        sources=sources,
        campaign_argv=campaign_argv,
        runner_dry_argv=runner_dry_argv,
        runner_execute_argv=runner_execute_argv,
        environment={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((str(source_root), str(root))),
        },
        project=project,
        scheduler_url=scheduler_url,
        task_prefix=task_prefix,
        project_active_cap=int(execution["project_active_cap"]),
        history_limit=int(execution["history_limit"]),
        scheduler_timeout_seconds=timeout,
        expected_rows=int(execution["expected_rows"]),
        shared_lock=shared_lock,
        plan=plan_snapshot.path,
        outputs=outputs,
        decision_snapshot=decision_snapshot,
        authority_snapshots=(
            config_snapshot,
            *source_snapshots.values(),
            *inherited_snapshots.values(),
            *prior_snapshots,
            plan_snapshot,
            decision_snapshot,
        ),
    )


def _assert_decision_unchanged(context: AcquisitionContext) -> None:
    try:
        authority.assert_snapshot_unchanged(
            context.decision_snapshot, "sealed acquisition decision"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc


def _assert_full_authority(context: AcquisitionContext) -> None:
    for snapshot in context.authority_snapshots:
        try:
            authority.assert_snapshot_unchanged(snapshot, snapshot.path.name)
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3AcquisitionError(str(exc)) from exc
    live = load_contract(context.path)
    if live.snapshot.sha256 != context.snapshot.sha256:
        raise Stage3AcquisitionError("acquisition contract changed during execution")
    _assert_decision_unchanged(live)


@contextmanager
def _frozen_authority(context: AcquisitionContext) -> Any:
    if os.name != "nt":
        raise Stage3AcquisitionError("v4r7 authority freeze requires C-native Windows")
    import ctypes
    from ctypes import wintypes

    deduped = {snapshot.path: snapshot for snapshot in context.authority_snapshots}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    handles: list[Any] = []
    try:
        for snapshot in deduped.values():
            handle = create_file(
                str(snapshot.path),
                0x80000000,  # GENERIC_READ
                0x00000001,  # FILE_SHARE_READ: deny WRITE and DELETE
                None,
                3,  # OPEN_EXISTING
                0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
                None,
            )
            if not handle or int(handle) == invalid:
                raise OSError(
                    ctypes.get_last_error(),
                    f"cannot freeze acquisition authority file: {snapshot.path}",
                )
            handles.append(handle)
        source_root = context.root / contract_builder.SOURCE_RELATIVE_ROOT
        directory_handle = create_file(
            str(source_root),
            0x80000000,
            0x00000001,
            None,
            3,
            0x00200000 | 0x02000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
            None,
        )
        if not directory_handle or int(directory_handle) == invalid:
            raise OSError(
                ctypes.get_last_error(),
                f"cannot freeze acquisition source directory: {source_root}",
            )
        handles.append(directory_handle)
        _assert_full_authority(context)
        yield
        _assert_full_authority(context)
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def _read_csv_snapshot(
    snapshot: authority.FileSnapshot,
    label: str,
) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = snapshot.payload.decode("utf-8-sig")
    except UnicodeError as exc:
        raise Stage3AcquisitionError(f"{label} is not UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = list(reader.fieldnames or ())
    if (
        not headers
        or any(not str(column or "").strip() for column in headers)
        or len(headers) != len(set(headers))
    ):
        raise Stage3AcquisitionError(f"{label} has an invalid header")
    rows = [dict(row) for row in reader]
    if any(None in row for row in rows):
        raise Stage3AcquisitionError(f"{label} has fields beyond its header")
    return headers, rows


def _live_snapshot(path: Path, label: str) -> authority.FileSnapshot:
    try:
        return authority.read_single_link_snapshot(path, label)
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc


def _audit_output_provenance(context: AcquisitionContext) -> dict[str, Any]:
    output_dir = context.outputs["campaign_output_dir"]
    merged_path = context.outputs["merged_result"]
    selected_plan_path = output_dir / sealed_collector.SELECTED_PLAN_NAME
    results_dir = output_dir / "results"
    try:
        authority._directory_identity(output_dir, "Stage3 acquisition output directory")
        authority._directory_identity(results_dir, "Stage3 acquisition results directory")
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc
    expected_top_level = sorted(
        (sealed_collector.SELECTED_PLAN_NAME, merged_path.name, "results")
    )
    if sorted(path.name for path in output_dir.iterdir()) != expected_top_level:
        raise Stage3AcquisitionError("Stage3 acquisition output directory contents changed")

    original_plan_snapshot = _live_snapshot(context.plan, "sealed Stage3 plan")
    selected_plan_snapshot = _live_snapshot(
        selected_plan_path, "collected Stage3 selected plan"
    )
    original_headers, original_rows = _read_csv_snapshot(
        original_plan_snapshot, "sealed Stage3 plan"
    )
    selected_headers, selected_rows = _read_csv_snapshot(
        selected_plan_snapshot, "collected Stage3 selected plan"
    )
    if original_headers != selected_headers or original_rows != selected_rows:
        raise Stage3AcquisitionError("collected Stage3 selected plan differs from sealed plan")
    if len(original_rows) != context.expected_rows:
        raise Stage3AcquisitionError("collected Stage3 plan is not exactly 300 rows")
    try:
        case_ids = sealed_merger.unique_case_ids(original_rows, source=str(context.plan))
    except ValueError as exc:
        raise Stage3AcquisitionError(f"sealed Stage3 plan case IDs are invalid: {exc}") from exc

    expected_names: list[str] = []
    expected_by_case: dict[str, Path] = {}
    for case_id in case_ids:
        safe_case_id = sealed_submit.sanitize_case_id(case_id)
        result_path = results_dir / f"{safe_case_id}.csv"
        expected_names.append(result_path.name)
        expected_by_case[case_id] = result_path
    if len(set(expected_names)) != context.expected_rows:
        raise Stage3AcquisitionError("Stage3 result filename identities collide")
    if sorted(path.name for path in results_dir.iterdir()) != sorted(expected_names):
        raise Stage3AcquisitionError("Stage3 per-case result file set is not exact")

    union_headers: list[str] = []
    merged_rows_expected: list[dict[str, str]] = []
    collected_rows: list[dict[str, str]] = []
    result_records: list[dict[str, str]] = []
    plan_by_case = {str(row["case_id"]).strip(): row for row in original_rows}
    for case_id in case_ids:
        plan_row = plan_by_case[case_id]
        result_path = expected_by_case[case_id]
        result_snapshot = _live_snapshot(result_path, f"Stage3 result {case_id}")
        try:
            text = result_snapshot.payload.decode("utf-8-sig")
        except UnicodeError as exc:
            raise Stage3AcquisitionError(f"Stage3 result is not UTF-8: {case_id}") from exc
        expected_design_hash = str(plan_row.get("design_hash") or "").strip()
        try:
            headers, result_row = sealed_collector._one_remote_result(
                text,
                case_id,
                expected_design_hash,
            )
            sealed_collector.validate_result_matches_plan(plan_row, result_row)
        except Exception as exc:
            raise Stage3AcquisitionError(
                f"Stage3 result does not replay against the sealed plan: {case_id}: {exc}"
            ) from exc
        for header in headers:
            if header not in union_headers:
                union_headers.append(header)
        merged_rows_expected.append(result_row)
        collected_rows.append(result_row)
        result_records.append(
            {
                "case_id": case_id,
                "path": str(result_snapshot.path),
                "sha256": result_snapshot.sha256,
            }
        )
    try:
        sealed_collector.validate_homogeneous_fingerprints(collected_rows)
    except Exception as exc:
        raise Stage3AcquisitionError(f"Stage3 result fingerprints changed: {exc}") from exc

    merged_snapshot = _live_snapshot(merged_path, "Stage3 acquired merged result")
    merged_headers, merged_rows = _read_csv_snapshot(
        merged_snapshot, "Stage3 acquired merged result"
    )
    if merged_headers != union_headers or merged_rows != merged_rows_expected:
        raise Stage3AcquisitionError(
            "Stage3 merged result is not the exact ordered merge of per-case results"
        )
    return {
        "selected_plan": {
            "path": str(selected_plan_snapshot.path),
            "sha256": selected_plan_snapshot.sha256,
        },
        "merged_result": {
            "path": str(merged_snapshot.path),
            "sha256": merged_snapshot.sha256,
        },
        "result_count": len(result_records),
        "result_set_sha256": authority.canonical_sha256({"results": result_records}),
    }


def _audit_scheduler_provenance(context: AcquisitionContext) -> dict[str, Any]:
    try:
        args = sealed_campaign.build_parser().parse_args(list(context.campaign_argv[3:]))
        selected_rows = sealed_submit.load_and_validate_cases(
            context.plan,
            args.max_plan_cases,
            False,
        )
        selected_rows = sealed_submit.select_case_rows(
            selected_rows,
            args.case_start_index,
            args.case_limit,
        )
        campaign_tasks = sealed_submit.build_campaign_tasks(
            args,
            selected_rows,
            first_row_number=args.case_start_index,
        )
    except (RuntimeError, SystemExit) as exc:
        raise Stage3AcquisitionError(
            f"cannot reconstruct sealed scheduler task identities: {exc}"
        ) from exc
    if len(campaign_tasks) != context.expected_rows:
        raise Stage3AcquisitionError("scheduler provenance does not cover exactly 300 tasks")
    query = url_parse.urlencode(
        {
            "limit": context.history_limit,
            "project": context.project,
            "name_prefix": context.task_prefix,
        }
    )
    url = context.scheduler_url.rstrip("/") + f"/api/tasks?{query}"
    try:
        with url_request.urlopen(url, timeout=context.scheduler_timeout_seconds) as response:
            value = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise Stage3AcquisitionError(
            f"cannot audit scheduler provenance for completed acquisition: {exc}"
        ) from exc
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise Stage3AcquisitionError("scheduler provenance response is not a task list")
    history = [dict(item) for item in value]
    if len(history) >= context.history_limit:
        raise Stage3AcquisitionError("scheduler provenance history is saturated")
    expected_by_dedupe = {task.dedupe_key: task for task in campaign_tasks}
    attempts_by_dedupe: dict[str, list[dict[str, Any]]] = {
        dedupe_key: [] for dedupe_key in expected_by_dedupe
    }
    seen_task_ids: set[int | str] = set()
    for task in history:
        if (
            not sealed_submit.task_belongs_to_project(task, context.project)
            or not str(task.get("name") or "").startswith(context.task_prefix)
        ):
            raise Stage3AcquisitionError(
                "scheduler provenance returned a task outside the sealed project/prefix"
            )
        dedupe_key = str(task.get("dedupe_key") or "").strip()
        expected_task = expected_by_dedupe.get(dedupe_key)
        if expected_task is None:
            raise Stage3AcquisitionError(
                "scheduler provenance contains an unplanned task under the sealed prefix"
            )
        if str(task.get("name") or "") != expected_task.task_name:
            raise Stage3AcquisitionError("scheduler provenance task name changed")
        task_id = sealed_campaign._task_id(task)
        if task_id is None or task_id in seen_task_ids:
            raise Stage3AcquisitionError("scheduler provenance task IDs are missing or duplicated")
        seen_task_ids.add(task_id)
        attempts_by_dedupe[dedupe_key].append(task)

    selected_attempts: list[dict[str, Any]] = []
    for expected_task in campaign_tasks:
        attempts = attempts_by_dedupe[expected_task.dedupe_key]
        if not 1 <= len(attempts) <= 2:
            raise Stage3AcquisitionError(
                f"scheduler provenance attempt count changed: {expected_task.case_id}"
            )
        statuses = [str(item.get("status") or "").strip().lower() for item in attempts]
        if any(status not in sealed_campaign.KNOWN_STATUSES for status in statuses):
            raise Stage3AcquisitionError(
                f"scheduler provenance has an unknown status: {expected_task.case_id}"
            )
        if any(status in sealed_campaign.ACTIVE_STATUSES for status in statuses):
            raise Stage3AcquisitionError(
                f"scheduler provenance remains active: {expected_task.case_id}"
            )
        successful = [
            item
            for item in attempts
            if str(item.get("status") or "").strip().lower() == "completed"
            and sealed_campaign._exit_code(item) == 0
        ]
        retryable = [
            item
            for item in attempts
            if str(item.get("status") or "").strip().lower()
            in sealed_campaign.TERMINAL_RETRY_STATUSES
        ]
        if len(successful) != 1 or len(retryable) > 1 or len(attempts) != len(successful) + len(retryable):
            raise Stage3AcquisitionError(
                f"scheduler provenance terminal lineage changed: {expected_task.case_id}"
            )
        selected = successful[0]
        selected_attempts.append(
            {
                "case_id": expected_task.case_id,
                "dedupe_key": expected_task.dedupe_key,
                "task_id": sealed_campaign._task_id(selected),
            }
        )
    return {
        "history_count": len(history),
        "selected_task_count": len(selected_attempts),
        "selected_task_set_sha256": authority.canonical_sha256(
            {"tasks": selected_attempts}
        ),
    }


def _output_state(context: AcquisitionContext) -> str:
    output_dir = context.outputs["campaign_output_dir"]
    merged = context.outputs["merged_result"]
    if not output_dir.exists():
        return "absent"
    if not output_dir.is_dir() or not merged.is_file():
        raise Stage3AcquisitionError(
            f"Stage3 acquisition output is partial and cannot be resumed safely: {output_dir}"
        )
    try:
        stage2_continuation._validate_result_coverage(
            context.plan,
            merged,
            "Stage3 v4r7 acquired result",
        )
    except Exception as exc:
        raise Stage3AcquisitionError(f"Stage3 acquired result is not exact: {exc}") from exc
    _audit_output_provenance(context)
    return "complete"


def _completion_value(context: AcquisitionContext) -> dict[str, Any]:
    provenance = _audit_output_provenance(context)
    scheduler_provenance = _audit_scheduler_provenance(context)
    return {
        "schema_version": COMPLETION_SCHEMA_VERSION,
        "status": "complete",
        "contract": _contract_record(context),
        "prior_activation_contract": dict(context.prior["activation_contract"]),
        "decision": dict(context.prior["decision"]),
        "plan": {
            "path": str(context.plan),
            "sha256": context.prior["plan"]["sha256"],
            "rows": context.expected_rows,
        },
        "result": provenance["merged_result"],
        "collector_provenance": provenance,
        "scheduler_provenance": scheduler_provenance,
    }


def _audit_or_publish_completion(context: AcquisitionContext, *, publish: bool) -> bool:
    path = context.outputs["completion"]
    expected = _completion_value(context)
    payload = authority.canonical_json_bytes(expected)
    if path.is_file():
        try:
            snapshot = authority.read_single_link_snapshot(path, "v4r7 acquisition completion")
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3AcquisitionError(str(exc)) from exc
        if snapshot.payload != payload:
            raise Stage3AcquisitionError("existing v4r7 acquisition completion differs")
        return False
    if not publish:
        raise Stage3AcquisitionError("v4r7 acquisition completion is missing")

    def validate() -> None:
        live = load_contract(context.path)
        _assert_decision_unchanged(live)
        if _output_state(live) != "complete":
            raise Stage3AcquisitionError("Stage3 result changed during completion publication")
        if _completion_value(live) != expected:
            raise Stage3AcquisitionError("Stage3 result bytes changed during completion publication")

    try:
        return prior_builder._publish_no_replace(
            path,
            payload,
            post_publish_validate=validate,
        )
    except Exception as exc:
        raise Stage3AcquisitionError(f"cannot publish v4r7 acquisition completion: {exc}") from exc


def _audit_process_authority(context: AcquisitionContext, *, execute: bool) -> None:
    expected = context.runner_execute_argv if execute else context.runner_dry_argv
    observed_raw = getattr(sys, "orig_argv", None)
    if not isinstance(observed_raw, list) or not observed_raw:
        observed_raw = [sys.executable, *sys.argv]
    observed = tuple(str(item) for item in observed_raw)
    if observed != expected:
        raise Stage3AcquisitionError(
            "live runner argv differs from the sealed acquisition contract: "
            f"expected={list(expected)!r} actual={list(observed)!r}"
        )


def dry_run(context: AcquisitionContext) -> dict[str, Any]:
    _audit_process_authority(context, execute=False)
    _assert_decision_unchanged(context)
    state = _output_state(context)
    if state == "complete":
        action = "verify_acquisition_complete"
    else:
        action = "resume_acquisition_only"
    return {
        "schema_version": RUN_REPORT_SCHEMA_VERSION,
        "status": "planned",
        "mode": "dry-run",
        "action": action,
        "output_state": state,
        "project": context.project,
        "task_prefix": context.task_prefix,
        "project_active_cap": context.project_active_cap,
        "history_limit": context.history_limit,
        "scheduler_timeout_seconds": context.scheduler_timeout_seconds,
        "writes_performed": 0,
    }


def execute(
    context: AcquisitionContext,
    *,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    _audit_process_authority(context, execute=True)
    writes = 0
    with v3.ExecutionLock(context.shared_lock):
        live = load_contract(context.path)
        if live.snapshot.sha256 != context.snapshot.sha256:
            raise Stage3AcquisitionError("acquisition contract changed after lock acquisition")
        _assert_decision_unchanged(live)
        state = _output_state(live)
        if state == "absent":
            environment = dict(os.environ)
            environment.update(live.environment)
            with _frozen_authority(live):
                completed = runner(
                    list(live.campaign_argv),
                    cwd=live.root,
                    env=environment,
                    shell=False,
                    check=False,
                )
            if completed.returncode != 0:
                raise Stage3AcquisitionError(
                    f"Stage3 acquisition campaign returned {completed.returncode}"
                )
            _assert_decision_unchanged(live)
            if _output_state(live) != "complete":
                raise Stage3AcquisitionError(
                    "Stage3 acquisition campaign returned without an exact 300-case result"
                )
        writes += int(_audit_or_publish_completion(live, publish=True))
        final = load_contract(live.path)
        _assert_decision_unchanged(final)
        if _output_state(final) != "complete":
            raise Stage3AcquisitionError("Stage3 acquisition result changed after completion")
        _audit_or_publish_completion(final, publish=False)
        return {
            "schema_version": RUN_REPORT_SCHEMA_VERSION,
            "status": "acquisition_complete",
            "mode": "execute",
            "action": "acquisition_complete",
            "output_state": "complete",
            "project": final.project,
            "task_prefix": final.task_prefix,
            "project_active_cap": final.project_active_cap,
            "history_limit": final.history_limit,
            "scheduler_timeout_seconds": final.scheduler_timeout_seconds,
            "writes_performed": writes,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = load_contract(args.contract)
        report = execute(context) if args.execute else dry_run(context)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        Stage3AcquisitionError,
        contract_builder.Stage3AcquisitionBuildError,
        authority.TargetLoadAuthorityError,
        OSError,
    ) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
