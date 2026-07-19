"""Build a detached-source v4r9 authority for optimization through Pareto FEA.

The sealed LF runtime remains read-only input.  This builder emits a successor
v1 base contract, a standard v4 optimization-authorization contract, the
existing declaration/confirmation/receipt chain, and a small activation
contract consumed by :mod:`continue_ipmsm_v2_optimization_v4r9`.

The successor deliberately does not invoke the legacy pipeline supervisor:
that supervisor would continue into its speed stage.  The v4r9 runner executes
only the exact authorized optimization wrapper and therefore stops after the
strict Pareto-FEA decision becomes complete.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

import authorize_ipmsm_v2_optimization_v4 as optimizer_authorizer
import confirm_ipmsm_v2_optimization_inputs as optimizer_confirmation
import confirm_ipmsm_v2_target_load_inputs_v4r6 as publication
import continue_ipmsm_v2_optimization as legacy_optimization
import generate_ipmsm_v2_adaptive_batch as adaptive_generator
import ipmsm_optimization
import submit_ipmsm_v2_campaign as campaign_submitter
import supervise_ipmsm_v2_pipeline as v3
import supervise_ipmsm_v2_pipeline_v4 as v4


CONFIG_SCHEMA_VERSION = "ipmsm-v2-optimization-activation-v4r9-build-config-v1"
ACTIVATION_SCHEMA_VERSION = "ipmsm-v2-optimization-activation-v4r9-contract-v1"
EXPECTED_RUNTIME_ROOT = Path(r"C:\Users\peets\NEC\pyaedt_motor_blob_lf_325")
EXPECTED_PARENT_RELATIVE = Path("simul_log_smoke/v4r5_native/contract.json")
ACTIVATION_RELATIVE_ROOT = Path("simul_log_smoke/v4r9_optimization_activation")
OPTIMIZATION_RELATIVE_ROOT = Path("collected/ipmsm_v2_optimization_v4r9")
CHECKPOINT_RELATIVE_ROOT = ACTIVATION_RELATIVE_ROOT / "nsga2_checkpoints"

BASE_FILENAME = "base_contract.json"
V4_FILENAME = "contract.json"
ACTIVATION_FILENAME = "activation_contract.json"
DECLARATION_FILENAME = "declaration.json"
CONFIRMATION_FILENAME = "confirmation.json"
RECEIPT_FILENAME = "authorization_receipt.json"
DECISION_FILENAME = "optimization_decision.json"
RUNNER_FILENAME = "continue_ipmsm_v2_optimization_v4r9.py"
BUILDER_FILENAME = Path(__file__).name
SPEED_SCRIPT_FILENAMES = {
    "plan_argv": "generate_ipmsm_second_pass_cases.py",
    "campaign_argv": "run_ipmsm_v2_campaign.py",
    "rank_argv": "rank_ipmsm_second_pass_profiles.py",
}
DYNAMIC_SOURCE_MODULES = {
    "adaptive_batch": (
        "generate_ipmsm_v2_adaptive_batch",
        Path("generate_ipmsm_v2_adaptive_batch.py"),
    ),
    "aedt_attach_client": (
        "module.aedt_attach_client",
        Path("module/aedt_attach_client.py"),
    ),
}
STAGE3_ACQUISITION_RELATIVE_ROOT = Path("simul_log_smoke/v4r10_stage3_acquisition")
STAGE3_ACQUISITION_CONTRACT_FILENAME = "contract.json"
STAGE3_ACQUISITION_COMPLETION_FILENAME = "completion.json"
EXPECTED_STAGE3_ACQUISITION_CONTRACT_RAW_SHA256 = (
    "b43621417baafb8e24983f2647f75e0f955d33a809fc8dbbcf0ce5794f43cab6"
)
STAGE3_ACQUISITION_CONTRACT_SCHEMA_VERSION = (
    "ipmsm-v2-stage3-acquisition-v4r10-contract-v1"
)
STAGE3_ACQUISITION_COMPLETION_SCHEMA_VERSION = (
    "ipmsm-v2-stage3-acquisition-v4r10-completion-v1"
)
ADAPTIVE_CASE_MANIFEST_SCHEMA_VERSION = "ipmsm_v2_adaptive_enrichment_batch_v1"
ADAPTIVE_R2_HISTORY_SCHEMA_VERSION = "ipmsm_v2_adaptive_r2_history_v1"
RUNNER_PYCACHE_DIRNAME = "v4r9_runner_pycache"
CHILD_PYCACHE_DIRNAME = "v4r9_child_pycache"
REMOTE_DEPLOYMENT_POLICY = {
    "project": "PYAEDT_MOTOR_IPMSM_V2",
    "repository_key": "pyaedt_motor",
    "repository_url": "https://github.com/Schwalbe262/pyaedt_motor.git",
    "repository_subdir": "pyaedt_motor",
    "accounts": ["dhj02", "dw16", "harry261", "jji0930", "r1jae262"],
    "deployment_status": "deployed",
    "auto_pull": False,
    "cleanup_globs": "*.aedtresults",
    "max_active_tasks": 300,
    "minimum_validated_concurrency_limit": 300,
    "aedt_backend": "standalone",
    "setup": (
        "source /etc/profile.d/lmod.sh 2>/dev/null || true\n"
        "module load ansys-electronics/v252 2>/dev/null || "
        "export ANSYSEM_ROOT252=/opt/ohpc/pub/Electronics/v252/Linux64\n"
        "export FLEXLM_TIMEOUT=3000000"
    ),
    "request_timeout_seconds": 10.0,
}

EXPECTED_PARENT = {
    "raw_sha256": "39c0193b8cf0d9a91cb4db5ab6447a840b32c3c0fd28a25f9d30846156118c04",
    "canonical_sha256": "f9bf606157c4454ff36b367a4cf066269d00ec38c4df22299929361b8fb6f5fc",
    "contract_sha256": "3d304b8c219867366a773fea46f8a8ef0f41b40779e599da13c7749efe0cfa46",
}
EXPECTED_PARENT_BASE = {
    "raw_sha256": "f110014f9ee94cd1a720791b98713dd35790443a4fa957c814b3b3cf18e4d959",
    "canonical_sha256": "cb5eb160bd1ebc359585045a2518d52061f03a2dbd8fc958916ceb1d1dd909f9",
    "contract_sha256": "4e5963a6f7a3ecc7a1ea2926ac40067ae9af6c04d76636dfe2e59d427eaaa7f3",
}

SCHEDULER_POLICY = {
    "url": "http://127.0.0.1:8002",
    "endpoint": "/api/tasks",
    "project_active_cap": 300,
    "scheduling_profile": "fea_bursty",
    "required_capability": "conda:pyaedt2026v1",
    "env_profile": "pyaedt2026v1",
    "env_setup": "module load ansys-electronics/v252",
    "aedt_backend": "standalone",
    "max_workers_per_node": 0,
}
NAMESPACE = {
    "task_prefix": "ipmsm-v2-pareto-fea-v4r9",
    "remote_cases_dir": "remote/ipmsm_v2_pareto_fea_v4r9",
    "result_dir": "simul_log/ipmsm_v2_pareto_fea_v4r9",
    "simulation_dir": "simulation/ipmsm_v2_pareto_fea_v4r9",
    "log_dir": "simul_log_scheduler/ipmsm_v2_pareto_fea_v4r9_logs",
}
ACCEPTED_INPUTS = {
    "operating_points": [
        {
            "name": "rated_torque",
            "speed_rpm": 1200.0,
            "target_kind": "torque",
            "target_torque_nm": 65.1,
            "duty_weight": 0.5,
        },
        {
            "name": "rated_power_at_max_speed",
            "speed_rpm": 5000.0,
            "target_kind": "power",
            "target_power_w": 7500.0,
            "duty_weight": 0.5,
        },
    ],
    "inverter": {"vdc_v": 200.0, "phase_peak_current_limit_a": 137.8},
    "winding": {"series_turns_per_phase": 48},
    "nsga2": {
        "population_size": 160,
        "max_generations": 300,
        "seeds": [42, 43, 44],
        "max_fea_candidates": 12,
    },
    "volume_definition": dict(optimizer_confirmation.VOLUME_DEFINITION),
}

PIPELINE_PATH_FIELDS = {
    "stage1": {"case_plan", "output_dir", "result", "validation", "model_dir", "metadata", "r2"},
    "stage2": {"decision"},
    "stage3": {"prior_plan", "prior_manifest", "plan", "manifest", "decision"},
    "optimization": {"decision"},
    "speed": {"plan", "output_dir", "result", "rank", "top", "marker"},
}
LOCAL_ARGV_PATH_FLAGS = {
    "--beta-calibration-manifest",
    "--beta-case-plan",
    "--beta-results",
    "--beta-summary",
    "--case-plan",
    "--cases",
    "--checkpoint-dir",
    "--combined-output-dir",
    "--data",
    "--decision-output",
    "--exclude-case-plan",
    "--manifest-output",
    "--model-dir",
    "--optimization-spec",
    "--output",
    "--output-dir",
    "--source-cases",
    "--source-results",
    "--spec",
    "--stage1-case-plan",
    "--stage1-metadata",
    "--stage1-r2",
    "--stage1-result",
    "--stage1-runner-pid-file",
    "--stage1-validation",
    "--stage1-watcher-pid-file",
    "--stage2-case-plan",
    "--stage2-decision",
    "--stage2-failed-decision",
    "--stage2-output-dir",
    "--stage3-manifest-output",
    "--strict-candidate-results",
    "--strict-reference-results",
    "--strict-speed-plan",
    "--summary",
    "--top-profiles-output",
    "--verification-output",
}


class OptimizationActivationBuildError(RuntimeError):
    """The detached optimization authority could not be proven exactly."""


@dataclass(frozen=True)
class FileBinding:
    path: Path
    sha256: str

    def as_mapping(self) -> dict[str, str]:
        return {"path": str(self.path), "sha256": self.sha256}


@dataclass(frozen=True)
class BuildPaths:
    root: Path
    base_contract: Path
    v4_contract: Path
    activation_contract: Path
    declaration: Path
    confirmation: Path
    receipt: Path
    decision: Path
    shared_lock: Path
    stage1_workspace: Path
    optimization_output: Path
    checkpoint_dir: Path


@dataclass(frozen=True)
class BuildPlan:
    config_path: Path
    config_sha256: str
    source_root: Path
    source_revision: str
    runtime_root: Path
    python_executable: Path
    parent_contract: Any
    passed_decision: FileBinding
    stage3_acquisition_completion: FileBinding
    stage3_lineage: Mapping[str, Any]
    remote_deployment_snapshot: Mapping[str, Any]
    audited_inputs: Any
    paths: BuildPaths
    source_pins: Mapping[str, FileBinding]
    base_document: Mapping[str, Any]
    v4_document: Mapping[str, Any]
    authority: Mapping[str, str]
    scheduler_project: str


@dataclass(frozen=True)
class SealedParentAudit:
    source: Path
    base_contract: Any
    immutable_inputs: tuple[FileBinding, ...]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OptimizationActivationBuildError(f"{label} must be an object")
    return dict(value)


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise OptimizationActivationBuildError(
            f"{label} fields changed: expected={sorted(expected)} actual={sorted(value)}"
        )


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise OptimizationActivationBuildError(f"{label} must be an exact nonblank string")
    return value


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return optimizer_confirmation.canonical_json_bytes(value)


def _strict_json(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        payload = path.read_bytes()
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise OptimizationActivationBuildError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise OptimizationActivationBuildError(f"{label} must contain a JSON object")
    return payload, value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _c_path(value: Any, label: str, *, must_exist: bool = True) -> Path:
    raw = Path(_text(value, label))
    if not raw.is_absolute():
        raise OptimizationActivationBuildError(f"{label} must be absolute")
    try:
        path = publication._require_c_local(raw.absolute(), label)
        publication._audit_parent_chain(path, label)
    except (OSError, publication.TargetLoadAuthorityError) as exc:
        raise OptimizationActivationBuildError(str(exc)) from exc
    if must_exist and not path.exists():
        raise OptimizationActivationBuildError(f"{label} is missing: {path}")
    return path.resolve(strict=must_exist)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _source_revision(value: Any) -> str:
    revision = _text(value, "source_revision")
    if revision != revision.lower():
        raise OptimizationActivationBuildError(
            "source_revision must already be lowercase canonical 40-hex"
        )
    return revision


def runner_pycache_prefix(activation_contract: Path) -> Path:
    return (activation_contract / RUNNER_PYCACHE_DIRNAME).resolve(strict=False)


def child_environment(activation_contract: Path) -> dict[str, str]:
    return {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(
            (activation_contract / CHILD_PYCACHE_DIRNAME).resolve(strict=False)
        ),
    }


def remote_task_env_setup(source_revision: str) -> str:
    revision = _source_revision(source_revision)
    return "\n".join(
        (
            "module load ansys-electronics/v252",
            "export PYTHONDONTWRITEBYTECODE=1",
            (
                "export PYTHONPYCACHEPREFIX="
                "/tmp/pyaedt_motor_pycache_${SLURM_SCHED_TASK_ID}"
            ),
            f'test "$(git rev-parse HEAD)" = \'{revision}\'',
            "git diff --quiet",
            "git diff --cached --quiet",
            (
                "test -z \"$(git ls-files --others --exclude-standard -- "
                "'*.py' '*.pyd' '*.so')\""
            ),
            (
                "test -z \"$(git ls-files --others --ignored --exclude-standard -- "
                "'*.py' '*.pyd' '*.so')\""
            ),
        )
    )


def activation_scheduler_policy(source_revision: str) -> dict[str, Any]:
    return {
        **SCHEDULER_POLICY,
        "project": "PYAEDT_MOTOR_IPMSM_V2",
        "task_env_setup": remote_task_env_setup(source_revision),
    }


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:500]
        raise OptimizationActivationBuildError(f"git {' '.join(args)} failed: {detail}")
    return result


def audit_source_root(root: Path, revision: str) -> dict[str, str]:
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise OptimizationActivationBuildError("source_revision must be a lowercase 40-hex commit")
    repository = Path(
        _git(root, "rev-parse", "--show-toplevel").stdout.decode("utf-8").strip()
    ).resolve(strict=True)
    if repository != root.resolve(strict=True):
        raise OptimizationActivationBuildError("source_root must be the exact Git repository root")
    head = _git(root, "rev-parse", "HEAD").stdout.decode("ascii").strip().lower()
    if head != revision:
        raise OptimizationActivationBuildError("source_root HEAD differs from source_revision")
    if _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    ).stdout:
        raise OptimizationActivationBuildError(
            "source_root tree is dirty or contains untracked/ignored files"
        )
    if _git(root, "symbolic-ref", "-q", "HEAD", check=False).returncode == 0:
        raise OptimizationActivationBuildError("source_root must be a detached exact-commit checkout")
    tree = _git(root, "rev-parse", f"{revision}^{{tree}}").stdout.decode("ascii").strip()
    return {"revision": revision, "tree": tree}


def _git_blob(root: Path, revision: str, relative: Path) -> bytes:
    return _git(root, "show", f"{revision}:{relative.as_posix()}").stdout


def _source_binding(root: Path, revision: str, relative: Path, label: str) -> FileBinding:
    path = (root / relative).resolve(strict=True)
    payload = path.read_bytes()
    if _git_blob(root, revision, relative) != payload:
        raise OptimizationActivationBuildError(
            f"{label} working bytes differ from source_revision: {relative.as_posix()}"
        )
    return FileBinding(path, _sha256_bytes(payload))


def _read_config(path: Path) -> tuple[bytes, dict[str, Any]]:
    payload, config = _strict_json(path, "v4r9 optimization build config")
    if payload != _canonical_bytes(config):
        raise OptimizationActivationBuildError("build config must use canonical JSON bytes")
    _expect_keys(
        config,
        {
            "schema_version",
            "source_root",
            "source_revision",
            "runtime_root",
            "parent_v4_contract",
            "passed_decision",
            "stage3_acquisition_completion",
            "output_root",
            "python_executable",
            "scheduler_project",
            "authority",
        },
        "build config",
    )
    if config["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise OptimizationActivationBuildError("unsupported build config schema_version")
    return payload, config


def _binding(path: Path) -> FileBinding:
    resolved = path.resolve(strict=True)
    return FileBinding(resolved, _file_sha256(resolved))


def _contract_identity(path: Path, document: Mapping[str, Any]) -> dict[str, str]:
    return {
        "path": str(path),
        "raw_sha256": _sha256_bytes(_canonical_bytes(document)),
        "canonical_sha256": v3._canonical_sha256(document),
        "contract_sha256": str(document["contract_sha256"]),
    }


def _flag_index(argv: Sequence[str], flag: str, label: str) -> int:
    positions = [index for index, value in enumerate(argv) if value == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise OptimizationActivationBuildError(f"{label} must contain one {flag}")
    return positions[0] + 1


def _replace_flag(argv: list[str], flag: str, value: str, label: str) -> None:
    argv[_flag_index(argv, flag, label)] = value


def _resolved_flag(argv: Sequence[str], flag: str, workdir: Path, label: str) -> Path:
    value = Path(argv[_flag_index(argv, flag, label)])
    return (value if value.is_absolute() else workdir / value).resolve(strict=False)


def _runtime_path(value: Any, runtime_root: Path, label: str) -> str:
    path = Path(_text(value, label))
    return str((path if path.is_absolute() else runtime_root / path).resolve(strict=False))


def _absolutize_legacy_pipeline(
    pipeline: dict[str, Any], *, runtime_root: Path, source_root: Path
) -> None:
    """Move contract semantics to a clean workdir without moving sealed data."""

    immutable = pipeline.get("immutable_inputs")
    if not isinstance(immutable, list):
        raise OptimizationActivationBuildError("parent immutable_inputs changed")
    for index, item in enumerate(immutable):
        artifact = _mapping(item, f"parent immutable_inputs[{index}]")
        artifact["path"] = _runtime_path(
            artifact.get("path"), runtime_root, f"parent immutable_inputs[{index}].path"
        )
        immutable[index] = artifact

    for section_name, fields in PIPELINE_PATH_FIELDS.items():
        section = _mapping(pipeline.get(section_name), f"parent {section_name}")
        for field in fields:
            section[field] = _runtime_path(
                section.get(field), runtime_root, f"parent {section_name}.{field}"
            )
        for argv_name, raw_argv in tuple(section.items()):
            if not (
                argv_name in {"argv", "argv_template"}
                or argv_name.endswith("_argv")
                or argv_name.endswith("_argv_template")
            ):
                continue
            if not isinstance(raw_argv, list) or len(raw_argv) < 2:
                raise OptimizationActivationBuildError(
                    f"parent {section_name}.{argv_name} is invalid"
                )
            argv = list(raw_argv)
            for index, token in enumerate(argv[:-1]):
                if token not in LOCAL_ARGV_PATH_FLAGS:
                    continue
                value = argv[index + 1]
                if value == v3.UPSTREAM_PLACEHOLDER or value.startswith("--"):
                    continue
                argv[index + 1] = _runtime_path(
                    value,
                    runtime_root,
                    f"parent {section_name}.{argv_name} {token}",
                )
            section[argv_name] = argv
        pipeline[section_name] = section
    pipeline["workdir"] = str(source_root)


def _hard_disable_speed_argv(pipeline: dict[str, Any], blocker_file: Path) -> None:
    speed = _mapping(pipeline.get("speed"), "successor speed")
    for argv_name, filename in SPEED_SCRIPT_FILENAMES.items():
        raw_argv = speed.get(argv_name)
        if not isinstance(raw_argv, list) or len(raw_argv) < 2:
            raise OptimizationActivationBuildError(f"successor speed.{argv_name} is invalid")
        argv = list(raw_argv)
        if Path(argv[1]).name.lower() != filename.lower():
            raise OptimizationActivationBuildError(
                f"successor speed.{argv_name} executable changed"
            )
        argv[1] = str(blocker_file / filename)
        speed[argv_name] = argv
    pipeline["speed"] = speed


def _audit_parent(path: Path) -> SealedParentAudit:
    payload, document = _strict_json(path, "sealed parent v4 contract")
    actual = {
        "raw_sha256": _sha256_bytes(payload),
        "canonical_sha256": v3._canonical_sha256(document),
        "contract_sha256": document.get("contract_sha256"),
    }
    if actual != EXPECTED_PARENT:
        raise OptimizationActivationBuildError("sealed parent v4 identity changed")
    pipeline = _mapping(document.get("pipeline"), "sealed parent pipeline")
    workdir = Path(_text(pipeline.get("workdir"), "sealed parent workdir")).resolve(strict=True)
    base_binding = _mapping(pipeline.get("base_contract"), "sealed parent base binding")
    _expect_keys(
        base_binding,
        {"path", "raw_sha256", "canonical_sha256", "contract_sha256"},
        "sealed parent base binding",
    )
    raw_base_path = Path(_text(base_binding["path"], "sealed parent base path"))
    base_path = (
        raw_base_path if raw_base_path.is_absolute() else workdir / raw_base_path
    ).resolve(strict=True)
    base_payload, base_document = _strict_json(base_path, "sealed parent base contract")
    base_actual = {
        "raw_sha256": _sha256_bytes(base_payload),
        "canonical_sha256": v3._canonical_sha256(base_document),
        "contract_sha256": base_document.get("contract_sha256"),
    }
    expected_binding = {
        "raw_sha256": base_binding["raw_sha256"],
        "canonical_sha256": base_binding["canonical_sha256"],
        "contract_sha256": base_binding["contract_sha256"],
    }
    if base_actual != EXPECTED_PARENT_BASE or expected_binding != EXPECTED_PARENT_BASE:
        raise OptimizationActivationBuildError("sealed parent base identity changed")
    base = v3.load_contract(base_path)
    v3.audit_immutable_inputs(base)
    raw_immutable = pipeline.get("immutable_inputs")
    if not isinstance(raw_immutable, list) or not raw_immutable:
        raise OptimizationActivationBuildError("sealed parent immutable inputs changed")
    immutable: list[FileBinding] = []
    for index, raw in enumerate(raw_immutable):
        item = _mapping(raw, f"sealed parent immutable_inputs[{index}]")
        _expect_keys(item, {"path", "sha256"}, f"sealed parent immutable_inputs[{index}]")
        raw_item_path = Path(_text(item["path"], f"sealed parent immutable_inputs[{index}].path"))
        item_path = (
            raw_item_path if raw_item_path.is_absolute() else workdir / raw_item_path
        ).resolve(strict=True)
        binding = FileBinding(
            item_path,
            _text(item["sha256"], f"sealed parent immutable_inputs[{index}].sha256"),
        )
        if _file_sha256(binding.path) != binding.sha256:
            raise OptimizationActivationBuildError(f"sealed parent input changed: {binding.path}")
        immutable.append(binding)
    if not any(item.path == base_path and item.sha256 == base_actual["raw_sha256"] for item in immutable):
        raise OptimizationActivationBuildError("sealed parent v4 does not bind its exact base")
    return SealedParentAudit(
        source=path.resolve(strict=True),
        base_contract=base,
        immutable_inputs=tuple(immutable),
    )


def assert_accepted_spec(spec: Any) -> dict[str, Any]:
    points = list(spec.operating_points)
    if len(points) != 2:
        raise OptimizationActivationBuildError("accepted v1 optimization requires two points")
    expected_points = ACCEPTED_INPUTS["operating_points"]
    for actual, expected in zip(points, expected_points):
        if (
            actual.name != expected["name"]
            or actual.target_kind != expected["target_kind"]
            or float(actual.speed_rpm) != float(expected["speed_rpm"])
            or float(actual.duty_weight) != float(expected["duty_weight"])
        ):
            raise OptimizationActivationBuildError("optimization operating points differ from acceptance")
        target = actual.target_torque_nm if actual.target_kind == "torque" else actual.target_power_w
        expected_target = expected.get("target_torque_nm", expected.get("target_power_w"))
        if target is None or float(target) != float(expected_target):
            raise OptimizationActivationBuildError("optimization target value differs from acceptance")
    if float(spec.inverter.vdc_v) != 200.0:
        raise OptimizationActivationBuildError("accepted DC voltage must be 200 V")
    if float(spec.inverter.phase_peak_current_limit_a) != 137.8:
        raise OptimizationActivationBuildError("accepted peak-current limit must be 137.8 A")
    if spec.winding.series_turns_per_phase != 48:
        raise OptimizationActivationBuildError("accepted winding must have 48 series turns per phase")
    if (
        spec.nsga2.population_size != 160
        or spec.nsga2.max_generations != 300
        or tuple(spec.nsga2.seeds) != (42, 43, 44)
        or spec.nsga2.max_fea_candidates != 12
    ):
        raise OptimizationActivationBuildError("NSGA-II settings differ from accepted 160/300/42-44/12")
    probe = ipmsm_optimization.active_volume_m3(150.0, 49.45)
    expected_probe = math.pi * (150.0e-3) ** 2 * (49.45e-3)
    if not math.isclose(probe, expected_probe, rel_tol=0.0, abs_tol=1e-15):
        raise OptimizationActivationBuildError("active-volume implementation changed")
    return copy.deepcopy(ACCEPTED_INPUTS)


def _legacy_inputs(
    parent: Any,
    passed_decision: Path,
    *,
    artifact_workdir: Path | None = None,
) -> tuple[Any, dict[str, Path]]:
    evidence_workdir = (
        artifact_workdir or parent.base_contract.workdir
    ).resolve(strict=True)
    argv = list(parent.base_contract.optimization.argv_template)
    workdir = parent.base_contract.workdir
    paths = {
        name: _resolved_flag(argv, flag, workdir, "parent optimization")
        for name, flag in (
            ("optimization_spec", "--optimization-spec"),
            ("beta_summary", "--beta-summary"),
            ("beta_case_plan", "--beta-case-plan"),
            ("beta_results", "--beta-results"),
            ("beta_calibration_manifest", "--beta-calibration-manifest"),
        )
    }
    args = argparse.Namespace(
        stage2_decision=passed_decision,
        optimization_spec=paths["optimization_spec"],
        beta_summary=paths["beta_summary"],
        beta_case_plan=paths["beta_case_plan"],
        beta_results=paths["beta_results"],
        beta_calibration_manifest=paths["beta_calibration_manifest"],
        max_fea_candidates=12,
    )
    previous_workdir = Path.cwd()
    try:
        os.chdir(evidence_workdir)
        upstream = v3.audit_decision(
            parent.base_contract.stage2.decision,
            schema_version=v3.STAGE2_DECISION_SCHEMA_VERSION,
            allowed_statuses={"combined_r2_failed"},
            workdir=evidence_workdir,
        )
        if upstream.get("status") != "combined_r2_failed":
            raise OptimizationActivationBuildError(
                "optimization activation requires the Stage3 branch"
            )
        audited = legacy_optimization.audit_inputs(args)
    except OptimizationActivationBuildError:
        raise
    except Exception as exc:
        raise OptimizationActivationBuildError(
            f"passed Stage3/adaptive decision failed audit: {exc}"
        ) from exc
    finally:
        os.chdir(previous_workdir)
    assert_accepted_spec(audited.spec)
    return audited, paths


def _artifact_record(
    value: Any,
    label: str,
    *,
    runtime_root: Path | None = None,
) -> tuple[FileBinding, dict[str, str]]:
    record = _mapping(value, label)
    _expect_keys(record, {"path", "sha256"}, label)
    path = _c_path(record["path"], f"{label}.path")
    if runtime_root is not None and not _within(path, runtime_root):
        raise OptimizationActivationBuildError(f"{label} must be inside runtime_root")
    expected_sha = _text(record["sha256"], f"{label}.sha256")
    if (
        len(expected_sha) != 64
        or expected_sha != expected_sha.lower()
        or any(character not in "0123456789abcdef" for character in expected_sha)
    ):
        raise OptimizationActivationBuildError(f"{label}.sha256 is not lowercase SHA-256")
    binding = _binding(path)
    if binding.sha256 != expected_sha:
        raise OptimizationActivationBuildError(f"{label} bytes changed")
    return binding, binding.as_mapping()


def _audit_stage3_acquisition_completion(
    completion_path: Path,
    runtime_root: Path,
) -> tuple[FileBinding, dict[str, Any], dict[str, Any]]:
    expected_root = (runtime_root / STAGE3_ACQUISITION_RELATIVE_ROOT).resolve(
        strict=True
    )
    expected_completion = (
        expected_root / STAGE3_ACQUISITION_COMPLETION_FILENAME
    ).resolve(strict=True)
    if completion_path.resolve(strict=True) != expected_completion:
        raise OptimizationActivationBuildError(
            "stage3_acquisition_completion is not the fixed v4r10 completion"
        )
    payload, completion = _strict_json(completion_path, "Stage3 acquisition completion")
    if payload != _canonical_bytes(completion):
        raise OptimizationActivationBuildError(
            "Stage3 acquisition completion bytes are not canonical"
        )
    _expect_keys(
        completion,
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
        "Stage3 acquisition completion",
    )
    if (
        completion["schema_version"]
        != STAGE3_ACQUISITION_COMPLETION_SCHEMA_VERSION
        or completion["status"] != "acquisition_complete"
    ):
        raise OptimizationActivationBuildError(
            "Stage3 acquisition completion schema/status changed"
        )

    contract_record = _mapping(
        completion["contract"], "Stage3 acquisition completion.contract"
    )
    _expect_keys(
        contract_record,
        {"path", "raw_sha256", "contract_sha256"},
        "Stage3 acquisition completion.contract",
    )
    contract_path = _c_path(
        contract_record["path"], "Stage3 acquisition completion.contract.path"
    )
    expected_contract = (
        expected_root / STAGE3_ACQUISITION_CONTRACT_FILENAME
    ).resolve(strict=True)
    if contract_path != expected_contract:
        raise OptimizationActivationBuildError(
            "Stage3 acquisition completion binds a non-v4r10 contract path"
        )
    contract_payload, contract = _strict_json(
        contract_path, "Stage3 acquisition contract"
    )
    if (
        contract_payload != _canonical_bytes(contract)
        or _sha256_bytes(contract_payload)
        != EXPECTED_STAGE3_ACQUISITION_CONTRACT_RAW_SHA256
        or contract_record["raw_sha256"]
        != EXPECTED_STAGE3_ACQUISITION_CONTRACT_RAW_SHA256
    ):
        raise OptimizationActivationBuildError(
            "Stage3 acquisition contract raw identity changed"
        )
    _expect_keys(
        contract,
        {"schema_version", "contract_sha256", "recovery"},
        "Stage3 acquisition contract",
    )
    if contract["schema_version"] != STAGE3_ACQUISITION_CONTRACT_SCHEMA_VERSION:
        raise OptimizationActivationBuildError(
            "Stage3 acquisition contract schema_version changed"
        )
    unsigned_contract = {
        "schema_version": contract["schema_version"],
        "recovery": contract["recovery"],
    }
    if (
        contract["contract_sha256"] != v3._canonical_sha256(unsigned_contract)
        or contract_record["contract_sha256"] != contract["contract_sha256"]
    ):
        raise OptimizationActivationBuildError(
            "Stage3 acquisition logical contract identity changed"
        )

    recovery = _mapping(contract["recovery"], "Stage3 acquisition recovery")
    outputs = _mapping(recovery.get("outputs"), "Stage3 acquisition recovery.outputs")
    if Path(str(outputs.get("completion") or "")).resolve(
        strict=False
    ) != expected_completion:
        raise OptimizationActivationBuildError(
            "Stage3 acquisition contract completion output changed"
        )
    repository = _mapping(
        recovery.get("repository"), "Stage3 acquisition recovery.repository"
    )
    repository_revision = _text(
        repository.get("revision"), "Stage3 acquisition repository revision"
    )
    if (
        len(repository_revision) != 40
        or any(character not in "0123456789abcdef" for character in repository_revision)
        or completion["repository_revision"] != repository_revision
    ):
        raise OptimizationActivationBuildError(
            "Stage3 acquisition completion repository revision changed"
        )

    effective = _mapping(
        completion["effective_plan"], "Stage3 acquisition effective_plan"
    )
    _expect_keys(
        effective,
        {"path", "sha256", "kind", "rows", "geometry_groups"},
        "Stage3 acquisition effective_plan",
    )
    _artifact_record(
        {"path": effective["path"], "sha256": effective["sha256"]},
        "Stage3 acquisition effective plan",
        runtime_root=runtime_root,
    )
    if (
        effective["kind"] not in {"original", "replacement"}
        or effective["rows"] != 300
        or effective["geometry_groups"] != 50
    ):
        raise OptimizationActivationBuildError(
            "Stage3 acquisition effective-plan scope changed"
        )
    result = _mapping(completion["result"], "Stage3 acquisition result")
    _expect_keys(result, {"path", "sha256", "rows"}, "Stage3 acquisition result")
    _artifact_record(
        {"path": result["path"], "sha256": result["sha256"]},
        "Stage3 acquisition result",
        runtime_root=runtime_root,
    )
    if result["rows"] != 300:
        raise OptimizationActivationBuildError("Stage3 acquisition result scope changed")
    if Path(str(result["path"])).resolve(strict=True) != Path(
        str(outputs.get("merged_result") or "")
    ).resolve(strict=True):
        raise OptimizationActivationBuildError(
            "Stage3 acquisition result path differs from sealed contract output"
        )
    sealed_plan = _mapping(recovery.get("plan"), "Stage3 acquisition recovery.plan")
    sealed_replacement = _mapping(
        recovery.get("replacement"), "Stage3 acquisition recovery.replacement"
    )
    if effective["kind"] == "original":
        if {
            "path": effective["path"],
            "sha256": effective["sha256"],
            "rows": effective["rows"],
            "geometry_groups": effective["geometry_groups"],
        } != {
            "path": sealed_plan.get("path"),
            "sha256": sealed_plan.get("sha256"),
            "rows": sealed_plan.get("rows"),
            "geometry_groups": sealed_plan.get("geometry_groups"),
        }:
            raise OptimizationActivationBuildError(
                "original Stage3 effective plan differs from sealed contract plan"
            )
    elif Path(str(effective["path"])).resolve(strict=True) != Path(
        str(sealed_replacement.get("plan_output") or "")
    ).resolve(strict=True):
        raise OptimizationActivationBuildError(
            "replacement Stage3 effective plan path changed"
        )
    replacement = completion["replacement_manifest"]
    if effective["kind"] == "original" and replacement is not None:
        raise OptimizationActivationBuildError(
            "original Stage3 completion unexpectedly binds replacement evidence"
        )
    if effective["kind"] == "replacement" and not isinstance(replacement, Mapping):
        raise OptimizationActivationBuildError(
            "replacement Stage3 completion lacks replacement evidence"
        )
    if isinstance(replacement, Mapping):
        replacement_record = _mapping(
            replacement, "Stage3 acquisition replacement_manifest"
        )
        _expect_keys(
            replacement_record,
            {
                "path",
                "sha256",
                "failed_geometry_group_id",
                "replacement_geometry_group_id",
                "failure_evidence_manifest",
            },
            "Stage3 acquisition replacement_manifest",
        )
        replacement_binding, _replacement_artifact = _artifact_record(
            {
                "path": replacement_record["path"],
                "sha256": replacement_record["sha256"],
            },
            "Stage3 acquisition replacement manifest",
            runtime_root=runtime_root,
        )
        if Path(str(replacement_record["path"])).resolve(strict=True) != Path(
            str(sealed_replacement.get("manifest_output") or "")
        ).resolve(strict=True):
            raise OptimizationActivationBuildError(
                "Stage3 replacement manifest path changed"
            )
        _, replacement_document = _strict_json(
            replacement_binding.path,
            "Stage3 acquisition replacement manifest",
        )
        replacement_output = _mapping(
            replacement_document.get("output"),
            "Stage3 acquisition replacement manifest.output",
        )
        if (
            replacement_document.get("schema_version")
            != "ipmsm_v2_failed_geometry_replacement_v1"
            or replacement_document.get("mode") != "execute"
            or replacement_document.get("status") != "created"
            or replacement_document.get("manifest_path")
            != str(replacement_binding.path)
            or replacement_document.get("failed_geometry_group_id")
            != replacement_record["failed_geometry_group_id"]
            or replacement_document.get("replacement_geometry_group_id")
            != replacement_record["replacement_geometry_group_id"]
            or replacement_output
            != {
                "path": effective["path"],
                "sha256": effective["sha256"],
            }
        ):
            raise OptimizationActivationBuildError(
                "Stage3 replacement manifest internal identity changed"
            )
        _artifact_record(
            replacement_record["failure_evidence_manifest"],
            "Stage3 acquisition replacement failure evidence",
            runtime_root=runtime_root,
        )
        failure_record = _mapping(
            replacement_record["failure_evidence_manifest"],
            "Stage3 replacement failure evidence",
        )
        if Path(str(failure_record["path"])).resolve(strict=True) != Path(
            str(sealed_replacement.get("failure_evidence_manifest") or "")
        ).resolve(strict=True):
            raise OptimizationActivationBuildError(
                "Stage3 replacement failure evidence path changed"
            )

    scheduler = _mapping(completion["scheduler"], "Stage3 acquisition scheduler")
    _expect_keys(
        scheduler,
        {"url", "project", "task_prefix", "history_tasks", "project_active_cap"},
        "Stage3 acquisition scheduler",
    )
    expected_history = 309 if effective["kind"] == "original" else 315
    execution = _mapping(recovery.get("execution"), "Stage3 acquisition execution")
    if scheduler != {
        "url": "http://127.0.0.1:8002",
        "project": "PYAEDT_MOTOR_IPMSM_V2",
        "task_prefix": execution.get("task_prefix"),
        "history_tasks": expected_history,
        "project_active_cap": 50,
    }:
        raise OptimizationActivationBuildError(
            "Stage3 acquisition scheduler provenance changed"
        )
    return _binding(completion_path), completion, contract


def _audit_direct_precollected_decision(
    decision: Mapping[str, Any],
    *,
    completion_binding: FileBinding,
    completion: Mapping[str, Any],
    acquisition_contract: Mapping[str, Any],
) -> None:
    decision_stage2 = _mapping(decision.get("stage2"), "decision.stage2")
    execution = _mapping(decision.get("execution_contract"), "execution_contract")
    execution_stage2 = _mapping(execution.get("stage2"), "execution_contract.stage2")
    top = decision_stage2.get("precollected_completion")
    nested = execution_stage2.get("precollected_completion")
    if not isinstance(top, Mapping) or dict(top) != nested:
        raise OptimizationActivationBuildError(
            "decision does not exactly bind one precollected completion"
        )
    precollected = _mapping(nested, "precollected completion provenance")
    _expect_keys(
        precollected,
        {
            "completion",
            "contract",
            "effective_plan",
            "live_verification",
            "replacement_manifest",
            "repository_revision",
            "result",
            "runner_source",
            "scheduler",
            "schema_version",
            "status",
        },
        "precollected completion provenance",
    )
    if precollected["completion"] != completion_binding.as_mapping():
        raise OptimizationActivationBuildError(
            "decision precollected completion path/SHA changed"
        )
    for name in (
        "contract",
        "effective_plan",
        "replacement_manifest",
        "repository_revision",
        "result",
        "scheduler",
        "schema_version",
        "status",
    ):
        if precollected[name] != completion[name]:
            raise OptimizationActivationBuildError(
                f"decision precollected completion {name} changed"
            )
    expected_live = {
        "action": "verified_existing_completion",
        "history_tasks": completion["scheduler"]["history_tasks"],
        "mode": "execute",
        "plan_kind": completion["effective_plan"]["kind"],
        "schema_version": "ipmsm-v2-stage3-acquisition-v4r10-run-v1",
        "status": "acquisition_complete",
        "successful_results": 300,
        "writes_performed": 0,
    }
    if precollected["live_verification"] != expected_live:
        raise OptimizationActivationBuildError(
            "decision precollected live verification changed"
        )
    runner_source = _mapping(
        precollected["runner_source"], "precollected runner_source"
    )
    _expect_keys(
        runner_source,
        {"acquisition", "continuation", "repository_revision", "source_root"},
        "precollected runner_source",
    )
    repository = _mapping(
        acquisition_contract["recovery"]["repository"],
        "Stage3 acquisition repository",
    )
    repository_sources = _mapping(
        repository.get("sources"), "Stage3 acquisition repository.sources"
    )
    source_root = _c_path(repository["source_root"], "Stage3 acquisition source_root")
    if (
        runner_source["repository_revision"] != repository["revision"]
        or Path(str(runner_source["source_root"])).resolve(strict=True) != source_root
    ):
        raise OptimizationActivationBuildError(
            "precollected runner repository binding changed"
        )
    for name, filename in (
        ("acquisition", "continue_ipmsm_v2_stage3_acquisition_v4r9.py"),
        ("continuation", "continue_ipmsm_v2_stage2.py"),
    ):
        binding, record = _artifact_record(
            runner_source[name], f"precollected runner_source.{name}"
        )
        if binding.path != (source_root / filename).resolve(strict=True):
            raise OptimizationActivationBuildError(
                f"precollected runner_source.{name} path changed"
            )
        if runner_source[name] != record:
            raise OptimizationActivationBuildError(
                f"precollected runner_source.{name} binding changed"
            )
        source_key = "runner" if name == "acquisition" else "stage2_continuation"
        source_record = _mapping(
            repository_sources.get(source_key),
            f"Stage3 acquisition repository.sources.{source_key}",
        )
        if (
            source_record.get("path") != record["path"]
            or source_record.get("sha256") != record["sha256"]
            or source_record.get("git_blob_sha256") != record["sha256"]
        ):
            raise OptimizationActivationBuildError(
                f"precollected runner_source.{name} differs from sealed Git blob"
            )


def _load_lineage_decision(
    record: Mapping[str, Any],
    *,
    label: str,
    runtime_root: Path,
    allowed_statuses: set[str],
) -> tuple[FileBinding, dict[str, Any]]:
    binding, exact = _artifact_record(record, label, runtime_root=runtime_root)
    payload, decision = _strict_json(binding.path, label)
    if _sha256_bytes(payload) != exact["sha256"]:
        raise OptimizationActivationBuildError(f"{label} changed while reading")
    if (
        decision.get("schema_version") != v3.STAGE2_DECISION_SCHEMA_VERSION
        or decision.get("mode") != "execute"
        or decision.get("decision") != "run_stage2"
        or decision.get("status") not in allowed_statuses
    ):
        raise OptimizationActivationBuildError(f"{label} is not an allowed Stage2 decision")
    if Path(str(decision.get("decision_output") or "")).resolve(
        strict=False
    ) != binding.path:
        raise OptimizationActivationBuildError(f"{label} moved from decision_output")
    try:
        audited = v3.audit_decision(
            binding.path,
            schema_version=v3.STAGE2_DECISION_SCHEMA_VERSION,
            allowed_statuses=allowed_statuses,
            workdir=runtime_root,
        )
    except Exception as exc:
        raise OptimizationActivationBuildError(
            f"{label} contract/artifact audit failed: {exc}"
        ) from exc
    if audited != decision:
        raise OptimizationActivationBuildError(f"{label} changed during audit")
    return binding, decision


def _adaptive_manifest(
    decision_binding: FileBinding,
    decision: Mapping[str, Any],
    *,
    runtime_root: Path,
) -> dict[str, Any]:
    stage2 = _mapping(decision.get("stage2"), "adaptive decision.stage2")
    execution_contract = _mapping(
        decision.get("execution_contract"), "adaptive execution_contract"
    )
    execution_stage2 = _mapping(
        execution_contract.get("stage2"), "adaptive execution_contract.stage2"
    )
    manifest_binding, manifest_record = _artifact_record(
        execution_stage2.get("case_manifest"),
        "adaptive case_manifest",
        runtime_root=runtime_root,
    )
    if (
        Path(str(stage2.get("case_manifest") or "")).resolve(strict=False)
        != manifest_binding.path
        or stage2.get("case_manifest_sha256") != manifest_binding.sha256
    ):
        raise OptimizationActivationBuildError(
            "adaptive decision top-level case manifest binding changed"
        )
    _, manifest = _strict_json(manifest_binding.path, "adaptive case manifest")
    if manifest.get("schema_version") != ADAPTIVE_CASE_MANIFEST_SCHEMA_VERSION or manifest.get(
        "mode"
    ) != "write":
        raise OptimizationActivationBuildError("adaptive case manifest schema/mode changed")
    execution = _mapping(manifest.get("execution_contract"), "adaptive manifest execution")
    _expect_keys(
        execution,
        {
            "batch_index",
            "case_plan",
            "failed_decision",
            "fixed_audit_case_plan",
            "plateau_policy",
            "r2_history",
            "seed_policy",
        },
        "adaptive manifest execution",
    )
    if manifest.get("execution_contract_sha256") != v3._canonical_sha256(execution):
        raise OptimizationActivationBuildError(
            "adaptive manifest execution_contract_sha256 changed"
        )
    batch_index = execution.get("batch_index")
    if type(batch_index) is not int or batch_index < 1:
        raise OptimizationActivationBuildError("adaptive batch_index is invalid")
    _case_plan_binding, case_plan_record = _artifact_record(
        execution.get("case_plan"), "adaptive case plan", runtime_root=runtime_root
    )
    if execution_stage2.get("case_plan") != case_plan_record:
        raise OptimizationActivationBuildError(
            "adaptive manifest case plan differs from decision contract"
        )
    _fixed_binding, fixed_record = _artifact_record(
        execution.get("fixed_audit_case_plan"),
        "adaptive fixed audit case plan",
        runtime_root=runtime_root,
    )
    failed_binding, failed_record = _artifact_record(
        execution.get("failed_decision"),
        "adaptive failed decision",
        runtime_root=runtime_root,
    )
    history_binding, history_record = _artifact_record(
        execution.get("r2_history"),
        "adaptive R2 history",
        runtime_root=runtime_root,
    )
    try:
        history = adaptive_generator.load_adaptive_r2_history(
            history_binding.path,
            failed_decision=failed_binding.path,
            batch_index=batch_index,
        )
    except (OSError, ValueError) as exc:
        raise OptimizationActivationBuildError(
            f"adaptive R2 history audit failed: {exc}"
        ) from exc
    if history["artifact"] != history_record or history["plateau"] != execution.get(
        "plateau_policy"
    ):
        raise OptimizationActivationBuildError(
            "adaptive R2 history/plateau binding changed"
        )
    seed = _mapping(execution.get("seed_policy"), "adaptive seed_policy")
    stride = seed.get("stride")
    adaptation_base = seed.get("adaptation_seed_base")
    calibration_base = seed.get("calibration_seed_base")
    if (
        stride != 100
        or seed.get("formula") != "role_seed_base + 100 * batch_index"
        or type(adaptation_base) is not int
        or type(calibration_base) is not int
        or seed.get("adaptation_seed") != adaptation_base + stride * batch_index
        or seed.get("calibration_seed") != calibration_base + stride * batch_index
    ):
        raise OptimizationActivationBuildError("adaptive seed_policy changed")
    summary = _mapping(manifest.get("summary"), "adaptive manifest summary")
    if (
        summary.get("rows") != 300
        or summary.get("split_groups")
        != {"train": 40, "calibration": 10, "test": 0}
        or summary.get("split_rows")
        != {"train": 240, "calibration": 60, "test": 0}
    ):
        raise OptimizationActivationBuildError("adaptive manifest summary changed")
    selection = _mapping(manifest.get("selection"), "adaptive manifest selection")
    adaptation = _mapping(selection.get("adaptation"), "adaptive selection.adaptation")
    candidate_pool = _mapping(
        adaptation.get("candidate_pool"), "adaptive selection candidate_pool"
    )
    scoring = _mapping(adaptation.get("scoring"), "adaptive selection scoring")
    calibration = _mapping(
        selection.get("calibration"), "adaptive selection.calibration"
    )
    expected_scoring = {
        "residual_weight": 0.5,
        "uncertainty_weight": 0.3,
        "domain_distance_weight": 0.2,
        "diversity_weight": 0.2,
    }
    if (
        selection.get("batch_index") != batch_index
        or selection.get("candidate_pool_geometries") != 1024
        or selection.get("fixed_audit_policy")
        != "reuse_sealed_stage3_test_without_new_test_rows"
        or selection.get("seed_policy") != seed
        or adaptation.get("geometry_count") != 40
        or candidate_pool.get("geometry_count") != 1024
        or calibration.get("geometry_count") != 10
        or any(scoring.get(key) != value for key, value in expected_scoring.items())
    ):
        raise OptimizationActivationBuildError("adaptive selection authority changed")
    return {
        "batch_index": batch_index,
        "decision": decision_binding.as_mapping(),
        "case_manifest": manifest_record,
        "case_plan": case_plan_record,
        "failed_decision": failed_record,
        "fixed_audit_case_plan": fixed_record,
        "r2_history": history_record,
        "history_records": history["records"],
        "plateau_policy": history["plateau"],
        "seed_policy": dict(seed),
    }


def audit_passed_decision_provenance(
    passed_decision: Path,
    completion_path: Path,
    runtime_root: Path,
) -> tuple[FileBinding, dict[str, Any]]:
    completion_binding, completion, acquisition_contract = (
        _audit_stage3_acquisition_completion(completion_path, runtime_root)
    )
    passed_binding, passed = _load_lineage_decision(
        _binding(passed_decision).as_mapping(),
        label="passed decision",
        runtime_root=runtime_root,
        allowed_statuses={"complete"},
    )
    execution_stage2 = _mapping(
        _mapping(passed.get("execution_contract"), "passed execution_contract").get(
            "stage2"
        ),
        "passed execution_contract.stage2",
    )
    if "precollected_completion" in execution_stage2:
        _audit_direct_precollected_decision(
            passed,
            completion_binding=completion_binding,
            completion=completion,
            acquisition_contract=acquisition_contract,
        )
        return completion_binding, {
            "kind": "direct_precollected",
            "batch_index": 0,
            "completion": completion_binding.as_mapping(),
            "baseline_decision": passed_binding.as_mapping(),
            "final_decision": passed_binding.as_mapping(),
            "adaptive_batches": [],
        }
    if "case_manifest" not in execution_stage2:
        raise OptimizationActivationBuildError(
            "passed decision is neither direct precollected nor adaptive-chained"
        )

    final_batch = _adaptive_manifest(
        passed_binding, passed, runtime_root=runtime_root
    )
    batch_index = final_batch["batch_index"]
    records = final_batch["history_records"]
    if len(records) != batch_index:
        raise OptimizationActivationBuildError("adaptive final history length changed")
    baseline_binding, baseline = _load_lineage_decision(
        records[0]["decision"],
        label="adaptive baseline decision",
        runtime_root=runtime_root,
        allowed_statuses={"combined_r2_failed"},
    )
    _audit_direct_precollected_decision(
        baseline,
        completion_binding=completion_binding,
        completion=completion,
        acquisition_contract=acquisition_contract,
    )
    expected_fixed_audit = {
        "path": completion["effective_plan"]["path"],
        "sha256": completion["effective_plan"]["sha256"],
    }
    if final_batch["fixed_audit_case_plan"] != expected_fixed_audit:
        raise OptimizationActivationBuildError(
            "adaptive fixed audit differs from baseline completion effective plan"
        )

    batches: list[dict[str, Any]] = []
    fixed_record = final_batch["fixed_audit_case_plan"]
    for expected_index in range(1, batch_index):
        decision_binding, decision = _load_lineage_decision(
            records[expected_index]["decision"],
            label=f"adaptive decision {expected_index}",
            runtime_root=runtime_root,
            allowed_statuses={"combined_r2_failed"},
        )
        batch = _adaptive_manifest(
            decision_binding, decision, runtime_root=runtime_root
        )
        if (
            batch["batch_index"] != expected_index
            or batch["failed_decision"] != records[expected_index - 1]["decision"]
            or batch["history_records"] != records[:expected_index]
            or batch["fixed_audit_case_plan"] != fixed_record
        ):
            raise OptimizationActivationBuildError(
                f"adaptive batch {expected_index} is not contiguous"
            )
        batches.append({key: value for key, value in batch.items() if not key.startswith("_")})
    if (
        final_batch["failed_decision"] != records[-1]["decision"]
        or final_batch["fixed_audit_case_plan"] != fixed_record
    ):
        raise OptimizationActivationBuildError("adaptive final batch is not chained")
    batches.append(
        {key: value for key, value in final_batch.items() if not key.startswith("_")}
    )
    return completion_binding, {
        "kind": "adaptive",
        "batch_index": batch_index,
        "completion": completion_binding.as_mapping(),
        "baseline_decision": baseline_binding.as_mapping(),
        "final_decision": passed_binding.as_mapping(),
        "r2_history": final_batch["r2_history"],
        "history_records": records,
        "fixed_audit_case_plan": fixed_record,
        "adaptive_batches": batches,
    }


def _audit_fresh_passed_decision(
    parent: Any,
    passed_decision: Path,
    runtime_root: Path,
) -> tuple[Any, dict[str, Path]]:
    if not _within(passed_decision, runtime_root):
        raise OptimizationActivationBuildError(
            "passed_decision must be inside sealed runtime_root"
        )
    # The sealed parent's Stage3 path records the historical branch and may be
    # stale.  The successor consumes the independently supplied completed/pass
    # decision after audit_inputs binds its exact decision and model bundle.
    return _legacy_inputs(parent, passed_decision)


def _build_paths(runtime_root: Path, output_root: Path) -> BuildPaths:
    expected = (runtime_root / ACTIVATION_RELATIVE_ROOT).resolve(strict=False)
    if output_root.resolve(strict=False) != expected:
        raise OptimizationActivationBuildError(f"output_root must be the fixed v4r9 path: {expected}")
    return BuildPaths(
        root=output_root,
        base_contract=output_root / BASE_FILENAME,
        v4_contract=output_root / V4_FILENAME,
        activation_contract=output_root / ACTIVATION_FILENAME,
        declaration=output_root / DECLARATION_FILENAME,
        confirmation=output_root / CONFIRMATION_FILENAME,
        receipt=output_root / RECEIPT_FILENAME,
        decision=output_root / DECISION_FILENAME,
        shared_lock=output_root / "optimization.lock",
        stage1_workspace=output_root / "stage1_official",
        optimization_output=(runtime_root / OPTIMIZATION_RELATIVE_ROOT).resolve(strict=False),
        checkpoint_dir=(runtime_root / CHECKPOINT_RELATIVE_ROOT).resolve(strict=False),
    )


def _build_base_document(
    parent: Any,
    *,
    source_root: Path,
    source_revision: str,
    python_executable: Path,
    passed_decision: Path,
    paths: BuildPaths,
    scheduler_project: str,
) -> dict[str, Any]:
    _, raw = _strict_json(parent.base_contract.source, "parent base contract")
    pipeline = copy.deepcopy(_mapping(raw["pipeline"], "parent base pipeline"))
    _absolutize_legacy_pipeline(
        pipeline,
        runtime_root=parent.base_contract.workdir,
        source_root=source_root,
    )
    _hard_disable_speed_argv(pipeline, paths.activation_contract)
    pipeline["external_pid_files"] = []
    pipeline["lock_path"] = str(paths.shared_lock)

    implementation = _source_binding(
        source_root, source_revision, Path("ipmsm_optimization.py"), "optimization implementation"
    )
    immutable = list(pipeline["immutable_inputs"])
    if not any(Path(str(item["path"])).resolve(strict=False) == implementation.path for item in immutable):
        immutable.append(implementation.as_mapping())
    pipeline["immutable_inputs"] = immutable

    stage3 = _mapping(pipeline["stage3"], "parent stage3")
    stage3["decision"] = str(passed_decision)
    continuation_argv = list(stage3["continuation_argv"])
    _replace_flag(
        continuation_argv,
        "--decision-output",
        str(passed_decision),
        "parent Stage3 continuation",
    )
    stage3["continuation_argv"] = continuation_argv
    pipeline["stage3"] = stage3

    optimization = _mapping(pipeline["optimization"], "parent optimization")
    argv = list(optimization["argv_template"])
    if len(argv) < 2 or Path(argv[1]).name != "continue_ipmsm_v2_optimization.py":
        raise OptimizationActivationBuildError("parent optimization executable changed")
    argv[0] = str(python_executable)
    argv[1] = str((source_root / "continue_ipmsm_v2_optimization.py").resolve(strict=True))
    replacements = {
        "--output-dir": str(paths.optimization_output),
        "--checkpoint-dir": str(paths.checkpoint_dir),
        "--decision-output": str(paths.decision),
        "--project": scheduler_project,
        "--scheduler-url": SCHEDULER_POLICY["url"],
        "--project-active-cap": str(SCHEDULER_POLICY["project_active_cap"]),
        "--max-fea-candidates": "12",
        "--task-prefix": NAMESPACE["task_prefix"],
        "--remote-cases-dir": NAMESPACE["remote_cases_dir"],
        "--result-dir": NAMESPACE["result_dir"],
        "--simulation-dir": NAMESPACE["simulation_dir"],
        "--log-dir": NAMESPACE["log_dir"],
    }
    for flag, value in replacements.items():
        _replace_flag(argv, flag, str(value), "successor optimization")
    if "--env-setup" in argv:
        raise OptimizationActivationBuildError(
            "parent optimization unexpectedly predefines --env-setup"
        )
    argv.extend(["--env-setup", remote_task_env_setup(source_revision)])
    optimization["decision"] = str(paths.decision)
    optimization["argv_template"] = argv
    pipeline["optimization"] = optimization
    unsigned = {"schema_version": v3.CONTRACT_SCHEMA_VERSION, "pipeline": pipeline}
    return {**unsigned, "contract_sha256": v3._canonical_sha256(unsigned)}


def _source_pins(source_root: Path, revision: str) -> dict[str, FileBinding]:
    pins = {
        name: _source_binding(source_root, revision, Path(relative), f"v4 source pin {name}")
        for name, relative in sorted(v4.SOURCE_PIN_FILENAMES.items())
    }
    return pins


def _build_v4_document(
    *,
    base_document: Mapping[str, Any],
    base_path: Path,
    v4_path: Path,
    python_executable: Path,
    source_pins: Mapping[str, FileBinding],
    paths: BuildPaths,
) -> dict[str, Any]:
    base_identity = _contract_identity(base_path, base_document)
    pins = {name: binding.as_mapping() for name, binding in source_pins.items()}
    legacy = list(base_document["pipeline"]["optimization"]["argv_template"])
    wrapper = [
        str(python_executable),
        str(source_pins["optimization_runner_v4"].path),
        "--pipeline-contract",
        str(v4_path),
        "--authorization-receipt",
        str(paths.receipt),
        "--confirmation",
        str(paths.confirmation),
        *legacy[2:],
    ]
    pipeline = {
        "workdir": str(Path(base_document["pipeline"]["workdir"]).resolve(strict=True)),
        "shared_lock": str(paths.shared_lock),
        "base_contract": base_identity,
        "immutable_inputs": [
            {"path": str(base_path), "sha256": base_identity["raw_sha256"]},
            *(pins[name] for name in sorted(pins)),
        ],
        "source_pins": pins,
        "stage1_official": {
            "workspace": str(paths.stage1_workspace),
            "completion": str(paths.stage1_workspace / "completion.json"),
            "publisher_argv": [
                str(python_executable),
                str(source_pins["stage1_publisher_v4"].path),
                "--pipeline-contract",
                str(v4_path),
                "--base-contract",
                str(base_path),
                "--workspace",
                str(paths.stage1_workspace),
            ],
        },
        "optimization_confirmation": {
            "declaration": str(paths.declaration),
            "confirmation": str(paths.confirmation),
            "receipt": str(paths.receipt),
            "authorizer_argv": [
                str(python_executable),
                str(source_pins["optimization_authorizer_v4"].path),
                "--contract",
                str(v4_path),
                "--confirmation",
                str(paths.confirmation),
                "--output",
                str(paths.receipt),
            ],
        },
        "optimization": {"wrapper_argv_template": wrapper},
    }
    unsigned = {"schema_version": v4.CONTRACT_SCHEMA_VERSION, "pipeline": pipeline}
    return {**unsigned, "contract_sha256": v3._canonical_sha256(unsigned)}


def _validate_base_document(document: Mapping[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="ipmsm-v4r9-base-audit-") as temporary:
        path = Path(temporary) / BASE_FILENAME
        path.write_bytes(_canonical_bytes(document))
        contract = v3.load_contract(path)
        v3.audit_immutable_inputs(contract)


def _audit_campaign_defaults() -> None:
    if campaign_submitter.DEFAULT_PROJECT_ACTIVE_CAP != 50:
        raise OptimizationActivationBuildError("campaign default active cap changed")
    parser = campaign_submitter.build_parser()
    args = parser.parse_args(
        [
            "--cases",
            "placeholder.csv",
            "--project",
            "placeholder",
            "--scheduler-url",
            SCHEDULER_POLICY["url"],
            "--project-active-cap",
            str(SCHEDULER_POLICY["project_active_cap"]),
        ]
    )
    actual = {
        "url": args.scheduler_url,
        "endpoint": "/api/tasks",
        "project_active_cap": args.project_active_cap,
        "scheduling_profile": args.scheduling_profile,
        "required_capability": args.required_capability,
        "env_profile": args.env_profile,
        "env_setup": args.env_setup,
        "aedt_backend": args.aedt_backend,
        "max_workers_per_node": args.max_workers_per_node,
    }
    if actual != SCHEDULER_POLICY:
        raise OptimizationActivationBuildError("campaign defaults differ from v4r9 scheduler policy")


def _strict_json_payload(payload: bytes, label: str) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8-sig")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OptimizationActivationBuildError(f"cannot parse {label}: {exc}") from exc
    return _mapping(value, label)


def audit_remote_deployment(source_revision: str) -> dict[str, Any]:
    revision = _source_revision(source_revision)
    project = REMOTE_DEPLOYMENT_POLICY["project"]
    endpoint = (
        SCHEDULER_POLICY["url"].rstrip("/")
        + "/api/projects/"
        + url_parse.quote(str(project), safe="")
    )
    try:
        with url_request.urlopen(
            endpoint,
            timeout=float(REMOTE_DEPLOYMENT_POLICY["request_timeout_seconds"]),
        ) as response:
            payload = response.read()
    except (OSError, url_error.URLError, TimeoutError) as exc:
        raise OptimizationActivationBuildError(
            f"remote deployment preflight failed: {exc}"
        ) from exc
    document = _strict_json_payload(payload, "scheduler project deployment")
    if document.get("name") != project:
        raise OptimizationActivationBuildError("scheduler project name changed")

    repos = document.get("repos")
    if not isinstance(repos, list) or len(repos) != 1:
        raise OptimizationActivationBuildError(
            "scheduler project repository set changed"
        )
    repository = _mapping(repos[0], "scheduler project repository")
    _expect_keys(repository, {"url", "ref", "subdir"}, "scheduler project repository")
    expected_repository = {
        "url": REMOTE_DEPLOYMENT_POLICY["repository_url"],
        "ref": revision,
        "subdir": REMOTE_DEPLOYMENT_POLICY["repository_subdir"],
    }
    if repository != expected_repository:
        raise OptimizationActivationBuildError(
            "scheduler project repository URL/subdir/ref changed"
        )

    raw_deployments = document.get("deployments")
    if not isinstance(raw_deployments, list):
        raise OptimizationActivationBuildError("scheduler deployments are invalid")
    deployments: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_deployments):
        item = _mapping(raw, f"scheduler deployment[{index}]")
        account = _text(
            item.get("account_name"), f"scheduler deployment[{index}].account_name"
        )
        refs_value = item.get("deployed_refs")
        if isinstance(refs_value, str):
            refs = _strict_json_payload(
                refs_value.encode("utf-8"),
                f"scheduler deployment[{index}].deployed_refs",
            )
        else:
            refs = _mapping(
                refs_value, f"scheduler deployment[{index}].deployed_refs"
            )
        expected_refs = {
            REMOTE_DEPLOYMENT_POLICY["repository_key"]: revision
        }
        status = item.get("status")
        if status != REMOTE_DEPLOYMENT_POLICY["deployment_status"] or refs != expected_refs:
            raise OptimizationActivationBuildError(
                f"scheduler deployment {account} status/ref changed"
            )
        deployments.append(
            {
                "account_name": account,
                "status": status,
                "deployed_refs": refs,
            }
        )
    deployments.sort(key=lambda item: item["account_name"])
    if [item["account_name"] for item in deployments] != sorted(
        REMOTE_DEPLOYMENT_POLICY["accounts"]
    ):
        raise OptimizationActivationBuildError(
            "scheduler deployment account set changed"
        )

    validated = document.get("validated_concurrency_limit")
    if (
        document.get("auto_pull") is not REMOTE_DEPLOYMENT_POLICY["auto_pull"]
        or document.get("cleanup_globs")
        != REMOTE_DEPLOYMENT_POLICY["cleanup_globs"]
        or document.get("max_active_tasks")
        != REMOTE_DEPLOYMENT_POLICY["max_active_tasks"]
        or type(validated) is not int
        or validated < REMOTE_DEPLOYMENT_POLICY["minimum_validated_concurrency_limit"]
        or document.get("aedt_backend") != REMOTE_DEPLOYMENT_POLICY["aedt_backend"]
        or document.get("setup") != REMOTE_DEPLOYMENT_POLICY["setup"]
    ):
        raise OptimizationActivationBuildError(
            "scheduler project cap/backend/setup policy changed"
        )
    entrypoints = document.get("entrypoints")
    if not isinstance(entrypoints, list):
        raise OptimizationActivationBuildError("scheduler entrypoints are invalid")
    expected_entrypoints = {
        path: {
            "workdir": "pyaedt_motor",
            "path": path,
            "conda_env": "pyaedt2026v1",
        }
        for path in ("run_ipmsm_batch.py", "subprocess_run.py")
    }
    sealed_entrypoints: dict[str, dict[str, str]] = {}
    for path, expected_entrypoint in expected_entrypoints.items():
        matches = [
            item
            for item in entrypoints
            if isinstance(item, Mapping) and item.get("path") == path
        ]
        if len(matches) != 1 or dict(matches[0]) != expected_entrypoint:
            raise OptimizationActivationBuildError(
                f"scheduler {path} entrypoint changed"
            )
        sealed_entrypoints[path] = expected_entrypoint
    simulation = _mapping(
        document.get("simulation_policy"), "scheduler simulation_policy"
    )
    simulation_core = {
        key: simulation.get(key)
        for key in (
            "project",
            "name",
            "desired_simulations",
            "effective_simulations",
            "validated_concurrency_limit",
            "min_desired_simulations",
            "max_desired_simulations",
            "scale_down_mode",
            "control_enabled",
        )
    }
    expected_simulation_core = {
        "project": project,
        "name": project,
        "desired_simulations": SCHEDULER_POLICY["project_active_cap"],
        "effective_simulations": SCHEDULER_POLICY["project_active_cap"],
        "validated_concurrency_limit": SCHEDULER_POLICY["project_active_cap"],
        "min_desired_simulations": 0,
        "max_desired_simulations": SCHEDULER_POLICY["project_active_cap"],
        "scale_down_mode": "drain",
        "control_enabled": True,
    }
    if simulation_core != expected_simulation_core:
        raise OptimizationActivationBuildError(
            "scheduler simulation control policy changed"
        )
    return {
        "project": project,
        "source_revision": revision,
        "repository": expected_repository,
        "deployments": deployments,
        "auto_pull": document["auto_pull"],
        "cleanup_globs": document["cleanup_globs"],
        "max_active_tasks": document["max_active_tasks"],
        "validated_concurrency_limit": validated,
        "aedt_backend": document["aedt_backend"],
        "setup": document["setup"],
        "entrypoints": sealed_entrypoints,
        "simulation_policy": simulation_core,
    }


def build_plan(config_path: Path) -> BuildPlan:
    config_payload, config = _read_config(config_path.resolve(strict=True))
    runtime = _c_path(config["runtime_root"], "runtime_root")
    if runtime != EXPECTED_RUNTIME_ROOT.resolve(strict=True):
        raise OptimizationActivationBuildError("runtime_root is not the sealed LF325 runtime")
    source_root = _c_path(config["source_root"], "source_root")
    if source_root == runtime or _within(source_root, runtime) or _within(runtime, source_root):
        raise OptimizationActivationBuildError("source_root must be distinct from sealed runtime_root")
    revision = _source_revision(config["source_revision"])
    audit_source_root(source_root, revision)
    if Path(__file__).resolve(strict=True) != (source_root / BUILDER_FILENAME).resolve(strict=True):
        raise OptimizationActivationBuildError("loaded builder is not the exact source_root builder")
    runner_binding = _source_binding(source_root, revision, Path(RUNNER_FILENAME), "v4r9 runner")
    _source_binding(source_root, revision, Path(BUILDER_FILENAME), "v4r9 builder")

    parent_path = _c_path(config["parent_v4_contract"], "parent_v4_contract")
    expected_parent = (runtime / EXPECTED_PARENT_RELATIVE).resolve(strict=True)
    if parent_path != expected_parent:
        raise OptimizationActivationBuildError("parent_v4_contract is not the sealed v4r5 contract")
    parent = _audit_parent(parent_path)
    passed = _c_path(config["passed_decision"], "passed_decision")
    audited, _ = _audit_fresh_passed_decision(parent, passed, runtime)
    completion = _c_path(
        config["stage3_acquisition_completion"],
        "stage3_acquisition_completion",
    )
    completion_binding, stage3_lineage = audit_passed_decision_provenance(
        passed,
        completion,
        runtime,
    )

    output_root = _c_path(config["output_root"], "output_root")
    if not output_root.is_dir():
        raise OptimizationActivationBuildError("output_root must be an existing directory")
    paths = _build_paths(runtime, output_root)
    python = _c_path(config["python_executable"], "python_executable")
    if python != Path(sys.executable).resolve(strict=True):
        raise OptimizationActivationBuildError("builder must run under the contracted Python executable")
    project = _text(config["scheduler_project"], "scheduler_project")
    if project != "PYAEDT_MOTOR_IPMSM_V2":
        raise OptimizationActivationBuildError("scheduler_project must be PYAEDT_MOTOR_IPMSM_V2")
    authority = _mapping(config["authority"], "authority")
    _expect_keys(
        authority,
        {"confirmed_by", "confirmed_at_utc", "evidence_reference", "duty_basis"},
        "authority",
    )
    for key in authority:
        _text(authority[key], f"authority.{key}")
    try:
        confirmed = datetime.fromisoformat(authority["confirmed_at_utc"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise OptimizationActivationBuildError("confirmed_at_utc must be RFC3339") from exc
    if confirmed.tzinfo is None or confirmed > datetime.now(timezone.utc):
        raise OptimizationActivationBuildError("confirmed_at_utc must be timezone-aware and not future")

    pins = _source_pins(source_root, revision)
    base_document = _build_base_document(
        parent,
        source_root=source_root,
        source_revision=revision,
        python_executable=python,
        passed_decision=passed,
        paths=paths,
        scheduler_project=project,
    )
    _validate_base_document(base_document)
    v4_document = _build_v4_document(
        base_document=base_document,
        base_path=paths.base_contract,
        v4_path=paths.v4_contract,
        python_executable=python,
        source_pins=pins,
        paths=paths,
    )
    if runner_binding.path in {binding.path for binding in pins.values()}:
        raise OptimizationActivationBuildError("activation runner must be separate from v4 source pins")
    _audit_campaign_defaults()
    remote_deployment_snapshot = audit_remote_deployment(revision)
    return BuildPlan(
        config_path=config_path.resolve(strict=True),
        config_sha256=_sha256_bytes(config_payload),
        source_root=source_root,
        source_revision=revision,
        runtime_root=runtime,
        python_executable=python,
        parent_contract=parent,
        passed_decision=_binding(passed),
        stage3_acquisition_completion=completion_binding,
        stage3_lineage=stage3_lineage,
        remote_deployment_snapshot=remote_deployment_snapshot,
        audited_inputs=audited,
        paths=paths,
        source_pins=pins,
        base_document=base_document,
        v4_document=v4_document,
        authority=authority,
        scheduler_project=project,
    )


def _publish(document: Mapping[str, Any], path: Path) -> int:
    result = publication.publish_canonical_no_replace(document, path)
    return int(result.writes_performed)


def _publish_optimization_authority(plan: BuildPlan) -> tuple[Any, int]:
    context = optimizer_confirmation.load_bound_context(plan.paths.v4_contract)
    declaration = optimizer_confirmation.declaration_template(context)
    declaration["authority"] = {
        "confirmed_by": plan.authority["confirmed_by"],
        "confirmed_at_utc": plan.authority["confirmed_at_utc"],
        "evidence_reference": plan.authority["evidence_reference"],
        "attestation_kind": optimizer_confirmation.ATTESTATION_KIND,
    }
    declaration["confirmed_inputs"]["duty_cycle"]["basis"] = plan.authority["duty_basis"]
    declaration["acknowledgements"] = {
        name: True for name in optimizer_confirmation.ACKNOWLEDGEMENT_FIELDS
    }
    writes = _publish(declaration, plan.paths.declaration)
    declaration_snapshot, live_declaration = optimizer_confirmation._read_json_snapshot(
        plan.paths.declaration, "optimization declaration"
    )
    confirmation = optimizer_confirmation.build_confirmation(
        context,
        live_declaration,
        declaration_path=declaration_snapshot.path,
        declaration_sha256=declaration_snapshot.sha256,
    )
    confirmation_result = optimizer_confirmation.publish_confirmation_with_outcome(
        plan.paths.confirmation,
        confirmation,
        context,
        declaration_snapshot,
    )
    writes += int(confirmation_result.mutated)
    inspection = optimizer_authorizer.inspect_authorization(
        plan.paths.v4_contract,
        plan.paths.confirmation,
        plan.paths.receipt,
    )
    if inspection is None:
        raise OptimizationActivationBuildError("optimization confirmation did not become auditable")
    receipt_existed = plan.paths.receipt.exists()
    optimizer_authorizer.publish_authorization_receipt(
        inspection,
        contract_path=plan.paths.v4_contract,
        confirmation_path=plan.paths.confirmation,
    )
    writes += int(not receipt_existed)
    contract = v4.load_contract(plan.paths.v4_contract)
    authorization = v4.audit_authorization(contract)
    return authorization, writes


def _build_activation_document(plan: BuildPlan, authorization: Any) -> dict[str, Any]:
    runner = _source_binding(
        plan.source_root, plan.source_revision, Path(RUNNER_FILENAME), "v4r9 runner"
    )
    builder = _source_binding(
        plan.source_root, plan.source_revision, Path(BUILDER_FILENAME), "v4r9 builder"
    )
    dynamic_sources = {
        name: _source_binding(
            plan.source_root,
            plan.source_revision,
            relative,
            f"v4r9 dynamic source {module_name}",
        ).as_mapping()
        for name, (module_name, relative) in DYNAMIC_SOURCE_MODULES.items()
    }
    contract = v4.load_contract(plan.paths.v4_contract)
    wrapper = [
        str(plan.passed_decision.path) if item == v4.UPSTREAM_PLACEHOLDER else item
        for item in contract.optimization.wrapper_argv_template
    ]
    runner_argv = [
        str(plan.python_executable),
        "-B",
        "-X",
        f"pycache_prefix={runner_pycache_prefix(plan.paths.activation_contract)}",
        str(runner.path),
        "--activation-contract",
        str(plan.paths.activation_contract),
        "--execute",
    ]
    unsigned = {
        "schema_version": ACTIVATION_SCHEMA_VERSION,
        "activation": {
            "source": {
                "root": str(plan.source_root),
                "revision": plan.source_revision,
                "builder": builder.as_mapping(),
                "runner": runner.as_mapping(),
                "dynamic_sources": dynamic_sources,
                "config": {
                    "path": str(plan.config_path),
                    "sha256": plan.config_sha256,
                },
            },
            "runtime_root": str(plan.runtime_root),
            "base_contract": _contract_identity(plan.paths.base_contract, plan.base_document),
            "v4_contract": _contract_identity(plan.paths.v4_contract, plan.v4_document),
            "passed_decision": plan.passed_decision.as_mapping(),
            "stage3_acquisition_completion": (
                plan.stage3_acquisition_completion.as_mapping()
            ),
            "stage3_lineage": copy.deepcopy(plan.stage3_lineage),
            "remote_deployment": {
                "required_policy": copy.deepcopy(REMOTE_DEPLOYMENT_POLICY),
                "observed_at_build": copy.deepcopy(
                    plan.remote_deployment_snapshot
                ),
            },
            "authorization": authorization.record,
            "accepted_inputs": copy.deepcopy(ACCEPTED_INPUTS),
            "model_bundle": copy.deepcopy(plan.audited_inputs.model_bundle_contract),
            "scheduler": activation_scheduler_policy(plan.source_revision),
            "namespace": dict(NAMESPACE),
            "optimization": {
                "wrapper_argv": wrapper,
                "decision": str(plan.paths.decision),
                "output_dir": str(plan.paths.optimization_output),
                "checkpoint_dir": str(plan.paths.checkpoint_dir),
                "stop_after": "validated_pareto_fea",
                "legacy_speed_stage_authorized": False,
                "target_load_stage_authorized": False,
            },
            "runner": {
                "argv": runner_argv,
                "child_environment": child_environment(
                    plan.paths.activation_contract
                ),
            },
        },
    }
    return {**unsigned, "contract_sha256": v3._canonical_sha256(unsigned)}


def execute_plan(plan: BuildPlan) -> dict[str, Any]:
    writes = _publish(plan.base_document, plan.paths.base_contract)
    base = v3.load_contract(plan.paths.base_contract)
    v3.audit_immutable_inputs(base)
    writes += _publish(plan.v4_document, plan.paths.v4_contract)
    contract = v4.load_contract(plan.paths.v4_contract)
    v4.audit_contract(contract)
    authorization, authority_writes = _publish_optimization_authority(plan)
    writes += authority_writes
    activation = _build_activation_document(plan, authorization)
    writes += _publish(activation, plan.paths.activation_contract)
    payload, committed = _strict_json(plan.paths.activation_contract, "activation contract")
    if payload != _canonical_bytes(activation) or committed != activation:
        raise OptimizationActivationBuildError("committed activation contract changed")
    return {
        "status": "ready",
        "writes_performed": writes,
        "source_revision": plan.source_revision,
        "base_contract": str(plan.paths.base_contract),
        "v4_contract": str(plan.paths.v4_contract),
        "activation_contract": str(plan.paths.activation_contract),
        "authorization_receipt": str(plan.paths.receipt),
        "passed_decision": str(plan.passed_decision.path),
        "optimization_decision": str(plan.paths.decision),
        "scheduler": {**SCHEDULER_POLICY, "project": plan.scheduler_project},
        "accepted_inputs": copy.deepcopy(ACCEPTED_INPUTS),
    }


def dry_run(plan: BuildPlan) -> dict[str, Any]:
    return {
        "status": "ready_to_publish",
        "writes_performed": 0,
        "source_revision": plan.source_revision,
        "base_contract": {
            "path": str(plan.paths.base_contract),
            "sha256": _sha256_bytes(_canonical_bytes(plan.base_document)),
        },
        "v4_contract": {
            "path": str(plan.paths.v4_contract),
            "sha256": _sha256_bytes(_canonical_bytes(plan.v4_document)),
        },
        "activation_contract": str(plan.paths.activation_contract),
        "passed_decision": plan.passed_decision.as_mapping(),
        "selected_model_source": plan.audited_inputs.model_source,
        "scheduler": {**SCHEDULER_POLICY, "project": plan.scheduler_project},
        "accepted_inputs": copy.deepcopy(ACCEPTED_INPUTS),
        "next_action": "publish_contracts_authority_and_activation",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = build_plan(args.config)
        result = execute_plan(plan) if args.execute else dry_run(plan)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        OptimizationActivationBuildError,
        optimizer_confirmation.OptimizationInputConfirmationError,
        optimizer_authorizer.OptimizationAuthorizationError,
        publication.TargetLoadAuthorityError,
        OSError,
        ValueError,
    ) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
