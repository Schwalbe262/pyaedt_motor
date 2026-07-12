"""Immutable contract primitives for per-beta target-load matched IPMSM FEA.

This module does not submit scheduler work and does not mutate the live v3
optimization artifacts.  It freezes a revisioned root identity, derives one
independent probe per candidate / operating point / beta role, and produces at
most one deterministic sequential-current attempt for each probe.
"""

from __future__ import annotations

import base64
import binascii
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from calibrate_ipmsm_beta import validated_zero_manifest
from ipmsm_optimization import (
    OptimizationSpec,
    OptimizationSpecError,
    active_volume_m3,
    optimization_spec_from_mapping,
)
from ipmsm_target_load_matching import (
    LoadObservation,
    TargetLoadDecision,
    TargetLoadPolicy,
    plan_target_load_match,
)
import ipmsm_target_load_matching as target_load_matching
import ipmsm_surrogate_bundle as surrogate_bundle
import optimize_ipmsm_nsga2 as nsga2
from optimize_ipmsm_nsga2 import (
    BETA_VALIDATION_ROLE_CENTER,
    BETA_VALIDATION_ROLES,
    beta_validation_case_id,
    local_beta_validation_points,
)
import validate_ipmsm_pareto_fea as pareto_validator


ROOT_SCHEMA_VERSION = "ipmsm-target-load-match-root-v2"
UPSTREAM_PARETO_BINDING_SCHEMA_VERSION = "ipmsm-target-load-upstream-pareto-v1"
OPTIMIZATION_PRODUCER_SOURCE_FILES = (
    "continue_ipmsm_v2_optimization.py",
    "continue_ipmsm_v2_stage2.py",
    "calibrate_ipmsm_beta.py",
    "ipmsm_optimization.py",
    "ipmsm_surrogate_bundle.py",
    "optimize_ipmsm_nsga2.py",
    "run_ipmsm_v2_campaign.py",
    "submit_ipmsm_v2_campaign.py",
    "validate_ipmsm_pareto_fea.py",
)
ATTEMPT_SCHEMA_VERSION = "ipmsm-target-load-match-attempt-v1"
OBSERVATION_SCHEMA_VERSION = "ipmsm-target-load-observation-v1"
FIXED_MTPA_EVIDENCE_SCHEMA_VERSION = "ipmsm-fixed-current-mtpa-evidence-v1"
CANDIDATE_SUMMARY_SCHEMA_VERSION = "ipmsm-target-load-candidate-summary-v1"
WORKFLOW_REVISION = "target-load-v4"
TARGET_LOAD_CONTROL_SOURCE = "independent_target_load_efficiency"
PROJECT_SOURCE_ROOT = Path(__file__).resolve().parent
COORDINATOR_SOURCE_PATH = PROJECT_SOURCE_ROOT / "ipmsm_target_load_coordinator.py"
PYAEDT_CORE_RELATIVE_PATH = Path("pyaedt_module/core/pydesktop.py")
PYAEDT_CORE_SOURCE_CANDIDATES = tuple(
    root / PYAEDT_CORE_RELATIVE_PATH
    for root in (
        Path("Y:/git/pyaedt_library/src"),
        PROJECT_SOURCE_ROOT.parent / "pyaedt_library" / "src",
        PROJECT_SOURCE_ROOT.parent / "git" / "pyaedt_library" / "src",
        Path("/home1/r1jae262/jupyter/git/pyaedt_library/src"),
        Path("/home1/dhj02/NEC/git/pyaedt_library/src"),
        Path("/home1/dw16/NEC/git/pyaedt_library/src"),
        Path("/home1/harry261/NEC/git/pyaedt_library/src"),
        Path("/home1/hmlee31/NEC/git/pyaedt_library/src"),
        Path("/home1/jji0930/NEC/git/pyaedt_library/src"),
        Path("/home1/wjddn5916/NEC/git/pyaedt_library/src"),
    )
)
PYAEDT_CORE_SOURCE_PATH = next(
    (path.resolve() for path in PYAEDT_CORE_SOURCE_CANDIDATES if path.is_file()),
    PYAEDT_CORE_SOURCE_CANDIDATES[0],
)
MIN_TASK_TIMEOUT_SECONDS = 43_200
RUNTIME_SOURCE_PATHS = MappingProxyType({
    "matcher_source": Path(target_load_matching.__file__).resolve(),
    "workflow_source": Path(__file__).resolve(),
    "coordinator_source": COORDINATOR_SOURCE_PATH,
    "atomic_publish_source": Path(__file__).resolve().with_name("atomic_publish.py"),
    "validator_source": Path(pareto_validator.__file__).resolve(),
    "submit_ipmsm_v2_campaign_source": Path(__file__).resolve().with_name(
        "submit_ipmsm_v2_campaign.py"
    ),
    "submit_ipmsm_scheduler_task_source": Path(__file__).resolve().with_name(
        "submit_ipmsm_scheduler_task.py"
    ),
    "submit_ipmsm_scheduler_job_source": Path(__file__).resolve().with_name(
        "submit_ipmsm_scheduler_job.py"
    ),
    "subprocess_run_source": Path(__file__).resolve().with_name("subprocess_run.py"),
    "run_ipmsm_batch_source": Path(__file__).resolve().with_name("run_ipmsm_batch.py"),
    "ipmsm_ppt_setup_source": Path(__file__).resolve().parent
    / "module"
    / "ipmsm_ppt_setup.py",
    "ipmsm_geometry_source": Path(__file__).resolve().parent
    / "module"
    / "ipmsm_geometry.py",
    "variable_source": Path(__file__).resolve().parent / "module" / "variable.py",
    "pyaedt_core_source": PYAEDT_CORE_SOURCE_PATH,
})
REQUIRED_SOURCE_HASHES = frozenset(
    {
        "optimization_spec_sha256",
        "pareto_sha256",
        "seed_fea_plan_sha256",
        "model_metadata_sha256",
        "model_artifact_manifest_sha256",
        "beta_calibration_artifact_sha256",
        "matcher_source_sha256",
        "workflow_source_sha256",
        "coordinator_source_sha256",
        "atomic_publish_source_sha256",
        "validator_source_sha256",
        "submit_ipmsm_v2_campaign_source_sha256",
        "submit_ipmsm_scheduler_task_source_sha256",
        "submit_ipmsm_scheduler_job_source_sha256",
        "subprocess_run_source_sha256",
        "run_ipmsm_batch_source_sha256",
        "ipmsm_ppt_setup_source_sha256",
        "ipmsm_geometry_source_sha256",
        "variable_source_sha256",
        "pyaedt_core_source_sha256",
    }
)
REQUIRED_SCHEDULER_FIELDS = frozenset(
    {
        "project",
        "project_id",
        "server_cap",
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
    }
)
SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")
SURROGATE_SELECTION_FIELDS = (
    "surrogate_torque_lcb_nm",
    "surrogate_voltage_peak_ucb_v",
    "surrogate_total_loss_ucb_w",
)
ATTEMPT_SURROGATE_STATUS = "seed_selection_only_not_recomputed_at_attempt_current"
ROOT_SOURCE_DOCUMENT_FIELDS = (
    "optimization_spec_json",
    "pareto_csv",
    "seed_fea_plan_csv",
    "model_metadata_json",
    "beta_calibration_manifest_json",
    "matcher_source",
    "workflow_source",
    "coordinator_source",
    "atomic_publish_source",
    "validator_source",
    "submit_ipmsm_v2_campaign_source",
    "submit_ipmsm_scheduler_task_source",
    "submit_ipmsm_scheduler_job_source",
    "subprocess_run_source",
    "run_ipmsm_batch_source",
    "ipmsm_ppt_setup_source",
    "ipmsm_geometry_source",
    "variable_source",
    "pyaedt_core_source",
)


class TargetLoadWorkflowError(RuntimeError):
    """The target-load workflow contract cannot be proven."""


def _validate_runtime_source_documents(documents: Mapping[str, bytes]) -> None:
    for field, path in RUNTIME_SOURCE_PATHS.items():
        try:
            runtime_bytes = path.read_bytes()
        except OSError as exc:
            raise TargetLoadWorkflowError(f"cannot read runtime source for {field}") from exc
        if documents.get(field) != runtime_bytes:
            raise TargetLoadWorkflowError(f"{field} differs from the executing runtime source")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TargetLoadWorkflowError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise TargetLoadWorkflowError(f"non-finite JSON constant: {value}")


