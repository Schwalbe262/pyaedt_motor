"""Generate and analyze fixed-geometry electrical-zero calibration sweeps."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from generate_ipmsm_quality_cases import MESH_ELEMENT_KEYS, QUALITY_PROFILES
from run_ipmsm_batch import extract_fixed_geometry


BETA_CONVENTION = "dq_current_advance_v2"
DATASET_SCHEMA_VERSION = "ipmsm_v2"
ZERO_CALIBRATION_METHOD = "signed_phasea_back_emf_fundamental_v2"
MTPA_VALIDATION_METHOD = "loaded_beta_sweep_v2"


def parse_float_list(text: str) -> list[float]:
    values = [float(item.strip()) for item in text.split(",") if item.strip()]
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("at least one finite value is required")
    return values


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
) -> dict[str, Any]:
    electrical_zero_deg, calibration_id = validated_zero_manifest(calibration_manifest)
    if not math.isfinite(max_dq_current_relative_error) or max_dq_current_relative_error < 0.0:
        raise ValueError("max_dq_current_relative_error must be finite and nonnegative")
    from module.ipmsm_ppt_setup import canonical_dq_current_components

    points: list[dict[str, float]] = []
    identities: dict[str, set[str]] = {
        "geometry_group_id": set(),
        "design_hash": set(),
        "input_setup_fingerprint": set(),
    }
    for row in rows:
        if str(row.get("status") or "").strip().lower() != "ok":
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
        for column, values in identities.items():
            value = str(row.get(column) or "").strip()
            if value:
                values.add(value)
    if len(points) < 3 or len({point["beta_dq_deg"] for point in points}) < 3:
        raise ValueError("MTPA validation needs at least three successful distinct beta rows")
    if any(len(values) > 1 for values in identities.values()):
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
    payload: dict[str, Any] = {
        "workflow_version": "beta_calibration_v2",
        "method": MTPA_VALIDATION_METHOD,
        "convention": BETA_CONVENTION,
        "beta_calibration_id": calibration_id,
        "electrical_zero_deg": electrical_zero_deg,
        "best_beta_dq_deg": best["beta_dq_deg"],
        "best_torque_nm": best["torque_nm"],
        "best_torque_per_peak_amp": best["torque_per_peak_amp"],
        "speed_rpm": best["speed_rpm"],
        "current_peak_a": best["current_peak_a"],
        "points": points,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    payload["sweep_id"] = f"beta-mtpa:sha256:{hashlib.sha256(encoded).hexdigest()}"
    return payload


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
    beta_analyze.add_argument("--summary", type=Path, required=True)
    beta_analyze.add_argument("--max-dq-current-relative-error", type=float, default=0.02)

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


def write_json_object(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    with args.results.open("r", encoding="utf-8-sig", newline="") as file:
        summary = analyze_beta_sweep_rows(
            csv.DictReader(file),
            read_json_object(args.calibration_manifest),
            max_dq_current_relative_error=args.max_dq_current_relative_error,
        )
    write_json_object(args.summary, summary)
    print(f"analyzed_beta_mtpa best_beta_dq_deg={summary['best_beta_dq_deg']} summary={args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
