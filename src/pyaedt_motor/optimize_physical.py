"""Batched, conservative design proposals from an approved physical bundle.

No FEA is submitted and no held-out labels are read. A supported prediction is
not an independently verified motor design, nor a proof of global optimality.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
import threading
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.stats import qmc

from ipmsm_optimization import (
    OperatingPoint, active_volume_m3,
    current_density_a_per_mm2, dq_currents, geometry_metrics, fixed_case_context_fields,
    optimization_spec_from_mapping, phase_resistance_100c_ohm,
)
from . import doe
from . import model_bundle as bundle_runtime
from .doe import ACTIVE_GEOMETRY, physical_geometry_hash, sample_feasible_geometries
from .model_bundle import (
    ALL_TARGETS, REQUIRED_FINGERPRINTS, PhysicalMotorBundle, canonical_json,
    check_fingerprints, derived_geometry_values, load_bundle, physics_issues,
    sha256, validate_inputs,
)

TORQUE = "output_torque_last_avg_nm"
PHASE_VOLTAGE = "output_phase_voltage_last_peak_abs_v"
VOLTAGE_SPAN = "output_voltage_span_last_peak_v"
LOSS = "output_total_loss_last_avg_w"
EFFICIENCY = "output_efficiency_last_pct"
PROPOSAL_STATUS = "surrogate_proposal_requires_independent_fea"
_OPTIMIZER_SOURCE_PATHS = {"optimize_physical.py": Path(__file__), "doe.py": Path(doe.__file__)}
_IMPORTED_OPTIMIZER_SOURCES = {name: sha256(path) for name, path in _OPTIMIZER_SOURCE_PATHS.items()}
_IMPORTED_OPTIMIZER_SHA256 = _IMPORTED_OPTIMIZER_SOURCES["optimize_physical.py"]


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _execution_context():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), id(task) if task is not None else None


@dataclass
class PredictionBatch:
    inputs: pd.DataFrame
    point: pd.DataFrame
    lower: pd.DataFrame
    upper: pd.DataFrame
    unsupported: dict[str, int]
    requested: int


class PhysicalBundleAdapter:
    """Bind an approved bundle to independent fingerprint/spec/zero context.

    ``source_spec`` is the original, unmodified training mapping. A separate
    hashed v3 zero manifest supplies the parser's calibrated beta block without
    changing that mapping's training SHA. No rejected-model override exists.
    """

    def __init__(self, bundle: PhysicalMotorBundle, source_spec: Mapping,
                 expected_fingerprints: Mapping, calibration_manifest: Mapping, *, frame_evidence=None):
        if set(REQUIRED_FINGERPRINTS) - set(expected_fingerprints):
            raise ValueError("optimization requires the full independent fingerprint context")
        check_fingerprints(bundle.metadata["fingerprints"], expected_fingerprints)
        if VOLTAGE_SPAN not in ALL_TARGETS:
            raise ValueError("optimization requires a span-bearing physical bundle contract")
        if not bundle.approved:
            raise ValueError("rejected or unverified surrogate cannot propose a design")
        self.source_spec = deepcopy(dict(source_spec))
        if bundle.metadata.get("spec_sha256") != _digest(self.source_spec):
            raise ValueError("source spec differs from the surrogate training spec")
        from . import winding_frames as frames
        calibration_manifest = deepcopy(dict(calibration_manifest))
        if (calibration_manifest.get("metric_contract") != "motor_metrics_v3" or
                calibration_manifest.get("back_emf_source") != "periodic_flux_derivative"):
            raise ValueError("optimization requires a v3 physical flux-derived zero manifest")
        calibrated = frames.apply_reference(self.source_spec, calibration_manifest, frame_evidence)
        if "beta_calibration" in self.source_spec and self.source_spec["beta_calibration"] != calibrated["beta_calibration"]:
            raise ValueError("source spec and calibrated-zero manifest disagree")
        self.spec = optimization_spec_from_mapping(calibrated)
        if (self.spec.beta_calibration.calibration_id != expected_fingerprints["input_beta_calibration_id"] or
                self.spec.beta_calibration.convention != expected_fingerprints["input_beta_convention"]):
            raise ValueError("calibrated-zero frame differs from expected fingerprints")
        if calibration_manifest.get("material_fingerprint") not in (None, expected_fingerprints["input_material_fingerprint"]):
            raise ValueError("zero calibration material differs from the approved model")
        self.bundle = bundle
        self.fingerprints = deepcopy(dict(expected_fingerprints))
        self.domain = deepcopy(bundle.metadata["domain"])
        self.calibration_manifest = calibration_manifest
        self.frame_evidence = frame_evidence
        if frames.is_family(calibration_manifest):
            frames.verify_domain(self.domain, calibration_manifest, frame_evidence)
        self._bind_domain()
        self._metadata_sha256 = _digest(bundle.metadata)
        self.artifact_provenance: dict = {"kind": "in_memory_approved_bundle"}
        self._search_session = ContextVar("checked_search_" + str(id(self)), default=None)

    def _session(self):
        session = self._search_session.get()
        if session is not None and session["active"] and session["owner"] == _execution_context():
            return session
        return None

    def _context_pin(self):
        return _digest({"metadata": self.bundle.metadata, "fingerprints": self.fingerprints,
            "domain": self.domain, "spec": asdict(self.spec), "source_spec": self.source_spec,
            "manifest": self.calibration_manifest, "artifact": self.artifact_provenance,
            "imported_optimizer_sources": _IMPORTED_OPTIMIZER_SOURCES,
            "imported_prediction_sources": bundle_runtime._IMPORTED_PREDICTION_SOURCES,
            "evidence_identity": id(self.frame_evidence)})

    def _full_search_check(self):
        if _digest(self.bundle.metadata) != self._metadata_sha256:
            raise ValueError("bundle metadata changed after optimization context binding")
        if self.frame_evidence is not None:
            self.frame_evidence.assert_unchanged()
        if any(sha256(path) != _IMPORTED_OPTIMIZER_SOURCES[name] for name, path in _OPTIMIZER_SOURCE_PATHS.items()):
            raise ValueError("optimizer source changed; refusing checked search publication")
        if bundle_runtime.source_contract() != bundle_runtime._IMPORTED_PREDICTION_SOURCES:
            raise ValueError("model prediction source changed; refusing checked search publication")

    def _check_prediction_evidence(self):
        session = self._session()
        if session is None:
            if self.frame_evidence is not None:
                self.frame_evidence.assert_unchanged()
            return
        if self._context_pin() != session["pin"]:
            raise ValueError("checked search memory authority or model context changed")
        if self.frame_evidence is not None:
            memory_check = getattr(self.frame_evidence, "assert_memory_unchanged", None)
            if memory_check is not None:
                memory_check()
            else:
                from . import winding_frames as frames
                if frames.digest(self.frame_evidence.mapping) != self.frame_evidence._mapping_digest:
                    raise ValueError("local evidence mapping changed in memory")

    @contextmanager
    def _checked_search(self, *, join=False):
        # Only this synchronous atomic call may defer disk scans. A copied
        # context on another thread/task and a closed session remain strict.
        if join and self._session() is not None:
            self._check_prediction_evidence()
            try:
                yield
            finally:
                self._check_prediction_evidence()
            return
        self._full_search_check()
        session = {"owner": _execution_context(), "pin": self._context_pin(), "active": True}
        token = self._search_session.set(session)
        try:
            yield
        finally:
            try:
                self._check_prediction_evidence()
                self._full_search_check()
            finally:
                session["active"] = False
                self._search_session.reset(token)

    @classmethod
    def from_directory(cls, directory: Path, source_spec: Mapping,
                        expected_fingerprints: Mapping, calibration_manifest: Mapping, *, frame_evidence=None):
        bundle = load_bundle(Path(directory), expected_fingerprints=expected_fingerprints,
                             require_approved=True)
        result = cls(bundle, source_spec, expected_fingerprints, calibration_manifest, frame_evidence=frame_evidence)
        result.artifact_provenance = {"kind": "checked_artifact", "path": os.path.abspath(directory),
                                      "manifest_sha256": sha256(Path(directory) / "manifest.json")}
        return result

    def _bind_domain(self):
        spec, declaration = self.spec, self.domain["declaration"]
        bounds = {"input_" + bound.name: [bound.lower, bound.upper] for bound in spec.design_space}
        fraction = float(self.source_spec.get("control", {}).get("minimum_current_fraction", .25))
        if not 0 < fraction <= 1:
            raise ValueError("invalid declared minimum current fraction")
        bounds.update(input_i_peak_a=[fraction * spec.effective_peak_current_limit_a,
                                     spec.effective_peak_current_limit_a],
                      input_beta_dq_deg=list(spec.beta_bounds_deg))
        expected = {"bounds": bounds, "winding": asdict(spec.winding),
                    "constants": {"input_slot_num": 12., "input_pole_num": 8., "input_stator_shoe_thick": 1.5},
                    "speeds_rpm": sorted({point.speed_rpm for point in spec.operating_points})}
        if declaration != expected:
            raise ValueError("surrogate declared geometry/winding/control domain differs from spec")
        constants = self.domain["winding_case_constants"]
        if (constants.get("input_series_turns_per_phase") != spec.winding.series_turns_per_phase or
                constants.get("input_turns_per_coil_side") != spec.winding.turns_per_coil_side):
            raise ValueError("actual trained winding differs from declared spec")
        if (spec.winding.parallel_branches != 1 or spec.winding.coils_per_phase != 4 or
                spec.winding.winding_temperature_c != 100):
            raise ValueError("unsupported physical winding topology or resistance temperature")
        if (expected["speeds_rpm"] != [1200., 5000.] or len(spec.operating_points) != 2 or
                any(not math.isclose(point.required_power_w, 7500., rel_tol=0, abs_tol=1e-7)
                    for point in spec.operating_points)):
            raise ValueError("this proposal search requires7500W at1200/5000rpm")
        if (spec.inverter.vdc_v != 200 or spec.inverter.voltage_utilization > .95 or
                spec.current_limit_a > 137.8 or spec.constraints.current_density_limit_a_per_mm2 > 6.36 or
                spec.winding.fill_factor > .45 or spec.stack_length_bounds.lower < 40 or
                spec.stack_length_bounds.upper > 70 or spec.geometry_design_space[0].upper > 150 or
                not 0 <= spec.beta_bounds_deg[0] < spec.beta_bounds_deg[1] <= 80):
            raise ValueError("source spec exceeds the approved physical design scope")

    def normalize_design(self, design: Mapping) -> dict[str, float]:
        missing = set(self.spec.design_variable_names) - set(design)
        if missing:
            raise ValueError(f"missing independent design variables: {sorted(missing)}")
        values = {}
        for bound in self.spec.design_space:
            raw = design[bound.name]
            if isinstance(raw, bool):
                raise ValueError("design variables must be finite numbers")
            value = float(raw)
            if not math.isfinite(value) or not bound.lower <= value <= bound.upper:
                raise ValueError(f"design outside declared bounds: {bound.name}")
            values[bound.name] = value
        if values["stator_shoe_thick"] != 1.5:
            raise ValueError("inactive stator_shoe_thick must remain1.5")
        # Match the FEA model-depth assignment precision before computing R/volume.
        values["stack_length_mm"] = float(f"{values['stack_length_mm']:.6g}")
        if not self.spec.stack_length_bounds.lower <= values["stack_length_mm"] <= self.spec.stack_length_bounds.upper:
            raise ValueError("rounded FEA model depth is outside declared bounds")
        return values

    def build_inputs(self, design: Mapping, point: OperatingPoint,
                     controls: Sequence[tuple[float, float]]) -> pd.DataFrame:
        self._check_prediction_evidence()
        values = self.normalize_design(design)
        length = values["stack_length_mm"]
        metrics = geometry_metrics(values, length, self.spec.winding, slot_number=self.spec.slot_number)
        if metrics.slot_fill_ratio > self.spec.winding.fill_factor + 1e-9:
            raise ValueError("design exceeds declared slot fill")
        resistance = phase_resistance_100c_ohm(values, length, self.spec.winding, slot_number=self.spec.slot_number)
        base = {"input_" + key: value for key, value in values.items()}
        # Same six explicit derived fields used by the FEA/flat training contract.
        base.update({"input_" + key: value for key, value in derived_geometry_values(values).items()})
        base.update(self.domain["declaration"]["constants"])
        base.update(self.domain["winding_case_constants"])
        base.update({"input_" + key: value for key, value in asdict(self.spec.winding).items()})
        base.update(input_base_rpm=point.speed_rpm, input_phase_resistance_ohm=resistance)
        for column in self.domain["input_columns"]:
            if column not in base and column not in {"input_i_peak_a", "input_beta_dq_deg"}:
                lower, upper = self.domain["observed_bounds"][column]
                if lower != upper:
                    raise ValueError(f"no declared constant for model input: {column}")
                base[column] = lower
        rows = [{**base, "input_i_peak_a": float(current), "input_beta_dq_deg": float(beta)}
                for current, beta in controls]
        return pd.DataFrame(rows)

    def predict_controls(self, design: Mapping, point: OperatingPoint,
                         controls: Sequence[tuple[float, float]]) -> PredictionBatch:
        self._check_prediction_evidence()
        if _digest(self.bundle.metadata) != self._metadata_sha256:
            raise ValueError("bundle metadata changed after optimization context binding")
        frame = self.build_inputs(design, point, controls)
        if not len(frame):
            empty = pd.DataFrame(columns=ALL_TARGETS)
            self._check_prediction_evidence()
            return PredictionBatch(frame, empty, empty.copy(), empty.copy(), {}, 0)
        columns = self.domain["input_columns"]
        supported = np.isfinite(frame[columns].to_numpy(float)).all(axis=1)
        reasons: dict[str, int] = {}
        for category, bounds in (("declared", self.domain["declaration"]["bounds"]),
                                 ("training", self.domain["observed_bounds"])):
            for column, (lower, upper) in bounds.items():
                tolerance = max(abs(lower), abs(upper), 1.) * 1e-10 if category == "training" else 1e-10
                valid = frame[column].between(lower - tolerance, upper + tolerance).to_numpy()
                if not valid.all():
                    reasons[category + ":" + column] = int((~valid).sum())
                supported &= valid
        valid_speed = frame.input_base_rpm.isin(self.domain["declaration"]["speeds_rpm"]).to_numpy()
        supported &= valid_speed
        if not valid_speed.all():
            reasons["unsupported_discrete_speed"] = int((~valid_speed).sum())
        selected = frame.loc[supported]
        empty = pd.DataFrame(columns=ALL_TARGETS, index=selected.index)
        if not len(selected):
            self._check_prediction_evidence()
            return PredictionBatch(selected, empty, empty.copy(), empty.copy(), reasons, len(frame))
        validate_inputs(selected, self.domain)
        predictions = self.bundle.predict(selected, fingerprints=self.fingerprints, with_bounds=True)
        for output in predictions:
            if (not output.index.equals(selected.index) or set(ALL_TARGETS) - set(output) or
                    not np.isfinite(output[list(ALL_TARGETS)].to_numpy(float)).all()):
                raise ValueError("bundle returned invalid or misaligned prediction intervals")
        center, lower, upper = (output[list(ALL_TARGETS)] for output in predictions)
        if (physics_issues(center) or (lower > center + 1e-9).any().any() or
                (upper < center - 1e-9).any().any()):
            raise ValueError("bundle returned inconsistent physical prediction intervals")
        self._check_prediction_evidence()
        return PredictionBatch(selected, center, lower, upper, reasons, len(frame))


@dataclass
class ControlSearchResult:
    operating_point: dict
    status: str
    selected: dict | None
    support: dict


def _axis(lower: float, upper: float, step: float) -> np.ndarray:
    count = int(math.ceil((upper - lower) / step))
    if count > 128:
        raise ValueError("control refinement axis exceeds bounded129-point search")
    return np.unique(np.r_[np.arange(lower, upper, step), upper])


def search_controls(adapter: PhysicalBundleAdapter, design: Mapping, point: OperatingPoint,
                    *, refinement_centers: int = 3) -> ControlSearchResult:
    """One coarse batch and at most two bounded local batches per speed."""
    if isinstance(refinement_centers, bool) or not isinstance(refinement_centers, int) or not 1 <= refinement_centers <= 8:
        raise ValueError("refinement_centers must be between1 and8")
    spec = adapter.spec
    if point not in spec.operating_points:
        raise ValueError("control target differs from the bound operating specification")
    if len(spec.control.beta_refinement_steps_deg) != len(spec.control.current_refinement_denominators):
        raise ValueError("control refinement beta/current levels must have equal lengths")
    low_i, high_i = adapter.domain["declaration"]["bounds"]["input_i_peak_a"]
    low_b, high_b = spec.beta_bounds_deg
    if spec.control.current_grid_points > 129 or len(spec.control.beta_refinement_steps_deg) > 2:
        raise ValueError("control configuration exceeds bounded proposal search")
    currents = np.linspace(low_i, high_i, spec.control.current_grid_points)
    betas = _axis(low_b, high_b, spec.control.coarse_beta_step_deg)
    pending = [(float(i), float(b)) for i in currents for b in betas]
    seen: set[tuple[float, float]] = set()
    records: list[dict] = []
    requested = supported = calls = 0
    reasons: Counter = Counter()
    old_i_step = (high_i - low_i) / (len(currents) - 1)
    old_b_step = spec.control.coarse_beta_step_deg
    refinements = list(zip(spec.control.beta_refinement_steps_deg, spec.control.current_refinement_denominators))
    for level in range(len(refinements) + 1):
        unique = []
        for current, beta in pending:
            key = (round(current, 12), round(beta, 12))
            if key not in seen:
                seen.add(key); unique.append((current, beta))
        if unique:
            batch = adapter.predict_controls(design, point, unique)
            requested += batch.requested; supported += len(batch.inputs)
            calls += int(len(batch.inputs) > 0)
            reasons.update(batch.unsupported)
            for index, inputs in batch.inputs.iterrows():
                current, beta = float(inputs.input_i_peak_a), float(inputs.input_beta_dq_deg)
                torque_margin = float(batch.lower.loc[index, TORQUE]) - point.required_torque_nm
                voltage_margin = spec.inverter.voltage_utilization * spec.inverter.vdc_v - float(batch.upper.loc[index, VOLTAGE_SPAN])
                density = current_density_a_per_mm2(current, spec.winding)
                margins = {"torque_lcb_nm": torque_margin, "voltage_span_ucb_v": voltage_margin,
                           "current_peak_a": spec.current_limit_a - current,
                           "current_density_a_per_mm2": spec.constraints.current_density_limit_a_per_mm2 - density}
                violation = (max(0., -torque_margin) / point.required_torque_nm +
                             max(0., -voltage_margin) / (spec.inverter.voltage_utilization * spec.inverter.vdc_v) +
                             max(0., -margins["current_peak_a"]) / spec.current_limit_a +
                             max(0., -margins["current_density_a_per_mm2"]) / spec.constraints.current_density_limit_a_per_mm2)
                id_a, iq_a = dq_currents(current, beta)
                records.append({"current_peak_a": current, "beta_dq_deg": beta, "id_peak_a": id_a, "iq_peak_a": iq_a,
                                "current_density_a_per_mm2": density,
                                "phase_resistance_100c_ohm": float(inputs.input_phase_resistance_ohm),
                                "copper_loss_w": 1.5 * float(inputs.input_phase_resistance_ohm) * current**2,
                                "constraint_margins": margins, "normalized_violation": violation,
                                "feasible": all(value >= -1e-10 for value in margins.values()),
                                "metrics": {target: {"mean": float(batch.point.loc[index, target]),
                                                      "lower": float(batch.lower.loc[index, target]),
                                                      "upper": float(batch.upper.loc[index, target])} for target in ALL_TARGETS}})
        if level == len(refinements) or not records:
            break
        anchors = sorted(records, key=_control_key)[:refinement_centers]
        b_step, denominator = refinements[level]
        i_step = high_i / denominator
        pending = []
        for anchor in anchors:
            i, b = anchor["current_peak_a"], anchor["beta_dq_deg"]
            local_i = _axis(max(low_i, i - old_i_step), min(high_i, i + old_i_step), i_step)
            local_b = _axis(max(low_b, b - old_b_step), min(high_b, b + old_b_step), b_step)
            pending.extend((float(i), float(b)) for i in local_i for b in local_b)
        old_i_step, old_b_step = i_step, b_step
    best = min(records, key=_control_key) if records else None
    status = "feasible" if best and best["feasible"] else ("no_feasible_supported_control" if best else "unsupported")
    return ControlSearchResult(
        operating_point={**asdict(point), "required_torque_nm": point.required_torque_nm,
                         "required_power_w": point.required_power_w,
                         "voltage_span_limit_v": spec.inverter.voltage_utilization * spec.inverter.vdc_v},
        status=status, selected=best,
        support={"requested_controls": requested, "supported_controls": supported,
                 "unsupported_controls": requested - supported, "model_batch_calls": calls,
                 "domain_reasons": dict(sorted(reasons.items())),
                 "interpretation": "unsupported predictions and finite search misses do not prove physical infeasibility"})


def _control_key(row):
    return (not row["feasible"], row["normalized_violation"], row["metrics"][LOSS]["upper"],
            -row["metrics"][TORQUE]["lower"], row["current_peak_a"], row["beta_dq_deg"])


@dataclass
class DesignProposal:
    design_hash: str
    geometry: dict
    status: str
    active_volume_m3: float
    slot_fill_ratio: float | None
    phase_resistance_100c_ohm: float | None
    controls: list[ControlSearchResult] = field(default_factory=list)
    duty_weighted_loss_w: dict | None = None
    cycle_efficiency_pct: dict | None = None
    issues: list[str] = field(default_factory=list)
    final_design_status: str = PROPOSAL_STATUS

    @property
    def feasible(self):
        return self.status == "predicted_feasible"

    @property
    def objectives(self):
        return (self.active_volume_m3, self.duty_weighted_loss_w["upper"]) if self.feasible else None


def evaluate_design(adapter: PhysicalBundleAdapter, design: Mapping) -> DesignProposal:
    values = adapter.normalize_design(design)
    volume = active_volume_m3(values["stator_outer_radius"], values["stack_length_mm"])
    result = DesignProposal(physical_geometry_hash(values), values, "geometry_constraint_violation", volume, None, None)
    try:
        metrics = geometry_metrics(values, values["stack_length_mm"], adapter.spec.winding)
    except ValueError as exc:
        result.issues.append(str(exc)); return result
    result.slot_fill_ratio = metrics.slot_fill_ratio
    if metrics.slot_fill_ratio > adapter.spec.winding.fill_factor + 1e-9:
        result.issues.append("approximate slot fill exceeds declared winding limit"); return result
    result.phase_resistance_100c_ohm = phase_resistance_100c_ohm(values, values["stack_length_mm"], adapter.spec.winding)
    result.controls = [search_controls(adapter, values, point) for point in adapter.spec.operating_points]
    if not all(control.status == "feasible" for control in result.controls):
        result.status = "unsupported" if any(control.status == "unsupported" for control in result.controls) else "no_feasible_supported_control"
        return result
    result.status = "predicted_feasible"
    result.duty_weighted_loss_w = {
        bound: sum(control.operating_point["duty_weight"] * control.selected["metrics"][LOSS][bound]
                   for control in result.controls) for bound in ("mean", "lower", "upper")}
    power = {bound: sum(control.operating_point["duty_weight"] * control.selected["metrics"][TORQUE][bound] *
                        control.operating_point["speed_rpm"] * 2 * math.pi / 60 for control in result.controls)
             for bound in ("mean", "lower", "upper")}
    loss = result.duty_weighted_loss_w
    result.cycle_efficiency_pct = {"mean": 100 * power["mean"] / (power["mean"] + loss["mean"]),
                                   "lower": 100 * power["lower"] / (power["lower"] + loss["upper"]),
                                   "upper": 100 * power["upper"] / (power["upper"] + loss["lower"])}
    return result


def pareto_front(proposals: Sequence[DesignProposal]) -> list[DesignProposal]:
    rows = [proposal for proposal in proposals if proposal.feasible]
    return sorted([row for row in rows if not any(
        all(a <= b for a, b in zip(other.objectives, row.objectives)) and
        any(a < b for a, b in zip(other.objectives, row.objectives)) for other in rows)], key=lambda row: row.objectives)


def generate_design_plan(adapter: PhysicalBundleAdapter, baseline: Mapping, *, count: int = 32,
                         seed: int = 42, mode: str = "full_geometry",
                         variables: Sequence[str] | None = None) -> list[dict]:
    """Baseline plus feasible full-geometry DOE, or explicit local-subspace LHS."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 2048:
        raise ValueError("proposal count must be between1 and2048")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if mode not in {"full_geometry", "local_subspace"}:
        raise ValueError("mode must be full_geometry or local_subspace")
    base = adapter.normalize_design(baseline)
    if mode == "full_geometry":
        if variables is not None:
            raise ValueError("variables require the explicit local_subspace mode")
        sampled = sample_feasible_geometries(adapter.spec, count - 1, seed,
            excluded_hashes={physical_geometry_hash(base)}) if count > 1 else []
        return [base, *[{**geometry, "stack_length_mm": depth} for geometry, depth, _ in sampled]]
    variables = ("stator_outer_radius", "stack_length_mm") if variables is None else variables
    if not variables or len(set(variables)) != len(variables) or set(variables) - set(adapter.spec.design_variable_names) or "stator_shoe_thick" in variables:
        raise ValueError("search variables must be unique active independent design variables")
    declared = {bound.name: (bound.lower, bound.upper) for bound in adapter.spec.design_space}
    intervals = []
    for name in variables:
        lo, hi = declared[name]
        observed = adapter.domain["observed_bounds"]["input_" + name]
        lo, hi = max(lo, observed[0]), min(hi, observed[1])
        if lo >= hi:
            raise ValueError(f"no supported search span for variable: {name}")
        intervals.append((lo, hi))
    plan = [base]
    seen = {physical_geometry_hash(base)}
    sampler = qmc.LatinHypercube(len(variables), seed=seed)
    attempts = 0
    while len(plan) < count and attempts < 100 * count:
        samples = qmc.scale(sampler.random(64), [pair[0] for pair in intervals], [pair[1] for pair in intervals])
        for sample in samples:
            attempts += 1
            values = adapter.normalize_design({**base, **dict(zip(variables, sample))})
            key = physical_geometry_hash(values)
            if key not in seen:
                seen.add(key); plan.append(values)
                if len(plan) == count:
                    break
    if len(plan) != count:
        raise ValueError("cannot produce the requested number of unique designs")
    return plan


