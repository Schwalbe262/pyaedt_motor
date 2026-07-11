"""Generate and analyze fixed-geometry electrical-zero calibration sweeps."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from generate_ipmsm_quality_cases import MESH_ELEMENT_KEYS, QUALITY_PROFILES
from run_ipmsm_batch import extract_fixed_geometry


BETA_CONVENTION = "dq_current_advance_v2"
DATASET_SCHEMA_VERSION = "ipmsm_v2"
ZERO_CALIBRATION_METHOD = "signed_phasea_back_emf_fundamental_v2"
MTPA_VALIDATION_METHOD = "loaded_beta_sweep_v2"
BETA_SUMMARY_SCHEMA_VERSION = "beta_mtpa_summary_v1"
DEFAULT_STAGE_BETA_BOUNDS_DEG = (0.0, 80.0)
MAX_STAGE_DQ_CURRENT_RELATIVE_ERROR = 0.02
BETA_SUMMARY_IDENTITY_FIELDS = (
    "geometry_group_id",
    "design_hash",
    "setup_fingerprint",
    "material_fingerprint",
    "aedt_version",
    "quality_profile",
)
BETA_SUMMARY_POINT_FIELDS = (
    "beta_dq_deg",
    "current_peak_a",
    "speed_rpm",
    "torque_nm",
    "torque_per_peak_amp",
    "actual_id_a",
    "actual_iq_a",
    "dq_current_relative_error",
)
BETA_SUMMARY_FIELDS = (
    "summary_schema_version",
    "workflow_version",
    "method",
    "convention",
    "status",
    "pass",
    "gate_failures",
    "strict_case_plan_validation",
    "beta_calibration_id",
    "electrical_zero_deg",
    "expected_rows",
    "successful_rows",
    "tested_beta_bounds_deg",
    "tested_beta_values_deg",
    "stage_beta_bounds_deg",
    "max_dq_current_relative_error",
    "max_observed_dq_current_relative_error",
    "homogeneous_identities",
    "plan_hash",
    "result_hash",
    "best_beta_dq_deg",
    "best_torque_nm",
    "best_torque_per_peak_amp",
    "speed_rpm",
    "current_peak_a",
    "points",
    "sweep_id",
)


def parse_float_list(text: str) -> list[float]:
    values = [float(item.strip()) for item in text.split(",") if item.strip()]
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("at least one finite value is required")
    return values


def parse_beta_bounds(text: str) -> tuple[float, float]:
    values = parse_float_list(text)
    if len(values) != 2 or values[0] >= values[1]:
        raise ValueError("stage beta bounds must contain two increasing finite values")
    return values[0], values[1]


def canonical_hash(prefix: str, value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def rows_hash(prefix: str, rows: Iterable[Mapping[str, Any]]) -> str:
    return canonical_hash(prefix, [dict(row) for row in rows])


def safe_name(value: object) -> str:
    text = f"{float(value):g}" if isinstance(value, (int, float)) else str(value)
    return text.replace("-", "m").replace(".", "p")


def read_source_row(path: Path, row_index: int) -> dict[str, str]:
    if row_index < 1:
        raise ValueError("source row index must be >= 1")
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    if row_index > len(rows):
        raise ValueError(f"source row index {row_index} exceeds {len(rows)} rows")
    row = rows[row_index - 1]
    for column in ("input_slot_opening_ratio", "input_magnet_space_height_ratio"):
        if not str(row.get(column) or row.get(column.removeprefix("input_")) or "").strip():
            raise ValueError(f"source row is not v2-complete; missing {column}")
    return row


def calibration_source_from_spec(path: Path, *, geometry_seed: int = 42) -> dict[str, Any]:
    """Create one reproducible feasible reference geometry from a motor spec.

    Zero calibration is allowed before ``beta_calibration`` exists.  The
    temporary value injected here is never written to a solve row or manifest.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain one JSON object")
    spec_raw = dict(raw)
    spec_raw.setdefault(
        "beta_calibration",
        {
            "electrical_zero_deg": 0.0,
            "calibration_id": "pending-zero-calibration",
            "convention": BETA_CONVENTION,
        },
    )
    from generate_ipmsm_v2_cases import _valid_geometry_samples
    from ipmsm_optimization import optimization_spec_from_mapping, phase_resistance_100c_ohm

    spec = optimization_spec_from_mapping(spec_raw)
    design, stack_length_mm, design_hash = _valid_geometry_samples(spec, 1, geometry_seed)[0]
    return {
        "case_id": f"beta_source_{design_hash[:12]}",
        "geometry_group_id": f"beta_source_{design_hash[:12]}",
        "design_hash": design_hash,
        "stack_length_mm": stack_length_mm,
        "phase_resistance_ohm": phase_resistance_100c_ohm(
            design,
            stack_length_mm,
            spec.winding,
            slot_number=spec.slot_number,
        ),
        "vdc_v": spec.inverter.vdc_v,
        "initial_position_deg": -22.5,
        "slot_num": spec.slot_number,
        "pole_num": spec.pole_number,
        **design,
    }


def quality_profile_values(name: str) -> dict[str, Any]:
    try:
        profile = QUALITY_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown quality profile {name!r}") from exc
    values: dict[str, Any] = {
        "quality_profile": name,
        "transient_periods": profile.transient_periods,
        "steps_per_period": profile.steps_per_period,
    }
    for key in MESH_ELEMENT_KEYS:
        values[f"mesh_{key}_elements"] = profile.mesh_elements[key]
    return values


def _source_case_common(source: dict[str, str], quality_profile: str) -> dict[str, Any]:
    geometry = extract_fixed_geometry(source)
    required_operating_values: dict[str, float] = {}
    for canonical, aliases in {
        "stack_length_mm": ("input_stack_length_mm", "stack_length_mm"),
        "phase_resistance_ohm": ("input_phase_resistance_ohm", "phase_resistance_ohm"),
        "vdc_v": ("input_vdc_v", "vdc_v"),
    }.items():
        raw = next((source.get(alias) for alias in aliases if str(source.get(alias) or "").strip()), None)
        value = finite_float(raw)
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"source row is not v2-complete; missing positive {canonical}")
        required_operating_values[canonical] = value
    source_id = str(source.get("case_id") or source.get("source_case_id") or "source")
    group_id = str(source.get("geometry_group_id") or source.get("design_hash") or source_id)
    profile = quality_profile_values(quality_profile)
    initial_position_deg = finite_float(
        row_value(source, "input_initial_position_deg", "initial_position_deg", default=-22.5)
    )
    if not math.isfinite(initial_position_deg):
        raise ValueError("source row must provide a finite initial_position_deg")
    return {
        "geometry_group_id": group_id,
        "design_hash": str(source.get("design_hash") or group_id),
        "doe_split": "calibration",
        "repeat_of_case_id": "",
        "source_case_id": source_id,
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "model_extent": "full_360",
        "symmetry_factor": 1,
        "use_periodic_boundary": False,
        "beta_convention": BETA_CONVENTION,
        "initial_position_deg": initial_position_deg,
        **required_operating_values,
        **geometry,
        **profile,
    }


