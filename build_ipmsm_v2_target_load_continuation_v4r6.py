"""Build the sealed production v4r6 target-load continuation contract.

The builder accepts only deployment/runtime values.  Candidate identity,
completed optimization evidence, target-load semantics, PyAEDT bytes, and
human authority are derived from and replayed against the immutable v4r6
authority.  Build mode is read-only; ``--execute`` publishes no-replace.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import atomic_publish
import build_ipmsm_v2_target_load_authority_v4r6 as authority_builder
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import continue_ipmsm_v2_target_load_v4r6 as continuation
import ipmsm_target_load_coordinator as coordinator
import ipmsm_target_load_matching as matching
import ipmsm_target_load_workflow as workflow


CONFIG_SCHEMA_VERSION = "ipmsm-v2-target-load-continuation-build-config-v1"
CONTRACT_FILENAME = "target_load_continuation_contract.json"
WORKSPACE_NAME = "target_load_workspace"
DECISION_FILENAME = "target_load_continuation_decision.json"
COMPLETION_FILENAME = "target_load_completion.json"
MEASURED_FRONT_FILENAME = "measured_target_load_front.csv"
MEASURED_FRONT_MANIFEST_FILENAME = "measured_target_load_front.manifest.json"


class TargetLoadContinuationBuildError(ValueError):
    """A production continuation contract cannot be proven exactly."""


@dataclass(frozen=True)
class BuiltContinuationContract:
    document: Mapping[str, Any]
    output: Path
    publication_snapshots: tuple[authority.FileSnapshot, ...]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TargetLoadContinuationBuildError(f"{label} must be an object")
    return value


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise TargetLoadContinuationBuildError(
            f"{label} fields mismatch: missing={sorted(expected - set(value))} "
            f"extra={sorted(set(value) - expected)}"
        )


def _path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TargetLoadContinuationBuildError(f"{label} must be an exact nonblank path")
    path = Path(value)
    if not path.is_absolute():
        raise TargetLoadContinuationBuildError(f"{label} must be absolute")
    try:
        return authority._require_c_local(path.absolute(), label)
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationBuildError(str(exc)) from exc


def _strict_snapshot(path: Path, label: str) -> authority.FileSnapshot:
    try:
        return authority.read_single_link_snapshot(path, label)
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationBuildError(str(exc)) from exc


def _read_config(path: Path) -> tuple[authority.FileSnapshot, dict[str, Any]]:
    try:
        snapshot, config = authority._strict_json_snapshot(path, "continuation build config")
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationBuildError(str(exc)) from exc
    _expect_keys(
        config,
        {
            "schema_version",
            "v4r6_authority_contract",
            "pyaedt_core_snapshot",
            "output_root",
            "scheduler",
            "runtime",
        },
        "continuation build config",
    )
    if config["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise TargetLoadContinuationBuildError("unsupported continuation build config schema")
    return snapshot, config


def _unique_snapshots(
    values: Sequence[authority.FileSnapshot],
) -> tuple[authority.FileSnapshot, ...]:
    result: list[authority.FileSnapshot] = []
    seen: set[str] = set()
    for snapshot in values:
        key = str(snapshot.path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(snapshot)
    return tuple(result)


def _path_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
        return True
    except ValueError:
        return False


def _output_paths(output_root: Path) -> dict[str, Path]:
    try:
        authority._directory_identity(output_root, "continuation output root")
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationBuildError(str(exc)) from exc
    workspace = output_root / WORKSPACE_NAME
    decision = output_root / DECISION_FILENAME
    raw = {
        "workspace": str(workspace),
        "decision": str(decision),
        "claim": str(decision.with_name(decision.name + ".claim")),
        "recovery": str(decision.with_name(decision.name + ".claim.recover")),
        "progress": str(workspace / "progress.json"),
        "completion": str(workspace / COMPLETION_FILENAME),
        "measured_front_csv": str(workspace / MEASURED_FRONT_FILENAME),
        "measured_front_manifest": str(workspace / MEASURED_FRONT_MANIFEST_FILENAME),
    }
    try:
        return continuation._validate_paths(raw)
    except continuation.TargetLoadContinuationError as exc:
        raise TargetLoadContinuationBuildError(str(exc)) from exc


def _scheduler(config: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping(config.get("scheduler"), "scheduler")
    _expect_keys(
        raw,
        {
            "url",
            "project",
            "project_id",
            "remote_root",
            "cpus",
            "cores_per_process",
            "memory_mb",
            "task_timeout_seconds",
            "request_timeout_seconds",
            "history_limit",
        },
        "scheduler",
    )
    policy = _mapping(target["scheduler"], "target_load.scheduler")
    scheduler = {
        "url": raw["url"],
        "project": raw["project"],
        "project_id": raw["project_id"],
        "project_active_cap": policy["project_active_cap"],
        "endpoint": policy["endpoint"],
        "scheduling_profile": policy["scheduling_profile"],
        "required_capability": policy["required_capability"],
        "env_profile": policy["env_profile"],
        "env_setup": policy["env_setup"],
        "partition": "auto",
        "max_workers_per_node": policy["max_workers_per_node"],
        "remote_root": raw["remote_root"],
        "entrypoint": "subprocess_run.py",
        "cpus": raw["cpus"],
        "cores_per_process": raw["cores_per_process"],
        "memory_mb": raw["memory_mb"],
        "task_timeout_seconds": raw["task_timeout_seconds"],
        "request_timeout_seconds": raw["request_timeout_seconds"],
        "history_limit": raw["history_limit"],
    }
    try:
        continuation._validate_scheduler(scheduler, target)
        workflow._validate_scheduler_contract(
            {
                key: value
                for key, value in scheduler.items()
                if key not in {"url", "project_active_cap", "request_timeout_seconds", "history_limit"}
            }
            | {"server_cap": scheduler["project_active_cap"]}
        )
    except (continuation.TargetLoadContinuationError, workflow.TargetLoadWorkflowError) as exc:
        raise TargetLoadContinuationBuildError(str(exc)) from exc
    return scheduler


def _runtime(config: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping(config.get("runtime"), "runtime")
    _expect_keys(
        raw,
        {
            "task_retry_limit",
            "result_identity_relative_tolerance",
            "poll_interval_seconds",
            "overall_timeout_seconds",
        },
        "runtime",
    )
    try:
        return continuation._validate_runtime(dict(raw), target)
    except continuation.TargetLoadContinuationError as exc:
        raise TargetLoadContinuationBuildError(str(exc)) from exc


def _derived_upstream(
    target: Mapping[str, Any],
    audited: authority_builder.CompletedUpstreamAudit,
) -> dict[str, Any]:
    target_upstream = _mapping(target["upstream_authority"], "target upstream authority")
    per_case = _mapping(target["upstream_results"], "target upstream results")
    expected = {
        "binding_schema_version": target_upstream["binding_schema_version"],
        "binding_hash_algorithm": target_upstream["binding_hash_algorithm"],
        "upstream_binding_sha256": target_upstream["upstream_binding_sha256"],
        "filtered_plan_sha256": target_upstream["filtered_plan_sha256"],
        "selected_candidate_ids": target_upstream["selected_candidate_ids"],
        "upstream_artifacts_manifest_sha256": target_upstream[
            "upstream_artifacts_manifest_sha256"
        ],
        "per_case_results_manifest_sha256": per_case[
            "per_case_results_manifest_sha256"
        ],
    }
    artifact_sha = authority.canonical_sha256(
        {
            "schema_version": authority.UPSTREAM_ARTIFACTS_MANIFEST_SCHEMA_VERSION,
            "artifacts": list(audited.upstream_artifacts_manifest),
        }
    )
    result_sha = authority.canonical_sha256(
        {
            "schema_version": authority.PER_CASE_RESULTS_MANIFEST_SCHEMA_VERSION,
            "results": list(audited.per_case_results_manifest),
        }
    )
    actual = {
        "binding_schema_version": authority.UPSTREAM_BINDING_SCHEMA_VERSION,
        "binding_hash_algorithm": authority.UPSTREAM_BINDING_HASH_ALGORITHM,
        "upstream_binding_sha256": audited.upstream_binding_sha256,
        "filtered_plan_sha256": audited.filtered_plan_sha256,
        "selected_candidate_ids": list(audited.candidate_ids),
        "upstream_artifacts_manifest_sha256": artifact_sha,
        "per_case_results_manifest_sha256": result_sha,
    }
    if actual != expected:
        raise TargetLoadContinuationBuildError(
            "completed optimization replay differs from the human-authorized upstream binding"
        )
    return actual


def _authority_record(
    context: authority.TargetLoadAuthorityContext,
    receipt: authority.AuthorizationAudit,
) -> dict[str, Any]:
    return {
        "base_v4r5_contract": dict(context.base_v4r5_binding),
        "v4r6_contract": dict(context.contract_binding),
        "authorization_receipt": {
            "path": str(receipt.path),
            "raw_sha256": receipt.file_sha256,
            "receipt_sha256": receipt.receipt_sha256,
            "confirmation_sha256": receipt.confirmation_sha256,
            "contract_sha256": receipt.contract_sha256,
        },
    }


def build_contract(config_path: Path) -> BuiltContinuationContract:
    config_snapshot, config = _read_config(config_path)
    v6_path = _path(config["v4r6_authority_contract"], "v4r6_authority_contract")
    try:
        authority_context = authority.load_authority_context(v6_path)
        receipt = authority.audit_authorization_receipt(v6_path)
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationBuildError(
            f"completed human target-load authority is required: {exc}"
        ) from exc
    try:
        upstream = authority_builder.audit_completed_upstream(
            authority_context.base_v4r5_contract.path
        )
    except authority_builder.TargetLoadAuthorityBuildError as exc:
        raise TargetLoadContinuationBuildError(
            f"completed authorized Stage3/optimization artifacts are required: {exc}"
        ) from exc
    derived = _derived_upstream(authority_context.target_load, upstream)

    pyaedt = _mapping(config["pyaedt_core_snapshot"], "pyaedt_core_snapshot")
    _expect_keys(pyaedt, {"path", "sha256"}, "pyaedt_core_snapshot")
    pyaedt_path = _path(pyaedt["path"], "pyaedt_core_snapshot.path")
    pyaedt_snapshot = _strict_snapshot(pyaedt_path, "explicit PyAEDT core source")
    if (
        pyaedt_snapshot.path != authority_context.pyaedt_core_snapshot.path
        or pyaedt_snapshot.sha256
        != authority._sha256(pyaedt["sha256"], "pyaedt_core_snapshot.sha256")
        or pyaedt_snapshot.payload != authority_context.pyaedt_core_snapshot.payload
    ):
        raise TargetLoadContinuationBuildError(
            "explicit PyAEDT bytes differ from the v4r6 human authority"
        )

    output_root_lexical = _path(config["output_root"], "output_root")
    try:
        authority._directory_identity(output_root_lexical, "continuation output root")
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationBuildError(str(exc)) from exc
    output_root = output_root_lexical.resolve(strict=True)
    output = output_root / CONTRACT_FILENAME
    paths = _output_paths(output_root)
    immutable_paths = {
        item.path.resolve(strict=True)
        for item in (
            *authority_context.bound_snapshots,
            authority_context.contract,
        )
    }
    immutable_paths.update(
        {
            authority_context.declaration_path.resolve(strict=True),
            authority_context.confirmation_path.resolve(strict=True),
            authority_context.authorization_receipt_path.resolve(strict=True),
            config_snapshot.path,
        }
    )
    if any(
        _path_within(path, output_root)
        for path in immutable_paths
    ):
        raise TargetLoadContinuationBuildError(
            "continuation output root overlaps immutable authority inputs"
        )
    for protected in authority_context.protected_input_directories:
        if _path_within(output_root, protected) or _path_within(protected, output_root):
            raise TargetLoadContinuationBuildError(
                "continuation output root overlaps a protected upstream directory"
            )
    for name, path in paths.items():
        if name != "workspace" and os.path.lexists(path):
            raise TargetLoadContinuationBuildError(f"fresh continuation path required: {name}")
    if os.path.lexists(paths["workspace"]):
        raise TargetLoadContinuationBuildError("fresh coordinator workspace is required")

    scheduler = _scheduler(config, authority_context.target_load)
    runtime = _runtime(config, authority_context.target_load)
    source_paths = {
        "continuation_adapter": Path(continuation.__file__).resolve(strict=True),
        "continuation_builder": Path(__file__).resolve(strict=True),
        "target_load_authority": Path(authority.__file__).resolve(strict=True),
        "target_load_authority_builder": Path(authority_builder.__file__).resolve(strict=True),
        "target_load_coordinator": Path(coordinator.__file__).resolve(strict=True),
        "target_load_workflow": Path(workflow.__file__).resolve(strict=True),
        "target_load_matching": Path(matching.__file__).resolve(strict=True),
        "atomic_publish": Path(atomic_publish.__file__).resolve(strict=True),
        "run_ipmsm_batch": workflow.RUNTIME_SOURCE_PATHS[
            "run_ipmsm_batch_source"
        ].resolve(strict=True),
        "submit_ipmsm_scheduler_job": workflow.RUNTIME_SOURCE_PATHS[
            "submit_ipmsm_scheduler_job_source"
        ].resolve(strict=True),
        "submit_ipmsm_scheduler_task": workflow.RUNTIME_SOURCE_PATHS[
            "submit_ipmsm_scheduler_task_source"
        ].resolve(strict=True),
        "submit_ipmsm_v2_campaign": workflow.RUNTIME_SOURCE_PATHS[
            "submit_ipmsm_v2_campaign_source"
        ].resolve(strict=True),
        "subprocess_run": workflow.RUNTIME_SOURCE_PATHS[
            "subprocess_run_source"
        ].resolve(strict=True),
        "pareto_validator": workflow.RUNTIME_SOURCE_PATHS[
            "validator_source"
        ].resolve(strict=True),
        "ipmsm_geometry": workflow.RUNTIME_SOURCE_PATHS[
            "ipmsm_geometry_source"
        ].resolve(strict=True),
        "ipmsm_ppt_setup": workflow.RUNTIME_SOURCE_PATHS[
            "ipmsm_ppt_setup_source"
        ].resolve(strict=True),
        "variable": workflow.RUNTIME_SOURCE_PATHS["variable_source"].resolve(strict=True),
        "pyaedt_core": pyaedt_snapshot.path,
    }
    if set(source_paths) != continuation.REQUIRED_SOURCE_PINS:
        raise TargetLoadContinuationBuildError("continuation source closure changed")
    if len({str(path).casefold() for path in source_paths.values()}) != len(source_paths):
        raise TargetLoadContinuationBuildError("continuation source paths must be unique")
    source_snapshots = {
        name: _strict_snapshot(path, f"continuation source {name}")
        for name, path in sorted(source_paths.items())
    }
    source_pins = {
        name: {"path": str(snapshot.path), "sha256": snapshot.sha256}
        for name, snapshot in source_snapshots.items()
    }
    executable = authority.read_single_link_snapshot(
        Path(sys.executable).resolve(strict=True),
        "continuation executable",
        require_single_link=False,
    )
    if any(
        _path_within(snapshot.path, output_root)
        for snapshot in (*source_snapshots.values(), executable)
    ):
        raise TargetLoadContinuationBuildError(
            "continuation output root contains a retained runtime source"
        )
    runner_argv = [
        str(executable.path),
        str(source_snapshots["continuation_adapter"].path),
        "--continuation-contract",
        str(output),
        "--execute",
    ]
    final_front = {
        "schema_version": continuation.FRONT_SCHEMA_VERSION,
        "objectives": list(continuation.FRONT_OBJECTIVES),
        "dominance_tolerance": continuation.FRONT_TOLERANCE,
        "deterministic_order": list(continuation.FRONT_ORDER),
    }
    unsigned = {
        "schema_version": continuation.SCHEMA_VERSION,
        "continuation": {
            "authority": _authority_record(authority_context, receipt),
            "runner": {
                "executable": {"path": str(executable.path), "sha256": executable.sha256},
                "source": {
                    "path": str(source_snapshots["continuation_adapter"].path),
                    "sha256": source_snapshots["continuation_adapter"].sha256,
                },
                "argv": runner_argv,
            },
            "paths": {name: str(path) for name, path in paths.items()},
            "source_pins": source_pins,
            "scheduler": scheduler,
            "runtime": runtime,
            "upstream_derived_binding": derived,
            "final_front": final_front,
        },
    }
    document = {
        **unsigned,
        "contract_sha256": authority.contract_logical_sha256(unsigned),
    }
    contract_payload = authority.canonical_json_bytes(document)
    expected_stage, expected_proof = authority._publication_paths(output, contract_payload)
    allowed_output_entries = {
        str(path).casefold() for path in (output, expected_stage, expected_proof)
    }
    unexpected_entries = [
        path
        for path in output_root.iterdir()
        if str(path).casefold() not in allowed_output_entries
    ]
    if unexpected_entries:
        raise TargetLoadContinuationBuildError(
            "continuation output root contains an unauthorized artifact: "
            f"{unexpected_entries[0]}"
        )
    declaration = _strict_snapshot(
        authority_context.declaration_path, "target-load declaration"
    )
    confirmation = _strict_snapshot(
        authority_context.confirmation_path, "target-load confirmation"
    )
    receipt_snapshot = _strict_snapshot(
        authority_context.authorization_receipt_path,
        "target-load authorization receipt",
    )
    publication_snapshots = _unique_snapshots(
        (
            config_snapshot,
            *authority_context.bound_snapshots,
            declaration,
            confirmation,
            receipt_snapshot,
            *upstream.snapshots,
            *source_snapshots.values(),
            executable,
        )
    )
    if str(output).casefold() in {
        str(snapshot.path).casefold() for snapshot in publication_snapshots
    }:
        raise TargetLoadContinuationBuildError("continuation output aliases an immutable input")
    try:
        authority.assert_context_unchanged(authority_context)
        if authority.audit_authorization_receipt(v6_path) != receipt:
            raise TargetLoadContinuationBuildError(
                "human target-load authorization changed during continuation build"
            )
        for index, snapshot in enumerate(publication_snapshots):
            authority.assert_snapshot_unchanged(
                snapshot, f"continuation build input {index}"
            )
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationBuildError(str(exc)) from exc
    return BuiltContinuationContract(document, output, publication_snapshots)


def _print(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(authority.canonical_json_bytes(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        built = build_contract(args.config)
        if not args.execute:
            _print(built.document)
            return 0
        rebuilt = build_contract(args.config)
        if rebuilt.document != built.document or rebuilt.output != built.output:
            raise TargetLoadContinuationBuildError(
                "continuation inputs changed before publication"
            )
        result = authority.publish_canonical_no_replace(
            rebuilt.document,
            rebuilt.output,
            additional_snapshots=rebuilt.publication_snapshots,
        )
        _print(
            {
                "status": result.outcome,
                "path": str(rebuilt.output),
                "contract_sha256": rebuilt.document["contract_sha256"],
                "writes_performed": result.writes_performed,
            }
        )
        return 0
    except (
        TargetLoadContinuationBuildError,
        authority.TargetLoadAuthorityError,
        authority_builder.TargetLoadAuthorityBuildError,
        continuation.TargetLoadContinuationError,
        workflow.TargetLoadWorkflowError,
        OSError,
        ValueError,
    ) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
