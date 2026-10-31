"""Deterministic, geometry-disjoint loaded-motor plans; never submit FEA.

The sampling helper is independent of calibration and the retired campaign
controllers. Production planning additionally requires physical-zero and
explicit profile evidence. Hashing evidence does not certify convergence.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

import numpy as np
import scipy
from scipy.stats import qmc

import ipmsm_optimization as _geometry_helper
from ipmsm_optimization import (
    GEOMETRY_VARIABLE_NAMES, MaterialContextSpec, OptimizationSpec, WindingSpec, current_density_a_per_mm2,
    geometry_metrics, optimization_spec_from_mapping, phase_resistance_100c_ohm,
)

PLAN_SCHEMA = "ipmsm_v3_training_plan_v2"
PROFILE_SCHEMA = "ipmsm_v3_selected_profile_v1"
METRIC_CONTRACT = "motor_metrics_v3"
ACTIVE_GEOMETRY = tuple(name for name in GEOMETRY_VARIABLE_NAMES if name != "stator_shoe_thick")
MESH_GROUPS = ("magnet", "rotor", "stator", "winding", "band")
WINDING_CONTEXT_FIELDS = ("series_turns_per_phase", "turns_per_coil_side",
                          "conductor_area_per_branch_mm2", "slot_fill_limit")
TRAINING_ATTEMPT_BUDGET = 2400
DEFAULT_RETRY_RESERVE = 240
PAIRED_CONTROL_PROTOCOL = "paired_two_controls_two_speeds_v1"
CANONICAL_FLAGS = {"circle_segments": 0, "loss_policy": "laminated_core", "strict_setup": True,
                   "require_steady_state": True, "compute_inductance": True, "analysis_purpose": "training"}
_GENERATOR_NAME = "src/pyaedt_motor/doe.py"
_HELPER_NAME = "ipmsm_optimization.py"
_SOURCE_PATHS = {_GENERATOR_NAME: Path(__file__).resolve(),
                 _HELPER_NAME: Path(__file__).resolve().parents[2] / _HELPER_NAME}
_GENERATOR_MODULE = sys.modules[__name__]
_HELPER_BINDINGS = {name: globals()[name] for name in (
    "GEOMETRY_VARIABLE_NAMES", "MaterialContextSpec", "OptimizationSpec", "WindingSpec", "current_density_a_per_mm2",
    "geometry_metrics", "optimization_spec_from_mapping", "phase_resistance_100c_ohm")}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _capture_source_pin() -> dict[str, dict[str, str]]:
    origins = {}
    for name, module in ((_GENERATOR_NAME, _GENERATOR_MODULE), (_HELPER_NAME, _geometry_helper)):
        expected = _SOURCE_PATHS[name].resolve()
        origin = getattr(module, "__file__", None)
        spec_origin = getattr(getattr(module, "__spec__", None), "origin", origin)
        if (sys.modules.get(module.__name__) is not module or not origin or not spec_origin
                or Path(origin).resolve() != expected or Path(spec_origin).resolve() != expected):
            raise RuntimeError(f"planning import origin differs from project source: {name}")
        origins[name] = str(expected)
    if any(globals()[name] is not value or getattr(_geometry_helper, name) is not value
           for name, value in _HELPER_BINDINGS.items()):
        raise RuntimeError("planning helper bindings changed after import; start a fresh process")
    payloads = {name: path.read_bytes() for name, path in _SOURCE_PATHS.items()}
    hashes = {name: _sha(payload) for name, payload in payloads.items()}
    if getattr(_geometry_helper, "_IMPORTED_SOURCE_SHA256", None) != hashes[_HELPER_NAME]:
        raise RuntimeError("geometry helper source changed after import; start a fresh process")
    return {"source_sha256": hashes, "source_import_origins": origins}


def _assert_source_pin(expected: Mapping | None = None) -> dict[str, dict[str, str]]:
    current = _capture_source_pin()
    if current != _IMPORTED_SOURCE_PIN or (expected is not None and current != expected):
        raise RuntimeError("planning source changed after import; start a fresh process")
    return current


def _require_manifest_sources(manifest: Mapping[str, Any], pin: Mapping[str, Any]) -> None:
    hashes = pin["source_sha256"]
    if (manifest.get("schema_version") != PLAN_SCHEMA or manifest.get("source_sha256") != hashes
            or manifest.get("generator_sha256") != hashes[_GENERATOR_NAME]
            or manifest.get("geometry_helper_sha256") != hashes[_HELPER_NAME]):
        raise ValueError("training plan generator/helper source SHA256/schema mismatch")
    origins = manifest.get("source_import_origins")
    if not isinstance(origins, dict) or set(origins) != set(hashes) or any(
            not isinstance(origin, str) or not Path(origin).is_absolute()
            or Path(origin).name != Path(name).name for name, origin in origins.items()):
        raise ValueError("training plan lacks actual generation import origins")
    # Generation origins are historical provenance. Identical source bytes may
    # be verified from another correctly laid-out C-native frozen checkout.


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _field(row: Mapping[str, Any], name: str) -> Any:
    for key in (name, "input_" + name):
        if key in row and str(row[key]).strip():
            return row[key]
    raise ValueError(f"complete physical geometry requires {name}")


def physical_geometry_hash(row: Mapping[str, Any], stack_length_mm: float | None = None) -> str:
    """Identity excludes inactive shoe height and derived, control and legacy ID fields.

    Active geometry values are canonicalized to12 decimal places. Model depth
    is canonicalized to the6 significant digits currently applied by the FEA
    setup. This conservatively treats numerically indistinguishable inputs as
    the same geometry, including older result CSVs with input_ column names.
    """
    geometry = {name: round(_finite(_field(row, name), name), 12) for name in ACTIVE_GEOMETRY}
    depth = _field(row, "stack_length_mm") if stack_length_mm is None else stack_length_mm
    geometry["stack_length_mm"] = float(f"{_finite(depth, 'stack_length_mm'):.6g}")
    return _sha(_json_bytes({"identity_schema": "active_geometry_v3", "pole_number": 8,
                             "slot_number": 12, "geometry": geometry}))


def sample_feasible_geometries(
    spec: OptimizationSpec, count: int, seed: int, *, excluded_hashes: Iterable[str] = (),
    max_candidates: int | None = None,
) -> list[tuple[dict[str, float], float, str]]:
    """Return (15-input geometry, stack_mm, physical_SHA), without calibration imports.

    Candidate batches have fixed size so the sequence does not depend on the
    requested count. Constraints alter the final Latin-hypercube stratification.
    Only positive derived geometry, specified slot fill and prior identities
    exclude candidates; supplied bounds remain generic for calibration callers.
    """
    source_pin = _assert_source_pin()
    count = _integer(count, "count", 1)
    seed = _integer(seed, "seed")
    limit = max(count * 100, 4096) if max_candidates is None else _integer(max_candidates, "max_candidates", count)
    bounds = {bound.name: bound for bound in spec.geometry_design_space}
    shoe = bounds["stator_shoe_thick"]
    if not shoe.lower <= 1.5 <= shoe.upper:
        raise ValueError("inactive stator_shoe_thick must permit the frozen value1.5")
    names = (*ACTIVE_GEOMETRY, "stack_length_mm")
    active_bounds = [bounds[name] for name in ACTIVE_GEOMETRY] + [spec.stack_length_bounds]
    sampler = qmc.LatinHypercube(len(names), seed=seed)
    excluded = set(excluded_hashes)
    for value in excluded:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("excluded geometry hash must be a canonical SHA256")
    seen = set(excluded)
    selected = []
    rejected: Counter[str] = Counter()
    scanned = 0
    while len(selected) < count and scanned < limit:
        size = min(128, limit - scanned)
        unit = sampler.random(size)
        samples = qmc.scale(unit, [bound.lower for bound in active_bounds], [bound.upper for bound in active_bounds])
        for values in samples:
            scanned += 1
            candidate = {name: float(value) for name, value in zip(names, values)}
            depth = float(f"{candidate.pop('stack_length_mm'):.6g}")
            if not spec.stack_length_bounds.lower <= depth <= spec.stack_length_bounds.upper:
                rejected["depth_precision_outside_bounds"] += 1
                continue
            candidate["stator_shoe_thick"] = 1.5
            try:
                metrics = geometry_metrics(candidate, depth, spec.winding, slot_number=spec.slot_number)
            except ValueError:
                rejected["nonpositive_derived_geometry"] += 1
                continue
            if metrics.slot_fill_ratio > spec.winding.fill_factor:
                rejected["slot_fill"] += 1
                continue
            key = physical_geometry_hash(candidate, depth)
            if key in seen:
                rejected["prior_or_duplicate_geometry"] += 1
                continue
            selected.append((candidate, depth, key))
            seen.add(key)
            if len(selected) == count:
                break
    if len(selected) != count:
        raise ValueError(f"feasible geometry sampling exhausted after {scanned} candidates; "
                         f"found {len(selected)}/{count}; rejections={dict(rejected)}")
    _assert_source_pin(source_pin)
    return selected


def exclusion_hashes(paths: Iterable[Path]) -> tuple[set[str], list[dict[str, Any]]]:
    excluded: set[str] = set()
    sources = []
    for path in paths:
        path = Path(path)
        payload = path.read_bytes()
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        count = 0
        for number, row in enumerate(reader, 2):
            try:
                excluded.add(physical_geometry_hash(row))
            except ValueError as exc:
                raise ValueError(f"cannot establish prior geometry identity: {path.name}:{number}: {exc}") from exc
            count += 1
        if not count:
            raise ValueError(f"excluded prior CSV contains no cases: {path}")
        sources.append({"path": str(path.resolve()), "sha256": _sha(payload), "rows": count})
    return excluded, sources


def _strict_json(payload: bytes) -> dict:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    value = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=object_pairs,
                       parse_constant=lambda text: (_ for _ in ()).throw(ValueError(f"nonfinite JSON value: {text}")))
    if not isinstance(value, dict):
        raise ValueError("manifest/spec must contain one JSON object")
    return value


def profile_settings(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate explicit numerical settings; never infer numerical convergence."""
    result = dict(raw)
    allowed = {"quality_profile", "transient_periods", "steps_per_period", "mesh_elements",
               "mesh_maximum_lengths_mm", "mesh_refinement_modes", "core_material_model", "core_material", "magnet_material", "shaft_material",
               "winding_material", "air_material", "initial_position_deg", "setup_name",
               "band_circle_segments", "solver_output_error", "solver_nonlinear_residual", *CANONICAL_FLAGS}
    if set(result) - allowed:
        raise ValueError(f"unsupported profile settings: {sorted(set(result) - allowed)}")
    if "solver_output_error" in result and type(result["solver_output_error"]) is not bool:
        raise ValueError("solver_output_error must be a boolean")
    result.setdefault("solver_output_error", True)
    residual = result.get("solver_nonlinear_residual", 0.005)
    if type(residual) not in (int, float):
        raise ValueError("solver_nonlinear_residual must be finite and positive")
    try:
        residual = float(residual)
    except OverflowError as exc:
        raise ValueError("solver_nonlinear_residual must be finite and positive") from exc
    if not math.isfinite(residual) or residual <= 0:
        raise ValueError("solver_nonlinear_residual must be finite and positive")
    result["solver_nonlinear_residual"] = residual
    name = result.get("quality_profile")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
        raise ValueError("quality_profile must be an explicit safe identifier")
    _integer(result.get("transient_periods"), "transient_periods", 2)
    _integer(result.get("steps_per_period"), "steps_per_period", 16)
    for key, expected in CANONICAL_FLAGS.items():
        if key in result and (type(result[key]) is not type(expected) or result[key] != expected):
            raise ValueError(f"training profile requires {key}={expected!r}")
        result[key] = expected
    counts = result.get("mesh_elements")
    if not isinstance(counts, dict) or set(counts) != set(MESH_GROUPS):
        raise ValueError("mesh_elements must explicitly contain the five motor mesh groups")
    result["mesh_elements"] = {key: _integer(counts[key], f"mesh_elements.{key}", 1) for key in MESH_GROUPS}
    lengths = result.get("mesh_maximum_lengths_mm", {})
    if not isinstance(lengths, dict) or set(lengths) - set(MESH_GROUPS):
        raise ValueError("mesh_maximum_lengths_mm contains unsupported groups")
    result["mesh_maximum_lengths_mm"] = {}
    for key, value in lengths.items():
        value = _finite(value, f"mesh_maximum_lengths_mm.{key}")
        if value <= 0:
            raise ValueError("local mesh length must be positive")
        result["mesh_maximum_lengths_mm"][key] = value
    modes = result.get("mesh_refinement_modes", {})
    if not isinstance(modes, dict) or set(modes) - set(MESH_GROUPS):
        raise ValueError("mesh_refinement_modes contains unsupported groups")
    for group, mode in modes.items():
        if mode != "length_only" or group not in result["mesh_maximum_lengths_mm"]:
            raise ValueError("length_only requires a positive length and a supported mode/group")
    if modes:
        result["mesh_refinement_modes"] = dict(modes)
    else:
        result.pop("mesh_refinement_modes", None)
    if "band_circle_segments" in result:
        _integer(result["band_circle_segments"], "band_circle_segments")
    if "initial_position_deg" in result:
        result["initial_position_deg"] = _finite(result["initial_position_deg"], "initial_position_deg")
    for key in ("core_material_model", "core_material", "magnet_material", "shaft_material", "winding_material", "air_material", "setup_name"):
        if key in result and (not isinstance(result[key], str) or not result[key].strip()):
            raise ValueError(f"{key} must be nonblank text")
    return result


