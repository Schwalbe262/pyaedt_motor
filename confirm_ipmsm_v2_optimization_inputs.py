"""Create or audit a human-authorized IPMSM optimization-input confirmation.

The pipeline optimization specification is immutable, but some values in the
current specification are explicitly assumption-marked.  This standalone
sidecar never changes the pipeline contract or the specification.  Instead it
requires an operator-authored declaration that repeats every production input
and acknowledges each section.  A declaration that differs from the immutable
specification is rejected so the correction must go through a new spec and
contract revision.

The default build mode is read-only.  ``--execute`` is required to atomically
publish a fresh, canonical JSON artifact without replacing an existing file.
``audit_confirmation`` is intentionally importable by a future supervisor.

This artifact is not a digital signature.  ``confirmed_by`` is a
self-attestation; filesystem ACLs and the operator workflow that protects the
declaration are the identity/authorization trust boundary.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping, Sequence

from atomic_publish import (
    cleanup_publish_receipt,
    publish_no_replace,
    rollback_owned_output,
)
import ipmsm_optimization as optimization
import supervise_ipmsm_v2_pipeline as supervisor


DECLARATION_SCHEMA_VERSION = "ipmsm-v2-optimization-input-declaration-v1"
CONFIRMATION_SCHEMA_VERSION = "ipmsm-v2-optimization-input-confirmation-v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ATTESTATION_KIND = "filesystem_acl_self_attestation"
MAX_FUTURE_CLOCK_SKEW_SECONDS = 300
ACKNOWLEDGEMENT_FIELDS = (
    "operating_points_confirmed",
    "duty_cycle_confirmed",
    "inverter_confirmed",
    "winding_confirmed",
    "design_space_confirmed",
    "constraints_and_derived_limits_confirmed",
    "beta_calibration_and_control_confirmed",
    "topology_confirmed",
    "nsga2_settings_confirmed",
    "volume_definition_confirmed",
    "efficiency_objective_confirmed",
    "spec_assumptions_reviewed",
    "authorized_for_production_optimization",
)
VOLUME_DEFINITION = {
    "definition_id": "cylindrical_active_envelope_v1",
    "quantity": "active_volume_m3",
    "objective": "minimize",
    "unit": "m^3",
    "formula": "pi * (stator_outer_radius_mm * 1e-3)^2 * (stack_length_mm * 1e-3)",
    "radial_extent": "stator_outer_radius_mm",
    "axial_extent": "stack_length_mm",
    "end_windings_included": False,
    "housing_included": False,
    "shaft_extensions_included": False,
    "inverter_included": False,
}
EFFICIENCY_OBJECTIVE_DEFINITION = {
    "definition_id": "duty_weighted_conservative_cycle_efficiency_v1",
    "quantity": "cycle_efficiency",
    "objective": "maximize",
    "optimizer_minimization_value": "1.0 - cycle_efficiency",
    "formula": (
        "sum(duty_weight_i * required_power_w_i) / "
        "sum(duty_weight_i * (required_power_w_i + total_loss_ucb_w_i))"
    ),
    "loss_basis": "core_loss_ucb_w + solid_loss_ucb_w + copper_loss_w",
    "weights_source": "confirmed_inputs.duty_cycle.weights",
}


class OptimizationInputConfirmationError(ValueError):
    """The declaration or confirmation cannot be trusted."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    payload: bytes
    sha256: str
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class BoundContext:
    contract_path: Path
    contract_file_sha256: str
    contract_sha256: str
    spec_path: Path
    spec_sha256: str
    spec_canonical_sha256: str
    spec: optimization.OptimizationSpec
    spec_assumptions: Mapping[str, Any]
    implementation_path: Path
    implementation_sha256: str
    snapshots: tuple[FileSnapshot, ...]


@dataclass(frozen=True)
class ConfirmationAudit:
    path: Path
    file_sha256: str
    confirmation_sha256: str
    contract_sha256: str
    optimization_spec_sha256: str
    confirmed_by: str
    confirmed_at_utc: str
    duty_basis: str

    def as_mapping(self) -> dict[str, Any]:
        return {
            "status": "confirmed",
            "path": str(self.path),
            "file_sha256": self.file_sha256,
            "confirmation_sha256": self.confirmation_sha256,
            "contract_sha256": self.contract_sha256,
            "optimization_spec_sha256": self.optimization_spec_sha256,
            "confirmed_by": self.confirmed_by,
            "confirmed_at_utc": self.confirmed_at_utc,
            "duty_basis": self.duty_basis,
            "authorized_for_production_optimization": True,
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OptimizationInputConfirmationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise OptimizationInputConfirmationError(f"non-finite JSON constant: {value}")


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _snapshot_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise OptimizationInputConfirmationError(f"input is not a regular file: {path}")
    return int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns)


