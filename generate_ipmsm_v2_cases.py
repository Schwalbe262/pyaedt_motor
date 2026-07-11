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
import tempfile
from typing import Any, Iterable

from atomic_publish import publish_no_replace, rollback_owned_output
from generate_ipmsm_quality_cases import MESH_ELEMENT_KEYS, QUALITY_PROFILES
from ipmsm_optimization import (
    OptimizationSpec,
    current_density_a_per_mm2,
    geometry_metrics,
    load_optimization_spec,
    phase_resistance_100c_ohm,
)


DATASET_SCHEMA_VERSION = "ipmsm_v2"
BETA_CONVENTION = "dq_current_advance_v2"
MODEL_EXTENT = "full_360"
STAGE3_SCHEMA_VERSION = "ipmsm_v2_stage3_fallback_plan_v1"
STAGE2_DECISION_SCHEMA_VERSION = "ipmsm_v2_stage2_continuation_v1"
STAGE3_ADAPTATION_GEOMETRIES = 30
STAGE3_TRAIN_GEOMETRIES = 20
STAGE3_CALIBRATION_GEOMETRIES = 10
STAGE3_FINAL_AUDIT_GEOMETRIES = 20
STAGE3_SAMPLES_PER_OPERATING_POINT = 3
STAGE3_ADAPTATION_SEED = 730_031
STAGE3_FINAL_AUDIT_SEED = 730_037

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
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = list(reader.fieldnames or ())
            missing = sorted(required - set(fieldnames))
            if missing:
                raise ValueError(f"Stage3 exclusion plan {path} is missing strict columns: {missing}")
            rows = [dict(row) for row in reader]
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
                "sha256": _file_sha256(path),
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


