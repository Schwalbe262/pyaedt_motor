"""Contract-only continuation adapter for authorized v4r6 target-load FEA.

The adapter deliberately exposes no path, scheduler, candidate, or resume
override.  Dry-run is read-only.  ``--execute`` is accepted only when the
exact executable/source/argv and every upstream authority binding match the
immutable continuation contract.
"""

from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import socket
import stat
import sys
import time
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from urllib import parse
import uuid

import atomic_publish
import build_ipmsm_v2_target_load_authority_v4r6 as authority_builder
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import ipmsm_target_load_coordinator as coordinator
import ipmsm_target_load_matching as matching
import ipmsm_target_load_workflow as workflow
import supervise_ipmsm_v2_pipeline as v3
import supervise_ipmsm_v2_pipeline_v4 as v4


SCHEMA_VERSION = "ipmsm-v2-target-load-continuation-contract-v1"
DECISION_SCHEMA_VERSION = "ipmsm-v2-target-load-continuation-decision-v1"
CLAIM_SCHEMA_VERSION = "ipmsm-v2-target-load-continuation-claim-v1"
RECOVERY_SCHEMA_VERSION = "ipmsm-v2-target-load-continuation-recovery-v1"
FRONT_SCHEMA_VERSION = "ipmsm-v2-measured-target-load-pareto-front-v1"
FRONT_MANIFEST_SCHEMA_VERSION = "ipmsm-v2-measured-target-load-pareto-manifest-v1"
COMPLETION_SCHEMA_VERSION = "ipmsm-v2-target-load-continuation-completion-v1"
FRONT_TOLERANCE = 1.0e-12
FRONT_FIELDS = (
    "candidate_id",
    "objective_active_volume_m3",
    "objective_cycle_efficiency",
    "objective_one_minus_cycle_efficiency",
    "summary_sha256",
)
FRONT_OBJECTIVES = (
    {"field": "objective_active_volume_m3", "sense": "minimize"},
    {"field": "objective_one_minus_cycle_efficiency", "sense": "minimize"},
)
FRONT_ORDER = (
    "objective_active_volume_m3",
    "objective_one_minus_cycle_efficiency",
    "candidate_id",
)
REQUIRED_SOURCE_PINS = frozenset(
    {
        "continuation_adapter",
        "continuation_builder",
        "target_load_authority",
        "target_load_authority_builder",
        "target_load_coordinator",
        "target_load_workflow",
        "target_load_matching",
        "atomic_publish",
        "run_ipmsm_batch",
        "submit_ipmsm_scheduler_job",
        "submit_ipmsm_scheduler_task",
        "submit_ipmsm_v2_campaign",
        "subprocess_run",
        "pareto_validator",
        "ipmsm_geometry",
        "ipmsm_ppt_setup",
        "variable",
        "pyaedt_core",
    }
)


class TargetLoadContinuationError(RuntimeError):
    """Continuation evidence is incomplete, changed, or unsafe."""