def read_stable_snapshot(path: str | Path, label: str) -> FileSnapshot:
    try:
        source = Path(path).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise OptimizationInputConfirmationError(f"cannot resolve {label}: {path}") from exc
    before = _snapshot_identity(source)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise OptimizationInputConfirmationError(f"cannot read {label}: {source}") from exc
    after = _snapshot_identity(source)
    if before != after:
        raise OptimizationInputConfirmationError(f"{label} changed while being read: {source}")
    return FileSnapshot(
        path=source,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        identity=after,
    )


def assert_snapshot_unchanged(snapshot: FileSnapshot) -> None:
    try:
        current_identity = _snapshot_identity(snapshot.path)
    except (FileNotFoundError, OSError) as exc:
        raise OptimizationInputConfirmationError(
            f"bound input disappeared during confirmation: {snapshot.path}"
        ) from exc
    if current_identity != snapshot.identity:
        raise OptimizationInputConfirmationError(
            f"bound input changed during confirmation: {snapshot.path}"
        )
    current = read_stable_snapshot(snapshot.path, "bound input")
    if current.sha256 != snapshot.sha256:
        raise OptimizationInputConfirmationError(
            f"bound input hash changed during confirmation: {snapshot.path}"
        )


def _read_json_snapshot(path: str | Path, label: str) -> tuple[FileSnapshot, dict[str, Any]]:
    snapshot = read_stable_snapshot(path, label)
    try:
        text = snapshot.payload.decode("utf-8-sig")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OptimizationInputConfirmationError(f"invalid {label} JSON: {snapshot.path}") from exc
    if not isinstance(value, dict):
        raise OptimizationInputConfirmationError(f"{label} must be a JSON object")
    return snapshot, value


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(
        str(right.resolve(strict=False))
    )


def _flag_path(argv: Sequence[str], flag: str, workdir: Path) -> Path:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise OptimizationInputConfirmationError(
            f"pipeline Stage3 generator must contain exactly one {flag} value"
        )
    raw = argv[positions[0] + 1]
    if not isinstance(raw, str) or not raw.strip() or raw.startswith("--"):
        raise OptimizationInputConfirmationError(f"pipeline Stage3 {flag} value is invalid")
    path = Path(raw)
    return (path if path.is_absolute() else workdir / path).resolve(strict=True)


def _single_immutable(contract: supervisor.PipelineContract, path: Path, label: str) -> Any:
    matches = [item for item in contract.immutable_inputs if _same_path(item.path, path)]
    if len(matches) != 1:
        raise OptimizationInputConfirmationError(
            f"{label} must occur exactly once in pipeline immutable_inputs"
        )
    return matches[0]


