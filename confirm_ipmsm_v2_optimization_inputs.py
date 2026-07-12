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
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence

import atomic_publish
from atomic_publish import (
    FileIdentity,
    PROOF_SCHEMA_VERSION,
    publish_no_replace,
)
import ipmsm_optimization as optimization
import supervise_ipmsm_v2_pipeline as supervisor


DECLARATION_SCHEMA_VERSION = "ipmsm-v2-optimization-input-declaration-v1"
CONFIRMATION_SCHEMA_VERSION = "ipmsm-v2-optimization-input-confirmation-v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ATTESTATION_KIND = "filesystem_acl_self_attestation"
MAX_FUTURE_CLOCK_SKEW_SECONDS = 300
CONFIRMATION_STAGED_SUFFIX = ".confirmation.tmp"
CONFIRMATION_PROOF_SUFFIX = ".confirmation.proof.json"
CONFIRMATION_ATTEMPT_MARKER = ".confirmation.attempt."
CONFIRMATION_STAGE_READY_NAME = "stage-ready"
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
    identity: tuple[int, int, int, int, int]


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
    contract_canonical_sha256: str = ""
    base_contract_binding: Mapping[str, Any] | None = None


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


@dataclass(frozen=True)
class ConfirmationPublicationInspection:
    """Read-only state of the no-replace confirmation publication."""

    status: str
    destination: Path
    proof_path: Path
    pending_state: str | None = None
    audit: ConfirmationAudit | None = None

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "output": str(self.destination),
            "proof": str(self.proof_path),
        }
        if self.pending_state is not None:
            result["pending_state"] = self.pending_state
        return result


@dataclass(frozen=True)
class ConfirmationPublicationResult:
    audit: ConfirmationAudit
    outcome: str
    mutated: bool
    recovery_state: str | None = None

    @property
    def writes_performed(self) -> int:
        return int(self.mutated)

    @property
    def recovered(self) -> bool:
        return self.outcome == "recovered"

    @property
    def already_present(self) -> bool:
        return self.outcome == "already_present"


@dataclass(frozen=True)
class _ConfirmationAttempt:
    path: Path
    identity: tuple[int, int, int]
    stage_ready: bool
    stage_ready_identity: tuple[int, int, int] | None


@dataclass(frozen=True)
class _ConfirmationPublicationProof:
    proof_path: Path
    source: Path
    destination: Path
    identity: FileIdentity
    payload: bytes
    proof_identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _ConfirmationPublicationState:
    inspection: ConfirmationPublicationInspection
    expected_payload: bytes
    proof: _ConfirmationPublicationProof | None = None
    attempt: _ConfirmationAttempt | None = None
    staged_path: Path | None = None
    staged_identity: FileIdentity | None = None
    incomplete_proof_identity: FileIdentity | None = None


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


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _reject_link_components(path: Path, label: str) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _path_is_link_or_reparse(current):
            raise OptimizationInputConfirmationError(
                f"{label} contains a symlink/reparse component: {current}"
            )
        if not os.path.lexists(current):
            break


def _identity_from_stat(
    info: os.stat_result, path: Path
) -> tuple[int, int, int, int, int]:
    if not stat.S_ISREG(info.st_mode):
        raise OptimizationInputConfirmationError(f"input is not a regular file: {path}")
    if int(getattr(info, "st_nlink", 1)) != 1:
        raise OptimizationInputConfirmationError(f"input must not be a hardlink: {path}")
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(getattr(info, "st_nlink", 1)),
    )


def _snapshot_identity(path: Path) -> tuple[int, int, int, int, int]:
    return _identity_from_stat(path.stat(follow_symlinks=False), path)


def read_stable_snapshot(path: str | Path, label: str) -> FileSnapshot:
    lexical = Path(os.path.abspath(os.fspath(path)))
    _reject_link_components(lexical, label)
    try:
        source = lexical.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise OptimizationInputConfirmationError(f"cannot resolve {label}: {path}") from exc
    _reject_link_components(source, label)
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_BINARY", 0))
    )
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise OptimizationInputConfirmationError(f"cannot open {label}: {source}") from exc
    try:
        before = _identity_from_stat(os.fstat(descriptor), source)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = _identity_from_stat(os.fstat(descriptor), source)
    finally:
        os.close(descriptor)
    if before != after:
        raise OptimizationInputConfirmationError(f"{label} changed while being read: {source}")
    if _snapshot_identity(source) != after:
        raise OptimizationInputConfirmationError(f"{label} path changed while being read: {source}")
    payload = b"".join(chunks)
    if len(payload) != after[2]:
        raise OptimizationInputConfirmationError(f"{label} size changed while being read: {source}")
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


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(_lexical_absolute(path))))


def _same_directory(left: Path, right: Path) -> bool:
    """Accept mapped-drive/UNC aliases, but not merely similar strings."""

    first = _lexical_absolute(left)
    second = _lexical_absolute(right)
    if _path_key(first) == _path_key(second):
        return True
    try:
        return os.path.samefile(first, second)
    except (FileNotFoundError, OSError, ValueError):
        return os.path.normcase(str(first.resolve(strict=False))) == os.path.normcase(
            str(second.resolve(strict=False))
        )


def _same_path(left: Path, right: Path) -> bool:
    """Compare one pathname across aliases without accepting another hardlink name."""

    first = _lexical_absolute(left)
    second = _lexical_absolute(right)
    if _path_key(first) == _path_key(second):
        return True
    if os.path.normcase(first.name) != os.path.normcase(second.name):
        return False
    return _same_directory(first.parent, second.parent)


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


