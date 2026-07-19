"""Run only the v4r9-authorized NSGA-II and validated Pareto-FEA stage.

This entry point deliberately never calls either pipeline supervisor.  It
audits the detached-source activation, expands the exact standard v4 wrapper
command, resumes the legacy durable optimization decision when necessary,
and stops as soon as the optimization decision is strictly complete.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

import build_ipmsm_v2_optimization_activation_v4r9 as activation_builder
import build_ipmsm_v2_target_load_authority_v4r6 as target_authority
import continue_ipmsm_v2_optimization as legacy_optimization
import supervise_ipmsm_v2_pipeline as v3
import supervise_ipmsm_v2_pipeline_v4 as v4


class OptimizationActivationError(RuntimeError):
    """The v4r9 activation is invalid, stale, or unsafe to execute."""


@dataclass(frozen=True)
class ActivationContext:
    source: Path
    source_sha256: str
    canonical_sha256: str
    contract_sha256: str
    source_root: Path
    source_revision: str
    runtime_root: Path
    v4_path: Path
    contract: Any
    authorization: Any
    passed_decision: Path
    stage3_acquisition_completion: Path
    stage3_lineage: Mapping[str, Any]
    remote_deployment_snapshot: Mapping[str, Any]
    audited_inputs: Any
    wrapper_argv: tuple[str, ...]
    decision: Path
    output_dir: Path
    checkpoint_dir: Path
    runner_argv: tuple[str, ...]
    child_environment: Mapping[str, str]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OptimizationActivationError(f"{label} must be an object")
    return dict(value)


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise OptimizationActivationError(
            f"{label} fields changed: expected={sorted(expected)} actual={sorted(value)}"
        )


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OptimizationActivationError(f"{label} must be an exact nonblank string")
    return value


def _absolute_path(value: Any, label: str, *, must_exist: bool = True) -> Path:
    raw = Path(_text(value, label))
    if not raw.is_absolute():
        raise OptimizationActivationError(f"{label} must be absolute")
    try:
        path = activation_builder._c_path(str(raw), label, must_exist=must_exist)
    except Exception as exc:
        raise OptimizationActivationError(str(exc)) from exc
    return path


def _same_path(left: str | Path, right: str | Path) -> bool:
    left_path = Path(left).resolve(strict=False)
    right_path = Path(right).resolve(strict=False)
    try:
        return os.path.samefile(left_path, right_path)
    except (FileNotFoundError, OSError, ValueError):
        return left_path == right_path


def _file_binding(value: Any, label: str) -> Path:
    binding = _mapping(value, label)
    _expect_keys(binding, {"path", "sha256"}, label)
    path = _absolute_path(binding["path"], f"{label}.path")
    digest = _text(binding["sha256"], f"{label}.sha256")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise OptimizationActivationError(f"{label}.sha256 must be lowercase 64-hex")
    if activation_builder._file_sha256(path) != digest:
        raise OptimizationActivationError(f"{label} bytes changed: {path}")
    return path


def _contract_binding(value: Any, label: str) -> tuple[Path, dict[str, Any]]:
    binding = _mapping(value, label)
    _expect_keys(
        binding,
        {"path", "raw_sha256", "canonical_sha256", "contract_sha256"},
        label,
    )
    path = _absolute_path(binding["path"], f"{label}.path")
    payload, document = activation_builder._strict_json(path, label)
    actual = {
        "raw_sha256": activation_builder._sha256_bytes(payload),
        "canonical_sha256": v3._canonical_sha256(document),
        "contract_sha256": document.get("contract_sha256"),
    }
    expected = {name: _text(binding[name], f"{label}.{name}") for name in actual}
    if actual != expected:
        raise OptimizationActivationError(f"{label} identity changed")
    return path, document


def _flag_value(argv: Sequence[str], flag: str, label: str) -> str:
    try:
        return argv[activation_builder._flag_index(argv, flag, label)]
    except activation_builder.OptimizationActivationBuildError as exc:
        raise OptimizationActivationError(str(exc)) from exc


def _flag_path(argv: Sequence[str], flag: str, workdir: Path, label: str) -> Path:
    raw = Path(_flag_value(argv, flag, label))
    return (raw if raw.is_absolute() else workdir / raw).resolve(strict=False)


def _audit_speed_hard_disabled(contract: Any, blocker_file: Path) -> None:
    if not blocker_file.is_file():
        raise OptimizationActivationError("speed blocker must be the committed activation file")
    speed = contract.base_contract.speed
    for argv_name, filename in activation_builder.SPEED_SCRIPT_FILENAMES.items():
        argv = getattr(speed, argv_name)
        raw_script = Path(argv[1])
        script = (
            raw_script if raw_script.is_absolute() else contract.workdir / raw_script
        ).resolve(strict=False)
        expected = (blocker_file / filename).resolve(strict=False)
        if script != expected:
            raise OptimizationActivationError(
                f"successor speed.{argv_name} is not hard-disabled by activation"
            )


def _audit_config(
    value: Any,
    *,
    source_root: Path,
    source_revision: str,
    runtime_root: Path,
    v4_path: Path,
    passed_decision: Path,
    stage3_acquisition_completion: Path,
) -> None:
    config_path = _file_binding(value, "activation.source.config")
    try:
        _, config = activation_builder._read_config(config_path)
    except Exception as exc:
        raise OptimizationActivationError(f"activation build config audit failed: {exc}") from exc
    expected_paths = {
        "source_root": source_root,
        "runtime_root": runtime_root,
        "parent_v4_contract": (
            runtime_root / activation_builder.EXPECTED_PARENT_RELATIVE
        ).resolve(strict=True),
        "passed_decision": passed_decision,
        "stage3_acquisition_completion": stage3_acquisition_completion,
        "output_root": v4_path.parent,
        "python_executable": Path(sys.executable).resolve(strict=True),
    }
    for name, expected in expected_paths.items():
        if not _same_path(Path(str(config[name])), expected):
            raise OptimizationActivationError(f"activation build config {name} changed")
    if config["source_revision"] != source_revision:
        raise OptimizationActivationError("activation build config source_revision changed")
    if config["scheduler_project"] != "PYAEDT_MOTOR_IPMSM_V2":
        raise OptimizationActivationError("activation build config scheduler_project changed")


def _audit_dynamic_sources(value: Any, root: Path, revision: str) -> None:
    sources = _mapping(value, "activation.source.dynamic_sources")
    _expect_keys(
        sources,
        set(activation_builder.DYNAMIC_SOURCE_MODULES),
        "activation.source.dynamic_sources",
    )
    for name, (module_name, relative) in activation_builder.DYNAMIC_SOURCE_MODULES.items():
        binding = _mapping(
            sources[name],
            f"activation.source.dynamic_sources.{name}",
        )
        path = _file_binding(
            binding,
            f"activation.source.dynamic_sources.{name}",
        )
        expected = (root / relative).resolve(strict=True)
        if not _same_path(path, expected):
            raise OptimizationActivationError(
                f"dynamic source {module_name} differs from the exact source_root path"
            )
        try:
            activation_builder._source_binding(
                root,
                revision,
                relative,
                f"v4r9 dynamic source {module_name}",
            )
            module = importlib.import_module(module_name)
            loaded = Path(module.__file__).resolve(strict=True)
        except Exception as exc:
            raise OptimizationActivationError(
                f"dynamic source {module_name} audit failed: {exc}"
            ) from exc
        if not _same_path(loaded, expected) or activation_builder._file_sha256(
            loaded
        ) != binding["sha256"]:
            raise OptimizationActivationError(
                f"loaded dynamic source {module_name} differs from its path/SHA binding"
            )


def _audit_source(value: Any) -> tuple[Path, str]:
    source = _mapping(value, "activation.source")
    _expect_keys(
        source,
        {"root", "revision", "builder", "runner", "dynamic_sources", "config"},
        "activation.source",
    )
    root = _absolute_path(source["root"], "activation.source.root")
    revision = _text(source["revision"], "activation.source.revision")
    try:
        activation_builder.audit_source_root(root, revision)
    except Exception as exc:
        raise OptimizationActivationError(f"detached source audit failed: {exc}") from exc
    builder_path = _file_binding(source["builder"], "activation.source.builder")
    runner_path = _file_binding(source["runner"], "activation.source.runner")
    expected_builder = (root / activation_builder.BUILDER_FILENAME).resolve(strict=True)
    expected_runner = (root / activation_builder.RUNNER_FILENAME).resolve(strict=True)
    if not _same_path(builder_path, expected_builder) or not _same_path(
        Path(activation_builder.__file__).resolve(strict=True), expected_builder
    ):
        raise OptimizationActivationError("loaded activation builder differs from its source pin")
    if not _same_path(runner_path, expected_runner) or not _same_path(
        Path(__file__).resolve(strict=True), expected_runner
    ):
        raise OptimizationActivationError("loaded activation runner differs from its source pin")
    for relative, label in (
        (Path(activation_builder.BUILDER_FILENAME), "activation builder"),
        (Path(activation_builder.RUNNER_FILENAME), "activation runner"),
    ):
        try:
            activation_builder._source_binding(root, revision, relative, label)
        except Exception as exc:
            raise OptimizationActivationError(str(exc)) from exc
    _audit_dynamic_sources(source["dynamic_sources"], root, revision)
    return root, revision


def _audit_scheduler(
    scheduler: Mapping[str, Any], wrapper: Sequence[str], source_revision: str
) -> None:
    expected = activation_builder.activation_scheduler_policy(source_revision)
    if dict(scheduler) != expected:
        raise OptimizationActivationError("activation scheduler policy changed")
    try:
        activation_builder._audit_campaign_defaults()
    except Exception as exc:
        raise OptimizationActivationError(str(exc)) from exc
    expected_flags = {
        "--project": expected["project"],
        "--scheduler-url": expected["url"],
        "--project-active-cap": str(expected["project_active_cap"]),
        "--max-fea-candidates": "12",
        "--env-setup": expected["task_env_setup"],
    }
    for flag, value in expected_flags.items():
        if _flag_value(wrapper, flag, "authorized optimization wrapper") != value:
            raise OptimizationActivationError(f"authorized wrapper {flag} changed")


def _audit_namespace(namespace: Mapping[str, Any], wrapper: Sequence[str]) -> None:
    if dict(namespace) != activation_builder.NAMESPACE:
        raise OptimizationActivationError("activation namespace changed")
    for flag, name in (
        ("--task-prefix", "task_prefix"),
        ("--remote-cases-dir", "remote_cases_dir"),
        ("--result-dir", "result_dir"),
        ("--simulation-dir", "simulation_dir"),
        ("--log-dir", "log_dir"),
    ):
        if _flag_value(wrapper, flag, "authorized optimization wrapper") != namespace[name]:
            raise OptimizationActivationError(f"authorized wrapper {flag} changed")


def load_activation(path: str | Path) -> ActivationContext:
    """Load and re-audit every byte that can authorize the optimization run."""

    source = _absolute_path(path, "activation_contract")
    try:
        payload, document = activation_builder._strict_json(source, "activation contract")
    except Exception as exc:
        raise OptimizationActivationError(str(exc)) from exc
    if payload != activation_builder._canonical_bytes(document):
        raise OptimizationActivationError("activation contract bytes are not canonical")
    _expect_keys(document, {"schema_version", "activation", "contract_sha256"}, "contract")
    if document["schema_version"] != activation_builder.ACTIVATION_SCHEMA_VERSION:
        raise OptimizationActivationError("unsupported activation schema_version")
    activation = _mapping(document["activation"], "activation")
    _expect_keys(
        activation,
        {
            "source",
            "runtime_root",
            "base_contract",
            "v4_contract",
            "passed_decision",
            "stage3_acquisition_completion",
            "stage3_lineage",
            "remote_deployment",
            "authorization",
            "accepted_inputs",
            "model_bundle",
            "scheduler",
            "namespace",
            "optimization",
            "runner",
        },
        "activation",
    )
    unsigned = {
        "schema_version": activation_builder.ACTIVATION_SCHEMA_VERSION,
        "activation": activation,
    }
    expected_contract_hash = v3._canonical_sha256(unsigned)
    if document["contract_sha256"] != expected_contract_hash:
        raise OptimizationActivationError("activation contract_sha256 mismatch")

    expected_pycache_prefix = activation_builder.runner_pycache_prefix(source)
    actual_pycache_prefix = getattr(sys, "pycache_prefix", None)
    if (
        sys.dont_write_bytecode is not True
        or not isinstance(actual_pycache_prefix, str)
        or Path(actual_pycache_prefix).resolve(strict=False) != expected_pycache_prefix
    ):
        raise OptimizationActivationError(
            "activation runner requires contracted -B/-X pycache_prefix isolation"
        )

    source_root, revision = _audit_source(activation["source"])
    runtime_root = _absolute_path(activation["runtime_root"], "activation.runtime_root")
    if not _same_path(runtime_root, activation_builder.EXPECTED_RUNTIME_ROOT):
        raise OptimizationActivationError("activation runtime_root changed")
    base_path, _ = _contract_binding(activation["base_contract"], "activation.base_contract")
    v4_path, _ = _contract_binding(activation["v4_contract"], "activation.v4_contract")
    if not _same_path(base_path.parent, v4_path.parent):
        raise OptimizationActivationError("successor base/v4 contracts are not co-located")
    try:
        contract = v4.load_contract(v4_path)
        v4.audit_contract(contract)
    except Exception as exc:
        raise OptimizationActivationError(f"standard v4 contract audit failed: {exc}") from exc
    if not _same_path(contract.base_contract.source, base_path):
        raise OptimizationActivationError("standard v4 contract binds a different base contract")
    if not _same_path(contract.workdir, source_root):
        raise OptimizationActivationError("standard v4 workdir differs from detached source_root")
    if _same_path(contract.workdir, runtime_root):
        raise OptimizationActivationError("detached source_root and sealed runtime_root must differ")
    _audit_speed_hard_disabled(contract, source)

    passed_decision = _file_binding(activation["passed_decision"], "activation.passed_decision")
    stage3_acquisition_completion = _file_binding(
        activation["stage3_acquisition_completion"],
        "activation.stage3_acquisition_completion",
    )
    try:
        completion_binding, stage3_lineage = (
            activation_builder.audit_passed_decision_provenance(
                passed_decision,
                stage3_acquisition_completion,
                runtime_root,
            )
        )
    except Exception as exc:
        raise OptimizationActivationError(
            f"Stage3 acquisition lineage audit failed: {exc}"
        ) from exc
    if (
        not _same_path(completion_binding.path, stage3_acquisition_completion)
        or activation["stage3_acquisition_completion"]
        != completion_binding.as_mapping()
        or activation["stage3_lineage"] != stage3_lineage
    ):
        raise OptimizationActivationError("activation Stage3 lineage binding changed")
    remote_deployment = _mapping(
        activation["remote_deployment"], "activation.remote_deployment"
    )
    _expect_keys(
        remote_deployment,
        {"required_policy", "observed_at_build"},
        "activation.remote_deployment",
    )
    if remote_deployment["required_policy"] != activation_builder.REMOTE_DEPLOYMENT_POLICY:
        raise OptimizationActivationError("activation remote deployment policy changed")
    remote_snapshot = _mapping(
        remote_deployment["observed_at_build"],
        "activation.remote_deployment.observed_at_build",
    )
    if (
        remote_snapshot.get("source_revision") != revision
        or remote_snapshot.get("project")
        != activation_builder.REMOTE_DEPLOYMENT_POLICY["project"]
        or remote_snapshot.get("auto_pull") is not False
        or remote_snapshot.get("cleanup_globs") != "*.aedtresults"
        or remote_snapshot.get("max_active_tasks")
        != activation_builder.REMOTE_DEPLOYMENT_POLICY["max_active_tasks"]
        or type(remote_snapshot.get("validated_concurrency_limit")) is not int
        or remote_snapshot["validated_concurrency_limit"]
        < activation_builder.REMOTE_DEPLOYMENT_POLICY[
            "minimum_validated_concurrency_limit"
        ]
    ):
        raise OptimizationActivationError(
            "activation remote deployment snapshot changed"
        )
    if not _same_path(contract.base_contract.stage3.decision, passed_decision):
        raise OptimizationActivationError("successor Stage3 decision differs from passed decision")
    if not _same_path(
        _flag_path(
            contract.base_contract.stage3.continuation_argv,
            "--decision-output",
            contract.workdir,
            "successor Stage3 continuation",
        ),
        passed_decision,
    ):
        raise OptimizationActivationError("successor Stage3 argv differs from passed decision")
    try:
        audited_inputs, _ = activation_builder._legacy_inputs(
            contract,
            passed_decision,
            artifact_workdir=runtime_root,
        )
    except Exception as exc:
        raise OptimizationActivationError(str(exc)) from exc
    if activation["accepted_inputs"] != activation_builder.ACCEPTED_INPUTS:
        raise OptimizationActivationError("accepted optimization inputs changed")
    if activation["model_bundle"] != audited_inputs.model_bundle_contract:
        raise OptimizationActivationError("selected passed model bundle changed")

    try:
        authorization = v4.audit_authorization(contract)
    except Exception as exc:
        raise OptimizationActivationError(f"v4 authorization audit failed: {exc}") from exc
    if activation["authorization"] != authorization.record:
        raise OptimizationActivationError("activation authorization record changed")

    optimization = _mapping(activation["optimization"], "activation.optimization")
    _expect_keys(
        optimization,
        {
            "wrapper_argv",
            "decision",
            "output_dir",
            "checkpoint_dir",
            "stop_after",
            "legacy_speed_stage_authorized",
            "target_load_stage_authorized",
        },
        "activation.optimization",
    )
    raw_wrapper = optimization["wrapper_argv"]
    if not isinstance(raw_wrapper, list) or any(
        not isinstance(item, str) or not item for item in raw_wrapper
    ):
        raise OptimizationActivationError("activation optimization wrapper_argv is invalid")
    wrapper = tuple(raw_wrapper)
    expected_wrapper = tuple(
        str(passed_decision) if item == v4.UPSTREAM_PLACEHOLDER else item
        for item in contract.optimization.wrapper_argv_template
    )
    if wrapper != expected_wrapper or v4.UPSTREAM_PLACEHOLDER in wrapper:
        raise OptimizationActivationError("expanded v4 optimization wrapper argv changed")
    if "--execute" in wrapper or "--resume" in wrapper:
        raise OptimizationActivationError("authorized wrapper must not pre-authorize mode flags")
    _audit_scheduler(
        _mapping(activation["scheduler"], "activation.scheduler"),
        wrapper,
        revision,
    )
    _audit_namespace(_mapping(activation["namespace"], "activation.namespace"), wrapper)

    decision = _absolute_path(
        optimization["decision"], "activation.optimization.decision", must_exist=False
    )
    output_dir = _absolute_path(
        optimization["output_dir"], "activation.optimization.output_dir", must_exist=False
    )
    checkpoint_dir = _absolute_path(
        optimization["checkpoint_dir"],
        "activation.optimization.checkpoint_dir",
        must_exist=False,
    )
    if not _same_path(decision, contract.base_contract.optimization.decision):
        raise OptimizationActivationError("activation optimization decision path changed")
    for flag, expected in (
        ("--decision-output", decision),
        ("--output-dir", output_dir),
        ("--checkpoint-dir", checkpoint_dir),
    ):
        if not _same_path(_flag_path(wrapper, flag, contract.workdir, "wrapper"), expected):
            raise OptimizationActivationError(f"activation optimization {flag} path changed")
    if (
        optimization["stop_after"] != "validated_pareto_fea"
        or optimization["legacy_speed_stage_authorized"] is not False
        or optimization["target_load_stage_authorized"] is not False
    ):
        raise OptimizationActivationError("activation stop boundary changed")

    runner = _mapping(activation["runner"], "activation.runner")
    _expect_keys(runner, {"argv", "child_environment"}, "activation.runner")
    child_environment = _mapping(
        runner["child_environment"], "activation.runner.child_environment"
    )
    if child_environment != activation_builder.child_environment(source):
        raise OptimizationActivationError("activation child environment changed")
    raw_runner_argv = runner["argv"]
    if not isinstance(raw_runner_argv, list) or any(
        not isinstance(item, str) or not item for item in raw_runner_argv
    ):
        raise OptimizationActivationError("activation runner argv is invalid")
    expected_runner = (
        str(Path(sys.executable).resolve(strict=True)),
        "-B",
        "-X",
        f"pycache_prefix={activation_builder.runner_pycache_prefix(source)}",
        str(Path(__file__).resolve(strict=True)),
        "--activation-contract",
        str(source),
        "--execute",
    )
    if tuple(raw_runner_argv) != expected_runner:
        raise OptimizationActivationError("activation runner argv changed")

    _audit_config(
        _mapping(activation["source"], "activation.source")["config"],
        source_root=source_root,
        source_revision=revision,
        runtime_root=runtime_root,
        v4_path=v4_path,
        passed_decision=passed_decision,
        stage3_acquisition_completion=stage3_acquisition_completion,
    )
    return ActivationContext(
        source=source,
        source_sha256=activation_builder._sha256_bytes(payload),
        canonical_sha256=v3._canonical_sha256(document),
        contract_sha256=expected_contract_hash,
        source_root=source_root,
        source_revision=revision,
        runtime_root=runtime_root,
        v4_path=v4_path,
        contract=contract,
        authorization=authorization,
        passed_decision=passed_decision,
        stage3_acquisition_completion=stage3_acquisition_completion,
        stage3_lineage=stage3_lineage,
        remote_deployment_snapshot=remote_snapshot,
        audited_inputs=audited_inputs,
        wrapper_argv=wrapper,
        decision=decision,
        output_dir=output_dir,
        checkpoint_dir=checkpoint_dir,
        runner_argv=tuple(raw_runner_argv),
        child_environment=child_environment,
    )


def _actual_process_argv() -> tuple[str, ...]:
    original = getattr(sys, "orig_argv", None)
    if isinstance(original, list) and original and all(isinstance(item, str) for item in original):
        return tuple(original)
    raise OptimizationActivationError("Python did not expose exact original process argv")


def _audit_process_argv(context: ActivationContext, *, execute: bool) -> None:
    expected = context.runner_argv if execute else context.runner_argv[:-1]
    if _actual_process_argv() != expected:
        raise OptimizationActivationError("actual process argv differs from the activation contract")


def _legacy_resume_authority(
    context: ActivationContext,
) -> tuple[argparse.Namespace, Any, dict[str, Any]]:
    try:
        first_legacy = context.wrapper_argv.index("--stage2-decision")
    except ValueError as exc:
        raise OptimizationActivationError(
            "authorized wrapper lost legacy Stage2 arguments"
        ) from exc
    legacy_argv = list(context.wrapper_argv[first_legacy:])
    try:
        args = legacy_optimization.build_parser().parse_args(legacy_argv)
        paths = legacy_optimization.output_paths(args)
        base = legacy_optimization._base_payload(
            args,
            context.audited_inputs,
            paths,
        )
        authorized_contract = legacy_optimization._execution_contract(
            args,
            context.audited_inputs,
            paths,
        )
        authorized_contract["authorization"] = v4.authorization_record(
            context.contract,
            context.authorization.audit,
        )
    except Exception as exc:
        raise OptimizationActivationError(
            f"cannot recompute authorized legacy contract: {exc}"
        ) from exc
    base["execution_contract"] = authorized_contract
    base["contract_sha256"] = legacy_optimization._canonical_sha256(
        authorized_contract
    )
    if not _same_path(args.decision_output, context.decision):
        raise OptimizationActivationError(
            "legacy decision path differs from activation authority"
        )
    return args, paths, base


def _validate_orphan_fresh_claim(context: ActivationContext) -> dict[str, Any]:
    claim = legacy_optimization._claim_path(context.decision)
    recovery = legacy_optimization._recovery_claim_path(context.decision)
    if context.decision.exists() or recovery.exists() or not claim.is_file():
        raise OptimizationActivationError(
            "orphan fresh claim state changed during reconciliation"
        )
    args, paths, expected = _legacy_resume_authority(context)
    try:
        value = legacy_optimization._read_json(claim, "orphan fresh optimization claim")
        expected_keys = {
            "schema_version",
            "decision_output",
            "decision_sha256",
            "contract_sha256",
            "original_owner",
            "owner",
        }
        owner = legacy_optimization._mapping(
            value.get("owner"), "orphan fresh claim owner"
        )
        original_owner = legacy_optimization._mapping(
            value.get("original_owner"), "orphan fresh claim original_owner"
        )
        failures = []
        if set(value) != expected_keys:
            failures.append("fields")
        if value.get("schema_version") != legacy_optimization.SCHEMA_VERSION:
            failures.append("schema_version")
        if not _same_path(
            Path(str(value.get("decision_output") or "")), context.decision
        ):
            failures.append("decision_output")
        legacy_optimization._valid_sha256(
            value.get("decision_sha256"), "orphan fresh claim decision_sha256"
        )
        if value.get("contract_sha256") != expected["contract_sha256"]:
            failures.append("contract_sha256")
        if (
            owner != original_owner
            or set(owner) != {"hostname", "pid", "mode", "nonce"}
            or owner.get("mode") != "execute"
        ):
            failures.append("owner")
        if failures:
            raise OptimizationActivationError(
                "orphan fresh optimization claim changed: " + ", ".join(failures)
            )
        legacy_optimization._require_owner_inactive(
            owner, "orphan fresh optimization claim owner"
        )
        legacy_optimization._assert_new_outputs_fresh(paths)
        if args.resume:
            raise OptimizationActivationError("fresh legacy authority unexpectedly resumes")
    except OptimizationActivationError:
        raise
    except Exception as exc:
        raise OptimizationActivationError(
            f"orphan fresh optimization claim is not recoverable: {exc}"
        ) from exc
    return value


def _validate_orphan_recovery_lock(
    context: ActivationContext,
    decision: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    recovery = legacy_optimization._recovery_claim_path(context.decision)
    if not context.decision.is_file() or not recovery.is_file():
        raise OptimizationActivationError(
            "stale recovery-lock state changed during reconciliation"
        )
    args, _paths, expected = _legacy_resume_authority(context)
    try:
        prior = legacy_optimization._validate_prior_decision(args, expected)
        if prior != dict(decision):
            raise OptimizationActivationError(
                "recovery-lock decision changed during legacy validation"
            )
        original_owner = legacy_optimization._mapping(
            prior.get("owner"), "recovery-lock original owner"
        )
        legacy_optimization._require_owner_inactive(
            original_owner, "recovery-lock original owner"
        )
        value = legacy_optimization._read_json(
            recovery, "orphan stale-claim recovery lock"
        )
        if set(value) != {
            "schema_version",
            "decision_output",
            "decision_sha256",
            "owner",
        }:
            raise OptimizationActivationError("recovery-lock fields changed")
        if (
            value.get("schema_version") != legacy_optimization.SCHEMA_VERSION
            or not _same_path(
                Path(str(value.get("decision_output") or "")), context.decision
            )
            or value.get("decision_sha256")
            != legacy_optimization._sha256(context.decision)
        ):
            raise OptimizationActivationError("recovery-lock identity changed")
        owner = legacy_optimization._mapping(
            value.get("owner"), "recovery-lock owner"
        )
        if (
            set(owner) != {"hostname", "pid", "mode", "nonce"}
            or owner.get("mode") != "resume"
        ):
            raise OptimizationActivationError("recovery-lock owner schema changed")
        legacy_optimization._require_owner_inactive(owner, "recovery-lock owner")
        claim = legacy_optimization._claim_path(context.decision)
        if claim.exists():
            legacy_optimization._validate_stale_claim(args, prior)
    except OptimizationActivationError:
        raise
    except Exception as exc:
        raise OptimizationActivationError(
            f"orphan stale-claim recovery lock is not recoverable: {exc}"
        ) from exc
    status = str(prior["status"])
    return value, "recover_complete" if status == "complete" else "resume"


def _decision_state(context: ActivationContext) -> tuple[str, dict[str, Any] | None]:
    claim = legacy_optimization._claim_path(context.decision)
    recovery = legacy_optimization._recovery_claim_path(context.decision)
    if not context.decision.is_file():
        if recovery.exists():
            raise OptimizationActivationError(
                "optimization decision is absent but recovery state exists"
            )
        if claim.exists():
            _validate_orphan_fresh_claim(context)
            return "recover_fresh_claim", None
        return "fresh", None
    try:
        decision = v3.audit_decision(
            context.decision,
            schema_version=v3.OPTIMIZATION_DECISION_SCHEMA_VERSION,
            allowed_statuses={"optimization_started", "pareto_fea_started", "complete", "failed"},
            workdir=context.contract.workdir,
        )
        v4.audit_optimization_decision_authorization(decision, context.authorization)
    except Exception as exc:
        raise OptimizationActivationError(f"optimization decision audit failed: {exc}") from exc
    status = str(decision["status"])
    if status == "failed":
        detail = str(decision.get("error") or "no recorded error")
        raise OptimizationActivationError(f"optimization decision is failed: {detail[:500]}")
    if status == "complete":
        try:
            target_authority.audit_completed_upstream(context.v4_path)
        except Exception as exc:
            raise OptimizationActivationError(
                f"completed optimization is not target-load consumable: {exc}"
            ) from exc
    if recovery.exists():
        _, recovered_state = _validate_orphan_recovery_lock(context, decision)
        return f"recover_recovery_lock:{recovered_state}", decision
    if status == "complete":
        return ("recover_complete" if claim.exists() else "complete"), decision
    return "resume", decision


def _reconcile_durable_state(
    context: ActivationContext,
    state: str,
    decision: Mapping[str, Any] | None,
) -> tuple[str, int]:
    if state == "recover_fresh_claim":
        claim = legacy_optimization._claim_path(context.decision)
        before = legacy_optimization._sha256(claim)
        _validate_orphan_fresh_claim(context)
        if legacy_optimization._sha256(claim) != before:
            raise OptimizationActivationError(
                "orphan fresh claim changed before cleanup"
            )
        claim.unlink()
        return "fresh", 1
    prefix = "recover_recovery_lock:"
    if state.startswith(prefix):
        if decision is None:
            raise OptimizationActivationError("recovery-lock decision disappeared")
        recovery = legacy_optimization._recovery_claim_path(context.decision)
        before = legacy_optimization._sha256(recovery)
        _value, resumed_state = _validate_orphan_recovery_lock(context, decision)
        if resumed_state != state[len(prefix) :] or legacy_optimization._sha256(
            recovery
        ) != before:
            raise OptimizationActivationError(
                "recovery lock changed before cleanup"
            )
        recovery.unlink()
        return resumed_state, 1
    return state, 0


def _last_json(stdout: str, label: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise OptimizationActivationError(f"{label} did not emit a JSON object")


def _run_child(
    argv: Sequence[str],
    *,
    workdir: Path,
    label: str,
    capture_output: bool,
    child_environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    environment_contract = dict(child_environment)
    if (
        set(environment_contract)
        != {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}
        or environment_contract["PYTHONDONTWRITEBYTECODE"] != "1"
        or Path(environment_contract["PYTHONPYCACHEPREFIX"]).name
        != activation_builder.CHILD_PYCACHE_DIRNAME
    ):
        raise OptimizationActivationError(
            "child environment differs from activation authority"
        )
    environment = os.environ.copy()
    environment.update(child_environment)
    completed = subprocess.run(
        list(argv),
        cwd=workdir,
        env=environment,
        shell=False,
        check=False,
        capture_output=capture_output,
        text=True,
    )
    if completed.returncode != 0:
        tail = ""
        if capture_output:
            lines = (completed.stderr or "").splitlines()
            tail = next((line.strip() for line in reversed(lines) if line.strip()), "")
        raise OptimizationActivationError(
            f"{label} returned {completed.returncode}" + (f": {tail[:500]}" if tail else "")
        )
    return completed


def _run_wrapper(context: ActivationContext, state: str) -> None:
    resume = state in {"resume", "recover_complete"}
    dry_argv = (*context.wrapper_argv, *(["--resume"] if resume else []))
    dry = _run_child(
        dry_argv,
        workdir=context.runtime_root,
        label="authorized optimization dry-run",
        capture_output=True,
        child_environment=context.child_environment,
    )
    proof = _last_json(dry.stdout, "authorized optimization dry-run")
    if resume:
        if proof.get("mode") != "resume-dry-run" or proof.get("status") not in {
            "optimization_started",
            "pareto_fea_started",
            "complete",
        }:
            raise OptimizationActivationError("optimization resume dry-run returned unexpected state")
    elif proof.get("mode") != "dry-run" or proof.get("status") != "planned":
        raise OptimizationActivationError("optimization fresh dry-run returned unexpected state")
    _run_child(
        (*dry_argv, "--execute"),
        workdir=context.runtime_root,
        label="authorized optimization execute",
        capture_output=False,
        child_environment=context.child_environment,
    )


def _audit_live_remote_deployment(context: ActivationContext) -> dict[str, Any]:
    try:
        current = activation_builder.audit_remote_deployment(context.source_revision)
    except Exception as exc:
        raise OptimizationActivationError(
            f"live remote deployment audit failed: {exc}"
        ) from exc
    if current != dict(context.remote_deployment_snapshot):
        raise OptimizationActivationError(
            "live remote deployment differs from the sealed build snapshot"
        )
    return current


def run(context: ActivationContext, *, execute: bool) -> dict[str, Any]:
    state, decision = _decision_state(context)
    if not execute:
        if state.startswith("recover_recovery_lock:"):
            dry_status = "ready_to_recover_stale_claim_recovery_lock"
        else:
            dry_status = {
                "fresh": "ready_to_execute",
                "recover_fresh_claim": "ready_to_recover_fresh_claim",
                "resume": "ready_to_resume",
                "recover_complete": "ready_to_recover_complete_commit",
                "complete": "complete",
            }[state]
        return {
            "mode": "dry-run",
            "status": dry_status,
            "writes_performed": 0,
            "source_revision": context.source_revision,
            "activation_contract": str(context.source),
            "v4_contract": str(context.v4_path),
            "passed_decision": str(context.passed_decision),
            "optimization_decision": str(context.decision),
            "stop_after": "validated_pareto_fea",
            "remote_deployment_preflight": "planned_not_requested",
        }
    _audit_live_remote_deployment(context)
    state, reconciliation_writes = _reconcile_durable_state(
        context,
        state,
        decision,
    )
    if state != "complete":
        _run_wrapper(context, state)
        refreshed = load_activation(context.source)
        final_state, decision = _decision_state(refreshed)
        if final_state != "complete":
            raise OptimizationActivationError(
                f"optimization wrapper returned without complete Pareto-FEA state: {final_state}"
            )
        context = refreshed
    _audit_live_remote_deployment(context)
    return {
        "mode": "execute",
        "status": "complete",
        "writes_performed": reconciliation_writes + (0 if state == "complete" else 1),
        "reconciliation_writes": reconciliation_writes,
        "source_revision": context.source_revision,
        "activation_contract": str(context.source),
        "v4_contract": str(context.v4_path),
        "optimization_decision": str(context.decision),
        "optimization_decision_sha256": activation_builder._file_sha256(context.decision),
        "decision_status": decision["status"] if decision else "complete",
        "stop_after": "validated_pareto_fea",
        "legacy_speed_stage_started": False,
        "target_load_stage_started": False,
        "remote_deployment_preflight": "passed_before_and_after",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation-contract", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = load_activation(args.activation_contract)
        _audit_process_argv(context, execute=args.execute)
        result = run(context, execute=args.execute)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        OptimizationActivationError,
        activation_builder.OptimizationActivationBuildError,
        OSError,
        ValueError,
    ) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