def load_bound_context(contract_path: str | Path) -> BoundContext:
    contract_snapshot, _ = _read_json_snapshot(contract_path, "pipeline contract")
    try:
        contract = supervisor.load_contract(contract_snapshot.path)
        supervisor.audit_immutable_inputs(contract)
    except (ValueError, RuntimeError, OSError) as exc:
        raise OptimizationInputConfirmationError(f"pipeline contract audit failed: {exc}") from exc
    if contract.source.resolve(strict=False) != contract_snapshot.path:
        raise OptimizationInputConfirmationError("loaded contract source differs from audited path")

    spec_path = _flag_path(contract.stage3.generate_argv, "--spec", contract.workdir)
    optimization_spec_path = _flag_path(
        contract.optimization.argv_template, "--optimization-spec", contract.workdir
    )
    if not _same_path(spec_path, optimization_spec_path):
        raise OptimizationInputConfirmationError(
            "Stage3 generation and production optimization reference different specs"
        )
    spec_artifact = _single_immutable(contract, spec_path, "optimization spec")
    spec_snapshot, spec_raw = _read_json_snapshot(spec_path, "optimization spec")
    if spec_snapshot.sha256 != spec_artifact.sha256:
        raise OptimizationInputConfirmationError(
            "optimization spec differs from its immutable contract SHA256"
        )
    try:
        spec = optimization.optimization_spec_from_mapping(spec_raw)
    except (ValueError, TypeError) as exc:
        raise OptimizationInputConfirmationError(f"optimization spec validation failed: {exc}") from exc

    implementation_path = Path(optimization.__file__).resolve(strict=True)
    if implementation_path.suffix.lower() == ".pyc":
        implementation_path = implementation_path.with_suffix(".py").resolve(strict=True)
    implementation_artifact = _single_immutable(
        contract, implementation_path, "optimization implementation"
    )
    implementation_snapshot = read_stable_snapshot(
        implementation_path, "optimization implementation"
    )
    if implementation_snapshot.sha256 != implementation_artifact.sha256:
        raise OptimizationInputConfirmationError(
            "optimization implementation differs from its immutable contract SHA256"
        )

    assumptions = spec_raw.get("_assumptions", {})
    if not isinstance(assumptions, dict):
        raise OptimizationInputConfirmationError("optimization spec _assumptions must be an object")
    # Bind the advertised volume semantics to the imported, immutable function.
    expected_probe = math.pi * (150.0e-3) ** 2 * (49.45e-3)
    if not math.isclose(
        optimization.active_volume_m3(150.0, 49.45), expected_probe, rel_tol=0.0, abs_tol=1e-15
    ):
        raise OptimizationInputConfirmationError(
            "immutable active_volume_m3 implementation does not match the declared definition"
        )

    snapshots = (contract_snapshot, spec_snapshot, implementation_snapshot)
    for snapshot in snapshots:
        assert_snapshot_unchanged(snapshot)
    return BoundContext(
        contract_path=contract_snapshot.path,
        contract_file_sha256=contract_snapshot.sha256,
        contract_sha256=contract.contract_sha256,
        spec_path=spec_snapshot.path,
        spec_sha256=spec_snapshot.sha256,
        spec_canonical_sha256=canonical_sha256(spec_raw),
        spec=spec,
        spec_assumptions=dict(assumptions),
        implementation_path=implementation_snapshot.path,
        implementation_sha256=implementation_snapshot.sha256,
        snapshots=snapshots,
    )


