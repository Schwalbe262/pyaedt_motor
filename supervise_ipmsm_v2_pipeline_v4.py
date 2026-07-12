"""Inactive v4 supervisor with atomic Stage1 and human authorization gates.

The v4 contract is an immutable envelope around an audited v3 base contract.
It deliberately shares the v3 process lock, but is not registered or launched
by this module.  Stage1 validation/training artifacts become authoritative only
through the v4 official-bundle completion manifest.  Optimization is delegated
only to a v4 wrapper that records a freshly audited authorization receipt.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping, Sequence

import supervise_ipmsm_v2_pipeline as v3


CONTRACT_SCHEMA_VERSION = "ipmsm-v2-pipeline-contract-v4"
REPORT_SCHEMA_VERSION = "ipmsm-v2-pipeline-supervisor-v4"
MAX_TRANSITIONS = 16
UPSTREAM_PLACEHOLDER = v3.UPSTREAM_PLACEHOLDER

def _frozen_legacy_optimization_source_modules() -> tuple[tuple[str, str], ...]:
    """Return the source-pinned manifest embedded in this immutable module."""

    return (
        ("continue_ipmsm_v2_optimization", "continue_ipmsm_v2_optimization.py"),
        ("continue_ipmsm_v2_stage2", "continue_ipmsm_v2_stage2.py"),
        ("calibrate_ipmsm_beta", "calibrate_ipmsm_beta.py"),
        ("ipmsm_optimization", "ipmsm_optimization.py"),
        ("ipmsm_surrogate_bundle", "ipmsm_surrogate_bundle.py"),
        ("optimize_ipmsm_nsga2", "optimize_ipmsm_nsga2.py"),
        ("run_ipmsm_v2_campaign", "run_ipmsm_v2_campaign.py"),
        ("submit_ipmsm_v2_campaign", "submit_ipmsm_v2_campaign.py"),
        ("validate_ipmsm_pareto_fea", "validate_ipmsm_pareto_fea.py"),
        ("atomic_publish", "atomic_publish.py"),
        ("collect_ipmsm_v2_campaign", "collect_ipmsm_v2_campaign.py"),
        ("generate_ipmsm_quality_cases", "generate_ipmsm_quality_cases.py"),
        ("generate_ipmsm_v2_cases", "generate_ipmsm_v2_cases.py"),
        ("inspect_ipmsm_scheduler_job", "inspect_ipmsm_scheduler_job.py"),
        ("merge_ipmsm_v2_results", "merge_ipmsm_v2_results.py"),
        ("module.ipmsm_geometry", "module/ipmsm_geometry.py"),
        ("module.ipmsm_ppt_setup", "module/ipmsm_ppt_setup.py"),
        ("module.variable", "module/variable.py"),
        ("run_ipmsm_batch", "run_ipmsm_batch.py"),
        ("submit_ipmsm_scheduler_job", "submit_ipmsm_scheduler_job.py"),
        ("submit_ipmsm_scheduler_task", "submit_ipmsm_scheduler_task.py"),
        ("subprocess_run", "subprocess_run.py"),
        ("train_ipmsm_lightgbm", "train_ipmsm_lightgbm.py"),
        ("validate_ipmsm_v2_dataset", "validate_ipmsm_v2_dataset.py"),
        ("verify_regression_metrics", "verify_regression_metrics.py"),
    )


def _frozen_legacy_optimizer_declared_source_filenames() -> tuple[str, ...]:
    """Return the exact legacy ``SOURCE_CONTRACT_FILES`` portion of the closure."""

    return tuple(
        filename
        for _, filename in _frozen_legacy_optimization_source_modules()[:9]
    )


LEGACY_OPTIMIZATION_SOURCE_MODULES = _frozen_legacy_optimization_source_modules()
LEGACY_OPTIMIZATION_SOURCE_FILENAMES = tuple(
    filename for _, filename in LEGACY_OPTIMIZATION_SOURCE_MODULES
)


def _optimization_source_pin_key(module_name: str) -> str:
    if module_name == "verify_regression_metrics":
        return "verification_helper"
    return f"optimization_source_{module_name.replace('.', '__')}"


LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS = {
    module_name: _optimization_source_pin_key(module_name)
    for module_name, _ in LEGACY_OPTIMIZATION_SOURCE_MODULES
}

SOURCE_PIN_FILENAMES = {
    "supervisor_v3": "supervise_ipmsm_v2_pipeline.py",
    "supervisor_v4": "supervise_ipmsm_v2_pipeline_v4.py",
    "stage1_publisher_v4": "publish_ipmsm_v2_stage1_official_v4.py",
    "verification_helper": "verify_regression_metrics.py",
    "confirmation_helper": "confirm_ipmsm_v2_optimization_inputs.py",
    "optimization_authorizer_v4": "authorize_ipmsm_v2_optimization_v4.py",
    "optimization_runner_v4": "continue_ipmsm_v2_optimization_v4.py",
    **{
        LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[module_name]: filename
        for module_name, filename in LEGACY_OPTIMIZATION_SOURCE_MODULES
        if LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[module_name]
        != "verification_helper"
    },
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
CONTRACT_ATTEMPT_MARKER = ".v4-contract-attempt."
CONTRACT_STAGE_READY_NAME = "stage-ready"
CONTRACT_STAGED_SUFFIX = ".v4-contract-stage"
CONTRACT_PROOF_SUFFIX = ".v4-contract-proof.json"
CONTRACT_PROOF_SCHEMA_VERSION = "atomic-no-replace-proof-v1"
AUTHORIZATION_AUDIT_FIELDS = {
    "status",
    "authorized",
    "receipt_path",
    "receipt_raw_sha256",
    "receipt_sha256",
    "confirmation_path",
    "confirmation_raw_sha256",
    "confirmation_canonical_sha256",
    "confirmation_sha256",
    "declaration_path",
    "declaration_raw_sha256",
    "declaration_canonical_sha256",
    "contract_path",
    "contract_raw_sha256",
    "contract_canonical_sha256",
    "contract_sha256",
    "base_contract_path",
    "base_contract_raw_sha256",
    "base_contract_canonical_sha256",
    "base_contract_sha256",
    "optimization_spec_path",
    "optimization_spec_raw_sha256",
    "optimization_spec_canonical_sha256",
    "optimization_spec_schema_version",
    "optimization_implementation_path",
    "optimization_implementation_sha256",
    "confirmation_helper_path",
    "confirmation_helper_sha256",
    "confirmed_by",
    "confirmed_at_utc",
    "evidence_reference",
    "attestation_kind",
    "duty_basis",
    "authorization_effective_at_utc",
}

PipelineContractError = v3.PipelineContractError
PipelineStateError = v3.PipelineStateError


def _audit_legacy_optimization_source_manifest() -> tuple[tuple[str, str], ...]:
    """Reject mutable-global drift from the source-pinned manifest literal."""

    frozen = _frozen_legacy_optimization_source_modules()
    filenames = tuple(filename for _, filename in frozen)
    pin_keys = {
        module_name: _optimization_source_pin_key(module_name)
        for module_name, _ in frozen
    }
    if LEGACY_OPTIMIZATION_SOURCE_MODULES != frozen:
        raise PipelineContractError("legacy optimizer source module manifest changed in memory")
    if LEGACY_OPTIMIZATION_SOURCE_FILENAMES != filenames:
        raise PipelineContractError("legacy optimizer source filename manifest changed in memory")
    if LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS != pin_keys:
        raise PipelineContractError("legacy optimizer source pin-key manifest changed in memory")
    expected_pins = {
        pin_keys[module_name]: filename
        for module_name, filename in frozen
        if pin_keys[module_name] != "verification_helper"
    }
    actual_pins = {
        key: value
        for key, value in SOURCE_PIN_FILENAMES.items()
        if key.startswith("optimization_source_")
    }
    if actual_pins != expected_pins:
        raise PipelineContractError("legacy optimizer SOURCE_PIN_FILENAMES manifest changed")
    if SOURCE_PIN_FILENAMES.get("verification_helper") != "verify_regression_metrics.py":
        raise PipelineContractError("verification helper source-pin role changed")
    return frozen


@dataclasses.dataclass(frozen=True)
class BoundArtifact:
    path: Path
    sha256: str
    canonical_sha256: str | None = None
    contract_sha256: str | None = None


@dataclasses.dataclass(frozen=True)
class Stage1OfficialContract:
    workspace: Path
    completion: Path
    publisher_argv: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class OptimizationConfirmationContract:
    declaration: Path
    confirmation: Path
    receipt: Path
    authorizer_argv: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class AuthorizedOptimizationContract:
    wrapper_argv_template: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class V4Contract:
    source: Path
    source_sha256: str
    canonical_sha256: str
    contract_sha256: str
    workdir: Path
    lock_path: Path
    base_contract_binding: BoundArtifact
    base_contract: v3.PipelineContract
    immutable_inputs: tuple[BoundArtifact, ...]
    source_pins: Mapping[str, BoundArtifact]
    stage1_official: Stage1OfficialContract
    optimization_confirmation: OptimizationConfirmationContract
    optimization: AuthorizedOptimizationContract


@dataclasses.dataclass(frozen=True)
class PipelineSnapshot:
    next_action: str
    branch: str
    upstream_decision: Path | None = None
    terminal: bool = False
    exit_code: int = 0
    detail: Mapping[str, Any] | None = None

    def report(
        self, contract: V4Contract, *, mode: str, transitions: int = 0
    ) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "contract": str(contract.source),
            "contract_sha256": contract.contract_sha256,
            "detail": dict(self.detail or {}),
            "mode": mode,
            "next_action": self.next_action,
            "schema_version": REPORT_SCHEMA_VERSION,
            "terminal": self.terminal,
            "transitions": transitions,
            "upstream_decision": (
                str(self.upstream_decision) if self.upstream_decision is not None else ""
            ),
            "writes_performed": transitions if mode == "execute" else 0,
        }


def _file_sha256(path: Path) -> str:
    payload, _ = _stable_regular_bytes(path, "hashed file")
    return hashlib.sha256(payload).hexdigest()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _reject_link_components(path: Path, label: str, *, include_leaf: bool = True) -> None:
    absolute = _absolute(path)
    parts = absolute.parts
    current = Path(parts[0])
    limit = len(parts) if include_leaf else max(1, len(parts) - 1)
    for part in parts[1:limit]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise PipelineContractError(f"cannot audit {label} path component: {current}") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise PipelineContractError(f"{label} traverses a link or reparse point: {current}")


def _canonical_no_links(path: Path, label: str) -> Path:
    """Canonicalize mapped-drive aliases only after rejecting link traversal."""

    lexical = _absolute(path)
    _reject_link_components(lexical, label)
    try:
        resolved = lexical.resolve(strict=False)
    except OSError as exc:
        raise PipelineContractError(f"cannot canonicalize {label}: {lexical}") from exc
    # Repeat both checks around resolution to close ordinary rename races and
    # to reject a canonical target that itself traverses a reparse component.
    _reject_link_components(lexical, label)
    _reject_link_components(resolved, label)
    return resolved


def _stable_regular_bytes(path: Path, label: str) -> tuple[bytes, os.stat_result]:
    source = _absolute(path)
    _reject_link_components(source, label)
    try:
        before = os.lstat(source)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
            raise PipelineContractError(f"{label} is not a regular non-reparse file: {source}")
        if before.st_nlink != 1:
            raise PipelineContractError(f"{label} must have exactly one hard link: {source}")
        with source.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise PipelineContractError(f"{label} changed while being opened: {source}")
            payload = stream.read()
            after_open = os.fstat(stream.fileno())
        after = os.lstat(source)
    except PipelineContractError:
        raise
    except OSError as exc:
        raise PipelineContractError(f"cannot read {label}: {source}: {exc}") from exc
    if _identity(before) != _identity(after_open) or _identity(before) != _identity(after):
        raise PipelineContractError(f"{label} changed while being read: {source}")
    return payload, after


def _strict_document(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        payload, _ = _stable_regular_bytes(path, label)
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=v3._unique_object,
            parse_constant=v3._reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineContractError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineContractError(f"{label} must be a JSON object")
    return payload, value


def _artifact(value: Any, workdir: Path, label: str, *, extended: bool = False) -> BoundArtifact:
    item = v3._mapping(value, label)
    expected = {"path", "raw_sha256", "canonical_sha256", "contract_sha256"} if extended else {
        "path", "sha256"
    }
    v3._expect_keys(item, expected, label)
    path = _canonical_no_links(
        v3._path(item["path"], workdir, f"{label}.path"), f"{label}.path"
    )
    digest_key = "raw_sha256" if extended else "sha256"
    digest = item[digest_key]
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        raise PipelineContractError(f"{label}.{digest_key} is invalid")
    if extended:
        canonical = item["canonical_sha256"]
        contract_hash = item["contract_sha256"]
        if not all(
            isinstance(entry, str) and SHA256_PATTERN.fullmatch(entry) is not None
            for entry in (canonical, contract_hash)
        ):
            raise PipelineContractError(f"{label} semantic hashes are invalid")
        return BoundArtifact(path, digest.lower(), canonical.lower(), contract_hash.lower())
    return BoundArtifact(path, digest.lower())


def _script_path(argv: Sequence[str], filename: str, workdir: Path, label: str) -> Path:
    matches = [item for item in argv[1:] if Path(item).name.lower() == filename.lower()]
    if len(matches) != 1:
        raise PipelineContractError(f"{label} must invoke {filename} exactly once")
    return _canonical_no_links(
        v3._path(matches[0], workdir, f"{label} script"), f"{label} script"
    )


def _require_flag_path(argv: Sequence[str], flag: str, expected: Path, workdir: Path, label: str) -> None:
    v3._require_flag_path(argv, flag, expected, workdir, label)


def _same_configured_path(
    left: str | Path, right: str | Path, workdir: Path, label: str
) -> bool:
    left_path = _canonical_no_links(
        v3._path(str(left), workdir, f"{label} actual"), f"{label} actual"
    )
    right_path = _canonical_no_links(
        v3._path(str(right), workdir, f"{label} expected"), f"{label} expected"
    )
    try:
        return os.path.samefile(left_path, right_path)
    except (FileNotFoundError, OSError, ValueError):
        return left_path == right_path


def _require_exact_argv(
    actual: Sequence[str],
    expected: Sequence[str],
    *,
    path_positions: set[int],
    workdir: Path,
    label: str,
) -> None:
    if len(actual) != len(expected):
        raise PipelineContractError(f"{label} differs from its deterministic argv template")
    for index, (value, required) in enumerate(zip(actual, expected)):
        if index in path_positions:
            matches = _same_configured_path(
                value, required, workdir, f"{label}[{index}]"
            )
        else:
            matches = value == required
        if not matches:
            raise PipelineContractError(
                f"{label} differs from its deterministic argv template at index {index}"
            )


def _validate_legacy_optimizer_arguments(argv: Sequence[str]) -> None:
    required = {
        "--stage2-decision",
        "--optimization-spec",
        "--beta-summary",
        "--beta-case-plan",
        "--beta-results",
        "--beta-calibration-manifest",
        "--output-dir",
        "--checkpoint-dir",
        "--decision-output",
        "--project",
    }
    optional = {
        "--scheduler-url",
        "--project-active-cap",
        "--max-fea-candidates",
        "--task-prefix",
        "--remote-cases-dir",
        "--result-dir",
        "--simulation-dir",
        "--log-dir",
        "--poll-interval-seconds",
        "--overall-timeout-seconds",
        "--terminal-retry-limit",
        "--minimum-coverage",
        "--identity-relative-tolerance",
    }
    if len(argv) % 2 or not argv or argv[0] != "--stage2-decision":
        raise PipelineContractError(
            "base optimization arguments must be ordered flag/value pairs beginning with --stage2-decision"
        )
    flags: list[str] = []
    for index in range(0, len(argv), 2):
        flag, value = argv[index : index + 2]
        if flag not in required | optional or value.startswith("--"):
            raise PipelineContractError(
                f"base optimization arguments contain an unsupported token: {flag}"
            )
        flags.append(flag)
    if len(flags) != len(set(flags)):
        raise PipelineContractError("base optimization arguments contain a duplicate flag")
    missing = required - set(flags)
    if missing:
        raise PipelineContractError(
            "base optimization arguments lack required flags: "
            + ", ".join(sorted(missing))
        )


def load_contract(path: str | Path) -> V4Contract:
    """Load and structurally validate an immutable v4 envelope."""

    _audit_legacy_optimization_source_manifest()
    lexical_source = _absolute(Path(path))
    payload, document = _strict_document(lexical_source, "v4 pipeline contract")
    source = _canonical_no_links(lexical_source, "v4 pipeline contract")
    if source != lexical_source:
        canonical_payload, canonical_document = _strict_document(
            source, "canonical v4 pipeline contract"
        )
        if canonical_payload != payload or canonical_document != document:
            raise PipelineContractError("v4 mapped-path aliases resolved to different bytes")
    v3._expect_keys(document, {"schema_version", "contract_sha256", "pipeline"}, "contract")
    if document["schema_version"] != CONTRACT_SCHEMA_VERSION:
        raise PipelineContractError("unsupported v4 pipeline contract schema_version")
    pipeline = v3._mapping(document["pipeline"], "pipeline")
    v3._expect_keys(
        pipeline,
        {
            "workdir",
            "shared_lock",
            "base_contract",
            "immutable_inputs",
            "source_pins",
            "stage1_official",
            "optimization_confirmation",
            "optimization",
        },
        "pipeline",
    )
    expected_contract_hash = v3._canonical_sha256(
        {"schema_version": CONTRACT_SCHEMA_VERSION, "pipeline": pipeline}
    )
    if document["contract_sha256"] != expected_contract_hash:
        raise PipelineContractError("v4 pipeline contract_sha256 mismatch")
    workdir = _canonical_no_links(
        v3._path(pipeline["workdir"], source.parent, "pipeline.workdir"),
        "pipeline.workdir",
    )
    lock_path = _canonical_no_links(
        v3._path(pipeline["shared_lock"], workdir, "pipeline.shared_lock"),
        "pipeline.shared_lock",
    )
    _reject_link_components(workdir, "pipeline.workdir")
    _reject_link_components(lock_path, "pipeline.shared_lock")

    base_binding = _artifact(pipeline["base_contract"], workdir, "base_contract", extended=True)
    base_payload, base_document = _strict_document(base_binding.path, "base pipeline contract")
    if hashlib.sha256(base_payload).hexdigest() != base_binding.sha256:
        raise PipelineContractError("base contract raw_sha256 mismatch")
    if v3._canonical_sha256(base_document) != base_binding.canonical_sha256:
        raise PipelineContractError("base contract canonical_sha256 mismatch")
    if base_document.get("contract_sha256") != base_binding.contract_sha256:
        raise PipelineContractError("base contract contract_sha256 mismatch")
    base_contract = v3.load_contract(base_binding.path)
    if (
        _absolute(base_contract.source) != base_binding.path
        or base_contract.contract_sha256 != base_binding.contract_sha256
    ):
        raise PipelineContractError("loaded base contract differs from the exact v4 binding")
    if _canonical_no_links(base_contract.workdir, "base workdir") != workdir:
        raise PipelineContractError("v4 workdir must exactly match the base contract workdir")
    if _canonical_no_links(base_contract.lock_path, "base shared lock") != lock_path:
        raise PipelineContractError("v4 shared_lock must exactly match the base v3 lock")
    for flag, expected in (
        ("--stage1-validation", base_contract.stage1.validation),
        ("--stage1-metadata", base_contract.stage1.metadata),
        ("--stage1-r2", base_contract.stage1.r2),
    ):
        _require_flag_path(
            base_contract.stage2.argv, flag, expected, workdir, "base Stage2 continuation"
        )

    immutable_raw = pipeline["immutable_inputs"]
    if not isinstance(immutable_raw, list) or not immutable_raw:
        raise PipelineContractError("pipeline.immutable_inputs must be a nonempty array")
    immutable = tuple(
        _artifact(item, workdir, f"immutable_inputs[{index}]")
        for index, item in enumerate(immutable_raw)
    )
    if len({item.path for item in immutable}) != len(immutable):
        raise PipelineContractError("v4 immutable input paths must be unique")

    pins_raw = v3._mapping(pipeline["source_pins"], "source_pins")
    v3._expect_keys(pins_raw, set(SOURCE_PIN_FILENAMES), "source_pins")
    pins = {
        name: _artifact(value, workdir, f"source_pins.{name}")
        for name, value in pins_raw.items()
    }
    for name, filename in SOURCE_PIN_FILENAMES.items():
        if pins[name].path.name.lower() != Path(filename).name.lower():
            raise PipelineContractError(f"source_pins.{name} must name {filename}")
    for module_name, filename in LEGACY_OPTIMIZATION_SOURCE_MODULES:
        key = LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[module_name]
        expected = _canonical_no_links(
            workdir / filename, f"source_pins.{key} expected path"
        )
        if pins[key].path != expected:
            raise PipelineContractError(
                f"source_pins.{key} must bind the workdir optimizer source"
            )
    legacy_runner_key = LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[
        "continue_ipmsm_v2_optimization"
    ]
    if _script_path(
        base_contract.optimization.argv_template,
        "continue_ipmsm_v2_optimization.py",
        workdir,
        "base optimization",
    ) != pins[legacy_runner_key].path:
        raise PipelineContractError(
            "legacy optimizer source pin differs from the base optimization script"
        )
    expected_immutable = {(base_binding.path, base_binding.sha256)} | {
        (pin.path, pin.sha256) for pin in pins.values()
    }
    if {(item.path, item.sha256) for item in immutable} != expected_immutable:
        raise PipelineContractError("immutable_inputs must exactly bind the base contract and source pins")

    official_raw = v3._mapping(pipeline["stage1_official"], "stage1_official")
    v3._expect_keys(official_raw, {"workspace", "completion", "publisher_argv"}, "stage1_official")
    workspace = _canonical_no_links(
        v3._path(official_raw["workspace"], workdir, "stage1_official.workspace"),
        "stage1_official.workspace",
    )
    completion = _canonical_no_links(
        v3._path(official_raw["completion"], workdir, "stage1_official.completion"),
        "stage1_official.completion",
    )
    if completion != _canonical_no_links(
        workspace / "completion.json", "stage1_official.completion"
    ):
        raise PipelineContractError("stage1_official.completion must be workspace/completion.json")
    publisher_argv = v3._argv(
        official_raw["publisher_argv"],
        "stage1_official.publisher_argv",
        SOURCE_PIN_FILENAMES["stage1_publisher_v4"],
    )
    _require_flag_path(publisher_argv, "--pipeline-contract", source, workdir, "Stage1 publisher")
    _require_flag_path(
        publisher_argv, "--base-contract", base_binding.path, workdir, "Stage1 publisher"
    )
    _require_flag_path(publisher_argv, "--workspace", workspace, workdir, "Stage1 publisher")
    if "--execute" in publisher_argv:
        raise PipelineContractError("Stage1 publisher template must omit --execute")

    confirmation_raw = v3._mapping(
        pipeline["optimization_confirmation"], "optimization_confirmation"
    )
    v3._expect_keys(
        confirmation_raw,
        {"declaration", "confirmation", "receipt", "authorizer_argv"},
        "optimization_confirmation",
    )
    declaration = _canonical_no_links(
        v3._path(
            confirmation_raw["declaration"],
            workdir,
            "optimization_confirmation.declaration",
        ),
        "optimization_confirmation.declaration",
    )
    confirmation = _canonical_no_links(
        v3._path(
            confirmation_raw["confirmation"],
            workdir,
            "optimization_confirmation.confirmation",
        ),
        "optimization_confirmation.confirmation",
    )
    receipt = _canonical_no_links(
        v3._path(
            confirmation_raw["receipt"], workdir, "optimization_confirmation.receipt"
        ),
        "optimization_confirmation.receipt",
    )
    authorizer_argv = v3._argv(
        confirmation_raw["authorizer_argv"],
        "optimization_confirmation.authorizer_argv",
        SOURCE_PIN_FILENAMES["optimization_authorizer_v4"],
    )
    _require_flag_path(authorizer_argv, "--contract", source, workdir, "optimization authorizer")
    _require_flag_path(authorizer_argv, "--confirmation", confirmation, workdir, "optimization authorizer")
    _require_flag_path(authorizer_argv, "--output", receipt, workdir, "optimization authorizer")
    if "--execute" in authorizer_argv or "--audit-receipt" in authorizer_argv:
        raise PipelineContractError("optimization authorizer template must be an uncommitted output action")

    optimization_raw = v3._mapping(pipeline["optimization"], "optimization")
    v3._expect_keys(optimization_raw, {"wrapper_argv_template"}, "optimization")
    wrapper_argv = v3._argv(
        optimization_raw["wrapper_argv_template"],
        "optimization.wrapper_argv_template",
        SOURCE_PIN_FILENAMES["optimization_runner_v4"],
        placeholder=True,
    )
    _require_flag_path(wrapper_argv, "--pipeline-contract", source, workdir, "optimization wrapper")
    _require_flag_path(wrapper_argv, "--authorization-receipt", receipt, workdir, "optimization wrapper")
    _require_flag_path(wrapper_argv, "--confirmation", confirmation, workdir, "optimization wrapper")
    if v3._flag_value(wrapper_argv, "--stage2-decision", "optimization wrapper") != UPSTREAM_PLACEHOLDER:
        raise PipelineContractError("optimization wrapper upstream placeholder is invalid")
    _require_flag_path(
        wrapper_argv,
        "--decision-output",
        base_contract.optimization.decision,
        workdir,
        "optimization wrapper",
    )
    if "--execute" in wrapper_argv or "--resume" in wrapper_argv:
        raise PipelineContractError("optimization wrapper template must omit execution mode flags")
    legacy = base_contract.optimization.argv_template
    if (
        len(legacy) < 2
        or Path(legacy[1]).name.lower() != "continue_ipmsm_v2_optimization.py"
    ):
        raise PipelineContractError(
            "base optimization argv must invoke its pinned legacy script at index 1"
        )
    _validate_legacy_optimizer_arguments(legacy[2:])

    script_bindings = {
        "stage1_publisher_v4": _script_path(
            publisher_argv, SOURCE_PIN_FILENAMES["stage1_publisher_v4"], workdir, "Stage1 publisher"
        ),
        "optimization_authorizer_v4": _script_path(
            authorizer_argv,
            SOURCE_PIN_FILENAMES["optimization_authorizer_v4"],
            workdir,
            "optimization authorizer",
        ),
        "optimization_runner_v4": _script_path(
            wrapper_argv, SOURCE_PIN_FILENAMES["optimization_runner_v4"], workdir, "optimization wrapper"
        ),
    }
    for name, script in script_bindings.items():
        if script != pins[name].path:
            raise PipelineContractError(f"source_pins.{name} does not match its argv script")
    python = legacy[0]
    _require_exact_argv(
        publisher_argv,
        (
            python,
            str(pins["stage1_publisher_v4"].path),
            "--pipeline-contract",
            str(source),
            "--base-contract",
            str(base_binding.path),
            "--workspace",
            str(workspace),
        ),
        path_positions={0, 1, 3, 5, 7},
        workdir=workdir,
        label="Stage1 publisher argv",
    )
    _require_exact_argv(
        authorizer_argv,
        (
            python,
            str(pins["optimization_authorizer_v4"].path),
            "--contract",
            str(source),
            "--confirmation",
            str(confirmation),
            "--output",
            str(receipt),
        ),
        path_positions={0, 1, 3, 5, 7},
        workdir=workdir,
        label="optimization authorizer argv",
    )
    _require_exact_argv(
        wrapper_argv,
        (
            python,
            str(pins["optimization_runner_v4"].path),
            "--pipeline-contract",
            str(source),
            "--authorization-receipt",
            str(receipt),
            "--confirmation",
            str(confirmation),
            *legacy[2:],
        ),
        path_positions={0, 1, 3, 5, 7},
        workdir=workdir,
        label="optimization wrapper argv",
    )

    protected_outputs = {
        workspace,
        completion,
        declaration,
        confirmation,
        receipt,
        _absolute(base_contract.optimization.decision),
    }
    protected_inputs = (
        {item.path for item in immutable}
        | {_absolute(item.path) for item in base_contract.immutable_inputs}
        | {source}
    )
    base_outputs = {
        _absolute(base_contract.stage1.output_dir),
        _absolute(base_contract.stage1.validation),
        _absolute(base_contract.stage1.model_dir),
        _absolute(base_contract.stage1.r2),
        _absolute(base_contract.stage2.decision),
        _absolute(base_contract.stage3.prior_plan),
        _absolute(base_contract.stage3.prior_manifest),
        _absolute(base_contract.stage3.plan),
        _absolute(base_contract.stage3.manifest),
        _absolute(base_contract.stage3.decision),
        _absolute(base_contract.optimization.decision),
        _absolute(base_contract.speed.plan),
        _absolute(base_contract.speed.output_dir),
        _absolute(base_contract.speed.rank),
        _absolute(base_contract.speed.top),
        _absolute(base_contract.speed.marker),
    }
    if protected_outputs & protected_inputs or lock_path in protected_outputs | protected_inputs:
        raise PipelineContractError("v4 inputs, outputs, and shared lock must be distinct")
    if len({completion, declaration, confirmation, receipt}) != 4:
        raise PipelineContractError("v4 completion and authorization artifacts must be distinct")
    for candidate, label in (
        (workspace, "stage1_official.workspace"),
        (completion, "stage1_official.completion"),
        (declaration, "optimization_confirmation.declaration"),
        (confirmation, "optimization_confirmation.confirmation"),
        (receipt, "optimization_confirmation.receipt"),
    ):
        _reject_link_components(candidate, label)
    for sensitive in protected_inputs | base_outputs | {lock_path}:
        if sensitive == workspace or sensitive in workspace.parents or workspace in sensitive.parents:
            raise PipelineContractError(
                "stage1 official workspace must not contain or be contained by an authority path"
            )
    independent_outputs = (declaration, confirmation, receipt, _absolute(base_contract.optimization.decision))
    for index, left in enumerate(independent_outputs):
        for right in independent_outputs[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise PipelineContractError("v4 output paths must not alias by containment")
    for output in (declaration, confirmation, receipt):
        for reserved in protected_inputs | base_outputs | {lock_path, workspace, completion}:
            if output == reserved or output in reserved.parents or reserved in output.parents:
                raise PipelineContractError(
                    "v4 authorization outputs must not alias reserved paths by containment"
                )

    contract = V4Contract(
        source=source,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        canonical_sha256=v3._canonical_sha256(document),
        contract_sha256=expected_contract_hash,
        workdir=workdir,
        lock_path=lock_path,
        base_contract_binding=base_binding,
        base_contract=base_contract,
        immutable_inputs=immutable,
        source_pins=pins,
        stage1_official=Stage1OfficialContract(workspace, completion, publisher_argv),
        optimization_confirmation=OptimizationConfirmationContract(
            declaration, confirmation, receipt, authorizer_argv
        ),
        optimization=AuthorizedOptimizationContract(wrapper_argv),
    )
    audit_contract(contract)
    return contract


def audit_contract(contract: V4Contract) -> None:
    """Re-read every authority byte and fail if the loaded contract went stale."""

    _audit_legacy_optimization_source_manifest()
    payload, document = _strict_document(contract.source, "v4 pipeline contract")
    if hashlib.sha256(payload).hexdigest() != contract.source_sha256:
        raise PipelineStateError("v4 pipeline contract raw bytes changed")
    if v3._canonical_sha256(document) != contract.canonical_sha256:
        raise PipelineStateError("v4 pipeline contract canonical content changed")
    if document.get("contract_sha256") != contract.contract_sha256:
        raise PipelineStateError("v4 pipeline contract semantic hash changed")
    for item in contract.immutable_inputs:
        if not item.path.is_file() or _file_sha256(item.path) != item.sha256:
            raise PipelineStateError(f"v4 immutable input is missing or changed: {item.path}")
    for item in contract.base_contract.immutable_inputs:
        path = _absolute(item.path)
        if not path.is_file() or _file_sha256(path) != item.sha256:
            raise PipelineStateError(f"base immutable input is missing or changed: {path}")
    if _canonical_no_links(Path(v3.__file__), "loaded v3 supervisor") != contract.source_pins["supervisor_v3"].path:
        raise PipelineStateError("loaded v3 supervisor differs from its source pin")
    if _canonical_no_links(Path(__file__), "loaded v4 supervisor") != contract.source_pins["supervisor_v4"].path:
        raise PipelineStateError("loaded v4 supervisor differs from its source pin")
    base_payload, base_document = _strict_document(
        contract.base_contract_binding.path, "base pipeline contract"
    )
    if hashlib.sha256(base_payload).hexdigest() != contract.base_contract_binding.sha256:
        raise PipelineStateError("base contract changed after v4 load")
    if (
        v3._canonical_sha256(base_document)
        != contract.base_contract_binding.canonical_sha256
        or base_document.get("contract_sha256")
        != contract.base_contract_binding.contract_sha256
    ):
        raise PipelineStateError("base contract semantic identity changed after v4 load")
    for candidate, label in (
        (contract.lock_path, "pipeline.shared_lock"),
        (contract.stage1_official.workspace, "stage1_official.workspace"),
        (contract.stage1_official.completion, "stage1_official.completion"),
        (contract.optimization_confirmation.declaration, "optimization declaration"),
        (contract.optimization_confirmation.confirmation, "optimization confirmation"),
        (contract.optimization_confirmation.receipt, "optimization receipt"),
    ):
        _reject_link_components(candidate, label)
    v3.audit_immutable_inputs(contract.base_contract)


@dataclasses.dataclass(frozen=True)
class AuditedOfficialStage1:
    bundle: Any
    stage1: v3.Stage1Contract
    gate: Any


@dataclasses.dataclass(frozen=True)
class AuditedAuthorization:
    audit: Any
    mapping: Mapping[str, Any]
    record: Mapping[str, Any]


def _loaded_module(name: str, pin: BoundArtifact) -> Any:
    module = importlib.import_module(name)
    module_path = Path(module.__file__).resolve(strict=False)
    try:
        same_source = os.path.samefile(module_path, pin.path)
    except (FileNotFoundError, OSError, ValueError):
        same_source = (
            _canonical_no_links(module_path, f"loaded {name} source")
            == _canonical_no_links(pin.path, f"pinned {name} source")
        )
    if not same_source or _file_sha256(module_path) != pin.sha256:
        raise PipelineStateError(f"loaded helper differs from source pin: {name}")
    return module


def _completion_requires_publication_inspection(contract: V4Contract) -> bool:
    completion = contract.stage1_official.completion
    proof = completion.with_name(f".{completion.name}.publish-proof.json")
    if os.path.lexists(proof):
        return True
    if not os.path.lexists(completion):
        return False
    try:
        return int(os.lstat(completion).st_nlink) != 1
    except OSError as exc:
        raise PipelineStateError(
            f"cannot inspect Stage1 completion publication identity: {completion}"
        ) from exc


def inspect_pending_official_publications(contract: V4Contract) -> tuple[str, ...]:
    """Read-only replay of publisher-owned crash-recovery authority."""

    publisher = _loaded_module(
        "publish_ipmsm_v2_stage1_official_v4",
        contract.source_pins["stage1_publisher_v4"],
    )
    inspect_pending = getattr(publisher, "inspect_pending_publications", None)
    if not callable(inspect_pending):
        raise PipelineStateError(
            "pinned Stage1 publisher lacks pending-publication inspection"
        )
    try:
        pending = inspect_pending(
            contract.source,
            contract.base_contract_binding.path,
            contract.stage1_official.workspace,
        )
    except Exception as exc:
        raise PipelineStateError(
            f"Stage1 pending-publication audit failed: {exc}"
        ) from exc
    if (
        not isinstance(pending, tuple)
        or any(not isinstance(item, str) or not item for item in pending)
        or pending != tuple(sorted(set(pending)))
    ):
        raise PipelineStateError(
            "Stage1 pending-publication inspection returned an invalid manifest"
        )
    audit_contract(contract)
    return pending


def audit_official_stage1(contract: V4Contract) -> AuditedOfficialStage1:
    """Resolve all official validation/model/R2 paths through completion only."""

    publisher = _loaded_module(
        "publish_ipmsm_v2_stage1_official_v4",
        contract.source_pins["stage1_publisher_v4"],
    )
    try:
        bundle = publisher.audit_completion(
            contract.stage1_official.completion,
            contract,
            workspace=contract.stage1_official.workspace,
        )
    except Exception as exc:
        raise PipelineStateError(f"Stage1 official completion audit failed: {exc}") from exc
    required_paths = ("completion_path", "validation", "model_dir", "metadata", "r2")
    if any(not hasattr(bundle, name) for name in required_paths):
        raise PipelineStateError("Stage1 official audit returned an incomplete bundle")
    if _canonical_no_links(Path(bundle.completion_path), "official completion") != contract.stage1_official.completion:
        raise PipelineStateError("Stage1 official audit resolved a different completion")
    base = contract.base_contract.stage1
    if getattr(bundle, "result_sha256", None) != _file_sha256(base.result):
        raise PipelineStateError("Stage1 official completion binds a different result")
    official = dataclasses.replace(
        base,
        validation=_canonical_no_links(Path(bundle.validation), "official validation"),
        model_dir=_canonical_no_links(Path(bundle.model_dir), "official model directory"),
        metadata=_canonical_no_links(Path(bundle.metadata), "official metadata"),
        r2=_canonical_no_links(Path(bundle.r2), "official R2"),
    )
    gate = v3._audit_stage1_training(official)
    recorded_gate = getattr(bundle, "gate", None)
    if recorded_gate is None or not hasattr(recorded_gate, "summary"):
        raise PipelineStateError("Stage1 official completion lacks its audited surrogate gate")
    if recorded_gate.summary() != gate.summary():
        raise PipelineStateError("Stage1 official completion gate differs from live replay")
    audit_contract(contract)
    return AuditedOfficialStage1(bundle=bundle, stage1=official, gate=gate)


def _replace_flag(argv: Sequence[str], flag: str, value: Path, label: str) -> tuple[str, ...]:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise PipelineContractError(f"{label} must contain exactly one {flag} value")
    result = list(argv)
    result[positions[0] + 1] = str(value)
    return tuple(result)


def official_stage2_argv(contract: V4Contract, official: AuditedOfficialStage1) -> tuple[str, ...]:
    """Return Stage2 argv with legacy Stage1 outputs replaced by completion paths."""

    argv = contract.base_contract.stage2.argv
    for flag, path in (
        ("--stage1-validation", official.stage1.validation),
        ("--stage1-metadata", official.stage1.metadata),
        ("--stage1-r2", official.stage1.r2),
    ):
        argv = _replace_flag(argv, flag, path, "Stage2 continuation")
    return argv


def _artifact_record(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve(strict=False)), "sha256": _file_sha256(path)}


def audit_stage2_official_binding(
    decision: Mapping[str, Any], official: AuditedOfficialStage1
) -> None:
    execution = decision.get("execution_contract")
    stage1 = execution.get("stage1") if isinstance(execution, Mapping) else None
    if not isinstance(stage1, Mapping):
        raise PipelineStateError("Stage2 decision lacks execution_contract.stage1")
    expected = {
        "result": _artifact_record(official.stage1.result),
        "validation": _artifact_record(official.stage1.validation),
        "metadata": _artifact_record(official.stage1.metadata),
        "r2": _artifact_record(official.stage1.r2),
    }
    for name, artifact in expected.items():
        if stage1.get(name) != artifact:
            raise PipelineStateError(f"Stage2 decision did not bind official Stage1 {name}")


def _authorization_mapping(audit: Any, contract: V4Contract | None = None) -> dict[str, Any]:
    if not hasattr(audit, "as_mapping"):
        raise PipelineStateError("authorization audit does not expose as_mapping()")
    value = audit.as_mapping()
    if not isinstance(value, Mapping):
        raise PipelineStateError("authorization audit mapping is invalid")
    try:
        # A JSON round-trip rejects custom/lossy objects before they become a
        # durable optimizer execution contract.
        mapping = json.loads(json.dumps(dict(value), allow_nan=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise PipelineStateError("authorization audit mapping is not canonical JSON data") from exc
    if set(mapping) != AUTHORIZATION_AUDIT_FIELDS:
        raise PipelineStateError("authorization audit mapping fields are not the exact v4 schema")
    if mapping["status"] != "authorized" or mapping["authorized"] is not True:
        raise PipelineStateError("authorization audit does not grant authorization")
    for name in AUTHORIZATION_AUDIT_FIELDS:
        if name.endswith("sha256") and (
            not isinstance(mapping[name], str)
            or SHA256_PATTERN.fullmatch(mapping[name]) is None
        ):
            raise PipelineStateError(f"authorization audit has invalid {name}")
    if contract is not None:
        exact = {
            "receipt_path": str(contract.optimization_confirmation.receipt),
            "receipt_raw_sha256": _file_sha256(contract.optimization_confirmation.receipt),
            "confirmation_path": str(contract.optimization_confirmation.confirmation),
            "confirmation_raw_sha256": _file_sha256(
                contract.optimization_confirmation.confirmation
            ),
            "declaration_path": str(contract.optimization_confirmation.declaration),
            "declaration_raw_sha256": _file_sha256(
                contract.optimization_confirmation.declaration
            ),
            "contract_path": str(contract.source),
            "contract_raw_sha256": contract.source_sha256,
            "contract_canonical_sha256": contract.canonical_sha256,
            "contract_sha256": contract.contract_sha256,
            "base_contract_path": str(contract.base_contract_binding.path),
            "base_contract_raw_sha256": contract.base_contract_binding.sha256,
            "base_contract_canonical_sha256": contract.base_contract_binding.canonical_sha256,
            "base_contract_sha256": contract.base_contract_binding.contract_sha256,
            "confirmation_helper_path": str(
                contract.source_pins["confirmation_helper"].path
            ),
            "confirmation_helper_sha256": contract.source_pins[
                "confirmation_helper"
            ].sha256,
        }
        mismatches = [name for name, expected in exact.items() if mapping[name] != expected]
        if mismatches:
            raise PipelineStateError(
                "authorization audit differs from configured authority: " + ", ".join(mismatches)
            )
    return mapping


def authorization_record(contract: V4Contract, audit: Any) -> dict[str, Any]:
    """Build the one exact authorization record a v4 optimizer may commit."""

    mapping = _authorization_mapping(audit, contract)
    unsigned = {
        "schema_version": "ipmsm-v2-optimization-authorization-binding-v1",
        "audit": mapping,
        "sources": {
            "authorization_helper": {
                "path": str(contract.source_pins["optimization_authorizer_v4"].path),
                "sha256": contract.source_pins["optimization_authorizer_v4"].sha256,
            },
            "optimization_wrapper": {
                "path": str(contract.source_pins["optimization_runner_v4"].path),
                "sha256": contract.source_pins["optimization_runner_v4"].sha256,
            },
        },
    }
    return {**unsigned, "binding_sha256": v3._canonical_sha256(unsigned)}


def audit_authorization(contract: V4Contract) -> AuditedAuthorization:
    """Strictly re-audit the receipt against the exact v4 envelope."""

    helper = _loaded_module(
        "authorize_ipmsm_v2_optimization_v4",
        contract.source_pins["optimization_authorizer_v4"],
    )
    try:
        audit = helper.audit_authorization_receipt(
            contract.optimization_confirmation.receipt,
            contract.source,
            contract.optimization_confirmation.confirmation,
        )
    except Exception as exc:
        raise PipelineStateError(f"optimization authorization receipt audit failed: {exc}") from exc
    mapping = _authorization_mapping(audit, contract)
    record = authorization_record(contract, audit)
    audit_contract(contract)
    return AuditedAuthorization(audit=audit, mapping=mapping, record=record)


def audit_optimization_decision_authorization(
    decision: Mapping[str, Any], authorization: AuditedAuthorization
) -> None:
    execution = decision.get("execution_contract")
    if not isinstance(execution, Mapping) or execution.get("authorization") != authorization.record:
        raise PipelineStateError(
            "optimization decision does not contain the exact v4 authorization record"
        )


def _expanded_wrapper_argv(contract: V4Contract, upstream: Path) -> tuple[str, ...]:
    return tuple(
        str(upstream) if item == UPSTREAM_PLACEHOLDER else item
        for item in contract.optimization.wrapper_argv_template
    )


def _inspect_downstream(
    contract: V4Contract, official: AuditedOfficialStage1
) -> PipelineSnapshot:
    base = contract.base_contract
    gate_detail = {
        "decision": official.gate.decision,
        "min_primary_test_r2": min(official.gate.primary_test_r2.values()),
        "stage1_completion": str(contract.stage1_official.completion),
        "stage1_completion_sha256": getattr(official.bundle, "completion_sha256", ""),
    }
    if not base.stage2.decision.is_file():
        return PipelineSnapshot("run_stage2_fresh", "stage2", detail=gate_detail)
    stage2_decision = v3.audit_decision(
        base.stage2.decision,
        schema_version=v3.STAGE2_DECISION_SCHEMA_VERSION,
        allowed_statuses={"stage2_started", "complete", "combined_r2_failed"},
        workdir=base.workdir,
    )
    audit_stage2_official_binding(stage2_decision, official)
    stage2_status = str(stage2_decision["status"])
    if stage2_status == "stage2_started":
        return PipelineSnapshot("run_stage2_resume", "stage2", detail=gate_detail)
    upstream: Path
    branch: str
    if stage2_status == "complete":
        upstream = base.stage2.decision
        branch = "stage2_complete"
    else:
        prior_exists = v3._audit_pair_presence(
            base.stage3.prior_plan, base.stage3.prior_manifest, "Stage12 merge pair"
        )
        if not prior_exists:
            return PipelineSnapshot("merge_stage12_plan", "stage3")
        v3._audit_merge_pair(base.stage3, base.workdir)
        stage3_pair_exists = v3._audit_pair_presence(
            base.stage3.plan, base.stage3.manifest, "Stage3 plan pair"
        )
        if not stage3_pair_exists:
            return PipelineSnapshot("generate_stage3_plan", "stage3")
        v3._audit_stage3_pair(base.stage3, base.workdir)
        if not base.stage3.decision.is_file():
            return PipelineSnapshot("run_stage3_fresh", "stage3")
        stage3_decision = v3.audit_decision(
            base.stage3.decision,
            schema_version=v3.STAGE2_DECISION_SCHEMA_VERSION,
            allowed_statuses={"stage2_started", "complete", "combined_r2_failed"},
            workdir=base.workdir,
        )
        stage3_status = str(stage3_decision["status"])
        if stage3_status == "stage2_started":
            return PipelineSnapshot("run_stage3_resume", "stage3")
        if stage3_status == "combined_r2_failed":
            return PipelineSnapshot(
                "blocked_stage3_r2_failed", "stage3_r2_failed", terminal=True, exit_code=1
            )
        upstream = base.stage3.decision
        branch = "stage3_complete"

    auth = contract.optimization_confirmation
    confirmation_exists = auth.confirmation.is_file()
    receipt_exists = auth.receipt.is_file()
    if receipt_exists and not confirmation_exists:
        raise PipelineStateError("optimization receipt exists without its confirmation")
    if not confirmation_exists:
        return PipelineSnapshot(
            "wait_optimization_confirmation",
            branch,
            upstream_decision=upstream,
            terminal=True,
            detail={"confirmation": str(auth.confirmation), "writes_performed": 0},
        )
    if not auth.declaration.is_file():
        raise PipelineStateError("optimization confirmation exists without configured declaration")
    if not receipt_exists:
        # The authorizer repeats this audit before publication.  This first
        # check prevents an invalid human artifact from becoming an action.
        confirmation_helper = _loaded_module(
            "confirm_ipmsm_v2_optimization_inputs",
            contract.source_pins["confirmation_helper"],
        )
        try:
            confirmation_helper.audit_confirmation(auth.confirmation, contract.source)
        except Exception as exc:
            raise PipelineStateError(f"optimization confirmation audit failed: {exc}") from exc
        return PipelineSnapshot(
            "commit_optimization_authorization", branch, upstream_decision=upstream
        )

    authorization = audit_authorization(contract)
    if not base.optimization.decision.is_file():
        return PipelineSnapshot(
            "run_optimization_fresh",
            branch,
            upstream_decision=upstream,
            detail={"authorization": authorization.record},
        )
    optimization_decision = v3.audit_decision(
        base.optimization.decision,
        schema_version=v3.OPTIMIZATION_DECISION_SCHEMA_VERSION,
        allowed_statuses={"optimization_started", "pareto_fea_started", "complete", "failed"},
        workdir=base.workdir,
    )
    audit_optimization_decision_authorization(optimization_decision, authorization)
    optimization_status = str(optimization_decision["status"])
    if optimization_status in {"optimization_started", "pareto_fea_started"}:
        return PipelineSnapshot(
            "run_optimization_resume", branch, upstream_decision=upstream
        )
    if optimization_status != "complete":
        raise PipelineStateError(f"optimization is not resumable: status={optimization_status}")

    speed = base.speed
    if speed.marker.is_file():
        v3._audit_speed_marker(base)
        return PipelineSnapshot("complete", branch, upstream_decision=upstream, terminal=True)
    plan_exists = speed.plan.is_file()
    output_exists = speed.output_dir.is_dir()
    result_exists = speed.result.is_file()
    rank_exists = speed.rank.is_file()
    top_exists = speed.top.is_file()
    if not any((plan_exists, output_exists, result_exists, rank_exists, top_exists)):
        return PipelineSnapshot("run_speed_plan", branch, upstream_decision=upstream)
    if not plan_exists:
        raise PipelineStateError("speed downstream artifact exists without its case plan")
    try:
        with speed.plan.open("r", encoding="utf-8-sig", newline="") as stream:
            plan_rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PipelineStateError(f"cannot audit speed plan: {exc}") from exc
    case_ids = [str(row.get("case_id") or "").strip() for row in plan_rows]
    if (
        len(plan_rows) != speed.expected_rows
        or "" in case_ids
        or len(case_ids) != len(set(case_ids))
    ):
        raise PipelineStateError("speed plan row/case-id contract failed")
    if not output_exists and not result_exists and not rank_exists and not top_exists:
        return PipelineSnapshot("run_speed_campaign", branch, upstream_decision=upstream)
    if output_exists != result_exists:
        raise PipelineStateError("unsupported partial speed campaign output")
    if not output_exists:
        raise PipelineStateError("speed rank output exists before campaign completion")
    v3._audit_csv_coverage(speed.plan, speed.result, speed.expected_rows, "speed campaign")
    if not rank_exists and not top_exists:
        return PipelineSnapshot("run_speed_rank", branch, upstream_decision=upstream)
    if rank_exists != top_exists:
        raise PipelineStateError("unsupported partial speed rank output")
    if speed.rank.stat().st_size <= 0:
        raise PipelineStateError("speed rank output is empty")
    v3._read_top_profiles(speed.top, speed.minimum_top_profiles)
    return PipelineSnapshot("commit_speed_completion", branch, upstream_decision=upstream)


def inspect_pipeline(contract: V4Contract) -> PipelineSnapshot:
    audit_contract(contract)
    active_external = v3.audit_external_pid_files(contract.base_contract)
    if active_external:
        return PipelineSnapshot(
            "wait_external_process",
            "external_live_chain",
            detail={"active_external_processes": active_external},
        )
    stage1 = contract.base_contract.stage1
    campaign_exists = stage1.output_dir.is_dir()
    result_exists = stage1.result.is_file()
    if not campaign_exists and not result_exists:
        return PipelineSnapshot("run_stage1_campaign", "stage1")
    if not campaign_exists or not result_exists:
        raise PipelineStateError("unsupported partial Stage1 campaign output")
    v3._audit_csv_coverage(stage1.case_plan, stage1.result, stage1.expected_rows, "Stage1")
    if _completion_requires_publication_inspection(contract):
        pending = inspect_pending_official_publications(contract)
        if not pending:
            raise PipelineStateError(
                "Stage1 completion has a publication-recovery signal without pending authority"
            )
        return PipelineSnapshot(
            "publish_stage1_official",
            "stage1_official",
            detail={"pending_publications": list(pending)},
        )
    if not contract.stage1_official.completion.is_file():
        # Workspace attempts and every legacy validation/model/R2 combination
        # are deliberately ignored until a sole completion commits authority.
        return PipelineSnapshot("publish_stage1_official", "stage1_official")
    official = audit_official_stage1(contract)
    return _inspect_downstream(contract, official)


def execute_action(contract: V4Contract, snapshot: PipelineSnapshot) -> None:
    """Execute one inspected transition while preserving the shared lock."""

    audit_contract(contract)
    base = contract.base_contract
    action = snapshot.next_action
    if action == "run_stage1_campaign":
        v3.run_child(base.stage1.campaign_argv, workdir=base.workdir, label="Stage1 campaign")
    elif action == "publish_stage1_official":
        v3.run_child(
            (*contract.stage1_official.publisher_argv, "--execute"),
            workdir=contract.workdir,
            label="Stage1 official publication",
        )
    elif action in {"run_stage2_fresh", "run_stage2_resume"}:
        official = audit_official_stage1(contract)
        v3._run_dry_then_execute(
            official_stage2_argv(contract, official),
            workdir=base.workdir,
            label="Stage2 continuation",
            execute_suffix=["--execute"],
            resume=action.endswith("resume"),
            allowed_execute_returncodes={0, 1},
        )
    elif action == "merge_stage12_plan":
        v3._run_dry_then_execute(
            base.stage3.merge_argv,
            workdir=base.workdir,
            label="Stage12 merge",
            execute_suffix=["--execute"],
        )
    elif action == "generate_stage3_plan":
        v3._run_dry_then_execute(
            base.stage3.generate_argv,
            workdir=base.workdir,
            label="Stage3 generation",
            execute_suffix=["--write-stage3"],
            require_execute_matches_dry=True,
        )
    elif action in {"run_stage3_fresh", "run_stage3_resume"}:
        v3._run_dry_then_execute(
            base.stage3.continuation_argv,
            workdir=base.workdir,
            label="Stage3 continuation",
            execute_suffix=["--execute"],
            resume=action.endswith("resume"),
            allowed_execute_returncodes={0, 1},
        )
    elif action == "commit_optimization_authorization":
        v3.run_child(
            (*contract.optimization_confirmation.authorizer_argv, "--execute"),
            workdir=contract.workdir,
            label="optimization authorization",
        )
        audit_authorization(contract)
    elif action in {"run_optimization_fresh", "run_optimization_resume"}:
        if snapshot.upstream_decision is None:
            raise PipelineStateError("optimization action lacks an upstream decision")
        authorization = audit_authorization(contract)
        v3._run_dry_then_execute(
            _expanded_wrapper_argv(contract, snapshot.upstream_decision),
            workdir=contract.workdir,
            label="authorized optimization continuation",
            execute_suffix=["--execute"],
            resume=action.endswith("resume"),
            expected_dry_statuses=(
                {"optimization_started", "pareto_fea_started"}
                if action.endswith("resume")
                else {"planned"}
            ),
        )
        decision = v3.audit_decision(
            base.optimization.decision,
            schema_version=v3.OPTIMIZATION_DECISION_SCHEMA_VERSION,
            allowed_statuses={"optimization_started", "pareto_fea_started", "complete", "failed"},
            workdir=base.workdir,
        )
        audit_optimization_decision_authorization(decision, authorization)
    elif action == "run_speed_plan":
        v3.run_child(base.speed.plan_argv, workdir=base.workdir, label="speed plan")
    elif action == "run_speed_campaign":
        v3.run_child(base.speed.campaign_argv, workdir=base.workdir, label="speed campaign")
    elif action == "run_speed_rank":
        v3.run_child(base.speed.rank_argv, workdir=base.workdir, label="speed rank")
    elif action == "commit_speed_completion":
        v3._write_speed_marker(base)
    else:
        raise PipelineStateError(f"v4 action is not executable: {action}")
    audit_contract(contract)


def _json_artifact(path: Path) -> tuple[str, str, str]:
    payload, document = _strict_document(path, "contract input")
    raw = hashlib.sha256(payload).hexdigest()
    canonical = v3._canonical_sha256(document)
    logical = document.get("contract_sha256")
    if not isinstance(logical, str) or SHA256_PATTERN.fullmatch(logical) is None:
        raise PipelineContractError(f"contract input lacks a valid contract_sha256: {path}")
    return raw, canonical, logical


def build_contract_document(
    *,
    base_contract_path: str | Path,
    output_path: str | Path,
    stage1_workspace: str | Path,
    declaration: str | Path,
    confirmation: str | Path,
    receipt: str | Path,
    optimization_runner: str | Path | None = None,
) -> dict[str, Any]:
    """Build a deterministic v4 envelope without writing it.

    Every source hash is sampled from a strict regular single-link file.  The
    returned document can be published only by :func:`publish_contract`.
    """

    _audit_legacy_optimization_source_manifest()
    base_input = _absolute(Path(base_contract_path))
    input_identity = _json_artifact(base_input)
    base = v3.load_contract(base_input)
    base_path = _canonical_no_links(base.source, "builder base contract")
    base_raw, base_canonical, base_logical = _json_artifact(base_path)
    if input_identity != (base_raw, base_canonical, base_logical):
        raise PipelineContractError("builder base path aliases resolved to different bytes")
    if base.contract_sha256 != base_logical:
        raise PipelineContractError("builder loaded a different base contract")
    output = _canonical_no_links(Path(output_path), "builder v4 output")
    v3.audit_immutable_inputs(base)
    workdir = _canonical_no_links(base.workdir, "builder workdir")
    workspace = _canonical_no_links(Path(stage1_workspace), "builder Stage1 workspace")
    declaration_path = _canonical_no_links(Path(declaration), "builder declaration")
    confirmation_path = _canonical_no_links(Path(confirmation), "builder confirmation")
    receipt_path = _canonical_no_links(Path(receipt), "builder receipt")
    runner = _canonical_no_links(
        Path(optimization_runner)
        if optimization_runner is not None
        else workdir / SOURCE_PIN_FILENAMES["optimization_runner_v4"],
        "builder optimization runner",
    )
    source_paths = {
        "supervisor_v3": _canonical_no_links(Path(v3.__file__), "builder v3 supervisor"),
        "supervisor_v4": _canonical_no_links(Path(__file__), "builder v4 supervisor"),
        "stage1_publisher_v4": workdir / SOURCE_PIN_FILENAMES["stage1_publisher_v4"],
        "verification_helper": workdir / SOURCE_PIN_FILENAMES["verification_helper"],
        "confirmation_helper": workdir / SOURCE_PIN_FILENAMES["confirmation_helper"],
        "optimization_authorizer_v4": workdir
        / SOURCE_PIN_FILENAMES["optimization_authorizer_v4"],
        "optimization_runner_v4": runner,
        **{
            LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[module_name]: workdir / filename
            for module_name, filename in LEGACY_OPTIMIZATION_SOURCE_MODULES
        },
    }
    source_pins: dict[str, dict[str, str]] = {}
    for name, source in source_paths.items():
        source = _canonical_no_links(source, f"builder source {name}")
        if source.name.lower() != Path(SOURCE_PIN_FILENAMES[name]).name.lower():
            raise PipelineContractError(
                f"builder source {name} must name {SOURCE_PIN_FILENAMES[name]}"
            )
        source_pins[name] = {"path": str(source), "sha256": _file_sha256(source)}
    for item in base.immutable_inputs:
        if _file_sha256(_absolute(item.path)) != item.sha256:
            raise PipelineContractError(f"builder base immutable input changed: {item.path}")
    base_outputs = {
        _absolute(base.stage1.output_dir),
        _absolute(base.stage1.validation),
        _absolute(base.stage1.model_dir),
        _absolute(base.stage1.r2),
        _absolute(base.stage2.decision),
        _absolute(base.stage3.prior_plan),
        _absolute(base.stage3.prior_manifest),
        _absolute(base.stage3.plan),
        _absolute(base.stage3.manifest),
        _absolute(base.stage3.decision),
        _absolute(base.optimization.decision),
        _absolute(base.speed.plan),
        _absolute(base.speed.output_dir),
        _absolute(base.speed.rank),
        _absolute(base.speed.top),
        _absolute(base.speed.marker),
    }
    protected_inputs = (
        {base_path, output, *(_absolute(path) for path in source_paths.values())}
        | {_absolute(item.path) for item in base.immutable_inputs}
        | {_absolute(base.lock_path)}
        | base_outputs
    )
    for path, label in (
        (output, "v4 output contract"),
        (workspace, "Stage1 official workspace"),
        (declaration_path, "optimization declaration"),
        (confirmation_path, "optimization confirmation"),
        (receipt_path, "optimization receipt"),
    ):
        _reject_link_components(path, label)
    for reserved in protected_inputs:
        if workspace == reserved or workspace in reserved.parents or reserved in workspace.parents:
            raise PipelineContractError("builder Stage1 workspace aliases a reserved path")
    authority_paths = (declaration_path, confirmation_path, receipt_path)
    for index, path in enumerate(authority_paths):
        for other in authority_paths[index + 1 :]:
            if path == other or path in other.parents or other in path.parents:
                raise PipelineContractError("builder authorization paths alias by containment")
        for reserved in protected_inputs | {workspace, _absolute(workspace / "completion.json")}:
            if path == reserved or path in reserved.parents or reserved in path.parents:
                raise PipelineContractError("builder authorization path aliases a reserved path")
    base_binding = {
        "path": str(base_path),
        "raw_sha256": base_raw,
        "canonical_sha256": base_canonical,
        "contract_sha256": base_logical,
    }
    immutable = [
        {"path": str(base_path), "sha256": base_raw},
        *(source_pins[name] for name in sorted(source_pins)),
    ]
    legacy_template = list(base.optimization.argv_template)
    if (
        len(legacy_template) < 2
        or Path(legacy_template[1]).name.lower()
        != "continue_ipmsm_v2_optimization.py"
    ):
        raise PipelineContractError(
            "builder base optimization argv must invoke its legacy script at index 1"
        )
    _validate_legacy_optimizer_arguments(legacy_template[2:])
    python = legacy_template[0]
    legacy = legacy_template[2:]
    wrapper_argv = [
        python,
        str(runner),
        "--pipeline-contract",
        str(output),
        "--authorization-receipt",
        str(receipt_path),
        "--confirmation",
        str(confirmation_path),
        *legacy,
    ]
    pipeline = {
        "workdir": str(workdir),
        "shared_lock": str(_absolute(base.lock_path)),
        "base_contract": base_binding,
        "immutable_inputs": immutable,
        "source_pins": source_pins,
        "stage1_official": {
            "workspace": str(workspace),
            "completion": str(_absolute(workspace / "completion.json")),
            "publisher_argv": [
                python,
                str(source_paths["stage1_publisher_v4"]),
                "--pipeline-contract",
                str(output),
                "--base-contract",
                str(base_path),
                "--workspace",
                str(workspace),
            ],
        },
        "optimization_confirmation": {
            "declaration": str(declaration_path),
            "confirmation": str(confirmation_path),
            "receipt": str(receipt_path),
            "authorizer_argv": [
                python,
                str(source_paths["optimization_authorizer_v4"]),
                "--contract",
                str(output),
                "--confirmation",
                str(confirmation_path),
                "--output",
                str(receipt_path),
            ],
        },
        "optimization": {"wrapper_argv_template": wrapper_argv},
    }
    unsigned = {"schema_version": CONTRACT_SCHEMA_VERSION, "pipeline": pipeline}
    return {**unsigned, "contract_sha256": v3._canonical_sha256(unsigned)}


@dataclasses.dataclass(frozen=True)
class _ContractFileIdentity:
    device: int
    inode: int
    size: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "_ContractFileIdentity":
        identity = cls(int(value.st_dev), int(value.st_ino), int(value.st_size))
        if identity.device < 0 or identity.inode <= 0 or identity.size < 0:
            raise PipelineStateError("v4 contract publication has an unusable file identity")
        return identity

    @classmethod
    def from_mapping(cls, value: Any) -> "_ContractFileIdentity":
        mapping = v3._mapping(value, "v4 contract publication proof identity")
        v3._expect_keys(mapping, {"device", "inode", "size"}, "proof identity")
        if any(type(mapping[name]) is not int for name in ("device", "inode", "size")):
            raise PipelineStateError("v4 contract proof identity fields must be integers")
        identity = cls(mapping["device"], mapping["inode"], mapping["size"])
        if identity.device < 0 or identity.inode <= 0 or identity.size < 0:
            raise PipelineStateError("v4 contract proof identity is unusable")
        return identity

    def as_mapping(self) -> dict[str, int]:
        return {"device": self.device, "inode": self.inode, "size": self.size}


@dataclasses.dataclass(frozen=True)
class _ContractPublicationAttempt:
    path: Path
    identity: tuple[int, int, int]
    stage_ready: bool
    stage_ready_identity: tuple[int, int, int] | None


@dataclasses.dataclass(frozen=True)
class _ContractPublicationProof:
    path: Path
    source: Path
    destination: Path
    identity: _ContractFileIdentity
    payload: bytes
    path_identity: _ContractFileIdentity


@dataclasses.dataclass(frozen=True)
class _ContractPublicationState:
    status: str
    destination: Path
    expected_payload: bytes
    pending_state: str = ""
    attempt: _ContractPublicationAttempt | None = None
    staged_path: Path | None = None
    staged_identity: _ContractFileIdentity | None = None
    proof: _ContractPublicationProof | None = None
    incomplete_proof_identity: _ContractFileIdentity | None = None
    contract: V4Contract | None = None


@dataclasses.dataclass(frozen=True)
class ContractPublicationResult:
    contract: V4Contract
    outcome: str
    mutated: bool
    recovery_state: str
    mutation_count: int


def _contract_document_bytes(document: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(document),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PipelineContractError("v4 contract document is not canonical JSON data") from exc


def contract_attempt_path(output: str | Path, payload: bytes) -> Path:
    destination = _absolute(Path(output))
    digest = hashlib.sha256(payload).hexdigest()
    return destination.with_name(f".{destination.name}{CONTRACT_ATTEMPT_MARKER}{digest}")


def contract_staged_path(output: str | Path, payload: bytes) -> Path:
    destination = _absolute(Path(output))
    digest = hashlib.sha256(payload).hexdigest()
    return destination.with_name(
        f".{destination.name}.{digest[:32]}{CONTRACT_STAGED_SUFFIX}"
    )


def contract_proof_path(output: str | Path) -> Path:
    destination = _absolute(Path(output))
    return destination.with_name(f".{destination.name}{CONTRACT_PROOF_SUFFIX}")


def _fsync_contract_directory(path: Path) -> None:
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _contract_directory_identity(path: Path, label: str) -> tuple[int, int, int]:
    _reject_link_components(path, label)
    try:
        before = os.lstat(path)
        entries = tuple(path.iterdir())
        after = os.lstat(path)
    except OSError as exc:
        raise PipelineStateError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISDIR(before.st_mode) or _is_reparse(before):
        raise PipelineStateError(f"{label} is not a regular no-follow directory")
    first = int(before.st_dev), int(before.st_ino), int(before.st_mtime_ns)
    second = int(after.st_dev), int(after.st_ino), int(after.st_mtime_ns)
    if first != second or entries:
        raise PipelineStateError(f"{label} changed or is not empty")
    return first


def _inspect_contract_attempt(path: Path) -> _ContractPublicationAttempt:
    _reject_link_components(path, "v4 contract attempt journal")
    try:
        before = os.lstat(path)
        entries = tuple(path.iterdir())
    except OSError as exc:
        raise PipelineStateError(f"cannot inspect v4 contract attempt journal: {path}") from exc
    if not stat.S_ISDIR(before.st_mode) or _is_reparse(before):
        raise PipelineStateError("v4 contract attempt journal is not a no-follow directory")
    ready = path / CONTRACT_STAGE_READY_NAME
    if not entries:
        ready_identity = None
    elif len(entries) == 1 and entries[0] == ready:
        ready_identity = _contract_directory_identity(
            ready, "v4 contract stage-ready marker"
        )
    else:
        raise PipelineStateError("v4 contract attempt journal has an unauthorized entry")
    after = os.lstat(path)
    identity = int(after.st_dev), int(after.st_ino), int(after.st_mtime_ns)
    before_identity = int(before.st_dev), int(before.st_ino), int(before.st_mtime_ns)
    if identity != before_identity:
        raise PipelineStateError("v4 contract attempt journal changed during inspection")
    return _ContractPublicationAttempt(path, identity, ready_identity is not None, ready_identity)


def _contract_attempt_candidates(destination: Path) -> tuple[Path, ...]:
    if not destination.parent.exists():
        return ()
    _reject_link_components(destination.parent, "v4 contract output parent")
    prefix = f".{destination.name}{CONTRACT_ATTEMPT_MARKER}"
    return tuple(
        sorted(path for path in destination.parent.iterdir() if path.name.startswith(prefix))
    )


def _contract_stage_name_allowed(path: Path, destination: Path) -> bool:
    prefix = f".{destination.name}."
    if path.parent != destination.parent or not path.name.startswith(prefix):
        return False
    if not path.name.endswith(CONTRACT_STAGED_SUFFIX):
        return False
    token = path.name[len(prefix) : -len(CONTRACT_STAGED_SUFFIX)]
    return len(token) == 32 and all(character in "0123456789abcdef" for character in token)


def _contract_staged_candidates(destination: Path) -> tuple[Path, ...]:
    if not destination.parent.exists():
        return ()
    _reject_link_components(destination.parent, "v4 contract output parent")
    return tuple(
        sorted(
            path
            for path in destination.parent.iterdir()
            if _contract_stage_name_allowed(path, destination)
        )
    )


def _contract_file_identity_at(path: Path, label: str) -> _ContractFileIdentity | None:
    _reject_link_components(path, label)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PipelineStateError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise PipelineStateError(f"{label} is not a regular no-follow file: {path}")
    return _ContractFileIdentity.from_stat(info)


def _contract_recovery_stat(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(getattr(value, "st_nlink", 1)),
    )


def _read_contract_owned_payload(
    path: Path,
    identity: _ContractFileIdentity,
    *,
    expected_links: int,
    label: str,
) -> bytes:
    if expected_links not in {1, 2}:
        raise PipelineStateError("v4 contract recovery link expectation is invalid")
    _reject_link_components(path, label)
    pathname_before = os.lstat(path)
    if int(getattr(pathname_before, "st_nlink", 1)) != expected_links:
        raise PipelineStateError(f"{label} has ambiguous hardlink ownership")
    if _contract_file_identity_at(path, label) != identity:
        raise PipelineStateError(f"{label} pathname identity changed")
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)) | int(
        getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PipelineStateError(f"cannot open {label}: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if _ContractFileIdentity.from_stat(opened) != identity:
            raise PipelineStateError(f"{label} identity changed while opening")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    pathname_after = os.lstat(path)
    if any(
        int(getattr(item, "st_nlink", 1)) != expected_links
        for item in (opened, after, pathname_after)
    ):
        raise PipelineStateError(f"{label} hardlink ownership changed while reading")
    if not (
        _contract_recovery_stat(pathname_before)
        == _contract_recovery_stat(opened)
        == _contract_recovery_stat(after)
        == _contract_recovery_stat(pathname_after)
    ):
        raise PipelineStateError(f"{label} changed while being read")
    payload = b"".join(chunks)
    if len(payload) != identity.size:
        raise PipelineStateError(f"{label} payload size changed")
    return payload


def _single_link_contract_payload(path: Path, label: str) -> tuple[_ContractFileIdentity, bytes]:
    identity = _contract_file_identity_at(path, label)
    if identity is None:
        raise PipelineStateError(f"{label} disappeared")
    return identity, _read_contract_owned_payload(
        path, identity, expected_links=1, label=label
    )


def _contract_proof_bytes(
    source: Path, destination: Path, identity: _ContractFileIdentity
) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": CONTRACT_PROOF_SCHEMA_VERSION,
                "source": str(source),
                "destination": str(destination),
                "identity": identity.as_mapping(),
            },
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _same_contract_path(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError, ValueError):
        return _canonical_no_links(left, "v4 contract recovery path") == _canonical_no_links(
            right, "v4 contract recovery path"
        )


def _parse_contract_proof(path: Path, destination: Path) -> _ContractPublicationProof:
    path_identity, payload = _single_link_contract_payload(
        path, "v4 contract publication proof"
    )
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=v3._unique_object,
            parse_constant=v3._reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PipelineStateError("v4 contract publication proof is invalid JSON") from exc
    mapping = v3._mapping(raw, "v4 contract publication proof")
    v3._expect_keys(mapping, {"schema_version", "source", "destination", "identity"}, "proof")
    if mapping["schema_version"] != CONTRACT_PROOF_SCHEMA_VERSION:
        raise PipelineStateError("v4 contract publication proof schema is unsupported")
    if not isinstance(mapping["source"], str) or not isinstance(mapping["destination"], str):
        raise PipelineStateError("v4 contract publication proof paths are invalid")
    source = Path(mapping["source"])
    proof_destination = Path(mapping["destination"])
    if not source.is_absolute() or not proof_destination.is_absolute():
        raise PipelineStateError("v4 contract publication proof paths must be absolute")
    source = _absolute(source)
    proof_destination = _absolute(proof_destination)
    if not _same_contract_path(proof_destination, destination):
        raise PipelineStateError("v4 contract publication proof destination differs")
    identity = _ContractFileIdentity.from_mapping(mapping["identity"])
    if payload != _contract_proof_bytes(source, proof_destination, identity):
        raise PipelineStateError("v4 contract publication proof is not canonical")
    if not _same_contract_path(path, contract_proof_path(destination)):
        raise PipelineStateError("v4 contract publication proof pathname differs")
    return _ContractPublicationProof(
        path=path,
        source=source,
        destination=proof_destination,
        identity=identity,
        payload=payload,
        path_identity=path_identity,
    )


def _contract_pending_state(
    proof: _ContractPublicationProof, expected_payload: bytes
) -> str:
    source_identity = _contract_file_identity_at(proof.source, "v4 contract staging path")
    destination_identity = _contract_file_identity_at(
        proof.destination, "v4 contract destination"
    )
    if source_identity is not None and source_identity != proof.identity:
        raise PipelineStateError("v4 contract staging identity differs from its proof")
    if destination_identity is not None and destination_identity != proof.identity:
        raise PipelineStateError("v4 contract destination is not owned by its proof")
    live = tuple(
        path
        for path, identity in (
            (proof.source, source_identity),
            (proof.destination, destination_identity),
        )
        if identity is not None
    )
    if not live:
        raise PipelineStateError("v4 contract proof owns neither staging nor destination")
    for path in live:
        if _read_contract_owned_payload(
            path,
            proof.identity,
            expected_links=len(live),
            label="proof-owned v4 contract",
        ) != expected_payload:
            raise PipelineStateError("proof-owned v4 contract bytes differ from authority")
    if source_identity is not None and destination_identity is None:
        return "pre_commit"
    if source_identity is not None:
        return "post_commit_stage_linked"
    return "post_commit_stage_unlinked"


def _inspect_contract_publication_state(
    output: str | Path, document: Mapping[str, Any]
) -> _ContractPublicationState:
    destination = _canonical_no_links(Path(output), "v4 contract output")
    expected_payload = _contract_document_bytes(document)
    expected_attempt = contract_attempt_path(destination, expected_payload)
    attempts = _contract_attempt_candidates(destination)
    if len(attempts) > 1 or (attempts and attempts[0] != expected_attempt):
        raise PipelineStateError("v4 contract attempt journal differs from current authority")
    attempt = _inspect_contract_attempt(attempts[0]) if attempts else None
    expected_staged = contract_staged_path(destination, expected_payload)
    staged_candidates = _contract_staged_candidates(destination)
    if len(staged_candidates) > 1 or (
        staged_candidates and staged_candidates[0] != expected_staged
    ):
        raise PipelineStateError("v4 contract staging path differs from current authority")
    staged = staged_candidates[0] if staged_candidates else None
    proof_path = contract_proof_path(destination)
    if os.path.lexists(proof_path):
        proof_identity, proof_payload = _single_link_contract_payload(
            proof_path, "v4 contract publication proof"
        )
        try:
            proof = _parse_contract_proof(proof_path, destination)
        except PipelineStateError:
            if attempt is None or not attempt.stage_ready or staged is None:
                raise PipelineStateError(
                    "incomplete v4 contract proof lacks sealed attempt authority"
                )
            staged_identity, staged_payload = _single_link_contract_payload(
                staged, "sealed v4 contract staging path"
            )
            if staged_payload != expected_payload:
                raise PipelineStateError("sealed v4 contract staging bytes changed")
            expected_proof = _contract_proof_bytes(
                expected_staged, destination, staged_identity
            )
            if len(proof_payload) >= len(expected_proof) or not expected_proof.startswith(
                proof_payload
            ):
                raise PipelineStateError(
                    "invalid v4 contract proof is not a durable-write prefix"
                )
            return _ContractPublicationState(
                status="pending",
                destination=destination,
                expected_payload=expected_payload,
                pending_state="pre_commit_proof_incomplete",
                attempt=attempt,
                staged_path=staged,
                staged_identity=staged_identity,
                incomplete_proof_identity=proof_identity,
            )
        if proof.source != expected_staged:
            raise PipelineStateError("v4 contract proof source is not deterministic")
        pending = _contract_pending_state(proof, expected_payload)
        if pending != "post_commit_stage_unlinked" and (
            attempt is None or not attempt.stage_ready
        ):
            raise PipelineStateError("proof-owned v4 contract lacks sealed attempt authority")
        if attempt is not None and not attempt.stage_ready and pending != "post_commit_stage_unlinked":
            raise PipelineStateError("v4 contract attempt lost its stage-ready authority")
        return _ContractPublicationState(
            status="pending",
            destination=destination,
            expected_payload=expected_payload,
            pending_state=pending,
            attempt=attempt,
            staged_path=staged,
            proof=proof,
        )
    if os.path.lexists(destination):
        if staged is not None or (attempt is not None and attempt.stage_ready):
            raise PipelineStateError(
                "proofless v4 contract destination has unfinished transaction artifacts"
            )
        _, payload = _single_link_contract_payload(destination, "committed v4 contract")
        if payload != expected_payload:
            raise FileExistsError(f"refusing to replace v4 pipeline contract: {destination}")
        contract = load_contract(destination)
        if attempt is not None:
            return _ContractPublicationState(
                status="committed_late_attempt",
                destination=destination,
                expected_payload=expected_payload,
                pending_state="cleanup_late_empty_attempt",
                attempt=attempt,
                contract=contract,
            )
        return _ContractPublicationState(
            status="committed",
            destination=destination,
            expected_payload=expected_payload,
            contract=contract,
        )
    if attempt is not None:
        if attempt.stage_ready:
            if staged is None:
                raise PipelineStateError("sealed v4 contract staging path is missing")
            staged_identity, staged_payload = _single_link_contract_payload(
                staged, "sealed v4 contract staging path"
            )
            if staged_payload != expected_payload:
                raise PipelineStateError("sealed v4 contract staging bytes changed")
            pending = "pre_commit_no_proof"
        elif staged is None:
            staged_identity = None
            pending = "pre_stage"
        else:
            staged_identity, _ = _single_link_contract_payload(
                staged, "unsealed v4 contract staging path"
            )
            pending = "pre_stage_incomplete"
        return _ContractPublicationState(
            status="pending",
            destination=destination,
            expected_payload=expected_payload,
            pending_state=pending,
            attempt=attempt,
            staged_path=staged,
            staged_identity=staged_identity,
        )
    if staged is not None:
        raise PipelineStateError("unproven v4 contract staging orphan exists")
    return _ContractPublicationState("absent", destination, expected_payload)


def _create_contract_attempt(path: Path) -> _ContractPublicationAttempt:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        raise
    except OSError as exc:
        raise PipelineStateError("cannot create v4 contract attempt journal") from exc
    _fsync_contract_directory(path.parent)
    return _inspect_contract_attempt(path)


def _write_contract_stage_payload(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(
        getattr(os, "O_NOFOLLOW", 0)
    ) | int(getattr(os, "O_BINARY", 0))
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _stage_contract_publication(
    destination: Path, payload: bytes, attempt: _ContractPublicationAttempt
) -> None:
    if _inspect_contract_attempt(attempt.path) != attempt or attempt.stage_ready:
        raise PipelineStateError("v4 contract attempt changed before staging")
    staged = contract_staged_path(destination, payload)
    if os.path.lexists(staged):
        raise FileExistsError(f"v4 contract staging path already exists: {staged}")
    _write_contract_stage_payload(staged, payload)
    _, staged_payload = _single_link_contract_payload(staged, "new v4 contract staging path")
    if staged_payload != payload or _inspect_contract_attempt(attempt.path) != attempt:
        raise PipelineStateError("v4 contract staging authority changed before sealing")
    ready = attempt.path / CONTRACT_STAGE_READY_NAME
    try:
        os.mkdir(ready, 0o700)
    except OSError as exc:
        raise PipelineStateError("cannot create v4 contract stage-ready marker") from exc
    _fsync_contract_directory(attempt.path)
    sealed = _inspect_contract_attempt(attempt.path)
    if not sealed.stage_ready:
        raise PipelineStateError("v4 contract stage-ready marker was not durable")


def _write_contract_proof(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(
        getattr(os, "O_NOFOLLOW", 0)
    ) | int(getattr(os, "O_BINARY", 0))
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_contract_directory(path.parent)


def _link_contract_destination(proof: _ContractPublicationProof) -> None:
    if _contract_file_identity_at(proof.source, "v4 contract staging path") != proof.identity:
        raise PipelineStateError("v4 contract staging inode is no longer proof-owned")
    if int(getattr(os.lstat(proof.source), "st_nlink", 1)) != 1:
        raise PipelineStateError("v4 contract staging hardlink ownership is ambiguous")
    if os.path.lexists(proof.destination):
        raise FileExistsError(
            f"v4 contract destination appeared before commit: {proof.destination}"
        )
    try:
        os.link(proof.source, proof.destination)
    except FileExistsError:
        raise
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 50:
            raise PipelineStateError("cannot commit v4 contract without replacement") from exc
        try:
            os.rename(proof.source, proof.destination)
        except OSError as rename_exc:
            raise PipelineStateError("cannot commit v4 contract by no-replace rename") from rename_exc
    if _contract_file_identity_at(proof.destination, "v4 contract destination") != proof.identity:
        raise PipelineStateError("committed v4 contract identity differs from its proof")
    _fsync_contract_directory(proof.destination.parent)


def _unlink_contract_stage(proof: _ContractPublicationProof) -> None:
    proof.source.unlink()
    _fsync_contract_directory(proof.destination.parent)


def _remove_contract_attempt(attempt: _ContractPublicationAttempt) -> None:
    current = _inspect_contract_attempt(attempt.path)
    if current != attempt:
        raise PipelineStateError("v4 contract attempt changed before cleanup")
    if current.stage_ready:
        (current.path / CONTRACT_STAGE_READY_NAME).rmdir()
        _fsync_contract_directory(current.path)
        current = _inspect_contract_attempt(current.path)
    current.path.rmdir()
    _fsync_contract_directory(current.path.parent)


def _unlink_contract_proof(proof: _ContractPublicationProof) -> None:
    proof.path.unlink()
    _fsync_contract_directory(proof.path.parent)


def _restore_contract_proof(proof: _ContractPublicationProof) -> None:
    if os.path.lexists(proof.path):
        return
    _write_contract_proof(proof.path, proof.payload)


def _recover_contract_publication(
    output: str | Path,
    document: Mapping[str, Any],
    initial: _ContractPublicationState,
) -> ContractPublicationResult:
    mutation_count = 0
    committed_here = False

    def result(contract: V4Contract) -> ContractPublicationResult:
        if initial.status == "absent":
            outcome = "created" if committed_here else "already_present"
        elif mutation_count:
            outcome = "recovered"
        else:
            outcome = "already_present"
        return ContractPublicationResult(
            contract=contract,
            outcome=outcome,
            mutated=mutation_count > 0,
            recovery_state=initial.pending_state or initial.status,
            mutation_count=mutation_count,
        )

    for _ in range(24):
        state = _inspect_contract_publication_state(output, document)
        if state.status == "committed":
            assert state.contract is not None
            return result(state.contract)
        if state.status == "committed_late_attempt":
            assert state.attempt is not None and state.contract is not None
            current = _inspect_contract_publication_state(output, document)
            if current.status == "committed":
                continue
            if (
                current.status != state.status
                or current.attempt != state.attempt
                or current.contract is None
                or current.contract.source_sha256 != state.contract.source_sha256
            ):
                raise PipelineStateError(
                    "late v4 contract attempt changed before convergence cleanup"
                )
            try:
                _remove_contract_attempt(current.attempt)
            except FileNotFoundError:
                pass
            else:
                mutation_count += 1
            continue
        if state.status == "absent":
            try:
                _create_contract_attempt(
                    contract_attempt_path(state.destination, state.expected_payload)
                )
            except FileExistsError:
                pass
            else:
                mutation_count += 1
            continue
        pending = state.pending_state
        if pending == "pre_stage":
            assert state.attempt is not None
            try:
                _stage_contract_publication(
                    state.destination, state.expected_payload, state.attempt
                )
            except FileExistsError:
                pass
            else:
                mutation_count += 1
            continue
        if pending == "pre_stage_incomplete":
            assert state.staged_path is not None
            current = _inspect_contract_publication_state(output, document)
            if current != state:
                raise PipelineStateError("partial v4 contract stage changed before cleanup")
            state.staged_path.unlink()
            _fsync_contract_directory(state.staged_path.parent)
            mutation_count += 1
            continue
        if pending == "pre_commit_proof_incomplete":
            current = _inspect_contract_publication_state(output, document)
            if current != state:
                raise PipelineStateError("partial v4 contract proof changed before cleanup")
            contract_proof_path(state.destination).unlink()
            _fsync_contract_directory(state.destination.parent)
            mutation_count += 1
            continue
        if pending == "pre_commit_no_proof":
            assert state.staged_path is not None and state.staged_identity is not None
            proof_payload = _contract_proof_bytes(
                state.staged_path, state.destination, state.staged_identity
            )
            try:
                _write_contract_proof(contract_proof_path(state.destination), proof_payload)
            except FileExistsError:
                pass
            else:
                mutation_count += 1
            continue
        if pending == "pre_commit":
            assert state.proof is not None
            try:
                _link_contract_destination(state.proof)
            except FileExistsError:
                pass
            else:
                mutation_count += 1
                committed_here = True
            continue
        if pending == "post_commit_stage_linked":
            assert state.proof is not None
            current = _inspect_contract_publication_state(output, document)
            if (
                current.proof == state.proof
                and current.pending_state == "post_commit_stage_unlinked"
            ):
                continue
            if current.proof != state.proof or current.pending_state != pending:
                raise PipelineStateError("v4 contract publication changed before stage cleanup")
            try:
                _unlink_contract_stage(state.proof)
            except FileNotFoundError:
                pass
            else:
                mutation_count += 1
            continue
        if pending == "post_commit_stage_unlinked":
            assert state.proof is not None
            contract = load_contract(state.destination)
            current = _inspect_contract_publication_state(output, document)
            if current.proof != state.proof or current.pending_state != pending:
                raise PipelineStateError("v4 contract publication changed before final cleanup")
            if current.attempt is not None:
                try:
                    _remove_contract_attempt(current.attempt)
                except FileNotFoundError:
                    pass
                else:
                    mutation_count += 1
                continue
            try:
                _unlink_contract_proof(state.proof)
            except FileNotFoundError:
                continue
            mutation_count += 1
            try:
                committed = _inspect_contract_publication_state(output, document)
                if committed.status != "committed" or committed.contract is None:
                    raise PipelineStateError("v4 contract did not become committed")
            except Exception:
                _restore_contract_proof(state.proof)
                raise
            return result(committed.contract)
        raise PipelineStateError(f"unsupported v4 contract recovery state: {pending}")
    raise PipelineStateError("v4 contract publication exceeded its recovery transition limit")


def publish_contract_with_outcome(
    output: str | Path, document: Mapping[str, Any]
) -> ContractPublicationResult:
    """Publish/recover one exact v4 contract with mutation-aware telemetry."""

    destination = _canonical_no_links(Path(output), "v4 contract output")
    initial = _inspect_contract_publication_state(destination, document)
    if initial.status == "committed":
        assert initial.contract is not None
        return ContractPublicationResult(
            contract=initial.contract,
            outcome="already_present",
            mutated=False,
            recovery_state="committed",
            mutation_count=0,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_link_components(destination.parent, "v4 contract output parent")
    if not destination.parent.is_dir():
        raise PipelineStateError("v4 contract output parent is not a directory")
    return _recover_contract_publication(destination, document, initial)


def publish_contract(output: str | Path, document: Mapping[str, Any]) -> V4Contract:
    """Compatibility wrapper returning the exact committed v4 contract."""

    return publish_contract_with_outcome(output, document).contract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--contract", type=Path)
    mode.add_argument("--build-base-contract", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-transitions", type=int, default=MAX_TRANSITIONS)
    parser.add_argument("--output-contract", type=Path)
    parser.add_argument("--stage1-workspace", type=Path)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--confirmation", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--optimization-runner", type=Path)
    parser.add_argument(
        "--write-contract",
        action="store_true",
        help="Atomically publish a newly built v4 contract; omit for read-only dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.build_base_contract is not None:
        if args.execute:
            parser.error("contract build mode uses --write-contract, not --execute")
        required = {
            "--output-contract": args.output_contract,
            "--stage1-workspace": args.stage1_workspace,
            "--declaration": args.declaration,
            "--confirmation": args.confirmation,
            "--receipt": args.receipt,
        }
        missing = [flag for flag, value in required.items() if value is None]
        if missing:
            parser.error("contract build mode requires " + ", ".join(missing))
        canonical_output = _canonical_no_links(args.output_contract, "builder v4 output")
        document = build_contract_document(
            base_contract_path=args.build_base_contract,
            output_path=args.output_contract,
            stage1_workspace=args.stage1_workspace,
            declaration=args.declaration,
            confirmation=args.confirmation,
            receipt=args.receipt,
            optimization_runner=args.optimization_runner,
        )
        if args.write_contract:
            publication = publish_contract_with_outcome(args.output_contract, document)
            output_hash = publication.contract.source_sha256
            status = publication.outcome
            publication_state = publication.recovery_state
            writes_performed = 1 if publication.outcome in {"created", "recovered"} else 0
            transaction_mutations = publication.mutation_count
        else:
            inspection = _inspect_contract_publication_state(
                args.output_contract, document
            )
            output_hash = hashlib.sha256(_contract_document_bytes(document)).hexdigest()
            if inspection.status == "absent":
                status = "validated"
                publication_state = "absent"
            elif inspection.status == "committed":
                status = "already_present"
                publication_state = "committed"
            else:
                status = "publication_recovery_pending"
                publication_state = inspection.pending_state or inspection.status
            writes_performed = 0
            transaction_mutations = 0
        print(
            json.dumps(
                {
                    "schema_version": CONTRACT_SCHEMA_VERSION,
                    "status": status,
                    "mode": "write" if args.write_contract else "dry-run",
                    "output": str(canonical_output),
                    "output_raw_sha256": output_hash,
                    "contract_sha256": document["contract_sha256"],
                    "publication_state": publication_state,
                    "transaction_mutations": transaction_mutations,
                    "writes_performed": writes_performed,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    if args.write_contract:
        parser.error("--write-contract is valid only in contract build mode")
    if not 1 <= args.max_transitions <= MAX_TRANSITIONS:
        raise PipelineContractError(f"--max-transitions must be between 1 and {MAX_TRANSITIONS}")
    contract = load_contract(args.contract)
    if not args.execute:
        snapshot = inspect_pipeline(contract)
        print(
            json.dumps(
                snapshot.report(contract, mode="dry-run"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return snapshot.exit_code

    transitions = 0
    preflight = inspect_pipeline(contract)
    if preflight.terminal or preflight.next_action == "wait_external_process":
        print(
            json.dumps(
                preflight.report(contract, mode="execute", transitions=0),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return preflight.exit_code
    with v3.ExecutionLock(contract.lock_path):
        while True:
            snapshot = inspect_pipeline(contract)
            if snapshot.terminal or snapshot.next_action == "wait_external_process":
                print(
                    json.dumps(
                        snapshot.report(contract, mode="execute", transitions=transitions),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                return snapshot.exit_code
            if transitions >= args.max_transitions:
                raise PipelineStateError(
                    f"transition limit reached before a terminal state: {snapshot.next_action}"
                )
            execute_action(contract, snapshot)
            transitions += 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PipelineContractError, PipelineStateError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
