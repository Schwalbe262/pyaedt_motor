"""Run only the sealed v4r8 Stage3 acquisition/collection maintenance path."""

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

import build_ipmsm_v2_stage3_acquisition_v4r8 as contract_builder
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


RUN_REPORT_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r8-run-v1"
COMPLETION_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r8-completion-v1"


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
    aedt_backend: str
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
    expected_inherited = {
        f"prior_acquisition_{name}"
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
    if set(inherited) != expected_inherited:
        raise Stage3AcquisitionError("prior v4r7 source closure changed")


def load_contract(path: str | Path) -> AcquisitionContext:
    try:
        snapshot, document = authority._strict_json_snapshot(
            path, "Stage3 v4r8 acquisition contract"
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
            "run_batch",
            "scheduler_job",
            "scheduler_task",
            "ppt_setup",
            "aedt_attach_client",
            "subprocess_runner",
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
            "run_batch",
            "scheduler_job",
            "scheduler_task",
            "ppt_setup",
            "aedt_attach_client",
            "subprocess_runner",
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
    expected_runtime_paths = {
        "run_batch": root / contract_builder.RUN_BATCH_FILENAME,
        "scheduler_job": root / contract_builder.SCHEDULER_JOB_FILENAME,
        "scheduler_task": root / contract_builder.SCHEDULER_TASK_FILENAME,
        "ppt_setup": root / "module" / contract_builder.PPT_SETUP_FILENAME,
        "aedt_attach_client": root
        / "module"
        / contract_builder.AEDT_ATTACH_CLIENT_FILENAME,
        "subprocess_runner": root / contract_builder.SUBPROCESS_RUN_FILENAME,
    }
    if any(
        source_snapshots[name].path != expected_path
        for name, expected_path in expected_runtime_paths.items()
    ):
        raise Stage3AcquisitionError("current pooled runtime source paths changed")

    prior_record = _mapping(acquisition["prior"], "acquisition.prior")
    try:
        prior_context, prior_audit, prior_snapshots = contract_builder._audit_prior_acquisition(
            Path(prior_record["acquisition_contract"]["path"])
        )
    except (KeyError, TypeError, contract_builder.Stage3AcquisitionBuildError) as exc:
        raise Stage3AcquisitionError(f"prior v4r7 acquisition replay failed: {exc}") from exc
    if prior_audit["binding"] != prior_record:
        raise Stage3AcquisitionError("prior v4r7 acquisition authority changed")
    if resolved["prior_snapshot"].path != prior_context.snapshot.path:
        raise Stage3AcquisitionError("build config points to another prior v4r7 contract")

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
            "aedt_backend",
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
    aedt_backend = str(execution["aedt_backend"])
    if aedt_backend not in contract_builder.AEDT_BACKENDS:
        raise Stage3AcquisitionError("acquisition AEDT backend changed")
    if int(execution["history_limit"]) != contract_builder.HISTORY_LIMIT:
        raise Stage3AcquisitionError("acquisition history limit changed")
    timeout = float(execution["scheduler_timeout_seconds"])
    if timeout < contract_builder.SCHEDULER_TIMEOUT_SECONDS:
        raise Stage3AcquisitionError("acquisition scheduler timeout is below 300 seconds")
    if int(execution["expected_rows"]) != contract_builder.EXPECTED_ROWS:
        raise Stage3AcquisitionError("acquisition expected row count changed")

    decision = prior_audit["decision"]
    base_argv = tuple(str(item) for item in prior_context.campaign_argv[3:])
    campaign_base_argv = contract_builder._replace_flag_value(
        base_argv,
        "--project-active-cap",
        str(contract_builder.PROJECT_ACTIVE_CAP),
        "Stage3 campaign",
    )
    expected_campaign = (
        str(source_snapshots["runner_executable"].path),
        "-B",
        str(source_snapshots["campaign"].path),
        *campaign_base_argv,
        "--aedt-backend",
        aedt_backend,
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
        project != prior_context.project
        or scheduler_url != prior_context.scheduler_url
        or task_prefix != prior_context.task_prefix
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
    if plan_snapshot.path != prior_context.plan or int(plan_record["rows"]) != contract_builder.EXPECTED_ROWS:
        raise Stage3AcquisitionError("acquisition plan binding changed")

    outputs_raw = _mapping(acquisition["outputs"], "acquisition.outputs")
    _expect_keys(
        outputs_raw,
        {
            "campaign_output_dir",
            "merged_result",
            "campaign_summary",
            "campaign_decision",
            "completion",
        },
        "acquisition.outputs",
    )
    output_dir_from_contract = _path(
        outputs_raw["campaign_output_dir"], "acquisition output campaign_output_dir"
    )
    completion_from_contract = _path(
        outputs_raw["completion"], "acquisition output completion"
    )
    summary_from_contract = Path(str(outputs_raw["campaign_summary"])).absolute()
    campaign_decision_from_contract = Path(str(outputs_raw["campaign_decision"])).absolute()
    merged_from_contract = Path(str(outputs_raw["merged_result"])).absolute()
    try:
        merged_from_contract = authority._require_c_local(
            merged_from_contract, "acquisition output merged_result"
        )
        summary_from_contract = authority._require_c_local(
            summary_from_contract, "acquisition output campaign_summary"
        )
        campaign_decision_from_contract = authority._require_c_local(
            campaign_decision_from_contract, "acquisition output campaign_decision"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc
    outputs = {
        "campaign_output_dir": output_dir_from_contract,
        "merged_result": merged_from_contract,
        "campaign_summary": summary_from_contract,
        "campaign_decision": campaign_decision_from_contract,
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
        "campaign_summary": output_dir / contract_builder.CAMPAIGN_SUMMARY_FILENAME,
        "campaign_decision": output_dir / contract_builder.CAMPAIGN_DECISION_FILENAME,
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
        aedt_backend=aedt_backend,
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
        raise Stage3AcquisitionError("v4r8 authority freeze requires C-native Windows")
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
    successful_plan_path = output_dir / sealed_collector.SUCCESSFUL_PLAN_NAME
    results_dir = output_dir / "results"
    try:
        authority._directory_identity(output_dir, "Stage3 acquisition output directory")
        authority._directory_identity(results_dir, "Stage3 acquisition results directory")
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc

    summary_snapshot = _live_snapshot(
        context.outputs["campaign_summary"],
        "Stage3 campaign summary",
    )
    campaign_decision_snapshot = _live_snapshot(
        context.outputs["campaign_decision"],
        "Stage3 campaign decision",
    )
    try:
        _, summary = authority._strict_json_snapshot(
            summary_snapshot.path,
            "Stage3 campaign summary",
        )
        _, campaign_decision = authority._strict_json_snapshot(
            campaign_decision_snapshot.path,
            "Stage3 campaign decision",
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionError(str(exc)) from exc
    _expect_keys(
        summary,
        {
            "schema_version",
            "status",
            "project",
            "history_rows",
            "history_campaign_tasks",
            "selected_cases",
            "successful_cases",
            "permanently_failed_cases",
            "selected_plan",
            "successful_plan",
            "merged_output",
            "output_dir",
            "cases",
            "permanent_failures",
        },
        "Stage3 campaign summary",
    )
    _expect_keys(
        campaign_decision,
        {
            "schema_version",
            "status",
            "selected_cases",
            "successful_cases",
            "permanently_failed_cases",
            "summary",
            "permanent_failures",
        },
        "Stage3 campaign decision",
    )
    if summary["schema_version"] != sealed_collector.CAMPAIGN_SUMMARY_SCHEMA_VERSION:
        raise Stage3AcquisitionError("Stage3 campaign summary schema changed")
    if campaign_decision["schema_version"] != sealed_collector.CAMPAIGN_DECISION_SCHEMA_VERSION:
        raise Stage3AcquisitionError("Stage3 campaign decision schema changed")
    status = str(summary["status"])
    if status not in {"complete", "completed_with_permanent_failures"}:
        raise Stage3AcquisitionError("Stage3 campaign has not reached a sealed terminal status")
    if campaign_decision["status"] != status:
        raise Stage3AcquisitionError("Stage3 campaign terminal decisions disagree")
    selected_count = int(summary["selected_cases"])
    successful_count = int(summary["successful_cases"])
    failed_count = int(summary["permanently_failed_cases"])
    if (
        selected_count != context.expected_rows
        or successful_count < 0
        or failed_count < 0
        or successful_count + failed_count != selected_count
    ):
        raise Stage3AcquisitionError("Stage3 campaign terminal counts changed")
    if (status == "complete") != (successful_count == context.expected_rows and failed_count == 0):
        raise Stage3AcquisitionError("Stage3 campaign terminal status/counts disagree")
    if any(
        campaign_decision[name] != summary[name]
        for name in (
            "selected_cases",
            "successful_cases",
            "permanently_failed_cases",
            "permanent_failures",
        )
    ):
        raise Stage3AcquisitionError("Stage3 campaign decision payload changed")
    decision_summary = _mapping(
        campaign_decision["summary"],
        "Stage3 campaign decision summary binding",
    )
    if (
        set(decision_summary) != {"path", "sha256"}
        or str(decision_summary["sha256"]) != summary_snapshot.sha256
    ):
        raise Stage3AcquisitionError("Stage3 campaign decision summary binding changed")

    def resolved_output_path(value: Any, label: str) -> Path:
        raw = Path(str(value))
        return _path(raw if raw.is_absolute() else context.root / raw, label)

    if resolved_output_path(
        decision_summary["path"],
        "campaign decision summary path",
    ) != context.outputs["campaign_summary"]:
        raise Stage3AcquisitionError("Stage3 campaign decision summary path changed")

    if (
        summary["project"] != context.project
        or resolved_output_path(summary["output_dir"], "campaign summary output_dir")
        != output_dir
        or resolved_output_path(summary["selected_plan"], "campaign summary selected_plan")
        != selected_plan_path
        or resolved_output_path(summary["successful_plan"], "campaign summary successful_plan")
        != successful_plan_path
        or resolved_output_path(summary["merged_output"], "campaign summary merged_output")
        != merged_path
    ):
        raise Stage3AcquisitionError("Stage3 campaign summary path identity changed")

    original_plan_snapshot = _live_snapshot(context.plan, "sealed Stage3 plan")
    selected_plan_snapshot = _live_snapshot(
        selected_plan_path, "collected Stage3 selected plan"
    )
    successful_plan_snapshot = _live_snapshot(
        successful_plan_path,
        "collected Stage3 successful plan",
    )
    original_headers, original_rows = _read_csv_snapshot(
        original_plan_snapshot, "sealed Stage3 plan"
    )
    selected_headers, selected_rows = _read_csv_snapshot(
        selected_plan_snapshot, "collected Stage3 selected plan"
    )
    successful_headers, successful_rows = _read_csv_snapshot(
        successful_plan_snapshot, "collected Stage3 successful plan"
    )
    if original_headers != selected_headers or original_rows != selected_rows:
        raise Stage3AcquisitionError("collected Stage3 selected plan differs from sealed plan")
    if len(original_rows) != context.expected_rows:
        raise Stage3AcquisitionError("collected Stage3 plan is not exactly 300 rows")
    try:
        case_ids = sealed_merger.unique_case_ids(original_rows, source=str(context.plan))
    except ValueError as exc:
        raise Stage3AcquisitionError(f"sealed Stage3 plan case IDs are invalid: {exc}") from exc

    case_records = summary["cases"]
    failures = summary["permanent_failures"]
    if not isinstance(case_records, list) or not isinstance(failures, list):
        raise Stage3AcquisitionError("Stage3 campaign terminal evidence is not a list")
    if [str(item.get("case_id") or "") for item in case_records if isinstance(item, Mapping)] != case_ids:
        raise Stage3AcquisitionError("Stage3 campaign case evidence order changed")
    if len(case_records) != context.expected_rows or any(
        not isinstance(item, Mapping) for item in case_records
    ):
        raise Stage3AcquisitionError("Stage3 campaign case evidence coverage changed")
    success_ids = [
        str(item["case_id"])
        for item in case_records
        if item.get("outcome") == "success"
    ]
    failed_ids = [
        str(item["case_id"])
        for item in case_records
        if item.get("outcome") == "permanent_failure"
    ]
    if (
        len(success_ids) != successful_count
        or len(failed_ids) != failed_count
        or len(success_ids) + len(failed_ids) != context.expected_rows
    ):
        raise Stage3AcquisitionError("Stage3 campaign case outcomes changed")
    if successful_headers != original_headers or successful_rows != [
        row for row in original_rows if str(row["case_id"]).strip() in set(success_ids)
    ]:
        raise Stage3AcquisitionError("collected Stage3 successful plan changed")

    expected_names: list[str] = []
    expected_by_case: dict[str, Path] = {}
    for case_id in success_ids:
        safe_case_id = sealed_submit.sanitize_case_id(case_id)
        result_path = results_dir / f"{safe_case_id}.csv"
        expected_names.append(result_path.name)
        expected_by_case[case_id] = result_path
    if len(set(expected_names)) != successful_count:
        raise Stage3AcquisitionError("Stage3 result filename identities collide")
    if sorted(path.name for path in results_dir.iterdir()) != sorted(expected_names):
        raise Stage3AcquisitionError("Stage3 per-case result file set is not exact")

    union_headers: list[str] = []
    merged_rows_expected: list[dict[str, str]] = []
    collected_rows: list[dict[str, str]] = []
    result_records: list[dict[str, str]] = []
    plan_by_case = {str(row["case_id"]).strip(): row for row in original_rows}
    for case_id in success_ids:
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
    if collected_rows:
        try:
            sealed_collector.validate_homogeneous_fingerprints(collected_rows)
        except Exception as exc:
            raise Stage3AcquisitionError(f"Stage3 result fingerprints changed: {exc}") from exc

    merged_snapshot = _live_snapshot(merged_path, "Stage3 acquired merged result")
    merged_headers, merged_rows = _read_csv_snapshot(
        merged_snapshot, "Stage3 acquired merged result"
    )
    expected_merged_headers = union_headers or ["case_id", "status"]
    if merged_headers != expected_merged_headers or merged_rows != merged_rows_expected:
        raise Stage3AcquisitionError(
            "Stage3 merged result is not the exact ordered merge of per-case results"
        )
    failure_by_case = {
        str(item.get("case_id") or ""): item
        for item in failures
        if isinstance(item, Mapping)
    }
    if set(failure_by_case) != set(failed_ids) or len(failure_by_case) != failed_count:
        raise Stage3AcquisitionError("Stage3 permanent failure coverage changed")
    expected_failed_files: set[str] = set()
    preserved_failures: list[dict[str, Any]] = []
    retry_limit = int(
        context.campaign_argv[context.campaign_argv.index("--terminal-retry-limit") + 1]
    )
    for case_id in failed_ids:
        failure = _mapping(failure_by_case[case_id], f"permanent failure {case_id}")
        evidence = failure.get("failure_evidence")
        if (
            int(failure.get("attempts", -1)) <= retry_limit
            or not isinstance(evidence, list)
            or len(evidence) != int(failure["attempts"])
        ):
            raise Stage3AcquisitionError(f"permanent failure retry evidence changed: {case_id}")
        for item in evidence:
            if not isinstance(item, Mapping):
                raise Stage3AcquisitionError(f"permanent failure evidence changed: {case_id}")
            local_result = item.get("local_result")
            if local_result is None:
                continue
            local_path = resolved_output_path(
                local_result,
                f"permanent failure local result {case_id}",
            )
            expected_parent = output_dir / sealed_collector.FAILED_RESULTS_DIR_NAME
            if local_path.parent != expected_parent:
                raise Stage3AcquisitionError("permanent failure evidence path escaped output")
            failed_snapshot = _live_snapshot(local_path, f"failed Stage3 result {case_id}")
            if failed_snapshot.sha256 != str(item.get("local_result_sha256") or ""):
                raise Stage3AcquisitionError("permanent failure evidence hash changed")
            _, failed_rows = _read_csv_snapshot(failed_snapshot, f"failed Stage3 result {case_id}")
            if (
                len(failed_rows) != 1
                or str(failed_rows[0].get("case_id") or "").strip() != case_id
                or str(failed_rows[0].get("status") or "").strip().lower() != "failed"
            ):
                raise Stage3AcquisitionError("preserved failure result row changed")
            expected_failed_files.add(local_path.name)
        preserved_failures.append(dict(failure))
    for record_raw in case_records:
        record = _mapping(record_raw, "successful case evidence")
        if record.get("outcome") != "success":
            continue
        case_id = str(record["case_id"])
        evidence = record.get("attempt_evidence")
        if (
            not isinstance(evidence, list)
            or len(evidence) != int(record.get("attempts", -1))
        ):
            raise Stage3AcquisitionError(f"successful retry evidence changed: {case_id}")
        for item in evidence:
            if not isinstance(item, Mapping) or "local_result_sha256" not in item:
                continue
            local_path = resolved_output_path(
                item.get("local_result"),
                f"recovered failure local result {case_id}",
            )
            expected_parent = output_dir / sealed_collector.FAILED_RESULTS_DIR_NAME
            if local_path.parent != expected_parent:
                raise Stage3AcquisitionError("recovered failure evidence path escaped output")
            failed_snapshot = _live_snapshot(local_path, f"recovered failed result {case_id}")
            if failed_snapshot.sha256 != str(item["local_result_sha256"]):
                raise Stage3AcquisitionError("recovered failure evidence hash changed")
            _, failed_rows = _read_csv_snapshot(
                failed_snapshot,
                f"recovered failed result {case_id}",
            )
            if (
                len(failed_rows) != 1
                or str(failed_rows[0].get("case_id") or "").strip() != case_id
                or str(failed_rows[0].get("status") or "").strip().lower() != "failed"
            ):
                raise Stage3AcquisitionError("recovered failed result row changed")
            expected_failed_files.add(local_path.name)
    failed_results_dir = output_dir / sealed_collector.FAILED_RESULTS_DIR_NAME
    if expected_failed_files:
        try:
            authority._directory_identity(failed_results_dir, "failed Stage3 results directory")
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3AcquisitionError(str(exc)) from exc
        if {path.name for path in failed_results_dir.iterdir()} != expected_failed_files:
            raise Stage3AcquisitionError("preserved failure result file set changed")
    elif failed_results_dir.exists():
        raise Stage3AcquisitionError("unexpected failed Stage3 results directory")
    expected_top_level = {
        sealed_collector.SELECTED_PLAN_NAME,
        sealed_collector.SUCCESSFUL_PLAN_NAME,
        merged_path.name,
        "results",
        sealed_collector.CAMPAIGN_SUMMARY_NAME,
        sealed_collector.CAMPAIGN_DECISION_NAME,
    }
    if expected_failed_files:
        expected_top_level.add(sealed_collector.FAILED_RESULTS_DIR_NAME)
    if {path.name for path in output_dir.iterdir()} != expected_top_level:
        raise Stage3AcquisitionError("Stage3 acquisition output directory contents changed")
    return {
        "status": status,
        "selected_plan": {
            "path": str(selected_plan_snapshot.path),
            "sha256": selected_plan_snapshot.sha256,
        },
        "successful_plan": {
            "path": str(successful_plan_snapshot.path),
            "sha256": successful_plan_snapshot.sha256,
        },
        "merged_result": {
            "path": str(merged_snapshot.path),
            "sha256": merged_snapshot.sha256,
        },
        "result_count": len(result_records),
        "successful_case_ids": success_ids,
        "failed_case_ids": failed_ids,
        "case_records": [dict(item) for item in case_records],
        "permanently_failed_count": failed_count,
        "permanent_failures": preserved_failures,
        "campaign_summary": {
            "path": str(summary_snapshot.path),
            "sha256": summary_snapshot.sha256,
        },
        "campaign_decision": {
            "path": str(campaign_decision_snapshot.path),
            "sha256": campaign_decision_snapshot.sha256,
        },
        "result_set_sha256": authority.canonical_sha256({"results": result_records}),
    }


def _audit_scheduler_provenance(
    context: AcquisitionContext,
    output_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    provenance = (
        dict(output_provenance)
        if output_provenance is not None
        else _audit_output_provenance(context)
    )
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
        lineages = sealed_submit.build_campaign_task_lineages(
            args,
            selected_rows,
            first_row_number=args.case_start_index,
            terminal_retry_limit=args.terminal_retry_limit,
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
    expected_by_dedupe = {
        attempt.dedupe_key: attempt
        for task in campaign_tasks
        for attempt in lineages[task.dedupe_key]
    }
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
    if any(
        expected_by_dedupe[dedupe_key].retry_index > 0 and len(items) > 1
        for dedupe_key, items in attempts_by_dedupe.items()
    ):
        raise Stage3AcquisitionError("scheduler provenance duplicated a fresh retry identity")
    history_by_id = {
        sealed_campaign._task_id(item): item
        for items in attempts_by_dedupe.values()
        for item in items
    }
    for item in history:
        status = str(item.get("status") or "").strip().lower()
        if status not in sealed_campaign.KNOWN_STATUSES:
            raise Stage3AcquisitionError("scheduler provenance has an unknown status")
        if status in sealed_campaign.ACTIVE_STATUSES:
            raise Stage3AcquisitionError("scheduler provenance remains active")

    case_records = provenance["case_records"]
    if not isinstance(case_records, list):
        raise Stage3AcquisitionError("scheduler case evidence changed")
    audited_attempts: list[dict[str, Any]] = []
    for base_task, record_raw in zip(campaign_tasks, case_records, strict=True):
        record = _mapping(record_raw, f"scheduler case evidence {base_task.case_id}")
        if str(record.get("case_id") or "") != base_task.case_id:
            raise Stage3AcquisitionError("scheduler case evidence order changed")
        outcome = str(record.get("outcome") or "")
        evidence_name = "attempt_evidence" if outcome == "success" else "failure_evidence"
        evidence = record.get(evidence_name)
        attempts = int(record.get("attempts", -1))
        observed_lineage = [
            item
            for attempt in lineages[base_task.dedupe_key]
            for item in attempts_by_dedupe[attempt.dedupe_key]
        ]
        if (
            outcome not in {"success", "permanent_failure"}
            or not isinstance(evidence, list)
            or not evidence
            or attempts != len(observed_lineage)
            or not 1 <= attempts <= args.terminal_retry_limit + 1
        ):
            raise Stage3AcquisitionError(
                f"scheduler terminal attempt coverage changed: {base_task.case_id}"
            )
        if outcome == "permanent_failure" and attempts <= args.terminal_retry_limit:
            raise Stage3AcquisitionError("scheduler permanent failure is not retry-exhausted")
        for evidence_raw in evidence:
            item = _mapping(evidence_raw, f"scheduler evidence {base_task.case_id}")
            dedupe_key = str(item.get("dedupe_key") or "")
            expected_attempt = expected_by_dedupe.get(dedupe_key)
            task_id = item.get("task_id")
            history_task = history_by_id.get(task_id)
            if (
                expected_attempt is None
                or expected_attempt.case_id != base_task.case_id
                or int(item.get("retry_index", -1)) != expected_attempt.retry_index
                or str(item.get("remote_result") or "") != expected_attempt.result_csv
                or history_task is None
                or str(history_task.get("dedupe_key") or "") != dedupe_key
            ):
                raise Stage3AcquisitionError(
                    f"scheduler attempt evidence changed: {base_task.case_id}"
                )
            scheduler_status = str(history_task.get("status") or "").strip().lower()
            if str(item.get("scheduler_status") or "").strip().lower() != scheduler_status:
                raise Stage3AcquisitionError("scheduler evidence status changed")
            kind = str(item.get("kind") or "")
            if kind == "scheduler_terminal":
                if scheduler_status not in sealed_campaign.TERMINAL_RETRY_STATUSES:
                    raise Stage3AcquisitionError("scheduler terminal evidence changed")
            elif kind in {"result", "result_level_terminal"}:
                if scheduler_status != "completed" or sealed_campaign._exit_code(history_task) != 0:
                    raise Stage3AcquisitionError("scheduler result evidence changed")
                if kind == "result" and outcome != "success":
                    raise Stage3AcquisitionError("scheduler result evidence kind changed")
                expected_result = "ok" if kind == "result" else "failed"
                if str(item.get("result_status") or "").strip().lower() != expected_result:
                    raise Stage3AcquisitionError("scheduler result outcome changed")
            else:
                raise Stage3AcquisitionError("scheduler evidence kind changed")
            audited_attempts.append(
                {
                    "case_id": base_task.case_id,
                    "dedupe_key": dedupe_key,
                    "task_id": task_id,
                    "outcome": outcome,
                }
            )
        result_kinds = [str(item.get("kind") or "") for item in evidence]
        if outcome == "success" and (
            result_kinds.count("result") != 1 or result_kinds[-1] != "result"
        ):
            raise Stage3AcquisitionError("scheduler successful result evidence changed")
        if outcome == "permanent_failure" and "result" in result_kinds:
            raise Stage3AcquisitionError("scheduler permanent failure result evidence changed")
    return {
        "history_count": len(history),
        "terminal_status": provenance["status"],
        "successful_case_count": len(provenance["successful_case_ids"]),
        "permanently_failed_case_count": len(provenance["failed_case_ids"]),
        "audited_attempt_count": len(audited_attempts),
        "audited_attempt_set_sha256": authority.canonical_sha256(
            {"tasks": audited_attempts}
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
    provenance = _audit_output_provenance(context)
    return str(provenance["status"])


def _completion_value(context: AcquisitionContext) -> dict[str, Any]:
    provenance = _audit_output_provenance(context)
    scheduler_provenance = _audit_scheduler_provenance(context, provenance)
    return {
        "schema_version": COMPLETION_SCHEMA_VERSION,
        "status": provenance["status"],
        "contract": _contract_record(context),
        "prior_acquisition_contract": dict(context.prior["acquisition_contract"]),
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
            snapshot = authority.read_single_link_snapshot(path, "v4r8 acquisition completion")
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3AcquisitionError(str(exc)) from exc
        if snapshot.payload != payload:
            raise Stage3AcquisitionError("existing v4r8 acquisition completion differs")
        return False
    if not publish:
        raise Stage3AcquisitionError("v4r8 acquisition completion is missing")

    def validate() -> None:
        live = load_contract(context.path)
        _assert_decision_unchanged(live)
        if _output_state(live) != expected["status"]:
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
        raise Stage3AcquisitionError(f"cannot publish v4r8 acquisition completion: {exc}") from exc


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
    elif state == "completed_with_permanent_failures":
        action = "verify_acquisition_permanent_failures"
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
        "aedt_backend": context.aedt_backend,
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
            state = _output_state(live)
            if state not in {"complete", "completed_with_permanent_failures"}:
                raise Stage3AcquisitionError(
                    "Stage3 acquisition campaign returned without a terminal 300-case decision"
                )
        writes += int(_audit_or_publish_completion(live, publish=True))
        final = load_contract(live.path)
        _assert_decision_unchanged(final)
        final_state = _output_state(final)
        if final_state not in {"complete", "completed_with_permanent_failures"}:
            raise Stage3AcquisitionError("Stage3 acquisition result changed after completion")
        _audit_or_publish_completion(final, publish=False)
        final_provenance = _audit_output_provenance(final)
        acquisition_complete = final_state == "complete"
        return {
            "schema_version": RUN_REPORT_SCHEMA_VERSION,
            "status": (
                "acquisition_complete"
                if acquisition_complete
                else "acquisition_completed_with_permanent_failures"
            ),
            "mode": "execute",
            "action": (
                "acquisition_complete"
                if acquisition_complete
                else "record_permanent_failures"
            ),
            "output_state": final_state,
            "successful_cases": final_provenance["result_count"],
            "permanently_failed_cases": final_provenance["permanently_failed_count"],
            "project": final.project,
            "task_prefix": final.task_prefix,
            "project_active_cap": final.project_active_cap,
            "aedt_backend": final.aedt_backend,
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