def _effective_inputs(spec: optimization.OptimizationSpec, duty_basis: str) -> dict[str, Any]:
    operating_points = [
        {
            "name": point.name,
            "speed_rpm": point.speed_rpm,
            "target_kind": point.target_kind,
            "target_torque_nm": point.target_torque_nm,
            "target_power_w": point.target_power_w,
            "required_torque_nm": point.required_torque_nm,
            "required_power_w": point.required_power_w,
        }
        for point in spec.operating_points
    ]
    winding = spec.winding
    return {
        "operating_points": operating_points,
        "duty_cycle": {
            "basis": duty_basis,
            "weights": [
                {"name": point.name, "duty_weight": point.duty_weight}
                for point in spec.operating_points
            ],
            "weight_sum": sum(point.duty_weight for point in spec.operating_points),
        },
        "inverter": {
            "vdc_v": spec.inverter.vdc_v,
            "phase_peak_current_limit_a": spec.inverter.phase_peak_current_limit_a,
            "voltage_utilization": spec.inverter.voltage_utilization,
            "phase_peak_voltage_limit_v": spec.inverter.phase_peak_voltage_limit_v,
        },
        "winding": {
            "series_turns_per_phase": winding.series_turns_per_phase,
            "turns_per_coil_side": winding.turns_per_coil_side,
            "coils_per_phase": winding.coils_per_phase,
            "parallel_branches": winding.parallel_branches,
            "strand_area_mm2": winding.strand_area_mm2,
            "strands_per_turn": winding.strands_per_turn,
            "fill_factor": winding.fill_factor,
            "end_turn_factor": winding.end_turn_factor,
            "overhang_mm": winding.overhang_mm,
            "copper_resistivity_20c_ohm_m": winding.copper_resistivity_20c_ohm_m,
            "copper_temp_coefficient_per_c": winding.copper_temp_coefficient_per_c,
            "winding_temperature_c": winding.winding_temperature_c,
            "conductor_area_per_branch_mm2": winding.conductor_area_per_branch_mm2,
            "total_parallel_conductor_area_mm2": winding.total_parallel_conductor_area_mm2,
        },
        "design_space": {
            "geometry": [
                {"name": bound.name, "lower": bound.lower, "upper": bound.upper}
                for bound in spec.geometry_design_space
            ],
            "stack_length_mm": {
                "lower": spec.stack_length_bounds.lower,
                "upper": spec.stack_length_bounds.upper,
            },
        },
        "constraints_and_derived_limits": {
            "current_density_limit_a_per_mm2": (
                spec.constraints.current_density_limit_a_per_mm2
            ),
            "inverter_phase_peak_current_limit_a": spec.current_limit_a,
            "current_density_limited_peak_current_a": (
                spec.current_density_limited_peak_current_a
            ),
            "effective_peak_current_limit_a": spec.effective_peak_current_limit_a,
            "phase_peak_voltage_limit_v": spec.phase_peak_voltage_limit_v,
            "slot_fill_ratio_limit": spec.winding.fill_factor,
            "required_nonnegative_margins": [
                "torque_target_nm_or_power_target_w",
                "voltage_v",
                "inverter_current_a",
                "current_density_a_per_mm2",
                "slot_fill_ratio",
                "in_domain",
                "geometry_margin",
            ],
            "prediction_bound_policy": "target_lcb_voltage_and_loss_ucb",
        },
        "beta_calibration_and_control": {
            "calibration": {
                "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
                "calibration_id": spec.beta_calibration.calibration_id,
                "convention": spec.beta_calibration.convention,
            },
            "search": {
                "beta_bounds_deg": list(spec.control.beta_bounds_deg),
                "current_grid_points": spec.control.current_grid_points,
                "coarse_beta_step_deg": spec.control.coarse_beta_step_deg,
                "beta_refinement_steps_deg": list(
                    spec.control.beta_refinement_steps_deg
                ),
                "current_refinement_denominators": list(
                    spec.control.current_refinement_denominators
                ),
            },
        },
        "topology": {
            "slot_number": spec.slot_number,
            "pole_number": spec.pole_number,
            "pole_pairs": spec.pole_number // 2,
        },
        "nsga2": {
            "population_size": spec.nsga2.population_size,
            "max_generations": spec.nsga2.max_generations,
            "seeds": list(spec.nsga2.seeds),
            "crossover_probability": spec.nsga2.crossover_probability,
            "crossover_eta": spec.nsga2.crossover_eta,
            "mutation_eta": spec.nsga2.mutation_eta,
            "max_fea_candidates": spec.nsga2.max_fea_candidates,
        },
        "objectives": {
            "volume": dict(VOLUME_DEFINITION),
            "efficiency": dict(EFFICIENCY_OBJECTIVE_DEFINITION),
        },
    }


def _context_bindings(context: BoundContext) -> dict[str, Any]:
    """Return the complete context an operator declaration authorizes."""

    return {
        "contract": {
            "path": str(context.contract_path),
            "file_sha256": context.contract_file_sha256,
            "contract_sha256": context.contract_sha256,
        },
        "optimization_spec": {
            "path": str(context.spec_path),
            "sha256": context.spec_sha256,
            "canonical_sha256": context.spec_canonical_sha256,
            "schema_version": context.spec.schema_version,
        },
        "optimization_implementation": {
            "path": str(context.implementation_path),
            "sha256": context.implementation_sha256,
            "volume_function": "ipmsm_optimization.active_volume_m3",
        },
        "spec_assumptions": dict(context.spec_assumptions),
    }