def build_fea_validation_rows(adapter: PhysicalBundleAdapter, proposals: Sequence[DesignProposal],
                              selected_profile: Path) -> tuple[list[dict], dict]:
    with adapter._checked_search(join=True):
        return _build_fea_validation_rows_checked(adapter, proposals, selected_profile)


def _build_fea_validation_rows_checked(adapter: PhysicalBundleAdapter, proposals: Sequence[DesignProposal],
                                      selected_profile: Path) -> tuple[list[dict], dict]:
    """Replay a sealed approved profile; never guess missing artifact settings.

    Only supported predicted-feasible proposals receive runnable rows. These
    are independent FEA requests, not certified designs or submitted tasks.
    """
    if not adapter.bundle.approved or _digest(adapter.bundle.metadata) != adapter._metadata_sha256:
        raise ValueError("validation plan requires the unchanged approved model")
    from . import winding_frames as frames
    from .execution_compatibility import ProductionFrameEvidence
    if isinstance(adapter.frame_evidence, ProductionFrameEvidence):
        profile = doe.load_selected_profile(selected_profile, execution_backend="public_owned_cli_v1")
    elif type(adapter.frame_evidence) is frames.ProductionWindingEvidence:
        profile = doe.load_selected_profile(selected_profile, execution_backend="public_owned_sdk_v1")
    else:
        profile = doe.load_selected_profile(selected_profile)
    if frames.is_family(adapter.calibration_manifest):
        frames.validate_reference(adapter.calibration_manifest, adapter.frame_evidence)
        frames.profile_context(profile, adapter.calibration_manifest)
    fields = fixed_case_context_fields(adapter.spec.material_context, adapter.spec.winding)
    material = {key: fields[key] for key in ("core_material_model", "core_material")}
    winding = {key: fields[key] for key in doe.WINDING_CONTEXT_FIELDS}
    if profile.producer_fingerprint != adapter.fingerprints["input_producer_fingerprint"]:
        raise ValueError("validation profile producer differs from the approved model")
    if profile.material_fingerprint != adapter.fingerprints["input_material_fingerprint"]:
        raise ValueError("validation profile material fingerprint differs from the approved model")
    if profile.material_context != material or profile.winding_context != winding:
        raise ValueError("validation profile material/winding context differs from the source spec")
    settings = doe.profile_settings(profile.settings)
    if settings["quality_profile"] != adapter.fingerprints["input_quality_profile"]:
        raise ValueError("validation profile quality differs from the approved model")
    for key in material:
        if settings.get(key, fields[key]) != fields[key]:
            raise ValueError(f"validation profile has a conflicting material setting: {key}")
    from calibrate_ipmsm_beta import _require_zero_context
    _require_zero_context(fields, adapter.calibration_manifest)
    if material["core_material_model"] != "legacy_pnf_hybrid_v1" and adapter.calibration_manifest.get("producer_fingerprint") != profile.producer_fingerprint:
        raise ValueError("validation profile producer differs from reference zero calibration")
    if adapter.calibration_manifest.get("material_fingerprint") not in (None, profile.material_fingerprint):
        raise ValueError("validation profile material differs from zero calibration")
    initial = float(adapter.calibration_manifest.get("initial_position_deg", float("nan")))
    if not math.isfinite(initial) or settings.get("initial_position_deg", initial) != initial:
        raise ValueError("validation profile must replay the zero calibration initial position")
    replay = {key: value for key, value in settings.items()
              if key not in {"mesh_elements", "mesh_maximum_lengths_mm", "mesh_refinement_modes"}}
    replay.update({f"mesh_{group}_elements": value for group, value in settings["mesh_elements"].items()})
    replay.update({f"mesh_{group}_max_length_mm": value for group, value in settings["mesh_maximum_lengths_mm"].items()})
    replay.update({f"mesh_{group}_mode": value for group, value in settings.get("mesh_refinement_modes", {}).items()})
    replay["initial_position_deg"] = initial
    # Retain analysis_purpose=training and every numerical flag: changing these
    # at proposal time would no longer replay the approved setup fingerprint.
    from run_ipmsm_batch import build_spec, setup_fingerprint
    context_key = _digest({"fingerprints": adapter.fingerprints, "profile": profile.manifest_sha256})[:12]
    rows: list[dict] = []
    for proposal in proposals:
        if not proposal.feasible:
            continue
        design = adapter.normalize_design(proposal.geometry)
        if physical_geometry_hash(design) != proposal.design_hash or len(proposal.controls) != len(adapter.spec.operating_points):
            raise ValueError("validation proposal geometry/control identity changed")
        for index, (control, point) in enumerate(zip(proposal.controls, adapter.spec.operating_points)):
            if (control.status != "feasible" or not control.selected or not control.selected["feasible"] or
                    any(control.operating_point.get(key) != value for key, value in asdict(point).items())):
                raise ValueError("validation plan requires every selected control to be supported and feasible")
            current, beta = control.selected["current_peak_a"], control.selected["beta_dq_deg"]
            inputs = adapter.build_inputs(design, point, [(current, beta)])
            validate_inputs(inputs, adapter.domain)
            row = {
                "case_id": f"physical_validation_{context_key}_{proposal.design_hash[:20]}_p{index}",
                "geometry_group_id": "v3geo_" + proposal.design_hash,
                "design_hash": proposal.design_hash, "doe_split": "validation", "repeat_of_case_id": "",
                "plan_purpose": "independent_design_validation", "dataset_schema_version": "ipmsm_v2",
                "operating_point_id": point.name, "slot_num": adapter.spec.slot_number,
                "pole_num": adapter.spec.pole_number, "geometry_mode": "fixed", **design,
                "phase_resistance_ohm": float(inputs.iloc[0].input_phase_resistance_ohm),
                "vdc_v": adapter.spec.inverter.vdc_v, "base_rpm": point.speed_rpm,
                "i_peak_a": current, "beta_dq_deg": beta, "beta_deg": beta,
                "beta_convention": adapter.spec.beta_calibration.convention,
                "beta_calibration_id": adapter.spec.beta_calibration.calibration_id,
                "electrical_zero_deg": adapter.spec.beta_calibration.electrical_zero_deg,
                **frames.frame_fields(adapter.calibration_manifest),
                "model_extent": "full_360", "symmetry_factor": 1, "use_periodic_boundary": False,
                "operation": "sin_current", **replay, **fields,
                "expected_material_fingerprint": adapter.fingerprints["input_material_fingerprint"],
                "expected_setup_fingerprint": adapter.fingerprints["input_setup_fingerprint"],
            }
            applied = build_spec(row)
            for key, value in settings.items():
                if key != "quality_profile" and getattr(applied, key, None) != value:
                    raise ValueError(f"runner does not replay selected profile setting {key!r}")
            if any(getattr(applied, key, None) != value for key, value in fields.items()):
                raise ValueError("runner does not replay validation material/winding context")
            if setup_fingerprint(applied, row["quality_profile"], "sin_current", False) != adapter.fingerprints["input_setup_fingerprint"]:
                raise ValueError("validation profile setup differs from the approved model")
            rows.append(row)
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate validation cases")
    return rows, {"status": "ready_for_independent_fea", "submitted": False,
                  **({"reference_frame_context": frames.semantic_context(adapter.calibration_manifest)}
                     if frames.is_family(adapter.calibration_manifest) else {}),
                  "selected_profile_sha256": profile.manifest_sha256,
                  "selected_profile_evidence": list(profile.evidence), "settings": settings,
                  "material_context": material, "winding_context": winding,
                  "expected_material_fingerprint": profile.material_fingerprint}