@dataclass(frozen=True)
class SelectedProfile:
    settings: dict[str, Any]
    evidence: tuple[dict[str, str], ...]
    producer_fingerprint: str
    manifest_sha256: str
    selection_content_sha256: str
    material_context: dict[str, str] | None = None
    material_fingerprint: str | None = None
    winding_context: dict[str, Any] | None = None


def _profile_context(document: Mapping[str, Any]) -> tuple[dict | None, str | None, dict | None]:
    material, winding = document.get("material_context"), document.get("winding_context")
    if material is not None:
        try:
            if material != asdict(MaterialContextSpec(**material)):
                raise ValueError("both material identifiers must be explicit")
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid selected-profile material context: " + str(exc)) from exc
        material = dict(material)
    fingerprint = document.get("material_fingerprint")
    if fingerprint is not None and not re.fullmatch(r"materials_v2:sha256:[0-9a-f]{64}", str(fingerprint)):
        raise ValueError("selected profile requires a valid actual material fingerprint")
    if winding is not None:
        if not isinstance(winding, dict) or set(winding) != set(WINDING_CONTEXT_FIELDS):
            raise ValueError("selected-profile winding context requires the four explicit fields")
        winding = {key: _finite(value, key) for key, value in winding.items()}
        if any(value <= 0 for value in winding.values()) or any(
                not winding[key].is_integer() for key in WINDING_CONTEXT_FIELDS[:2]) or winding["slot_fill_limit"] > 1:
            raise ValueError("invalid selected-profile winding context")
        for key in WINDING_CONTEXT_FIELDS[:2]: winding[key] = int(winding[key])
    if material and material["core_material_model"] != "legacy_pnf_hybrid_v1" and (fingerprint is None or winding is None):
        raise ValueError("reference profile requires actual material fingerprint and winding context")
    return material, fingerprint, winding