def declaration_template(context: BoundContext) -> dict[str, Any]:
    return {
        "schema_version": DECLARATION_SCHEMA_VERSION,
        "bindings": _context_bindings(context),
        "authority": {
            "confirmed_by": "",
            "confirmed_at_utc": "",
            "evidence_reference": "",
            "attestation_kind": ATTESTATION_KIND,
        },
        "confirmed_inputs": _effective_inputs(context.spec, ""),
        "acknowledgements": {name: False for name in ACKNOWLEDGEMENT_FIELDS},
    }


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise OptimizationInputConfirmationError(
            f"{label} fields mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OptimizationInputConfirmationError(f"{label} must be an object")
    return value


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise OptimizationInputConfirmationError(f"{label} must be a nonblank string")
    return value.strip()


def _confirmed_at(value: Any) -> str:
    raw = _nonblank(value, "authority.confirmed_at_utc")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OptimizationInputConfirmationError(
            "authority.confirmed_at_utc must be an RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OptimizationInputConfirmationError(
            "authority.confirmed_at_utc must include a timezone"
        )
    utc = parsed.astimezone(timezone.utc)
    if utc > datetime.now(timezone.utc) + timedelta(
        seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS
    ):
        raise OptimizationInputConfirmationError(
            "authority.confirmed_at_utc is beyond the allowed future clock skew"
        )
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def _assert_semantically_equal(actual: Any, expected: Any, path: str) -> None:
    if isinstance(expected, bool):
        if type(actual) is not bool or actual is not expected:
            raise OptimizationInputConfirmationError(f"{path} does not match the immutable spec")
        return
    if expected is None:
        if actual is not None:
            raise OptimizationInputConfirmationError(f"{path} does not match the immutable spec")
        return
    if isinstance(expected, int):
        if type(actual) is not int or actual != expected:
            raise OptimizationInputConfirmationError(f"{path} does not match the immutable spec")
        return
    if isinstance(expected, float):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            raise OptimizationInputConfirmationError(f"{path} must be a finite number")
        value = float(actual)
        if not math.isfinite(value) or value != expected:
            raise OptimizationInputConfirmationError(f"{path} does not match the immutable spec")
        return
    if isinstance(expected, str):
        if type(actual) is not str or actual != expected:
            raise OptimizationInputConfirmationError(f"{path} does not match the immutable spec")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise OptimizationInputConfirmationError(f"{path} does not match the immutable spec")
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            _assert_semantically_equal(actual_item, expected_item, f"{path}[{index}]")
        return
    if isinstance(expected, dict):
        actual_mapping = _mapping(actual, path)
        _expect_keys(actual_mapping, set(expected), path)
        for key, expected_item in expected.items():
            _assert_semantically_equal(actual_mapping[key], expected_item, f"{path}.{key}")
        return
    raise AssertionError(f"unsupported expected JSON value at {path}")


def _validate_declaration(
    context: BoundContext, declaration: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, bool]]:
    _expect_keys(
        declaration,
        {
            "schema_version",
            "bindings",
            "authority",
            "confirmed_inputs",
            "acknowledgements",
        },
        "declaration",
    )
    if declaration["schema_version"] != DECLARATION_SCHEMA_VERSION:
        raise OptimizationInputConfirmationError("unsupported declaration schema_version")

    bindings = _mapping(declaration["bindings"], "bindings")
    expected_bindings = _context_bindings(context)
    _assert_semantically_equal(bindings, expected_bindings, "bindings")

    authority_raw = _mapping(declaration["authority"], "authority")
    _expect_keys(
        authority_raw,
        {
            "confirmed_by",
            "confirmed_at_utc",
            "evidence_reference",
            "attestation_kind",
        },
        "authority",
    )
    if authority_raw["attestation_kind"] != ATTESTATION_KIND:
        raise OptimizationInputConfirmationError(
            f"authority.attestation_kind must be {ATTESTATION_KIND!r}"
        )
    authority = {
        "confirmed_by": _nonblank(authority_raw["confirmed_by"], "authority.confirmed_by"),
        "confirmed_at_utc": _confirmed_at(authority_raw["confirmed_at_utc"]),
        "evidence_reference": _nonblank(
            authority_raw["evidence_reference"], "authority.evidence_reference"
        ),
        "attestation_kind": ATTESTATION_KIND,
    }

    confirmed = _mapping(declaration["confirmed_inputs"], "confirmed_inputs")
    _expect_keys(
        confirmed,
        {
            "operating_points",
            "duty_cycle",
            "inverter",
            "winding",
            "design_space",
            "constraints_and_derived_limits",
            "beta_calibration_and_control",
            "topology",
            "nsga2",
            "objectives",
        },
        "confirmed_inputs",
    )
    duty = _mapping(confirmed["duty_cycle"], "confirmed_inputs.duty_cycle")
    _expect_keys(duty, {"basis", "weights", "weight_sum"}, "confirmed_inputs.duty_cycle")
    duty_basis = _nonblank(duty["basis"], "confirmed_inputs.duty_cycle.basis")
    expected = _effective_inputs(context.spec, duty_basis)
    _assert_semantically_equal(confirmed, expected, "confirmed_inputs")

    acknowledgements = _mapping(declaration["acknowledgements"], "acknowledgements")
    _expect_keys(acknowledgements, set(ACKNOWLEDGEMENT_FIELDS), "acknowledgements")
    for name in ACKNOWLEDGEMENT_FIELDS:
        if acknowledgements[name] is not True:
            raise OptimizationInputConfirmationError(
                f"acknowledgements.{name} must be explicitly true"
            )
    return (
        expected_bindings,
        authority,
        expected,
        {name: True for name in ACKNOWLEDGEMENT_FIELDS},
    )


