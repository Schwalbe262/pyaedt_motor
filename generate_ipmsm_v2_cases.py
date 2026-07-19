"""Generate deterministic, grouped IPMSM v2 foundation DOE case rows."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pickle
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from atomic_publish import (
    FileIdentity,
    PROOF_SCHEMA_VERSION,
    PublishReceipt,
    cleanup_publish_receipt,
    publish_no_replace,
    receipt_owns_destination,
    recover_owned_output,
    rollback_owned_output,
)
import continue_ipmsm_v2_stage2 as stage2_continuation
from generate_ipmsm_quality_cases import MESH_ELEMENT_KEYS, QUALITY_PROFILES
from ipmsm_optimization import (
    OptimizationSpec,
    geometry_metrics,
    load_optimization_spec as load_optimization_spec,
    optimization_spec_from_mapping,
    phase_resistance_100c_ohm,
)
import train_ipmsm_lightgbm as trainer


DATASET_SCHEMA_VERSION = "ipmsm_v2"
BETA_CONVENTION = "dq_current_advance_v2"
MODEL_EXTENT = "full_360"
STAGE3_SCHEMA_VERSION = "ipmsm_v2_stage3_fallback_plan_v2"
STAGE2_DECISION_SCHEMA_VERSION = "ipmsm_v2_stage2_continuation_v1"
STAGE3_TRAIN_GEOMETRIES = 20
STAGE3_CALIBRATION_GEOMETRIES = 10
STAGE3_FINAL_AUDIT_GEOMETRIES = 20
STAGE3_SAMPLES_PER_OPERATING_POINT = 3
STAGE3_ADAPTATION_SEED = 730_031
STAGE3_CALIBRATION_SEED = 730_033
STAGE3_FINAL_AUDIT_SEED = 730_037
STAGE3_CANDIDATE_POOL_GEOMETRIES = 1024
STAGE3_NEAREST_AUDIT_ROWS = 5
STAGE3_ADAPTIVE_SELECTION_VERSION = "stage3_audit_residual_adaptive_v2"
TRAINING_ARTIFACT_CONTRACT_SCHEMA_VERSION = trainer.V2_ARTIFACT_CONTRACT_SCHEMA_VERSION
STAGE3_RESIDUAL_WEIGHT = 0.50
STAGE3_UNCERTAINTY_WEIGHT = 0.30
STAGE3_DOMAIN_DISTANCE_WEIGHT = 0.20
STAGE3_DIVERSITY_WEIGHT = 0.20
STAGE3_MIN_INVALID_DERIVED_GEOMETRIES = 2

METADATA_FIELDS = (
    "case_id",
    "geometry_group_id",
    "design_hash",
    "operating_point_id",
    "doe_split",
    "repeat_of_case_id",
    "beta_calibration_id",
    "dataset_schema_version",
    "quality_profile",
    "model_extent",
    "symmetry_factor",
    "use_periodic_boundary",
    "beta_convention",
    "electrical_zero_deg",
    "operation",
)

OPERATING_FIELDS = (
    "base_rpm",
    "i_peak_a",
    "beta_dq_deg",
    "stack_length_mm",
    "phase_resistance_ohm",
    "vdc_v",
)


def _scipy_qmc() -> Any:
    try:
        from scipy.stats import qmc
    except ImportError as exc:  # pragma: no cover - dependency gate
        raise RuntimeError("scipy is required for scrambled-Sobol v2 DOE generation") from exc
    return qmc


def stable_design_hash(design: dict[str, float], stack_length_mm: float) -> str:
    payload = {**design, "stack_length_mm": float(stack_length_mm)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def safe_name(value: object) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in str(value))


def split_by_design_hash(design_hashes: Iterable[str]) -> dict[str, str]:
    ordered = sorted(set(design_hashes))
    count = len(ordered)
    train_end = math.floor(count * 0.60)
    calibration_end = train_end + math.floor(count * 0.20)
    # Small smoke plans still need every split represented when possible.
    if count >= 3:
        train_end = max(1, train_end)
        calibration_end = max(train_end + 1, calibration_end)
        calibration_end = min(count - 1, calibration_end)
    result: dict[str, str] = {}
    for index, design_hash in enumerate(ordered):
        if index < train_end:
            split = "train"
        elif index < calibration_end:
            split = "calibration"
        else:
            split = "test"
        result[design_hash] = split
    return result


def _quality_profile_values(name: str) -> dict[str, Any]:
    try:
        profile = QUALITY_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown quality profile {name!r}") from exc
    values: dict[str, Any] = {
        "transient_periods": profile.transient_periods,
        "steps_per_period": profile.steps_per_period,
    }
    for key in MESH_ELEMENT_KEYS:
        values[f"mesh_{key}_elements"] = profile.mesh_elements[key]
    return values


def _valid_geometry_samples(
    spec: OptimizationSpec,
    count: int,
    seed: int,
    *,
    excluded_design_hashes: Iterable[str] = (),
) -> list[tuple[dict[str, float], float, str]]:
    qmc = _scipy_qmc()
    bounds = spec.design_space
    sampler = qmc.Sobol(d=len(bounds), scramble=True, seed=seed)
    accepted: list[tuple[dict[str, float], float, str]] = []
    seen: set[str] = {str(value).strip() for value in excluded_design_hashes if str(value).strip()}
    attempts = 0
    max_attempts = max(4096, count * 256)
    while len(accepted) < count and attempts < max_attempts:
        unit_rows = sampler.random(n=min(256, max_attempts - attempts))
        for unit_row in unit_rows:
            attempts += 1
            values = {
                bound.name: bound.lower + float(unit) * (bound.upper - bound.lower)
                for bound, unit in zip(bounds, unit_row)
            }
            stack = values.pop("stack_length_mm")
            design_hash = stable_design_hash(values, stack)
            if design_hash in seen:
                continue
            try:
                metrics = geometry_metrics(values, stack, spec.winding, slot_number=spec.slot_number)
            except ValueError:
                continue
            if metrics.slot_fill_ratio > spec.winding.fill_factor:
                continue
            seen.add(design_hash)
            accepted.append((values, stack, design_hash))
            if len(accepted) >= count:
                break
    if len(accepted) != count:
        raise RuntimeError(
            f"could only generate {len(accepted)}/{count} feasible geometry groups after {attempts} Sobol candidates"
        )
    return accepted


def _operating_samples(spec: OptimizationSpec, geometry_count: int, samples_per_point: int, seed: int) -> list[list[tuple[float, float]]]:
    qmc = _scipy_qmc()
    total = geometry_count * len(spec.operating_points) * samples_per_point
    sampler = qmc.Sobol(d=2, scramble=True, seed=seed + 104729)
    unit_rows = sampler.random_base2(m=max(0, math.ceil(math.log2(total))))[:total]
    current_limit = min(
        spec.current_limit_a,
        spec.constraints.current_density_limit_a_per_mm2
        * spec.winding.total_parallel_conductor_area_mm2
        * math.sqrt(2.0),
    )
    beta_low, beta_high = spec.beta_bounds_deg
    rows = [
        (
            current_limit * (0.25 + 0.75 * float(unit[0])),
            beta_low + float(unit[1]) * (beta_high - beta_low),
        )
        for unit in unit_rows
    ]
    result: list[list[tuple[float, float]]] = []
    cursor = 0
    for _ in range(geometry_count):
        group: list[tuple[float, float]] = []
        for _point in spec.operating_points:
            group.extend(rows[cursor : cursor + samples_per_point])
            cursor += samples_per_point
        result.append(group)
    return result


def generate_foundation_rows(
    spec: OptimizationSpec,
    *,
    geometry_count: int = 160,
    samples_per_operating_point: int = 3,
    repeat_count: int = 40,
    seed: int = 42,
    quality_profile: str = "reference_ultra",
    electrical_zero_deg: float | None = None,
    case_prefix: str = "v2",
    excluded_design_hashes: Iterable[str] = (),
) -> list[dict[str, Any]]:
    if geometry_count < 1 or samples_per_operating_point < 1 or repeat_count < 0:
        raise ValueError("geometry_count and samples_per_operating_point must be positive; repeat_count must be nonnegative")
    if electrical_zero_deg is None:
        electrical_zero_deg = spec.beta_calibration.electrical_zero_deg
    if not math.isfinite(electrical_zero_deg):
        raise ValueError("electrical_zero_deg must be finite")
    if not math.isclose(
        electrical_zero_deg,
        spec.beta_calibration.electrical_zero_deg,
        abs_tol=1e-12,
    ):
        raise ValueError("electrical_zero_deg does not match the required beta_calibration manifest")
    prefix = safe_name(case_prefix).strip("_")
    if not prefix:
        raise ValueError("case_prefix must contain at least one safe character")
    profile_values = _quality_profile_values(quality_profile)
    geometries = _valid_geometry_samples(
        spec,
        geometry_count,
        seed,
        excluded_design_hashes=excluded_design_hashes,
    )
    operating_samples = _operating_samples(spec, geometry_count, samples_per_operating_point, seed)
    split_map = split_by_design_hash(design_hash for _, _, design_hash in geometries)
    rows: list[dict[str, Any]] = []
    loaded_train_rows: list[dict[str, Any]] = []
    current_limit = spec.effective_peak_current_limit_a
    beta_low, beta_high = spec.beta_bounds_deg
    control_anchors = (
        (0.25 * current_limit, beta_low),
        (current_limit, beta_high),
        (0.25 * current_limit, beta_high),
        (current_limit, beta_low),
    )
    anchor_counts = {"train": 0, "calibration": 0, "test": 0}

    for geometry_index, ((design, stack, design_hash), group_samples) in enumerate(zip(geometries, operating_samples), start=1):
        geometry_group_id = f"{prefix}_geometry_{geometry_index:04d}_{design_hash[:12]}"
        common: dict[str, Any] = {
            "geometry_group_id": geometry_group_id,
            "design_hash": design_hash,
            "doe_split": split_map[design_hash],
            "repeat_of_case_id": "",
            "beta_calibration_id": spec.beta_calibration.calibration_id,
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "quality_profile": quality_profile,
            "model_extent": MODEL_EXTENT,
            "symmetry_factor": 1,
            "use_periodic_boundary": False,
            "beta_convention": BETA_CONVENTION,
            "electrical_zero_deg": electrical_zero_deg,
            "slot_num": spec.slot_number,
            "pole_num": spec.pole_number,
            "stack_length_mm": stack,
            "phase_resistance_ohm": phase_resistance_100c_ohm(
                design,
                stack,
                spec.winding,
                slot_number=spec.slot_number,
            ),
            "vdc_v": spec.inverter.vdc_v,
            **design,
            **profile_values,
        }
        sample_index = 0
        for point in spec.operating_points:
            for local_index in range(1, samples_per_operating_point + 1):
                current, beta = group_samples[sample_index]
                sample_index += 1
                split_name = str(common["doe_split"])
                anchor_index = anchor_counts[split_name]
                if anchor_index < len(control_anchors):
                    current, beta = control_anchors[anchor_index]
                    anchor_counts[split_name] += 1
                loaded = {
                    **common,
                    "case_id": f"{prefix}_{geometry_index:04d}_{safe_name(point.name)}_{local_index:02d}",
                    "operating_point_id": point.name,
                    "operation": "sin_current",
                    "base_rpm": point.speed_rpm,
                    "i_peak_a": current,
                    "beta_dq_deg": beta,
                }
                rows.append(loaded)
                if common["doe_split"] == "train":
                    loaded_train_rows.append(loaded)

    if repeat_count and not loaded_train_rows:
        raise RuntimeError("repeat rows require at least one train geometry group")
    repeat_sources_by_group: dict[str, list[dict[str, Any]]] = {}
    for source in loaded_train_rows:
        repeat_sources_by_group.setdefault(str(source["geometry_group_id"]), []).append(source)
    repeat_group_ids = sorted(repeat_sources_by_group)
    for repeat_index in range(repeat_count):
        group_index = repeat_index % len(repeat_group_ids)
        group_cycle = repeat_index // len(repeat_group_ids)
        group_rows = repeat_sources_by_group[repeat_group_ids[group_index]]
        source = group_rows[group_cycle % len(group_rows)]
        repeat = dict(source)
        repeat["case_id"] = f"{source['case_id']}_repeat_{repeat_index + 1:03d}"
        repeat["repeat_of_case_id"] = source["case_id"]
        rows.append(repeat)

    case_ids = [str(row["case_id"]) for row in rows]
    if len(case_ids) != len(set(case_ids)):
        raise RuntimeError("generated v2 case IDs are not unique")
    return rows


def fieldnames_for_rows(spec: OptimizationSpec) -> list[str]:
    profile_fields = ["transient_periods", "steps_per_period", *(f"mesh_{key}_elements" for key in MESH_ELEMENT_KEYS)]
    topology_fields = ["slot_num", "pole_num"]
    geometry_fields = [bound.name for bound in spec.geometry_design_space]
    return [*METADATA_FIELDS, *topology_fields, *geometry_fields, *OPERATING_FIELDS, *profile_fields]


def write_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_excluded_design_hashes(paths: Iterable[Path]) -> set[str]:
    hashes: set[str] = set()
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if not reader.fieldnames or not ({"design_hash", "input_design_hash"} & set(reader.fieldnames)):
                raise ValueError(f"exclusion CSV has no design_hash column: {path}")
            for row in reader:
                value = str(row.get("design_hash") or row.get("input_design_hash") or "").strip()
                if value:
                    hashes.add(value)
    return hashes


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return _bytes_sha256(encoded)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value!r}")


def _strict_json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return decoded


def _verified_artifact(record: object, label: str) -> tuple[Path, bytes, dict[str, str]]:
    if not isinstance(record, Mapping):
        raise ValueError(f"{label} artifact contract must be an object")
    raw_path = str(record.get("path") or "").strip()
    recorded_hash = str(record.get("sha256") or "").strip().lower()
    if not raw_path or len(recorded_hash) != 64 or any(character not in "0123456789abcdef" for character in recorded_hash):
        raise ValueError(f"{label} artifact contract is incomplete")
    path = Path(raw_path).resolve(strict=False)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} artifact is missing or unreadable: {path}: {exc}") from exc
    actual_hash = _bytes_sha256(payload)
    if actual_hash != recorded_hash:
        raise ValueError(
            f"{label} artifact hash mismatch: expected={recorded_hash}, actual={actual_hash}"
        )
    return path, payload, {"path": str(path), "sha256": actual_hash}


def _csv_rows_from_bytes(payload: bytes, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(f"cannot decode {label}: {exc}") from exc
    try:
        with io.StringIO(text, newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = list(reader.fieldnames or ())
            if not fieldnames or len(fieldnames) != len(set(fieldnames)):
                raise ValueError(f"{label} has a missing or duplicate CSV header")
            rows = [dict(row) for row in reader]
    except csv.Error as exc:
        raise ValueError(f"cannot parse {label}: {exc}") from exc
    if any(None in row for row in rows):
        raise ValueError(f"{label} has fields beyond its CSV header")
    return fieldnames, rows


def _finite_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _false_like(value: object) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def stage3_exclusion_contract(paths: Iterable[Path]) -> tuple[set[str], list[dict[str, Any]]]:
    plans = [Path(path) for path in paths]
    resolved = [path.resolve(strict=False) for path in plans]
    if len(plans) != 2 or len(set(resolved)) != 2:
        raise ValueError("--stage3-fallback requires exactly two distinct --exclude-case-plan files")
    excluded: set[str] = set()
    case_ids: set[str] = set()
    design_sets: list[set[str]] = []
    calibration_ids: set[str] = set()
    electrical_zeros: set[float] = set()
    artifacts: list[dict[str, Any]] = []
    required = {
        "case_id",
        "design_hash",
        "dataset_schema_version",
        "quality_profile",
        "model_extent",
        "symmetry_factor",
        "use_periodic_boundary",
        "beta_convention",
        "beta_calibration_id",
        "electrical_zero_deg",
    }
    for path in plans:
        if not path.is_file():
            raise ValueError(f"Stage3 exclusion plan is missing: {path}")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read Stage3 exclusion plan {path}: {exc}") from exc
        fieldnames, rows = _csv_rows_from_bytes(payload, f"Stage3 exclusion plan {path}")
        missing = sorted(required - set(fieldnames))
        if missing:
            raise ValueError(f"Stage3 exclusion plan {path} is missing strict columns: {missing}")
        if not rows:
            raise ValueError(f"Stage3 exclusion plan is empty: {path}")
        plan_hashes: set[str] = set()
        plan_calibration_ids: set[str] = set()
        plan_electrical_zeros: set[float] = set()
        for index, row in enumerate(rows, start=1):
            case_id = str(row.get("case_id") or "").strip()
            design_hash = str(row.get("design_hash") or "").strip()
            strict = (
                str(row.get("dataset_schema_version") or "").strip() == DATASET_SCHEMA_VERSION
                and str(row.get("quality_profile") or "").strip() == "reference_ultra"
                and str(row.get("model_extent") or "").strip() == MODEL_EXTENT
                and str(row.get("beta_convention") or "").strip() == BETA_CONVENTION
                and str(row.get("beta_calibration_id") or "").strip()
                and _false_like(row.get("use_periodic_boundary"))
            )
            try:
                symmetry_ok = math.isclose(float(row.get("symmetry_factor", "nan")), 1.0, abs_tol=1e-12)
            except (TypeError, ValueError):
                symmetry_ok = False
            if not case_id or not design_hash or not strict or not symmetry_ok:
                raise ValueError(f"Stage3 exclusion plan {path} row {index} is not strict ipmsm_v2")
            try:
                electrical_zero = float(row["electrical_zero_deg"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Stage3 exclusion plan {path} row {index} has invalid electrical_zero_deg") from exc
            if not math.isfinite(electrical_zero):
                raise ValueError(f"Stage3 exclusion plan {path} row {index} has invalid electrical_zero_deg")
            if case_id in case_ids:
                raise ValueError(f"Stage3 exclusion plans contain duplicate case_id={case_id!r}")
            case_ids.add(case_id)
            plan_hashes.add(design_hash)
            plan_calibration_ids.add(str(row["beta_calibration_id"]).strip())
            plan_electrical_zeros.add(electrical_zero)
        if len(plan_calibration_ids) != 1 or len(plan_electrical_zeros) != 1:
            raise ValueError(f"Stage3 exclusion plan mixes beta calibration identity: {path}")
        calibration_ids.update(plan_calibration_ids)
        electrical_zeros.update(plan_electrical_zeros)
        design_sets.append(plan_hashes)
        excluded.update(plan_hashes)
        artifacts.append(
            {
                "path": str(path.resolve(strict=False)),
                "sha256": _bytes_sha256(payload),
                "rows": len(rows),
                "design_hashes": len(plan_hashes),
                "beta_calibration_id": next(iter(plan_calibration_ids)),
                "electrical_zero_deg": next(iter(plan_electrical_zeros)),
            }
        )
    overlap = sorted(design_sets[0] & design_sets[1])
    if overlap:
        raise ValueError(f"Stage1/Stage2 exclusion plans overlap by design_hash: {overlap[:3]}")
    if len(calibration_ids) != 1 or len(electrical_zeros) != 1:
        raise ValueError("Stage1/Stage2 exclusion plans use different beta calibration identity")
    return excluded, artifacts


def _rows_by_design_hash(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        design_hash = str(row.get("design_hash") or "").strip()
        if not design_hash:
            raise ValueError("generated Stage3 row has a blank design_hash")
        grouped.setdefault(design_hash, []).append(row)
    return grouped


def generate_stage3_sealed_rows(
    spec: OptimizationSpec,
    *,
    excluded_design_hashes: Iterable[str],
    calibration_seed: int = STAGE3_CALIBRATION_SEED,
    final_audit_seed: int = STAGE3_FINAL_AUDIT_SEED,
    case_prefix: str = "v2s3",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generate final-audit and conformal-calibration cohorts before model access."""

    if calibration_seed == final_audit_seed:
        raise ValueError("Stage3 calibration and final-audit Sobol seeds must be distinct")
    if len(spec.operating_points) * STAGE3_SAMPLES_PER_OPERATING_POINT != 6:
        raise ValueError("Stage3 fallback requires exactly two operating points and three samples per point")
    excluded = {str(value).strip() for value in excluded_design_hashes if str(value).strip()}

    audit_rows = generate_foundation_rows(
        spec,
        geometry_count=STAGE3_FINAL_AUDIT_GEOMETRIES,
        samples_per_operating_point=STAGE3_SAMPLES_PER_OPERATING_POINT,
        repeat_count=0,
        seed=final_audit_seed,
        quality_profile="reference_ultra",
        case_prefix=f"{case_prefix}_final_audit",
        excluded_design_hashes=excluded,
    )
    audit_hashes = list(_rows_by_design_hash(audit_rows))
    for row in audit_rows:
        row["doe_split"] = "test"

    calibration_rows = generate_foundation_rows(
        spec,
        geometry_count=STAGE3_CALIBRATION_GEOMETRIES,
        samples_per_operating_point=STAGE3_SAMPLES_PER_OPERATING_POINT,
        repeat_count=0,
        seed=calibration_seed,
        quality_profile="reference_ultra",
        case_prefix=f"{case_prefix}_calibration",
        excluded_design_hashes=excluded | set(audit_hashes),
    )
    calibration_hashes = list(_rows_by_design_hash(calibration_rows))
    for row in calibration_rows:
        row["doe_split"] = "calibration"

    audit_contract: dict[str, Any] = {
        "design_hashes": audit_hashes,
        "excluded_design_hashes_sha256": _canonical_sha256(sorted(excluded)),
        "generated_before_adaptive_evidence": True,
        "generated_before_adaptation": True,
        "geometry_count": STAGE3_FINAL_AUDIT_GEOMETRIES,
        "model_independent": True,
        "residual_independent": True,
        "seed": final_audit_seed,
    }
    audit_contract["contract_sha256"] = _canonical_sha256(audit_contract)
    calibration_contract: dict[str, Any] = {
        "design_hashes": calibration_hashes,
        "excluded_design_hashes_sha256": _canonical_sha256(sorted(excluded | set(audit_hashes))),
        "generated_before_adaptive_evidence": True,
        "geometry_count": STAGE3_CALIBRATION_GEOMETRIES,
        "model_independent": True,
        "residual_independent": True,
        "seed": calibration_seed,
    }
    calibration_contract["contract_sha256"] = _canonical_sha256(calibration_contract)
    return [*calibration_rows, *audit_rows], {
        "generation_order": ["final_audit", "calibration", "adaptation"],
        "calibration": calibration_contract,
        "final_audit": audit_contract,
    }