def _selection_content(settings, evidence, producer, material, fingerprint, winding) -> str:
    return _sha(_json_bytes({"settings": settings, "evidence": evidence, "producer": producer,
                             "material_context": material, "material_fingerprint": fingerprint,
                             "winding_context": winding}))


def load_selected_profile(path: Path) -> SelectedProfile:
    path = Path(path)
    payload = path.read_bytes()
    document = _strict_json(payload)
    if document.get("schema_version") != PROFILE_SCHEMA:
        raise ValueError(f"profile schema_version must be {PROFILE_SCHEMA}")
    if document.get("metric_contract_version") != METRIC_CONTRACT:
        raise ValueError("selected profile requires motor_metrics_v3")
    producer = document.get("producer_fingerprint", "")
    if not isinstance(producer, str) or not re.fullmatch(r"producer_v3:sha256:[0-9a-f]{64}", producer):
        raise ValueError("selected profile requires an explicit producer fingerprint")
    settings = document.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("selected profile requires explicit settings")
    evidence = document.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("selected profile requires hashed convergence evidence")
    verified = []
    for record in evidence:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("profile evidence requires path and SHA256")
        source = Path(record["path"])
        source = source if source.is_absolute() else path.parent / source
        digest = record.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or _sha(source.read_bytes()) != digest:
            raise ValueError(f"profile evidence SHA256 mismatch: {source}")
        verified.append({"path": str(source.resolve()), "sha256": digest})
    normalized = profile_settings(settings)
    material, fingerprint, winding = _profile_context(document)
    if material is None:
        legacy = asdict(MaterialContextSpec())
        if any(key in normalized and normalized[key] != value for key, value in legacy.items()):
            raise ValueError("nonlegacy selected profile requires explicit material context")
    content_sha = _selection_content(normalized, verified, producer, material, fingerprint, winding)
    return SelectedProfile(normalized, tuple(verified), producer, _sha(payload), content_sha,
                           material, fingerprint, winding)