def _load_v4_bound_context(
    contract_snapshot: FileSnapshot,
    contract_raw: Mapping[str, Any],
) -> BoundContext:
    try:
        supervisor_v4 = importlib.import_module("supervise_ipmsm_v2_pipeline_v4")
        contract = supervisor_v4.load_contract(contract_snapshot.path)
        supervisor_v4.audit_contract(contract)
    except (ImportError, ValueError, RuntimeError, OSError, AttributeError) as exc:
        raise OptimizationInputConfirmationError(f"v4 pipeline contract audit failed: {exc}") from exc
    if not _same_path(Path(contract.source), contract_snapshot.path):
        raise OptimizationInputConfirmationError("loaded v4 contract source differs from audited path")
    if contract.source_sha256 != contract_snapshot.sha256:
        raise OptimizationInputConfirmationError("loaded v4 contract raw SHA256 differs")

    base = load_bound_context(contract.base_contract.source)
    helper_path = Path(__file__).resolve(strict=True)
    helper_snapshot = read_stable_snapshot(helper_path, "confirmation helper")
    helper_pin = contract.source_pins.get("confirmation_helper")
    if (
        helper_pin is None
        or not _same_path(Path(helper_pin.path), helper_snapshot.path)
        or helper_pin.sha256 != helper_snapshot.sha256
    ):
        raise OptimizationInputConfirmationError(
            "loaded confirmation helper differs from the v4 source pin"
        )

    base_snapshot = base.snapshots[0]
    replay_snapshot, base_document = _read_json_snapshot(
        base_snapshot.path, "v4 base pipeline contract"
    )
    if (
        replay_snapshot.sha256 != base_snapshot.sha256
        or replay_snapshot.identity != base_snapshot.identity
    ):
        raise OptimizationInputConfirmationError("v4 base pipeline contract changed")
    base_canonical_sha256 = supervisor_v4.v3._canonical_sha256(base_document)
    contract_canonical_sha256 = supervisor_v4.v3._canonical_sha256(contract_raw)
    if contract_canonical_sha256 != contract.canonical_sha256:
        raise OptimizationInputConfirmationError("v4 contract canonical SHA256 differs")
    base_binding = {
        "path": str(base_snapshot.path),
        "file_sha256": base_snapshot.sha256,
        "canonical_sha256": base_canonical_sha256,
        "contract_sha256": base.contract_sha256,
    }
    expected_base = contract.base_contract_binding
    if (
        not _same_path(Path(expected_base.path), base_snapshot.path)
        or expected_base.sha256 != base_snapshot.sha256
        or expected_base.canonical_sha256 != base_binding["canonical_sha256"]
        or expected_base.contract_sha256 != base.contract_sha256
    ):
        raise OptimizationInputConfirmationError("v4 base-contract authority differs")

    snapshots = (contract_snapshot, *base.snapshots, helper_snapshot)
    for snapshot in snapshots:
        assert_snapshot_unchanged(snapshot)
    return BoundContext(
        contract_path=contract_snapshot.path,
        contract_file_sha256=contract_snapshot.sha256,
        contract_sha256=contract.contract_sha256,
        spec_path=base.spec_path,
        spec_sha256=base.spec_sha256,
        spec_canonical_sha256=base.spec_canonical_sha256,
        spec=base.spec,
        spec_assumptions=base.spec_assumptions,
        implementation_path=base.implementation_path,
        implementation_sha256=base.implementation_sha256,
        snapshots=snapshots,
        contract_canonical_sha256=contract_canonical_sha256,
        base_contract_binding=base_binding,
    )