def _rename_stage3_train_rows(
    grouped_rows: Sequence[tuple[str, list[dict[str, Any]]]],
    *,
    case_prefix: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for geometry_index, (design_hash, group_rows) in enumerate(grouped_rows, start=1):
        group_id = f"{safe_name(case_prefix)}_train_geometry_{geometry_index:04d}_{design_hash[:12]}"
        local_by_point: dict[str, int] = {}
        for source in group_rows:
            point = str(source["operating_point_id"])
            local_by_point[point] = local_by_point.get(point, 0) + 1
            row = dict(source)
            row["case_id"] = (
                f"{safe_name(case_prefix)}_train_{geometry_index:04d}_"
                f"{safe_name(point)}_{local_by_point[point]:02d}"
            )
            row["geometry_group_id"] = group_id
            row["doe_split"] = "train"
            row["repeat_of_case_id"] = ""
            rows.append(row)
    return rows


def _generate_stage3_preview_train_rows(
    spec: OptimizationSpec,
    *,
    excluded_design_hashes: Iterable[str],
    adaptation_seed: int,
    case_prefix: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    preview = generate_foundation_rows(
        spec,
        geometry_count=STAGE3_TRAIN_GEOMETRIES,
        samples_per_operating_point=STAGE3_SAMPLES_PER_OPERATING_POINT,
        repeat_count=0,
        seed=adaptation_seed,
        quality_profile="reference_ultra",
        case_prefix=f"{case_prefix}_preview_pool",
        excluded_design_hashes=excluded_design_hashes,
    )
    groups = _rows_by_design_hash(preview)
    ordered = [(design_hash, groups[design_hash]) for design_hash in sorted(groups)]
    rows = _rename_stage3_train_rows(ordered, case_prefix=case_prefix)
    return rows, {
        "design_hashes": [design_hash for design_hash, _ in ordered],
        "geometry_count": STAGE3_TRAIN_GEOMETRIES,
        "mode": "deterministic_preview",
        "seed": adaptation_seed,
        "split_groups": {"train": STAGE3_TRAIN_GEOMETRIES},
    }


def generate_stage3_fallback_rows(
    spec: OptimizationSpec,
    *,
    excluded_design_hashes: Iterable[str],
    adaptation_seed: int = STAGE3_ADAPTATION_SEED,
    calibration_seed: int = STAGE3_CALIBRATION_SEED,
    final_audit_seed: int = STAGE3_FINAL_AUDIT_SEED,
    case_prefix: str = "v2s3",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the deterministic no-decision preview used by dry-runs and tests."""

    if len({adaptation_seed, calibration_seed, final_audit_seed}) != 3:
        raise ValueError("Stage3 adaptation, calibration, and final-audit Sobol seeds must be distinct")
    excluded = {str(value).strip() for value in excluded_design_hashes if str(value).strip()}
    sealed_rows, selection = generate_stage3_sealed_rows(
        spec,
        excluded_design_hashes=excluded,
        calibration_seed=calibration_seed,
        final_audit_seed=final_audit_seed,
        case_prefix=case_prefix,
    )
    sealed_hashes = set(_rows_by_design_hash(sealed_rows))
    train_rows, adaptation = _generate_stage3_preview_train_rows(
        spec,
        excluded_design_hashes=excluded | sealed_hashes,
        adaptation_seed=adaptation_seed,
        case_prefix=case_prefix,
    )
    rows = [*train_rows, *sealed_rows]
    validate_stage3_fallback_rows(rows, excluded_design_hashes=excluded)
    selection["adaptation"] = adaptation
    return rows, selection


def validate_stage3_fallback_rows(
    rows: list[dict[str, Any]],
    *,
    excluded_design_hashes: Iterable[str],
) -> dict[str, Any]:
    grouped = _rows_by_design_hash(rows)
    excluded = {str(value).strip() for value in excluded_design_hashes if str(value).strip()}
    case_ids = [str(row.get("case_id") or "").strip() for row in rows]
    split_groups = {"train": 0, "calibration": 0, "test": 0}
    split_rows = {"train": 0, "calibration": 0, "test": 0}
    split_design_hashes = {"train": set(), "calibration": set(), "test": set()}
    failures: list[str] = []
    for design_hash, group_rows in grouped.items():
        splits = {str(row.get("doe_split") or "").strip() for row in group_rows}
        if len(splits) != 1 or next(iter(splits), "") not in split_groups:
            failures.append(f"group_split:{design_hash[:12]}")
            continue
        split = next(iter(splits))
        split_groups[split] += 1
        split_rows[split] += len(group_rows)
        split_design_hashes[split].add(design_hash)
        if len(group_rows) != 6:
            failures.append(f"group_rows:{design_hash[:12]}={len(group_rows)}")
    expected_group_splits = {"train": 20, "calibration": 10, "test": 20}
    expected_row_splits = {"train": 120, "calibration": 60, "test": 120}
    if len(rows) != 300:
        failures.append(f"rows={len(rows)}")
    if len(grouped) != 50:
        failures.append(f"groups={len(grouped)}")
    if split_groups != expected_group_splits:
        failures.append(f"split_groups={split_groups}")
    if split_rows != expected_row_splits:
        failures.append(f"split_rows={split_rows}")
    if len(case_ids) != len(set(case_ids)) or "" in case_ids:
        failures.append("case_ids_not_unique")
    if excluded & set(grouped):
        failures.append("prior_design_overlap")
    cross_split_overlap = (
        (split_design_hashes["train"] & split_design_hashes["calibration"])
        | (split_design_hashes["train"] & split_design_hashes["test"])
        | (split_design_hashes["calibration"] & split_design_hashes["test"])
    )
    if cross_split_overlap:
        failures.append("cross_split_design_overlap")
    if any(str(row.get("repeat_of_case_id") or "").strip() for row in rows):
        failures.append("repeat_rows")
    strict_values = {
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "quality_profile": "reference_ultra",
        "model_extent": MODEL_EXTENT,
        "beta_convention": BETA_CONVENTION,
    }
    for row in rows:
        if any(str(row.get(column) or "").strip() != value for column, value in strict_values.items()):
            failures.append("strict_identity")
            break
        if not _false_like(row.get("use_periodic_boundary")) or not math.isclose(
            float(row.get("symmetry_factor", math.nan)), 1.0, abs_tol=1e-12
        ):
            failures.append("strict_extent")
            break
    if failures:
        raise ValueError("invalid Stage3 fallback plan: " + "; ".join(failures))
    return {
        "rows": len(rows),
        "geometry_groups": len(grouped),
        "split_groups": split_groups,
        "split_rows": split_rows,
        "repeats": 0,
        "prior_design_overlap": 0,
        "cross_split_design_overlap": 0,
    }


def _stage3_csv_bytes(rows: list[dict[str, Any]], spec: OptimizationSpec) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames_for_rows(spec), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def validate_stage2_failed_decision(path: Path, exclusion_artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Stage2 failed decision is missing: {path}")
    try:
        decision_bytes = path.read_bytes()
        decision = _strict_json_bytes(decision_bytes, "Stage2 failed decision")
    except OSError as exc:
        raise ValueError(f"cannot read Stage2 failed decision: {exc}") from exc
    required = {
        "schema_version": STAGE2_DECISION_SCHEMA_VERSION,
        "decision": "run_stage2",
        "status": "combined_r2_failed",
        "mode": "execute",
    }
    mismatches = [key for key, value in required.items() if decision.get(key) != value]
    decision_output = str(decision.get("decision_output") or "").strip()
    if not decision_output or Path(decision_output).resolve(strict=False) != path.resolve(strict=False):
        mismatches.append("decision_output")
    combined = decision.get("combined")
    if not isinstance(combined, dict) or not (combined.get("primary_failures") or combined.get("voltage_failed")):
        mismatches.append("combined untouched-audit failure evidence")
    contract = decision.get("execution_contract")
    if not isinstance(contract, dict):
        mismatches.append("execution_contract")
        contract = {}
    expected_contract_hash = hashlib.sha256(
        json.dumps(contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")
    ).hexdigest()
    if decision.get("contract_sha256") != expected_contract_hash:
        mismatches.append("contract_sha256")
    recorded_plans: set[tuple[str, str]] = set()
    for stage in ("stage1", "stage2"):
        stage_contract = contract.get(stage)
        artifact = stage_contract.get("case_plan") if isinstance(stage_contract, dict) else None
        if isinstance(artifact, dict):
            recorded_plans.add(
                (str(Path(str(artifact.get("path") or "")).resolve(strict=False)), str(artifact.get("sha256") or ""))
            )
    supplied_plans = {
        (str(Path(item["path"]).resolve(strict=False)), str(item["sha256"]))
        for item in exclusion_artifacts
    }
    if recorded_plans != supplied_plans:
        mismatches.append("Stage1/Stage2 case-plan artifacts")
    training = contract.get("training")
    if not isinstance(training, dict) or training.get("test_evaluation_scope") != "audit_case_plan_test":
        mismatches.append("isolated Stage2 audit scope")
    audit_case_plan = training.get("audit_case_plan") if isinstance(training, dict) else None
    if not isinstance(audit_case_plan, dict):
        mismatches.append("fixed audit case-plan contract")
    if mismatches:
        raise ValueError("Stage3 write requires exact failed Stage2 evidence: " + ", ".join(mismatches))
    _, _, fixed_audit_case_plan = _verified_artifact(
        audit_case_plan,
        "fixed audit case plan",
    )

    combined_artifacts = combined.get("artifacts") if isinstance(combined, dict) else None
    required_artifacts = {"merged", "validation", "metadata", "r2"}
    if not isinstance(combined_artifacts, dict) or set(combined_artifacts) != required_artifacts:
        raise ValueError(
            "Stage3 write requires exact failed Stage2 evidence: combined artifacts must be "
            f"{sorted(required_artifacts)}"
        )
    verified: dict[str, dict[str, str]] = {}
    for name in sorted(required_artifacts):
        _, _, verified[name] = _verified_artifact(
            combined_artifacts[name],
            f"combined {name}",
        )

    stage2_value = decision.get("stage2")
    if not isinstance(stage2_value, dict):
        raise ValueError("Stage3 write requires exact failed Stage2 evidence: stage2 result")
    _, _, stage2_result = _verified_artifact(
        {"path": stage2_value.get("result"), "sha256": stage2_value.get("result_sha256")},
        "Stage2 result",
    )
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": _bytes_sha256(decision_bytes),
        "contract_sha256": expected_contract_hash,
        "fixed_audit_case_plan": fixed_audit_case_plan,
        "combined_artifacts": verified,
        "stage2_result": stage2_result,
    }


def _metadata_string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be an array")
    result = tuple(str(item).strip() for item in value)
    if not result or any(not item for item in result) or len(result) != len(set(result)):
        raise ValueError(f"{label} must contain unique nonblank strings")
    return result


def _resolve_ordered_feature_values(
    source: Mapping[str, Any],
    input_columns: Sequence[str],
) -> tuple[float, ...]:
    resolved: dict[str, float] = {}

    def optional(column: str, *aliases: str) -> float | None:
        if column in resolved:
            return resolved[column]
        candidates = (column, column.removeprefix("input_"), *aliases)
        for candidate in candidates:
            if candidate in source and source[candidate] not in (None, ""):
                resolved[column] = _finite_float(source[candidate], f"adaptive feature {column}")
                return resolved[column]
        return None

    optional("input_beta_dq_deg", "beta_deg", "input_beta_deg")
    optional("input_base_rpm", "speed_rpm", "base_rpm")
    optional("input_i_peak_a", "current_peak_a", "i_peak_a")
    optional("input_phase_resistance_ohm", "phase_resistance_ohm")
    for column in input_columns:
        optional(column)

    outer = optional("input_stator_outer_radius")
    yoke_ratio = optional("input_stator_back_yoke_thick_ratio")
    inner_ratio = optional("input_stator_inner_ratio")
    tooth_length_ratio = optional("input_stator_teeth_length_ratio")
    tooth_width_ratio = optional("input_stator_teeth_width_ratio")
    slot_num = optional("input_slot_num", "slot_num")
    rotator_gap = optional("input_rotator_gap")
    shaft_ratio = optional("input_shaft_ratio")
    if outer is not None and yoke_ratio is not None:
        resolved.setdefault("input_stator_back_yoke_thick", outer * yoke_ratio)
    if outer is not None and inner_ratio is not None:
        resolved.setdefault("input_stator_inner_radius", outer * inner_ratio)
    yoke = resolved.get("input_stator_back_yoke_thick")
    inner = resolved.get("input_stator_inner_radius")
    if outer is not None and yoke is not None and inner is not None and tooth_length_ratio is not None:
        resolved.setdefault("input_stator_teeth_length", (outer - yoke - inner) * tooth_length_ratio)
    tooth_length = resolved.get("input_stator_teeth_length")
    if (
        outer is not None
        and yoke is not None
        and tooth_length is not None
        and tooth_width_ratio is not None
        and slot_num is not None
        and slot_num > 0.0
    ):
        resolved.setdefault(
            "input_stator_teeth_width",
            (outer - yoke - tooth_length)
            * math.tan(math.radians(360.0 / slot_num) / 2.0)
            * tooth_width_ratio
            * 2.0,
        )
    if inner is not None and rotator_gap is not None:
        resolved.setdefault("input_rotor_radius", inner - rotator_gap)
    rotor = resolved.get("input_rotor_radius")
    if rotor is not None and shaft_ratio is not None:
        resolved.setdefault("input_shaft_radius", rotor * shaft_ratio)

    missing = [column for column in input_columns if column not in resolved]
    if missing:
        raise ValueError(f"adaptive features cannot derive model inputs: {missing}")
    return tuple(resolved[column] for column in input_columns)


def _load_model_members(
    metadata_path: Path,
    metadata: Mapping[str, Any],
    model_targets: Sequence[str],
) -> tuple[dict[str, tuple[Any, ...]], list[dict[str, Any]]]:
    model_paths = metadata.get("model_paths")
    if not isinstance(model_paths, Mapping):
        raise ValueError("adaptive metadata.model_paths must be an object")
    model_artifacts = metadata.get("model_artifacts")
    if not isinstance(model_artifacts, Mapping):
        raise ValueError("adaptive metadata.model_artifacts must be an object")
    ensemble_size = int(_finite_float(metadata.get("ensemble_size"), "adaptive metadata.ensemble_size"))
    if ensemble_size < 2:
        raise ValueError("adaptive Stage3 requires an ensemble_size of at least 2")
    models: dict[str, tuple[Any, ...]] = {}
    proofs: list[dict[str, Any]] = []
    used_paths: set[Path] = set()
    for target in model_targets:
        raw_paths = model_paths.get(target)
        if isinstance(raw_paths, str) and raw_paths.strip():
            recorded_paths = (raw_paths,)
        elif isinstance(raw_paths, Sequence) and not isinstance(raw_paths, (str, bytes)):
            recorded_paths = tuple(str(item).strip() for item in raw_paths)
        else:
            raise ValueError(f"adaptive metadata.model_paths is missing target {target!r}")
        if len(recorded_paths) != 1:
            raise ValueError(f"adaptive target {target!r} must bind exactly one ensemble artifact")
        members: list[Any] = []
        target_artifacts: list[dict[str, str]] = []
        for recorded in recorded_paths:
            if not recorded:
                raise ValueError(f"adaptive model path for {target!r} is blank")
            artifact = (metadata_path.parent / Path(recorded).name).resolve(strict=False)
            if Path(recorded).resolve(strict=False) != artifact:
                raise ValueError(f"adaptive model path escapes the hash-validated model directory: {recorded}")
            if artifact in used_paths:
                raise ValueError(f"adaptive model artifacts are not unique: {artifact}")
            used_paths.add(artifact)
            try:
                payload = artifact.read_bytes()
            except OSError as exc:
                raise ValueError(f"cannot read adaptive model artifact {artifact}: {exc}") from exc
            artifact_contract = model_artifacts.get(target)
            if not isinstance(artifact_contract, Mapping):
                raise ValueError(f"adaptive metadata.model_artifacts is missing target {target!r}")
            contract_name = Path(str(artifact_contract.get("path") or "")).name
            contract_path = Path(str(artifact_contract.get("path") or "")).resolve(strict=False)
            contract_hash = str(artifact_contract.get("sha256") or "").strip().lower()
            contract_members = artifact_contract.get("ensemble_members")
            if contract_name != artifact.name or contract_path != artifact or contract_hash != _bytes_sha256(payload):
                raise ValueError(f"adaptive model artifact contract mismatch for target {target!r}")
            if contract_members != ensemble_size:
                raise ValueError(f"adaptive model artifact ensemble contract mismatch for target {target!r}")
            try:
                loaded = pickle.loads(payload)
            except Exception as exc:
                raise ValueError(f"cannot load adaptive model artifact {artifact}: {exc}") from exc
            artifact_members = tuple(loaded) if isinstance(loaded, (list, tuple)) else (loaded,)
            if not artifact_members:
                raise ValueError(f"adaptive model artifact is empty: {artifact}")
            members.extend(artifact_members)
            target_artifacts.append(
                {
                    "path": str(artifact),
                    "recorded_path": str(artifact_contract.get("path")),
                    "sha256": _bytes_sha256(payload),
                }
            )
        if len(members) != ensemble_size:
            raise ValueError(
                f"adaptive model {target!r} has {len(members)} ensemble members; expected {ensemble_size}"
            )
        models[target] = tuple(members)
        proofs.append(
            {
                "artifacts": target_artifacts,
                "ensemble_members": len(members),
                "target": target,
            }
        )
    return models, proofs


def _verified_metadata_artifacts(
    metadata_path: Path,
    metadata: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    raw = metadata.get("training_artifacts")
    required = {"metrics", "auxiliary_metrics"}
    allowed = required | {"tuning_trials"}
    if not isinstance(raw, Mapping) or not required <= set(raw) or not set(raw) <= allowed:
        raise ValueError("adaptive metadata.training_artifacts is incomplete")
    proofs: dict[str, dict[str, str]] = {}
    path_fields = {
        "metrics": "metrics_path",
        "auxiliary_metrics": "auxiliary_metrics_path",
        "tuning_trials": "tuning_trials_path",
    }
    for name, record in raw.items():
        if not isinstance(record, Mapping):
            raise ValueError(f"adaptive metadata.training_artifacts.{name} must be an object")
        recorded_path = str(record.get("path") or "").strip()
        recorded_hash = str(record.get("sha256") or "").strip().lower()
        artifact = (metadata_path.parent / Path(recorded_path).name).resolve(strict=False)
        if Path(recorded_path).resolve(strict=False) != artifact:
            raise ValueError(f"adaptive training artifact path escapes model directory: {recorded_path}")
        if Path(str(metadata.get(path_fields[str(name)]) or "")).resolve(strict=False) != artifact:
            raise ValueError(f"adaptive training artifact path disagrees for {name!r}")
        try:
            payload = artifact.read_bytes()
        except OSError as exc:
            raise ValueError(f"adaptive training artifact is missing: {artifact}: {exc}") from exc
        if not recorded_path or _bytes_sha256(payload) != recorded_hash:
            raise ValueError(f"adaptive training artifact contract mismatch for {name!r}")
        proofs[str(name)] = {
            "path": str(artifact),
            "recorded_path": recorded_path,
            "sha256": recorded_hash,
        }
    return proofs


def _prediction_members_by_target(
    models: Mapping[str, Sequence[Any]],
    matrix: Sequence[Sequence[float]],
) -> dict[str, list[list[float]]]:
    predictions: dict[str, list[list[float]]] = {}
    feature_rows = [list(row) for row in matrix]
    for target, members in models.items():
        try:
            raw_members = trainer.predict_model_members(tuple(members), feature_rows)
        except Exception as exc:
            raise ValueError(f"adaptive model prediction failed for {target}: {exc}") from exc
        if any(len(values) != len(feature_rows) for values in raw_members):
            raise ValueError(f"adaptive model prediction for {target} returned an invalid row count")
        predictions[target] = [
            [
                _finite_float(value, f"adaptive model prediction {target}/member-{index + 1}")
                for value in values
            ]
            for index, values in enumerate(raw_members)
        ]
    return predictions


def _target_member_signal(
    target: str,
    row_index: int,
    direct_predictions: Mapping[str, Sequence[Sequence[float]]],
    output_name_map: Mapping[str, str],
    input_columns: Sequence[str],
    features: Sequence[float],
) -> tuple[list[float], float]:
    if target not in trainer.V2_DERIVED_OUTPUT_COLUMNS:
        model_target = str(output_name_map.get(target) or target)
        if model_target not in direct_predictions:
            raise ValueError(f"adaptive predictions are missing target {target!r}")
        return [member[row_index] for member in direct_predictions[model_target]], 0.0

    required = (
        "output_torque_last_avg_nm",
        "output_coreloss_last_avg_w",
        "output_solidloss_last_avg_w",
    )
    mapped = {name: str(output_name_map.get(name) or name) for name in required}
    if any(mapped[name] not in direct_predictions for name in required):
        raise ValueError(f"adaptive derived prediction {target!r} is missing primitive models")
    feature_by_name = dict(zip(input_columns, features))
    try:
        member_counts = {len(direct_predictions[mapped[name]]) for name in required}
        if len(member_counts) != 1 or not member_counts:
            raise ValueError(f"adaptive derived prediction {target!r} has inconsistent ensembles")
        member_count = next(iter(member_counts))
        if member_count <= 0:
            raise ValueError(f"adaptive derived prediction {target!r} has an empty ensemble")
        values: list[float] = []
        invalid_count = 0
        for member_index in range(member_count):
            derived = trainer.derive_v2_outputs(
                torque_avg_nm=direct_predictions[mapped[required[0]]][member_index][row_index],
                core_loss_w=direct_predictions[mapped[required[1]]][member_index][row_index],
                solid_loss_w=direct_predictions[mapped[required[2]]][member_index][row_index],
                i_peak_a=feature_by_name["input_i_peak_a"],
                phase_resistance_ohm=feature_by_name["input_phase_resistance_ohm"],
                rpm=feature_by_name["input_base_rpm"],
            )
            value = trainer.finite_float(derived[target])
            physically_valid = math.isfinite(value) and (
                (target == "output_total_loss_last_avg_w" and value >= 0.0)
                or (target == "output_efficiency_last_pct" and 0.0 <= value <= 100.0)
            )
            if physically_valid:
                values.append(value)
            else:
                invalid_count += 1
        return values, invalid_count / member_count
    except KeyError as exc:
        raise ValueError(f"adaptive derived prediction lacks required feature {exc}") from exc


def _actual_target_value(
    target: str,
    row: Mapping[str, Any],
    output_name_map: Mapping[str, str],
    input_columns: Sequence[str],
    features: Sequence[float],
) -> float:
    if target not in trainer.V2_DERIVED_OUTPUT_COLUMNS:
        actual_column = str(output_name_map.get(target) or target)
        return _finite_float(row.get(actual_column), f"untouched-audit actual {target}")
    feature_by_name = dict(zip(input_columns, features))
    primitives = {
        name: str(output_name_map.get(name) or name)
        for name in (
            "output_torque_last_avg_nm",
            "output_coreloss_last_avg_w",
            "output_solidloss_last_avg_w",
        )
    }
    try:
        derived = trainer.derive_v2_outputs(
            torque_avg_nm=row.get(primitives["output_torque_last_avg_nm"]),
            core_loss_w=row.get(primitives["output_coreloss_last_avg_w"]),
            solid_loss_w=row.get(primitives["output_solidloss_last_avg_w"]),
            i_peak_a=feature_by_name["input_i_peak_a"],
            phase_resistance_ohm=feature_by_name["input_phase_resistance_ohm"],
            rpm=feature_by_name["input_base_rpm"],
        )
    except KeyError as exc:
        raise ValueError(f"untouched-audit derived target lacks required feature {exc}") from exc
    return _finite_float(derived[target], f"untouched-audit actual {target}")


def _target_point_value(
    target: str,
    row_index: int,
    direct_predictions: Mapping[str, Sequence[Sequence[float]]],
    output_name_map: Mapping[str, str],
    input_columns: Sequence[str],
    features: Sequence[float],
) -> float:
    if target not in trainer.V2_DERIVED_OUTPUT_COLUMNS:
        model_target = str(output_name_map.get(target) or target)
        return _mean([member[row_index] for member in direct_predictions[model_target]])
    mapped = {
        name: str(output_name_map.get(name) or name)
        for name in (
            "output_torque_last_avg_nm",
            "output_coreloss_last_avg_w",
            "output_solidloss_last_avg_w",
        )
    }
    feature_by_name = dict(zip(input_columns, features))
    derived = trainer.derive_v2_outputs(
        torque_avg_nm=_mean(
            [member[row_index] for member in direct_predictions[mapped["output_torque_last_avg_nm"]]]
        ),
        core_loss_w=_mean(
            [member[row_index] for member in direct_predictions[mapped["output_coreloss_last_avg_w"]]]
        ),
        solid_loss_w=_mean(
            [member[row_index] for member in direct_predictions[mapped["output_solidloss_last_avg_w"]]]
        ),
        i_peak_a=feature_by_name["input_i_peak_a"],
        phase_resistance_ohm=feature_by_name["input_phase_resistance_ohm"],
        rpm=feature_by_name["input_base_rpm"],
    )
    return _finite_float(derived[target], f"adaptive point prediction {target}")


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("adaptive signal cannot average an empty sequence")
    return sum(values) / len(values)


def _population_std(values: Sequence[float]) -> float:
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / len(values))


def _p90_absolute(values: Sequence[float]) -> float:
    ordered = sorted(abs(value) for value in values)
    if not ordered:
        raise ValueError("adaptive target scale requires audit values")
    return ordered[max(0, math.ceil(0.90 * len(ordered)) - 1)]


def _normalized_features(
    values: Sequence[float],
    bounds: Sequence[tuple[float, float]],
) -> tuple[float, ...]:
    normalized: list[float] = []
    for value, (lower, upper) in zip(values, bounds):
        span = upper - lower
        if span > 1e-15:
            normalized.append((value - lower) / span)
        else:
            normalized.append(0.0 if math.isclose(value, lower, abs_tol=1e-12) else value - lower)
    return tuple(normalized)


def _feature_distance(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("adaptive distance vectors must have equal nonzero dimensions")
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)) / len(left))


def _domain_distance(values: Sequence[float]) -> float:
    outside = [max(0.0, -value, value - 1.0) for value in values]
    return math.sqrt(sum(value * value for value in outside) / len(outside)) if outside else 0.0


def _project_audit_residual(
    candidate: Sequence[float],
    audit_features: Sequence[Sequence[float]],
    audit_residuals: Sequence[float],
) -> float:
    nearest = sorted(
        ((_feature_distance(candidate, feature), index) for index, feature in enumerate(audit_features)),
        key=lambda item: (item[0], item[1]),
    )[: min(STAGE3_NEAREST_AUDIT_ROWS, len(audit_features))]
    exact = [audit_residuals[index] for distance, index in nearest if distance <= 1e-12]
    if exact:
        return _mean(exact)
    weighted = [(1.0 / (distance + 1e-12), audit_residuals[index]) for distance, index in nearest]
    return sum(weight * value for weight, value in weighted) / sum(weight for weight, _ in weighted)


def load_stage3_adaptive_evidence(
    decision_path: Path,
    exclusion_artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Load the failed untouched audit and failed ensemble without accepting an optimizer bundle."""

    decision_proof = validate_stage2_failed_decision(decision_path, exclusion_artifacts)
    decision_bytes = decision_path.read_bytes()
    if _bytes_sha256(decision_bytes) != decision_proof["sha256"]:
        raise ValueError("Stage2 failed decision changed during adaptive evidence validation")
    decision = _strict_json_bytes(decision_bytes, "Stage2 failed decision")
    contract = decision["execution_contract"]
    stage2_plan_record = contract["training"]["audit_case_plan"]
    stage2_plan_path, _, stage2_plan_proof = _verified_artifact(
        stage2_plan_record,
        "Stage2 untouched-audit case plan",
    )

    combined_proofs = decision_proof["combined_artifacts"]
    merged_path, merged_bytes, _ = _verified_artifact(
        decision["combined"]["artifacts"]["merged"],
        "combined merged",
    )
    metadata_path, metadata_bytes, _ = _verified_artifact(
        decision["combined"]["artifacts"]["metadata"],
        "combined metadata",
    )
    _, merged_rows = _csv_rows_from_bytes(merged_bytes, "combined merged")
    metadata = _strict_json_bytes(metadata_bytes, "combined metadata")
    if metadata.get("artifact_contract_schema_version") != TRAINING_ARTIFACT_CONTRACT_SCHEMA_VERSION:
        raise ValueError(
            "combined metadata lacks the authoritative adaptive artifact contract "
            f"{TRAINING_ARTIFACT_CONTRACT_SCHEMA_VERSION!r}"
        )
    combined_contract = contract.get("combined")
    training_contract = contract.get("training")
    if not isinstance(combined_contract, Mapping) or not isinstance(training_contract, Mapping):
        raise ValueError("Stage2 decision lacks combined/training execution contracts")
    try:
        gate = stage2_continuation.evaluate_gate(
            Path(combined_proofs["validation"]["path"]),
            metadata_path,
            Path(combined_proofs["r2"]["path"]),
            expected_rows=int(combined_contract["expected_rows"]),
            expected_groups=int(combined_contract["expected_groups"]),
            expected_repeats=int(combined_contract["expected_repeats"]),
            threshold=_finite_float(training_contract.get("r2_threshold"), "training.r2_threshold"),
            expected_ensemble_size=int(training_contract["ensemble_size"]),
            expected_conformal_coverage=_finite_float(
                training_contract.get("conformal_coverage"),
                "training.conformal_coverage",
            ),
            expected_audit_case_plan=stage2_plan_path,
        )
    except Exception as exc:
        raise ValueError(f"combined failed gate cannot be revalidated: {exc}") from exc
    if gate.passed:
        raise ValueError("combined gate now passes; Stage3 adaptive fallback is forbidden")
    combined_summary = decision["combined"]
    if list(gate.primary_failures) != list(combined_summary.get("primary_failures") or []):
        raise ValueError("combined primary failure map disagrees with revalidated gate")
    if gate.voltage_failed is not combined_summary.get("voltage_failed"):
        raise ValueError("combined voltage failure flag disagrees with revalidated gate")
    recorded_gate_r2 = combined_summary.get("primary_test_r2")
    if not isinstance(recorded_gate_r2, Mapping) or set(recorded_gate_r2) != set(gate.primary_test_r2):
        raise ValueError("combined primary R2 map disagrees with revalidated gate")
    if any(
        not math.isclose(
            _finite_float(recorded_gate_r2[target], f"combined.primary_test_r2.{target}"),
            value,
            rel_tol=1e-12,
            abs_tol=1e-15,
        )
        for target, value in gate.primary_test_r2.items()
    ):
        raise ValueError("combined primary R2 values disagree with revalidated gate")
    if not math.isclose(
        _finite_float(combined_summary.get("voltage_test_r2"), "combined.voltage_test_r2"),
        gate.voltage_test_r2,
        rel_tol=1e-12,
        abs_tol=1e-15,
    ):
        raise ValueError("combined voltage R2 disagrees with revalidated gate")
    for name, record in decision["combined"]["artifacts"].items():
        _verified_artifact(record, f"combined {name} post-gate")
    _verified_artifact(stage2_plan_record, "Stage2 untouched-audit case plan post-gate")
    training_artifact_proofs = _verified_metadata_artifacts(metadata_path, metadata)
    merged_by_case: dict[str, dict[str, str]] = {}
    for row in merged_rows:
        case_id = str(row.get("case_id") or "").strip()
        if not case_id or case_id in merged_by_case:
            raise ValueError("combined merged has blank or duplicate case_id")
        merged_by_case[case_id] = row
    expected_case_ids: list[str] = []
    for index, artifact in enumerate(exclusion_artifacts, start=1):
        _, plan_bytes, _ = _verified_artifact(artifact, f"Stage{index} full case plan")
        _, full_plan_rows = _csv_rows_from_bytes(plan_bytes, f"Stage{index} full case plan")
        current_ids = [str(row.get("case_id") or "").strip() for row in full_plan_rows]
        if "" in current_ids or len(current_ids) != len(set(current_ids)):
            raise ValueError(f"Stage{index} full case plan has blank or duplicate case_id")
        expected_case_ids.extend(current_ids)
    if list(merged_by_case) != expected_case_ids:
        raise ValueError("combined merged does not exactly cover Stage1+Stage2 case plans in order")
    geometry_column = str(metadata.get("geometry_group_column") or "").strip()
    if geometry_column not in trainer.V2_GEOMETRY_ID_COLUMNS:
        raise ValueError("combined metadata.geometry_group_column is invalid")
    try:
        plan_rows, expected_test_contract = trainer.load_v2_audit_case_plan(
            stage2_plan_path,
            geometry_column=geometry_column,
        )
        test_plan_rows = [
            row
            for row in plan_rows
            if str(row.get("doe_split") or "").strip().lower() == "test"
        ]
        test_case_ids, test_groups = trainer.validate_v2_audit_records(
            test_plan_rows,
            merged_rows,
            geometry_column=geometry_column,
        )
    except ValueError as exc:
        raise ValueError(f"Stage2 untouched-audit cohort is invalid: {exc}") from exc
    audit_rows: list[dict[str, str]] = []
    for plan_row in test_plan_rows:
        case_id = str(plan_row["case_id"]).strip()
        result = merged_by_case[case_id]
        identities = ("design_hash", "geometry_group_id", "doe_split")
        if any(str(result.get(name) or "").strip() != str(plan_row.get(name) or "").strip() for name in identities):
            raise ValueError(f"untouched-audit identity mismatch for case_id={case_id!r}")
        if str(result.get("status") or "").strip().lower() != "ok":
            raise ValueError(f"untouched-audit result is not ok for case_id={case_id!r}")
        audit_rows.append(result)

    test_contract = metadata.get("test_evaluation")
    if not isinstance(test_contract, Mapping):
        raise ValueError("combined metadata.test_evaluation must be an object")
    test_contract_failures = [
        key
        for key, expected in expected_test_contract.items()
        if test_contract.get(key) != expected
    ]
    if test_contract_failures:
        raise ValueError(
            "combined metadata untouched-audit contract mismatch: " + ", ".join(test_contract_failures)
        )
    expected_test_ids_hash = str(expected_test_contract["test_case_ids_sha256"])

    input_columns = _metadata_string_list(metadata.get("input_columns"), "adaptive metadata.input_columns")
    raw_bounds = metadata.get("feature_bounds")
    if not isinstance(raw_bounds, Mapping) or set(input_columns) - set(raw_bounds):
        raise ValueError("adaptive metadata.feature_bounds is incomplete")
    bounds: list[tuple[float, float]] = []
    for column in input_columns:
        item = raw_bounds[column]
        if not isinstance(item, Mapping):
            raise ValueError(f"adaptive feature bound {column!r} must be an object")
        lower = _finite_float(item.get("min"), f"adaptive feature bound {column}.min")
        upper = _finite_float(item.get("max"), f"adaptive feature bound {column}.max")
        if lower > upper:
            raise ValueError(f"adaptive feature bound {column!r} has min > max")
        bounds.append((lower, upper))

    combined = decision["combined"]
    primary_failures = combined.get("primary_failures")
    if not isinstance(primary_failures, list) or any(not isinstance(item, str) for item in primary_failures):
        raise ValueError("combined.primary_failures must be an array of target names")
    signal_targets = list(dict.fromkeys(primary_failures))
    if combined.get("voltage_failed") is True:
        signal_targets.append(trainer.V2_AUXILIARY_OUTPUT_COLUMNS[0])
    allowed_targets = set(trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS) | set(trainer.V2_AUXILIARY_OUTPUT_COLUMNS)
    if not signal_targets or any(target not in allowed_targets for target in signal_targets):
        raise ValueError(f"combined failed targets are invalid: {signal_targets}")

    output_name_map_raw = metadata.get("output_name_map")
    if not isinstance(output_name_map_raw, Mapping):
        raise ValueError("adaptive metadata.output_name_map must be an object")
    output_name_map = {str(key): str(value) for key, value in output_name_map_raw.items()}
    gate_targets = (
        *trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS,
        *trainer.V2_AUXILIARY_OUTPUT_COLUMNS,
    )
    direct_canonical = {
        *trainer.V2_PRIMITIVE_OUTPUT_COLUMNS,
        *trainer.V2_AUXILIARY_OUTPUT_COLUMNS,
    }
    model_targets = tuple(sorted(str(output_name_map.get(target) or target) for target in direct_canonical))
    if not isinstance(metadata.get("model_paths"), Mapping) or set(metadata["model_paths"]) != set(model_targets):
        raise ValueError("adaptive metadata.model_paths must contain exactly seven audited model targets")
    if not isinstance(metadata.get("model_artifacts"), Mapping) or set(
        metadata["model_artifacts"]
    ) != set(model_targets):
        raise ValueError("adaptive metadata.model_artifacts must contain exactly seven audited model targets")
    models, model_proofs = _load_model_members(metadata_path, metadata, model_targets)

    audit_matrix = [_resolve_ordered_feature_values(row, input_columns) for row in audit_rows]
    direct_predictions = _prediction_members_by_target(models, audit_matrix)
    actual_by_target: dict[str, list[float]] = {target: [] for target in gate_targets}
    member_by_target: dict[str, list[list[float]]] = {target: [] for target in gate_targets}
    invalid_member_fraction_by_target: dict[str, list[float]] = {
        target: [] for target in gate_targets
    }
    point_by_target: dict[str, list[float]] = {target: [] for target in gate_targets}
    for row_index, features in enumerate(audit_matrix):
        for target in gate_targets:
            actual_by_target[target].append(
                _actual_target_value(
                    target,
                    audit_rows[row_index],
                    output_name_map,
                    input_columns,
                    features,
                )
            )
            finite_members, invalid_fraction = _target_member_signal(
                target,
                row_index,
                direct_predictions,
                output_name_map,
                input_columns,
                features,
            )
            member_by_target[target].append(finite_members)
            invalid_member_fraction_by_target[target].append(invalid_fraction)
            point_by_target[target].append(
                _target_point_value(
                    target,
                    row_index,
                    direct_predictions,
                    output_name_map,
                    input_columns,
                    features,
                )
            )
    recomputed_r2 = {
        target: trainer.regression_metrics(
            actual_by_target[target],
            point_by_target[target],
        )["R2"]
        for target in gate_targets
    }
    recorded_primary = metadata.get("primary_test_r2")
    if not isinstance(recorded_primary, Mapping):
        raise ValueError("adaptive metadata.primary_test_r2 must be an object")
    r2_mismatches: list[str] = []
    for target in trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS:
        recorded = (
            _finite_float(recorded_primary.get(target), f"metadata.primary_test_r2.{target}")
            if target in recorded_primary
            else math.nan
        )
        if not math.isfinite(recorded) or not math.isclose(
            recomputed_r2[target],
            recorded,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            r2_mismatches.append(
                f"{target}(recomputed={recomputed_r2[target]:.17g},recorded={recorded:.17g})"
            )
    voltage_target = trainer.V2_AUXILIARY_OUTPUT_COLUMNS[0]
    if not math.isclose(
        recomputed_r2[voltage_target],
        _finite_float(metadata.get("voltage_test_r2"), "metadata.voltage_test_r2"),
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        r2_mismatches.append(
            f"{voltage_target}(recomputed={recomputed_r2[voltage_target]:.17g},"
            f"recorded={_finite_float(metadata.get('voltage_test_r2'), 'metadata.voltage_test_r2'):.17g})"
        )
    if r2_mismatches:
        raise ValueError(
            "hash-validated ensemble does not reproduce untouched-audit R2: "
            + ", ".join(r2_mismatches)
        )
    threshold = _finite_float(training_contract.get("r2_threshold"), "training.r2_threshold")
    recomputed_failures = [
        target
        for target in trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS
        if recomputed_r2[target] < threshold
    ]
    recomputed_voltage_failed = recomputed_r2[voltage_target] < threshold
    if recomputed_failures != list(primary_failures) or recomputed_voltage_failed is not combined.get("voltage_failed"):
        raise ValueError("hash-validated ensemble failure map disagrees with Stage2 decision")

    signal_actual_by_target = {target: actual_by_target[target] for target in signal_targets}
    signal_member_by_target = {target: member_by_target[target] for target in signal_targets}
    signal_invalid_fraction_by_target = {
        target: invalid_member_fraction_by_target[target] for target in signal_targets
    }
    signal_point_by_target = {target: point_by_target[target] for target in signal_targets}
    target_scales = {
        target: max(_p90_absolute(values), 1e-12)
        for target, values in signal_actual_by_target.items()
    }
    audit_residuals: list[float] = []
    residuals_by_target: dict[str, list[float]] = {target: [] for target in signal_targets}
    uncertainty_by_target: dict[str, list[float]] = {target: [] for target in signal_targets}
    for row_index in range(len(audit_rows)):
        row_values: list[float] = []
        for target in signal_targets:
            residual = abs(
                signal_point_by_target[target][row_index] - signal_actual_by_target[target][row_index]
            ) / target_scales[target]
            residuals_by_target[target].append(residual)
            finite_members = signal_member_by_target[target][row_index]
            uncertainty_by_target[target].append(
                (_population_std(finite_members) / target_scales[target]) if finite_members else 0.0
            )
            row_values.append(residual)
        audit_residuals.append(max(row_values))
    normalized_audit_features = [_normalized_features(row, bounds) for row in audit_matrix]
    audit_signal_records = [
        {
            "case_id": str(row["case_id"]),
            "design_hash": str(row["design_hash"]),
            "normalized_absolute_residuals": {
                target: residuals_by_target[target][index] for target in signal_targets
            },
            "normalized_ensemble_std": {
                target: uncertainty_by_target[target][index] for target in signal_targets
            },
            "invalid_derived_prediction_fraction": {
                target: signal_invalid_fraction_by_target[target][index]
                for target in signal_targets
            },
            "residual_signal": audit_residuals[index],
        }
        for index, row in enumerate(audit_rows)
    ]
    proof = {
        "audit": {
            "case_ids_sha256": expected_test_ids_hash,
            "geometry_groups": len(test_groups),
            "recomputed_test_r2": recomputed_r2,
            "recomputed_test_r2_sha256": _canonical_sha256(recomputed_r2),
            "residual_signals_sha256": _canonical_sha256(audit_signal_records),
            "rows": len(audit_rows),
            "target_summary": {
                target: {
                    "max_normalized_absolute_residual": max(residuals_by_target[target]),
                    "max_normalized_ensemble_std": max(uncertainty_by_target[target]),
                    "mean_normalized_absolute_residual": _mean(residuals_by_target[target]),
                    "mean_normalized_ensemble_std": _mean(uncertainty_by_target[target]),
                    "max_invalid_derived_prediction_fraction": max(
                        signal_invalid_fraction_by_target[target]
                    ),
                    "mean_invalid_derived_prediction_fraction": _mean(
                        signal_invalid_fraction_by_target[target]
                    ),
                    "scale": target_scales[target],
                }
                for target in signal_targets
            },
        },
        "combined_artifacts": combined_proofs,
        "decision": decision_proof,
        "model_artifacts": model_proofs,
        "signal_targets": signal_targets,
        "stage2_audit_case_plan": stage2_plan_proof,
        "training_artifacts": training_artifact_proofs,
        "version": STAGE3_ADAPTIVE_SELECTION_VERSION,
    }
    return {
        "audit_features": normalized_audit_features,
        "audit_residuals": audit_residuals,
        "bounds": bounds,
        "input_columns": input_columns,
        "models": models,
        "output_name_map": output_name_map,
        "proof": proof,
        "signal_targets": tuple(signal_targets),
        "target_scales": target_scales,
    }


def _rank_signals(values: Mapping[str, float]) -> dict[str, float]:
    unique = sorted(set(values.values()))
    if len(unique) <= 1:
        return {key: 0.0 for key in values}
    rank = {value: index / (len(unique) - 1) for index, value in enumerate(unique)}
    return {key: rank[value] for key, value in values.items()}


def _geometry_vector(spec: OptimizationSpec, row: Mapping[str, Any]) -> tuple[float, ...]:
    return tuple(
        (_finite_float(row.get(bound.name), f"candidate geometry {bound.name}") - bound.lower)
        / (bound.upper - bound.lower)
        for bound in spec.design_space
    )


def select_stage3_adaptive_train_rows(
    spec: OptimizationSpec,
    *,
    excluded_design_hashes: Iterable[str],
    adaptive_evidence: Mapping[str, Any],
    adaptation_seed: int,
    candidate_pool_geometries: int,
    case_prefix: str,
    train_geometries: int = STAGE3_TRAIN_GEOMETRIES,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if train_geometries < 1:
        raise ValueError("Stage3 adaptive selection requires at least one train geometry")
    if candidate_pool_geometries < train_geometries:
        raise ValueError(
            f"Stage3 adaptive candidate pool requires at least {train_geometries} geometries"
        )
    pool_rows = generate_foundation_rows(
        spec,
        geometry_count=candidate_pool_geometries,
        samples_per_operating_point=STAGE3_SAMPLES_PER_OPERATING_POINT,
        repeat_count=0,
        seed=adaptation_seed,
        quality_profile="reference_ultra",
        case_prefix=f"{case_prefix}_adaptive_pool",
        excluded_design_hashes=excluded_design_hashes,
    )
    groups = _rows_by_design_hash(pool_rows)
    input_columns = tuple(adaptive_evidence["input_columns"])
    bounds = list(adaptive_evidence["bounds"])
    matrix = [_resolve_ordered_feature_values(row, input_columns) for row in pool_rows]
    normalized = [_normalized_features(row, bounds) for row in matrix]
    direct_predictions = _prediction_members_by_target(adaptive_evidence["models"], matrix)
    output_name_map = adaptive_evidence["output_name_map"]
    signal_targets = tuple(adaptive_evidence["signal_targets"])
    target_scales = adaptive_evidence["target_scales"]
    audit_features = adaptive_evidence["audit_features"]
    audit_residuals = adaptive_evidence["audit_residuals"]
    indexes_by_hash: dict[str, list[int]] = {}
    for index, row in enumerate(pool_rows):
        indexes_by_hash.setdefault(str(row["design_hash"]), []).append(index)

    raw_signals: dict[str, dict[str, float]] = {}
    for design_hash, indexes in indexes_by_hash.items():
        residual_values: list[float] = []
        uncertainty_values: list[float] = []
        invalid_derived_values: list[float] = []
        domain_values: list[float] = []
        for row_index in indexes:
            residual_values.append(
                _project_audit_residual(normalized[row_index], audit_features, audit_residuals)
            )
            target_uncertainty = []
            target_invalid_derived = []
            for target in signal_targets:
                members, invalid_fraction = _target_member_signal(
                    target,
                    row_index,
                    direct_predictions,
                    output_name_map,
                    input_columns,
                    matrix[row_index],
                )
                target_uncertainty.append(
                    (_population_std(members) / target_scales[target]) if members else 0.0
                )
                target_invalid_derived.append(invalid_fraction)
            uncertainty_values.append(max(target_uncertainty))
            invalid_derived_values.append(max(target_invalid_derived))
            domain_values.append(_domain_distance(normalized[row_index]))
        raw_signals[design_hash] = {
            "domain_distance_signal": max(domain_values),
            "invalid_derived_prediction_signal": max(invalid_derived_values),
            "residual_signal": max(residual_values),
            "uncertainty_signal": max(uncertainty_values),
        }

    residual_ranks = _rank_signals({key: value["residual_signal"] for key, value in raw_signals.items()})
    uncertainty_ranks = _rank_signals({key: value["uncertainty_signal"] for key, value in raw_signals.items()})
    invalid_derived_ranks = _rank_signals(
        {key: value["invalid_derived_prediction_signal"] for key, value in raw_signals.items()}
    )
    domain_ranks = _rank_signals({key: value["domain_distance_signal"] for key, value in raw_signals.items()})
    candidates: dict[str, dict[str, Any]] = {}
    for design_hash, signals in raw_signals.items():
        first = groups[design_hash][0]
        uncertainty_component = max(
            uncertainty_ranks[design_hash],
            invalid_derived_ranks[design_hash],
        )
        acquisition = (
            STAGE3_RESIDUAL_WEIGHT * residual_ranks[design_hash]
            + STAGE3_UNCERTAINTY_WEIGHT * uncertainty_component
            + STAGE3_DOMAIN_DISTANCE_WEIGHT * domain_ranks[design_hash]
        )
        candidates[design_hash] = {
            **signals,
            "acquisition_score": acquisition,
            "geometry_vector": _geometry_vector(spec, first),
            "uncertainty_component_rank": uncertainty_component,
        }

    selected: list[str] = []
    selected_records: list[dict[str, Any]] = []
    invalid_derived_candidates = {
        design_hash
        for design_hash, candidate in candidates.items()
        if candidate["invalid_derived_prediction_signal"] > 0.0
    }
    required_invalid_derived = min(
        STAGE3_MIN_INVALID_DERIVED_GEOMETRIES,
        len(invalid_derived_candidates),
        train_geometries,
    )
    while len(selected) < train_geometries:
        selected_invalid_derived = len(invalid_derived_candidates.intersection(selected))
        remaining_invalid_required = required_invalid_derived - selected_invalid_derived
        remaining_slots = train_geometries - len(selected)
        require_invalid_derived = remaining_invalid_required >= remaining_slots
        scored: list[tuple[float, float, str, float]] = []
        for design_hash, candidate in candidates.items():
            if design_hash in selected:
                continue
            if require_invalid_derived and design_hash not in invalid_derived_candidates:
                continue
            diversity = (
                1.0
                if not selected
                else min(
                    _feature_distance(candidate["geometry_vector"], candidates[item]["geometry_vector"])
                    for item in selected
                )
            )
            final_score = (
                (1.0 - STAGE3_DIVERSITY_WEIGHT) * candidate["acquisition_score"]
                + STAGE3_DIVERSITY_WEIGHT * diversity
            )
            scored.append((final_score, candidate["acquisition_score"], design_hash, diversity))
        if not scored:
            raise RuntimeError("Stage3 adaptive selection has no candidate satisfying coverage")
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        final_score, _, design_hash, diversity = scored[0]
        selected.append(design_hash)
        candidate = candidates[design_hash]
        selected_records.append(
            {
                "acquisition_score": candidate["acquisition_score"],
                "design_hash": design_hash,
                "diversity_score_at_selection": diversity,
                "domain_distance_signal": candidate["domain_distance_signal"],
                "final_selection_score": final_score,
                "invalid_derived_prediction_signal": candidate[
                    "invalid_derived_prediction_signal"
                ],
                "rank": len(selected),
                "residual_signal": candidate["residual_signal"],
                "selection_constraint": (
                    "invalid_derived_minimum_coverage"
                    if require_invalid_derived
                    else "adaptive_score"
                ),
                "uncertainty_component_rank": candidate["uncertainty_component_rank"],
                "uncertainty_signal": candidate["uncertainty_signal"],
            }
        )

    selected_invalid_derived_count = len(invalid_derived_candidates.intersection(selected))
    if selected_invalid_derived_count < required_invalid_derived:
        raise RuntimeError("Stage3 invalid-derived minimum coverage was not satisfied")

    ordered_groups = [(design_hash, groups[design_hash]) for design_hash in selected]
    rows = _rename_stage3_train_rows(ordered_groups, case_prefix=case_prefix)
    pool_contract = [
        {
            "design_hash": design_hash,
            "operating_controls": [
                {
                    "base_rpm": row["base_rpm"],
                    "beta_dq_deg": row["beta_dq_deg"],
                    "i_peak_a": row["i_peak_a"],
                }
                for row in groups[design_hash]
            ],
        }
        for design_hash in groups
    ]
    all_signal_records = [
        {"design_hash": design_hash, **raw_signals[design_hash]}
        for design_hash in sorted(raw_signals)
    ]
    return rows, {
        "candidate_pool": {
            "geometry_count": candidate_pool_geometries,
            "invalid_derived_prediction_geometry_count": sum(
                value["invalid_derived_prediction_signal"] > 0.0
                for value in raw_signals.values()
            ),
            "max_invalid_derived_prediction_fraction": max(
                value["invalid_derived_prediction_signal"] for value in raw_signals.values()
            ),
            "pool_sha256": _canonical_sha256(pool_contract),
            "required_invalid_derived_prediction_geometry_count": required_invalid_derived,
            "selected_invalid_derived_prediction_geometry_count": selected_invalid_derived_count,
            "signals_sha256": _canonical_sha256(all_signal_records),
        },
        "design_hashes": selected,
        "evidence": adaptive_evidence["proof"],
        "geometry_count": train_geometries,
        "mode": STAGE3_ADAPTIVE_SELECTION_VERSION,
        "scoring": {
            "diversity_weight": STAGE3_DIVERSITY_WEIGHT,
            "domain_distance_weight": STAGE3_DOMAIN_DISTANCE_WEIGHT,
            "nearest_audit_rows": STAGE3_NEAREST_AUDIT_ROWS,
            "residual_weight": STAGE3_RESIDUAL_WEIGHT,
            "invalid_derived_prediction_coverage_policy": (
                "reserve_final_slots_for_up_to_two_invalid_geometries_with_greedy_diversity"
            ),
            "invalid_derived_prediction_minimum_geometry_coverage": (
                STAGE3_MIN_INVALID_DERIVED_GEOMETRIES
            ),
            "uncertainty_component_policy": (
                "max_rank_of_finite_ensemble_std_and_invalid_derived_prediction_fraction"
            ),
            "uncertainty_weight": STAGE3_UNCERTAINTY_WEIGHT,
        },
        "seed": adaptation_seed,
        "selected": selected_records,
        "split_groups": {"train": train_geometries},
    }


def publish_stage3_pair(
    output: Path,
    manifest_output: Path,
    plan_bytes: bytes,
    manifest: dict[str, Any],
    *,
    schema_version: str = STAGE3_SCHEMA_VERSION,
) -> None:
    if output.resolve(strict=False) == manifest_output.resolve(strict=False):
        raise ValueError("Stage3 plan and manifest paths must be distinct")
    if output.is_file() and manifest_output.is_file():
        audit_existing_stage3_pair(
            output,
            manifest_output,
            plan_bytes,
            manifest,
            schema_version=schema_version,
        )
        return
    if output.exists() or manifest_output.exists():
        raise ValueError("Stage3 plan/manifest pair is partial or changed")
    for path in (output, manifest_output):
        path.parent.mkdir(parents=True, exist_ok=True)

    def stage(path: Path, payload: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        staged = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return staged

    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    staged_plan: Path | None = None
    staged_manifest: Path | None = None
    manifest_receipt: PublishReceipt | None = None
    plan_receipt: PublishReceipt | None = None
    unsafe_receipts: set[int] = set()
    try:
        staged_plan = stage(output, plan_bytes)
        staged_manifest = stage(manifest_output, manifest_bytes)
        manifest_receipt = publish_no_replace(
            staged_manifest,
            manifest_output,
            proof_path=stage3_publish_proof_path(manifest_output),
        )
        plan_receipt = publish_no_replace(
            staged_plan,
            output,
            proof_path=stage3_publish_proof_path(output),
        )
    except BaseException as exc:
        rollback_safe = True
        for receipt in (plan_receipt, manifest_receipt):
            if receipt is not None and not rollback_owned_output(receipt):
                rollback_safe = False
                unsafe_receipts.add(id(receipt))
        if not rollback_safe:
            raise RuntimeError("Stage3 pair publication failed and rollback was unsafe") from exc
        raise
    finally:
        for receipt in (plan_receipt, manifest_receipt):
            if receipt is not None and id(receipt) not in unsafe_receipts:
                cleanup_publish_receipt(receipt)
        for staged in (staged_plan, staged_manifest):
            if staged is not None:
                staged.unlink(missing_ok=True)


def stage3_publish_proof_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.publish-proof.json")


def _stage3_proof_receipt(destination: Path) -> PublishReceipt | None:
    proof = stage3_publish_proof_path(destination)
    if not proof.exists():
        return None
    try:
        raw = _strict_json_bytes(proof.read_bytes(), f"Stage3 publication proof {proof}")
        if raw.get("schema_version") != PROOF_SCHEMA_VERSION or set(raw) != {
            "schema_version",
            "source",
            "destination",
            "identity",
        }:
            raise ValueError("unexpected proof fields")
        if Path(str(raw["destination"])).absolute() != destination.absolute():
            raise ValueError("proof destination mismatch")
        identity_raw = raw["identity"]
        if not isinstance(identity_raw, Mapping):
            raise ValueError("proof identity must be an object")
        receipt = PublishReceipt(
            source=Path(str(raw["source"])),
            destination=destination.absolute(),
            identity=FileIdentity.from_mapping(identity_raw),
            strategy="complete_pair_proof_cleanup",
            proof_path=proof.absolute(),
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"invalid Stage3 publication proof: {proof}: {exc}") from exc
    if not receipt_owns_destination(receipt):
        raise RuntimeError(f"Stage3 publication proof no longer owns destination: {proof}")
    return receipt


def audit_existing_stage3_pair(
    output: Path,
    manifest_output: Path,
    plan_bytes: bytes,
    manifest: Mapping[str, Any],
    *,
    schema_version: str = STAGE3_SCHEMA_VERSION,
) -> None:
    """Accept an exact existing commit without overwriting either member."""

    try:
        existing_plan = output.read_bytes()
        existing_manifest = _strict_json_bytes(
            manifest_output.read_bytes(),
            "existing Stage3 manifest",
        )
    except OSError as exc:
        raise ValueError(f"cannot audit existing Stage3 pair: {exc}") from exc
    expected_plan_hash = _bytes_sha256(plan_bytes)
    required = (
        existing_manifest.get("schema_version") == schema_version
        and existing_manifest.get("mode") == "write"
        and Path(str(existing_manifest.get("case_plan") or "")).resolve(strict=False)
        == output.resolve(strict=False)
        and existing_manifest.get("case_plan_sha256") == expected_plan_hash
        and isinstance(existing_manifest.get("summary"), Mapping)
    )
    if not required or existing_plan != plan_bytes or existing_manifest != dict(manifest):
        raise ValueError("existing Stage3 pair does not exactly match the current invocation")

    receipts = [
        receipt
        for destination in (output, manifest_output)
        if (receipt := _stage3_proof_receipt(destination)) is not None
    ]
    for receipt in receipts:
        assert receipt.proof_path is not None
        cleanup_publish_receipt(receipt)
        if receipt.proof_path.exists():
            raise RuntimeError(
                f"cannot clean verified Stage3 publication proof: {receipt.proof_path}"
            )


def recover_stage3_pair(output: Path, manifest_output: Path) -> bool:
    """Recover only proof-owned partial publications from a killed prior writer."""

    proofs = {
        destination: stage3_publish_proof_path(destination)
        for destination in (output, manifest_output)
    }
    if output.is_file() and manifest_output.is_file():
        return False
    recovered = False
    for destination, proof in proofs.items():
        if proof.exists():
            if not recover_owned_output(proof, destination):
                raise RuntimeError(f"cannot safely recover Stage3 publication proof: {proof}")
            recovered = True
    partial = [str(path) for path in (output, manifest_output) if path.exists()]
    if partial:
        raise RuntimeError(
            "unsupported Stage3 partial publication without a valid ownership proof: "
            + ", ".join(partial)
        )
    return recovered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic grouped IPMSM v2 DOE cases.")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--geometry-count", type=int, default=160)
    parser.add_argument("--samples-per-operating-point", type=int, default=3)
    parser.add_argument("--repeat-count", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--quality-profile", default="reference_ultra")
    parser.add_argument("--case-prefix", default="v2")
    parser.add_argument("--exclude-case-plan", type=Path, action="append", default=[])
    parser.add_argument("--electrical-zero-deg", type=float, help="Optional consistency check against beta_calibration.")
    parser.add_argument("--max-cases", type=int, default=1200)
    parser.add_argument(
        "--stage3-fallback",
        action="store_true",
        help="Build the adaptive-train/independently-sealed calibration/audit Stage3 contract.",
    )
    parser.add_argument("--stage3-manifest-output", type=Path)
    parser.add_argument("--stage3-adaptation-seed", type=int, default=STAGE3_ADAPTATION_SEED)
    parser.add_argument("--stage3-calibration-seed", type=int, default=STAGE3_CALIBRATION_SEED)
    parser.add_argument("--stage3-final-audit-seed", type=int, default=STAGE3_FINAL_AUDIT_SEED)
    parser.add_argument(
        "--stage3-candidate-pool-geometries",
        type=int,
        default=STAGE3_CANDIDATE_POOL_GEOMETRIES,
    )
    parser.add_argument("--stage2-failed-decision", type=Path)
    parser.add_argument("--write-stage3", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        spec_bytes = args.spec.read_bytes()
    except OSError as exc:
        raise SystemExit(f"cannot read optimization spec {args.spec}: {exc}") from exc
    spec_sha256 = _bytes_sha256(spec_bytes)
    spec = optimization_spec_from_mapping(_strict_json_bytes(spec_bytes, "optimization spec"))
    if args.stage3_fallback:
        if args.write_stage3 and args.stage3_manifest_output is not None:
            recover_stage3_pair(args.output, args.stage3_manifest_output)
        existing_complete_pair = bool(
            args.write_stage3
            and args.stage3_manifest_output is not None
            and args.output.is_file()
            and args.stage3_manifest_output.is_file()
        )
        if args.output.exists() and not existing_complete_pair:
            raise SystemExit(f"fresh Stage3 output already exists: {args.output}")
        if args.max_cases < 300:
            raise SystemExit("Stage3 fallback requires --max-cases >= 300")
        if args.write_stage3 and args.stage3_manifest_output is None:
            raise SystemExit("--write-stage3 requires --stage3-manifest-output")
        if args.write_stage3 and args.stage2_failed_decision is None:
            raise SystemExit("--write-stage3 requires --stage2-failed-decision")
        if (
            args.stage3_manifest_output is not None
            and args.stage3_manifest_output.exists()
            and not existing_complete_pair
        ):
            raise SystemExit(f"fresh Stage3 manifest already exists: {args.stage3_manifest_output}")
        excluded, exclusion_artifacts = stage3_exclusion_contract(args.exclude_case_plan)
        source_calibration_id = str(exclusion_artifacts[0]["beta_calibration_id"])
        source_electrical_zero = float(exclusion_artifacts[0]["electrical_zero_deg"])
        if source_calibration_id != spec.beta_calibration.calibration_id or not math.isclose(
            source_electrical_zero,
            spec.beta_calibration.electrical_zero_deg,
            abs_tol=1e-12,
        ):
            raise SystemExit("Stage3 spec beta calibration does not match Stage1/Stage2 plans")
        if len(
            {
                args.stage3_adaptation_seed,
                args.stage3_calibration_seed,
                args.stage3_final_audit_seed,
            }
        ) != 3:
            raise SystemExit("Stage3 adaptation, calibration, and final-audit Sobol seeds must be distinct")
        if args.stage2_failed_decision is not None and (
            args.stage3_calibration_seed != STAGE3_CALIBRATION_SEED
            or args.stage3_final_audit_seed != STAGE3_FINAL_AUDIT_SEED
        ):
            raise SystemExit(
                "adaptive Stage3 requires the fixed sealed calibration and final-audit seeds"
            )
        decision_proof: dict[str, Any] | None = None
        if args.stage2_failed_decision is None:
            rows, selection = generate_stage3_fallback_rows(
                spec,
                excluded_design_hashes=excluded,
                adaptation_seed=args.stage3_adaptation_seed,
                calibration_seed=args.stage3_calibration_seed,
                final_audit_seed=args.stage3_final_audit_seed,
                case_prefix=args.case_prefix,
            )
        else:
            # These two cohorts are materialized before any failed-model or
            # residual artifact is read. Their bytes therefore cannot depend
            # on Stage2 model behavior.
            sealed_rows, selection = generate_stage3_sealed_rows(
                spec,
                excluded_design_hashes=excluded,
                calibration_seed=args.stage3_calibration_seed,
                final_audit_seed=args.stage3_final_audit_seed,
                case_prefix=args.case_prefix,
            )
            sealed_hashes = set(_rows_by_design_hash(sealed_rows))
            adaptive_evidence = load_stage3_adaptive_evidence(
                args.stage2_failed_decision,
                exclusion_artifacts,
            )
            train_rows, adaptation = select_stage3_adaptive_train_rows(
                spec,
                excluded_design_hashes=excluded | sealed_hashes,
                adaptive_evidence=adaptive_evidence,
                adaptation_seed=args.stage3_adaptation_seed,
                candidate_pool_geometries=args.stage3_candidate_pool_geometries,
                case_prefix=args.case_prefix,
            )
            rows = [*train_rows, *sealed_rows]
            selection["adaptation"] = adaptation
            decision_proof = adaptive_evidence["proof"]["decision"]
        summary = validate_stage3_fallback_rows(rows, excluded_design_hashes=excluded)
        plan_bytes = _stage3_csv_bytes(rows, spec)
        if _file_sha256(args.spec) != spec_sha256:
            raise RuntimeError("optimization spec changed during Stage3 generation")
        for artifact in exclusion_artifacts:
            if _file_sha256(Path(artifact["path"])) != artifact["sha256"]:
                raise RuntimeError("Stage1/Stage2 exclusion plan changed during Stage3 generation")
        manifest: dict[str, Any] = {
            "schema_version": STAGE3_SCHEMA_VERSION,
            "mode": "write" if args.write_stage3 else "dry-run",
            "case_plan": str(args.output.resolve(strict=False)),
            "case_plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
            "spec": {"path": str(args.spec.resolve(strict=False)), "sha256": spec_sha256},
            "source_case_plans": exclusion_artifacts,
            "selection": selection,
            "summary": summary,
        }
        if decision_proof is not None:
            manifest["stage2_failed_decision"] = decision_proof
        if args.write_stage3:
            assert args.stage3_manifest_output is not None
            assert args.stage2_failed_decision is not None
            publish_stage3_pair(args.output, args.stage3_manifest_output, plan_bytes, manifest)
        print(json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return 0
    if args.write_stage3 or args.stage3_manifest_output is not None or args.stage2_failed_decision is not None:
        raise SystemExit("Stage3-only arguments require --stage3-fallback")
    rows = generate_foundation_rows(
        spec,
        geometry_count=args.geometry_count,
        samples_per_operating_point=args.samples_per_operating_point,
        repeat_count=args.repeat_count,
        seed=args.seed,
        quality_profile=args.quality_profile,
        electrical_zero_deg=args.electrical_zero_deg,
        case_prefix=args.case_prefix,
        excluded_design_hashes=read_excluded_design_hashes(args.exclude_case_plan),
    )
    if len(rows) > args.max_cases:
        raise SystemExit(f"generated {len(rows)} cases, exceeding --max-cases={args.max_cases}")
    write_rows(args.output, rows, fieldnames_for_rows(spec))
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["doe_split"])] = counts.get(str(row["doe_split"]), 0) + 1
    print(
        f"generated_ipmsm_v2_cases rows={len(rows)} geometry_groups={args.geometry_count} "
        f"split_counts={counts} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