def _case_context_fields(material: MaterialContextSpec, winding: WindingSpec) -> dict[str, Any]:
    if (winding.series_turns_per_phase, winding.turns_per_coil_side, winding.parallel_branches) != (48, 12, 1):
        raise ValueError("production winding requires fixed48series turns/12turns per side/1parallel branch")
    if winding.fill_factor > .45 or winding.winding_temperature_c != 100:
        raise ValueError("production winding context requires fill<=0.45 and temperature100C")
    return {**asdict(material), "conductor_area_per_branch_mm2": winding.conductor_area_per_branch_mm2,
            "slot_fill_limit": winding.fill_factor, "series_turns_per_phase": winding.series_turns_per_phase,
            "turns_per_coil_side": winding.turns_per_coil_side}


def _require_plan_context(rows: list[dict], manifest: Mapping[str, Any]) -> None:
    try:
        material_raw, winding_raw = manifest["material_context"], manifest["winding_spec"]
        material, winding = MaterialContextSpec(**material_raw), WindingSpec(**winding_raw)
        if material_raw != asdict(material) or winding_raw != asdict(winding):
            raise ValueError("context must declare all material/winding values")
        expected = _case_context_fields(material, winding)
        if manifest.get("winding_context") != {key: expected[key] for key in WINDING_CONTEXT_FIELDS}:
            raise ValueError("declared winding context differs from its full specification")
        fingerprint = manifest["expected_material_fingerprint"]
        if not isinstance(fingerprint, str) or (fingerprint and not re.fullmatch(r"materials_v2:sha256:[0-9a-f]{64}", fingerprint)):
            raise ValueError("invalid expected material fingerprint")
        if material.core_material_model != "legacy_pnf_hybrid_v1" and not fingerprint:
            raise ValueError("reference context requires a selected-profile material fingerprint")
        expected["expected_material_fingerprint"] = fingerprint
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("training plan material/winding context is invalid: " + str(exc)) from exc
    for row in rows:
        for key, value in expected.items():
            matches = row.get(key) == value if isinstance(value, str) else math.isclose(
                _finite(row.get(key), key), value, rel_tol=1e-12, abs_tol=1e-12)
            if not matches:
                raise ValueError("training plan mixes or changes material/winding context: " + key)


def _production_spec(spec: OptimizationSpec) -> None:
    radius = next(bound for bound in spec.geometry_design_space if bound.name == "stator_outer_radius")
    if radius.upper > 150 or spec.stack_length_bounds.lower < 40 or spec.stack_length_bounds.upper > 70:
        raise ValueError("production plan requires diameter<=300mm and stack bounds within40..70mm")
    if spec.current_limit_a > 137.8 or spec.inverter.vdc_v != 200:
        raise ValueError("production plan requires current<=137.8A and Vdc=200V")
    if spec.constraints.current_density_limit_a_per_mm2 > 6.36 or spec.winding.fill_factor > .45:
        raise ValueError("production plan exceeds approved current-density or slot-fill limits")
    if spec.winding.winding_temperature_c != 100:
        raise ValueError("production phase resistance is defined at winding temperature100C")
    if sorted({point.speed_rpm for point in spec.operating_points}) != [1200., 5000.]:
        raise ValueError("production plan supports the agreed1200/5000rpm operating points")
    if not 0 <= spec.beta_bounds_deg[0] < spec.beta_bounds_deg[1] <= 80:
        raise ValueError("production beta bounds must stay within0..80deg")
    _case_context_fields(spec.material_context, spec.winding)


def _controls_per_geometry(manifest: Mapping[str, Any]) -> int:
    count = _integer(manifest.get("controls_per_geometry", 3), "controls_per_geometry")
    if count not in (3, 4):
        raise ValueError("controls_per_geometry must be3 or4")
    if count == 3 and any(name in manifest for name in ("control_protocol", "support_diagnostics")):
        raise ValueError("three-control legacy plan cannot contain paired-only metadata")
    if count == 4 and manifest.get("control_protocol") != PAIRED_CONTROL_PROTOCOL:
        raise ValueError("four-control plan requires the paired control protocol")
    return count


def _paired_support_diagnostics(rows: list[dict], winding: WindingSpec) -> dict:
    """Input-only feasibility and raw17 box diagnostics, never certification.

    Bounds come only from the preassigned training partition. Derived model
    features can exclude more rows; these counts never filter/reassign them.
    """
    names = (*ACTIVE_GEOMETRY, "stack_length_mm", "i_peak_a", "beta_dq_deg")
    values = [{name: _finite(row[name], name) for name in names} for row in rows]
    training = [value for row, value in zip(rows, values) if row["doe_split"] == "train"]
    if not training:
        raise ValueError("paired support diagnostics require training groups")
    bounds = {name: [min(row[name] for row in training), max(row[name] for row in training)] for name in names}
    outside = {}
    for label in ("train", "calibration", "test"):
        selected = [(row, value) for row, value in zip(rows, values) if row["doe_split"] == label]
        rejected = [row for row, value in selected if any(
            value[name] < bounds[name][0] or value[name] > bounds[name][1] for name in names)]
        outside[label] = {"rows": len(rejected), "groups": len({row["design_hash"] for row in rejected}),
                          "total_rows": len(selected)}
    unique = {row["design_hash"]: row for row in rows}
    fills = []
    for row in unique.values():
        metrics = geometry_metrics(row, _finite(row["stack_length_mm"], "stack_length_mm"), winding)
        if metrics.slot_fill_ratio > winding.fill_factor:
            raise ValueError("paired plan contains an infeasible slot fill")
        fills.append(metrics.slot_fill_ratio)
    return {"schema_version": "paired_raw17_support_diagnostics_v1", "coverage_certified": False,
            "scope": "active14+stack+I+beta; derived model features not evaluated",
            "raw_feature_names": list(names), "training_bounds": bounds, "outside_training_box": outside,
            "feasible_geometry_groups": len(unique), "maximum_slot_fill_ratio": max(fills),
            "slot_fill_limit": winding.fill_factor, "geometry_partition_changed": False,
            "rows_filtered": 0, "test_targets_used": False}