def generate_zero_calibration_rows(
    source: dict[str, str],
    *,
    speeds_rpm: Iterable[float],
    quality_profile: str = "reference_ultra",
) -> list[dict[str, Any]]:
    """Generate no-load rows used only to identify the physical dq zero."""
    speeds = list(dict.fromkeys(float(value) for value in speeds_rpm))
    if not speeds or any(not math.isfinite(speed) or speed <= 0.0 for speed in speeds):
        raise ValueError("zero calibration requires at least one finite positive speed")
    common = _source_case_common(source, quality_profile)
    rows: list[dict[str, Any]] = []
    for speed in speeds:
        rows.append(
            {
                **common,
                "case_id": (
                    f"beta_zero_{safe_name(common['source_case_id'])}_noload_rpm{safe_name(speed)}"
                ),
                "operating_point_id": "beta_zero_no_load",
                "beta_calibration_id": "",
                "electrical_zero_deg": 0.0,
                "beta_dq_deg": 0.0,
                "base_rpm": speed,
                "i_peak_a": 0.0,
                "operation": "no_load",
            }
        )
    return rows


def generate_calibration_rows(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
    raise ValueError(
        "legacy loaded torque-max electrical-zero generation was removed; "
        "use generate_zero_calibration_rows, then generate_beta_sweep_rows"
    )


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def finite_float(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def row_value(row: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def _required_text_alias(row: Mapping[str, Any], names: tuple[str, ...], label: str) -> str:
    values = [str(row[name]).strip() for name in names if row.get(name) not in (None, "")]
    if not values or not values[0]:
        raise ValueError(f"{label} must not be blank")
    if len(set(values)) != 1:
        raise ValueError(f"{label} aliases disagree: {values!r}")
    return values[0]


def _required_finite_alias(row: Mapping[str, Any], names: tuple[str, ...], label: str) -> float:
    values = [finite_float(row[name]) for name in names if row.get(name) not in (None, "")]
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError(f"{label} must be finite")
    if any(not math.isclose(value, values[0], rel_tol=0.0, abs_tol=1e-12) for value in values[1:]):
        raise ValueError(f"{label} aliases disagree")
    return values[0]


def _duplicate_values(values: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    duplicates: list[Any] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def validate_beta_case_plan_results(
    case_plan_rows: Iterable[Mapping[str, Any]],
    result_rows: Iterable[Mapping[str, Any]],
    calibration_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate exact ordered coverage for the strict beta stage gate."""

    plan = [dict(row) for row in case_plan_rows]
    results = [dict(row) for row in result_rows]
    electrical_zero_deg, calibration_id = validated_zero_manifest(calibration_manifest)
    if len(plan) < 3:
        raise ValueError("strict beta case plan needs at least three rows")
    manifest_design_hash = str(calibration_manifest.get("design_hash") or "").strip()
    if not manifest_design_hash:
        raise ValueError("strict beta stage gate requires zero-manifest design_hash")
    manifest_quality_profile = str(calibration_manifest.get("quality_profile") or "").strip()
    if not manifest_quality_profile:
        raise ValueError("strict beta stage gate requires zero-manifest quality_profile")

    plan_ids = [
        _required_text_alias(row, ("case_id",), f"case plan row {index} case_id")
        for index, row in enumerate(plan, start=1)
    ]
    result_ids = [
        _required_text_alias(row, ("case_id",), f"result row {index} case_id")
        for index, row in enumerate(results, start=1)
    ]
    duplicate_plan_ids = _duplicate_values(plan_ids)
    if duplicate_plan_ids:
        raise ValueError(f"strict beta case plan has duplicate case_id values: {duplicate_plan_ids!r}")
    duplicate_result_ids = _duplicate_values(result_ids)
    if duplicate_result_ids:
        raise ValueError(f"strict beta results have duplicate case_id values: {duplicate_result_ids!r}")
    if set(result_ids) != set(plan_ids):
        missing = [case_id for case_id in plan_ids if case_id not in set(result_ids)]
        unexpected = [case_id for case_id in result_ids if case_id not in set(plan_ids)]
        raise ValueError(
            f"strict beta result coverage mismatch: missing={missing!r}, unexpected={unexpected!r}"
        )
    if result_ids != plan_ids:
        raise ValueError("strict beta result order does not match the case plan")

    plan_betas: list[float] = []
    plan_design_hashes: list[str] = []
    for index, (plan_row, result_row) in enumerate(zip(plan, results), start=1):
        case_id = plan_ids[index - 1]
        status = str(result_row.get("status") or "").strip().lower()
        if status != "ok":
            raise ValueError(f"strict beta result must be ok for case_id={case_id!r}; got {status!r}")
        plan_schema = _required_text_alias(
            plan_row,
            ("dataset_schema_version", "input_dataset_schema_version"),
            f"case plan schema for {case_id}",
        )
        result_schema = _required_text_alias(
            result_row,
            ("input_dataset_schema_version", "dataset_schema_version"),
            f"result schema for {case_id}",
        )
        if plan_schema != DATASET_SCHEMA_VERSION or result_schema != DATASET_SCHEMA_VERSION:
            raise ValueError(f"strict beta schema mismatch for case_id={case_id!r}")

        plan_beta = _required_finite_alias(
            plan_row,
            ("beta_dq_deg", "input_beta_dq_deg"),
            f"case plan beta for {case_id}",
        )
        result_beta = _required_finite_alias(
            result_row,
            ("input_beta_dq_deg", "beta_dq_deg"),
            f"result beta for {case_id}",
        )
        plan_rpm = _required_finite_alias(
            plan_row,
            ("base_rpm", "input_base_rpm"),
            f"case plan rpm for {case_id}",
        )
        result_rpm = _required_finite_alias(
            result_row,
            ("input_base_rpm", "base_rpm"),
            f"result rpm for {case_id}",
        )
        plan_current = _required_finite_alias(
            plan_row,
            ("i_peak_a", "input_i_peak_a"),
            f"case plan current for {case_id}",
        )
        result_current = _required_finite_alias(
            result_row,
            ("input_i_peak_a", "i_peak_a"),
            f"result current for {case_id}",
        )
        for name, expected, actual in (
            ("beta", plan_beta, result_beta),
            ("rpm", plan_rpm, result_rpm),
            ("current", plan_current, result_current),
        ):
            if not math.isclose(expected, actual, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(
                    f"strict beta {name} mismatch for case_id={case_id!r}: {expected} != {actual}"
                )

        plan_design_hash = _required_text_alias(
            plan_row,
            ("design_hash", "input_design_hash"),
            f"case plan design_hash for {case_id}",
        )
        result_design_hash = _required_text_alias(
            result_row,
            ("design_hash", "input_design_hash"),
            f"result design_hash for {case_id}",
        )
        if result_design_hash != plan_design_hash:
            raise ValueError(f"strict beta design_hash mismatch for case_id={case_id!r}")
        if plan_design_hash != manifest_design_hash:
            raise ValueError(f"strict beta design_hash does not match zero manifest for case_id={case_id!r}")

        plan_calibration_id = _required_text_alias(
            plan_row,
            ("beta_calibration_id", "input_beta_calibration_id"),
            f"case plan calibration_id for {case_id}",
        )
        result_calibration_id = _required_text_alias(
            result_row,
            ("input_beta_calibration_id", "beta_calibration_id"),
            f"result calibration_id for {case_id}",
        )
        if plan_calibration_id != calibration_id or result_calibration_id != calibration_id:
            raise ValueError(f"strict beta calibration_id mismatch for case_id={case_id!r}")

        plan_zero = _required_finite_alias(
            plan_row,
            ("electrical_zero_deg", "input_electrical_zero_deg"),
            f"case plan electrical_zero_deg for {case_id}",
        )
        result_zero = _required_finite_alias(
            result_row,
            ("input_electrical_zero_deg", "electrical_zero_deg"),
            f"result electrical_zero_deg for {case_id}",
        )
        if not math.isclose(plan_zero, electrical_zero_deg, rel_tol=0.0, abs_tol=1e-12) or not math.isclose(
            result_zero, electrical_zero_deg, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError(f"strict beta electrical_zero_deg mismatch for case_id={case_id!r}")

        plan_quality = _required_text_alias(
            plan_row,
            ("quality_profile", "input_quality_profile"),
            f"case plan quality_profile for {case_id}",
        )
        result_quality = _required_text_alias(
            result_row,
            ("input_quality_profile", "quality_profile"),
            f"result quality_profile for {case_id}",
        )
        if plan_quality != result_quality:
            raise ValueError(f"strict beta quality_profile mismatch for case_id={case_id!r}")
        plan_betas.append(plan_beta)
        plan_design_hashes.append(plan_design_hash)

    duplicate_betas = _duplicate_values(plan_betas)
    if duplicate_betas:
        raise ValueError(f"strict beta case plan has duplicate beta values: {duplicate_betas!r}")
    if len(set(plan_design_hashes)) != 1:
        raise ValueError("strict beta case plan must use one design_hash")

    identity_aliases = {
        "geometry_group_id": ("geometry_group_id", "input_geometry_group_id"),
        "design_hash": ("design_hash", "input_design_hash"),
        "setup_fingerprint": ("input_setup_fingerprint", "setup_fingerprint"),
        "material_fingerprint": ("input_material_fingerprint", "material_fingerprint"),
        "aedt_version": ("input_aedt_version", "aedt_version"),
        "quality_profile": ("input_quality_profile", "quality_profile"),
    }
    identities: dict[str, str] = {}
    for name, aliases in identity_aliases.items():
        values = [
            _required_text_alias(row, aliases, f"strict beta result {name} for {case_id}")
            for row, case_id in zip(results, result_ids)
        ]
        if len(set(values)) != 1:
            raise ValueError(f"strict beta results mix {name}: {sorted(set(values))!r}")
        identities[name] = values[0]
    if identities["quality_profile"] != manifest_quality_profile:
        raise ValueError("strict beta results quality_profile does not match the zero manifest")

    return {
        "expected_rows": len(plan),
        "plan_hash": rows_hash("beta-plan", plan),
        "result_hash": rows_hash("beta-results", results),
        "homogeneous_identities": identities,
    }


def truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def wrap_degrees(value: float) -> float:
    wrapped = (float(value) + 180.0) % 360.0 - 180.0
    return 180.0 if math.isclose(wrapped, -180.0, abs_tol=1e-12) else wrapped


def circular_mean_degrees(values: Iterable[float]) -> tuple[float, float]:
    angles = [math.radians(float(value)) for value in values]
    if not angles:
        raise ValueError("at least one circular angle is required")
    x = sum(math.cos(angle) for angle in angles) / len(angles)
    y = sum(math.sin(angle) for angle in angles) / len(angles)
    resultant = math.hypot(x, y)
    if resultant < 1e-9:
        raise ValueError("electrical-zero observations are circularly ambiguous")
    return wrap_degrees(math.degrees(math.atan2(y, x))), resultant


def _require_full_model_row(row: Mapping[str, Any]) -> None:
    if str(row_value(row, "input_model_extent", "model_extent")).strip() != "full_360":
        raise ValueError("calibration result must use model_extent='full_360'")
    symmetry = finite_float(row_value(row, "input_symmetry_factor", "symmetry_factor"))
    if not math.isclose(symmetry, 1.0, abs_tol=1e-12):
        raise ValueError("calibration result must use symmetry_factor=1")
    if truthy(row_value(row, "input_use_periodic_boundary", "use_periodic_boundary", default=False)):
        raise ValueError("calibration result must not use a periodic boundary")
    if str(row_value(row, "input_beta_convention", "beta_convention")).strip() != BETA_CONVENTION:
        raise ValueError(f"calibration result must use beta convention {BETA_CONVENTION!r}")


def analyze_zero_calibration_rows(
    rows: Iterable[dict[str, str]],
    *,
    max_circular_deviation_deg: float = 3.0,
    min_distinct_speeds: int = 2,
) -> dict[str, Any]:
    """Infer physical ElectricalZero from signed no-load Phase-A back EMF."""
    if not math.isfinite(max_circular_deviation_deg) or max_circular_deviation_deg < 0.0:
        raise ValueError("max_circular_deviation_deg must be finite and nonnegative")
    if type(min_distinct_speeds) is not int or min_distinct_speeds < 1:
        raise ValueError("min_distinct_speeds must be a positive integer")
    successful = [row for row in rows if str(row.get("status") or "").strip().lower() == "ok"]
    if not successful:
        raise ValueError("zero calibration needs at least one successful no-load speed")

    observations: list[dict[str, float]] = []
    homogeneous: dict[str, set[str]] = {
        "design_hash": set(),
        "input_quality_profile": set(),
        "input_setup_fingerprint": set(),
        "input_material_fingerprint": set(),
        "input_aedt_version": set(),
    }
    initial_positions_deg: list[float] = []
    for row in successful:
        if str(row.get("input_dataset_schema_version") or "").strip() != DATASET_SCHEMA_VERSION:
            raise ValueError(
                f"zero calibration requires input_dataset_schema_version={DATASET_SCHEMA_VERSION!r}"
            )
        for column, values in homogeneous.items():
            value = str(row.get(column) or "").strip()
            if not value:
                raise ValueError(f"zero calibration requires nonblank {column}")
            values.add(value)
        initial_position_deg = finite_float(row.get("input_initial_position_deg"))
        if not math.isfinite(initial_position_deg):
            raise ValueError("zero calibration requires finite input_initial_position_deg")
        initial_positions_deg.append(initial_position_deg)

        operation = str(row_value(row, "input_operation", "operation")).strip().lower().replace("-", "_")
        current = finite_float(row_value(row, "input_i_peak_a", "i_peak_a"))
        if operation not in {"no_load", "noload", "back_emf", "backemf"} or not math.isclose(
            current, 0.0, abs_tol=1e-12
        ):
            raise ValueError(
                "loaded torque-max rows cannot calibrate electrical zero; use no-load signed back EMF"
            )
        _require_full_model_row(row)
        configured_zero = finite_float(row_value(row, "input_electrical_zero_deg", "electrical_zero_deg"))
        if not math.isclose(configured_zero, 0.0, abs_tol=1e-12):
            raise ValueError("zero-calibration solve must keep ElectricalZero at 0 because excitation is off")
        speed = finite_float(row_value(row, "input_base_rpm", "base_rpm"))
        cos_peak = finite_float(row.get("output_back_emf_phasea_h1_cos_peak_v"))
        sin_peak = finite_float(row.get("output_back_emf_phasea_h1_sin_peak_v"))
        if not all(math.isfinite(value) for value in (speed, cos_peak, sin_peak)) or speed <= 0.0:
            raise ValueError("zero calibration requires finite speed and signed H1 back-EMF coefficients")
        amplitude = math.hypot(cos_peak, sin_peak)
        if amplitude <= 0.0:
            raise ValueError("zero calibration back-EMF fundamental amplitude must be > 0")
        # E_a=C*cos(theta)+S*sin(theta). For canonical
        # Ia=-Iq*sin(theta+z), the physical dq zero is z=atan2(-C,-S).
        inferred_zero = wrap_degrees(math.degrees(math.atan2(-cos_peak, -sin_peak)))
        observations.append(
            {
                "speed_rpm": speed,
                "cos_peak_v": cos_peak,
                "sin_peak_v": sin_peak,
                "amplitude_peak_v": amplitude,
                "inferred_zero_deg": inferred_zero,
            }
        )

    mixed = [column for column, values in homogeneous.items() if len(values) > 1]
    initial_reference_deg = initial_positions_deg[0]
    if any(
        not math.isclose(value, initial_reference_deg, rel_tol=0.0, abs_tol=1e-9)
        for value in initial_positions_deg[1:]
    ):
        mixed.append("input_initial_position_deg")
    if mixed:
        raise ValueError("zero calibration mixes incompatible rows: " + ", ".join(mixed))
    successful_speeds = sorted({observation["speed_rpm"] for observation in observations})
    if len(successful_speeds) < min_distinct_speeds:
        raise ValueError(
            "zero calibration needs at least "
            f"{min_distinct_speeds} distinct successful speeds; got {len(successful_speeds)}"
        )
    electrical_zero_deg, resultant = circular_mean_degrees(
        observation["inferred_zero_deg"] for observation in observations
    )
    max_deviation = max(
        abs(wrap_degrees(observation["inferred_zero_deg"] - electrical_zero_deg))
        for observation in observations
    )
    if max_deviation > max_circular_deviation_deg:
        raise ValueError(
            f"electrical-zero observations differ by {max_deviation:g}deg; "
            f"limit is {max_circular_deviation_deg:g}deg"
        )

    first = successful[0]
    payload: dict[str, Any] = {
        "workflow_version": "beta_calibration_v2",
        "method": ZERO_CALIBRATION_METHOD,
        "convention": BETA_CONVENTION,
        "electrical_zero_deg": electrical_zero_deg,
        "source_case_id": str(row_value(first, "input_source_case_id", "source_case_id", "case_id")),
        "design_hash": str(row_value(first, "design_hash", "input_design_hash")),
        "quality_profile": str(row_value(first, "input_quality_profile", "quality_profile")),
        "initial_position_deg": initial_reference_deg,
        "successful_rows": len(observations),
        "successful_speeds_rpm": successful_speeds,
        "circular_resultant": resultant,
        "max_circular_deviation_deg": max_deviation,
        "observations": observations,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    payload["calibration_id"] = f"beta-calibration:sha256:{hashlib.sha256(encoded).hexdigest()}"
    return payload


def validated_zero_manifest(manifest: Mapping[str, Any]) -> tuple[float, str]:
    if str(manifest.get("method") or "") != ZERO_CALIBRATION_METHOD:
        raise ValueError(f"calibration manifest method must be {ZERO_CALIBRATION_METHOD!r}")
    if str(manifest.get("convention") or "") != BETA_CONVENTION:
        raise ValueError(f"calibration manifest convention must be {BETA_CONVENTION!r}")
    calibration_id = str(manifest.get("calibration_id") or "").strip()
    if not calibration_id:
        raise ValueError("calibration manifest calibration_id must not be blank")
    unhashed = dict(manifest)
    unhashed.pop("calibration_id", None)
    encoded = json.dumps(unhashed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    expected_id = f"beta-calibration:sha256:{hashlib.sha256(encoded).hexdigest()}"
    if calibration_id != expected_id:
        raise ValueError("calibration manifest content does not match calibration_id")
    electrical_zero_deg = finite_float(manifest.get("electrical_zero_deg"))
    if not math.isfinite(electrical_zero_deg):
        raise ValueError("calibration manifest electrical_zero_deg must be finite")
    return electrical_zero_deg, calibration_id


def apply_zero_manifest_to_spec(
    spec: Mapping[str, Any],
    calibration_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    electrical_zero_deg, calibration_id = validated_zero_manifest(calibration_manifest)
    updated = dict(spec)
    updated["beta_calibration"] = {
        "electrical_zero_deg": electrical_zero_deg,
        "calibration_id": calibration_id,
        "convention": BETA_CONVENTION,
    }
    from ipmsm_optimization import optimization_spec_from_mapping

    optimization_spec_from_mapping(updated)
    return updated


def generate_beta_sweep_rows(
    source: dict[str, str],
    calibration_manifest: Mapping[str, Any],
    *,
    rpm: float,
    current_peak_a: float,
    beta_values: Iterable[float],
    quality_profile: str = "reference_ultra",
) -> list[dict[str, Any]]:
    """Generate a loaded MTPA sweep while holding calibrated zero fixed."""
    electrical_zero_deg, calibration_id = validated_zero_manifest(calibration_manifest)
    if not math.isfinite(rpm) or rpm <= 0.0 or not math.isfinite(current_peak_a) or current_peak_a <= 0.0:
        raise ValueError("loaded beta sweep requires finite positive rpm and current_peak_a")
    betas = sorted(set(float(value) for value in beta_values))
    if len(betas) < 3 or any(not math.isfinite(beta) for beta in betas):
        raise ValueError("loaded beta sweep requires at least three distinct finite beta values")
    common = _source_case_common(source, quality_profile)
    manifest_initial_position = finite_float(calibration_manifest.get("initial_position_deg"))
    if math.isfinite(manifest_initial_position) and not math.isclose(
        float(common["initial_position_deg"]), manifest_initial_position, abs_tol=1e-9
    ):
        raise ValueError("loaded beta sweep initial_position_deg does not match zero calibration")
    rows: list[dict[str, Any]] = []
    for beta in betas:
        rows.append(
            {
                **common,
                "case_id": (
                    f"beta_mtpa_{safe_name(common['source_case_id'])}_rpm{safe_name(rpm)}_"
                    f"i{safe_name(current_peak_a)}_b{safe_name(beta)}"
                ),
                "operating_point_id": "beta_mtpa_validation",
                "beta_calibration_id": calibration_id,
                "electrical_zero_deg": electrical_zero_deg,
                "beta_dq_deg": beta,
                "base_rpm": rpm,
                "i_peak_a": current_peak_a,
                "operation": "sin_current",
            }
        )
    return rows


def analyze_beta_sweep_rows(
    rows: Iterable[dict[str, str]],
    calibration_manifest: Mapping[str, Any],
    *,
    max_dq_current_relative_error: float = 0.02,
    case_plan_rows: Iterable[Mapping[str, Any]] | None = None,
    stage_beta_bounds_deg: tuple[float, float] = DEFAULT_STAGE_BETA_BOUNDS_DEG,
) -> dict[str, Any]:
    electrical_zero_deg, calibration_id = validated_zero_manifest(calibration_manifest)
    if not math.isfinite(max_dq_current_relative_error) or max_dq_current_relative_error < 0.0:
        raise ValueError("max_dq_current_relative_error must be finite and nonnegative")
    stage_bounds = tuple(float(value) for value in stage_beta_bounds_deg)
    if len(stage_bounds) != 2:
        raise ValueError("stage_beta_bounds_deg must contain two increasing finite values")
    stage_beta_lower, stage_beta_upper = stage_bounds
    if not all(math.isfinite(value) for value in stage_bounds) or stage_beta_lower >= stage_beta_upper:
        raise ValueError("stage_beta_bounds_deg must contain two increasing finite values")
    from module.ipmsm_ppt_setup import canonical_dq_current_components

    result_rows = [dict(row) for row in rows]
    plan_rows = [dict(row) for row in case_plan_rows] if case_plan_rows is not None else None
    strict_contract = (
        validate_beta_case_plan_results(plan_rows, result_rows, calibration_manifest)
        if plan_rows is not None
        else None
    )
    points: list[dict[str, float]] = []
    identity_aliases = {
        "geometry_group_id": ("geometry_group_id", "input_geometry_group_id"),
        "design_hash": ("design_hash", "input_design_hash"),
        "setup_fingerprint": ("input_setup_fingerprint", "setup_fingerprint"),
        "material_fingerprint": ("input_material_fingerprint", "material_fingerprint"),
        "aedt_version": ("input_aedt_version", "aedt_version"),
        "quality_profile": ("input_quality_profile", "quality_profile"),
    }
    identities: dict[str, set[str]] = {
        "geometry_group_id": set(),
        "design_hash": set(),
        "setup_fingerprint": set(),
        "material_fingerprint": set(),
        "aedt_version": set(),
        "quality_profile": set(),
    }
    for row in result_rows:
        if str(row.get("status") or "").strip().lower() != "ok":
            if strict_contract is not None:
                raise ValueError("strict beta analysis cannot skip a non-ok result row")
            continue
        _require_full_model_row(row)
        operation = str(row_value(row, "input_operation", "operation")).strip().lower().replace("-", "_")
        if operation not in {"sin_current", "sincurrent"}:
            raise ValueError("loaded beta sweep result must use sin_current operation")
        row_calibration_id = str(row_value(row, "beta_calibration_id", "input_beta_calibration_id")).strip()
        if row_calibration_id != calibration_id:
            raise ValueError("loaded beta sweep row does not match calibration_id")
        manifest_design_hash = str(calibration_manifest.get("design_hash") or "").strip()
        row_design_hash = str(row_value(row, "design_hash", "input_design_hash")).strip()
        if manifest_design_hash and row_design_hash and row_design_hash != manifest_design_hash:
            raise ValueError("loaded beta sweep geometry does not match zero-calibration manifest")
        row_zero = finite_float(row_value(row, "input_electrical_zero_deg", "electrical_zero_deg"))
        if not math.isclose(row_zero, electrical_zero_deg, abs_tol=1e-9):
            raise ValueError("loaded beta sweep must not change calibrated electrical_zero_deg")
        manifest_initial_position = finite_float(calibration_manifest.get("initial_position_deg"))
        row_initial_position = finite_float(
            row_value(row, "input_initial_position_deg", "initial_position_deg")
        )
        if math.isfinite(manifest_initial_position) and not math.isclose(
            row_initial_position, manifest_initial_position, abs_tol=1e-9
        ):
            raise ValueError("loaded beta sweep initial_position_deg does not match zero calibration")
        beta = finite_float(row_value(row, "input_beta_dq_deg", "beta_dq_deg"))
        current = finite_float(row_value(row, "input_i_peak_a", "i_peak_a"))
        speed = finite_float(row_value(row, "input_base_rpm", "base_rpm"))
        torque = finite_float(row.get("output_torque_last_avg_nm"))
        actual_id = finite_float(row.get("output_id_current_last_avg_a"))
        actual_iq = finite_float(row.get("output_iq_current_last_avg_a"))
        if not all(math.isfinite(value) for value in (beta, current, speed, torque, actual_id, actual_iq)):
            raise ValueError("loaded beta sweep requires finite beta/current/speed/torque and measured Id/Iq")
        if current <= 0.0 or speed <= 0.0:
            raise ValueError("loaded beta sweep current and speed must be > 0")
        expected_id, expected_iq = canonical_dq_current_components(current, beta)
        dq_error = math.hypot(actual_id - expected_id, actual_iq - expected_iq) / current
        if dq_error > max_dq_current_relative_error:
            raise ValueError(
                f"measured dq current mismatch at beta={beta:g}deg: {dq_error:g} > "
                f"{max_dq_current_relative_error:g}"
            )
        points.append(
            {
                "beta_dq_deg": beta,
                "current_peak_a": current,
                "speed_rpm": speed,
                "torque_nm": torque,
                "torque_per_peak_amp": torque / current,
                "actual_id_a": actual_id,
                "actual_iq_a": actual_iq,
                "dq_current_relative_error": dq_error,
            }
        )
        for name, aliases in identity_aliases.items():
            value = str(row_value(row, *aliases)).strip()
            if value:
                identities[name].add(value)
    if len(points) < 3 or len({point["beta_dq_deg"] for point in points}) < 3:
        raise ValueError("MTPA validation needs at least three successful distinct beta rows")
    if any(
        len(identities[name]) > 1
        for name in ("geometry_group_id", "design_hash", "setup_fingerprint")
    ):
        raise ValueError("loaded beta sweep mixes geometry or setup fingerprints")
    currents = [point["current_peak_a"] for point in points]
    speeds = [point["speed_rpm"] for point in points]
    if max(currents) - min(currents) > max(currents) * 1e-9 or max(speeds) - min(speeds) > max(speeds) * 1e-9:
        raise ValueError("MTPA validation rows must use one fixed speed and current")
    points.sort(key=lambda point: point["beta_dq_deg"])
    best = max(points, key=lambda point: (point["torque_per_peak_amp"], -abs(point["beta_dq_deg"])))
    if best is points[0] or best is points[-1]:
        raise ValueError("MTPA optimum is on the beta sweep boundary; extend the beta range")
    if best["torque_nm"] <= 0.0:
        raise ValueError("MTPA validation did not produce positive motoring torque")
    tested_beta_values = sorted({point["beta_dq_deg"] for point in points})
    gate_failures: list[str] = []
    if strict_contract is None:
        gate_failures.append("strict_case_plan_validation_required")
    if max_dq_current_relative_error > MAX_STAGE_DQ_CURRENT_RELATIVE_ERROR:
        gate_failures.append("dq_threshold_exceeds_stage_limit")
    if not stage_beta_lower <= best["beta_dq_deg"] <= stage_beta_upper:
        gate_failures.append("best_beta_outside_stage_bounds")
    stage_gate_passed = not gate_failures
    homogeneous_identities = (
        dict(strict_contract["homogeneous_identities"])
        if strict_contract is not None
        else {
            name: next(iter(values)) if len(values) == 1 else ""
            for name, values in identities.items()
        }
    )
    payload: dict[str, Any] = {
        "summary_schema_version": BETA_SUMMARY_SCHEMA_VERSION,
        "workflow_version": "beta_calibration_v2",
        "method": MTPA_VALIDATION_METHOD,
        "convention": BETA_CONVENTION,
        "status": "passed" if stage_gate_passed else "diagnostic_only",
        "pass": stage_gate_passed,
        "gate_failures": gate_failures,
        "strict_case_plan_validation": strict_contract is not None,
        "beta_calibration_id": calibration_id,
        "electrical_zero_deg": electrical_zero_deg,
        "expected_rows": strict_contract["expected_rows"] if strict_contract is not None else len(result_rows),
        "successful_rows": len(points),
        "tested_beta_bounds_deg": [tested_beta_values[0], tested_beta_values[-1]],
        "tested_beta_values_deg": tested_beta_values,
        "stage_beta_bounds_deg": [stage_beta_lower, stage_beta_upper],
        "max_dq_current_relative_error": float(max_dq_current_relative_error),
        "max_observed_dq_current_relative_error": max(
            point["dq_current_relative_error"] for point in points
        ),
        "homogeneous_identities": homogeneous_identities,
        "plan_hash": strict_contract["plan_hash"] if strict_contract is not None else "",
        "result_hash": (
            strict_contract["result_hash"]
            if strict_contract is not None
            else rows_hash("beta-results", result_rows)
        ),
        "best_beta_dq_deg": best["beta_dq_deg"],
        "best_torque_nm": best["torque_nm"],
        "best_torque_per_peak_amp": best["torque_per_peak_amp"],
        "speed_rpm": best["speed_rpm"],
        "current_peak_a": best["current_peak_a"],
        "points": points,
    }
    payload["sweep_id"] = canonical_hash("beta-mtpa", payload)
    return payload


def validate_beta_sweep_summary(
    summary: Mapping[str, Any],
    *,
    case_plan_rows: Iterable[Mapping[str, Any]] | None = None,
    result_rows: Iterable[Mapping[str, Any]] | None = None,
    calibration_manifest: Mapping[str, Any] | None = None,
    require_stage_pass: bool = False,
) -> dict[str, Any]:
    """Validate the exact beta summary schema and optionally replay raw inputs."""

    value = dict(summary)
    missing = sorted(set(BETA_SUMMARY_FIELDS) - set(value))
    extra = sorted(set(value) - set(BETA_SUMMARY_FIELDS))
    if missing or extra:
        raise ValueError(f"beta summary schema mismatch: missing={missing!r}, extra={extra!r}")
    for key, expected in (
        ("summary_schema_version", BETA_SUMMARY_SCHEMA_VERSION),
        ("workflow_version", "beta_calibration_v2"),
        ("method", MTPA_VALIDATION_METHOD),
        ("convention", BETA_CONVENTION),
    ):
        if value[key] != expected:
            raise ValueError(f"beta summary {key} must be {expected!r}")
    if type(value["pass"]) is not bool or type(value["strict_case_plan_validation"]) is not bool:
        raise ValueError("beta summary pass and strict_case_plan_validation must be booleans")
    if value["status"] not in {"passed", "diagnostic_only"}:
        raise ValueError("beta summary status must be 'passed' or 'diagnostic_only'")
    gate_failures = value["gate_failures"]
    if not isinstance(gate_failures, list) or any(
        not isinstance(item, str) or not item for item in gate_failures
    ) or len(set(gate_failures)) != len(gate_failures):
        raise ValueError("beta summary gate_failures must be a unique string array")

    for key in ("expected_rows", "successful_rows"):
        if type(value[key]) is not int or value[key] < 3:
            raise ValueError(f"beta summary {key} must be an integer >= 3")
    if value["successful_rows"] > value["expected_rows"]:
        raise ValueError("beta summary successful_rows cannot exceed expected_rows")
    if value["strict_case_plan_validation"] and value["successful_rows"] != value["expected_rows"]:
        raise ValueError("strict beta summary must cover every expected row")

    def numeric(key: str) -> float:
        number = finite_float(value[key])
        if not math.isfinite(number):
            raise ValueError(f"beta summary {key} must be finite")
        return number

    electrical_zero_deg = numeric("electrical_zero_deg")
    threshold = numeric("max_dq_current_relative_error")
    max_observed_error = numeric("max_observed_dq_current_relative_error")
    if threshold < 0.0 or max_observed_error < 0.0 or max_observed_error > threshold:
        raise ValueError("beta summary dq-current errors are inconsistent with the threshold")
    calibration_id = str(value["beta_calibration_id"] or "").strip()
    if not calibration_id:
        raise ValueError("beta summary beta_calibration_id must not be blank")

    def numeric_pair(key: str) -> tuple[float, float]:
        raw = value[key]
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError(f"beta summary {key} must contain two numbers")
        pair = (finite_float(raw[0]), finite_float(raw[1]))
        if not all(math.isfinite(item) for item in pair) or pair[0] >= pair[1]:
            raise ValueError(f"beta summary {key} must contain two increasing finite numbers")
        return pair

    tested_bounds = numeric_pair("tested_beta_bounds_deg")
    stage_bounds = numeric_pair("stage_beta_bounds_deg")
    tested_values_raw = value["tested_beta_values_deg"]
    if not isinstance(tested_values_raw, list):
        raise ValueError("beta summary tested_beta_values_deg must be an array")
    tested_values = [finite_float(item) for item in tested_values_raw]
    if (
        len(tested_values) < 3
        or any(not math.isfinite(item) for item in tested_values)
        or tested_values != sorted(set(tested_values))
        or tested_bounds != (tested_values[0], tested_values[-1])
    ):
        raise ValueError("beta summary tested beta values/bounds are inconsistent")

    points_raw = value["points"]
    if not isinstance(points_raw, list) or len(points_raw) != value["successful_rows"]:
        raise ValueError("beta summary points must match successful_rows")
    points: list[dict[str, float]] = []
    from module.ipmsm_ppt_setup import canonical_dq_current_components

    for index, raw_point in enumerate(points_raw):
        if not isinstance(raw_point, Mapping) or set(raw_point) != set(BETA_SUMMARY_POINT_FIELDS):
            raise ValueError(f"beta summary point {index} has an invalid schema")
        point = {key: finite_float(raw_point[key]) for key in BETA_SUMMARY_POINT_FIELDS}
        if any(not math.isfinite(item) for item in point.values()):
            raise ValueError(f"beta summary point {index} must contain finite numbers")
        if point["current_peak_a"] <= 0.0 or point["speed_rpm"] <= 0.0:
            raise ValueError(f"beta summary point {index} current/speed must be > 0")
        if point["dq_current_relative_error"] < 0.0 or point["dq_current_relative_error"] > threshold:
            raise ValueError(f"beta summary point {index} exceeds the dq-current error threshold")
        expected_id, expected_iq = canonical_dq_current_components(
            point["current_peak_a"], point["beta_dq_deg"]
        )
        recomputed_dq_error = math.hypot(
            point["actual_id_a"] - expected_id,
            point["actual_iq_a"] - expected_iq,
        ) / point["current_peak_a"]
        if not math.isclose(
            point["dq_current_relative_error"],
            recomputed_dq_error,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError(f"beta summary point {index} dq-current error is inconsistent")
        expected_ratio = point["torque_nm"] / point["current_peak_a"]
        if not math.isclose(
            point["torque_per_peak_amp"], expected_ratio, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError(f"beta summary point {index} torque/current ratio is inconsistent")
        points.append(point)
    point_betas = [point["beta_dq_deg"] for point in points]
    if point_betas != tested_values:
        raise ValueError("beta summary points must be unique and sorted by tested beta")
    currents = [point["current_peak_a"] for point in points]
    speeds = [point["speed_rpm"] for point in points]
    if max(currents) - min(currents) > max(currents) * 1e-9 or max(speeds) - min(speeds) > max(speeds) * 1e-9:
        raise ValueError("beta summary points must use one fixed speed and current")
    best = max(points, key=lambda point: (point["torque_per_peak_amp"], -abs(point["beta_dq_deg"])))
    if best is points[0] or best is points[-1]:
        raise ValueError("beta summary MTPA optimum must be inside the tested beta sweep")
    if best["torque_nm"] <= 0.0:
        raise ValueError("beta summary best torque must be positive")
    for summary_key, point_key in (
        ("best_beta_dq_deg", "beta_dq_deg"),
        ("best_torque_nm", "torque_nm"),
        ("best_torque_per_peak_amp", "torque_per_peak_amp"),
        ("speed_rpm", "speed_rpm"),
        ("current_peak_a", "current_peak_a"),
    ):
        if not math.isclose(numeric(summary_key), best[point_key], rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"beta summary {summary_key} does not match the recomputed optimum")
    if not math.isclose(
        max_observed_error,
        max(point["dq_current_relative_error"] for point in points),
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValueError("beta summary max observed dq-current error is inconsistent")

    identities = value["homogeneous_identities"]
    if not isinstance(identities, Mapping) or set(identities) != set(BETA_SUMMARY_IDENTITY_FIELDS):
        raise ValueError("beta summary homogeneous_identities has an invalid schema")
    if any(not isinstance(identities[key], str) for key in BETA_SUMMARY_IDENTITY_FIELDS):
        raise ValueError("beta summary homogeneous identity values must be strings")
    if value["strict_case_plan_validation"] and any(
        not identities[key].strip() for key in BETA_SUMMARY_IDENTITY_FIELDS
    ):
        raise ValueError("strict beta summary homogeneous identities must not be blank")

    expected_gate_failures: list[str] = []
    if not value["strict_case_plan_validation"]:
        expected_gate_failures.append("strict_case_plan_validation_required")
    if threshold > MAX_STAGE_DQ_CURRENT_RELATIVE_ERROR:
        expected_gate_failures.append("dq_threshold_exceeds_stage_limit")
    if not stage_bounds[0] <= best["beta_dq_deg"] <= stage_bounds[1]:
        expected_gate_failures.append("best_beta_outside_stage_bounds")
    if gate_failures != expected_gate_failures:
        raise ValueError("beta summary gate_failures do not match the recomputed stage gate")
    expected_pass = not expected_gate_failures
    if value["pass"] is not expected_pass or value["status"] != (
        "passed" if expected_pass else "diagnostic_only"
    ):
        raise ValueError("beta summary status/pass do not match the recomputed stage gate")

    plan_hash = str(value["plan_hash"] or "")
    result_hash = str(value["result_hash"] or "")

    def valid_prefixed_sha256(text: str, prefix: str) -> bool:
        if not text.startswith(prefix):
            return False
        digest = text.removeprefix(prefix)
        try:
            int(digest, 16)
        except ValueError:
            return False
        return len(digest) == 64

    if value["strict_case_plan_validation"]:
        if not valid_prefixed_sha256(plan_hash, "beta-plan:sha256:"):
            raise ValueError("strict beta summary plan_hash is invalid")
    elif plan_hash:
        raise ValueError("diagnostic beta summary plan_hash must be blank without a case plan")
    if not valid_prefixed_sha256(result_hash, "beta-results:sha256:"):
        raise ValueError("beta summary result_hash is invalid")
    unhashed = dict(value)
    sweep_id = str(unhashed.pop("sweep_id") or "")
    if sweep_id != canonical_hash("beta-mtpa", unhashed):
        raise ValueError("beta summary content does not match sweep_id")

    raw_values = (case_plan_rows, result_rows, calibration_manifest)
    if any(item is not None for item in raw_values):
        if not all(item is not None for item in raw_values):
            raise ValueError("exact beta summary replay requires case plan, results, and calibration manifest")
        replay_plan = [dict(row) for row in case_plan_rows or ()]
        replay_results = [dict(row) for row in result_rows or ()]
        assert calibration_manifest is not None
        replayed = analyze_beta_sweep_rows(
            replay_results,
            calibration_manifest,
            max_dq_current_relative_error=threshold,
            case_plan_rows=replay_plan,
            stage_beta_bounds_deg=stage_bounds,
        )
        if value != replayed:
            raise ValueError("beta summary does not exactly replay from the case plan and results")
        manifest_zero, manifest_id = validated_zero_manifest(calibration_manifest)
        if calibration_id != manifest_id or not math.isclose(
            electrical_zero_deg, manifest_zero, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("beta summary does not match the calibration manifest")
    if require_stage_pass:
        if not all(item is not None for item in raw_values):
            raise ValueError("stage-pass validation requires case plan, results, and calibration manifest")
        if stage_bounds != DEFAULT_STAGE_BETA_BOUNDS_DEG:
            raise ValueError(
                "stage-pass validation requires the spec beta bounds [0, 80]"
            )
        if not value["pass"]:
            raise ValueError("beta summary stage gate did not pass: " + ", ".join(gate_failures))
    return value


def analyze_calibration_rows(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise ValueError(
        "legacy loaded torque-max electrical-zero analysis was removed; "
        "use analyze_zero_calibration_rows, then analyze_beta_sweep_rows"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    zero_generate = subparsers.add_parser("zero-generate", help="Generate no-load back-EMF cases.")
    zero_source = zero_generate.add_mutually_exclusive_group(required=True)
    zero_source.add_argument("--source", type=Path)
    zero_source.add_argument("--spec", type=Path)
    zero_generate.add_argument("--source-row", type=int, default=1)
    zero_generate.add_argument("--geometry-seed", type=int, default=42)
    zero_generate.add_argument("--output", type=Path, required=True)
    zero_generate.add_argument("--rpm-values", required=True)
    zero_generate.add_argument("--quality-profile", default="reference_ultra")

    zero_analyze = subparsers.add_parser("zero-analyze", help="Infer ElectricalZero from signed H1 phase.")
    zero_analyze.add_argument("--results", type=Path, required=True)
    zero_analyze.add_argument("--manifest", type=Path, required=True)
    zero_analyze.add_argument("--max-circular-deviation-deg", type=float, default=3.0)
    zero_analyze.add_argument("--min-distinct-speeds", type=int, default=2)

    beta_generate = subparsers.add_parser("beta-generate", help="Generate a loaded fixed-zero MTPA sweep.")
    beta_source = beta_generate.add_mutually_exclusive_group(required=True)
    beta_source.add_argument("--source", type=Path)
    beta_source.add_argument("--spec", type=Path)
    beta_generate.add_argument("--source-row", type=int, default=1)
    beta_generate.add_argument("--geometry-seed", type=int, default=42)
    beta_generate.add_argument("--calibration-manifest", type=Path, required=True)
    beta_generate.add_argument("--output", type=Path, required=True)
    beta_generate.add_argument("--rpm", type=float, required=True)
    beta_generate.add_argument("--i-peak-a", type=float, required=True)
    beta_generate.add_argument("--beta-values", default="-10,0,10,20,30,40,50,60,70,80")
    beta_generate.add_argument("--quality-profile", default="reference_ultra")

    beta_analyze = subparsers.add_parser("beta-analyze", help="Validate measured dq and select MTPA beta.")
    beta_analyze.add_argument("--results", type=Path, required=True)
    beta_analyze.add_argument("--calibration-manifest", type=Path, required=True)
    beta_analyze.add_argument(
        "--case-plan",
        type=Path,
        help="Enable strict ordered plan/result coverage validation for the stage gate.",
    )
    beta_analyze.add_argument("--summary", type=Path, required=True)
    beta_analyze.add_argument("--max-dq-current-relative-error", type=float, default=0.02)
    beta_analyze.add_argument(
        "--stage-beta-bounds",
        default="0,80",
        help="Inclusive beta bounds required for a stage-pass optimum (default: 0,80).",
    )
    beta_analyze.add_argument(
        "--require-stage-pass",
        action="store_true",
        help="Fail without writing a summary unless strict --case-plan validation and the stage gate pass.",
    )
    beta_analyze.add_argument(
        "--overwrite-summary",
        action="store_true",
        help="Explicitly replace an existing summary; default is fresh-output only.",
    )

    apply_manifest = subparsers.add_parser(
        "apply-manifest", help="Write a validated optimization spec with the physical dq zero."
    )
    apply_manifest.add_argument("--spec", type=Path, required=True)
    apply_manifest.add_argument("--calibration-manifest", type=Path, required=True)
    apply_manifest.add_argument("--output", type=Path, required=True)

    # Retain old command names only to produce an explicit migration failure.
    legacy_generate = subparsers.add_parser("generate", help=argparse.SUPPRESS)
    legacy_generate.add_argument("--source")
    legacy_generate.add_argument("--source-row")
    legacy_generate.add_argument("--output")
    legacy_generate.add_argument("--rpm")
    legacy_generate.add_argument("--i-peak-a")
    legacy_generate.add_argument("--electrical-zero-values")
    legacy_generate.add_argument("--beta-values")
    legacy_generate.add_argument("--quality-profile")
    legacy_analyze = subparsers.add_parser("analyze", help=argparse.SUPPRESS)
    legacy_analyze.add_argument("--results")
    legacy_analyze.add_argument("--manifest")
    return parser


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def write_json_object(path: Path, value: Mapping[str, Any], *, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists; pass explicit overwrite to replace it: {path}")
    text = json.dumps(dict(value), indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        if path.exists() and not overwrite:
            raise FileExistsError(f"output appeared before atomic commit: {path}")
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"generate", "analyze"}:
        raise SystemExit(
            "legacy loaded torque-max electrical-zero commands were removed; use zero-generate/zero-analyze "
            "followed by beta-generate/beta-analyze"
        )
    if args.command == "zero-generate":
        source = (
            read_source_row(args.source, args.source_row)
            if args.source
            else calibration_source_from_spec(args.spec, geometry_seed=args.geometry_seed)
        )
        rows = generate_zero_calibration_rows(
            source,
            speeds_rpm=parse_float_list(args.rpm_values),
            quality_profile=args.quality_profile,
        )
        write_rows(args.output, rows)
        print(f"generated_beta_zero_no_load rows={len(rows)} output={args.output}")
        return 0
    if args.command == "zero-analyze":
        with args.results.open("r", encoding="utf-8-sig", newline="") as file:
            manifest = analyze_zero_calibration_rows(
                csv.DictReader(file),
                max_circular_deviation_deg=args.max_circular_deviation_deg,
                min_distinct_speeds=args.min_distinct_speeds,
            )
        write_json_object(args.manifest, manifest)
        print(
            f"analyzed_beta_zero electrical_zero_deg={manifest['electrical_zero_deg']} "
            f"manifest={args.manifest}"
        )
        return 0
    if args.command == "beta-generate":
        source = (
            read_source_row(args.source, args.source_row)
            if args.source
            else calibration_source_from_spec(args.spec, geometry_seed=args.geometry_seed)
        )
        rows = generate_beta_sweep_rows(
            source,
            read_json_object(args.calibration_manifest),
            rpm=args.rpm,
            current_peak_a=args.i_peak_a,
            beta_values=parse_float_list(args.beta_values),
            quality_profile=args.quality_profile,
        )
        write_rows(args.output, rows)
        print(f"generated_beta_mtpa rows={len(rows)} output={args.output}")
        return 0
    if args.command == "apply-manifest":
        updated = apply_zero_manifest_to_spec(
            read_json_object(args.spec),
            read_json_object(args.calibration_manifest),
        )
        write_json_object(args.output, updated)
        print(f"wrote_calibrated_optimization_spec output={args.output}")
        return 0
    if args.require_stage_pass and args.case_plan is None:
        raise ValueError("--require-stage-pass requires strict --case-plan validation")
    if args.summary.exists() and not args.overwrite_summary:
        raise FileExistsError(
            f"summary already exists; use a fresh path or pass --overwrite-summary: {args.summary}"
        )
    with args.results.open("r", encoding="utf-8-sig", newline="") as file:
        result_rows = [dict(row) for row in csv.DictReader(file)]
    case_plan_rows: list[dict[str, str]] | None = None
    if args.case_plan is not None:
        with args.case_plan.open("r", encoding="utf-8-sig", newline="") as file:
            case_plan_rows = [dict(row) for row in csv.DictReader(file)]
    calibration_manifest = read_json_object(args.calibration_manifest)
    stage_beta_bounds = parse_beta_bounds(args.stage_beta_bounds)
    summary = analyze_beta_sweep_rows(
        result_rows,
        calibration_manifest,
        max_dq_current_relative_error=args.max_dq_current_relative_error,
        case_plan_rows=case_plan_rows,
        stage_beta_bounds_deg=stage_beta_bounds,
    )
    if case_plan_rows is None:
        validate_beta_sweep_summary(summary)
    else:
        validate_beta_sweep_summary(
            summary,
            case_plan_rows=case_plan_rows,
            result_rows=result_rows,
            calibration_manifest=calibration_manifest,
            require_stage_pass=args.require_stage_pass,
        )
    write_json_object(args.summary, summary, overwrite=args.overwrite_summary)
    print(f"analyzed_beta_mtpa best_beta_dq_deg={summary['best_beta_dq_deg']} summary={args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