def search_designs(adapter: PhysicalBundleAdapter, designs: Sequence[Mapping], *, seed: int | None = None,
                   search_variables: Sequence[str] | None = None, search_mode: str = "explicit_plan",
                   max_design_evaluations: int = 256, selected_profile: Path | None = None) -> dict:
    # A report/FEA-row value cannot leave this boundary until the closing full
    # source/evidence scan succeeds, including exceptional/nested calls.
    with adapter._checked_search():
        return _search_designs_checked(adapter, designs, seed=seed, search_variables=search_variables,
            search_mode=search_mode, max_design_evaluations=max_design_evaluations,
            selected_profile=selected_profile)


def _search_designs_checked(adapter: PhysicalBundleAdapter, designs: Sequence[Mapping], *, seed: int | None = None,
                   search_variables: Sequence[str] | None = None, search_mode: str = "explicit_plan",
                   max_design_evaluations: int = 256, selected_profile: Path | None = None) -> dict:
    if not designs:
        raise ValueError("an explicit nonempty design plan is required")
    if (isinstance(max_design_evaluations, bool) or not isinstance(max_design_evaluations, int) or
            not 1 <= max_design_evaluations <= 2048 or len(designs) > max_design_evaluations):
        raise ValueError("design plan exceeds the explicit evaluation budget")
    normalized = [adapter.normalize_design(design) for design in designs]
    identities = [physical_geometry_hash(design) for design in normalized]
    if len(set(identities)) != len(identities):
        raise ValueError("design plan contains duplicate physical geometries")
    if any(sha256(path) != _IMPORTED_OPTIMIZER_SOURCES[name] for name, path in _OPTIMIZER_SOURCE_PATHS.items()):
        raise ValueError("optimizer source differs from the imported code; use a fresh matching process")
    proposals = [evaluate_design(adapter, design) for design in normalized]
    if any(sha256(path) != _IMPORTED_OPTIMIZER_SOURCES[name] for name, path in _OPTIMIZER_SOURCE_PATHS.items()):
        raise ValueError("optimizer source changed during search; refusing proposal publication")
    report = {"schema_version": "ipmsm_physical_proposals_v1", "status": PROPOSAL_STATUS,
              "search": {"method": "explicit_plan_with_batched_control_grid_and_local_refinement", "seed": seed,
                         "mode": search_mode,
                         "budget": {"maximum_design_evaluations": max_design_evaluations,
                                    "planned_design_evaluations": len(normalized),
                                    "maximum_model_batches": 6 * len(normalized)},
                         "variables": list(search_variables) if search_variables is not None else None,
                         "design_plan_sha256": _digest(normalized), "design_plan": normalized,
                         "global_optimality_claim": False, "held_out_labels_read": False, "fea_submitted": False},
              "context": {"expected_fingerprints": adapter.fingerprints, "source_spec_sha256": _digest(adapter.source_spec),
                          "calibration_manifest_sha256": _digest(adapter.calibration_manifest), "domain": adapter.domain,
                          "model_dataset_sha256": adapter.bundle.metadata.get("dataset_sha256"),
                          "model_prediction_sources": adapter.bundle.metadata.get("prediction_sources"),
                          "artifact": adapter.artifact_provenance, "optimizer_source_sha256": _IMPORTED_OPTIMIZER_SHA256,
                          "optimizer_sources": _IMPORTED_OPTIMIZER_SOURCES},
              "objective_names": ["active_envelope_volume_m3", "duty_weighted_total_loss_upper_w"],
              "counts": dict(Counter(proposal.status for proposal in proposals)),
              "pareto_design_hashes": [proposal.design_hash for proposal in pareto_front(proposals)],
              "proposals": [asdict(proposal) for proposal in proposals],
              "limitations": ["Finite control search can miss feasible controls or a lower-loss control.",
                              "The support box does not establish dense joint geometry/control coverage.",
                              "Joint geometry-calibrated intervals do not certify a newly optimized design by FEA.",
                              "Manufacturing, thermal, demagnetization and mechanical integrity need separate assessment."]}
    if selected_profile is None:
        report["fea_validation_rows"] = []
        report["fea_validation_plan"] = {"status": "proposal_only_profile_required", "submitted": False}
    else:
        rows, plan = build_fea_validation_rows(adapter, proposals, selected_profile)
        report["fea_validation_rows"], report["fea_validation_plan"] = rows, plan
    report["report_content_sha256"] = _digest(report)
    return report