def _require_paired_controls(rows: list[dict], manifest: Mapping[str, Any]) -> None:
    if _controls_per_geometry(manifest) != 4:
        return
    if (manifest.get("profile_convergence_certified_by_generator") is not False
            or manifest.get("numpy_version") != np.__version__ or manifest.get("scipy_version") != scipy.__version__):
        raise ValueError("paired plan certification/dependency contract differs")
    reserve = _integer(manifest.get("retry_attempt_reserve"), "retry_attempt_reserve")
    if (manifest.get("training_attempt_budget") != TRAINING_ATTEMPT_BUDGET
            or manifest.get("nominal_attempts") != len(rows) or manifest.get("cases") != len(rows)
            or len(rows) + reserve > TRAINING_ATTEMPT_BUDGET
            or manifest.get("unallocated_attempts") != TRAINING_ATTEMPT_BUDGET - len(rows) - reserve
            or manifest.get("concurrency_cap") != 300 or manifest.get("submission_performed") is not False
            or manifest.get("bounded_refill_required") is not (len(rows) > 300)
            or manifest.get("planned_wave_sizes") != [min(300, len(rows) - start) for start in range(0, len(rows), 300)]):
        raise ValueError("paired plan attempt budget/wave metadata differs")
    for name in ("current_domain_a", "beta_domain_deg"):
        if not isinstance(manifest.get(name), list) or len(manifest[name]) != 2:
            raise ValueError("paired current/beta domains must contain two endpoints")
    current_domain = [_finite(value, "current_domain_a") for value in manifest["current_domain_a"]]
    beta_domain = [_finite(value, "beta_domain_deg") for value in manifest["beta_domain_deg"]]
    if len(current_domain) != 2 or len(beta_domain) != 2 or not (0 < current_domain[0] < current_domain[1]
            and 0 <= beta_domain[0] < beta_domain[1] <= 80):
        raise ValueError("paired current/beta domains are invalid")
    observations = {}
    training_controls = {1200.: set(), 5000.: set()}
    for row in rows:
        observations.setdefault(row["design_hash"], []).append(row)
        current, beta = _finite(row["i_peak_a"], "i_peak_a"), _finite(row["beta_dq_deg"], "beta_dq_deg")
        if (_finite(row["beta_deg"], "beta_deg") != beta or not current_domain[0] <= current <= current_domain[1]
                or not beta_domain[0] <= beta <= beta_domain[1]):
            raise ValueError("paired control value/alias is outside the declared domain")
        if row["doe_split"] == "train" and _finite(row["base_rpm"], "base_rpm") in training_controls:
            training_controls[float(row["base_rpm"])].add((current, beta))
    for group_rows in observations.values():
        by_speed = {speed: [(_finite(row["i_peak_a"], "i_peak_a"), _finite(row["beta_dq_deg"], "beta_dq_deg"))
                            for row in group_rows if _finite(row["base_rpm"], "base_rpm") == speed]
                    for speed in (1200., 5000.)}
        if (len(group_rows) != 4 or any(len(values) != 2 or len(set(values)) != 2 for values in by_speed.values())
                or set(by_speed[1200.]) != set(by_speed[5000.])):
            raise ValueError("each geometry requires two distinct matched controls at each speed")
    corners = {(current, beta) for current in current_domain for beta in beta_domain}
    if any(not corners <= controls for controls in training_controls.values()):
        raise ValueError("paired training controls lack the four I/beta corner anchors at both speeds")
    expected_speeds = dict(Counter(str(int(_finite(row["base_rpm"], "base_rpm"))) for row in rows))
    if manifest.get("speed_rows") != expected_speeds:
        raise ValueError("paired speed row counts differ from manifest")
    expected_diagnostics = _paired_support_diagnostics(rows, WindingSpec(**manifest["winding_spec"]))
    if manifest.get("support_diagnostics") != expected_diagnostics:
        raise ValueError("paired input-only support diagnostics differ from rows")