def load_bound_context(contract_path: str | Path) -> BoundContext:
    contract_snapshot, contract_raw = _read_json_snapshot(contract_path, "pipeline contract")
    if contract_raw.get("schema_version") == "ipmsm-v2-pipeline-contract-v4":
        return _load_v4_bound_context(contract_snapshot, contract_raw)
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
        contract_canonical_sha256=supervisor._canonical_sha256(contract_raw),
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

    contract_binding = {
        "path": str(context.contract_path),
        "file_sha256": context.contract_file_sha256,
        "contract_sha256": context.contract_sha256,
    }
    if context.contract_canonical_sha256:
        contract_binding["canonical_sha256"] = context.contract_canonical_sha256
    bindings = {
        "contract": contract_binding,
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
    if context.base_contract_binding is not None:
        bindings["base_contract"] = dict(context.base_contract_binding)
    return bindings


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


def confirmation_proof_path(output: str | Path) -> Path:
    destination = _lexical_absolute(output)
    return destination.with_name(f".{destination.name}{CONFIRMATION_PROOF_SUFFIX}")


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def confirmation_attempt_path(output: str | Path, payload: bytes) -> Path:
    destination = _lexical_absolute(output)
    return destination.with_name(
        f".{destination.name}{CONFIRMATION_ATTEMPT_MARKER}{_payload_sha256(payload)}"
    )


def confirmation_staged_path(output: str | Path, payload: bytes) -> Path:
    destination = _lexical_absolute(output)
    return destination.with_name(
        f".{destination.name}.{_payload_sha256(payload)[:32]}{CONFIRMATION_STAGED_SUFFIX}"
    )


def _attempt_candidates(destination: Path) -> tuple[Path, ...]:
    parent = destination.parent
    if not parent.exists():
        return ()
    _reject_link_components(parent, "confirmation output parent")
    if not parent.is_dir():
        raise OptimizationInputConfirmationError(
            f"confirmation output parent is not a directory: {parent}"
        )
    prefix = f".{destination.name}{CONFIRMATION_ATTEMPT_MARKER}"
    return tuple(sorted(path for path in parent.iterdir() if path.name.startswith(prefix)))


def _empty_directory_identity(path: Path, label: str) -> tuple[int, int, int]:
    _reject_link_components(path, label)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise OptimizationInputConfirmationError(
            f"cannot inspect {label}: {path}"
        ) from exc
    if not stat.S_ISDIR(before.st_mode) or _path_is_link_or_reparse(path):
        raise OptimizationInputConfirmationError(
            f"{label} is not a regular no-follow directory"
        )
    try:
        entries = tuple(path.iterdir())
    except OSError as exc:
        raise OptimizationInputConfirmationError(
            f"cannot enumerate {label}"
        ) from exc
    if entries:
        raise OptimizationInputConfirmationError(
            f"{label} must remain empty"
        )
    after = os.lstat(path)
    first = int(before.st_dev), int(before.st_ino), int(before.st_mtime_ns)
    second = int(after.st_dev), int(after.st_ino), int(after.st_mtime_ns)
    if first != second:
        raise OptimizationInputConfirmationError(
            f"{label} changed during inspection"
        )
    return first


def _inspect_attempt(path: Path) -> _ConfirmationAttempt:
    _reject_link_components(path, "confirmation attempt journal")
    try:
        before = os.lstat(path)
        entries = tuple(path.iterdir())
    except OSError as exc:
        raise OptimizationInputConfirmationError(
            f"cannot inspect confirmation attempt journal: {path}"
        ) from exc
    if not stat.S_ISDIR(before.st_mode) or _path_is_link_or_reparse(path):
        raise OptimizationInputConfirmationError(
            "confirmation attempt journal is not a regular no-follow directory"
        )
    ready_path = path / CONFIRMATION_STAGE_READY_NAME
    if not entries:
        ready_identity = None
    elif len(entries) == 1 and _same_path(entries[0], ready_path):
        ready_identity = _empty_directory_identity(
            ready_path, "confirmation stage-ready marker"
        )
    else:
        raise OptimizationInputConfirmationError(
            "confirmation attempt journal contains an unauthorized entry"
        )
    after = os.lstat(path)
    identity = int(after.st_dev), int(after.st_ino), int(after.st_mtime_ns)
    before_identity = int(before.st_dev), int(before.st_ino), int(before.st_mtime_ns)
    if identity != before_identity:
        raise OptimizationInputConfirmationError(
            "confirmation attempt journal changed during inspection"
        )
    return _ConfirmationAttempt(
        path=path,
        identity=identity,
        stage_ready=ready_identity is not None,
        stage_ready_identity=ready_identity,
    )


def _staged_name_allowed(source: Path, destination: Path) -> bool:
    if not _same_directory(source.parent, destination.parent):
        return False
    prefix = f".{destination.name}."
    if not source.name.startswith(prefix) or not source.name.endswith(
        CONFIRMATION_STAGED_SUFFIX
    ):
        return False
    token = source.name[len(prefix) : -len(CONFIRMATION_STAGED_SUFFIX)]
    return len(token) == 32 and all(character in "0123456789abcdef" for character in token)


def _staged_candidates(destination: Path) -> tuple[Path, ...]:
    parent = destination.parent
    if not parent.exists():
        return ()
    _reject_link_components(parent, "confirmation output parent")
    if not parent.is_dir():
        raise OptimizationInputConfirmationError(
            f"confirmation output parent is not a directory: {parent}"
        )
    return tuple(
        sorted(
            path
            for path in parent.iterdir()
            if _staged_name_allowed(path, destination)
        )
    )


def _proof_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Replay the exact format emitted by ``atomic_publish``."""

    return (
        json.dumps(dict(value), indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _parse_confirmation_proof(
    proof_path: Path, destination: Path
) -> _ConfirmationPublicationProof:
    snapshot, raw = _read_json_snapshot(proof_path, "confirmation publication proof")
    _expect_keys(raw, {"schema_version", "source", "destination", "identity"}, "proof")
    if raw["schema_version"] != PROOF_SCHEMA_VERSION:
        raise OptimizationInputConfirmationError(
            "unsupported confirmation publication proof schema_version"
        )
    if snapshot.payload != _proof_json_bytes(raw):
        raise OptimizationInputConfirmationError(
            "confirmation publication proof is not canonical atomic proof bytes"
        )
    source_value = _nonblank(raw["source"], "proof.source")
    destination_value = _nonblank(raw["destination"], "proof.destination")
    source_raw = Path(source_value)
    destination_raw = Path(destination_value)
    if not source_raw.is_absolute() or not destination_raw.is_absolute():
        raise OptimizationInputConfirmationError("publication proof paths must be absolute")
    source = _lexical_absolute(source_raw)
    proof_destination = _lexical_absolute(destination_raw)
    if not _same_path(proof_destination, destination):
        raise OptimizationInputConfirmationError("publication proof destination mismatch")
    if not _staged_name_allowed(source, proof_destination):
        raise OptimizationInputConfirmationError(
            "publication proof source is outside the staging allow-list"
        )
    expected_proof_path = confirmation_proof_path(proof_destination)
    if not _same_path(snapshot.path, expected_proof_path):
        raise OptimizationInputConfirmationError(
            "publication proof path does not match its destination"
        )
    identity_raw = _mapping(raw["identity"], "proof.identity")
    try:
        identity = FileIdentity.from_mapping(identity_raw)
    except (TypeError, ValueError) as exc:
        raise OptimizationInputConfirmationError(
            "confirmation publication proof identity is invalid"
        ) from exc
    assert_snapshot_unchanged(snapshot)
    return _ConfirmationPublicationProof(
        proof_path=snapshot.path,
        source=source,
        destination=proof_destination,
        identity=identity,
        payload=snapshot.payload,
        proof_identity=snapshot.identity,
    )


def _identity_at(path: Path) -> FileIdentity | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OptimizationInputConfirmationError(
            f"cannot inspect confirmation recovery path: {path}"
        ) from exc
    if not stat.S_ISREG(info.st_mode) or _path_is_link_or_reparse(path):
        raise OptimizationInputConfirmationError(
            f"confirmation recovery path is not a regular no-follow file: {path}"
        )
    return FileIdentity(int(info.st_dev), int(info.st_ino), int(info.st_size))


def _recovery_stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(getattr(info, "st_nlink", 1)),
    )


def _read_proof_owned_payload(
    path: Path,
    identity: FileIdentity,
    *,
    expected_links: int,
) -> bytes:
    if expected_links not in {1, 2}:
        raise OptimizationInputConfirmationError(
            "confirmation recovery link expectation is invalid"
        )
    _reject_link_components(path, "proof-owned confirmation path")
    pathname_before = os.lstat(path)
    if int(getattr(pathname_before, "st_nlink", 1)) != expected_links:
        raise OptimizationInputConfirmationError(
            "confirmation recovery hardlink ownership is ambiguous"
        )
    if _identity_at(path) != identity:
        raise OptimizationInputConfirmationError(
            "proof-owned confirmation pathname identity changed"
        )
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_BINARY", 0))
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OptimizationInputConfirmationError(
            f"cannot open proof-owned confirmation path: {path}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if int(getattr(opened, "st_nlink", 1)) != expected_links:
            raise OptimizationInputConfirmationError(
                "confirmation recovery hardlink ownership changed while opening"
            )
        if FileIdentity(
            int(opened.st_dev), int(opened.st_ino), int(opened.st_size)
        ) != identity:
            raise OptimizationInputConfirmationError(
                "proof-owned confirmation identity changed while opening"
            )
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
        int(getattr(info, "st_nlink", 1)) != expected_links
        for info in (after, pathname_after)
    ):
        raise OptimizationInputConfirmationError(
            "confirmation recovery hardlink ownership changed while reading"
        )
    if not (
        _recovery_stat_identity(pathname_before)
        == _recovery_stat_identity(opened)
        == _recovery_stat_identity(after)
        == _recovery_stat_identity(pathname_after)
    ):
        raise OptimizationInputConfirmationError(
            "proof-owned confirmation changed while being read"
        )
    payload = b"".join(chunks)
    if len(payload) != identity.size:
        raise OptimizationInputConfirmationError(
            "proof-owned confirmation payload size changed"
        )
    return payload


def _declaration_from_snapshot(snapshot: FileSnapshot) -> dict[str, Any]:
    try:
        value = json.loads(
            snapshot.payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OptimizationInputConfirmationError(
            "bound optimization-input declaration is no longer valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise OptimizationInputConfirmationError(
            "bound optimization-input declaration must be a JSON object"
        )
    return value


def _expected_confirmation_payload(
    document: Mapping[str, Any],
    context: BoundContext,
    declaration_snapshot: FileSnapshot,
) -> bytes:
    for snapshot in (*context.snapshots, declaration_snapshot):
        assert_snapshot_unchanged(snapshot)
    rebuilt = build_confirmation(
        context,
        _declaration_from_snapshot(declaration_snapshot),
        declaration_path=declaration_snapshot.path,
        declaration_sha256=declaration_snapshot.sha256,
    )
    try:
        supplied = canonical_json_bytes(document)
    except (TypeError, ValueError) as exc:
        raise OptimizationInputConfirmationError(
            "proposed confirmation cannot be encoded as canonical JSON"
        ) from exc
    expected = canonical_json_bytes(rebuilt)
    if supplied != expected:
        raise OptimizationInputConfirmationError(
            "proposed confirmation differs from the live bound declaration"
        )
    for snapshot in (*context.snapshots, declaration_snapshot):
        assert_snapshot_unchanged(snapshot)
    return expected


def _confirmation_destination(
    output: str | Path,
    context: BoundContext,
    declaration_snapshot: FileSnapshot,
) -> Path:
    destination = _lexical_absolute(output)
    _reject_link_components(destination, "confirmation output")
    protected = (
        *[snapshot.path for snapshot in context.snapshots],
        context.contract_path,
        context.spec_path,
        context.implementation_path,
        declaration_snapshot.path,
    )
    if any(_same_path(destination, path) for path in protected):
        raise OptimizationInputConfirmationError("output must not alias a bound input")
    return destination


def _pending_state(
    proof: _ConfirmationPublicationProof,
    expected_payload: bytes,
) -> str:
    source_identity = _identity_at(proof.source)
    destination_identity = _identity_at(proof.destination)
    if source_identity is not None and source_identity != proof.identity:
        raise OptimizationInputConfirmationError(
            "confirmation staging identity differs from its proof"
        )
    if destination_identity is not None and destination_identity != proof.identity:
        raise OptimizationInputConfirmationError(
            "existing confirmation is not owned by its recovery proof"
        )
    live_paths = tuple(
        path
        for path, identity in (
            (proof.source, source_identity),
            (proof.destination, destination_identity),
        )
        if identity is not None
    )
    if not live_paths:
        raise OptimizationInputConfirmationError(
            "confirmation proof owns neither staging nor destination inode"
        )
    expected_links = len(live_paths)
    for path in live_paths:
        if (
            _read_proof_owned_payload(
                path, proof.identity, expected_links=expected_links
            )
            != expected_payload
        ):
            raise OptimizationInputConfirmationError(
                "proof-owned confirmation bytes differ from current authority"
            )
    current = _parse_confirmation_proof(proof.proof_path, proof.destination)
    if current != proof:
        raise OptimizationInputConfirmationError(
            "confirmation publication proof changed during inspection"
        )
    if source_identity is not None and destination_identity is None:
        return "pre_commit"
    if source_identity is not None and destination_identity is not None:
        return "post_commit_stage_linked"
    return "post_commit_stage_unlinked"


def _single_link_payload(path: Path, label: str) -> tuple[FileIdentity, bytes]:
    identity = _identity_at(path)
    if identity is None:
        raise OptimizationInputConfirmationError(f"{label} disappeared")
    if int(getattr(os.lstat(path), "st_nlink", 1)) != 1:
        raise OptimizationInputConfirmationError(f"{label} has a foreign hardlink")
    return identity, _read_proof_owned_payload(path, identity, expected_links=1)


def _inspect_confirmation_publication_state(
    output: str | Path,
    document: Mapping[str, Any],
    context: BoundContext,
    declaration_snapshot: FileSnapshot,
) -> _ConfirmationPublicationState:
    destination = _confirmation_destination(output, context, declaration_snapshot)
    proof_path = confirmation_proof_path(destination)
    expected_payload = _expected_confirmation_payload(
        document, context, declaration_snapshot
    )
    expected_attempt_path = confirmation_attempt_path(destination, expected_payload)
    attempt_candidates = _attempt_candidates(destination)
    if len(attempt_candidates) > 1 or (
        attempt_candidates
        and not _same_path(attempt_candidates[0], expected_attempt_path)
    ):
        raise OptimizationInputConfirmationError(
            "confirmation attempt journal does not match current authority"
        )
    attempt = (
        _inspect_attempt(attempt_candidates[0]) if attempt_candidates else None
    )
    expected_staged = confirmation_staged_path(destination, expected_payload)
    candidates = _staged_candidates(destination)
    if len(candidates) > 1 or (
        candidates and not _same_path(candidates[0], expected_staged)
    ):
        raise OptimizationInputConfirmationError(
            "confirmation staging path does not match current authority"
        )
    staged = candidates[0] if candidates else None
    if os.path.lexists(proof_path):
        proof_identity, proof_payload = _single_link_payload(
            proof_path, "confirmation publication proof"
        )
        try:
            decoded_proof = json.loads(proof_payload.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            decoded_proof = None
        if decoded_proof is None:
            if (
                attempt is None
                or not attempt.stage_ready
                or staged is None
                or os.path.lexists(destination)
            ):
                raise OptimizationInputConfirmationError(
                    "incomplete confirmation proof lacks its sealed attempt authority"
                )
            staged_identity, staged_payload = _single_link_payload(
                staged, "sealed confirmation staging path"
            )
            if staged_payload != expected_payload:
                raise OptimizationInputConfirmationError(
                    "sealed confirmation staging bytes differ from current authority"
                )
            expected_proof_payload = _proof_json_bytes(
                {
                    "schema_version": PROOF_SCHEMA_VERSION,
                    "source": str(expected_staged),
                    "destination": str(destination),
                    "identity": staged_identity.as_mapping(),
                }
            )
            if not expected_proof_payload.startswith(proof_payload):
                raise OptimizationInputConfirmationError(
                    "invalid confirmation proof is not a durable-write prefix"
                )
            return _ConfirmationPublicationState(
                inspection=ConfirmationPublicationInspection(
                    status="publication_recovery_pending",
                    destination=destination,
                    proof_path=proof_path,
                    pending_state="pre_commit_proof_incomplete",
                ),
                expected_payload=expected_payload,
                attempt=attempt,
                staged_path=staged,
                staged_identity=staged_identity,
                incomplete_proof_identity=proof_identity,
            )
        proof = _parse_confirmation_proof(proof_path, destination)
        if not _same_path(proof.source, expected_staged):
            raise OptimizationInputConfirmationError(
                "publication proof source differs from the deterministic staging path"
            )
        if staged is not None and not _same_path(staged, proof.source):
            raise OptimizationInputConfirmationError(
                "unproven confirmation staging path exists beside publication proof"
            )
        pending = _pending_state(proof, expected_payload)
        if pending in {"pre_commit", "post_commit_stage_linked"} and (
            attempt is None or not attempt.stage_ready
        ):
            raise OptimizationInputConfirmationError(
                "proof-owned confirmation staging lacks its sealed attempt journal"
            )
        for snapshot in (*context.snapshots, declaration_snapshot):
            assert_snapshot_unchanged(snapshot)
        return _ConfirmationPublicationState(
            inspection=ConfirmationPublicationInspection(
                status="publication_recovery_pending",
                destination=destination,
                proof_path=proof.proof_path,
                pending_state=pending,
            ),
            expected_payload=expected_payload,
            proof=proof,
            attempt=attempt,
            staged_path=staged,
        )
    if os.path.lexists(destination):
        if staged is not None:
            raise OptimizationInputConfirmationError(
                "proofless confirmation destination has a staging artifact"
            )
        snapshot = read_stable_snapshot(destination, "committed optimization confirmation")
        if snapshot.payload != expected_payload:
            raise OptimizationInputConfirmationError(
                "existing confirmation differs from current authority"
            )
        audit = _audit_confirmation_with_context(snapshot.path, context)
        for item in (*context.snapshots, declaration_snapshot, snapshot):
            assert_snapshot_unchanged(item)
        if attempt is not None:
            if attempt.stage_ready:
                raise OptimizationInputConfirmationError(
                    "committed confirmation has a sealed foreign attempt journal"
                )
            return _ConfirmationPublicationState(
                inspection=ConfirmationPublicationInspection(
                    status="publication_recovery_pending",
                    destination=destination,
                    proof_path=proof_path,
                    pending_state="committed_attempt_cleanup",
                    audit=audit,
                ),
                expected_payload=expected_payload,
                attempt=attempt,
            )
        return _ConfirmationPublicationState(
            inspection=ConfirmationPublicationInspection(
                status="committed",
                destination=destination,
                proof_path=proof_path,
                audit=audit,
            ),
            expected_payload=expected_payload,
        )
    if attempt is not None:
        if attempt.stage_ready:
            if staged is None:
                raise OptimizationInputConfirmationError(
                    "sealed confirmation staging path is missing"
                )
            staged_identity, staged_payload = _single_link_payload(
                staged, "sealed confirmation staging path"
            )
            if staged_payload != expected_payload:
                raise OptimizationInputConfirmationError(
                    "sealed confirmation staging bytes differ from current authority"
                )
            pending = "pre_commit"
        elif staged is None:
            staged_identity = None
            pending = "pre_stage"
        else:
            staged_identity, _ = _single_link_payload(
                staged, "unsealed confirmation staging path"
            )
            pending = "pre_stage_incomplete"
        return _ConfirmationPublicationState(
            inspection=ConfirmationPublicationInspection(
                status="publication_recovery_pending",
                destination=destination,
                proof_path=proof_path,
                pending_state=pending,
            ),
            expected_payload=expected_payload,
            attempt=attempt,
            staged_path=staged,
            staged_identity=staged_identity,
        )
    if staged is not None:
        raise OptimizationInputConfirmationError(
            "unproven confirmation staging orphan exists without attempt journal"
        )
    return _ConfirmationPublicationState(
        inspection=ConfirmationPublicationInspection(
            status="absent",
            destination=destination,
            proof_path=proof_path,
        ),
        expected_payload=expected_payload,
    )


def inspect_confirmation_publication(
    output: str | Path,
    document: Mapping[str, Any],
    context: BoundContext,
    declaration_snapshot: FileSnapshot,
) -> ConfirmationPublicationInspection:
    """Audit publication/recovery state without creating or changing any path."""

    return _inspect_confirmation_publication_state(
        output, document, context, declaration_snapshot
    ).inspection


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _create_confirmation_attempt(path: Path) -> _ConfirmationAttempt:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise OptimizationInputConfirmationError(
            "confirmation attempt journal already exists"
        ) from exc
    except OSError as exc:
        raise OptimizationInputConfirmationError(
            "cannot create confirmation attempt journal"
        ) from exc
    _fsync_directory(path.parent)
    return _inspect_attempt(path)


def _create_confirmation_stage_ready(
    expected: _ConfirmationAttempt,
) -> _ConfirmationAttempt:
    current = _inspect_attempt(expected.path)
    if current != expected or current.stage_ready:
        raise OptimizationInputConfirmationError(
            "confirmation attempt journal changed before stage sealing"
        )
    ready = expected.path / CONFIRMATION_STAGE_READY_NAME
    try:
        os.mkdir(ready, 0o700)
    except FileExistsError as exc:
        raise OptimizationInputConfirmationError(
            "confirmation stage-ready marker already exists"
        ) from exc
    except OSError as exc:
        raise OptimizationInputConfirmationError(
            "cannot create confirmation stage-ready marker"
        ) from exc
    _fsync_directory(expected.path)
    return _inspect_attempt(expected.path)


def _remove_confirmation_attempt(expected: _ConfirmationAttempt) -> None:
    current = _inspect_attempt(expected.path)
    if current != expected:
        raise OptimizationInputConfirmationError(
            "confirmation attempt journal changed before cleanup"
        )
    if current.stage_ready:
        ready = current.path / CONFIRMATION_STAGE_READY_NAME
        try:
            ready.rmdir()
        except OSError as exc:
            raise OptimizationInputConfirmationError(
                "cannot remove confirmation stage-ready marker"
            ) from exc
        if os.path.lexists(ready):
            raise OptimizationInputConfirmationError(
                "confirmation stage-ready marker survived cleanup"
            )
        _fsync_directory(current.path)
        current = _inspect_attempt(current.path)
    try:
        current.path.rmdir()
    except OSError as exc:
        raise OptimizationInputConfirmationError(
            "cannot remove confirmation attempt journal"
        ) from exc
    if os.path.lexists(current.path):
        raise OptimizationInputConfirmationError(
            "confirmation attempt journal survived cleanup"
        )
    _fsync_directory(current.path.parent)


def _resume_confirmation_commit(proof: _ConfirmationPublicationProof) -> None:
    if _identity_at(proof.source) != proof.identity:
        raise OptimizationInputConfirmationError(
            "orphan confirmation staging inode is no longer proof-owned"
        )
    if int(getattr(os.lstat(proof.source), "st_nlink", 1)) != 1:
        raise OptimizationInputConfirmationError(
            "orphan confirmation staging hardlink ownership is ambiguous"
        )
    if os.path.lexists(proof.destination):
        raise OptimizationInputConfirmationError(
            "confirmation destination appeared before orphan commit recovery"
        )
    try:
        if atomic_publish._is_windows_remote_path(
            proof.source
        ) or atomic_publish._is_windows_remote_path(proof.destination):
            atomic_publish._windows_rename_no_replace(proof.source, proof.destination)
        else:
            try:
                os.link(proof.source, proof.destination)
            except FileExistsError:
                raise
            except OSError as exc:
                if not atomic_publish._is_windows_hardlink_not_supported(exc):
                    raise
                atomic_publish._windows_rename_no_replace(
                    proof.source, proof.destination
                )
    except FileExistsError as exc:
        raise OptimizationInputConfirmationError(
            "confirmation orphan commit recovery raced with another destination"
        ) from exc
    except OSError as exc:
        raise OptimizationInputConfirmationError(
            "cannot resume proof-owned confirmation commit"
        ) from exc
    if _identity_at(proof.destination) != proof.identity:
        raise OptimizationInputConfirmationError(
            "resumed confirmation destination differs from its proof"
        )
    source_identity = _identity_at(proof.source)
    if source_identity is not None and source_identity != proof.identity:
        raise OptimizationInputConfirmationError(
            "resumed confirmation staging inode differs from its proof"
        )
    _fsync_directory(proof.destination.parent)


def _restore_confirmation_proof(proof: _ConfirmationPublicationProof) -> None:
    if os.path.lexists(proof.proof_path):
        return
    atomic_publish._write_proof_exclusive(
        proof.proof_path,
        source=proof.source,
        destination=proof.destination,
        identity=proof.identity,
    )


def _unlink_confirmation_stage(proof: _ConfirmationPublicationProof) -> None:
    proof.source.unlink()


def _unlink_confirmation_proof(proof: _ConfirmationPublicationProof) -> None:
    proof.proof_path.unlink()


def _recover_confirmation_publication(
    output: str | Path,
    document: Mapping[str, Any],
    context: BoundContext,
    declaration_snapshot: FileSnapshot,
) -> ConfirmationAudit:
    state = _inspect_confirmation_publication_state(
        output, document, context, declaration_snapshot
    )
    inspection = state.inspection
    if inspection.status == "committed":
        assert inspection.audit is not None
        return inspection.audit
    if inspection.status != "publication_recovery_pending":
        raise OptimizationInputConfirmationError(
            "confirmation recovery requested without durable transaction authority"
        )
    pending = inspection.pending_state
    if state.proof is None:
        if pending == "committed_attempt_cleanup":
            if state.attempt is None or state.attempt.stage_ready:
                raise OptimizationInputConfirmationError(
                    "committed-attempt cleanup lacks an exact empty journal"
                )
            current = _inspect_confirmation_publication_state(
                output, document, context, declaration_snapshot
            )
            if (
                current.inspection.pending_state != pending
                or current.attempt != state.attempt
                or current.inspection.audit is None
            ):
                raise OptimizationInputConfirmationError(
                    "committed confirmation attempt changed before cleanup"
                )
            _remove_confirmation_attempt(current.attempt)
            committed = _inspect_confirmation_publication_state(
                output, document, context, declaration_snapshot
            )
            if (
                committed.inspection.status != "committed"
                or committed.inspection.audit is None
            ):
                raise OptimizationInputConfirmationError(
                    "confirmation changed across committed-attempt cleanup"
                )
            return committed.inspection.audit
        if pending == "pre_stage":
            if state.attempt is None:
                raise OptimizationInputConfirmationError(
                    "pre-stage recovery lacks its attempt journal"
                )
            _stage_confirmation(
                inspection.destination, state.expected_payload, state.attempt
            )
            return _recover_confirmation_publication(
                output, document, context, declaration_snapshot
            )
        if pending == "pre_stage_incomplete":
            current = _inspect_confirmation_publication_state(
                output, document, context, declaration_snapshot
            )
            if (
                current.inspection.pending_state != pending
                or current.attempt != state.attempt
                or current.staged_path != state.staged_path
                or current.staged_identity != state.staged_identity
                or current.staged_path is None
            ):
                raise OptimizationInputConfirmationError(
                    "unsealed confirmation staging changed before replay"
                )
            current.staged_path.unlink()
            if os.path.lexists(current.staged_path):
                raise OptimizationInputConfirmationError(
                    "unsealed confirmation staging survived replay cleanup"
                )
            _fsync_directory(current.staged_path.parent)
            return _recover_confirmation_publication(
                output, document, context, declaration_snapshot
            )
        if pending == "pre_commit_proof_incomplete":
            current = _inspect_confirmation_publication_state(
                output, document, context, declaration_snapshot
            )
            if (
                current.inspection.pending_state != pending
                or current.attempt != state.attempt
                or current.staged_identity != state.staged_identity
                or current.incomplete_proof_identity
                != state.incomplete_proof_identity
                or current.incomplete_proof_identity is None
            ):
                raise OptimizationInputConfirmationError(
                    "incomplete confirmation proof changed before replay"
                )
            current.inspection.proof_path.unlink()
            if os.path.lexists(current.inspection.proof_path):
                raise OptimizationInputConfirmationError(
                    "incomplete confirmation proof survived replay cleanup"
                )
            _fsync_directory(current.inspection.proof_path.parent)
            return _recover_confirmation_publication(
                output, document, context, declaration_snapshot
            )
        if pending == "pre_commit":
            if (
                state.attempt is None
                or not state.attempt.stage_ready
                or state.staged_path is None
            ):
                raise OptimizationInputConfirmationError(
                    "pre-commit confirmation lacks a sealed staging attempt"
                )
            publish_no_replace(
                state.staged_path,
                inspection.destination,
                proof_path=inspection.proof_path,
            )
            return _recover_confirmation_publication(
                output, document, context, declaration_snapshot
            )
        raise OptimizationInputConfirmationError(
            f"unsupported proofless confirmation recovery state: {pending}"
        )

    proof = state.proof
    if pending == "pre_commit":
        _resume_confirmation_commit(proof)
        return _recover_confirmation_publication(
            output, document, context, declaration_snapshot
        )
    if pending == "post_commit_stage_linked":
        current = _inspect_confirmation_publication_state(
            output, document, context, declaration_snapshot
        )
        if current.proof != proof or current.inspection.pending_state != pending:
            raise OptimizationInputConfirmationError(
                "confirmation publication changed before staging cleanup"
            )
        _unlink_confirmation_stage(proof)
        if os.path.lexists(proof.source):
            raise OptimizationInputConfirmationError(
                "confirmation staging link survived recovery cleanup"
            )
        if _identity_at(proof.destination) != proof.identity:
            raise OptimizationInputConfirmationError(
                "confirmation destination changed across staging cleanup"
            )
        _fsync_directory(proof.destination.parent)
        return _recover_confirmation_publication(
            output, document, context, declaration_snapshot
        )
    if pending != "post_commit_stage_unlinked":
        raise OptimizationInputConfirmationError(
            f"unsupported confirmation recovery state: {pending}"
        )

    audit = _audit_confirmation_with_context(proof.destination, context)
    for snapshot in (*context.snapshots, declaration_snapshot):
        assert_snapshot_unchanged(snapshot)
    current = _inspect_confirmation_publication_state(
        output, document, context, declaration_snapshot
    )
    if current.proof != proof or current.inspection.pending_state != pending:
        raise OptimizationInputConfirmationError(
            "confirmation publication changed before proof cleanup"
        )
    if current.attempt is not None:
        _remove_confirmation_attempt(current.attempt)
        current = _inspect_confirmation_publication_state(
            output, document, context, declaration_snapshot
        )
        if current.proof != proof or current.inspection.pending_state != pending:
            raise OptimizationInputConfirmationError(
                "confirmation publication changed across attempt cleanup"
            )
    _unlink_confirmation_proof(proof)
    if os.path.lexists(proof.proof_path):
        raise OptimizationInputConfirmationError(
            "confirmation publication proof survived cleanup"
        )
    try:
        committed = _inspect_confirmation_publication_state(
            output, document, context, declaration_snapshot
        )
        if committed.inspection.status != "committed" or committed.inspection.audit is None:
            raise OptimizationInputConfirmationError(
                "confirmation did not become committed after proof cleanup"
            )
    except BaseException as exc:
        try:
            _restore_confirmation_proof(proof)
        except OSError as restore_exc:
            raise OptimizationInputConfirmationError(
                "confirmation ownership changed and recovery proof restoration failed"
            ) from restore_exc
        raise OptimizationInputConfirmationError(
            "confirmation ownership changed across final proof cleanup"
        ) from exc
    _fsync_directory(proof.destination.parent)
    return committed.inspection.audit


def _stage_confirmation(
    destination: Path,
    payload: bytes,
    attempt: _ConfirmationAttempt,
) -> tuple[Path, _ConfirmationAttempt]:
    current_attempt = _inspect_attempt(attempt.path)
    if current_attempt != attempt or current_attempt.stage_ready:
        raise OptimizationInputConfirmationError(
            "confirmation attempt changed before staging"
        )
    staged = confirmation_staged_path(destination, payload)
    if os.path.lexists(staged):
        raise OptimizationInputConfirmationError(
            "confirmation staging path already exists before staging"
        )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_BINARY", 0))
    )
    try:
        descriptor = os.open(staged, flags, 0o600)
        _identity_from_stat(os.fstat(descriptor), staged)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    _, staged_payload = _single_link_payload(staged, "new confirmation staging path")
    if staged_payload != payload or _inspect_attempt(attempt.path) != attempt:
        raise OptimizationInputConfirmationError(
            "confirmation staging authority changed before sealing"
        )
    sealed = _create_confirmation_stage_ready(attempt)
    return staged, sealed


def publish_confirmation_with_outcome(
    output: str | Path,
    document: Mapping[str, Any],
    context: BoundContext,
    declaration_snapshot: FileSnapshot,
) -> ConfirmationPublicationResult:
    initial = _inspect_confirmation_publication_state(
        output, document, context, declaration_snapshot
    )
    if initial.inspection.status == "committed":
        assert initial.inspection.audit is not None
        return ConfirmationPublicationResult(
            audit=initial.inspection.audit,
            outcome="already_present",
            mutated=False,
        )
    if initial.inspection.status == "publication_recovery_pending":
        audit = _recover_confirmation_publication(
            output, document, context, declaration_snapshot
        )
        if initial.inspection.pending_state == "committed_attempt_cleanup":
            return ConfirmationPublicationResult(
                audit=audit,
                outcome="already_present",
                mutated=False,
                recovery_state=initial.inspection.pending_state,
            )
        return ConfirmationPublicationResult(
            audit=audit,
            outcome="recovered",
            mutated=True,
            recovery_state=initial.inspection.pending_state,
        )
    destination = initial.inspection.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_link_components(destination.parent, "confirmation output parent")
    if not destination.parent.is_dir():
        raise OptimizationInputConfirmationError(
            f"confirmation output parent is not a directory: {destination.parent}"
        )
    refreshed = _inspect_confirmation_publication_state(
        output, document, context, declaration_snapshot
    )
    if refreshed.inspection.status != "absent":
        return publish_confirmation_with_outcome(
            output, document, context, declaration_snapshot
        )
    attempt_path = confirmation_attempt_path(destination, refreshed.expected_payload)
    try:
        acquired_attempt = _create_confirmation_attempt(attempt_path)
    except OptimizationInputConfirmationError:
        if not os.path.lexists(attempt_path):
            raise
        raced = _inspect_confirmation_publication_state(
            output, document, context, declaration_snapshot
        )
        if raced.inspection.status != "publication_recovery_pending":
            raise OptimizationInputConfirmationError(
                "confirmation attempt creation raced with an invalid state"
            )
        audit = _recover_confirmation_publication(
            output, document, context, declaration_snapshot
        )
        if raced.inspection.pending_state == "committed_attempt_cleanup":
            return ConfirmationPublicationResult(
                audit=audit,
                outcome="already_present",
                mutated=False,
                recovery_state=raced.inspection.pending_state,
            )
        return ConfirmationPublicationResult(
            audit=audit,
            outcome="recovered",
            mutated=True,
            recovery_state=raced.inspection.pending_state,
        )
    acquired_state = _inspect_confirmation_publication_state(
        output, document, context, declaration_snapshot
    )
    if acquired_state.inspection.status == "committed":
        assert acquired_state.inspection.audit is not None
        return ConfirmationPublicationResult(
            audit=acquired_state.inspection.audit,
            outcome="already_present",
            mutated=False,
        )
    if acquired_state.attempt != acquired_attempt:
        raise OptimizationInputConfirmationError(
            "confirmation attempt ownership changed immediately after acquisition"
        )
    lost_publication_race = os.path.lexists(destination)
    audit = _recover_confirmation_publication(
        output, document, context, declaration_snapshot
    )
    if lost_publication_race:
        return ConfirmationPublicationResult(
            audit=audit,
            outcome="already_present",
            mutated=False,
            recovery_state=acquired_state.inspection.pending_state,
        )
    return ConfirmationPublicationResult(
        audit=audit,
        outcome="published",
        mutated=True,
        recovery_state="pre_stage",
    )


def publish_confirmation(
    output: str | Path,
    document: Mapping[str, Any],
    context: BoundContext,
    declaration_snapshot: FileSnapshot,
) -> ConfirmationAudit:
    """Compatibility wrapper returning the strict audit after publish/recovery."""

    return publish_confirmation_with_outcome(
        output, document, context, declaration_snapshot
    ).audit


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
        publication_result = publish_confirmation_with_outcome(
            args.output, document, context, declaration_snapshot
        )
        result = {
            "mode": "execute",
            "writes_performed": publication_result.writes_performed,
            "recovered": publication_result.recovered,
            "already_present": publication_result.already_present,
            "publication_outcome": publication_result.outcome,
            **publication_result.audit.as_mapping(),
        }
        if publication_result.recovery_state is not None:
            result["recovery_state"] = publication_result.recovery_state
    else:
        publication = inspect_confirmation_publication(
            args.output, document, context, declaration_snapshot
        )
        if publication.status == "committed":
            assert publication.audit is not None
            result = {
                "mode": "dry_run",
                "writes_performed": 0,
                **publication.audit.as_mapping(),
                "status": "already_confirmed",
            }
        elif publication.status == "publication_recovery_pending":
            result = {
                "mode": "dry_run",
                "writes_performed": 0,
                **publication.as_mapping(),
                "contract_sha256": context.contract_sha256,
                "optimization_spec_sha256": context.spec_sha256,
                "confirmation_sha256": document["confirmation_sha256"],
            }
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