def write_search_report(report: Mapping, destination: Path) -> None:
    destination = Path(os.path.abspath(destination))
    if os.name == "nt" and destination.drive.casefold() != "c:":
        raise ValueError("generated optimization artifacts must be written to the C runtime drive")
    unhashed = dict(report); checksum = unhashed.pop("report_content_sha256", None)
    if checksum != _digest(unhashed):
        raise ValueError("optimization report content hash mismatch")
    payload = canonical_json(report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(payload)
    if destination.read_bytes() != payload:
        raise RuntimeError("optimization report publication verification failed")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--expected-fingerprints", type=Path, required=True)
    parser.add_argument("--calibration-manifest", type=Path, required=True)
    from . import winding_frames as frames
    frames.evidence_arguments(parser)
    parser.add_argument("--baseline", type=Path, required=True, help="JSON independent geometry plus stack_length_mm")
    parser.add_argument("--selected-profile", type=Path, help="Sealed approved profile required for runnable FEA validation rows")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=32)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mode", choices=["full_geometry", "local_subspace"], default="full_geometry")
    parser.add_argument("--variables", nargs="+", help="active variables for explicit local_subspace mode")
    parser.add_argument("--max-design-evaluations", type=int, default=256)
    args = parser.parse_args(argv)
    read = lambda path: json.loads(path.read_text(encoding="utf-8-sig"))
    adapter = PhysicalBundleAdapter.from_directory(args.bundle, read(args.spec), read(args.expected_fingerprints), read(args.calibration_manifest),
                                                   frame_evidence=frames.evidence_from_arguments(args))
    plan = generate_design_plan(adapter, read(args.baseline), count=args.count, seed=args.seed, mode=args.mode, variables=args.variables)
    variables = [*ACTIVE_GEOMETRY, "stack_length_mm"] if args.mode == "full_geometry" else (args.variables or ["stator_outer_radius", "stack_length_mm"])
    report = search_designs(adapter, plan, seed=args.seed, search_variables=variables,
                           search_mode=args.mode, max_design_evaluations=args.max_design_evaluations,
                           selected_profile=args.selected_profile)
    write_search_report(report, args.output)
    print(json.dumps({"output": str(args.output), "counts": report["counts"], "pareto_proposals": len(report["pareto_design_hashes"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