def build_confirmation(
    context: BoundContext,
    declaration: Mapping[str, Any],
    *,
    declaration_path: Path,
    declaration_sha256: str,
) -> dict[str, Any]:
    bindings, authority, confirmed_inputs, acknowledgements = _validate_declaration(
        context, declaration
    )
    if not isinstance(declaration_sha256, str) or SHA256_PATTERN.fullmatch(
        declaration_sha256
    ) is None:
        raise OptimizationInputConfirmationError("declaration SHA256 is invalid")
    unsigned = {
        "schema_version": CONFIRMATION_SCHEMA_VERSION,
        "declaration_source": {
            "path": str(declaration_path.resolve(strict=False)),
            "sha256": declaration_sha256,
        },
        "bindings": bindings,
        "authority": authority,
        "confirmed_inputs": confirmed_inputs,
        "acknowledgements": acknowledgements,
    }
    return {**unsigned, "confirmation_sha256": canonical_sha256(unsigned)}


def _audit_confirmation_with_context(
    confirmation_path: str | Path,
    bound: BoundContext,
) -> ConfirmationAudit:
    snapshot, document = _read_json_snapshot(confirmation_path, "optimization confirmation")
    if snapshot.payload != canonical_json_bytes(document):
        raise OptimizationInputConfirmationError("confirmation is not canonical JSON bytes")
    expected_fields = {
        "schema_version",
        "confirmation_sha256",
        "declaration_source",
        "bindings",
        "authority",
        "confirmed_inputs",
        "acknowledgements",
    }
    _expect_keys(document, expected_fields, "confirmation")
    if document["schema_version"] != CONFIRMATION_SCHEMA_VERSION:
        raise OptimizationInputConfirmationError("unsupported confirmation schema_version")
    declared_hash = document["confirmation_sha256"]
    if not isinstance(declared_hash, str) or SHA256_PATTERN.fullmatch(declared_hash) is None:
        raise OptimizationInputConfirmationError("confirmation_sha256 is invalid")
    unsigned = {key: value for key, value in document.items() if key != "confirmation_sha256"}
    if canonical_sha256(unsigned) != declared_hash:
        raise OptimizationInputConfirmationError("confirmation_sha256 mismatch")

    declaration_source = _mapping(document["declaration_source"], "declaration_source")
    _expect_keys(declaration_source, {"path", "sha256"}, "declaration_source")
    recorded_source_path = _nonblank(
        declaration_source["path"], "declaration_source.path"
    )
    if not Path(recorded_source_path).is_absolute():
        raise OptimizationInputConfirmationError(
            "declaration_source.path must be absolute"
        )
    source_hash = declaration_source["sha256"]
    if not isinstance(source_hash, str) or SHA256_PATTERN.fullmatch(source_hash) is None:
        raise OptimizationInputConfirmationError("declaration_source.sha256 is invalid")

    embedded_declaration = {
        "schema_version": DECLARATION_SCHEMA_VERSION,
        "bindings": document["bindings"],
        "authority": document["authority"],
        "confirmed_inputs": document["confirmed_inputs"],
        "acknowledgements": document["acknowledgements"],
    }
    embedded_bindings, authority, confirmed_inputs, embedded_acks = (
        _validate_declaration(bound, embedded_declaration)
    )

    declaration_snapshot, live_declaration = _read_json_snapshot(
        Path(recorded_source_path), "recorded optimization-input declaration"
    )
    if str(declaration_snapshot.path) != recorded_source_path:
        raise OptimizationInputConfirmationError(
            "declaration_source.path does not identify the exact recorded regular file"
        )
    if declaration_snapshot.sha256 != source_hash:
        raise OptimizationInputConfirmationError(
            "recorded declaration raw SHA256 mismatch"
        )
    live_bindings, live_authority, live_inputs, live_acks = _validate_declaration(
        bound, live_declaration
    )
    for name, embedded, live in (
        ("bindings", embedded_bindings, live_bindings),
        ("authority", authority, live_authority),
        ("confirmed_inputs", confirmed_inputs, live_inputs),
        ("acknowledgements", embedded_acks, live_acks),
    ):
        _assert_semantically_equal(embedded, live, name)

    for item in (*bound.snapshots, declaration_snapshot, snapshot):
        assert_snapshot_unchanged(item)
    return ConfirmationAudit(
        path=snapshot.path,
        file_sha256=snapshot.sha256,
        confirmation_sha256=declared_hash,
        contract_sha256=bound.contract_sha256,
        optimization_spec_sha256=bound.spec_sha256,
        confirmed_by=authority["confirmed_by"],
        confirmed_at_utc=authority["confirmed_at_utc"],
        duty_basis=confirmed_inputs["duty_cycle"]["basis"],
    )


