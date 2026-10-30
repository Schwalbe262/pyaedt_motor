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
    GEOMETRY_VARIABLE_NAMES, OptimizationSpec, current_density_a_per_mm2,
    geometry_metrics, optimization_spec_from_mapping, phase_resistance_100c_ohm,
)

PLAN_SCHEMA = "ipmsm_v3_training_plan_v2"
PROFILE_SCHEMA = "ipmsm_v3_selected_profile_v1"
METRIC_CONTRACT = "motor_metrics_v3"
ACTIVE_GEOMETRY = tuple(name for name in GEOMETRY_VARIABLE_NAMES if name != "stator_shoe_thick")
MESH_GROUPS = ("magnet", "rotor", "stator", "winding", "band")
TRAINING_ATTEMPT_BUDGET = 2400
DEFAULT_RETRY_RESERVE = 240
CANONICAL_FLAGS = {"circle_segments": 0, "loss_policy": "laminated_core", "strict_setup": True,
                   "require_steady_state": True, "compute_inductance": True, "analysis_purpose": "training"}
_GENERATOR_NAME = "src/pyaedt_motor/doe.py"
_HELPER_NAME = "ipmsm_optimization.py"
_SOURCE_PATHS = {_GENERATOR_NAME: Path(__file__).resolve(),
                 _HELPER_NAME: Path(__file__).resolve().parents[2] / _HELPER_NAME}
_GENERATOR_MODULE = sys.modules[__name__]
_HELPER_BINDINGS = {name: globals()[name] for name in (
    "GEOMETRY_VARIABLE_NAMES", "OptimizationSpec", "current_density_a_per_mm2",
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
               "mesh_maximum_lengths_mm", "core_material", "magnet_material", "shaft_material",
               "winding_material", "air_material", "initial_position_deg", "setup_name",
               "band_circle_segments", *CANONICAL_FLAGS}
    if set(result) - allowed:
        raise ValueError(f"unsupported profile settings: {sorted(set(result) - allowed)}")
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
    if "band_circle_segments" in result:
        _integer(result["band_circle_segments"], "band_circle_segments")
    if "initial_position_deg" in result:
        result["initial_position_deg"] = _finite(result["initial_position_deg"], "initial_position_deg")
    for key in ("core_material", "magnet_material", "shaft_material", "winding_material", "air_material", "setup_name"):
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
    content_sha = _sha(_json_bytes({"settings": normalized, "evidence": verified, "producer": producer}))
    return SelectedProfile(normalized, tuple(verified), producer, _sha(payload), content_sha)


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


def generate_training_plan(
    spec: OptimizationSpec, calibration: Mapping[str, Any], profile: SelectedProfile, *,
    geometries: int = 720, seed: int = 20261030, case_prefix: str = "v3train",
    excluded_hashes: Iterable[str] = (), retry_reserve: int = DEFAULT_RETRY_RESERVE,
    current_min_fraction: float = .25,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build three controls/geometry with independent preassigned group partitions."""
    source_pin = _assert_source_pin()
    from calibrate_ipmsm_beta import validated_zero_manifest
    _production_spec(spec)
    geometries = _integer(geometries, "geometries", 5)
    seed = _integer(seed, "seed")
    retry_reserve = _integer(retry_reserve, "retry_reserve")
    if 3 * geometries + retry_reserve > TRAINING_ATTEMPT_BUDGET:
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
    content_sha = _sha(_json_bytes({"settings": profile.settings, "evidence": profile.evidence,
                                    "producer": profile.producer_fingerprint}))
    if content_sha != profile.selection_content_sha256:
        raise ValueError("selected profile settings/evidence changed in memory")
    for record in profile.evidence:
        if _sha(Path(record["path"]).read_bytes()) != record["sha256"]:
            raise ValueError("profile evidence changed after selection")
    initial = _finite(calibration.get("initial_position_deg"), "calibration initial_position_deg")
    settings = profile_settings(profile.settings)
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
    profile_case = {key: value for key, value in settings.items()
                    if key not in {"mesh_elements", "mesh_maximum_lengths_mm", "initial_position_deg"}}
    for group, count in settings["mesh_elements"].items(): profile_case[f"mesh_{group}_elements"] = count
    for group, length in settings["mesh_maximum_lengths_mm"].items(): profile_case[f"mesh_{group}_max_length_mm"] = length
    rows = []
    for index, (design, depth, key) in enumerate(designs):
        resistance = phase_resistance_100c_ohm(design, depth, spec.winding, slot_number=spec.slot_number)
        for point, rpm in enumerate((1200, 5000, third_speeds[index])):
            current, beta = map(float, controls[3 * index + point])
            if point == 0: current = maximum
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
                         "symmetry_factor": 1, "use_periodic_boundary": False, "operation": "sin_current", **profile_case})
            if current_density_a_per_mm2(current, spec.winding) > spec.constraints.current_density_limit_a_per_mm2 * (1 + 1e-12):
                raise ValueError("generated current violates the declared RMS conductor current density")
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate case identities")
    from run_ipmsm_batch import build_spec
    applied = build_spec(rows[0])
    for key, value in settings.items():
        if key != "quality_profile" and getattr(applied, key, None) != value:
            raise ValueError(f"runner does not apply selected profile setting {key!r}")
    groups = {label: sorted(row["design_hash"] for row in rows[::3] if row["doe_split"] == label)
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
                **source_pin, "generator_sha256": source_pin["source_sha256"][_GENERATOR_NAME],
                "geometry_helper_sha256": source_pin["source_sha256"][_HELPER_NAME], "numpy_version": np.__version__,
                "scipy_version": scipy.__version__, "submission_performed": False}
    _assert_source_pin(source_pin)
    return rows, manifest


def write_training_plan(path: Path, rows: list[dict], manifest: Mapping[str, Any]) -> dict:
    source_pin = _assert_source_pin()
    _require_manifest_sources(manifest, source_pin)
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
    for group_rows in observations.values():
        if len(group_rows) != 3 or {_finite(row["base_rpm"], "base_rpm") for row in group_rows} != {1200., 5000.}:
            raise ValueError("each geometry requires three controls covering both speeds")
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
                                            retry_reserve=args.retry_reserve, current_min_fraction=args.current_min_fraction)
    manifest.update(excluded_sources=exclusions, spec_file_sha256=_sha(args.spec.read_bytes()),
                    calibration_file_sha256=_sha(args.calibration_manifest.read_bytes()))
    proof = write_training_plan(args.output, rows, manifest)
    print(f"Planned {len(rows)} cases/{args.geometries} groups; SHA256={proof['plan_sha256']}; "
          f"retry reserve={args.retry_reserve}; bounded refill={proof['bounded_refill_required']}; no submissions")
    return 0


_IMPORTED_SOURCE_PIN = _capture_source_pin()


if __name__ == "__main__":
    raise SystemExit(main())
