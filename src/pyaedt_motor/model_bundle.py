"""Checked forward-surrogate artifacts and geometry-level joint uncertainty.

The supported domain is deliberately conservative. A model passing its test
gate still needs independent FEA before a proposed design can be accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
from typing import Mapping

import joblib
import numpy as np
import pandas as pd

from .io import atomic_write_bytes
from . import surrogate
from .surrogate import GEOMETRY, PrimitiveModel, derive_performance, transform_target, inverse_target

SCHEMA = "ipmsm_physical_surrogate_v2"
VOLTAGE_SPAN_TARGET = "output_voltage_span_last_peak_v"
PRODUCTION_TARGETS = (*surrogate.TARGETS, VOLTAGE_SPAN_TARGET)
TARGETS = PRODUCTION_TARGETS
ALL_TARGETS = (*TARGETS, "output_total_loss_last_avg_w", "output_efficiency_last_pct")
REQUIRED_FINGERPRINTS = (
    "input_metric_contract_version", "input_producer_fingerprint", "input_material_fingerprint",
    "input_beta_calibration_id", "input_beta_convention", "input_model_extent", "input_quality_profile",
    "input_setup_fingerprint", "input_aedt_version", "input_dataset_schema_version",
)


def canonical_json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_files() -> dict[str, Path]:
    root = Path(__file__).resolve().parents[2]
    return {"model_bundle.py": Path(__file__), "surrogate.py": Path(surrogate.__file__),
            "reference_frames.py": Path(__file__).with_name("reference_frames.py"),
            "winding_frames.py": Path(__file__).with_name("winding_frames.py"),
            "production_zero.py": Path(__file__).with_name("production_zero.py"),
            "winding_native.py": Path(__file__).with_name("winding_native.py"),
            **{name: Path(__file__).with_name(name) for name in (
                "execution_compatibility.py", "execution_contracts.py", "execution_evidence.py",
                "execution_archive.py", "archive_numeric.py",
                "native_state_semantics.py", "public_batch_pilot.py", "batch_ownership.py",
                "quality.py", "cluster.py", "solver_profile.py")},
            **({"compdefs_transition.py": Path(__file__).with_name("compdefs_transition.py")}
               if Path(__file__).with_name("compdefs_transition.py").is_file() else {}),
            **{name: Path(__file__).with_name(name) for name in (
                "owned_sdk_production.py", "sdk_case_worker.py", "sdk_production_cluster.py", "sdk_node_fs.py",
                "sdk_launch.py", "_sdk_launch_binding.py", "_sdk_launch_resource.py")
               if Path(__file__).with_name(name).is_file()},
            "remote_case.py": Path(__file__).with_name("remote_case.py"),
            "doe.py": Path(__file__).with_name("doe.py"),
            "calibrate_ipmsm_beta.py": root / "calibrate_ipmsm_beta.py",
            "research_ns40_zero.py": root / "research_ns40_zero.py",
            "ipmsm_optimization.py": root / "ipmsm_optimization.py"}


def source_payload_sha256(payload: bytes) -> str:
    text = payload.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_contract() -> dict[str, str]:
    # Python normalizes source newlines and accepts a UTF-8 BOM. Preserve raw
    # snapshot hashes separately while comparing executable source across Git
    # LF and Windows CRLF checkouts. No other whitespace/code is normalized.
    return {name: source_payload_sha256(path.read_bytes()) for name, path in source_files().items()}


def dependency_versions() -> dict[str, str]:
    return {"python": platform.python_version(), **{
        name: importlib.metadata.version(name)
        for name in ["numpy", "pandas", "scipy", "scikit-learn", "lightgbm", "joblib"]}}


def capture_runtime_pin() -> dict:
    payloads = {name: path.read_bytes() for name, path in source_files().items()}
    contract = {name: source_payload_sha256(payload) for name, payload in payloads.items()}
    if contract != _IMPORTED_PREDICTION_SOURCES:
        raise RuntimeError("prediction code changed after import; start a fresh training process")
    pin = {"prediction_sources": contract, "dependencies": dependency_versions(), "source_payloads": payloads}
    assert_runtime_pin(pin)
    return pin


def assert_runtime_pin(pin: dict) -> None:
    if {name: source_payload_sha256(payload) for name, payload in pin["source_payloads"].items()} != pin["prediction_sources"]:
        raise RuntimeError("retained prediction source snapshot changed; refusing publication")
    if pin["prediction_sources"] != source_contract() or pin["dependencies"] != dependency_versions():
        raise RuntimeError("prediction code or dependencies changed during fitting; refusing publication")


def check_fingerprints(actual: Mapping, expected: Mapping | None = None) -> None:
    if not isinstance(actual, Mapping):
        raise ValueError("missing physical surrogate fingerprints")
    if any(not isinstance(actual.get(key), str) or not actual[key].strip() for key in REQUIRED_FINGERPRINTS):
        raise ValueError("missing physical surrogate fingerprint")
    if not re.fullmatch(r"producer_v3:sha256:[0-9a-f]{64}", actual["input_producer_fingerprint"]):
        raise ValueError("invalid physical producer fingerprint")
    if actual["input_dataset_schema_version"] != "ipmsm_v2":
        raise ValueError("unsupported physical dataset schema")
    if actual["input_metric_contract_version"] != "motor_metrics_v3":
        raise ValueError("physical surrogate requires motor_metrics_v3")
    if actual["input_beta_convention"] != "dq_current_advance_v2" or actual["input_model_extent"] != "full_360":
        raise ValueError("unsupported motor frame/model extent")
    for name, value in (expected or {}).items():
        if actual.get(name) != value:
            raise ValueError(f"surrogate fingerprint mismatch: {name}")


def physics_issues(predictions: pd.DataFrame) -> list[str]:
    if set(ALL_TARGETS) - set(predictions.columns):
        return ["missing_outputs"]
    if not np.isfinite(predictions[list(ALL_TARGETS)].to_numpy(float)).all():
        return ["nonfinite_predictions"]
    issues = []
    for key in ["output_torque_last_avg_nm", "output_ld_last_avg_h", "output_lq_last_avg_h",
                "output_phase_voltage_last_peak_abs_v", VOLTAGE_SPAN_TARGET]:
        if (predictions[key] <= 0).any():
            issues.append("nonpositive:" + key)
    phase_peak = predictions.output_phase_voltage_last_peak_abs_v.to_numpy(float)
    span = predictions[VOLTAGE_SPAN_TARGET].to_numpy(float)
    tolerance = np.maximum(1.0, 2 * np.abs(phase_peak)) * 1e-9
    if np.any(span > 2 * phase_peak + tolerance):
        issues.append("voltage_span_exceeds_two_phase_peaks")
    if (predictions.output_torque_last_max_nm < predictions.output_torque_last_avg_nm).any():
        issues.append("peak_torque_below_mean")
    for key in ["output_coreloss_last_avg_w", "output_solidloss_last_avg_w", "output_total_loss_last_avg_w"]:
        if (predictions[key] < 0).any():
            issues.append("negative:" + key)
    if not predictions.output_efficiency_last_pct.between(0, 100).all():
        issues.append("efficiency_out_of_range")
    return issues


def transformed_scales(inputs: pd.DataFrame, truth: pd.DataFrame) -> dict[str, float]:
    """Only fitting data determine standardization of joint residuals."""
    return {target: max(float(np.std(transform_target(inputs, target, truth[target]))), 1e-6)
            for target in TARGETS}


def calibrate_joint(inputs: pd.DataFrame, truth: pd.DataFrame, predictions: pd.DataFrame,
                    geometry_groups, scales: Mapping[str, float], coverage=.95) -> dict:
    """One maximum score per independent calibration geometry, not per row."""
    if not .5 < coverage < 1:
        raise ValueError("coverage must be between .5 and 1")
    groups = [str(group).strip() for group in geometry_groups]
    if len(groups) != len(inputs) or any(group.lower() in {"", "nan", "none"} for group in groups):
        raise ValueError("invalid calibration geometry identities")
    errors = []
    for target in TARGETS:
        scale = float(scales[target])
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("invalid training residual scale")
        errors.append(np.abs(transform_target(inputs, target, truth[target]) -
                             transform_target(inputs, target, predictions[target])) / scale)
    row_scores = np.max(np.asarray(errors), axis=0)
    if not np.isfinite(row_scores).all():
        raise ValueError("nonfinite calibration scores")
    group_scores = {}
    for group, score in zip(groups, row_scores):
        group_scores[group] = max(group_scores.get(group, 0.0), float(score))
    count = len(group_scores)
    rank = math.ceil((count + 1) * coverage)
    if rank > count or not count:
        raise ValueError("too few independent calibration geometries for a finite coverage bound")
    quantile = sorted(group_scores.values())[rank - 1]
    return {"method": "joint_max_transformed_residual_by_geometry", "coverage": coverage,
            "groups": count, "rows": len(inputs), "rank": rank, "quantile": quantile,
            "training_scales": dict(scales), "group_scores": dict(sorted(group_scores.items())),
            "assumption": "Nominal marginal coverage for exchangeable geometry groups; not a guarantee under optimization or distribution shift."}


WINDING_CASE_FIELDS = {
    "input_series_turns_per_phase": "series_turns_per_phase",
    "input_turns_per_coil_side": "turns_per_coil_side",
}


def case_winding_constants(frame: pd.DataFrame, winding: Mapping | None = None) -> dict:
    constants = {}
    for column, parameter in WINDING_CASE_FIELDS.items():
        if column not in frame:
            raise ValueError(f"missing actual case winding constant: {column}")
        values = pd.to_numeric(frame[column], errors="coerce").to_numpy(float)
        if (not len(values) or not np.isfinite(values).all() or np.any(values <= 0) or
                np.any(values != np.floor(values)) or len(set(values)) != 1):
            raise ValueError(f"mixed or invalid actual case winding constant: {column}")
        value = int(values[0])
        if winding is not None and value != winding[parameter]:
            raise ValueError(f"actual case winding differs from declared spec: {column}")
        constants[column] = value
    if "input_winding_material" in frame:
        values = [str(value).strip() for value in frame.input_winding_material]
        if any(not value or value.lower() in {"none", "nan"} for value in values) or len({value.casefold() for value in values}) != 1:
            raise ValueError("mixed or invalid actual winding material")
        constants["input_winding_material"] = values[0]
    return constants


def case_study_context(frame: pd.DataFrame, spec: Mapping, winding) -> dict:
    """Bind fixed material/wire declarations to actual FEA metadata, not features.

    Only literal legacy data may omit the newer context columns. In particular,
    copied flat plan fields cannot supply missing actual input_* reference data.
    Material fingerprints retain the existing upstream homogeneous-source gate;
    a core-only declaration cannot determine the magnet/shaft material choices.
    """
    from ipmsm_optimization import MaterialContextSpec

    raw = spec.get("material_context", {})
    if not isinstance(raw, Mapping):
        raise ValueError("material_context must be a fixed material declaration")
    material = MaterialContextSpec(**dict(raw))
    reference = material.core_material_model != "legacy_pnf_hybrid_v1"
    if not len(frame):
        raise ValueError("training study context requires actual cases")
    for key in ("core_material_model", "core_material"):
        column = "input_" + key
        if column not in frame:
            if reference:
                raise ValueError(f"missing actual material context: {column}")
            continue
        expected = getattr(material, key)
        if not frame[column].map(lambda value: isinstance(value, str) and value.strip() == expected).all():
            raise ValueError(f"actual material context differs from declared spec: {column}")
    wire = {"input_conductor_area_per_branch_mm2": winding.conductor_area_per_branch_mm2,
            "input_slot_fill_limit": winding.fill_factor}
    constants = {}
    if reference or any(column in frame for column in wire):
        for column, expected in wire.items():
            if column not in frame:
                raise ValueError(f"missing actual winding context: {column}")
            values = pd.to_numeric(frame[column], errors="coerce").to_numpy(float)
            if (frame[column].map(lambda value: isinstance(value, (bool, np.bool_))).any()
                    or not np.isfinite(values).all() or np.any(values <= 0)
                    or not np.allclose(values, expected, rtol=1e-12, atol=1e-12)):
                raise ValueError(f"actual winding context differs from declared spec: {column}")
            constants[column] = float(expected)
    return constants


def derived_geometry_values(geometry: Mapping[str, float]) -> dict[str, float]:
    outer = geometry["stator_outer_radius"]
    yoke = outer * geometry["stator_back_yoke_thick_ratio"]
    inner = outer * geometry["stator_inner_ratio"]
    tooth = (outer - yoke - inner) * geometry["stator_teeth_length_ratio"]
    width = (outer - yoke - tooth) * math.tan(math.pi / 12) * geometry["stator_teeth_width_ratio"] * 2
    rotor = inner - geometry["rotator_gap"]
    return {"stator_back_yoke_thick": yoke, "stator_inner_radius": inner,
            "stator_teeth_length": tooth, "stator_teeth_width": width,
            "rotor_radius": rotor, "shaft_radius": rotor * geometry["shaft_ratio"]}


def validate_raw_geometry_fields(frame: pd.DataFrame) -> None:
    """A new producer cannot invoke historical repair to redefine a motor."""
    derived = {"stator_back_yoke_thick", "stator_inner_radius", "stator_teeth_length",
               "stator_teeth_width", "rotor_radius", "shaft_radius"}
    required = {"input_" + name for name in (*GEOMETRY, *derived, "slot_num", "pole_num")}
    if required - set(frame):
        raise ValueError("missing raw v3 geometry columns")
    numeric = frame[sorted(required)].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(float)).all():
        raise ValueError("nonfinite raw v3 geometry")
    if not numeric.input_slot_num.eq(12).all() or not numeric.input_pole_num.eq(8).all():
        raise ValueError("raw v3 geometry must use12 slots and8 poles")
    for _, row in numeric.iterrows():
        geometry = {name: float(row["input_" + name]) for name in GEOMETRY}
        for name, expected in derived_geometry_values(geometry).items():
            if not math.isclose(float(row["input_" + name]), expected, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"inconsistent raw v3 derived geometry: {name}")


def _geometry_checks(inputs: pd.DataFrame, declaration: dict) -> None:
    from ipmsm_optimization import WindingSpec, GEOMETRY_VARIABLE_NAMES, geometry_metrics, phase_resistance_100c_ohm
    winding = WindingSpec(**declaration["winding"])
    for _, row in inputs.iterrows():
        geometry = {key: float(row["input_" + key]) for key in GEOMETRY_VARIABLE_NAMES}
        length = float(row["input_stack_length_mm"])
        metrics = geometry_metrics(geometry, length, winding)
        if metrics.slot_fill_ratio > winding.fill_factor + 1e-9:
            raise ValueError("surrogate input exceeds declared slot fill")
        resistance = phase_resistance_100c_ohm(geometry, length, winding)
        if not math.isclose(float(row["input_phase_resistance_ohm"]), resistance, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError("phase resistance disagrees with geometry/winding contract")
        for key, value in derived_geometry_values(geometry).items():
            if "input_" + key in row and not math.isclose(float(row["input_" + key]), value, rel_tol=1e-6, abs_tol=1e-8):
                raise ValueError(f"inconsistent derived input: {key}")


def validate_declared_inputs(inputs: pd.DataFrame, declaration: dict) -> None:
    if not len(inputs) or not np.isfinite(inputs.to_numpy(float)).all():
        raise ValueError("model inputs must be finite numeric nonempty rows")
    if (inputs.input_i_peak_a <= 0).any() or (inputs.input_base_rpm <= 0).any():
        raise ValueError("loaded motor inputs require positive current and speed")
    if not inputs.input_beta_dq_deg.between(0, 90, inclusive="left").all():
        raise ValueError("loaded motor inputs require 0 <= beta < 90 degrees")
    for column, (lower, upper) in declaration["bounds"].items():
        values = inputs[column].to_numpy(float)
        if np.any(values < lower - 1e-10) or np.any(values > upper + 1e-10):
            raise ValueError(f"input outside declared motor domain: {column}")
    for column, value in declaration["constants"].items():
        if not np.allclose(inputs[column].to_numpy(float), value, rtol=0, atol=1e-10):
            raise ValueError(f"unsupported motor constant: {column}")
    if not np.isin(inputs.input_base_rpm.to_numpy(float), declaration["speeds_rpm"]).all():
        raise ValueError("unsupported motor speed; interpolation between discrete speeds is not validated")
    _geometry_checks(inputs, declaration)


def domain_from_training(inputs: pd.DataFrame, spec: dict, *, case_frame: pd.DataFrame | None = None, frame_evidence=None) -> dict:
    """Declared engineering limits plus a conservative empirical support box."""
    from dataclasses import asdict
    from ipmsm_optimization import WindingSpec, GEOMETRY_VARIABLE_NAMES
    winding = WindingSpec(**spec["winding"])
    if winding.parallel_branches != 1 or winding.coils_per_phase != 4:
        raise ValueError("FEA winding topology requires one parallel branch and four coils per phase")
    if not math.isclose(winding.winding_temperature_c, 100):
        raise ValueError("this resistance contract supports 100 degC winding temperature")
    actual_cases = inputs if case_frame is None else case_frame
    from .winding_frames import verify_spec_rows
    frame_context = verify_spec_rows(spec, actual_cases.to_dict("records"), frame_evidence)
    study_constants = case_study_context(actual_cases, spec, winding)
    missing = {"input_" + key for key in GEOMETRY_VARIABLE_NAMES} - set(inputs)
    if missing:
        raise ValueError(f"missing independent geometry inputs: {sorted(missing)}")
    bounds = {"input_" + key: list(map(float, spec["design_space"][key])) for key in GEOMETRY_VARIABLE_NAMES}
    bounds["input_stack_length_mm"] = list(map(float, spec["stack_length_bounds_mm"]))
    for pair in bounds.values():
        if len(pair) != 2 or not all(math.isfinite(value) for value in pair) or pair[0] > pair[1]:
            raise ValueError("invalid declared geometry bounds")
    current_fraction = float(spec.get("control", {}).get("minimum_current_fraction", .25))
    if not 0 < current_fraction <= 1:
        raise ValueError("minimum current fraction must be positive and <= 1")
    if not math.isfinite(float(spec["constraints"]["current_density_limit_a_per_mm2"])):
        raise ValueError("current density limit must be finite")
    current_max = min(float(spec["inverter"]["phase_peak_current_limit_a"]),
                      float(spec["constraints"]["current_density_limit_a_per_mm2"]) * math.sqrt(2) *
                      winding.total_parallel_conductor_area_mm2)
    if not math.isfinite(current_max) or current_max <= 0:
        raise ValueError("phase current limit must be positive and finite")
    bounds["input_i_peak_a"] = [current_fraction * current_max, current_max]
    bounds["input_beta_dq_deg"] = list(map(float, spec["control"]["beta_bounds_deg"]))
    constants = {"input_slot_num": 12.0, "input_pole_num": 8.0, "input_stator_shoe_thick": 1.5}
    speeds = sorted({float(point["speed_rpm"]) for point in spec["operating_points"]})
    declaration = {"bounds": bounds, "constants": constants, "speeds_rpm": speeds, "winding": asdict(winding)}
    validate_declared_inputs(inputs, declaration)
    winding_values = case_winding_constants(actual_cases, asdict(winding))
    winding_values.update(study_constants)
    observed = {column: [float(inputs[column].min()), float(inputs[column].max())] for column in inputs}
    return {"input_columns": list(inputs.columns), "declaration": declaration, "observed_bounds": observed,
            **({"reference_frame_context": frame_context} if frame_context is not None else {}),
            "winding_case_constants": winding_values, "winding_case_source": "fea_input_columns",
            "support_note": "Prediction rejects extrapolation beyond training coordinate bounds; a box does not prove dense joint coverage."}


def validate_inputs(inputs: pd.DataFrame, domain: dict) -> pd.DataFrame:
    columns = domain["input_columns"]
    if len(columns) != len(set(columns)) or set(columns) - set(inputs):
        raise ValueError("surrogate input schema mismatch")
    context = dict(domain["winding_case_constants"])
    context.update({"input_" + name: value for name, value in domain["declaration"]["winding"].items()})
    for column, expected in context.items():
        if column not in inputs:
            continue
        if isinstance(expected, str):
            matches = inputs[column].map(lambda value: str(value).strip().casefold() == expected.casefold()).all()
        else:
            values = pd.to_numeric(inputs[column], errors="coerce").to_numpy(float)
            matches = np.isfinite(values).all() and np.allclose(values, expected, rtol=1e-10, atol=1e-12)
        if not matches:
            raise ValueError(f"prediction winding context differs from trained motor: {column}")
    selected = inputs[columns].astype(float)
    validate_declared_inputs(selected, domain["declaration"])
    for column, (lower, upper) in domain["observed_bounds"].items():
        tolerance = max(abs(lower), abs(upper), 1.0) * 1e-10
        values = selected[column].to_numpy(float)
        if np.any(values < lower - tolerance) or np.any(values > upper + tolerance):
            raise ValueError(f"input outside supported training domain: {column}")
    return selected


def prediction_frame(models: Mapping[str, PrimitiveModel], inputs: pd.DataFrame) -> pd.DataFrame:
    if set(models) != set(TARGETS):
        raise ValueError("surrogate primitive coverage mismatch")
    primitives = pd.DataFrame({target: models[target].predict(inputs) for target in TARGETS}, index=inputs.index)
    return derive_performance(inputs, primitives)


def promotion_issues(metadata: dict) -> list[str]:
    issues = []
    try:
        check_fingerprints(metadata["fingerprints"])
        calibration = metadata["calibration"]
        n = int(calibration["groups"])
        rank = math.ceil((n + 1) * float(calibration["coverage"]))
        if n <= 0 or float(calibration["coverage"]) != .95 or rank > n or rank != calibration["rank"]:
            issues.append("invalid_joint_calibration")
        if not math.isfinite(float(calibration["quantile"])) or calibration["quantile"] < 0:
            issues.append("invalid_joint_quantile")
        if set(calibration["training_scales"]) != set(TARGETS) or any(
                not math.isfinite(float(value)) or float(value) <= 0
                for value in calibration["training_scales"].values()):
            issues.append("invalid_calibration_training_scales")
        scores = sorted(float(value) for value in calibration["group_scores"].values())
        if (len(scores) != n or any(not math.isfinite(value) or value < 0 for value in scores) or
                rank < 1 or rank > len(scores) or
                not math.isclose(float(calibration["quantile"]), scores[rank - 1], rel_tol=1e-12, abs_tol=1e-12)):
            issues.append("joint_quantile_does_not_match_group_scores")
        if not isinstance(metadata.get("test_physics_issues"), list):
            issues.append("missing_test_physics_evidence")
        elif metadata["test_physics_issues"]:
            issues.append("test_physical_predictions_failed")
        for target in ALL_TARGETS:
            metric = metadata["test_metrics"][target]
            score = float(metric["r2"])
            if not metric.get("finite") or not math.isfinite(score) or score < .95:
                issues.append("r2_failed:" + target)
        if metadata.get("test_used_for_selection") is not False:
            issues.append("test_selection_policy_unverified")
        domain = metadata["domain"]
        winding = domain["declaration"]["winding"]
        if (domain.get("winding_case_source") != "fea_input_columns" or
                winding["parallel_branches"] != 1 or winding["coils_per_phase"] != 4 or
                any(domain["winding_case_constants"].get(column) != winding[key]
                    for column, key in WINDING_CASE_FIELDS.items())):
            issues.append("unverified_winding_constants")
        fitting = set(metadata["fit_groups"])
        calibration_groups = set(metadata["calibration_groups"])
        test_groups = set(metadata["test_groups"])
        if (len(fitting) < 2 or len(test_groups) < 2 or len(calibration_groups) != n or
                fitting & calibration_groups or fitting & test_groups or calibration_groups & test_groups or
                set(calibration["group_scores"]) != calibration_groups):
            issues.append("invalid_geometry_partitions")
        choice = metadata["selection"]
        if hashlib.sha256(canonical_json(choice)).hexdigest() != metadata["selection_sha256"]:
            issues.append("selection_evidence_changed")
        inner_fit, inner_selection = set(choice["fit_groups"]), set(choice["selection_groups"])
        if inner_fit & inner_selection or (inner_fit | inner_selection) != fitting:
            issues.append("selection_not_confined_to_training")
        if choice.get("test_used_for_selection") is not False:
            issues.append("selection_used_test")
    except (KeyError, TypeError, ValueError, OverflowError):
        issues.append("incomplete_or_invalid_promotion_evidence")
    return issues


@dataclass
class PhysicalMotorBundle:
    models: dict[str, PrimitiveModel]
    metadata: dict

    @property
    def approved(self) -> bool:
        return self.metadata.get("decision") == "accepted" and not promotion_issues(self.metadata)

    def predict(self, inputs: pd.DataFrame, *, fingerprints=None, allow_rejected=False,
                with_bounds=True) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame] | pd.DataFrame:
        if not self.approved and not allow_rejected:
            raise ValueError("surrogate is rejected for design optimization")
        check_fingerprints(self.metadata["fingerprints"], fingerprints)
        selected = validate_inputs(inputs, self.metadata["domain"])
        point = prediction_frame(self.models, selected)
        if physics_issues(point):
            raise ValueError("surrogate prediction violates physical output contract")
        if not with_bounds:
            return point
        calibration = self.metadata["calibration"]
        if "quantile" not in calibration or "training_scales" not in calibration:
            raise ValueError("artifact has no finite joint calibration")
        lower, upper = {}, {}
        for target in TARGETS:
            center = transform_target(selected, target, point[target])
            halfwidth = calibration["quantile"] * calibration["training_scales"][target]
            with np.errstate(over="ignore", invalid="ignore"):
                lower[target] = inverse_target(selected, target, center - halfwidth)
                upper[target] = inverse_target(selected, target, center + halfwidth)
        lower = pd.DataFrame(lower, index=selected.index)
        upper = pd.DataFrame(upper, index=selected.index)
        copper = 1.5 * selected.input_phase_resistance_ohm * selected.input_i_peak_a**2
        loss_lo = copper + lower.output_coreloss_last_avg_w + lower.output_solidloss_last_avg_w
        loss_hi = copper + upper.output_coreloss_last_avg_w + upper.output_solidloss_last_avg_w
        omega = selected.input_base_rpm * 2 * np.pi / 60
        power_lo = np.maximum(0, lower.output_torque_last_avg_nm) * omega
        power_hi = np.maximum(0, upper.output_torque_last_avg_nm) * omega
        lower["output_total_loss_last_avg_w"], upper["output_total_loss_last_avg_w"] = loss_lo, loss_hi
        lower["output_efficiency_last_pct"] = 100 * power_lo / (power_lo + loss_hi)
        upper["output_efficiency_last_pct"] = 100 * power_hi / (power_hi + loss_lo)
        if not np.isfinite(lower.to_numpy()).all() or not np.isfinite(upper.to_numpy()).all():
            raise ValueError("joint uncertainty bounds are not finite at this input")
        return point, lower, upper


def _reject_links(path: Path) -> None:
    for item in [path, *path.parents]:
        if not item.exists():
            continue
        attributes = item.lstat()
        if item.is_symlink() or (os.name == "nt" and attributes.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("surrogate artifact cannot use symlink/reparse paths")


def _safe_file(root: Path, relative: str) -> Path:
    name = PurePosixPath(relative)
    if name.is_absolute() or ".." in name.parts or "\\" in relative or ":" in relative:
        raise ValueError("artifact filename escapes its directory")
    result = root.joinpath(*name.parts)
    _reject_links(result)
    return result


def save_bundle(bundle: PhysicalMotorBundle, destination: Path, *, runtime_pin=None, publication_guard=None) -> None:
    """Exclusive directory; COMPLETE is the atomic publication marker."""
    runtime_pin = capture_runtime_pin() if runtime_pin is None else runtime_pin
    assert_runtime_pin(runtime_pin)
    if publication_guard is not None:
        publication_guard()
    root = Path(os.path.abspath(destination))
    _reject_links(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(exist_ok=False)
    check_fingerprints(bundle.metadata["fingerprints"])
    metadata = dict(bundle.metadata)
    metadata.update(schema=SCHEMA, prediction_sources=runtime_pin["prediction_sources"],
                    prediction_source_format="utf8_no_bom_universal_newlines", dependencies=runtime_pin["dependencies"])
    bundle.metadata = metadata
    (root / "source").mkdir()
    for name, payload in runtime_pin["source_payloads"].items():
        atomic_write_bytes(root / "source" / name, payload)
    joblib.dump(bundle.models, root / "models.joblib")
    atomic_write_bytes(root / "metadata.json", canonical_json(metadata))
    paths = ["models.joblib", "metadata.json", *["source/" + name for name in source_files()]]
    manifest = {"schema": SCHEMA, "files": {name: {"sha256": sha256(root / name),
                "bytes": (root / name).stat().st_size} for name in paths}}
    atomic_write_bytes(root / "manifest.json", canonical_json(manifest))
    assert_runtime_pin(runtime_pin)
    if publication_guard is not None:
        publication_guard()
    atomic_write_bytes(root / "COMPLETE", sha256(root / "manifest.json").encode())


def load_bundle(directory: Path, *, expected_fingerprints=None, require_approved=True) -> PhysicalMotorBundle:
    root = Path(os.path.abspath(directory))
    _reject_links(root)
    _safe_file(root, "COMPLETE")
    _safe_file(root, "manifest.json")
    if not root.is_dir() or not (root / "COMPLETE").is_file():
        raise ValueError("surrogate artifact is incomplete")
    manifest_payload = (root / "manifest.json").read_bytes()
    if (root / "COMPLETE").read_text(encoding="utf-8") != hashlib.sha256(manifest_payload).hexdigest():
        raise ValueError("surrogate manifest integrity mismatch")
    manifest = json.loads(manifest_payload)
    if manifest.get("schema") != SCHEMA:
        raise ValueError("unsupported surrogate artifact schema")
    expected_files = {"models.joblib", "metadata.json", *["source/" + name for name in source_files()]}
    if set(manifest.get("files", {})) != expected_files:
        raise ValueError("surrogate artifact filename/schema mismatch")
    checked = {}
    for name, record in manifest["files"].items():
        payload = _safe_file(root, name).read_bytes()
        if len(payload) != record["bytes"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise ValueError(f"surrogate artifact integrity mismatch: {name}")
        if name in {"models.joblib", "metadata.json"}:
            checked[name] = payload
    metadata = json.loads(checked["metadata.json"])
    if (metadata.get("schema") != SCHEMA or metadata.get("prediction_sources") != source_contract() or
            metadata.get("prediction_sources") != _IMPORTED_PREDICTION_SOURCES or
            metadata.get("prediction_source_format") != "utf8_no_bom_universal_newlines"):
        raise ValueError("surrogate prediction source contract changed; use its matching source revision")
    if metadata.get("dependencies") != dependency_versions():
        raise ValueError("surrogate dependency versions changed")
    check_fingerprints(metadata["fingerprints"], expected_fingerprints)
    if require_approved and (metadata.get("decision") != "accepted" or promotion_issues(metadata)):
        raise ValueError("surrogate artifact is rejected for design optimization")
    if require_approved and (not expected_fingerprints or set(REQUIRED_FINGERPRINTS) - set(expected_fingerprints)):
        raise ValueError("optimization load requires expected producer/material/frame fingerprints")
    # Deserialize the very bytes already checked; never reopen a mutable path.
    models = joblib.load(io.BytesIO(checked["models.joblib"]))
    if not isinstance(models, dict) or set(models) != set(TARGETS):
        raise ValueError("surrogate model target schema mismatch")
    for target, model in models.items():
        if not isinstance(model, PrimitiveModel) or model.target != target or list(model.raw_columns) != metadata["domain"]["input_columns"]:
            raise ValueError("surrogate model feature schema mismatch")
    return PhysicalMotorBundle(models, metadata)


# Detect ordinary editable-checkout drift before starting a fit or publication.
_IMPORTED_PREDICTION_SOURCES = source_contract()