def _exact_bytes(value: object, label: str) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise TargetLoadWorkflowError(f"{label} must be nonempty exact bytes")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _encode_exact_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_exact_bytes(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise TargetLoadWorkflowError(f"{label} must be non-empty canonical base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TargetLoadWorkflowError(f"{label} is not valid base64") from exc
    if not decoded or _encode_exact_bytes(decoded) != value:
        raise TargetLoadWorkflowError(f"{label} must be non-empty canonical base64")
    return decoded


def _strict_json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(
            _exact_bytes(payload, label).decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise TargetLoadWorkflowError(f"cannot decode strict {label}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise TargetLoadWorkflowError(f"{label} root must be an object")
    return decoded


def _producer_json_object(payload: bytes, label: str) -> dict[str, Any]:
    decoded = _strict_json_object(payload, label)
    expected = (
        json.dumps(
            decoded,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    if payload != expected:
        raise TargetLoadWorkflowError(f"{label} is not canonical producer JSON")
    return decoded


def _strict_csv(payload: bytes, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        stream = io.StringIO(_exact_bytes(payload, label).decode("utf-8-sig"), newline="")
        reader = csv.DictReader(stream)
        fieldnames = list(reader.fieldnames or ())
        if not fieldnames or any(not str(name or "").strip() for name in fieldnames):
            raise TargetLoadWorkflowError(f"{label} must have a nonblank CSV header")
        if len(fieldnames) != len(set(fieldnames)):
            raise TargetLoadWorkflowError(f"{label} has duplicate CSV header names")
        rows = [dict(row) for row in reader]
    except (UnicodeError, csv.Error) as exc:
        raise TargetLoadWorkflowError(f"cannot decode strict {label}: {exc}") from exc
    if not rows:
        raise TargetLoadWorkflowError(f"{label} must contain at least one row")
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise TargetLoadWorkflowError(f"{label} has fields beyond its header or missing cells")
    return fieldnames, rows


def _optimizer_canonical_json_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TargetLoadWorkflowError("optimizer provenance is not canonical JSON") from exc
    return _sha256_bytes(payload)


def canonical_json_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TargetLoadWorkflowError("contract value is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def canonical_float(value: object) -> str:
    number = _finite(value, "canonical float")
    if number == 0.0:
        number = 0.0
    return format(number, ".17g")


def _validate_sha256(value: object, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise TargetLoadWorkflowError(f"{label} must be a lowercase SHA256")
    return text


def _text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        raise TargetLoadWorkflowError(f"{label} must be nonblank")
    return text


def _artifact_binding(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise TargetLoadWorkflowError(f"{label} artifact binding is invalid")
    path = _text(value.get("path"), f"{label}.path")
    if not Path(path).is_absolute():
        raise TargetLoadWorkflowError(f"{label}.path must be absolute")
    return {
        "path": str(Path(path).resolve(strict=False)),
        "sha256": _validate_sha256(value.get("sha256"), f"{label}.sha256"),
    }


def _fingerprinted_id(value: object, namespace: str, label: str) -> str:
    text = _text(value, label)
    prefix = f"{namespace}:sha256:"
    if not text.startswith(prefix):
        raise TargetLoadWorkflowError(f"{label} namespace is invalid")
    _validate_sha256(text[len(prefix) :], label)
    return text


def validate_upstream_pareto_binding(value: object) -> dict[str, Any]:
    """Normalize the completed optimization/strict-FEA authority frozen by v4."""

    required = {
        "schema_version",
        "optimization_decision",
        "source_artifacts",
        "optimization_run_id",
        "execution_cwd",
        "validation",
        "authority_documents_base64",
        "model_artifacts_base64",
        "producer_sources_base64",
        "original_seed_candidate_ids",
        "fea_filtered_final_front_candidate_ids",
        "selected_candidate_ids",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise TargetLoadWorkflowError("upstream Pareto binding fields are invalid")
    if value.get("schema_version") != UPSTREAM_PARETO_BINDING_SCHEMA_VERSION:
        raise TargetLoadWorkflowError("upstream Pareto binding schema is invalid")

    decision = value.get("optimization_decision")
    if not isinstance(decision, Mapping) or set(decision) != {
        "path",
        "sha256",
        "schema_version",
        "contract_sha256",
        "mode",
        "status",
    }:
        raise TargetLoadWorkflowError("upstream optimization decision binding is invalid")
    normalized_decision: dict[str, Any] = {
        **_artifact_binding(
            {"path": decision.get("path"), "sha256": decision.get("sha256")},
            "upstream optimization decision",
        ),
        "schema_version": _text(
            decision.get("schema_version"), "upstream optimization decision schema"
        ),
        "contract_sha256": _validate_sha256(
            decision.get("contract_sha256"), "upstream optimization contract SHA256"
        ),
        "mode": decision.get("mode"),
        "status": decision.get("status"),
    }
    if normalized_decision["schema_version"] != "ipmsm_v2_optimization_continuation_v1":
        raise TargetLoadWorkflowError("upstream optimization decision schema is unsupported")
    if normalized_decision["mode"] != "execute" or normalized_decision["status"] != "complete":
        raise TargetLoadWorkflowError("upstream optimization decision is not complete")

    source_names = {
        "optimization_spec",
        "pareto",
        "seed_fea_plan",
        "pareto_fea_results",
        "model_metadata",
        "beta_calibration_manifest",
    }
    sources = value.get("source_artifacts")
    if not isinstance(sources, Mapping) or set(sources) != source_names | {
        "model_artifacts_manifest_sha256"
    }:
        raise TargetLoadWorkflowError("upstream source artifact coverage is invalid")
    normalized_sources: dict[str, Any] = {
        name: _artifact_binding(sources.get(name), f"upstream source {name}")
        for name in sorted(source_names)
    }
    normalized_sources["model_artifacts_manifest_sha256"] = _validate_sha256(
        sources.get("model_artifacts_manifest_sha256"),
        "upstream model artifact manifest SHA256",
    )

    validation = value.get("validation")
    if not isinstance(validation, Mapping) or set(validation) != {
        "validation_id",
        "summary_schema_version",
        "final_front_schema_version",
        "summary",
        "rows",
        "final_front",
        "status",
        "pass",
    }:
        raise TargetLoadWorkflowError("upstream validation binding is invalid")
    normalized_validation: dict[str, Any] = {
        "validation_id": _fingerprinted_id(
            validation.get("validation_id"),
            "ipmsm-pareto-fea-validation",
            "upstream validation_id",
        ),
        "summary_schema_version": _text(
            validation.get("summary_schema_version"), "upstream summary schema"
        ),
        "final_front_schema_version": _text(
            validation.get("final_front_schema_version"), "upstream final-front schema"
        ),
        "summary": _artifact_binding(validation.get("summary"), "upstream validation summary"),
        "rows": _artifact_binding(validation.get("rows"), "upstream validation rows"),
        "final_front": _artifact_binding(
            validation.get("final_front"), "upstream validation final front"
        ),
        "status": validation.get("status"),
        "pass": validation.get("pass"),
    }
    if normalized_validation["summary_schema_version"] != pareto_validator.SUMMARY_SCHEMA_VERSION:
        raise TargetLoadWorkflowError("upstream validation summary schema is unsupported")
    if normalized_validation["final_front_schema_version"] != (
        pareto_validator.FINAL_FRONT_SCHEMA_VERSION
    ):
        raise TargetLoadWorkflowError("upstream final-front schema is unsupported")
    if normalized_validation["status"] != "passed" or normalized_validation["pass"] is not True:
        raise TargetLoadWorkflowError("upstream strict Pareto FEA gate did not pass")

    authority_names = {
        "optimization_decision_json",
        "original_seed_fea_plan_csv",
        "pareto_fea_results_csv",
        "validation_summary_json",
        "validation_rows_csv",
        "final_front_csv",
    }
    authority = value.get("authority_documents_base64")
    if not isinstance(authority, Mapping) or set(authority) != authority_names:
        raise TargetLoadWorkflowError("upstream authority document coverage is invalid")
    normalized_authority = {
        name: _encode_exact_bytes(
            _decode_exact_bytes(authority[name], f"upstream authority {name}")
        )
        for name in sorted(authority_names)
    }
    encoded_models = value.get("model_artifacts_base64")
    if not isinstance(encoded_models, Mapping) or not encoded_models:
        raise TargetLoadWorkflowError("upstream embedded model artifacts are missing")
    normalized_models: dict[str, str] = {}
    for raw_name, encoded in sorted(encoded_models.items()):
        name = _text(raw_name, "upstream model artifact basename")
        if Path(name).name != name or name in normalized_models:
            raise TargetLoadWorkflowError("upstream model artifact basename is unsafe or duplicate")
        normalized_models[name] = _encode_exact_bytes(
            _decode_exact_bytes(encoded, f"upstream model artifact {name}")
        )
    encoded_sources = value.get("producer_sources_base64")
    if not isinstance(encoded_sources, Mapping) or not encoded_sources:
        raise TargetLoadWorkflowError("upstream producer sources are missing")
    normalized_producer_sources: dict[str, str] = {}
    for raw_name, encoded in sorted(encoded_sources.items()):
        name = _text(raw_name, "upstream producer source name")
        if Path(name).name != name or name in normalized_producer_sources:
            raise TargetLoadWorkflowError("upstream producer source name is unsafe or duplicate")
        normalized_producer_sources[name] = _encode_exact_bytes(
            _decode_exact_bytes(encoded, f"upstream producer source {name}")
        )

    def candidate_ids(raw: object, label: str) -> list[str]:
        if not isinstance(raw, list) or not raw:
            raise TargetLoadWorkflowError(f"{label} must be a nonempty array")
        result = [_text(item, f"{label} item") for item in raw]
        if len(result) != len(set(result)):
            raise TargetLoadWorkflowError(f"{label} contains duplicate IDs")
        return result

    original = candidate_ids(
        value.get("original_seed_candidate_ids"), "upstream original seed candidate IDs"
    )
    final_front = candidate_ids(
        value.get("fea_filtered_final_front_candidate_ids"),
        "upstream FEA-filtered final-front candidate IDs",
    )
    selected = candidate_ids(
        value.get("selected_candidate_ids"), "upstream selected candidate IDs"
    )
    if not set(final_front) <= set(original):
        raise TargetLoadWorkflowError("upstream final front contains an unknown seed candidate")
    expected_selected = [candidate_id for candidate_id in original if candidate_id in set(final_front)]
    if selected != expected_selected or set(selected) != set(final_front):
        raise TargetLoadWorkflowError("upstream selected candidates do not preserve seed-plan order")
    execution_cwd = Path(_text(value.get("execution_cwd"), "upstream execution cwd"))
    if not execution_cwd.is_absolute():
        raise TargetLoadWorkflowError("upstream execution cwd must be absolute")
    execution_cwd = execution_cwd.resolve(strict=False)

    return {
        "schema_version": UPSTREAM_PARETO_BINDING_SCHEMA_VERSION,
        "optimization_decision": normalized_decision,
        "source_artifacts": normalized_sources,
        "optimization_run_id": _fingerprinted_id(
            value.get("optimization_run_id"),
            "ipmsm-optimization-run",
            "upstream optimization_run_id",
        ),
        "execution_cwd": str(execution_cwd),
        "validation": normalized_validation,
        "authority_documents_base64": normalized_authority,
        "model_artifacts_base64": normalized_models,
        "producer_sources_base64": normalized_producer_sources,
        "original_seed_candidate_ids": original,
        "fea_filtered_final_front_candidate_ids": final_front,
        "selected_candidate_ids": selected,
    }


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise TargetLoadWorkflowError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TargetLoadWorkflowError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise TargetLoadWorkflowError(f"{label} must be a finite number")
    return number


def _nonnegative(value: object, label: str) -> float:
    number = _finite(value, label)
    if number < 0.0:
        raise TargetLoadWorkflowError(f"{label} must be >= 0")
    return number


def _safe_component(value: object, *, maximum: int = 20) -> str:
    text = SAFE_ID.sub("_", _text(value, "identifier")).strip("_")
    return (text or "id")[:maximum]


def _namespaced_id(namespace: str, payload: Mapping[str, Any]) -> str:
    return f"{namespace}:sha256:{canonical_json_sha256(payload)}"


@dataclass(frozen=True)
class MatchPolicyTemplate:
    relative_tolerance: float
    minimum_current_peak_a: float
    maximum_current_peak_a: float
    max_attempts: int
    monotonic_relative_tolerance: float
    minimum_step_relative: float = 0.01
    maximum_scale_per_attempt: float = 1.5

    def __post_init__(self) -> None:
        lower = _nonnegative(self.minimum_current_peak_a, "minimum_current_peak_a")
        upper = _finite(self.maximum_current_peak_a, "maximum_current_peak_a")
        tolerance = _finite(self.relative_tolerance, "relative_tolerance")
        if tolerance <= 0.0 or tolerance > 0.01:
            raise ValueError("relative_tolerance must be in (0, 0.01]")
        if lower != 0.0:
            raise ValueError("minimum_current_peak_a must be exactly 0 for full legal-range matching")
        if upper <= lower:
            raise ValueError("maximum_current_peak_a must exceed minimum_current_peak_a")
        bootstrap = max(lower, upper * 0.5)
        if bootstrap <= max(1.0e-9, upper * 1.0e-9):
            bootstrap = upper
        TargetLoadPolicy(
            target_value=1.0,
            relative_tolerance=self.relative_tolerance,
            initial_current_peak_a=bootstrap,
            minimum_current_peak_a=lower,
            maximum_current_peak_a=upper,
            max_attempts=self.max_attempts,
            monotonic_relative_tolerance=self.monotonic_relative_tolerance,
            minimum_step_relative=self.minimum_step_relative,
            maximum_scale_per_attempt=self.maximum_scale_per_attempt,
        )

    def materialize(self, *, target_value: float, initial_current_peak_a: float) -> TargetLoadPolicy:
        return TargetLoadPolicy(
            target_value=target_value,
            relative_tolerance=self.relative_tolerance,
            initial_current_peak_a=initial_current_peak_a,
            minimum_current_peak_a=self.minimum_current_peak_a,
            maximum_current_peak_a=self.maximum_current_peak_a,
            max_attempts=self.max_attempts,
            monotonic_relative_tolerance=self.monotonic_relative_tolerance,
            minimum_step_relative=self.minimum_step_relative,
            maximum_scale_per_attempt=self.maximum_scale_per_attempt,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_tolerance": self.relative_tolerance,
            "minimum_current_peak_a": self.minimum_current_peak_a,
            "maximum_current_peak_a": self.maximum_current_peak_a,
            "max_attempts": self.max_attempts,
            "monotonic_relative_tolerance": self.monotonic_relative_tolerance,
            "minimum_step_relative": self.minimum_step_relative,
            "maximum_scale_per_attempt": self.maximum_scale_per_attempt,
        }


def _policy_dict(policy: TargetLoadPolicy) -> dict[str, Any]:
    return {
        "target_value": policy.target_value,
        "relative_tolerance": policy.relative_tolerance,
        "initial_current_peak_a": policy.initial_current_peak_a,
        "minimum_current_peak_a": policy.minimum_current_peak_a,
        "maximum_current_peak_a": policy.maximum_current_peak_a,
        "max_attempts": policy.max_attempts,
        "monotonic_relative_tolerance": policy.monotonic_relative_tolerance,
        "minimum_step_relative": policy.minimum_step_relative,
        "maximum_scale_per_attempt": policy.maximum_scale_per_attempt,
    }


def _policy_from_probe(probe: Mapping[str, Any]) -> TargetLoadPolicy:
    policy = probe.get("policy")
    if not isinstance(policy, Mapping):
        raise TargetLoadWorkflowError("probe policy is missing")
    try:
        return TargetLoadPolicy(**dict(policy))
    except (TypeError, ValueError) as exc:
        raise TargetLoadWorkflowError("probe policy is invalid") from exc


def _validate_source_hashes(source_hashes: Mapping[str, object]) -> dict[str, str]:
    if set(source_hashes) != REQUIRED_SOURCE_HASHES:
        missing = sorted(REQUIRED_SOURCE_HASHES - set(source_hashes))
        extra = sorted(set(source_hashes) - REQUIRED_SOURCE_HASHES)
        raise TargetLoadWorkflowError(
            f"source hash contract differs: missing={missing} extra={extra}"
        )
    return {key: _validate_sha256(value, key) for key, value in source_hashes.items()}


def _validate_scheduler_contract(contract: Mapping[str, object]) -> dict[str, Any]:
    if set(contract) != REQUIRED_SCHEDULER_FIELDS:
        missing = sorted(REQUIRED_SCHEDULER_FIELDS - set(contract))
        extra = sorted(set(contract) - REQUIRED_SCHEDULER_FIELDS)
        raise TargetLoadWorkflowError(
            f"scheduler contract differs: missing={missing} extra={extra}"
        )
    normalized = {key: contract[key] for key in sorted(contract)}
    if _text(contract["endpoint"], "scheduler endpoint") != "/api/tasks":
        raise TargetLoadWorkflowError("target-load automation requires /api/tasks")
    if _text(contract["scheduling_profile"], "scheduling profile") != "fea_bursty":
        raise TargetLoadWorkflowError("target-load automation requires fea_bursty")
    if _text(contract["required_capability"], "required capability") != "conda:pyaedt2026v1":
        raise TargetLoadWorkflowError("target-load automation requires conda:pyaedt2026v1")
    if _text(contract["env_profile"], "environment profile") != "pyaedt2026v1":
        raise TargetLoadWorkflowError("target-load automation requires pyaedt2026v1")
    if "module load ansys-electronics/v252" not in _text(contract["env_setup"], "env_setup"):
        raise TargetLoadWorkflowError("target-load automation requires the explicit Ansys module")
    if _text(contract["entrypoint"], "scheduler entrypoint") != "subprocess_run.py":
        raise TargetLoadWorkflowError("target-load automation requires subprocess_run.py")
    if _text(contract["partition"], "scheduler partition") != "auto":
        raise TargetLoadWorkflowError("target-load automation requires partition='auto'")
    project_id = contract["project_id"]
    server_cap = contract["server_cap"]
    max_workers = contract["max_workers_per_node"]
    cpus = contract["cpus"]
    cores_per_process = contract["cores_per_process"]
    memory_mb = contract["memory_mb"]
    task_timeout_seconds = contract["task_timeout_seconds"]
    for value, label in (
        (project_id, "project_id"),
        (server_cap, "server_cap"),
        (max_workers, "max_workers_per_node"),
        (cpus, "cpus"),
        (cores_per_process, "cores_per_process"),
        (memory_mb, "memory_mb"),
        (task_timeout_seconds, "task_timeout_seconds"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise TargetLoadWorkflowError(f"{label} must be a positive integer")
    if server_cap > 200:
        raise TargetLoadWorkflowError("server_cap must not exceed the 200-task concurrency cap")
    if max_workers > server_cap:
        raise TargetLoadWorkflowError("max_workers_per_node must not exceed server_cap")
    if cores_per_process > cpus:
        raise TargetLoadWorkflowError("cores_per_process must not exceed cpus")
    if task_timeout_seconds < MIN_TASK_TIMEOUT_SECONDS:
        raise TargetLoadWorkflowError(
            f"task_timeout_seconds must be >= {MIN_TASK_TIMEOUT_SECONDS}"
        )
    for field in ("project", "remote_root"):
        _text(contract[field], f"scheduler {field}")
    return normalized


def _validated_model_fingerprints(
    metadata: Mapping[str, Any],
    spec: OptimizationSpec,
) -> dict[str, str]:
    if metadata.get("training_schema") != pareto_validator.V2_TRAINING_SCHEMA:
        raise TargetLoadWorkflowError("surrogate metadata training_schema is not strict v2")
    if metadata.get("feature_bounds_source") != pareto_validator.FEATURE_BOUNDS_SOURCE:
        raise TargetLoadWorkflowError("surrogate metadata feature_bounds_source is not the train split")
    threshold = _finite(metadata.get("r2_threshold"), "surrogate metadata r2_threshold")
    voltage_threshold = _finite(
        metadata.get("voltage_r2_threshold"),
        "surrogate metadata voltage_r2_threshold",
    )
    voltage_r2 = _finite(metadata.get("voltage_test_r2"), "surrogate metadata voltage_test_r2")
    if threshold < pareto_validator.MIN_OPTIMIZER_R2:
        raise TargetLoadWorkflowError("surrogate metadata primary R2 threshold is below production")
    if metadata.get("primary_test_r2_gate_complete") is not True or metadata.get(
        "primary_test_r2_gate_passed"
    ) is not True:
        raise TargetLoadWorkflowError("surrogate metadata primary R2 gate has not passed")
    primary = metadata.get("primary_test_r2")
    if not isinstance(primary, Mapping):
        raise TargetLoadWorkflowError("surrogate metadata primary_test_r2 is missing")
    for target in pareto_validator.PRIMARY_R2_TARGETS:
        if _finite(primary.get(target), f"primary_test_r2.{target}") < pareto_validator.MIN_OPTIMIZER_R2:
            raise TargetLoadWorkflowError(f"surrogate target {target} is below the production R2 gate")
    if metadata.get("voltage_test_r2_gate_complete") is not True or metadata.get(
        "voltage_test_r2_gate_passed"
    ) is not True:
        raise TargetLoadWorkflowError("surrogate metadata voltage R2 gate has not passed")
    if voltage_r2 < max(voltage_threshold, pareto_validator.MIN_OPTIMIZER_R2):
        raise TargetLoadWorkflowError("surrogate voltage R2 is below the production gate")

    raw = metadata.get("fingerprints")
    if not isinstance(raw, Mapping):
        raise TargetLoadWorkflowError("surrogate metadata fingerprints are missing")
    fingerprints = {
        column: _text(raw.get(column), f"surrogate fingerprint {column}")
        for column in pareto_validator.MODEL_FINGERPRINT_COLUMNS
    }
    expected = {
        "input_dataset_schema_version": nsga2.FEA_DATASET_SCHEMA_VERSION,
        "input_quality_profile": nsga2.REFERENCE_FEA_QUALITY_PROFILE,
        "input_beta_calibration_id": spec.beta_calibration.calibration_id,
        "input_beta_convention": nsga2.BETA_CONVENTION,
        "input_model_extent": nsga2.FEA_MODEL_EXTENT,
    }
    for column, value in expected.items():
        if fingerprints[column] != value:
            raise TargetLoadWorkflowError(f"surrogate fingerprint {column} differs from the spec")
    if not re.fullmatch(r"setup_v2:sha256:[0-9a-f]{64}", fingerprints["input_setup_fingerprint"]):
        raise TargetLoadWorkflowError("surrogate setup fingerprint is invalid")
    if not re.fullmatch(
        r"materials_v2:sha256:[0-9a-f]{64}",
        fingerprints["input_material_fingerprint"],
    ):
        raise TargetLoadWorkflowError("surrogate material fingerprint is invalid")
    if fingerprints["input_aedt_version"].lower() in {"auto", "unknown"}:
        raise TargetLoadWorkflowError("surrogate AEDT version is unknown")
    return fingerprints


def _model_artifact_index(metadata: Mapping[str, Any]) -> list[tuple[str, int, str]]:
    model_paths = metadata.get("model_paths")
    if not isinstance(model_paths, Mapping) or not model_paths:
        raise TargetLoadWorkflowError("surrogate metadata model_paths must be a nonempty object")
    seen_basenames: set[str] = set()
    indexed_paths: list[tuple[str, int, str]] = []
    for target in sorted(model_paths):
        recorded = model_paths[target]
        if isinstance(recorded, str):
            values = [recorded]
        elif isinstance(recorded, Sequence) and not isinstance(recorded, (str, bytes)):
            values = list(recorded)
        else:
            raise TargetLoadWorkflowError(f"model_paths.{target} must be a path or path array")
        if not values or any(not isinstance(value, str) or not value for value in values):
            raise TargetLoadWorkflowError(f"model_paths.{target} contains an invalid path")
        for index, value in enumerate(values):
            basename = Path(value).name
            if not basename or basename == surrogate_bundle.METADATA_FILENAME:
                raise TargetLoadWorkflowError("model artifact basename is reserved or empty")
            if basename in seen_basenames:
                raise TargetLoadWorkflowError("metadata model paths contain a duplicate basename")
            seen_basenames.add(basename)
            indexed_paths.append((str(target), index, basename))
    return indexed_paths


def _model_artifact_hashes(
    metadata: Mapping[str, Any],
    artifacts_by_basename: Mapping[str, bytes],
) -> dict[str, str]:
    indexed_paths = _model_artifact_index(metadata)
    normalized_artifacts: dict[str, bytes] = {}
    for raw_name, payload in artifacts_by_basename.items():
        name = _text(raw_name, "model artifact basename")
        if Path(name).name != name or name in normalized_artifacts:
            raise TargetLoadWorkflowError("model artifact keys must be unique basenames")
        normalized_artifacts[name] = _exact_bytes(payload, f"model artifact {name}")
    expected_basenames = {basename for _, _, basename in indexed_paths}
    if set(normalized_artifacts) != expected_basenames:
        raise TargetLoadWorkflowError("exact model artifacts differ from metadata model_paths")
    return {
        f"{target}[{index}]::{basename}": _sha256_bytes(normalized_artifacts[basename])
        for target, index, basename in indexed_paths
    }


def _validated_surrogate_bundle_documents(
    metadata_json: bytes,
    artifacts_by_basename: Mapping[str, bytes],
) -> dict[str, str]:
    """Run the production bundle loader against exact in-memory artifacts."""

    try:
        with tempfile.TemporaryDirectory(prefix="ipmsm-target-load-bundle-") as temporary:
            root = Path(temporary)
            (root / surrogate_bundle.METADATA_FILENAME).write_bytes(
                _exact_bytes(metadata_json, "surrogate metadata")
            )
            for raw_name, raw_payload in artifacts_by_basename.items():
                name = _text(raw_name, "model artifact basename")
                if Path(name).name != name or name == surrogate_bundle.METADATA_FILENAME:
                    raise TargetLoadWorkflowError("model artifact keys must be safe basenames")
                (root / name).write_bytes(_exact_bytes(raw_payload, f"model artifact {name}"))
            loaded = surrogate_bundle.load_surrogate_bundle(root)
            return dict(loaded.fingerprints)
    except surrogate_bundle.SurrogateBundleError as exc:
        raise TargetLoadWorkflowError(f"strict surrogate bundle validation failed: {exc}") from exc


def _validated_root_documents(
    *,
    optimization_spec_json: bytes,
    pareto_csv: bytes,
    seed_fea_plan_csv: bytes,
    model_metadata_json: bytes,
    model_artifacts_by_basename: Mapping[str, bytes],
    beta_calibration_manifest_json: bytes,
    matcher_source: bytes,
    workflow_source: bytes,
    coordinator_source: bytes,
    atomic_publish_source: bytes,
    validator_source: bytes,
    submit_ipmsm_v2_campaign_source: bytes,
    submit_ipmsm_scheduler_task_source: bytes,
    submit_ipmsm_scheduler_job_source: bytes,
    subprocess_run_source: bytes,
    run_ipmsm_batch_source: bytes,
    ipmsm_ppt_setup_source: bytes,
    ipmsm_geometry_source: bytes,
    variable_source: bytes,
    pyaedt_core_source: bytes,
) -> tuple[
    OptimizationSpec,
    dict[str, Any],
    list[dict[str, str]],
    list[str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    exact_documents = {
        "optimization_spec_json": _exact_bytes(optimization_spec_json, "optimization spec"),
        "pareto_csv": _exact_bytes(pareto_csv, "Pareto front"),
        "seed_fea_plan_csv": _exact_bytes(seed_fea_plan_csv, "seed FEA plan"),
        "model_metadata_json": _exact_bytes(model_metadata_json, "surrogate metadata"),
        "beta_calibration_manifest_json": _exact_bytes(
            beta_calibration_manifest_json,
            "beta calibration manifest",
        ),
        "matcher_source": _exact_bytes(matcher_source, "matcher source"),
        "workflow_source": _exact_bytes(workflow_source, "workflow source"),
        "coordinator_source": _exact_bytes(coordinator_source, "coordinator source"),
        "atomic_publish_source": _exact_bytes(
            atomic_publish_source, "atomic_publish source"
        ),
        "validator_source": _exact_bytes(validator_source, "validator source"),
        "submit_ipmsm_v2_campaign_source": _exact_bytes(
            submit_ipmsm_v2_campaign_source,
            "submit_ipmsm_v2_campaign source",
        ),
        "submit_ipmsm_scheduler_task_source": _exact_bytes(
            submit_ipmsm_scheduler_task_source,
            "submit_ipmsm_scheduler_task source",
        ),
        "submit_ipmsm_scheduler_job_source": _exact_bytes(
            submit_ipmsm_scheduler_job_source,
            "submit_ipmsm_scheduler_job source",
        ),
        "subprocess_run_source": _exact_bytes(
            subprocess_run_source,
            "subprocess_run source",
        ),
        "run_ipmsm_batch_source": _exact_bytes(
            run_ipmsm_batch_source,
            "run_ipmsm_batch source",
        ),
        "ipmsm_ppt_setup_source": _exact_bytes(
            ipmsm_ppt_setup_source,
            "ipmsm_ppt_setup source",
        ),
        "ipmsm_geometry_source": _exact_bytes(
            ipmsm_geometry_source,
            "ipmsm_geometry source",
        ),
        "variable_source": _exact_bytes(variable_source, "variable source"),
        "pyaedt_core_source": _exact_bytes(pyaedt_core_source, "pyaedt core source"),
    }
    _validate_runtime_source_documents(exact_documents)
    spec_mapping = _strict_json_object(optimization_spec_json, "optimization spec")
    metadata = _strict_json_object(model_metadata_json, "surrogate metadata")
    calibration_manifest = _strict_json_object(
        beta_calibration_manifest_json,
        "beta calibration manifest",
    )
    try:
        spec = optimization_spec_from_mapping(spec_mapping)
        electrical_zero_deg, calibration_id = validated_zero_manifest(calibration_manifest)
    except (OptimizationSpecError, ValueError) as exc:
        raise TargetLoadWorkflowError(str(exc)) from exc
    if calibration_id != spec.beta_calibration.calibration_id or not math.isclose(
        electrical_zero_deg,
        spec.beta_calibration.electrical_zero_deg,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise TargetLoadWorkflowError("optimization spec beta calibration differs from its exact manifest")
    if str(calibration_manifest.get("convention") or "") != spec.beta_calibration.convention:
        raise TargetLoadWorkflowError("optimization spec beta convention differs from its exact manifest")

    artifact_hashes = _model_artifact_hashes(metadata, model_artifacts_by_basename)
    bundle_fingerprints = _validated_surrogate_bundle_documents(
        model_metadata_json,
        model_artifacts_by_basename,
    )
    fingerprints = _validated_model_fingerprints(metadata, spec)
    if bundle_fingerprints != fingerprints:
        raise TargetLoadWorkflowError("loaded surrogate fingerprints differ from exact metadata")
    plan_fields, plan_rows = _strict_csv(seed_fea_plan_csv, "seed FEA plan")
    pareto_fields, pareto_rows = _strict_csv(pareto_csv, "Pareto front")
    raw_source_hashes = {
        "optimization_spec_sha256": _sha256_bytes(exact_documents["optimization_spec_json"]),
        "pareto_sha256": _sha256_bytes(exact_documents["pareto_csv"]),
        "seed_fea_plan_sha256": _sha256_bytes(exact_documents["seed_fea_plan_csv"]),
        "model_metadata_sha256": _sha256_bytes(exact_documents["model_metadata_json"]),
        "model_artifact_manifest_sha256": _optimizer_canonical_json_sha256(artifact_hashes),
        "beta_calibration_artifact_sha256": _sha256_bytes(
            exact_documents["beta_calibration_manifest_json"]
        ),
        "matcher_source_sha256": _sha256_bytes(exact_documents["matcher_source"]),
        "workflow_source_sha256": _sha256_bytes(exact_documents["workflow_source"]),
        "coordinator_source_sha256": _sha256_bytes(exact_documents["coordinator_source"]),
        "atomic_publish_source_sha256": _sha256_bytes(
            exact_documents["atomic_publish_source"]
        ),
        "validator_source_sha256": _sha256_bytes(exact_documents["validator_source"]),
        "submit_ipmsm_v2_campaign_source_sha256": _sha256_bytes(
            exact_documents["submit_ipmsm_v2_campaign_source"]
        ),
        "submit_ipmsm_scheduler_task_source_sha256": _sha256_bytes(
            exact_documents["submit_ipmsm_scheduler_task_source"]
        ),
        "submit_ipmsm_scheduler_job_source_sha256": _sha256_bytes(
            exact_documents["submit_ipmsm_scheduler_job_source"]
        ),
        "subprocess_run_source_sha256": _sha256_bytes(
            exact_documents["subprocess_run_source"]
        ),
        "run_ipmsm_batch_source_sha256": _sha256_bytes(
            exact_documents["run_ipmsm_batch_source"]
        ),
        "ipmsm_ppt_setup_source_sha256": _sha256_bytes(
            exact_documents["ipmsm_ppt_setup_source"]
        ),
        "ipmsm_geometry_source_sha256": _sha256_bytes(
            exact_documents["ipmsm_geometry_source"]
        ),
        "variable_source_sha256": _sha256_bytes(exact_documents["variable_source"]),
        "pyaedt_core_source_sha256": _sha256_bytes(exact_documents["pyaedt_core_source"]),
    }
    provenance_context = {
        nsga2.OPTIMIZATION_SPEC_SHA256_FIELD: raw_source_hashes["optimization_spec_sha256"],
        nsga2.SURROGATE_METADATA_SHA256_FIELD: raw_source_hashes["model_metadata_sha256"],
        nsga2.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: raw_source_hashes[
            "model_artifact_manifest_sha256"
        ],
        nsga2.SURROGATE_VERIFICATION_FIELD: nsga2.STRICT_BUNDLE_VERIFICATION,
    }
    try:
        expected_provenance = nsga2.build_optimization_run_provenance(
            pareto_csv,
            provenance_context,
        )
        candidate_order = pareto_validator.validate_case_plan(
            spec,
            plan_fields,
            plan_rows,
            expected_provenance,
        )
        pareto_validator.validate_pareto_front(
            spec,
            pareto_fields,
            pareto_rows,
            plan_rows,
            candidate_order,
        )
    except (ValueError, pareto_validator.ParetoFEAValidationError) as exc:
        raise TargetLoadWorkflowError(f"strict seed optimization validation failed: {exc}") from exc
    return (
        spec,
        spec_mapping,
        plan_rows,
        candidate_order,
        raw_source_hashes,
        fingerprints,
        artifact_hashes,
        expected_provenance,
        {key: _encode_exact_bytes(value) for key, value in exact_documents.items()},
    )


def _target_payload(point: Any) -> dict[str, Any]:
    target_value = point.required_torque_nm if point.target_kind == "torque" else point.required_power_w
    return {
        "operating_point_id": point.name,
        "target_kind": point.target_kind,
        "target_value": target_value,
        "target_unit": "N*m" if point.target_kind == "torque" else "W",
        "speed_rpm": point.speed_rpm,
        "required_torque_nm": point.required_torque_nm,
        "required_power_w": point.required_power_w,
        "duty_weight": point.duty_weight,
        "power_basis": "mechanical_shaft_output",
    }


def _ordered_probe_seeds(
    base_plan_rows: Sequence[Mapping[str, object]],
    spec: OptimizationSpec,
    policy_template: MatchPolicyTemplate,
) -> tuple[list[str], list[dict[str, Any]]]:
    if not base_plan_rows:
        raise TargetLoadWorkflowError("seed FEA plan is empty")
    if policy_template.maximum_current_peak_a > spec.effective_peak_current_limit_a + 1.0e-12:
        raise TargetLoadWorkflowError("policy maximum current exceeds the spec effective current limit")
    candidate_order: list[str] = []
    seen_case_ids: set[str] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    rows_by_candidate_point: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for index, raw in enumerate(base_plan_rows, start=1):
        row = dict(raw)
        case_id = _text(row.get("case_id"), f"seed plan row {index} case_id")
        candidate_id = _text(row.get("candidate_id"), f"seed plan row {index} candidate_id")
        point_id = _text(row.get("operating_point_id"), f"seed plan row {index} operating_point_id")
        role = _text(row.get("beta_validation_role"), f"seed plan row {index} beta role")
        if case_id in seen_case_ids:
            raise TargetLoadWorkflowError(f"duplicate seed case_id: {case_id}")
        key = (candidate_id, point_id, role)
        if key in seen_keys:
            raise TargetLoadWorkflowError(f"duplicate seed probe key: {key}")
        if role not in BETA_VALIDATION_ROLES:
            raise TargetLoadWorkflowError(f"unknown beta validation role: {role}")
        if case_id != beta_validation_case_id(candidate_id, point_id, role):
            raise TargetLoadWorkflowError("seed FEA plan case_id is not canonical v3 identity")
        if candidate_id not in candidate_order:
            candidate_order.append(candidate_id)
        seen_case_ids.add(case_id)
        seen_keys.add(key)
        rows_by_candidate_point.setdefault((candidate_id, point_id), []).append(row)

    point_by_name = {point.name: point for point in spec.operating_points}
    if {point for _, point in rows_by_candidate_point} != set(point_by_name):
        raise TargetLoadWorkflowError("seed plan operating-point coverage differs from the spec")
    probes: list[dict[str, Any]] = []
    for candidate_id in candidate_order:
        for point in spec.operating_points:
            rows = rows_by_candidate_point.get((candidate_id, point.name), [])
            center_rows = [
                row
                for row in rows
                if str(row.get("beta_validation_role") or "").strip() == BETA_VALIDATION_ROLE_CENTER
            ]
            if len(center_rows) != 1:
                raise TargetLoadWorkflowError("seed plan must contain exactly one selected beta center")
            center_beta = _finite(center_rows[0].get("selected_beta_dq_deg"), "selected beta")
            expected_points = local_beta_validation_points(center_beta, spec.beta_bounds_deg)
            expected_roles = [role for role, _, _ in expected_points]
            actual_by_role = {
                _text(row.get("beta_validation_role"), "beta role"): row
                for row in rows
            }
            if list(actual_by_role) != expected_roles or len(rows) != len(expected_points):
                raise TargetLoadWorkflowError("seed plan beta role order/coverage differs from canonical v3")
            seed_currents = {_finite(row.get("i_peak_a"), "seed current") for row in rows}
            if len(seed_currents) != 1:
                raise TargetLoadWorkflowError("seed v3 plan must have one shared current before independent matching")
            initial_current = next(iter(seed_currents))
            target = _target_payload(point)
            target_value = float(target["target_value"])
            for role, beta, offset in expected_points:
                row = actual_by_role[role]
                if not math.isclose(_finite(row.get("beta_dq_deg"), "beta"), beta, abs_tol=1.0e-12):
                    raise TargetLoadWorkflowError("seed beta value differs from canonical local probe")
                if not math.isclose(_finite(row.get("beta_offset_deg"), "beta offset"), offset, abs_tol=1.0e-12):
                    raise TargetLoadWorkflowError("seed beta offset differs from canonical local probe")
                policy = policy_template.materialize(
                    target_value=target_value,
                    initial_current_peak_a=initial_current,
                )
                probes.append(
                    {
                        "base_case_id": str(row["case_id"]).strip(),
                        "candidate_id": candidate_id,
                        "operating_point_id": point.name,
                        "beta_validation_role": role,
                        "beta_dq_deg": beta,
                        "selected_beta_dq_deg": center_beta,
                        "beta_offset_deg": offset,
                        "target": target,
                        "policy": _policy_dict(policy),
                        "policy_sha256": canonical_json_sha256(_policy_dict(policy)),
                        "base_row": row,
                        "base_row_sha256": canonical_json_sha256(row),
                    }
                )
    return candidate_order, probes


def build_root_manifest(
    *,
    optimization_spec_json: bytes,
    pareto_csv: bytes,
    seed_fea_plan_csv: bytes,
    model_metadata_json: bytes,
    model_artifacts_by_basename: Mapping[str, bytes],
    beta_calibration_manifest_json: bytes,
    matcher_source: bytes,
    workflow_source: bytes,
    coordinator_source: bytes,
    atomic_publish_source: bytes,
    validator_source: bytes,
    submit_ipmsm_v2_campaign_source: bytes,
    submit_ipmsm_scheduler_task_source: bytes,
    submit_ipmsm_scheduler_job_source: bytes,
    subprocess_run_source: bytes,
    run_ipmsm_batch_source: bytes,
    ipmsm_ppt_setup_source: bytes,
    ipmsm_geometry_source: bytes,
    variable_source: bytes,
    pyaedt_core_source: bytes,
    upstream_pareto_binding: Mapping[str, Any],
    scheduler_contract: Mapping[str, object],
    policy_template: MatchPolicyTemplate,
    task_retry_limit: int,
    result_settle_seconds: int,
    result_identity_relative_tolerance: float,
    revision: str = WORKFLOW_REVISION,
) -> dict[str, Any]:
    (
        spec,
        spec_mapping,
        base_plan_rows,
        strict_candidate_order,
        source_hashes,
        model_fingerprints,
        model_artifact_hashes,
        optimization_provenance,
        source_documents_base64,
    ) = _validated_root_documents(
        optimization_spec_json=optimization_spec_json,
        pareto_csv=pareto_csv,
        seed_fea_plan_csv=seed_fea_plan_csv,
        model_metadata_json=model_metadata_json,
        model_artifacts_by_basename=model_artifacts_by_basename,
        beta_calibration_manifest_json=beta_calibration_manifest_json,
        matcher_source=matcher_source,
        workflow_source=workflow_source,
        coordinator_source=coordinator_source,
        atomic_publish_source=atomic_publish_source,
        validator_source=validator_source,
        submit_ipmsm_v2_campaign_source=submit_ipmsm_v2_campaign_source,
        submit_ipmsm_scheduler_task_source=submit_ipmsm_scheduler_task_source,
        submit_ipmsm_scheduler_job_source=submit_ipmsm_scheduler_job_source,
        subprocess_run_source=subprocess_run_source,
        run_ipmsm_batch_source=run_ipmsm_batch_source,
        ipmsm_ppt_setup_source=ipmsm_ppt_setup_source,
        ipmsm_geometry_source=ipmsm_geometry_source,
        variable_source=variable_source,
        pyaedt_core_source=pyaedt_core_source,
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", revision):
        raise TargetLoadWorkflowError("revision must contain only letters, numbers, '_' or '-'")
    if isinstance(task_retry_limit, bool) or not isinstance(task_retry_limit, int) or task_retry_limit < 0:
        raise TargetLoadWorkflowError("task_retry_limit must be an integer >= 0")
    if isinstance(result_settle_seconds, bool) or not isinstance(result_settle_seconds, int) or result_settle_seconds < 1:
        raise TargetLoadWorkflowError("result_settle_seconds must be an integer >= 1")
    identity_tolerance = _finite(
        result_identity_relative_tolerance,
        "result_identity_relative_tolerance",
    )
    if not 0.0 < identity_tolerance <= 1.0e-6:
        raise TargetLoadWorkflowError("result_identity_relative_tolerance must be in (0, 1e-6]")
    normalized_hashes = _validate_source_hashes(source_hashes)
    normalized_upstream = validate_upstream_pareto_binding(upstream_pareto_binding)
    normalized_scheduler = _validate_scheduler_contract(scheduler_contract)
    if not math.isclose(
        policy_template.maximum_current_peak_a,
        spec.effective_peak_current_limit_a,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise TargetLoadWorkflowError(
            "policy maximum current must equal the spec effective current limit"
        )
    candidate_order, probe_seeds = _ordered_probe_seeds(base_plan_rows, spec, policy_template)
    if candidate_order != strict_candidate_order:
        raise TargetLoadWorkflowError("strict candidate order differs from probe seed order")
    if candidate_order != normalized_upstream["selected_candidate_ids"]:
        raise TargetLoadWorkflowError(
            "filtered seed-plan candidates differ from the upstream final-front selection"
        )
    identity = {
        "revision": revision,
        "source_hashes": normalized_hashes,
        "scheduler_contract": normalized_scheduler,
        "policy_template": policy_template.as_dict(),
        "task_retry_limit": task_retry_limit,
        "result_settle_seconds": result_settle_seconds,
        "result_identity_relative_tolerance": identity_tolerance,
        "candidate_order": candidate_order,
        "optimization_spec": spec_mapping,
        "operating_point_order": [point.name for point in spec.operating_points],
        "design_variable_names": list(spec.design_variable_names),
        "model_fingerprints": model_fingerprints,
        "model_artifact_hashes": model_artifact_hashes,
        "optimization_provenance": optimization_provenance,
        "upstream_pareto_binding": normalized_upstream,
        "source_documents_base64": source_documents_base64,
        "strict_input_validation": {
            "seed_plan": "validate_case_plan",
            "pareto_binding": "validate_pareto_front",
            "beta_manifest": "validated_zero_manifest",
            "surrogate_bundle": "load_surrogate_bundle",
            "exact_document_hashes_computed_internally": True,
            "embedded_source_documents_revalidated": True,
            "upstream_final_front": "completed_decision_and_strict_validation_v1",
        },
        "beta_validation_semantics": {
            "probe_family": TARGET_LOAD_CONTROL_SOURCE,
            "independent_current_per_beta_role": True,
            "fixed_current_mtpa_is_separate_evidence": True,
            "neighbor_step_deg": 2.0,
            "final_efficiency_basis": "required_mechanical_power_plus_matched_measured_loss",
        },
        "spec_limits": {
            "effective_peak_current_limit_a": spec.effective_peak_current_limit_a,
            "phase_peak_voltage_limit_v": spec.phase_peak_voltage_limit_v,
            "beta_bounds_deg": list(spec.beta_bounds_deg),
            "beta_calibration_id": spec.beta_calibration.calibration_id,
            "beta_convention": spec.beta_calibration.convention,
            "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
        },
        "probe_seeds": probe_seeds,
    }
    match_run_id = _namespaced_id("ipmsm-target-load-match", identity)
    probes: list[dict[str, Any]] = []
    for seed in probe_seeds:
        probe_payload = {
            "match_run_id": match_run_id,
            "candidate_id": seed["candidate_id"],
            "operating_point_id": seed["operating_point_id"],
            "beta_validation_role": seed["beta_validation_role"],
            "beta_dq_deg": canonical_float(seed["beta_dq_deg"]),
            "base_row_sha256": seed["base_row_sha256"],
            "policy_sha256": seed["policy_sha256"],
        }
        probes.append(
            {
                **seed,
                "probe_id": _namespaced_id("ipmsm-target-load-probe", probe_payload),
            }
        )
    return {
        "schema_version": ROOT_SCHEMA_VERSION,
        "status": "frozen_before_attempts",
        "match_run_id": match_run_id,
        "identity_sha256": canonical_json_sha256(identity),
        "identity": identity,
        "probes": probes,
    }


def _validate_embedded_upstream_authority(
    identity: Mapping[str, Any],
    upstream: Mapping[str, Any],
    documents: Mapping[str, bytes],
    spec: OptimizationSpec,
    expected_provenance: Mapping[str, str],
    filtered_candidate_order: Sequence[str],
) -> None:
    authority = {
        name: _decode_exact_bytes(encoded, f"root upstream authority {name}")
        for name, encoded in upstream["authority_documents_base64"].items()
    }
    model_artifacts = {
        name: _decode_exact_bytes(encoded, f"root upstream model artifact {name}")
        for name, encoded in upstream["model_artifacts_base64"].items()
    }
    source_records = upstream["source_artifacts"]
    validation_record = upstream["validation"]

    def require_hash(record: Mapping[str, Any], payload: bytes, label: str) -> None:
        if record.get("sha256") != _sha256_bytes(payload):
            raise TargetLoadWorkflowError(f"root upstream {label} hash is invalid")

    require_hash(
        upstream["optimization_decision"],
        authority["optimization_decision_json"],
        "optimization decision",
    )
    for name, payload in (
        ("optimization_spec", documents["optimization_spec_json"]),
        ("pareto", documents["pareto_csv"]),
        ("seed_fea_plan", authority["original_seed_fea_plan_csv"]),
        ("pareto_fea_results", authority["pareto_fea_results_csv"]),
        ("model_metadata", documents["model_metadata_json"]),
        ("beta_calibration_manifest", documents["beta_calibration_manifest_json"]),
    ):
        require_hash(source_records[name], payload, name)
    for name, payload in (
        ("summary", authority["validation_summary_json"]),
        ("rows", authority["validation_rows_csv"]),
        ("final_front", authority["final_front_csv"]),
    ):
        require_hash(validation_record[name], payload, f"validation {name}")

    metadata = _strict_json_object(documents["model_metadata_json"], "root surrogate metadata")
    indexed_models = _model_artifact_index(metadata)
    expected_basenames = {basename for _, _, basename in indexed_models}
    if set(model_artifacts) != expected_basenames:
        raise TargetLoadWorkflowError("root upstream embedded model artifact coverage is invalid")
    replayed_model_hashes = {
        f"{target}[{index}]::{basename}": _sha256_bytes(model_artifacts[basename])
        for target, index, basename in indexed_models
    }
    if identity.get("model_artifact_hashes") != replayed_model_hashes:
        raise TargetLoadWorkflowError("root embedded model artifact hashes are invalid")
    replayed_fingerprints = identity.get("model_fingerprints")
    replayed_manifest_sha = _optimizer_canonical_json_sha256(replayed_model_hashes)
    if source_records["model_artifacts_manifest_sha256"] != replayed_manifest_sha:
        raise TargetLoadWorkflowError("root upstream model artifact manifest hash is invalid")

    original_fields, original_rows = _strict_csv(
        authority["original_seed_fea_plan_csv"],
        "root original seed FEA plan",
    )
    pareto_fields, pareto_rows = _strict_csv(documents["pareto_csv"], "root Pareto front")
    try:
        original_candidate_order = pareto_validator.validate_case_plan(
            spec,
            original_fields,
            original_rows,
            expected_provenance,
        )
        pareto_validator.validate_pareto_front(
            spec,
            pareto_fields,
            pareto_rows,
            original_rows,
            original_candidate_order,
        )
    except pareto_validator.ParetoFEAValidationError as exc:
        raise TargetLoadWorkflowError(
            f"root original upstream optimization authority is invalid: {exc}"
        ) from exc
    if upstream["original_seed_candidate_ids"] != original_candidate_order:
        raise TargetLoadWorkflowError("root upstream original candidate order is invalid")

    decision = _producer_json_object(
        authority["optimization_decision_json"],
        "root optimization decision",
    )
    if (
        decision.get("schema_version") != "ipmsm_v2_optimization_continuation_v1"
        or decision.get("mode") != "execute"
        or decision.get("status") != "complete"
    ):
        raise TargetLoadWorkflowError("root embedded optimization decision is not complete")
    decision_binding = upstream["optimization_decision"]
    if decision.get("decision_output") != decision_binding["path"]:
        raise TargetLoadWorkflowError("root optimization decision path binding is invalid")
    contract = decision.get("execution_contract")
    if not isinstance(contract, Mapping):
        raise TargetLoadWorkflowError("root optimization execution contract is missing")
    contract_sha = _optimizer_canonical_json_sha256(contract)
    if (
        decision.get("contract_sha256") != contract_sha
        or decision_binding["contract_sha256"] != contract_sha
    ):
        raise TargetLoadWorkflowError("root optimization execution contract hash is invalid")
    producer_sources = {
        name: _decode_exact_bytes(encoded, f"root producer source {name}")
        for name, encoded in upstream["producer_sources_base64"].items()
    }
    source_contract = contract.get("source_sha256")
    if (
        set(producer_sources) != set(OPTIMIZATION_PRODUCER_SOURCE_FILES)
        or not isinstance(source_contract, Mapping)
        or set(source_contract) != set(OPTIMIZATION_PRODUCER_SOURCE_FILES)
    ):
        raise TargetLoadWorkflowError("root optimization producer source coverage is invalid")
    if any(
        source_contract[name] != _sha256_bytes(producer_sources[name])
        for name in OPTIMIZATION_PRODUCER_SOURCE_FILES
    ):
        raise TargetLoadWorkflowError("root optimization producer source hash is invalid")
    for name, payload in producer_sources.items():
        path = PROJECT_SOURCE_ROOT / name
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise TargetLoadWorkflowError(f"cannot replay producer source: {path}") from exc
        if payload != current:
            raise TargetLoadWorkflowError(f"root producer source differs from runtime: {name}")

    def bound_path(value: Any, record: Mapping[str, Any], label: str) -> None:
        if str(value or "") != record["path"]:
            raise TargetLoadWorkflowError(f"root {label} path binding is invalid")

    inputs = contract.get("inputs")
    optimization_contract = contract.get("optimization")
    fea_contract = contract.get("pareto_fea")
    validator_contract = contract.get("validation")
    if not all(
        isinstance(item, Mapping)
        for item in (inputs, optimization_contract, fea_contract, validator_contract)
    ):
        raise TargetLoadWorkflowError("root optimization decision contract sections are missing")
    beta_inputs = inputs.get("beta")
    model_contract = inputs.get("model_bundle")
    if not isinstance(beta_inputs, Mapping) or not isinstance(model_contract, Mapping):
        raise TargetLoadWorkflowError("root optimization input contract is incomplete")
    spec_input = inputs.get("optimization_spec")
    beta_manifest_input = beta_inputs.get("calibration_manifest")
    metadata_input = model_contract.get("metadata")
    if not all(
        isinstance(item, Mapping)
        for item in (spec_input, beta_manifest_input, metadata_input)
    ):
        raise TargetLoadWorkflowError("root optimization artifact input records are invalid")
    bound_path(spec_input.get("path"), source_records["optimization_spec"], "spec")
    if spec_input.get("sha256") != source_records["optimization_spec"]["sha256"]:
        raise TargetLoadWorkflowError("root optimization spec contract hash is invalid")
    bound_path(
        beta_manifest_input.get("path"),
        source_records["beta_calibration_manifest"],
        "beta manifest",
    )
    if beta_manifest_input.get("sha256") != source_records[
        "beta_calibration_manifest"
    ]["sha256"]:
        raise TargetLoadWorkflowError("root beta manifest contract hash is invalid")
    bound_path(metadata_input.get("path"), source_records["model_metadata"], "metadata")
    if metadata_input.get("sha256") != source_records["model_metadata"]["sha256"]:
        raise TargetLoadWorkflowError("root model metadata contract hash is invalid")
    recorded_models = model_contract.get("artifacts")
    if not isinstance(recorded_models, Mapping) or set(recorded_models) != set(replayed_model_hashes):
        raise TargetLoadWorkflowError("root decision model artifact coverage is invalid")
    for key, digest in replayed_model_hashes.items():
        record = recorded_models[key]
        if not isinstance(record, Mapping) or record.get("sha256") != digest:
            raise TargetLoadWorkflowError("root decision model artifact hash is invalid")
    if model_contract.get("fingerprints") != replayed_fingerprints:
        raise TargetLoadWorkflowError("root decision model fingerprints are invalid")
    selected_model = decision.get("selected_model")
    if not isinstance(selected_model, Mapping) or (
        selected_model.get("model_dir") != model_contract.get("model_dir")
        or selected_model.get("metadata_sha256") != source_records["model_metadata"]["sha256"]
        or selected_model.get("fingerprints") != replayed_fingerprints
    ):
        raise TargetLoadWorkflowError("root decision selected-model identity is invalid")

    artifacts = decision.get("optimization_artifacts")
    if not isinstance(artifacts, Mapping):
        raise TargetLoadWorkflowError("root decision optimization artifacts are missing")
    pareto_artifact = artifacts.get("pareto")
    plan_artifact = artifacts.get("fea_cases")
    if not isinstance(pareto_artifact, Mapping) or not isinstance(plan_artifact, Mapping):
        raise TargetLoadWorkflowError("root decision optimization artifact records are invalid")
    bound_path(pareto_artifact.get("path"), source_records["pareto"], "Pareto")
    bound_path(plan_artifact.get("path"), source_records["seed_fea_plan"], "seed plan")
    if (
        pareto_artifact.get("sha256") != source_records["pareto"]["sha256"]
        or plan_artifact.get("sha256") != source_records["seed_fea_plan"]["sha256"]
        or artifacts.get("provenance") != expected_provenance
        or artifacts.get("fea_candidate_ids") != original_candidate_order
        or artifacts.get("fea_case_rows") != len(original_rows)
    ):
        raise TargetLoadWorkflowError("root decision optimization artifact identity is invalid")
    if optimization_contract.get("pareto_output") != source_records["pareto"]["path"] or (
        optimization_contract.get("fea_cases_output") != source_records["seed_fea_plan"]["path"]
    ):
        raise TargetLoadWorkflowError("root optimization output contract path is invalid")
    maximum_candidates = optimization_contract.get("max_fea_candidates")
    if isinstance(maximum_candidates, bool) or not isinstance(maximum_candidates, int) or not (
        1 <= len(original_candidate_order) <= maximum_candidates
    ):
        raise TargetLoadWorkflowError("root optimization candidate bound is invalid")

    fea = decision.get("pareto_fea")
    if not isinstance(fea, Mapping):
        raise TargetLoadWorkflowError("root decision Pareto FEA evidence is missing")
    if (
        fea.get("results") != source_records["pareto_fea_results"]["path"]
        or fea.get("results_sha256") != source_records["pareto_fea_results"]["sha256"]
        or fea.get("case_rows") != len(original_rows)
        or fea_contract.get("results") != source_records["pareto_fea_results"]["path"]
    ):
        raise TargetLoadWorkflowError("root decision Pareto FEA result binding is invalid")

    expected_flags = [
        "--spec", "--model-dir", "--pareto", "--case-plan", "--results",
        "--summary-output", "--rows-output", "--final-front-output",
        "--minimum-coverage", "--identity-relative-tolerance",
    ]
    argv = validator_contract.get("argv")
    if not isinstance(argv, list) or len(argv) != 20 or argv[::2] != expected_flags:
        raise TargetLoadWorkflowError("root validator argv is invalid")
    expected_argv_paths = {
        "--spec": source_records["optimization_spec"]["path"],
        "--model-dir": model_contract.get("model_dir"),
        "--pareto": source_records["pareto"]["path"],
        "--case-plan": source_records["seed_fea_plan"]["path"],
        "--results": source_records["pareto_fea_results"]["path"],
        "--summary-output": validation_record["summary"]["path"],
        "--rows-output": validation_record["rows"]["path"],
        "--final-front-output": validation_record["final_front"]["path"],
    }
    argv_map = dict(zip(argv[::2], argv[1::2]))
    execution_cwd = Path(upstream["execution_cwd"])
    for flag, expected_path in expected_argv_paths.items():
        raw = argv_map.get(flag)
        if not isinstance(raw, str):
            raise TargetLoadWorkflowError("root validator argv path binding is invalid")
        recorded_path = Path(raw)
        resolved = (
            recorded_path.resolve(strict=False)
            if recorded_path.is_absolute()
            else (execution_cwd / recorded_path).resolve(strict=False)
        )
        if resolved != Path(str(expected_path)).resolve(strict=False):
            raise TargetLoadWorkflowError("root validator argv path binding is invalid")
    minimum_coverage = _finite(validator_contract.get("minimum_coverage"), "root minimum coverage")
    identity_tolerance = _finite(
        validator_contract.get("identity_relative_tolerance"),
        "root identity tolerance",
    )
    if (
        _finite(argv_map["--minimum-coverage"], "root argv minimum coverage") != minimum_coverage
        or _finite(argv_map["--identity-relative-tolerance"], "root argv identity tolerance")
        != identity_tolerance
    ):
        raise TargetLoadWorkflowError("root validator argv thresholds differ from contract")
    if (
        validator_contract.get("summary_output") != validation_record["summary"]["path"]
        or validator_contract.get("rows_output") != validation_record["rows"]["path"]
        or validator_contract.get("final_front_output") != validation_record["final_front"]["path"]
    ):
        raise TargetLoadWorkflowError("root validator output path contract is invalid")

    try:
        import ipmsm_target_load_coordinator as coordinator

        expected_summary, expected_summary_bytes, expected_rows_bytes, expected_front_bytes = (
            coordinator._recompute_strict_validation_from_bytes(
                spec_json=documents["optimization_spec_json"],
                metadata_json=documents["model_metadata_json"],
                model_artifacts=model_artifacts,
                pareto_csv=documents["pareto_csv"],
                seed_plan_csv=authority["original_seed_fea_plan_csv"],
                results_csv=authority["pareto_fea_results_csv"],
                minimum_coverage=minimum_coverage,
                identity_relative_tolerance=identity_tolerance,
            )
        )
    except (ValueError, OSError, pareto_validator.ParetoFEAValidationError) as exc:
        raise TargetLoadWorkflowError(f"root strict upstream validation replay failed: {exc}") from exc
    if (
        authority["validation_summary_json"] != expected_summary_bytes
        or authority["validation_rows_csv"] != expected_rows_bytes
        or authority["final_front_csv"] != expected_front_bytes
    ):
        raise TargetLoadWorkflowError("root upstream validation outputs differ from strict replay")
    if expected_summary.get("status") != "passed" or expected_summary.get("pass") is not True:
        raise TargetLoadWorkflowError("root replayed upstream validation did not pass")
    final_ids = expected_summary["fea_filtered_final_front_candidate_ids"]
    selected_ids = [candidate_id for candidate_id in original_candidate_order if candidate_id in set(final_ids)]
    if (
        upstream["fea_filtered_final_front_candidate_ids"] != final_ids
        or upstream["selected_candidate_ids"] != selected_ids
        or list(filtered_candidate_order) != selected_ids
        or upstream["optimization_run_id"] != expected_provenance[nsga2.OPTIMIZATION_RUN_ID_FIELD]
    ):
        raise TargetLoadWorkflowError("root upstream final-front selection identity is invalid")
    validation = decision.get("validation")
    if not isinstance(validation, Mapping):
        raise TargetLoadWorkflowError("root decision validation evidence is missing")
    recorded_front = validation.get("final_front")
    if not isinstance(recorded_front, Mapping) or (
        validation.get("summary") != validation_record["summary"]
        or validation.get("rows") != validation_record["rows"]
        or recorded_front.get("path") != validation_record["final_front"]["path"]
        or recorded_front.get("sha256") != validation_record["final_front"]["sha256"]
        or recorded_front.get("candidate_ids") != final_ids
        or recorded_front.get("candidate_count") != len(final_ids)
        or validation.get("validation_id") != expected_summary["validation_id"]
        or validation.get("feasible_candidate_count") != expected_summary["feasible_candidate_count"]
        or validation.get("gate_failures") != []
        or validation.get("pass") is not True
    ):
        raise TargetLoadWorkflowError("root decision validation binding is invalid")


def _validate_embedded_root_documents(
    identity: Mapping[str, Any],
    spec: OptimizationSpec,
    policy_template: MatchPolicyTemplate,
) -> tuple[list[str], list[dict[str, Any]]]:
    encoded = identity.get("source_documents_base64")
    if not isinstance(encoded, Mapping) or set(encoded) != set(ROOT_SOURCE_DOCUMENT_FIELDS):
        raise TargetLoadWorkflowError("root exact source-document coverage is invalid")
    documents = {
        field: _decode_exact_bytes(encoded[field], f"root {field}")
        for field in ROOT_SOURCE_DOCUMENT_FIELDS
    }
    _validate_runtime_source_documents(documents)
    source_hashes = identity.get("source_hashes")
    artifact_hashes_raw = identity.get("model_artifact_hashes")
    if not isinstance(source_hashes, Mapping) or not isinstance(artifact_hashes_raw, Mapping):
        raise TargetLoadWorkflowError("root source/model artifact hashes are missing")
    artifact_hashes = {
        _text(key, "model artifact identity key"): _validate_sha256(
            value,
            f"model artifact {key}",
        )
        for key, value in artifact_hashes_raw.items()
    }
    expected_source_hashes = {
        "optimization_spec_sha256": _sha256_bytes(documents["optimization_spec_json"]),
        "pareto_sha256": _sha256_bytes(documents["pareto_csv"]),
        "seed_fea_plan_sha256": _sha256_bytes(documents["seed_fea_plan_csv"]),
        "model_metadata_sha256": _sha256_bytes(documents["model_metadata_json"]),
        "model_artifact_manifest_sha256": _optimizer_canonical_json_sha256(artifact_hashes),
        "beta_calibration_artifact_sha256": _sha256_bytes(
            documents["beta_calibration_manifest_json"]
        ),
        "matcher_source_sha256": _sha256_bytes(documents["matcher_source"]),
        "workflow_source_sha256": _sha256_bytes(documents["workflow_source"]),
        "coordinator_source_sha256": _sha256_bytes(documents["coordinator_source"]),
        "atomic_publish_source_sha256": _sha256_bytes(documents["atomic_publish_source"]),
        "validator_source_sha256": _sha256_bytes(documents["validator_source"]),
        "submit_ipmsm_v2_campaign_source_sha256": _sha256_bytes(
            documents["submit_ipmsm_v2_campaign_source"]
        ),
        "submit_ipmsm_scheduler_task_source_sha256": _sha256_bytes(
            documents["submit_ipmsm_scheduler_task_source"]
        ),
        "submit_ipmsm_scheduler_job_source_sha256": _sha256_bytes(
            documents["submit_ipmsm_scheduler_job_source"]
        ),
        "subprocess_run_source_sha256": _sha256_bytes(documents["subprocess_run_source"]),
        "run_ipmsm_batch_source_sha256": _sha256_bytes(documents["run_ipmsm_batch_source"]),
        "ipmsm_ppt_setup_source_sha256": _sha256_bytes(documents["ipmsm_ppt_setup_source"]),
        "ipmsm_geometry_source_sha256": _sha256_bytes(documents["ipmsm_geometry_source"]),
        "variable_source_sha256": _sha256_bytes(documents["variable_source"]),
        "pyaedt_core_source_sha256": _sha256_bytes(documents["pyaedt_core_source"]),
    }
    if dict(source_hashes) != expected_source_hashes:
        raise TargetLoadWorkflowError("root source hashes differ from embedded exact documents")

    spec_mapping = _strict_json_object(documents["optimization_spec_json"], "root optimization spec")
    if spec_mapping != identity.get("optimization_spec"):
        raise TargetLoadWorkflowError("root optimization spec differs from its exact document")
    metadata = _strict_json_object(documents["model_metadata_json"], "root surrogate metadata")
    expected_fingerprints = _validated_model_fingerprints(metadata, spec)
    if identity.get("model_fingerprints") != expected_fingerprints:
        raise TargetLoadWorkflowError("root model fingerprints differ from exact metadata")
    expected_artifact_keys = {
        f"{target}[{index}]::{basename}"
        for target, index, basename in _model_artifact_index(metadata)
    }
    if set(artifact_hashes) != expected_artifact_keys:
        raise TargetLoadWorkflowError("root model artifact identity differs from exact metadata")
    upstream = validate_upstream_pareto_binding(identity.get("upstream_pareto_binding"))
    if identity.get("upstream_pareto_binding") != upstream:
        raise TargetLoadWorkflowError("root upstream Pareto binding is not normalized")
    early_model_artifacts = {
        name: _decode_exact_bytes(encoded, f"root upstream model artifact {name}")
        for name, encoded in upstream["model_artifacts_base64"].items()
    }
    early_fingerprints = _validated_surrogate_bundle_documents(
        documents["model_metadata_json"],
        early_model_artifacts,
    )
    if early_fingerprints != expected_fingerprints:
        raise TargetLoadWorkflowError("root embedded surrogate bundle fingerprints are invalid")

    calibration = _strict_json_object(
        documents["beta_calibration_manifest_json"],
        "root beta calibration manifest",
    )
    try:
        zero_deg, calibration_id = validated_zero_manifest(calibration)
    except ValueError as exc:
        raise TargetLoadWorkflowError(str(exc)) from exc
    if (
        calibration_id != spec.beta_calibration.calibration_id
        or not math.isclose(
            zero_deg,
            spec.beta_calibration.electrical_zero_deg,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        )
        or calibration.get("convention") != spec.beta_calibration.convention
    ):
        raise TargetLoadWorkflowError("root beta calibration differs from the optimization spec")

    plan_fields, plan_rows = _strict_csv(documents["seed_fea_plan_csv"], "root seed FEA plan")
    pareto_fields, pareto_rows = _strict_csv(documents["pareto_csv"], "root Pareto front")
    provenance_context = {
        nsga2.OPTIMIZATION_SPEC_SHA256_FIELD: expected_source_hashes[
            "optimization_spec_sha256"
        ],
        nsga2.SURROGATE_METADATA_SHA256_FIELD: expected_source_hashes[
            "model_metadata_sha256"
        ],
        nsga2.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: expected_source_hashes[
            "model_artifact_manifest_sha256"
        ],
        nsga2.SURROGATE_VERIFICATION_FIELD: nsga2.STRICT_BUNDLE_VERIFICATION,
    }
    expected_provenance = nsga2.build_optimization_run_provenance(
        documents["pareto_csv"],
        provenance_context,
    )
    if identity.get("optimization_provenance") != expected_provenance:
        raise TargetLoadWorkflowError("root optimization provenance differs from exact documents")
    try:
        strict_candidate_order = pareto_validator.validate_case_plan(
            spec,
            plan_fields,
            plan_rows,
            expected_provenance,
        )
        pareto_validator.validate_pareto_front(
            spec,
            pareto_fields,
            pareto_rows,
            plan_rows,
            strict_candidate_order,
        )
    except pareto_validator.ParetoFEAValidationError as exc:
        raise TargetLoadWorkflowError(f"root strict optimization validation failed: {exc}") from exc
    candidate_order, probe_seeds = _ordered_probe_seeds(plan_rows, spec, policy_template)
    if candidate_order != strict_candidate_order:
        raise TargetLoadWorkflowError("root candidate order differs from strict validation")
    if identity.get("candidate_order") != candidate_order:
        raise TargetLoadWorkflowError("root candidate order differs from exact seed documents")
    if identity.get("probe_seeds") != probe_seeds:
        raise TargetLoadWorkflowError("root probe seeds differ from exact seed documents")
    upstream_sources = upstream["source_artifacts"]
    for upstream_name, root_name in {
        "optimization_spec": "optimization_spec_sha256",
        "pareto": "pareto_sha256",
        "model_metadata": "model_metadata_sha256",
        "beta_calibration_manifest": "beta_calibration_artifact_sha256",
    }.items():
        if upstream_sources[upstream_name]["sha256"] != expected_source_hashes[root_name]:
            raise TargetLoadWorkflowError(
                f"root upstream {upstream_name} hash differs from its exact source document"
            )
    if upstream_sources["model_artifacts_manifest_sha256"] != expected_source_hashes[
        "model_artifact_manifest_sha256"
    ]:
        raise TargetLoadWorkflowError(
            "root upstream model artifact hash differs from exact model artifacts"
        )
    if upstream["optimization_run_id"] != expected_provenance[nsga2.OPTIMIZATION_RUN_ID_FIELD]:
        raise TargetLoadWorkflowError(
            "root upstream optimization_run_id differs from exact optimization provenance"
        )
    if candidate_order != upstream["selected_candidate_ids"]:
        raise TargetLoadWorkflowError(
            "root candidates differ from the upstream final-front selection"
        )
    _validate_embedded_upstream_authority(
        identity,
        upstream,
        documents,
        spec,
        expected_provenance,
        candidate_order,
    )
    return candidate_order, probe_seeds


def _validate_root_manifest_uncached(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != ROOT_SCHEMA_VERSION:
        raise TargetLoadWorkflowError("root manifest schema is invalid")
    if manifest.get("status") != "frozen_before_attempts":
        raise TargetLoadWorkflowError("root manifest status is invalid")
    identity = manifest.get("identity")
    if not isinstance(identity, Mapping):
        raise TargetLoadWorkflowError("root manifest identity is missing")
    identity_sha256 = canonical_json_sha256(identity)
    if manifest.get("identity_sha256") != identity_sha256:
        raise TargetLoadWorkflowError("root manifest identity SHA256 is invalid")
    expected_run_id = _namespaced_id("ipmsm-target-load-match", identity)
    if manifest.get("match_run_id") != expected_run_id:
        raise TargetLoadWorkflowError("root manifest match_run_id is invalid")
    source_hashes = identity.get("source_hashes")
    scheduler_contract = identity.get("scheduler_contract")
    policy_mapping = identity.get("policy_template")
    limits = identity.get("spec_limits")
    fingerprints = identity.get("model_fingerprints")
    spec_mapping = identity.get("optimization_spec")
    if not isinstance(source_hashes, Mapping):
        raise TargetLoadWorkflowError("root source hashes are missing")
    if not isinstance(scheduler_contract, Mapping):
        raise TargetLoadWorkflowError("root scheduler contract is missing")
    if (
        not isinstance(policy_mapping, Mapping)
        or not isinstance(limits, Mapping)
        or not isinstance(spec_mapping, Mapping)
    ):
        raise TargetLoadWorkflowError("root policy/spec limits are missing")
    if not isinstance(fingerprints, Mapping) or set(fingerprints) != set(
        pareto_validator.MODEL_FINGERPRINT_COLUMNS
    ):
        raise TargetLoadWorkflowError("root model fingerprints are incomplete")
    _validate_source_hashes(source_hashes)
    upstream = validate_upstream_pareto_binding(identity.get("upstream_pareto_binding"))
    if identity.get("upstream_pareto_binding") != upstream:
        raise TargetLoadWorkflowError("root upstream Pareto binding is not normalized")
    _validate_scheduler_contract(scheduler_contract)
    try:
        policy_template = MatchPolicyTemplate(**dict(policy_mapping))
        spec = optimization_spec_from_mapping(spec_mapping)
    except (TypeError, ValueError, OptimizationSpecError) as exc:
        raise TargetLoadWorkflowError("root policy template is invalid") from exc
    revision = _text(identity.get("revision"), "root revision")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", revision):
        raise TargetLoadWorkflowError("root revision is invalid")
    retry_limit = identity.get("task_retry_limit")
    settle_seconds = identity.get("result_settle_seconds")
    if isinstance(retry_limit, bool) or not isinstance(retry_limit, int) or retry_limit < 0:
        raise TargetLoadWorkflowError("root task_retry_limit is invalid")
    if isinstance(settle_seconds, bool) or not isinstance(settle_seconds, int) or settle_seconds < 1:
        raise TargetLoadWorkflowError("root result_settle_seconds is invalid")
    effective_limit = _finite(limits.get("effective_peak_current_limit_a"), "root current limit")
    if not math.isclose(
        policy_template.maximum_current_peak_a,
        effective_limit,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise TargetLoadWorkflowError("root policy does not cover the full legal current range")
    expected_limits = {
        "effective_peak_current_limit_a": spec.effective_peak_current_limit_a,
        "phase_peak_voltage_limit_v": spec.phase_peak_voltage_limit_v,
        "beta_bounds_deg": list(spec.beta_bounds_deg),
        "beta_calibration_id": spec.beta_calibration.calibration_id,
        "beta_convention": spec.beta_calibration.convention,
        "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
    }
    if dict(limits) != expected_limits:
        raise TargetLoadWorkflowError("root spec limits differ from the frozen optimization spec")
    if identity.get("design_variable_names") != list(spec.design_variable_names):
        raise TargetLoadWorkflowError("root design variable names differ from the optimization spec")
    if identity.get("operating_point_order") != [point.name for point in spec.operating_points]:
        raise TargetLoadWorkflowError("root operating-point order differs from the optimization spec")
    if identity.get("beta_validation_semantics") != {
        "probe_family": TARGET_LOAD_CONTROL_SOURCE,
        "independent_current_per_beta_role": True,
        "fixed_current_mtpa_is_separate_evidence": True,
        "neighbor_step_deg": 2.0,
        "final_efficiency_basis": "required_mechanical_power_plus_matched_measured_loss",
    }:
        raise TargetLoadWorkflowError("root beta validation semantics are invalid")
    _validate_embedded_root_documents(identity, spec, policy_template)
    for column, expected in {
        "input_dataset_schema_version": nsga2.FEA_DATASET_SCHEMA_VERSION,
        "input_quality_profile": nsga2.REFERENCE_FEA_QUALITY_PROFILE,
        "input_beta_calibration_id": spec.beta_calibration.calibration_id,
        "input_beta_convention": nsga2.BETA_CONVENTION,
        "input_model_extent": nsga2.FEA_MODEL_EXTENT,
    }.items():
        if fingerprints.get(column) != expected:
            raise TargetLoadWorkflowError(f"root model fingerprint {column} is invalid")
    identity_tolerance = _finite(
        identity.get("result_identity_relative_tolerance"),
        "root result identity tolerance",
    )
    if not 0.0 < identity_tolerance <= 1.0e-6:
        raise TargetLoadWorkflowError("root result identity tolerance exceeds 1e-6")
    strict_validation = identity.get("strict_input_validation")
    if strict_validation != {
        "seed_plan": "validate_case_plan",
        "pareto_binding": "validate_pareto_front",
        "beta_manifest": "validated_zero_manifest",
        "surrogate_bundle": "load_surrogate_bundle",
        "exact_document_hashes_computed_internally": True,
        "embedded_source_documents_revalidated": True,
        "upstream_final_front": "completed_decision_and_strict_validation_v1",
    }:
        raise TargetLoadWorkflowError("root strict input validation receipt is invalid")
    probes = manifest.get("probes")
    seeds = identity.get("probe_seeds")
    if not isinstance(probes, list) or not isinstance(seeds, list) or len(probes) != len(seeds):
        raise TargetLoadWorkflowError("root manifest probe coverage is invalid")
    seen: set[str] = set()
    for probe, seed in zip(probes, seeds):
        if not isinstance(probe, Mapping) or not isinstance(seed, Mapping):
            raise TargetLoadWorkflowError("root manifest probe entry is invalid")
        if {key: probe.get(key) for key in seed} != dict(seed):
            raise TargetLoadWorkflowError("root manifest probe differs from its frozen seed")
        base_row = seed.get("base_row")
        policy = seed.get("policy")
        if not isinstance(base_row, Mapping) or canonical_json_sha256(base_row) != seed.get(
            "base_row_sha256"
        ):
            raise TargetLoadWorkflowError("root probe base row SHA256 is invalid")
        if not isinstance(policy, Mapping) or canonical_json_sha256(policy) != seed.get(
            "policy_sha256"
        ):
            raise TargetLoadWorkflowError("root probe policy SHA256 is invalid")
        payload = {
            "match_run_id": expected_run_id,
            "candidate_id": seed["candidate_id"],
            "operating_point_id": seed["operating_point_id"],
            "beta_validation_role": seed["beta_validation_role"],
            "beta_dq_deg": canonical_float(seed["beta_dq_deg"]),
            "base_row_sha256": seed["base_row_sha256"],
            "policy_sha256": seed["policy_sha256"],
        }
        expected_probe_id = _namespaced_id("ipmsm-target-load-probe", payload)
        if probe.get("probe_id") != expected_probe_id or expected_probe_id in seen:
            raise TargetLoadWorkflowError("root manifest probe_id is invalid or duplicate")
        _policy_from_probe(probe)
        seen.add(expected_probe_id)


_ROOT_VALIDATION_CACHE: dict[tuple[str, str], None] = {}
_ROOT_VALIDATION_CACHE_LIMIT = 8


def _root_validation_runtime_sha256() -> str:
    digest = hashlib.sha256()
    paths = {
        *(path.resolve() for path in RUNTIME_SOURCE_PATHS.values()),
        *(PROJECT_SOURCE_ROOT / name for name in OPTIMIZATION_PRODUCER_SOURCE_FILES),
    }
    for path in sorted(paths, key=lambda item: str(item)):
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise TargetLoadWorkflowError(f"cannot hash root-validation runtime source: {path}") from exc
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def validate_root_manifest(manifest: Mapping[str, Any]) -> None:
    key = (canonical_json_sha256(manifest), _root_validation_runtime_sha256())
    if key in _ROOT_VALIDATION_CACHE:
        return
    _validate_root_manifest_uncached(manifest)
    if len(_ROOT_VALIDATION_CACHE) >= _ROOT_VALIDATION_CACHE_LIMIT:
        _ROOT_VALIDATION_CACHE.pop(next(iter(_ROOT_VALIDATION_CACHE)))
    _ROOT_VALIDATION_CACHE[key] = None


def _find_probe(manifest: Mapping[str, Any], probe_id: str) -> Mapping[str, Any]:
    matches = [probe for probe in manifest["probes"] if probe["probe_id"] == probe_id]
    if len(matches) != 1:
        raise TargetLoadWorkflowError("probe_id is absent or ambiguous")
    return matches[0]


def _build_attempt(
    manifest: Mapping[str, Any],
    probe: Mapping[str, Any],
    normalized_history: Sequence[Mapping[str, Any]],
    decision: TargetLoadDecision,
) -> dict[str, Any]:
    if decision.status != "propose" or decision.proposed_current_peak_a is None:
        raise TargetLoadWorkflowError("cannot build an attempt from a terminal planner decision")
    probe_id = _text(probe.get("probe_id"), "probe_id")
    attempt_index = len(normalized_history) + 1
    policy_sha256 = _validate_sha256(probe.get("policy_sha256"), "policy_sha256")
    current = float(decision.proposed_current_peak_a)
    attempt_id = attempt_id_for(probe_id, attempt_index, current, policy_sha256)
    case_id = _attempt_case_id(
        str(manifest["match_run_id"]),
        probe,
        attempt_index,
        attempt_id,
    )
    history_sha256 = canonical_json_sha256(list(normalized_history))
    plan_row = dict(probe["base_row"])
    for field in SURROGATE_SELECTION_FIELDS:
        plan_row[f"seed_selection_{field}"] = plan_row.get(field)
        plan_row[field] = ""
    plan_row.update(
        {
            "case_id": case_id,
            "repeat_of_case_id": "",
            "i_peak_a": current,
            "control_source": TARGET_LOAD_CONTROL_SOURCE,
            "match_run_id": manifest["match_run_id"],
            "probe_id": probe_id,
            "attempt_id": attempt_id,
            "attempt_index": attempt_index,
            "canonical_seed_case_id": probe["base_case_id"],
            "target_kind": probe["target"]["target_kind"],
            "target_value": probe["target"]["target_value"],
            "target_relative_tolerance": _policy_from_probe(probe).relative_tolerance,
            "surrogate_prediction_status": ATTEMPT_SURROGATE_STATUS,
        }
    )
    decision_payload = _decision_dict(decision)
    return {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "match_run_id": manifest["match_run_id"],
        "probe_id": probe_id,
        "attempt_id": attempt_id,
        "attempt_index": attempt_index,
        "case_id": case_id,
        "dedupe_key": f"ipmsm-target-load:{attempt_id.rsplit(':', 1)[-1]}",
        "current_peak_a": current,
        "policy_sha256": policy_sha256,
        "history_sha256": history_sha256,
        "planner_decision": decision_payload,
        "plan_row_sha256": canonical_json_sha256(plan_row),
        "plan_row": plan_row,
    }


def _constraint_aware_match_decision(
    policy: TargetLoadPolicy,
    normalized_history: Sequence[Mapping[str, Any]],
    physics_history: Sequence[LoadObservation],
) -> TargetLoadDecision:
    decision = plan_target_load_match(policy, tuple(physics_history))
    if decision.status != "matched":
        return decision
    feasible_in_band = [
        entry
        for entry in normalized_history
        if entry["hard_constraints_passed"]
        and policy.lower_output <= entry["output_value"] <= policy.upper_output
    ]
    if feasible_in_band or len(normalized_history) >= policy.max_attempts:
        return decision
    failed_in_band = [
        entry
        for entry in normalized_history
        if not entry["hard_constraints_passed"]
        and policy.lower_output <= entry["output_value"] <= policy.upper_output
    ]
    if not failed_in_band:
        return decision
    anchor = min(failed_in_band, key=lambda entry: entry["current_peak_a"])
    anchor_current = float(anchor["current_peak_a"])
    anchor_output = float(anchor["output_value"])
    epsilon = max(1.0e-9, policy.maximum_current_peak_a * 1.0e-9)
    minimum_step = max(epsilon * 2.0, anchor_current * policy.minimum_step_relative)
    lower_bound = max(
        policy.minimum_current_peak_a,
        anchor_current / policy.maximum_scale_per_attempt,
    )
    upper_bound = min(policy.maximum_current_peak_a, anchor_current - minimum_step)
    if upper_bound < lower_bound - epsilon or anchor_output <= 0.0:
        return decision
    desired = anchor_current * policy.lower_output / anchor_output
    desired = min(upper_bound, max(lower_bound, desired))
    existing_currents = sorted(float(item.current_peak_a) for item in physics_history)
    sampled = sorted({lower_bound, upper_bound, *existing_currents})
    candidates = [desired, lower_bound, upper_bound]
    candidates.extend(
        (left + right) / 2.0
        for left, right in zip(sampled, sampled[1:])
        if right - left > 2.0 * epsilon
    )
    legal = [
        value
        for value in candidates
        if lower_bound - epsilon <= value <= upper_bound + epsilon
        and all(abs(value - current) > epsilon for current in existing_currents)
    ]
    if not legal:
        return decision
    proposed = min(legal, key=lambda value: (abs(value - desired), -value))
    return TargetLoadDecision(
        status="propose",
        proposed_current_peak_a=proposed,
        matched_observation=None,
        relative_error=decision.relative_error,
        attempts_used=len(normalized_history),
        reason="hard_constraint_refinement_toward_lower_target_edge",
        bracketed=decision.bracketed,
    )


def _normalized_history(
    manifest: Mapping[str, Any],
    probe: Mapping[str, Any],
    observations: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[LoadObservation, ...]]:
    normalized: list[dict[str, Any]] = []
    physics: list[LoadObservation] = []
    probe_id = _text(probe.get("probe_id"), "probe_id")
    policy = _policy_from_probe(probe)
    for expected_index, raw in enumerate(observations, start=1):
        prefix_decision = _constraint_aware_match_decision(policy, normalized, physics)
        if prefix_decision.status != "propose":
            raise TargetLoadWorkflowError("observation exists after a terminal planner decision")
        expected_attempt = _build_attempt(manifest, probe, normalized, prefix_decision)
        result_csv = _decode_exact_bytes(
            raw.get("result_csv_base64"),
            f"observation {expected_index} result CSV",
        )
        entry = _observation_from_validated_attempt(manifest, expected_attempt, result_csv)
        if dict(raw) != entry:
            raise TargetLoadWorkflowError(
                "observation differs from the exact FEA result and issued attempt"
            )
        normalized.append(entry)
        physics.append(LoadObservation(entry["current_peak_a"], entry["output_value"]))
    return normalized, tuple(physics)


def attempt_id_for(
    probe_id: str,
    attempt_index: int,
    current_peak_a: float,
    policy_sha256: str,
) -> str:
    if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) or attempt_index < 1:
        raise TargetLoadWorkflowError("attempt_index must be an integer >= 1")
    return _namespaced_id(
        "ipmsm-target-load-attempt",
        {
            "probe_id": _text(probe_id, "probe_id"),
            "attempt_index": attempt_index,
            "current_peak_a": canonical_float(current_peak_a),
            "policy_sha256": _validate_sha256(policy_sha256, "policy_sha256"),
        },
    )


def _attempt_case_id(
    match_run_id: str,
    probe: Mapping[str, Any],
    attempt_index: int,
    attempt_id: str,
) -> str:
    run_digest = match_run_id.rsplit(":", 1)[-1][:10]
    attempt_digest = attempt_id.rsplit(":", 1)[-1][:12]
    return "__".join(
        (
            "tlm4",
            run_digest,
            _safe_component(probe.get("candidate_id")),
            _safe_component(probe.get("operating_point_id")),
            _safe_component(probe.get("beta_validation_role")),
            f"a{attempt_index:02d}",
            attempt_digest,
        )
    )


def _decision_dict(decision: TargetLoadDecision) -> dict[str, Any]:
    return {
        "status": decision.status,
        "proposed_current_peak_a": decision.proposed_current_peak_a,
        "relative_error": decision.relative_error,
        "attempts_used": decision.attempts_used,
        "reason": decision.reason,
        "bracketed": decision.bracketed,
        "matched_current_peak_a": (
            decision.matched_observation.current_peak_a
            if decision.matched_observation is not None
            else None
        ),
        "matched_output_value": (
            decision.matched_observation.output_value
            if decision.matched_observation is not None
            else None
        ),
    }


def plan_probe_attempt(
    manifest: Mapping[str, Any],
    probe_id: str,
    observations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    validate_root_manifest(manifest)
    probe = _find_probe(manifest, probe_id)
    normalized_history, physics_history = _normalized_history(manifest, probe, observations)
    policy = _policy_from_probe(probe)
    decision = _constraint_aware_match_decision(policy, normalized_history, physics_history)
    decision_payload = _decision_dict(decision)
    history_sha256 = canonical_json_sha256(normalized_history)
    response: dict[str, Any] = {
        "probe_id": probe_id,
        "history_sha256": history_sha256,
        "history_count": len(normalized_history),
        "decision": decision_payload,
    }
    if decision.status != "propose":
        if decision.status == "matched":
            feasible_matches = [
                entry
                for entry in normalized_history
                if entry["hard_constraints_passed"]
                and policy.lower_output <= entry["output_value"] <= policy.upper_output
            ]
            if not feasible_matches:
                response["terminal_status"] = "constraint_failed"
                return response
            matched = min(
                feasible_matches,
                key=lambda entry: (entry["relative_error"], entry["current_peak_a"]),
            )
            response["decision"] = {
                **decision_payload,
                "matched_current_peak_a": matched["current_peak_a"],
                "matched_output_value": matched["output_value"],
                "relative_error": matched["relative_error"],
            }
            response["matched_observation"] = matched
        response["terminal_status"] = decision.status
        return response
    response["attempt"] = _build_attempt(manifest, probe, normalized_history, decision)
    return response


def validate_attempt_manifest(
    manifest: Mapping[str, Any],
    attempt: Mapping[str, Any],
    prior_observations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Reconstruct the only legal next attempt from the frozen root and history."""

    validate_root_manifest(manifest)
    probe_id = _text(attempt.get("probe_id"), "attempt probe_id")
    planned = plan_probe_attempt(manifest, probe_id, prior_observations)
    expected = planned.get("attempt")
    if not isinstance(expected, Mapping):
        raise TargetLoadWorkflowError("attempt was issued after a terminal planner decision")
    if dict(attempt) != dict(expected):
        raise TargetLoadWorkflowError("attempt differs from the frozen planner reconstruction")
    return dict(expected)


def _close_identity(actual: float, expected: float, relative_tolerance: float) -> bool:
    return math.isclose(
        actual,
        expected,
        rel_tol=relative_tolerance,
        abs_tol=max(1.0e-12, relative_tolerance * max(abs(expected), 1.0)),
    )


def _validated_result_metrics(
    manifest: Mapping[str, Any],
    plan_row: Mapping[str, Any],
    result_csv: bytes,
    *,
    label: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    payload = _exact_bytes(result_csv, label)
    result_fields, result_rows = _strict_csv(payload, label)
    if len(result_rows) != 1:
        raise TargetLoadWorkflowError(f"{label} must contain exactly one row")
    result_row = result_rows[0]
    spec_mapping = manifest["identity"].get("optimization_spec")
    fingerprints = manifest["identity"].get("model_fingerprints")
    if not isinstance(spec_mapping, Mapping) or not isinstance(fingerprints, Mapping):
        raise TargetLoadWorkflowError("root spec/fingerprints are missing")
    try:
        spec = optimization_spec_from_mapping(spec_mapping)
        pareto_validator.validate_result_contract(
            spec,
            [dict(plan_row)],
            result_fields,
            result_rows,
            {str(key): str(value) for key, value in fingerprints.items()},
        )
    except (OptimizationSpecError, pareto_validator.ParetoFEAValidationError) as exc:
        raise TargetLoadWorkflowError(f"{label} contract failed: {exc}") from exc
    identity_tolerance = _finite(
        manifest["identity"].get("result_identity_relative_tolerance"),
        "result identity tolerance",
    )

    current_peak = _nonnegative(result_row.get("input_i_peak_a"), "result current")
    phase_rms = _nonnegative(
        result_row.get("output_phase_current_last_rms_a"),
        "phase RMS current",
    )
    expected_phase_rms = current_peak / math.sqrt(2.0)
    if not _close_identity(phase_rms, expected_phase_rms, identity_tolerance):
        raise TargetLoadWorkflowError("result violates phase-current RMS identity")
    resistance = _nonnegative(
        result_row.get("input_phase_resistance_ohm"),
        "phase resistance",
    )
    copper_loss = _nonnegative(
        result_row.get("output_copperloss_last_avg_w"),
        "reported copper loss",
    )
    expected_copper_loss = 3.0 * resistance * phase_rms * phase_rms
    if not _close_identity(copper_loss, expected_copper_loss, identity_tolerance):
        raise TargetLoadWorkflowError("result violates copper-loss identity")
    core_loss = _nonnegative(result_row.get("output_coreloss_last_avg_w"), "core loss")
    solid_loss = _nonnegative(result_row.get("output_solidloss_last_avg_w"), "solid loss")
    total_loss = core_loss + solid_loss + expected_copper_loss
    reported_total_loss = _nonnegative(
        result_row.get("output_total_loss_last_avg_w"),
        "reported total loss",
    )
    if not _close_identity(reported_total_loss, total_loss, identity_tolerance):
        raise TargetLoadWorkflowError("result violates total-loss identity")
    torque_min = _finite(result_row.get("output_torque_last_min_nm"), "minimum torque")
    torque = _finite(result_row.get("output_torque_last_avg_nm"), "average torque")
    torque_max = _finite(result_row.get("output_torque_last_max_nm"), "maximum torque")
    if torque_min > torque or torque > torque_max:
        raise TargetLoadWorkflowError("result violates torque min <= average <= max")
    rpm = _finite(result_row.get("input_base_rpm"), "result speed")
    actual_power = torque * 2.0 * math.pi * rpm / 60.0
    if torque <= 0.0 or actual_power <= 0.0:
        raise TargetLoadWorkflowError("result must produce positive torque and mechanical power")
    efficiency = actual_power / (actual_power + total_loss) * 100.0
    reported_efficiency = _finite(
        result_row.get("output_efficiency_last_pct"),
        "reported efficiency",
    )
    if not 0.0 <= reported_efficiency <= 100.0:
        raise TargetLoadWorkflowError("result efficiency is outside [0, 100]")
    if not _close_identity(reported_efficiency, efficiency, identity_tolerance):
        raise TargetLoadWorkflowError("result violates efficiency identity")
    phase_voltages = [
        _nonnegative(
            result_row.get(f"output_phase{phase}_voltage_last_peak_abs_v"),
            f"phase {phase} voltage",
        )
        for phase in ("a", "b", "c")
    ]
    voltage = _nonnegative(
        result_row.get("output_phase_voltage_last_peak_abs_v"),
        "phase voltage envelope",
    )
    if not _close_identity(voltage, max(phase_voltages), identity_tolerance):
        raise TargetLoadWorkflowError("result violates phase-voltage envelope identity")

    limits = manifest["identity"]["spec_limits"]
    hard_constraints_passed = bool(
        current_peak <= _finite(limits["effective_peak_current_limit_a"], "current limit")
        + 1.0e-12
        and voltage <= _finite(limits["phase_peak_voltage_limit_v"], "voltage limit")
        + 1.0e-12
    )
    return result_row, {
        "current_peak_a": current_peak,
        "phase_rms_current_a": phase_rms,
        "copper_loss_w": expected_copper_loss,
        "core_loss_w": core_loss,
        "solid_loss_w": solid_loss,
        "total_loss_w": total_loss,
        "torque_nm": torque,
        "speed_rpm": rpm,
        "actual_power_w": actual_power,
        "actual_efficiency_pct": efficiency,
        "voltage_peak_v": voltage,
        "hard_constraints_passed": hard_constraints_passed,
    }


def _observation_from_validated_attempt(
    manifest: Mapping[str, Any],
    expected_attempt: Mapping[str, Any],
    result_csv: bytes,
) -> dict[str, Any]:
    payload = _exact_bytes(result_csv, "target-load FEA result")
    plan_row = expected_attempt["plan_row"]
    result_row, metrics = _validated_result_metrics(
        manifest,
        plan_row,
        payload,
        label="target-load FEA result",
    )
    probe_id = str(expected_attempt["probe_id"])
    probe = _find_probe(manifest, probe_id)
    target = probe["target"]
    output_value = (
        metrics["torque_nm"]
        if target["target_kind"] == "torque"
        else metrics["actual_power_w"]
    )
    required_power = _finite(target["required_power_w"], "required mechanical power")
    target_load_efficiency = required_power / (required_power + metrics["total_loss_w"]) * 100.0
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "match_run_id": manifest["match_run_id"],
        "probe_id": probe_id,
        "attempt_id": expected_attempt["attempt_id"],
        "attempt_index": int(expected_attempt["attempt_index"]),
        "case_id": str(expected_attempt["case_id"]),
        "dedupe_key": expected_attempt["dedupe_key"],
        "policy_sha256": expected_attempt["policy_sha256"],
        "history_sha256": expected_attempt["history_sha256"],
        "plan_row_sha256": expected_attempt["plan_row_sha256"],
        "attempt_manifest_sha256": canonical_json_sha256(expected_attempt),
        "current_peak_a": float(expected_attempt["current_peak_a"]),
        "output_value": output_value,
        "result_csv_base64": _encode_exact_bytes(payload),
        "result_sha256": _sha256_bytes(payload),
        "result_row_sha256": canonical_json_sha256(dict(result_row)),
        "hard_constraints_passed": metrics["hard_constraints_passed"],
        "target_kind": target["target_kind"],
        "target_value": target["target_value"],
        "relative_error": abs(output_value - float(target["target_value"]))
        / float(target["target_value"]),
        "actual_torque_nm": metrics["torque_nm"],
        "actual_power_w": metrics["actual_power_w"],
        "actual_total_loss_w": metrics["total_loss_w"],
        "actual_efficiency_pct": metrics["actual_efficiency_pct"],
        "target_load_efficiency_pct": target_load_efficiency,
        "actual_voltage_peak_v": metrics["voltage_peak_v"],
    }


def observation_from_result(
    manifest: Mapping[str, Any],
    attempt: Mapping[str, Any],
    prior_observations: Iterable[Mapping[str, Any]],
    result_csv: bytes,
) -> dict[str, Any]:
    """Validate one exact single-row FEA artifact and turn it into planner evidence."""

    expected_attempt = validate_attempt_manifest(manifest, attempt, prior_observations)
    return _observation_from_validated_attempt(manifest, expected_attempt, result_csv)


def validate_fixed_current_mtpa_evidence(
    manifest: Mapping[str, Any],
    candidate_id: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate exact same-current seed FEA artifacts for torque-per-amp MTPA."""

    validate_root_manifest(manifest)
    candidate = _text(candidate_id, "candidate_id")
    if set(evidence) != {
        "schema_version",
        "candidate_id",
        "control_source",
        "operating_points",
    }:
        raise TargetLoadWorkflowError("fixed-current MTPA evidence fields differ")
    if evidence.get("schema_version") != FIXED_MTPA_EVIDENCE_SCHEMA_VERSION:
        raise TargetLoadWorkflowError("fixed-current MTPA evidence schema is invalid")
    if evidence.get("candidate_id") != candidate:
        raise TargetLoadWorkflowError("fixed-current MTPA evidence candidate differs")
    if evidence.get("control_source") != "fixed_current_mtpa":
        raise TargetLoadWorkflowError("fixed-current MTPA evidence control_source is invalid")
    candidate_probes = [probe for probe in manifest["probes"] if probe["candidate_id"] == candidate]
    if not candidate_probes:
        raise TargetLoadWorkflowError("candidate is absent from the target-load root")
    points = evidence.get("operating_points")
    if not isinstance(points, list):
        raise TargetLoadWorkflowError("fixed-current MTPA operating_points are missing")
    expected_point_order = list(manifest["identity"]["operating_point_order"])
    if any(not isinstance(point, Mapping) for point in points) or [
        point.get("operating_point_id") for point in points
    ] != expected_point_order:
        raise TargetLoadWorkflowError("fixed-current MTPA operating-point order/coverage differs")
    point_receipts: list[dict[str, Any]] = []
    for point in points:
        if set(point) != {"operating_point_id", "rows"}:
            raise TargetLoadWorkflowError("fixed-current MTPA point fields differ")
        point_id = _text(point.get("operating_point_id"), "MTPA operating point")
        probes = [probe for probe in candidate_probes if probe["operating_point_id"] == point_id]
        rows = point.get("rows")
        if not isinstance(rows, list) or len(rows) != len(probes):
            raise TargetLoadWorkflowError("fixed-current MTPA beta-role coverage differs")
        base = probes[0]["base_row"]
        current = _finite(base.get("i_peak_a"), "fixed-current MTPA seed current")
        speed = _finite(base.get("base_rpm"), "fixed-current MTPA seed speed")
        if current <= 0.0:
            raise TargetLoadWorkflowError("fixed-current MTPA seed current must be positive")
        normalized_rows: list[dict[str, Any]] = []
        for probe, row in zip(probes, rows):
            if not isinstance(row, Mapping):
                raise TargetLoadWorkflowError("fixed-current MTPA beta row is invalid")
            if set(row) != {"beta_validation_role", "case_id", "result_csv_base64"}:
                raise TargetLoadWorkflowError("fixed-current MTPA beta-row fields differ")
            if row.get("beta_validation_role") != probe["beta_validation_role"]:
                raise TargetLoadWorkflowError("fixed-current MTPA beta-role order differs")
            if row.get("case_id") != probe["base_case_id"]:
                raise TargetLoadWorkflowError("fixed-current MTPA case differs from the frozen seed")
            payload = _decode_exact_bytes(
                row.get("result_csv_base64"),
                f"fixed-current MTPA {probe['base_case_id']} result CSV",
            )
            result_row, metrics = _validated_result_metrics(
                manifest,
                probe["base_row"],
                payload,
                label="fixed-current MTPA FEA result",
            )
            if metrics["hard_constraints_passed"] is not True:
                raise TargetLoadWorkflowError("fixed-current MTPA result violates a hard constraint")
            if canonical_float(metrics["current_peak_a"]) != canonical_float(current):
                raise TargetLoadWorkflowError("fixed-current MTPA rows do not share the seed current")
            if canonical_float(metrics["speed_rpm"]) != canonical_float(speed):
                raise TargetLoadWorkflowError("fixed-current MTPA row speed differs from the seed plan")
            torque = metrics["torque_nm"]
            torque_per_amp = torque / current
            normalized_rows.append(
                {
                    "beta_validation_role": probe["beta_validation_role"],
                    "beta_dq_deg": float(probe["beta_dq_deg"]),
                    "current_peak_a": current,
                    "speed_rpm": speed,
                    "torque_nm": torque,
                    "torque_per_peak_amp": torque_per_amp,
                    "result_sha256": _sha256_bytes(payload),
                    "result_row_sha256": canonical_json_sha256(dict(result_row)),
                }
            )
        center = next(
            row
            for row in normalized_rows
            if row["beta_validation_role"] == BETA_VALIDATION_ROLE_CENTER
        )
        if any(
            center["torque_per_peak_amp"] + 1.0e-12 < row["torque_per_peak_amp"]
            for row in normalized_rows
            if row is not center
        ):
            raise TargetLoadWorkflowError("fixed-current MTPA center is not a local torque/A maximum")
        point_receipts.append(
            {
                "operating_point_id": point_id,
                "geometry_group_id": base["geometry_group_id"],
                "design_hash": base["design_hash"],
                "current_peak_a": current,
                "speed_rpm": speed,
                "rows": normalized_rows,
            }
        )
    receipt = {
        "schema_version": FIXED_MTPA_EVIDENCE_SCHEMA_VERSION,
        "candidate_id": candidate,
        "control_source": "fixed_current_mtpa",
        "operating_points": point_receipts,
    }
    return {**receipt, "evidence_sha256": canonical_json_sha256(receipt)}


def finalize_candidate_target_load(
    manifest: Mapping[str, Any],
    candidate_id: str,
    observations_by_probe: Mapping[str, Sequence[Mapping[str, Any]]],
    fixed_current_mtpa_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Finalize one candidate using required-power efficiency and independent beta matches."""

    validate_root_manifest(manifest)
    candidate = _text(candidate_id, "candidate_id")
    probes = [probe for probe in manifest["probes"] if probe["candidate_id"] == candidate]
    if not probes:
        raise TargetLoadWorkflowError("candidate is absent from the target-load root")
    expected_probe_ids = {probe["probe_id"] for probe in probes}
    if set(observations_by_probe) != expected_probe_ids:
        raise TargetLoadWorkflowError("candidate target-load probe coverage is incomplete or extra")
    mtpa_receipt = validate_fixed_current_mtpa_evidence(
        manifest,
        candidate,
        fixed_current_mtpa_evidence,
    )
    matched_by_point: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    history_receipts: list[dict[str, Any]] = []
    for probe in probes:
        result = plan_probe_attempt(
            manifest,
            probe["probe_id"],
            observations_by_probe[probe["probe_id"]],
        )
        if result.get("terminal_status") != "matched":
            raise TargetLoadWorkflowError(
                f"probe {probe['probe_id']} is not a hard-feasible target-load match"
            )
        matched = result.get("matched_observation")
        if not isinstance(matched, Mapping) or matched.get("hard_constraints_passed") is not True:
            raise TargetLoadWorkflowError("matched target-load observation is not hard-feasible")
        matched_by_point.setdefault(probe["operating_point_id"], []).append((probe, matched))
        history_receipts.append(
            {
                "probe_id": probe["probe_id"],
                "history_count": result["history_count"],
                "history_sha256": result["history_sha256"],
                "matched_attempt_id": matched["attempt_id"],
                "matched_attempt_manifest_sha256": matched["attempt_manifest_sha256"],
                "matched_result_sha256": matched["result_sha256"],
                "matched_result_row_sha256": matched["result_row_sha256"],
            }
        )

    numerator = 0.0
    denominator = 0.0
    diagnostic_actual_power = 0.0
    point_summaries: list[dict[str, Any]] = []
    for point_id in manifest["identity"]["operating_point_order"]:
        entries = matched_by_point.get(point_id, [])
        if not entries:
            raise TargetLoadWorkflowError("matched target-load operating-point coverage is incomplete")
        center_pairs = [
            pair for pair in entries if pair[0]["beta_validation_role"] == BETA_VALIDATION_ROLE_CENTER
        ]
        if len(center_pairs) != 1:
            raise TargetLoadWorkflowError("matched target-load beta center is missing or duplicate")
        center_probe, center = center_pairs[0]
        center_loss = _nonnegative(center["actual_total_loss_w"], "matched center loss")
        for probe, matched in entries:
            if probe is center_probe:
                continue
            neighbor_loss = _nonnegative(matched["actual_total_loss_w"], "matched neighbor loss")
            allowed = max(
                pareto_validator.LOCAL_BETA_LOSS_ABSOLUTE_TOLERANCE_W,
                pareto_validator.LOCAL_BETA_LOSS_RELATIVE_TOLERANCE * neighbor_loss,
            )
            if center_loss > neighbor_loss + allowed:
                raise TargetLoadWorkflowError(
                    "target-load beta center is not a local matched-load loss minimum"
                )
        target = center_probe["target"]
        weight = _finite(target["duty_weight"], "duty weight")
        required_power = _finite(target["required_power_w"], "required power")
        numerator += weight * required_power
        denominator += weight * (required_power + center_loss)
        diagnostic_actual_power += weight * _finite(center["actual_power_w"], "actual power")
        point_summaries.append(
            {
                "operating_point_id": point_id,
                "required_power_w": required_power,
                "duty_weight": weight,
                "matched_center_loss_w": center_loss,
                "matched_current_by_beta_role_a": {
                    probe["beta_validation_role"]: matched["current_peak_a"]
                    for probe, matched in entries
                },
                "matched_evidence_by_beta_role": {
                    probe["beta_validation_role"]: {
                        "case_id": matched["case_id"],
                        "attempt_id": matched["attempt_id"],
                        "attempt_manifest_sha256": matched["attempt_manifest_sha256"],
                        "result_sha256": matched["result_sha256"],
                        "result_row_sha256": matched["result_row_sha256"],
                    }
                    for probe, matched in entries
                },
                "target_load_efficiency_pct": required_power
                / (required_power + center_loss)
                * 100.0,
                "diagnostic_actual_power_w": center["actual_power_w"],
                "diagnostic_actual_efficiency_pct": center["actual_efficiency_pct"],
            }
        )
    if numerator <= 0.0 or denominator < numerator:
        raise TargetLoadWorkflowError("candidate required-power efficiency denominator is invalid")
    cycle_efficiency = numerator / denominator
    base_row = probes[0]["base_row"]
    volume = active_volume_m3(
        _finite(base_row.get("stator_outer_radius"), "stator outer radius"),
        _finite(base_row.get("stack_length_mm"), "stack length"),
    )
    summary = {
        "schema_version": CANDIDATE_SUMMARY_SCHEMA_VERSION,
        "match_run_id": manifest["match_run_id"],
        "candidate_id": candidate,
        "status": "matched_and_beta_validated",
        "objective_active_volume_m3": volume,
        "objective_cycle_efficiency": cycle_efficiency,
        "objective_one_minus_cycle_efficiency": 1.0 - cycle_efficiency,
        "efficiency_basis": "required_mechanical_power_plus_matched_measured_loss",
        "diagnostic_weighted_actual_power_w": diagnostic_actual_power,
        "operating_points": point_summaries,
        "target_load_history_receipts": history_receipts,
        "fixed_current_mtpa_evidence_sha256": mtpa_receipt["evidence_sha256"],
    }
    return {**summary, "summary_sha256": canonical_json_sha256(summary)}
