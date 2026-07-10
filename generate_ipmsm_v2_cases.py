"""Generate deterministic, grouped IPMSM v2 foundation DOE case rows."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = load_optimization_spec(args.spec)
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