@dataclass(frozen=True)
class ContinuationContext:
    path: Path
    snapshot: authority.FileSnapshot
    document: Mapping[str, Any]
    contract_sha256: str
    authority_context: authority.TargetLoadAuthorityContext
    authorization: authority.AuthorizationAudit
    paths: Mapping[str, Path]
    scheduler: Mapping[str, Any]
    runtime: Mapping[str, Any]
    runner_argv: tuple[str, ...]
    source_pins: Mapping[str, authority.FileSnapshot]
    runner_executable: authority.FileSnapshot
    runner_source: authority.FileSnapshot


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TargetLoadContinuationError(f"{label} must be an object")
    return value


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise TargetLoadContinuationError(
            f"{label} fields mismatch: missing={sorted(expected - set(value))} "
            f"extra={sorted(set(value) - expected)}"
        )


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TargetLoadContinuationError(f"{label} must be an exact nonblank string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TargetLoadContinuationError(f"{label} must be an integer >= {minimum}")
    return value


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise TargetLoadContinuationError(f"{label} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TargetLoadContinuationError(f"{label} must be finite") from exc
    if not math.isfinite(result) or (positive and result <= 0.0):
        raise TargetLoadContinuationError(f"{label} must be {'positive' if positive else 'finite'}")
    return result


def _sha256(value: Any, label: str) -> str:
    text = _nonblank(value, label).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise TargetLoadContinuationError(f"{label} must be lowercase SHA256")
    return text


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=True) == right.resolve(strict=True)
    except OSError:
        return left.resolve(strict=False) == right.resolve(strict=False)


def _c_path(value: Any, label: str, *, existing: bool = False) -> Path:
    raw = _nonblank(value, label)
    path = Path(raw)
    if not path.is_absolute():
        raise TargetLoadContinuationError(f"{label} must be absolute")
    try:
        path = authority._require_c_local(path.absolute(), label)
        if existing:
            path = path.resolve(strict=True)
    except (OSError, authority.TargetLoadAuthorityError) as exc:
        raise TargetLoadContinuationError(str(exc)) from exc
    return path


def _path_is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
        return True
    except ValueError:
        return False


def _strict_document(path: Path, label: str) -> tuple[authority.FileSnapshot, dict[str, Any]]:
    try:
        snapshot = authority.read_single_link_snapshot(path, label)
        value = json.loads(
            snapshot.payload.decode("utf-8"),
            object_pairs_hook=authority._unique_object,
            parse_constant=authority._reject_constant,
        )
    except (OSError, UnicodeError, ValueError, authority.TargetLoadAuthorityError) as exc:
        raise TargetLoadContinuationError(f"cannot read strict {label}: {exc}") from exc
    document = _mapping(value, label)
    if snapshot.payload != authority.canonical_json_bytes(document):
        raise TargetLoadContinuationError(f"{label} is not canonical JSON bytes")
    return snapshot, document


def _four_hash_binding(value: Any, label: str) -> tuple[dict[str, str], authority.FileSnapshot]:
    try:
        return authority._validate_four_hash_binding(value, label)
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationError(str(exc)) from exc


def _source_pins(
    value: Any,
    *,
    pyaedt_core_path: Path,
) -> dict[str, authority.FileSnapshot]:
    records = _mapping(value, "continuation.source_pins")
    if set(records) != REQUIRED_SOURCE_PINS:
        raise TargetLoadContinuationError("continuation source pin coverage is not exact")
    expected_paths = {
        "continuation_adapter": Path(__file__).resolve(strict=True),
        "continuation_builder": Path(__file__).with_name(
            "build_ipmsm_v2_target_load_continuation_v4r6.py"
        ).resolve(strict=True),
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
        "pyaedt_core": pyaedt_core_path.resolve(strict=True),
    }
    if len({str(path).casefold() for path in expected_paths.values()}) != len(
        expected_paths
    ):
        raise TargetLoadContinuationError("continuation source pin paths must be unique")
    result: dict[str, authority.FileSnapshot] = {}
    for name in sorted(REQUIRED_SOURCE_PINS):
        record = _mapping(records[name], f"source_pins.{name}")
        _expect_keys(record, {"path", "sha256"}, f"source_pins.{name}")
        path = _c_path(record["path"], f"source_pins.{name}.path", existing=True)
        if path != expected_paths[name]:
            raise TargetLoadContinuationError(f"source pin path differs from runtime: {name}")
        snapshot = authority.read_single_link_snapshot(path, f"continuation source {name}")
        if snapshot.sha256 != _sha256(record["sha256"], f"source_pins.{name}.sha256"):
            raise TargetLoadContinuationError(f"source pin SHA256 differs from runtime: {name}")
        result[name] = snapshot
    return result


def _validate_paths(value: Any) -> dict[str, Path]:
    raw = _mapping(value, "continuation.paths")
    expected = {
        "workspace",
        "decision",
        "claim",
        "recovery",
        "progress",
        "completion",
        "measured_front_csv",
        "measured_front_manifest",
    }
    _expect_keys(raw, expected, "continuation.paths")
    paths = {name: _c_path(raw[name], f"paths.{name}") for name in expected}
    if paths["claim"] != paths["decision"].with_name(paths["decision"].name + ".claim"):
        raise TargetLoadContinuationError("claim path is not derived from decision path")
    if paths["recovery"] != paths["claim"].with_name(paths["claim"].name + ".recover"):
        raise TargetLoadContinuationError("recovery path is not derived from claim path")
    if paths["progress"] != paths["workspace"] / "progress.json":
        raise TargetLoadContinuationError("progress path must be workspace/progress.json")
    for name in ("decision", "claim", "recovery"):
        if _path_is_within(paths[name], paths["workspace"]):
            raise TargetLoadContinuationError(f"{name} must remain outside coordinator workspace")
    for name in ("completion", "measured_front_csv", "measured_front_manifest"):
        if not _path_is_within(paths[name], paths["workspace"]):
            raise TargetLoadContinuationError(f"{name} must be inside the coordinator workspace")
        if paths[name].parent != paths["workspace"]:
            raise TargetLoadContinuationError(f"{name} must be an immediate workspace child")
    path_keys = {str(path).casefold() for name, path in paths.items() if name != "workspace"}
    if len(path_keys) != len(paths) - 1:
        raise TargetLoadContinuationError("continuation output paths must be distinct")
    return paths


def _validate_scheduler(value: Any, target: Mapping[str, Any]) -> dict[str, Any]:
    scheduler = _mapping(value, "continuation.scheduler")
    expected = {
        "url",
        "project",
        "project_id",
        "project_active_cap",
        "endpoint",
        "scheduling_profile",
        "required_capability",
        "env_profile",
        "env_setup",
        "partition",
        "max_workers_per_node",
        "remote_root",
        "entrypoint",
        "cpus",
        "cores_per_process",
        "memory_mb",
        "task_timeout_seconds",
        "request_timeout_seconds",
        "history_limit",
    }
    _expect_keys(scheduler, expected, "continuation.scheduler")
    target_scheduler = _mapping(target["scheduler"], "target_load.scheduler")
    for name in (
        "project_active_cap",
        "endpoint",
        "scheduling_profile",
        "required_capability",
        "env_profile",
        "env_setup",
        "max_workers_per_node",
    ):
        if scheduler[name] != target_scheduler[name]:
            raise TargetLoadContinuationError(f"scheduler.{name} differs from v4r6 authority")
    if scheduler["endpoint"] != "/api/tasks" or scheduler["project_active_cap"] != 50:
        raise TargetLoadContinuationError("scheduler endpoint/cap differs from required policy")
    if scheduler["partition"] != "auto" or scheduler["entrypoint"] != "subprocess_run.py":
        raise TargetLoadContinuationError("scheduler partition/entrypoint differs from required policy")
    parsed_url = parse.urlsplit(str(scheduler["url"]))
    if (
        parsed_url.scheme != "http"
        or parsed_url.hostname not in {"127.0.0.1", "localhost"}
        or parsed_url.port is None
        or parsed_url.path not in {"", "/"}
        or parsed_url.query
        or parsed_url.fragment
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise TargetLoadContinuationError("scheduler URL must be loopback HTTP")
    for name in (
        "project_id",
        "project_active_cap",
        "max_workers_per_node",
        "cpus",
        "cores_per_process",
        "memory_mb",
        "task_timeout_seconds",
        "history_limit",
    ):
        _integer(scheduler[name], f"scheduler.{name}")
    _finite(scheduler["request_timeout_seconds"], "scheduler.request_timeout_seconds", positive=True)
    for name in (
        "project",
        "endpoint",
        "scheduling_profile",
        "required_capability",
        "env_profile",
        "env_setup",
        "partition",
        "remote_root",
        "entrypoint",
    ):
        _nonblank(scheduler[name], f"scheduler.{name}")
    return scheduler


def _validate_runtime(value: Any, target: Mapping[str, Any]) -> dict[str, Any]:
    runtime = _mapping(value, "continuation.runtime")
    expected = {
        "task_retry_limit",
        "result_identity_relative_tolerance",
        "poll_interval_seconds",
        "overall_timeout_seconds",
    }
    _expect_keys(runtime, expected, "continuation.runtime")
    _integer(runtime["task_retry_limit"], "runtime.task_retry_limit", minimum=0)
    tolerance = _finite(
        runtime["result_identity_relative_tolerance"],
        "runtime.result_identity_relative_tolerance",
        positive=True,
    )
    if tolerance > 1.0e-3:
        raise TargetLoadContinuationError("result identity tolerance is too large")
    _finite(runtime["poll_interval_seconds"], "runtime.poll_interval_seconds", positive=True)
    _finite(runtime["overall_timeout_seconds"], "runtime.overall_timeout_seconds", positive=True)
    if runtime["overall_timeout_seconds"] < runtime["poll_interval_seconds"]:
        raise TargetLoadContinuationError("overall timeout is shorter than one poll interval")
    if target["result_settle_seconds"] <= 0:
        raise TargetLoadContinuationError("v4r6 result settle seconds is invalid")
    return runtime


def _validate_front_semantics(value: Any) -> None:
    semantics = _mapping(value, "continuation.final_front")
    _expect_keys(
        semantics,
        {"schema_version", "objectives", "dominance_tolerance", "deterministic_order"},
        "continuation.final_front",
    )
    expected = {
        "schema_version": FRONT_SCHEMA_VERSION,
        "objectives": list(FRONT_OBJECTIVES),
        "dominance_tolerance": FRONT_TOLERANCE,
        "deterministic_order": list(FRONT_ORDER),
    }
    if semantics != expected:
        raise TargetLoadContinuationError("final measured-front semantics differ")


def _actual_process_argv() -> tuple[str, ...]:
    original = getattr(sys, "orig_argv", None)
    if isinstance(original, list) and original:
        return tuple(str(item) for item in original)
    return (str(Path(sys.executable).resolve(strict=True)), *tuple(sys.argv))


def load_continuation_context(contract_path: str | Path) -> ContinuationContext:
    path = _c_path(str(contract_path), "continuation contract", existing=True)
    snapshot, document = _strict_document(path, "continuation contract")
    _expect_keys(document, {"schema_version", "contract_sha256", "continuation"}, "contract")
    if document["schema_version"] != SCHEMA_VERSION:
        raise TargetLoadContinuationError("unsupported continuation contract schema_version")
    unsigned = {key: value for key, value in document.items() if key != "contract_sha256"}
    logical_hash = authority.contract_logical_sha256(unsigned)
    if _sha256(document["contract_sha256"], "contract_sha256") != logical_hash:
        raise TargetLoadContinuationError("continuation contract_sha256 mismatch")
    continuation = _mapping(document["continuation"], "continuation")
    _expect_keys(
        continuation,
        {
            "authority",
            "runner",
            "paths",
            "source_pins",
            "scheduler",
            "runtime",
            "upstream_derived_binding",
            "final_front",
        },
        "continuation",
    )

    authority_record = _mapping(continuation["authority"], "continuation.authority")
    _expect_keys(
        authority_record,
        {"base_v4r5_contract", "v4r6_contract", "authorization_receipt"},
        "continuation.authority",
    )
    v6_binding, _ = _four_hash_binding(authority_record["v4r6_contract"], "v4r6 contract")
    if _same_path(Path(v6_binding["path"]), path):
        raise TargetLoadContinuationError("continuation contract cannot bind itself as v4r6 authority")
    try:
        authority_context = authority.load_authority_context(v6_binding["path"])
        authorization = authority.audit_authorization_receipt(v6_binding["path"])
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationError(f"v4r6 authorization audit failed: {exc}") from exc
    if dict(authority_context.contract_binding) != v6_binding:
        raise TargetLoadContinuationError("v4r6 authority binding changed")
    base_binding, _ = _four_hash_binding(
        authority_record["base_v4r5_contract"], "base v4r5 contract"
    )
    if base_binding != dict(authority_context.base_v4r5_binding):
        raise TargetLoadContinuationError("base v4r5 binding differs from v4r6 authority")
    receipt = _mapping(authority_record["authorization_receipt"], "authorization receipt")
    _expect_keys(
        receipt,
        {"path", "raw_sha256", "receipt_sha256", "confirmation_sha256", "contract_sha256"},
        "authorization receipt",
    )
    expected_receipt = {
        "path": str(authorization.path),
        "raw_sha256": authorization.file_sha256,
        "receipt_sha256": authorization.receipt_sha256,
        "confirmation_sha256": authorization.confirmation_sha256,
        "contract_sha256": authorization.contract_sha256,
    }
    if receipt != expected_receipt:
        raise TargetLoadContinuationError("v4r6 authorization receipt binding changed")

    source_pins = _source_pins(
        continuation["source_pins"],
        pyaedt_core_path=authority_context.pyaedt_core_snapshot.path,
    )
    runner = _mapping(continuation["runner"], "continuation.runner")
    _expect_keys(runner, {"executable", "source", "argv"}, "continuation.runner")
    executable_record = _mapping(runner["executable"], "runner.executable")
    source_record = _mapping(runner["source"], "runner.source")
    _expect_keys(executable_record, {"path", "sha256"}, "runner.executable")
    _expect_keys(source_record, {"path", "sha256"}, "runner.source")
    executable = authority.read_single_link_snapshot(
        _c_path(executable_record["path"], "runner.executable.path", existing=True),
        "continuation executable",
        require_single_link=False,
    )
    source = authority.read_single_link_snapshot(
        _c_path(source_record["path"], "runner.source.path", existing=True),
        "continuation source",
    )
    if executable.path != Path(sys.executable).resolve(strict=True):
        raise TargetLoadContinuationError("runner executable differs from sys.executable")
    if source.path != Path(__file__).resolve(strict=True):
        raise TargetLoadContinuationError("runner source differs from executing adapter")
    if executable.sha256 != _sha256(executable_record["sha256"], "runner executable SHA256"):
        raise TargetLoadContinuationError("runner executable SHA256 changed")
    if source.sha256 != _sha256(source_record["sha256"], "runner source SHA256"):
        raise TargetLoadContinuationError("runner source SHA256 changed")
    raw_argv = runner["argv"]
    if not isinstance(raw_argv, list):
        raise TargetLoadContinuationError("runner.argv must be an exact command list")
    runner_argv = tuple(_nonblank(item, "runner.argv item") for item in raw_argv)
    expected_argv = (
        str(executable.path),
        str(source.path),
        "--continuation-contract",
        str(path),
        "--execute",
    )
    if runner_argv != expected_argv:
        raise TargetLoadContinuationError("runner argv differs from exact execution command")

    paths = _validate_paths(continuation["paths"])
    try:
        authority._audit_parent_chain(paths["workspace"], "coordinator workspace")
        for name in ("decision", "claim", "recovery"):
            authority._audit_parent_chain(paths[name], f"continuation {name}")
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationError(str(exc)) from exc
    immutable_paths = {
        str(item.path).casefold()
        for item in (
            *authority_context.bound_snapshots,
            authority_context.contract,
            *source_pins.values(),
            executable,
            source,
        )
    }
    immutable_paths.update(
        {
            str(authority_context.declaration_path).casefold(),
            str(authority_context.confirmation_path).casefold(),
            str(authority_context.authorization_receipt_path).casefold(),
            str(path).casefold(),
        }
    )
    if any(
        _path_is_within(Path(immutable), paths["workspace"])
        for immutable in immutable_paths
    ):
        raise TargetLoadContinuationError("coordinator workspace contains immutable authority")
    for name, output in paths.items():
        if str(output).casefold() in immutable_paths:
            raise TargetLoadContinuationError(f"paths.{name} aliases immutable authority")
        for protected in authority_context.protected_input_directories:
            if _path_is_within(output, protected) or (
                name == "workspace" and _path_is_within(protected, output)
            ):
                raise TargetLoadContinuationError(
                    f"paths.{name} overlaps protected upstream input directory"
                )
    scheduler = _validate_scheduler(continuation["scheduler"], authority_context.target_load)
    runtime = _validate_runtime(continuation["runtime"], authority_context.target_load)
    upstream = _mapping(continuation["upstream_derived_binding"], "upstream derived binding")
    expected_upstream = _mapping(
        authority_context.target_load["upstream_authority"], "v4r6 upstream authority"
    )
    _expect_keys(
        upstream,
        {
            "binding_schema_version",
            "binding_hash_algorithm",
            "upstream_binding_sha256",
            "filtered_plan_sha256",
            "selected_candidate_ids",
            "upstream_artifacts_manifest_sha256",
            "per_case_results_manifest_sha256",
        },
        "upstream derived binding",
    )
    expected_derived = {
        "binding_schema_version": expected_upstream["binding_schema_version"],
        "binding_hash_algorithm": expected_upstream["binding_hash_algorithm"],
        "upstream_binding_sha256": expected_upstream["upstream_binding_sha256"],
        "filtered_plan_sha256": expected_upstream["filtered_plan_sha256"],
        "selected_candidate_ids": expected_upstream["selected_candidate_ids"],
        "upstream_artifacts_manifest_sha256": expected_upstream[
            "upstream_artifacts_manifest_sha256"
        ],
        "per_case_results_manifest_sha256": authority_context.target_load["upstream_results"][
            "per_case_results_manifest_sha256"
        ],
    }
    if upstream != expected_derived:
        raise TargetLoadContinuationError("upstream derived binding differs from v4r6 authority")
    _validate_front_semantics(continuation["final_front"])
    authority.assert_context_unchanged(authority_context)
    return ContinuationContext(
        path=path,
        snapshot=snapshot,
        document=document,
        contract_sha256=logical_hash,
        authority_context=authority_context,
        authorization=authorization,
        paths=paths,
        scheduler=scheduler,
        runtime=runtime,
        runner_argv=runner_argv,
        source_pins=source_pins,
        runner_executable=executable,
        runner_source=source,
    )


def _decision_path_from_v4(context: ContinuationContext) -> Path:
    try:
        contract = v4.load_contract(context.authority_context.base_v4r5_contract.path)
        v4.audit_contract(contract)
        authorization = v4.audit_authorization(contract)
        decision_path = contract.base_contract.optimization.decision
        decision = v3.audit_decision(
            decision_path,
            schema_version=v3.OPTIMIZATION_DECISION_SCHEMA_VERSION,
            allowed_statuses={"complete"},
            workdir=contract.base_contract.workdir,
        )
        v4.audit_optimization_decision_authorization(decision, authorization)
    except Exception as exc:
        raise TargetLoadContinuationError(f"v4r5 authorization audit failed: {exc}") from exc
    return decision_path


def _strict_upstream_replay(
    context: ContinuationContext,
) -> tuple[authority_builder.CompletedUpstreamAudit, Mapping[str, Any], SimpleNamespace]:
    decision_path = _decision_path_from_v4(context)
    try:
        upstream = authority_builder.audit_completed_upstream(
            context.authority_context.base_v4r5_contract.path
        )
        decision, _ = coordinator._read_indented_json(
            decision_path, "optimization decision"
        )
        paths = authority_builder._derived_upstream_paths(decision)
        paths.optimization_decision = decision_path
    except Exception as exc:
        raise TargetLoadContinuationError(f"strict upstream replay failed: {exc}") from exc
    target = context.authority_context.target_load
    target_upstream = target["upstream_authority"]
    per_case = target["upstream_results"]
    manifest_sha = authority.canonical_sha256(
        {
            "schema_version": authority.UPSTREAM_ARTIFACTS_MANIFEST_SCHEMA_VERSION,
            "artifacts": list(upstream.upstream_artifacts_manifest),
        }
    )
    per_case_sha = authority.canonical_sha256(
        {
            "schema_version": authority.PER_CASE_RESULTS_MANIFEST_SCHEMA_VERSION,
            "results": list(upstream.per_case_results_manifest),
        }
    )
    mismatches = []
    if upstream.upstream_binding_sha256 != target_upstream["upstream_binding_sha256"]:
        mismatches.append("upstream binding SHA256")
    if upstream.filtered_plan_sha256 != target_upstream["filtered_plan_sha256"]:
        mismatches.append("filtered plan SHA256")
    if list(upstream.candidate_ids) != target_upstream["selected_candidate_ids"]:
        mismatches.append("selected candidate order")
    if manifest_sha != target_upstream["upstream_artifacts_manifest_sha256"]:
        mismatches.append("upstream artifact manifest")
    if per_case_sha != per_case["per_case_results_manifest_sha256"]:
        mismatches.append("per-case results manifest")
    if mismatches:
        raise TargetLoadContinuationError(
            "strict coordinator upstream replay differs: " + ", ".join(mismatches)
        )
    for index, snapshot in enumerate(upstream.snapshots):
        authority.assert_snapshot_unchanged(snapshot, f"strict upstream input {index}")
    authority.assert_context_unchanged(context.authority_context)
    return upstream, decision, paths


def _assert_upstream_snapshots_unchanged(
    upstream: authority_builder.CompletedUpstreamAudit,
) -> None:
    for index, snapshot in enumerate(upstream.snapshots):
        authority.assert_snapshot_unchanged(snapshot, f"strict upstream input {index}")


def _validate_built_root_authority(
    context: ContinuationContext,
    upstream: authority_builder.CompletedUpstreamAudit,
    root: Mapping[str, Any],
) -> None:
    try:
        workflow.validate_root_manifest(root)
        identity = _mapping(root.get("identity"), "target-load root identity")
        root_upstream = _mapping(
            identity.get("upstream_pareto_binding"),
            "target-load root upstream binding",
        )
        documents = _mapping(
            identity.get("source_documents_base64"),
            "target-load root source documents",
        )
        seed_plan = base64.b64decode(
            _nonblank(documents.get("seed_fea_plan_csv"), "root filtered seed plan"),
            validate=True,
        )
        source_hashes = _mapping(
            identity.get("source_hashes"), "target-load root source hashes"
        )
    except (ValueError, workflow.TargetLoadWorkflowError) as exc:
        raise TargetLoadContinuationError(
            f"built target-load root authority is invalid: {exc}"
        ) from exc
    target = context.authority_context.target_load["upstream_authority"]
    mismatches: list[str] = []
    if authority.canonical_sha256(root_upstream) != target["upstream_binding_sha256"]:
        mismatches.append("upstream binding SHA256")
    if hashlib.sha256(seed_plan).hexdigest() != target["filtered_plan_sha256"]:
        mismatches.append("filtered plan SHA256")
    if identity.get("candidate_order") != list(upstream.candidate_ids):
        mismatches.append("candidate order")
    if source_hashes.get("pyaedt_core_source_sha256") != (
        context.authority_context.pyaedt_core_snapshot.sha256
    ):
        mismatches.append("PyAEDT source SHA256")
    if mismatches:
        raise TargetLoadContinuationError(
            "built target-load root differs from human authority: " + ", ".join(mismatches)
        )


def _coordinator_args(context: ContinuationContext, paths: SimpleNamespace) -> SimpleNamespace:
    scheduler = context.scheduler
    runtime = context.runtime
    current = context.authority_context.target_load["current_matching"]
    return SimpleNamespace(
        **vars(paths),
        project=scheduler["project"],
        project_id=scheduler["project_id"],
        project_active_cap=scheduler["project_active_cap"],
        remote_root=scheduler["remote_root"],
        env_setup=scheduler["env_setup"],
        max_workers_per_node=scheduler["max_workers_per_node"],
        cpus=scheduler["cpus"],
        cores_per_process=scheduler["cores_per_process"],
        memory_mb=scheduler["memory_mb"],
        task_timeout_seconds=scheduler["task_timeout_seconds"],
        task_retry_limit=runtime["task_retry_limit"],
        result_settle_seconds=context.authority_context.target_load["result_settle_seconds"],
        result_identity_relative_tolerance=runtime["result_identity_relative_tolerance"],
        relative_tolerance=current["relative_tolerance"],
        max_attempts=current["max_attempts"],
        monotonic_relative_tolerance=current["monotonic_relative_tolerance"],
        minimum_step_relative=current["minimum_step_relative"],
        maximum_scale_per_attempt=current["maximum_scale_per_attempt"],
        scheduler_url=scheduler["url"],
        scheduler_timeout=scheduler["request_timeout_seconds"],
        history_limit=scheduler["history_limit"],
    )


def _client(context: ContinuationContext) -> coordinator.SchedulerClient:
    return coordinator.SchedulerClient(
        context.scheduler["url"],
        timeout=context.scheduler["request_timeout_seconds"],
        history_limit=context.scheduler["history_limit"],
    )


def _publication_parent_identity(path: Path, label: str) -> tuple[int, int, int, int, int]:
    try:
        candidate = authority._require_c_local(path.absolute(), label)
        authority._audit_parent_chain(candidate, label)
        info = os.lstat(candidate.parent)
        identity = authority._stat_identity(info)
    except (OSError, authority.TargetLoadAuthorityError) as exc:
        raise TargetLoadContinuationError(f"cannot audit {label} parent: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or identity[-1]:
        raise TargetLoadContinuationError(f"{label} parent is not a local no-reparse directory")
    return (identity[0], identity[1], stat.S_IFMT(identity[2]), identity[3], identity[-1])


def _assert_publication_parent(
    path: Path,
    label: str,
    expected: tuple[int, int, int, int, int],
) -> None:
    if _publication_parent_identity(path, label) != expected:
        raise TargetLoadContinuationError(f"{label} parent changed during publication")


def _publish_no_replace_bytes(path: Path, payload: bytes, label: str) -> bool:
    path = authority._require_c_local(path.absolute(), label)
    parent_identity = _publication_parent_identity(path, label)
    if path.is_file():
        try:
            current = authority.read_single_link_snapshot(path, label).payload
        except authority.TargetLoadAuthorityError as exc:
            raise TargetLoadContinuationError(str(exc)) from exc
        if current != payload:
            raise TargetLoadContinuationError(f"existing {label} differs: {path}")
        _assert_publication_parent(path, label, parent_identity)
        return False
    token = hashlib.sha256(payload).hexdigest()
    staged = path.with_name(f".{path.name}.{token}.tmp")
    descriptor = -1
    created_stage = False
    created_stage_snapshot: authority.FileSnapshot | None = None
    try:
        try:
            descriptor = os.open(
                staged,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | int(getattr(os, "O_NOFOLLOW", 0)),
                0o600,
            )
            created_stage = True
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                opened_stage = os.fstat(stream.fileno())
            created_stage_snapshot = authority.read_single_link_snapshot(
                staged, f"created {label} staging"
            )
            if (
                not authority._opened_file_matches(
                    opened_stage, created_stage_snapshot.identity
                )
                or created_stage_snapshot.payload != payload
            ):
                raise TargetLoadContinuationError(
                    f"created {label} staging changed after fsync"
                )
        except FileExistsError:
            try:
                staged_payload = authority.read_single_link_snapshot(
                    staged, f"interrupted {label} staging"
                ).payload
            except authority.TargetLoadAuthorityError as exc:
                raise TargetLoadContinuationError(str(exc)) from exc
            if staged_payload != payload:
                raise TargetLoadContinuationError(
                    f"interrupted {label} staging differs from exact bytes"
                )
        _assert_publication_parent(path, label, parent_identity)
        try:
            if os.name != "nt":
                raise TargetLoadContinuationError(
                    "C-local no-replace publication requires Windows rename semantics"
                )
            atomic_publish._windows_rename_no_replace(staged, path)
        except (FileExistsError, OSError):
            if not path.is_file():
                raise
            current = authority.read_single_link_snapshot(path, f"raced {label}").payload
            if current != payload:
                raise TargetLoadContinuationError(f"{label} publication raced with different bytes")
            _assert_publication_parent(path, label, parent_identity)
            return False
        _assert_publication_parent(path, label, parent_identity)
        committed = authority.read_single_link_snapshot(path, f"committed {label}")
        if committed.payload != payload:
            raise TargetLoadContinuationError(f"committed {label} differs from exact bytes")
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        # A completed rename removes staging.  Preserve every pre-commit stage
        # on error so a later exact invocation can either adopt the complete
        # bytes or fail closed on a partial/foreign artifact.
        if (
            created_stage
            and created_stage_snapshot is not None
            and path.is_file()
            and os.path.lexists(staged)
        ):
            try:
                _unlink_bound_snapshot(
                    created_stage_snapshot, f"created {label} staging"
                )
            except (OSError, authority.TargetLoadAuthorityError, TargetLoadContinuationError):
                pass


def _publish_no_replace_json(path: Path, value: Mapping[str, Any], label: str) -> bool:
    return _publish_no_replace_bytes(path, authority.canonical_json_bytes(value), label)


def _owner(mode: str) -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "invocation_id": uuid.uuid4().hex,
        "mode": mode,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:
                return True
            if error == 87:
                return False
            raise OSError(error, f"OpenProcess failed for PID {pid}")
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
            return exit_code.value == 259
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_claim(path: Path, label: str) -> dict[str, Any]:
    _, value = _strict_document(path, label)
    return value


def _unlink_bound_snapshot(snapshot: authority.FileSnapshot, label: str) -> None:
    """Remove only the exact file object that was validated by the caller."""

    try:
        authority.assert_snapshot_unchanged(snapshot, label)
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationError(f"{label} changed before removal: {exc}") from exc
    receipt = atomic_publish.PublishReceipt(
        source=snapshot.path,
        destination=snapshot.path,
        identity=atomic_publish.FileIdentity(
            device=snapshot.identity[0],
            inode=snapshot.identity[1],
            size=snapshot.identity[4],
        ),
        strategy="bound-snapshot-delete",
    )
    if not atomic_publish.rollback_owned_output(receipt):
        raise TargetLoadContinuationError(f"{label} ownership changed before removal")
    if os.path.lexists(snapshot.path):
        raise TargetLoadContinuationError(f"{label} still exists after removal")


def _validate_claim(value: Mapping[str, Any], context: ContinuationContext) -> None:
    _expect_keys(
        value,
        {"schema_version", "contract", "decision", "original_owner", "owner"},
        "continuation claim",
    )
    if value["schema_version"] != CLAIM_SCHEMA_VERSION:
        raise TargetLoadContinuationError("claim schema_version changed")
    if value["contract"] != {
        "path": str(context.path),
        "raw_sha256": context.snapshot.sha256,
        "contract_sha256": context.contract_sha256,
    }:
        raise TargetLoadContinuationError("claim continuation contract binding changed")
    decision = _mapping(value["decision"], "claim.decision")
    _expect_keys(decision, {"path", "sha256"}, "claim.decision")
    if decision["path"] != str(context.paths["decision"]):
        raise TargetLoadContinuationError("claim decision path changed")
    if decision["sha256"] != "":
        raise TargetLoadContinuationError("claim decision digest policy changed")
    for key in ("original_owner", "owner"):
        owner = _mapping(value[key], f"claim.{key}")
        _expect_keys(
            owner,
            {"hostname", "pid", "invocation_id", "mode", "started_at_utc"},
            f"claim.{key}",
        )
        _integer(owner["pid"], f"claim.{key}.pid")
        for name in ("hostname", "invocation_id", "mode", "started_at_utc"):
            _nonblank(owner[name], f"claim.{key}.{name}")


def _claim_payload(
    context: ContinuationContext,
    owner: Mapping[str, Any],
    original_owner: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "contract": {
            "path": str(context.path),
            "raw_sha256": context.snapshot.sha256,
            "contract_sha256": context.contract_sha256,
        },
        # The immutable decision audits its own contract binding.  Keeping this
        # digest empty avoids a mutable claim transition and closes the
        # claim-created/decision-created hard-kill window.
        "decision": {"path": str(context.paths["decision"]), "sha256": ""},
        "original_owner": dict(original_owner),
        "owner": dict(owner),
    }


def _claim_owned(context: ContinuationContext, owner: Mapping[str, Any]) -> bool:
    try:
        value = _read_claim(context.paths["claim"], "continuation claim")
    except (OSError, TargetLoadContinuationError):
        return False
    return value.get("schema_version") == CLAIM_SCHEMA_VERSION and value.get("owner") == dict(owner)


def _original_owner_for_new_claim(
    context: ContinuationContext, new_owner: Mapping[str, Any]
) -> Mapping[str, Any]:
    decision_path = context.paths["decision"]
    if not decision_path.is_file():
        return new_owner
    _, decision = _strict_document(decision_path, "existing continuation decision")
    _expect_keys(
        decision,
        {"schema_version", "status", "contract", "original_owner"},
        "existing continuation decision",
    )
    if (
        decision["schema_version"] != DECISION_SCHEMA_VERSION
        or decision["status"] != "running"
        or decision["contract"]
        != {
            "path": str(context.path),
            "raw_sha256": context.snapshot.sha256,
            "contract_sha256": context.contract_sha256,
        }
    ):
        raise TargetLoadContinuationError("existing continuation decision identity changed")
    original_owner = _mapping(decision["original_owner"], "decision original_owner")
    probe_claim = _claim_payload(context, original_owner, original_owner)
    _validate_claim(probe_claim, context)
    return original_owner


def _resume_interrupted_recovery(
    context: ContinuationContext, new_owner: Mapping[str, Any]
) -> bool:
    """Finish a hard-killed claim adoption; return True when claim is acquired."""

    recovery_path = context.paths["recovery"]
    claim_path = context.paths["claim"]
    if not recovery_path.exists():
        return False
    recovery_snapshot, recovery = _strict_document(
        recovery_path, "claim recovery lock"
    )
    _expect_keys(
        recovery,
        {
            "schema_version",
            "contract_sha256",
            "claim_path",
            "stale_claim_sha256",
            "stale_claim",
            "owner",
        },
        "claim recovery lock",
    )
    if (
        recovery["schema_version"] != RECOVERY_SCHEMA_VERSION
        or recovery["contract_sha256"] != context.contract_sha256
        or recovery["claim_path"] != str(claim_path)
    ):
        raise TargetLoadContinuationError("claim recovery lock identity changed")
    recovery_owner = _mapping(recovery["owner"], "claim recovery owner")
    if recovery_owner.get("hostname") != socket.gethostname():
        raise TargetLoadContinuationError("claim recovery belongs to another host")
    if _pid_is_running(_integer(recovery_owner.get("pid"), "claim recovery owner pid")):
        raise TargetLoadContinuationError("claim recovery owner is still active")
    stale_claim = _mapping(recovery["stale_claim"], "claim recovery stale claim")
    _validate_claim(stale_claim, context)
    stale_payload = authority.canonical_json_bytes(stale_claim)
    if hashlib.sha256(stale_payload).hexdigest() != recovery["stale_claim_sha256"]:
        raise TargetLoadContinuationError("claim recovery stale-claim SHA256 changed")
    if claim_path.exists():
        current_snapshot, current = _strict_document(
            claim_path, "claim after interrupted recovery"
        )
        _validate_claim(current, context)
        if current_snapshot.sha256 == recovery["stale_claim_sha256"]:
            _unlink_bound_snapshot(current_snapshot, "claim after interrupted recovery")
        elif current.get("owner") == recovery_owner:
            _unlink_bound_snapshot(current_snapshot, "claim after interrupted recovery")
        elif current.get("original_owner") == stale_claim["original_owner"]:
            replacement_owner = _mapping(
                current.get("owner"), "interrupted replacement claim owner"
            )
            if replacement_owner.get("hostname") != socket.gethostname():
                raise TargetLoadContinuationError(
                    "interrupted replacement claim belongs to another host"
                )
            if _pid_is_running(
                _integer(replacement_owner.get("pid"), "replacement claim owner pid")
            ):
                raise TargetLoadContinuationError(
                    "interrupted replacement claim owner is still active"
                )
            _unlink_bound_snapshot(current_snapshot, "claim after interrupted recovery")
        else:
            raise TargetLoadContinuationError(
                "claim differs from both sides of interrupted recovery"
            )
    if authority.read_single_link_snapshot(
        recovery_path, "claim recovery lock"
    ).sha256 != recovery_snapshot.sha256:
        raise TargetLoadContinuationError("claim recovery lock changed during adoption")
    _publish_no_replace_json(
        claim_path,
        _claim_payload(context, new_owner, stale_claim["original_owner"]),
        "recovered continuation claim",
    )
    _unlink_bound_snapshot(recovery_snapshot, "claim recovery lock")
    return True


def _acquire_claim(context: ContinuationContext, new_owner: Mapping[str, Any]) -> Path:
    claim_path = context.paths["claim"]
    recovery_path = context.paths["recovery"]
    if _resume_interrupted_recovery(context, new_owner):
        return claim_path
    if not claim_path.exists():
        original_owner = _original_owner_for_new_claim(context, new_owner)
        _publish_no_replace_json(
            claim_path,
            _claim_payload(context, new_owner, original_owner),
            "continuation claim",
        )
        return claim_path

    old_snapshot, old_claim = _strict_document(claim_path, "stale continuation claim")
    _validate_claim(old_claim, context)
    old_owner = _mapping(old_claim["owner"], "stale claim owner")
    if old_owner["hostname"] != socket.gethostname():
        raise TargetLoadContinuationError("stale claim belongs to another host")
    if _pid_is_running(int(old_owner["pid"])):
        raise TargetLoadContinuationError(f"continuation owner is still active: pid={old_owner['pid']}")
    recovery = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "contract_sha256": context.contract_sha256,
        "claim_path": str(claim_path),
        "stale_claim_sha256": old_snapshot.sha256,
        "stale_claim": old_claim,
        "owner": dict(new_owner),
    }
    _publish_no_replace_json(recovery_path, recovery, "claim recovery lock")
    recovery_snapshot, committed_recovery = _strict_document(
        recovery_path, "claim recovery lock"
    )
    if committed_recovery != recovery:
        raise TargetLoadContinuationError("claim recovery lock changed after publication")
    if authority.read_single_link_snapshot(
        claim_path, "stale continuation claim"
    ).sha256 != old_snapshot.sha256:
        raise TargetLoadContinuationError("stale claim changed during recovery")
    _unlink_bound_snapshot(old_snapshot, "stale continuation claim")
    _publish_no_replace_json(
        claim_path,
        _claim_payload(context, new_owner, old_claim["original_owner"]),
        "adopted continuation claim",
    )
    _unlink_bound_snapshot(recovery_snapshot, "claim recovery lock")
    return claim_path


def _publish_decision(context: ContinuationContext, owner: Mapping[str, Any]) -> None:
    claim = _read_claim(context.paths["claim"], "continuation claim")
    _validate_claim(claim, context)
    if claim["owner"] != dict(owner):
        raise TargetLoadContinuationError("claim ownership was lost before decision publication")
    decision = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "status": "running",
        "contract": {
            "path": str(context.path),
            "raw_sha256": context.snapshot.sha256,
            "contract_sha256": context.contract_sha256,
        },
        "original_owner": dict(claim["original_owner"]),
    }
    if context.paths["decision"].is_file():
        _, existing = _strict_document(context.paths["decision"], "continuation decision")
        _expect_keys(existing, set(decision), "continuation decision")
        if existing["schema_version"] != DECISION_SCHEMA_VERSION or existing["status"] != "running":
            raise TargetLoadContinuationError("continuation decision schema/status changed")
        if existing["contract"] != decision["contract"]:
            raise TargetLoadContinuationError("continuation decision contract binding changed")
        if existing["original_owner"] != decision["original_owner"]:
            raise TargetLoadContinuationError("continuation decision original owner changed")
        return
    _publish_no_replace_json(context.paths["decision"], decision, "continuation decision")
    if not _claim_owned(context, owner):
        raise TargetLoadContinuationError("new claim ownership was lost after decision publication")


def _prepare_workspace(
    context: ContinuationContext,
) -> tuple[coordinator.SchedulerClient, Mapping[str, Any]]:
    _assert_authority_unchanged(context)
    upstream, _, upstream_paths = _strict_upstream_replay(context)
    args = _coordinator_args(context, upstream_paths)
    _assert_upstream_snapshots_unchanged(upstream)
    root = coordinator.build_root_from_files(
        args,
        pyaedt_core_source_bytes=context.authority_context.pyaedt_core_snapshot.payload,
    )
    _validate_built_root_authority(context, upstream, root)
    _assert_upstream_snapshots_unchanged(upstream)
    _assert_authority_unchanged(context)
    progress = coordinator.initialize_workspace(context.paths["workspace"], root)
    if progress["root_manifest_sha256"] != workflow.canonical_json_sha256(root):
        raise TargetLoadContinuationError("initialized root identity differs")
    for candidate_id in upstream.candidate_ids:
        _assert_upstream_snapshots_unchanged(upstream)
        evidence = coordinator.build_fixed_mtpa_evidence_from_results(
            root,
            candidate_id,
            context.authority_context.upstream_results_dir,
        )
        coordinator.publish_fixed_mtpa_evidence(
            context.paths["workspace"], candidate_id, evidence
        )
    _assert_upstream_snapshots_unchanged(upstream)
    state = coordinator.replay_workspace(context.paths["workspace"], repair=False)
    if state.root != root:
        raise TargetLoadContinuationError("coordinator root differs after fixed-MTPA import")
    _assert_authority_unchanged(context)
    return _client(context), root


def measured_nondominated_front(
    summaries: Sequence[Mapping[str, Any]],
    *,
    tolerance: float = FRONT_TOLERANCE,
) -> list[dict[str, Any]]:
    """Return deterministic measured nondominated rows on (volume, 1-efficiency)."""

    if tolerance < 0.0 or not math.isfinite(tolerance):
        raise TargetLoadContinuationError("dominance tolerance must be finite and nonnegative")
    rows: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    for summary in summaries:
        candidate_id = _nonblank(summary.get("candidate_id"), "summary candidate_id")
        if candidate_id in candidate_ids:
            raise TargetLoadContinuationError("candidate summaries contain a duplicate candidate")
        candidate_ids.add(candidate_id)
        volume = _finite(summary.get("objective_active_volume_m3"), "measured active volume", positive=True)
        efficiency = _finite(summary.get("objective_cycle_efficiency"), "measured cycle efficiency")
        one_minus = _finite(
            summary.get("objective_one_minus_cycle_efficiency"),
            "measured one-minus efficiency",
        )
        if not 0.0 < efficiency <= 1.0 or not math.isclose(
            one_minus, 1.0 - efficiency, rel_tol=0.0, abs_tol=1.0e-15
        ):
            raise TargetLoadContinuationError("measured efficiency objectives are inconsistent")
        rows.append(
            {
                "candidate_id": candidate_id,
                "objective_active_volume_m3": volume,
                "objective_cycle_efficiency": efficiency,
                "objective_one_minus_cycle_efficiency": one_minus,
                "summary_sha256": _sha256(summary.get("summary_sha256"), "summary SHA256"),
            }
        )

    def dominates(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        left_values = (
            float(left["objective_active_volume_m3"]),
            float(left["objective_one_minus_cycle_efficiency"]),
        )
        right_values = (
            float(right["objective_active_volume_m3"]),
            float(right["objective_one_minus_cycle_efficiency"]),
        )
        no_worse = all(a <= b + tolerance for a, b in zip(left_values, right_values))
        strictly_better = any(a < b - tolerance for a, b in zip(left_values, right_values))
        return no_worse and strictly_better

    front = [row for row in rows if not any(dominates(other, row) for other in rows if other is not row)]
    return sorted(
        front,
        key=lambda row: (
            float(row["objective_active_volume_m3"]),
            float(row["objective_one_minus_cycle_efficiency"]),
            str(row["candidate_id"]),
        ),
    )


def _front_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(FRONT_FIELDS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "candidate_id": row["candidate_id"],
                "objective_active_volume_m3": format(
                    float(row["objective_active_volume_m3"]), ".17g"
                ),
                "objective_cycle_efficiency": format(
                    float(row["objective_cycle_efficiency"]), ".17g"
                ),
                "objective_one_minus_cycle_efficiency": format(
                    float(row["objective_one_minus_cycle_efficiency"]), ".17g"
                ),
                "summary_sha256": row["summary_sha256"],
            }
        )
    return stream.getvalue().encode("utf-8")


def _final_live_state(
    context: ContinuationContext,
    client: coordinator.SchedulerClient,
) -> tuple[coordinator.ReplayState, Mapping[str, Any]]:
    now = datetime.now(timezone.utc)
    state = coordinator.replay_workspace(context.paths["workspace"], repair=False)
    scheduler_contract = state.root["identity"]["scheduler_contract"]
    snapshot = client.snapshot(scheduler_contract)
    coordinator._validate_observed_attempt_histories(
        context.paths["workspace"], state, snapshot, now
    )
    replayed = coordinator.replay_workspace(context.paths["workspace"], repair=False)
    closing_snapshot = client.snapshot(scheduler_contract)
    coordinator._validate_observed_attempt_histories(
        context.paths["workspace"], replayed, closing_snapshot, now
    )
    closed_replay = coordinator.replay_workspace(
        context.paths["workspace"], repair=False
    )
    if closed_replay != replayed:
        raise TargetLoadContinuationError("coordinator replay changed across closing snapshots")
    progress = coordinator.build_progress(
        closed_replay,
        closing_snapshot.history,
        now,
        workspace=context.paths["workspace"],
    )
    if progress["status"] != "complete" or closed_replay.failures:
        raise TargetLoadContinuationError("live coordinator replay is not complete")
    if set(closed_replay.summaries) != set(
        closed_replay.root["identity"]["candidate_order"]
    ):
        raise TargetLoadContinuationError("live candidate summary coverage is incomplete")
    return closed_replay, progress


def _assert_authority_unchanged(context: ContinuationContext) -> None:
    try:
        authority.assert_snapshot_unchanged(
            context.snapshot, "target-load continuation contract"
        )
        for name, snapshot in sorted(context.source_pins.items()):
            authority.assert_snapshot_unchanged(
                snapshot, f"target-load continuation source {name}"
            )
        authority.assert_snapshot_unchanged(
            context.runner_executable, "target-load continuation executable"
        )
        authority.assert_snapshot_unchanged(
            context.runner_source, "target-load continuation runner"
        )
        authority.assert_context_unchanged(context.authority_context)
        live_authorization = authority.audit_authorization_receipt(
            context.authority_context.contract.path
        )
        if live_authorization != context.authorization:
            raise TargetLoadContinuationError("v4r6 authorization receipt changed")
        authority.assert_snapshot_unchanged(
            context.snapshot, "target-load continuation contract"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadContinuationError(f"target-load authority changed: {exc}") from exc


def _publish_final_outputs(
    context: ContinuationContext,
    state: coordinator.ReplayState,
    progress: Mapping[str, Any],
    *,
    owner: Mapping[str, Any],
) -> Mapping[str, Any]:
    _assert_authority_unchanged(context)
    ordered_summaries = [
        state.summaries[candidate_id]
        for candidate_id in state.root["identity"]["candidate_order"]
    ]
    front = measured_nondominated_front(ordered_summaries)
    if not front:
        raise TargetLoadContinuationError("measured nondominated front is empty")
    csv_payload = _front_csv(front)
    manifest = {
        "schema_version": FRONT_MANIFEST_SCHEMA_VERSION,
        "contract": {
            "path": str(context.path),
            "raw_sha256": context.snapshot.sha256,
            "contract_sha256": context.contract_sha256,
        },
        "root_manifest_sha256": progress["root_manifest_sha256"],
        "source_candidate_order": list(state.root["identity"]["candidate_order"]),
        "source_summary_sha256": {
            candidate_id: state.summaries[candidate_id]["summary_sha256"]
            for candidate_id in state.root["identity"]["candidate_order"]
        },
        "front": {
            "schema_version": FRONT_SCHEMA_VERSION,
            "objectives": list(FRONT_OBJECTIVES),
            "dominance_tolerance": FRONT_TOLERANCE,
            "deterministic_order": list(FRONT_ORDER),
            "candidate_ids": [row["candidate_id"] for row in front],
            "candidate_count": len(front),
            "csv_path": str(context.paths["measured_front_csv"]),
            "csv_sha256": hashlib.sha256(csv_payload).hexdigest(),
        },
    }
    _publish_no_replace_bytes(
        context.paths["measured_front_csv"], csv_payload, "measured target-load front"
    )
    _publish_no_replace_json(
        context.paths["measured_front_manifest"], manifest, "measured target-load front manifest"
    )
    csv_snapshot = authority.read_single_link_snapshot(
        context.paths["measured_front_csv"], "measured target-load front"
    )
    if csv_snapshot.payload != csv_payload:
        raise TargetLoadContinuationError("measured target-load front bytes changed")
    manifest_snapshot = authority.read_single_link_snapshot(
        context.paths["measured_front_manifest"], "measured front manifest"
    )
    if manifest_snapshot.payload != authority.canonical_json_bytes(manifest):
        raise TargetLoadContinuationError("measured front manifest bytes changed")
    completion_core = {
        "schema_version": COMPLETION_SCHEMA_VERSION,
        "status": "complete",
        "contract": manifest["contract"],
        "root_manifest_sha256": progress["root_manifest_sha256"],
        "measured_front_csv": {
            "path": str(context.paths["measured_front_csv"]),
            "sha256": hashlib.sha256(csv_payload).hexdigest(),
        },
        "measured_front_manifest": {
            "path": str(context.paths["measured_front_manifest"]),
            "sha256": manifest_snapshot.sha256,
        },
    }
    _assert_authority_unchanged(context)
    authority.assert_snapshot_unchanged(csv_snapshot, "measured target-load front")
    authority.assert_snapshot_unchanged(manifest_snapshot, "measured front manifest")
    if not _claim_owned(context, owner):
        raise TargetLoadContinuationError("claim ownership was lost before completion publication")
    if context.paths["completion"].is_file():
        _, completion = _strict_document(context.paths["completion"], "target-load completion")
        _expect_keys(completion, {*completion_core, "completed_at_utc"}, "target-load completion")
        if {key: completion[key] for key in completion_core} != completion_core:
            raise TargetLoadContinuationError("existing completion differs from live measured front")
        _nonblank(completion["completed_at_utc"], "completion.completed_at_utc")
        authority.assert_snapshot_unchanged(csv_snapshot, "measured target-load front")
        authority.assert_snapshot_unchanged(manifest_snapshot, "measured front manifest")
        _assert_authority_unchanged(context)
        if not _claim_owned(context, owner):
            raise TargetLoadContinuationError(
                "claim ownership was lost while auditing existing completion"
            )
        return completion
    completion = {
        **completion_core,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    # Completion is deliberately the final publication.
    _publish_no_replace_json(context.paths["completion"], completion, "target-load completion")
    completion_snapshot, committed_completion = _strict_document(
        context.paths["completion"], "target-load completion"
    )
    if committed_completion != completion:
        raise TargetLoadContinuationError("committed completion bytes changed")
    authority.assert_snapshot_unchanged(csv_snapshot, "measured target-load front")
    authority.assert_snapshot_unchanged(manifest_snapshot, "measured front manifest")
    authority.assert_snapshot_unchanged(completion_snapshot, "target-load completion")
    _assert_authority_unchanged(context)
    if not _claim_owned(context, owner):
        raise TargetLoadContinuationError("claim ownership was lost after completion publication")
    return completion


def _assert_execution_argv(context: ContinuationContext, execute: bool) -> None:
    expected = context.runner_argv if execute else context.runner_argv[:-1]
    if _actual_process_argv() != expected:
        raise TargetLoadContinuationError("live argv differs from contracted runner command")


def execute(context: ContinuationContext) -> Mapping[str, Any]:
    _assert_execution_argv(context, True)
    current_owner = _owner("execute")
    _acquire_claim(context, current_owner)
    _publish_decision(context, current_owner)
    try:
        client, _ = _prepare_workspace(context)
        deadline = time.monotonic() + float(context.runtime["overall_timeout_seconds"])
        while True:
            _assert_authority_unchanged(context)
            result = coordinator.advance_workspace_once(
                context.paths["workspace"], client, submit=True
            )
            _assert_authority_unchanged(context)
            if result["status"] == "failed":
                raise TargetLoadContinuationError("target-load coordinator entered failed state")
            if result["status"] == "complete":
                break
            if time.monotonic() >= deadline:
                raise TargetLoadContinuationError("target-load continuation timeout exceeded")
            time.sleep(min(float(context.runtime["poll_interval_seconds"]), 60.0))
        # Close both mutable scheduler state and immutable upstream authority again.
        state, progress = _final_live_state(context, client)
        _strict_upstream_replay(context)
        refreshed = load_continuation_context(context.path)
        if refreshed.document != context.document or refreshed.snapshot.sha256 != context.snapshot.sha256:
            raise TargetLoadContinuationError("continuation authority changed before final publication")
        if not _claim_owned(context, current_owner):
            raise TargetLoadContinuationError("claim ownership was lost before final publication")
        completion = _publish_final_outputs(
            context, state, progress, owner=current_owner
        )
        if not _claim_owned(context, current_owner):
            raise TargetLoadContinuationError("claim ownership was lost after final publication")
        _assert_authority_unchanged(context)
        claim_snapshot, final_claim = _strict_document(
            context.paths["claim"], "completed continuation claim"
        )
        _validate_claim(final_claim, context)
        if final_claim["owner"] != dict(current_owner):
            raise TargetLoadContinuationError(
                "claim ownership was lost before completed claim removal"
            )
        _unlink_bound_snapshot(claim_snapshot, "completed continuation claim")
        return completion
    except BaseException:
        # Keep the claim as durable recovery evidence.  A later exact invocation may
        # adopt it only after this PID is proven inactive.
        raise


def dry_run(context: ContinuationContext) -> Mapping[str, Any]:
    _assert_execution_argv(context, False)
    upstream, _, _ = _strict_upstream_replay(context)
    return {
        "status": "authorized_dry_run",
        "contract_sha256": context.contract_sha256,
        "candidate_count": len(upstream.candidate_ids),
        "candidate_ids": list(upstream.candidate_ids),
        "workspace": str(context.paths["workspace"]),
        "scheduler_project_id": context.scheduler["project_id"],
        "scheduler_active_cap": context.scheduler["project_active_cap"],
        "would_execute": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--continuation-contract", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = load_continuation_context(args.continuation_contract)
        result = execute(context) if args.execute else dry_run(context)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        TargetLoadContinuationError,
        authority.TargetLoadAuthorityError,
        authority_builder.TargetLoadAuthorityBuildError,
        coordinator.TargetLoadCoordinatorError,
        workflow.TargetLoadWorkflowError,
        OSError,
        ValueError,
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