def generate_stage3_fallback_rows(
    spec: OptimizationSpec,
    *,
    excluded_design_hashes: Iterable[str],
    adaptation_seed: int = STAGE3_ADAPTATION_SEED,
    final_audit_seed: int = STAGE3_FINAL_AUDIT_SEED,
    case_prefix: str = "v2s3",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if adaptation_seed == final_audit_seed:
        raise ValueError("Stage3 adaptation and final-audit Sobol seeds must be distinct")
    if len(spec.operating_points) * STAGE3_SAMPLES_PER_OPERATING_POINT != 6:
        raise ValueError("Stage3 fallback requires exactly two operating points and three samples per point")
    excluded = {str(value).strip() for value in excluded_design_hashes if str(value).strip()}

    # The audit stream is sealed first and depends only on the spec, prior
    # design exclusions, its own seed, and fixed audit counts.
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
    audit_groups = _rows_by_design_hash(audit_rows)
    audit_hashes = list(audit_groups)
    for row in audit_rows:
        row["doe_split"] = "test"

    adaptation_rows = generate_foundation_rows(
        spec,
        geometry_count=STAGE3_ADAPTATION_GEOMETRIES,
        samples_per_operating_point=STAGE3_SAMPLES_PER_OPERATING_POINT,
        repeat_count=0,
        seed=adaptation_seed,
        quality_profile="reference_ultra",
        case_prefix=f"{case_prefix}_adaptation",
        excluded_design_hashes=excluded | set(audit_hashes),
    )
    adaptation_groups = _rows_by_design_hash(adaptation_rows)
    ordered_adaptation_hashes = sorted(adaptation_groups)
    split_by_hash = {
        design_hash: ("train" if index < STAGE3_TRAIN_GEOMETRIES else "calibration")
        for index, design_hash in enumerate(ordered_adaptation_hashes)
    }
    for row in adaptation_rows:
        row["doe_split"] = split_by_hash[str(row["design_hash"])]

    rows = [*adaptation_rows, *audit_rows]
    validate_stage3_fallback_rows(rows, excluded_design_hashes=excluded)
    audit_contract: dict[str, Any] = {
        "design_hashes": audit_hashes,
        "excluded_design_hashes_sha256": hashlib.sha256(
            json.dumps(sorted(excluded), separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "geometry_count": STAGE3_FINAL_AUDIT_GEOMETRIES,
        "generated_before_adaptation": True,
        "residual_independent": True,
        "seed": final_audit_seed,
    }
    audit_contract["contract_sha256"] = hashlib.sha256(
        json.dumps(audit_contract, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    selection = {
        "adaptation": {
            "design_hashes": ordered_adaptation_hashes,
            "geometry_count": STAGE3_ADAPTATION_GEOMETRIES,
            "seed": adaptation_seed,
            "split_groups": {"train": STAGE3_TRAIN_GEOMETRIES, "calibration": STAGE3_CALIBRATION_GEOMETRIES},
        },
        "final_audit": audit_contract,
    }
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
    failures: list[str] = []
    for design_hash, group_rows in grouped.items():
        splits = {str(row.get("doe_split") or "").strip() for row in group_rows}
        if len(splits) != 1 or next(iter(splits), "") not in split_groups:
            failures.append(f"group_split:{design_hash[:12]}")
            continue
        split = next(iter(splits))
        split_groups[split] += 1
        split_rows[split] += len(group_rows)
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
        decision = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Stage2 failed decision: {exc}") from exc
    required = {
        "schema_version": STAGE2_DECISION_SCHEMA_VERSION,
        "decision": "run_stage2",
        "status": "combined_r2_failed",
    }
    mismatches = [key for key, value in required.items() if decision.get(key) != value]
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
    supplied_plans = {(str(Path(item["path"]).resolve(strict=False)), str(item["sha256"])) for item in exclusion_artifacts}
    if recorded_plans != supplied_plans:
        mismatches.append("Stage1/Stage2 case-plan artifacts")
    training = contract.get("training")
    if not isinstance(training, dict) or training.get("test_evaluation_scope") != "audit_case_plan_test":
        mismatches.append("isolated Stage2 audit scope")
    if mismatches:
        raise ValueError("Stage3 write requires exact failed Stage2 evidence: " + ", ".join(mismatches))
    return {"path": str(path.resolve(strict=False)), "sha256": _file_sha256(path)}


def publish_stage3_pair(
    output: Path,
    manifest_output: Path,
    plan_bytes: bytes,
    manifest: dict[str, Any],
) -> None:
    if output.resolve(strict=False) == manifest_output.resolve(strict=False):
        raise ValueError("Stage3 plan and manifest paths must be distinct")
    for path in (output, manifest_output):
        if path.exists():
            raise ValueError(f"fresh Stage3 output already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
    plan_fd, plan_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    manifest_fd, manifest_name = tempfile.mkstemp(
        prefix=f".{manifest_output.name}.", suffix=".tmp", dir=manifest_output.parent
    )
    staged_plan = Path(plan_name)
    staged_manifest = Path(manifest_name)
    plan_receipt = None
    try:
        with os.fdopen(plan_fd, "wb") as stream:
            stream.write(plan_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        with os.fdopen(manifest_fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        plan_receipt = publish_no_replace(staged_plan, output)
        publish_no_replace(staged_manifest, manifest_output)
    except Exception:
        if plan_receipt is not None:
            rollback_owned_output(plan_receipt)
        raise
    finally:
        staged_plan.unlink(missing_ok=True)
        staged_manifest.unlink(missing_ok=True)


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
        help="Build the fixed 30-adaptation/20-sealed-audit Stage3 contract; dry-run unless --write-stage3.",
    )
    parser.add_argument("--stage3-manifest-output", type=Path)
    parser.add_argument("--stage3-adaptation-seed", type=int, default=STAGE3_ADAPTATION_SEED)
    parser.add_argument("--stage3-final-audit-seed", type=int, default=STAGE3_FINAL_AUDIT_SEED)
    parser.add_argument("--stage2-failed-decision", type=Path)
    parser.add_argument("--write-stage3", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = load_optimization_spec(args.spec)
    if args.stage3_fallback:
        if args.output.exists():
            raise SystemExit(f"fresh Stage3 output already exists: {args.output}")
        if args.max_cases < 300:
            raise SystemExit("Stage3 fallback requires --max-cases >= 300")
        if args.write_stage3 and args.stage3_manifest_output is None:
            raise SystemExit("--write-stage3 requires --stage3-manifest-output")
        if args.write_stage3 and args.stage2_failed_decision is None:
            raise SystemExit("--write-stage3 requires --stage2-failed-decision")
        if args.stage3_manifest_output is not None and args.stage3_manifest_output.exists():
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
        rows, selection = generate_stage3_fallback_rows(
            spec,
            excluded_design_hashes=excluded,
            adaptation_seed=args.stage3_adaptation_seed,
            final_audit_seed=args.stage3_final_audit_seed,
            case_prefix=args.case_prefix,
        )
        summary = validate_stage3_fallback_rows(rows, excluded_design_hashes=excluded)
        plan_bytes = _stage3_csv_bytes(rows, spec)
        manifest: dict[str, Any] = {
            "schema_version": STAGE3_SCHEMA_VERSION,
            "mode": "write" if args.write_stage3 else "dry-run",
            "case_plan": str(args.output.resolve(strict=False)),
            "case_plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
            "spec": {"path": str(args.spec.resolve(strict=False)), "sha256": _file_sha256(args.spec)},
            "source_case_plans": exclusion_artifacts,
            "selection": selection,
            "summary": summary,
        }
        if args.stage2_failed_decision is not None:
            manifest["stage2_failed_decision"] = validate_stage2_failed_decision(
                args.stage2_failed_decision,
                exclusion_artifacts,
            )
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