def generate_training_plan(
    spec: OptimizationSpec, calibration: Mapping[str, Any], profile: SelectedProfile, *,
    geometries: int = 720, seed: int = 20261030, case_prefix: str = "v3train",
    excluded_hashes: Iterable[str] = (), retry_reserve: int = DEFAULT_RETRY_RESERVE,
    current_min_fraction: float = .25, controls_per_geometry: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build default3 or explicit paired4 rows/geometry, with preassigned group partitions."""
    source_pin = _assert_source_pin()
    from calibrate_ipmsm_beta import validated_zero_manifest, _require_zero_context
    _production_spec(spec)
    geometries = _integer(geometries, "geometries", 5)
    seed = _integer(seed, "seed")
    retry_reserve = _integer(retry_reserve, "retry_reserve")
    control_contract = {"controls_per_geometry": controls_per_geometry}
    if controls_per_geometry == 4:
        control_contract["control_protocol"] = PAIRED_CONTROL_PROTOCOL
    controls_per_geometry = _controls_per_geometry(control_contract)
    if controls_per_geometry * geometries + retry_reserve > TRAINING_ATTEMPT_BUDGET:
        raise ValueError("nominal cases plus retry reserve exceed the2400-attempt training budget")
    if not isinstance(case_prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", case_prefix):
        raise ValueError("case_prefix must be a safe identifier of at most32 characters")
    fraction = _finite(current_min_fraction, "current_min_fraction")
    if not 0 < fraction < 1:
        raise ValueError("loaded-model current_min_fraction must be between0 and1")
    if calibration.get("metric_contract") != METRIC_CONTRACT or calibration.get("back_emf_source") != "periodic_flux_derivative":
        raise ValueError("production DOE requires a v3 physical flux-derived zero manifest")
    angle, calibration_id = validated_zero_manifest(calibration)
    if spec.beta_calibration.calibration_id != calibration_id or not math.isclose(
            spec.beta_calibration.electrical_zero_deg, angle, rel_tol=0, abs_tol=1e-12):
        raise ValueError("spec and supplied calibrated-zero manifest differ")
    if not isinstance(profile, SelectedProfile) or not profile.evidence:
        raise ValueError("an explicitly loaded selected profile is required")
    if not re.fullmatch(r"producer_v3:sha256:[0-9a-f]{64}", profile.producer_fingerprint):
        raise ValueError("selected profile requires an explicit producer fingerprint")
    content_sha = _selection_content(profile.settings, profile.evidence, profile.producer_fingerprint,
                                    profile.material_context, profile.material_fingerprint, profile.winding_context)
    if content_sha != profile.selection_content_sha256:
        raise ValueError("selected profile settings/evidence changed in memory")
    for record in profile.evidence:
        if _sha(Path(record["path"]).read_bytes()) != record["sha256"]:
            raise ValueError("profile evidence changed after selection")
    initial = _finite(calibration.get("initial_position_deg"), "calibration initial_position_deg")
    settings = profile_settings(profile.settings)
    context_fields = _case_context_fields(spec.material_context, spec.winding)
    reference_material = spec.material_context.core_material_model != "legacy_pnf_hybrid_v1"
    if profile.material_context != asdict(spec.material_context) and (reference_material or profile.material_context is not None):
        raise ValueError("selected profile and spec material context differ or reference declaration is missing")
    for key, value in asdict(spec.material_context).items():
        if key in settings and settings[key] != value:
            raise ValueError("selected profile and spec material context differ or reference declaration is missing: " + key)
    if reference_material and (profile.material_fingerprint is None or profile.winding_context is None):
        raise ValueError("reference profile requires actual material fingerprint and winding context")
    if profile.winding_context is not None and any(not math.isclose(
            profile.winding_context[key], context_fields[key], rel_tol=1e-12, abs_tol=1e-12) for key in WINDING_CONTEXT_FIELDS):
        raise ValueError("selected profile and spec winding context differ")
    _require_zero_context(context_fields, calibration)
    calibration_material = calibration.get("material_fingerprint")
    if (reference_material or (calibration_material is not None and profile.material_fingerprint is not None)) and calibration_material != profile.material_fingerprint:
        raise ValueError("zero calibration material fingerprint differs from selected profile")
    if reference_material and calibration.get("producer_fingerprint") != profile.producer_fingerprint:
        raise ValueError("reference zero calibration producer differs from selected profile")
    if "initial_position_deg" in settings and settings["initial_position_deg"] != initial:
        raise ValueError("profile initial position differs from calibrated geometry frame")
    excluded = set(excluded_hashes)
    designs = sample_feasible_geometries(spec, geometries, seed, excluded_hashes=excluded)
    rng = np.random.default_rng(np.random.SeedSequence(seed).spawn(1)[0])
    order = rng.permutation(geometries)
    calibration_count = test_count = max(1, round(geometries * .2))
    labels = ["train"] * geometries
    for index in order[:calibration_count]: labels[int(index)] = "calibration"
    for index in order[calibration_count:calibration_count + test_count]: labels[int(index)] = "test"
    train_indices = [index for index, label in enumerate(labels) if label == "train"]
    if len(train_indices) < 2:
        raise ValueError("training partition needs two distinct geometry anchors")
    maximum = spec.effective_peak_current_limit_a
    minimum = maximum * fraction
    beta_low, beta_high = spec.beta_bounds_deg
    if controls_per_geometry == 3:
        controls = qmc.LatinHypercube(2, seed=seed + 1).random(3 * geometries)
        controls[:, 0] = minimum + (maximum - minimum) * controls[:, 0]
        controls[:, 1] = beta_low + (beta_high - beta_low) * controls[:, 1]
        # Boundary anchors belong to training groups; calibration/test are not used
        # to choose domains or models. Both speeds have beta/current boundary data.
        first, second = train_indices[:2]
        for offset in (0, 1):
            controls[3 * first + offset, 1] = beta_low
            controls[3 * second + offset, 1] = beta_high
        controls[3 * first + 1, 0] = minimum
        controls[3 * second + 1, 0] = maximum
        third_speeds = [1200 if index % 2 == 0 else 5000 for index in range(geometries)]
        rated_low = next((index for index in train_indices if third_speeds[index] == 1200), None)
        if rated_low is None:
            rated_low = first
            swap = next(index for index, rpm in enumerate(third_speeds) if rpm == 1200)
            third_speeds[rated_low], third_speeds[swap] = third_speeds[swap], third_speeds[rated_low]
        controls[3 * rated_low + 2, 0] = minimum
    else:
        controls = qmc.LatinHypercube(2, seed=seed + 1).random(2 * geometries)
        controls[:, 0] = minimum + (maximum - minimum) * controls[:, 0]
        controls[:, 1] = beta_low + (beta_high - beta_low) * controls[:, 1]
        # Four joint I/beta corners, using two distinct settings in each of
        # two training geometries. Every setting is repeated at both speeds.
        first, second = train_indices[:2]
        controls[2 * first:2 * first + 2] = ((minimum, beta_low), (maximum, beta_high))
        controls[2 * second:2 * second + 2] = ((minimum, beta_high), (maximum, beta_low))
    profile_case = {key: value for key, value in settings.items()
                    if key not in {"mesh_elements", "mesh_maximum_lengths_mm", "mesh_refinement_modes", "initial_position_deg"}}
    for group, count in settings["mesh_elements"].items(): profile_case[f"mesh_{group}_elements"] = count
    for group, length in settings["mesh_maximum_lengths_mm"].items(): profile_case[f"mesh_{group}_max_length_mm"] = length
    for group, mode in settings.get("mesh_refinement_modes", {}).items(): profile_case[f"mesh_{group}_mode"] = mode
    rows = []
    for index, (design, depth, key) in enumerate(designs):
        resistance = phase_resistance_100c_ohm(design, depth, spec.winding, slot_number=spec.slot_number)
        speeds = (1200, 5000, third_speeds[index]) if controls_per_geometry == 3 else (1200, 5000, 1200, 5000)
        for point, rpm in enumerate(speeds):
            control_index = 3 * index + point if controls_per_geometry == 3 else 2 * index + point // 2
            current, beta = map(float, controls[control_index])
            if controls_per_geometry == 3 and point == 0: current = maximum
            rows.append({"case_id": f"{case_prefix}_{key[:20]}_p{point}_{rpm}", "geometry_group_id": "v3geo_" + key,
                         "design_hash": key, "doe_split": labels[index], "repeat_of_case_id": "",
                         "operating_point_id": f"map_{rpm}_p{point}", "dataset_schema_version": "ipmsm_v2",
                         "slot_num": spec.slot_number, "pole_num": spec.pole_number, "geometry_mode": "fixed",
                         **design, "stack_length_mm": depth, "phase_resistance_ohm": resistance,
                         "vdc_v": spec.inverter.vdc_v, "base_rpm": rpm, "i_peak_a": current,
                         "beta_dq_deg": beta, "beta_deg": beta, "beta_convention": "dq_current_advance_v2",
                         "electrical_zero_deg": angle, "beta_calibration_id": calibration_id,
                         "initial_position_deg": initial, "series_turns_per_phase": spec.winding.series_turns_per_phase,
                         "turns_per_coil_side": spec.winding.turns_per_coil_side, "model_extent": "full_360",
                         "symmetry_factor": 1, "use_periodic_boundary": False, "operation": "sin_current",
                         **profile_case, **context_fields,
                         "expected_material_fingerprint": profile.material_fingerprint or ""})
            if current_density_a_per_mm2(current, spec.winding) > spec.constraints.current_density_limit_a_per_mm2 * (1 + 1e-12):
                raise ValueError("generated current violates the declared RMS conductor current density")
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate case identities")
    from run_ipmsm_batch import build_spec
    applied = build_spec(rows[0])
    for key, value in settings.items():
        if key != "quality_profile" and getattr(applied, key, None) != value:
            raise ValueError(f"runner does not apply selected profile setting {key!r}")
    for key, value in context_fields.items():
        if getattr(applied, key, None) != value:
            raise ValueError(f"runner does not apply declared material/winding field {key!r}")
    groups = {label: sorted(row["design_hash"] for row in rows[::controls_per_geometry] if row["doe_split"] == label)
              for label in ("train", "calibration", "test")}
    manifest = {"schema_version": PLAN_SCHEMA, "seed": seed, "cases": len(rows), "geometries": geometries,
                "case_prefix": case_prefix, "group_counts": {key: len(values) for key, values in groups.items()},
                "group_partition_sha256": _sha(_json_bytes(groups)), "group_partitions": groups,
                "speed_rows": dict(Counter(str(row["base_rpm"]) for row in rows)),
                "geometry_identity": "active14+stack_mm; geometry12decimal/depth6significant; topology8p12s",
                "frozen_inactive_parameters": {"stator_shoe_thick": 1.5}, "excluded_geometry_count": len(excluded),
                "current_domain_a": [minimum, maximum], "beta_domain_deg": [beta_low, beta_high],
                "training_attempt_budget": TRAINING_ATTEMPT_BUDGET, "nominal_attempts": len(rows),
                "retry_attempt_reserve": retry_reserve, "unallocated_attempts": TRAINING_ATTEMPT_BUDGET-len(rows)-retry_reserve,
                "concurrency_cap": 300, "bounded_refill_required": len(rows) > 300,
                "planned_wave_sizes": [min(300, len(rows)-start) for start in range(0, len(rows), 300)],
                "calibration_id": calibration_id, "calibration_sha256": _sha(_json_bytes(calibration)),
                "calibration_producer_fingerprint": calibration.get("producer_fingerprint"),
                "profile_sha256": profile.manifest_sha256, "profile_selection_sha256": profile.selection_content_sha256,
                "profile_evidence": list(profile.evidence),
                "profile_settings": settings, "profile_convergence_certified_by_generator": False,
                "expected_metric_contract": METRIC_CONTRACT, "expected_producer_fingerprint": profile.producer_fingerprint,
                "spec_sha256": _sha(_json_bytes(asdict(spec))),
                "material_context": asdict(spec.material_context),
                "winding_context": {key: context_fields[key] for key in WINDING_CONTEXT_FIELDS},
                "winding_spec": asdict(spec.winding), "expected_material_fingerprint": profile.material_fingerprint or "",
                **source_pin, "generator_sha256": source_pin["source_sha256"][_GENERATOR_NAME],
                "geometry_helper_sha256": source_pin["source_sha256"][_HELPER_NAME], "numpy_version": np.__version__,
                "scipy_version": scipy.__version__, "submission_performed": False}
    if controls_per_geometry == 4:
        manifest.update(controls_per_geometry=4, control_protocol=PAIRED_CONTROL_PROTOCOL,
                        support_diagnostics=_paired_support_diagnostics(rows, spec.winding))
        _require_paired_controls(rows, manifest)
    _require_plan_context(rows, manifest)
    _assert_source_pin(source_pin)
    return rows, manifest


def write_training_plan(path: Path, rows: list[dict], manifest: Mapping[str, Any]) -> dict:
    source_pin = _assert_source_pin()
    _require_manifest_sources(manifest, source_pin)
    _require_plan_context(rows, manifest)
    _require_paired_controls(rows, manifest)
    path = Path(path)
    proof_path = path.with_suffix(".manifest.json")
    if path.suffix.lower() != ".csv": raise ValueError("plan output must have a .csv suffix")
    if path.exists() or proof_path.exists(): raise FileExistsError("plan or manifest already exists")
    if not rows or len({row["case_id"] for row in rows}) != len(rows): raise ValueError("plan has empty/duplicate cases")
    if any(set(row) != set(rows[0]) for row in rows):
        raise ValueError("plan rows have inconsistent fields")
    if manifest.get("cases") != len(rows) or manifest.get("nominal_attempts") != len(rows):
        raise ValueError("plan rows differ from manifest counts")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    payload = stream.getvalue().encode("utf-8")
    proof = dict(manifest, plan_sha256=_sha(payload), plan_file=path.name)
    proof["manifest_id"] = "training_plan_v3:sha256:" + _sha(_json_bytes(proof))
    _assert_source_pin(source_pin)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Both names are exclusive. A crash may leave partial evidence, which must
    # be retained/reconciled; consumers verify both hashes before submitting.
    with path.open("xb") as output, proof_path.open("xb") as certificate:
        for file, data in [(output, payload), (certificate, _json_bytes(proof) + b"\n")]:
            file.write(data); file.flush(); os.fsync(file.fileno())
    verify_training_plan(path)
    return proof


def verify_training_plan(path: Path) -> dict:
    source_pin = _assert_source_pin()
    path = Path(path)
    proof = _strict_json(path.with_suffix(".manifest.json").read_bytes())
    identifier = proof.pop("manifest_id", None)
    if identifier != "training_plan_v3:sha256:" + _sha(_json_bytes(proof)):
        raise ValueError("training manifest content differs from its identifier")
    _require_manifest_sources(proof, source_pin)
    payload = path.read_bytes()
    if proof.get("schema_version") != PLAN_SCHEMA or proof.get("plan_sha256") != _sha(payload):
        raise ValueError("training plan SHA256/schema mismatch")
    rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
    if len(rows) != proof.get("cases") or len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("training plan case count/identities differ")
    _require_plan_context(rows, proof)
    reserve = _integer(proof.get("retry_attempt_reserve"), "retry_attempt_reserve")
    if proof.get("training_attempt_budget") != TRAINING_ATTEMPT_BUDGET or proof.get("nominal_attempts") != len(rows) or len(rows)+reserve > TRAINING_ATTEMPT_BUDGET:
        raise ValueError("training plan attempt budget differs")
    groups: dict[str, set[str]] = {label: set() for label in ("train", "calibration", "test")}
    observations: dict[str, list[dict]] = {}
    for row in rows:
        key = physical_geometry_hash(row)
        if row.get("design_hash") != key or row.get("geometry_group_id") != "v3geo_" + key:
            raise ValueError("case physical geometry differs from group identity")
        label = row.get("doe_split")
        if label not in groups:
            raise ValueError("unknown preassigned group partition")
        groups[label].add(key)
        observations.setdefault(key, []).append(row)
    if any(groups[a] & groups[b] for a, b in [("train", "calibration"), ("train", "test"), ("calibration", "test")]):
        raise ValueError("geometry group leaks across partitions")
    actual = {label: sorted(values) for label, values in groups.items()}
    if proof.get("group_partitions") != actual or proof.get("group_partition_sha256") != _sha(_json_bytes(actual)):
        raise ValueError("preassigned geometry partitions differ from manifest")
    if proof.get("geometries") != len(observations) or proof.get("group_counts") != {label: len(values) for label, values in groups.items()}:
        raise ValueError("geometry group count differs from manifest")
    controls_per_geometry = _controls_per_geometry(proof)
    for group_rows in observations.values():
        if len(group_rows) != controls_per_geometry or {_finite(row["base_rpm"], "base_rpm") for row in group_rows} != {1200., 5000.}:
            raise ValueError(f"each geometry requires {controls_per_geometry} controls covering both speeds")
    _require_paired_controls(rows, proof)
    proof["manifest_id"] = identifier
    _assert_source_pin(source_pin)
    return proof


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--calibration-manifest", type=Path, required=True)
    parser.add_argument("--profile-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclude", type=Path, action="append", default=[])
    parser.add_argument("--geometries", type=int, default=720)
    parser.add_argument("--controls-per-geometry", type=int, choices=(3, 4), default=3)
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument("--case-prefix", default="v3train")
    parser.add_argument("--retry-reserve", type=int, default=DEFAULT_RETRY_RESERVE)
    parser.add_argument("--current-min-fraction", type=float, default=.25)
    args = parser.parse_args(argv)
    from calibrate_ipmsm_beta import apply_zero_manifest_to_spec
    spec_raw = _strict_json(args.spec.read_bytes())
    calibration = _strict_json(args.calibration_manifest.read_bytes())
    spec = optimization_spec_from_mapping(apply_zero_manifest_to_spec(spec_raw, calibration))
    profile = load_selected_profile(args.profile_manifest)
    excluded, exclusions = exclusion_hashes(args.exclude)
    rows, manifest = generate_training_plan(spec, calibration, profile, geometries=args.geometries, seed=args.seed,
                                            case_prefix=args.case_prefix, excluded_hashes=excluded,
                                            retry_reserve=args.retry_reserve, current_min_fraction=args.current_min_fraction,
                                            controls_per_geometry=args.controls_per_geometry)
    manifest.update(excluded_sources=exclusions, spec_file_sha256=_sha(args.spec.read_bytes()),
                    calibration_file_sha256=_sha(args.calibration_manifest.read_bytes()))
    proof = write_training_plan(args.output, rows, manifest)
    print(f"Planned {len(rows)} cases/{args.geometries} groups; SHA256={proof['plan_sha256']}; "
          f"retry reserve={args.retry_reserve}; bounded refill={proof['bounded_refill_required']}; no submissions")
    return 0


_IMPORTED_SOURCE_PIN = _capture_source_pin()


if __name__ == "__main__":
    raise SystemExit(main())