def audit_confirmation(
    confirmation_path: str | Path,
    contract_path: str | Path,
) -> ConfirmationAudit:
    """Read-only strict audit for use by a future pipeline supervisor."""

    return _audit_confirmation_with_context(
        confirmation_path,
        load_bound_context(contract_path),
    )


def publish_confirmation(
    output: str | Path,
    document: Mapping[str, Any],
    context: BoundContext,
    declaration_snapshot: FileSnapshot,
) -> ConfirmationAudit:
    destination = Path(output).absolute()
    protected = {
        context.contract_path.resolve(strict=False),
        context.spec_path.resolve(strict=False),
        context.implementation_path.resolve(strict=False),
        declaration_snapshot.path.resolve(strict=False),
    }
    if destination.resolve(strict=False) in protected:
        raise OptimizationInputConfirmationError("output must not alias a bound input")
    if os.path.lexists(destination):
        raise FileExistsError(f"refusing to replace existing confirmation: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    staged = Path(name)
    receipt = None
    preserve_recovery = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(document))
            stream.flush()
            os.fsync(stream.fileno())
        for snapshot in (*context.snapshots, declaration_snapshot):
            assert_snapshot_unchanged(snapshot)
        receipt = publish_no_replace(staged, destination)
        audit = _audit_confirmation_with_context(destination, context)
        for snapshot in (*context.snapshots, declaration_snapshot):
            assert_snapshot_unchanged(snapshot)
        return audit
    except BaseException as exc:
        if receipt is not None and not rollback_owned_output(receipt):
            preserve_recovery = True
            raise OptimizationInputConfirmationError(
                "confirmation publication failed and owned-output rollback was unsafe"
            ) from exc
        raise
    finally:
        if receipt is not None and not preserve_recovery:
            cleanup_publish_receipt(receipt)
        if not preserve_recovery:
            staged.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--print-declaration-template", action="store_true")
    mode.add_argument("--declaration", type=Path)
    mode.add_argument("--audit-confirmation", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Publish a fresh confirmation. Omit for a read-only validation dry run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.print_declaration_template:
        if args.output is not None or args.execute:
            parser.error("template mode does not accept --output or --execute")
        context = load_bound_context(args.contract)
        print(canonical_json_bytes(declaration_template(context)).decode("utf-8"), end="")
        return 0
    if args.audit_confirmation is not None:
        if args.output is not None or args.execute:
            parser.error("audit mode does not accept --output or --execute")
        audit = audit_confirmation(args.audit_confirmation, args.contract)
        print(canonical_json_bytes(audit.as_mapping()).decode("utf-8"), end="")
        return 0
    if args.output is None:
        parser.error("declaration mode requires --output")

    context = load_bound_context(args.contract)
    declaration_snapshot, declaration = _read_json_snapshot(
        args.declaration, "optimization-input declaration"
    )
    document = build_confirmation(
        context,
        declaration,
        declaration_path=declaration_snapshot.path,
        declaration_sha256=declaration_snapshot.sha256,
    )
    for snapshot in (*context.snapshots, declaration_snapshot):
        assert_snapshot_unchanged(snapshot)
    if args.execute:
        audit = publish_confirmation(args.output, document, context, declaration_snapshot)
        result = {"mode": "execute", "writes_performed": 1, **audit.as_mapping()}
    else:
        result = {
            "mode": "dry_run",
            "writes_performed": 0,
            "status": "ready_to_publish",
            "output": str(args.output.absolute()),
            "contract_sha256": context.contract_sha256,
            "optimization_spec_sha256": context.spec_sha256,
            "confirmation_sha256": document["confirmation_sha256"],
        }
    print(canonical_json_bytes(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
