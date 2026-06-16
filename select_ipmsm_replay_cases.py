"""Select deterministic fixed-geometry replay cases from IPMSM result CSVs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
from pathlib import Path
from typing import Any, Iterable

from generate_ipmsm_quality_cases import MESH_ELEMENT_KEYS, QualityProfile, parse_profiles
import run_ipmsm_batch


DEFAULT_REQUIRED_OUTPUTS = (
    "output_torque_all_avg_nm",
    "output_coreloss_all_avg_w",
    "output_solidloss_all_avg_w",
)
EFFICIENCY_COLUMNS = ("output_efficiency_all_pct", "output_efficiency_last_pct", "output_efficiency_last_pc")
DEFAULT_SELECTION_FEATURES = (
    "stator_outer_radius",
    "stator_back_yoke_thick_ratio",
    "stator_inner_ratio",
    "stator_teeth_length_ratio",
    "stator_teeth_width_ratio",
    "stator_gap",
    "rotator_gap",
    "shaft_ratio",
    "magnet_shield_thick",
    "magnet_setback_ratio",
    "magnet_thick_ratio",
    "magnet_height_ratio",
    "output_torque_all_avg_nm",
    "output_coreloss_all_avg_w",
    "output_solidloss_all_avg_w",
)
GEOMETRY_OUTPUT_KEYS = (
    "slot_num",
    "pole_num",
    "stator_outer_radius",
    "stator_back_yoke_thick_ratio",
    "stator_inner_ratio",
    "stator_shoe_thick",
    "stator_teeth_length_ratio",
    "stator_teeth_width_ratio",
    "stator_gap",
    "slot_opening_ratio",
    "rotator_gap",
    "shaft_ratio",
    "magnet_shield_thick",
    "magnet_setback_ratio",
    "magnet_thick_ratio",
    "magnet_space_height_ratio",
    "magnet_height_ratio",
)
FIELDNAMES = (
    "case_id",
    "source_case_id",
    "source_result_path",
    "quality_profile",
    "base_rpm",
    "i_peak_a",
    "beta_deg",
    "operation",
    "use_periodic_boundary",
    "transient_periods",
    "steps_per_period",
    *(f"mesh_{key}_elements" for key in MESH_ELEMENT_KEYS),
    *GEOMETRY_OUTPUT_KEYS,
)


def finite_float(value: object) -> float:
    try:
        number = float(str(value).strip())
    except Exception:
        return math.nan
    return number if math.isfinite(number) else math.nan


def safe_name_part(value: object) -> str:
    text = str(value).strip().replace(".", "p").replace("-", "m")
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text)


def case_value(row: dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return default


def parse_required_outputs(text: str) -> tuple[str, ...]:
    outputs = tuple(part.strip() for part in text.split(",") if part.strip())
    if not outputs:
        raise ValueError("at least one required output column is required")
    return outputs


def parse_selection_features(text: str) -> tuple[str, ...]:
    features = tuple(part.strip() for part in text.split(",") if part.strip())
    if not features:
        raise ValueError("at least one selection feature is required")
    return features


def row_has_required_outputs(row: dict[str, str], required_outputs: Iterable[str]) -> bool:
    return all(math.isfinite(finite_float(row.get(column, ""))) for column in required_outputs)


def physical_sanity_violations(row: dict[str, str]) -> list[str]:
    violations = []
    for column in EFFICIENCY_COLUMNS:
        if column not in row:
            continue
        value = finite_float(row.get(column, ""))
        if math.isfinite(value) and not 0.0 <= value <= 100.0:
            violations.append(column)
    return violations


def candidate_sort_key(row: dict[str, Any], seed: int) -> str:
    source_case_id = row.get("source_case_id", "")
    source_result_path = row.get("source_result_path", "")
    text = f"{seed}|{source_result_path}|{source_case_id}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_candidates(
    paths: Iterable[Path],
    required_outputs: tuple[str, ...],
    status: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    candidates: list[dict[str, Any]] = []
    metrics = {
        "rows": 0,
        "status_rejected": 0,
        "required_output_rejected": 0,
        "physical_sanity_rejected": 0,
        "geometry_rejected": 0,
    }
    for path in paths:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                metrics["rows"] += 1
                if status and row.get("status", "").strip().lower() != status.lower():
                    metrics["status_rejected"] += 1
                    continue
                if not row_has_required_outputs(row, required_outputs):
                    metrics["required_output_rejected"] += 1
                    continue
                if physical_sanity_violations(row):
                    metrics["physical_sanity_rejected"] += 1
                    continue
                try:
                    geometry = run_ipmsm_batch.extract_fixed_geometry(row)
                except ValueError:
                    metrics["geometry_rejected"] += 1
                    continue
                if not geometry:
                    metrics["geometry_rejected"] += 1
                    continue
                candidates.append(
                    {
                        "source_case_id": row.get("case_id", ""),
                        "source_result_path": str(path),
                        "base_rpm": finite_float(case_value(row, "input_base_rpm", "base_rpm", "rpm", default="1200")),
                        "i_peak_a": finite_float(case_value(row, "input_i_peak_a", "i_peak_a", "i_peak", default="137.8")),
                        "beta_deg": finite_float(case_value(row, "input_beta_deg", "beta_deg", "beta", default="30")),
                        "operation": case_value(row, "input_operation", "operation", default="sin_current"),
                        "use_periodic_boundary": case_value(row, "input_use_periodic_boundary", "use_periodic_boundary", default=""),
                        "geometry": geometry,
                        "source_outputs": {
                            column: finite_float(row.get(column, ""))
                            for column in required_outputs
                        },
                    }
                )
    return candidates, metrics


def candidate_feature(candidate: dict[str, Any], feature: str) -> float:
    if feature in candidate:
        return finite_float(candidate[feature])
    if feature in candidate.get("geometry", {}):
        return finite_float(candidate["geometry"][feature])
    if feature in candidate.get("source_outputs", {}):
        return finite_float(candidate["source_outputs"][feature])
    return math.nan


def normalization_ranges(candidates: list[dict[str, Any]], features: tuple[str, ...]) -> list[tuple[str, float, float]]:
    ranges: list[tuple[str, float, float]] = []
    for feature in features:
        values = [candidate_feature(candidate, feature) for candidate in candidates]
        finite_values = [value for value in values if math.isfinite(value)]
        if not finite_values:
            continue
        minimum = min(finite_values)
        maximum = max(finite_values)
        if maximum > minimum:
            ranges.append((feature, minimum, maximum))
    return ranges


def normalized_vector(candidate: dict[str, Any], ranges: list[tuple[str, float, float]]) -> tuple[float, ...]:
    values: list[float] = []
    for feature, minimum, maximum in ranges:
        value = candidate_feature(candidate, feature)
        if not math.isfinite(value):
            values.append(0.5)
        else:
            values.append((value - minimum) / (maximum - minimum))
    return tuple(values)


def squared_distance(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    return sum((a - b) ** 2 for a, b in zip(first, second))


def select_hash_candidates(candidates: list[dict[str, Any]], count: int, seed: int) -> list[dict[str, Any]]:
    ordered = sorted(candidates, key=lambda row: candidate_sort_key(row, seed))
    return ordered[:count]


def select_spread_candidates(
    candidates: list[dict[str, Any]],
    count: int,
    seed: int,
    features: tuple[str, ...],
) -> list[dict[str, Any]]:
    ranges = normalization_ranges(candidates, features)
    if not ranges:
        return select_hash_candidates(candidates, count, seed)

    ordered = sorted(candidates, key=lambda row: candidate_sort_key(row, seed))
    vectors = [normalized_vector(candidate, ranges) for candidate in ordered]
    origin = tuple(0.0 for _ in ranges)
    selected_indices: list[int] = []
    selected = set()
    nearest_selected_distance = [math.inf for _ in ordered]

    first_index = min(
        range(len(ordered)),
        key=lambda index: (squared_distance(vectors[index], origin), candidate_sort_key(ordered[index], seed)),
    )
    selected_indices.append(first_index)
    selected.add(first_index)

    while len(selected_indices) < count:
        last_vector = vectors[selected_indices[-1]]
        for index, vector in enumerate(vectors):
            if index in selected:
                continue
            nearest_selected_distance[index] = min(nearest_selected_distance[index], squared_distance(vector, last_vector))

        next_index = max(
            (index for index in range(len(ordered)) if index not in selected),
            key=lambda index: (nearest_selected_distance[index], candidate_sort_key(ordered[index], seed)),
        )
        selected_indices.append(next_index)
        selected.add(next_index)

    return [ordered[index] for index in selected_indices]


def select_candidates(
    candidates: list[dict[str, Any]],
    count: int,
    seed: int,
    mode: str = "spread",
    features: tuple[str, ...] = DEFAULT_SELECTION_FEATURES,
) -> list[dict[str, Any]]:
    if count < 1:
        raise ValueError("source case count must be at least 1")
    if not candidates:
        raise ValueError("at least one candidate is required")
    if count >= len(candidates):
        return select_hash_candidates(candidates, len(candidates), seed)
    if mode == "hash":
        return select_hash_candidates(candidates, count, seed)
    if mode == "spread":
        return select_spread_candidates(candidates, count, seed, features)
    raise ValueError(f"unknown selection mode: {mode}")


def expand_candidates(
    candidates: Iterable[dict[str, Any]],
    profiles: Iterable[QualityProfile],
    case_prefix: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        source_case_id = candidate.get("source_case_id") or f"source_{index:04d}"
        for profile in profiles:
            row: dict[str, Any] = {
                "case_id": f"{safe_name_part(case_prefix)}_{index:04d}_{safe_name_part(source_case_id)}_{profile.name}",
                "source_case_id": source_case_id,
                "source_result_path": candidate.get("source_result_path", ""),
                "quality_profile": profile.name,
                "base_rpm": candidate["base_rpm"],
                "i_peak_a": candidate["i_peak_a"],
                "beta_deg": candidate["beta_deg"],
                "operation": candidate.get("operation", "sin_current"),
                "use_periodic_boundary": candidate.get("use_periodic_boundary", ""),
                "transient_periods": profile.transient_periods,
                "steps_per_period": profile.steps_per_period,
            }
            for key in MESH_ELEMENT_KEYS:
                row[f"mesh_{key}_elements"] = profile.mesh_elements[key]
            row.update(candidate["geometry"])
            rows.append(row)
    return rows


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select fixed-geometry IPMSM replay cases from result CSVs.")
    parser.add_argument("--results", nargs="+", type=Path, required=True, help="Existing run_ipmsm_batch.py result CSVs.")
    parser.add_argument("--output", type=Path, required=True, help="Replay case CSV to write.")
    parser.add_argument("--profiles", default="baseline,mesh_fine,time_fine,mesh_time_fine")
    parser.add_argument("--source-cases", type=int, default=50, help="Number of source geometries before profile expansion.")
    parser.add_argument("--max-cases", type=int, default=200, help="Guardrail for costly Ansys solve batches.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--selection-mode", choices=("spread", "hash"), default="spread")
    parser.add_argument("--selection-features", default=",".join(DEFAULT_SELECTION_FEATURES))
    parser.add_argument("--status", default="ok", help="Required source row status; empty string disables status filtering.")
    parser.add_argument("--required-outputs", default=",".join(DEFAULT_REQUIRED_OUTPUTS))
    parser.add_argument("--case-prefix", default="replay")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        profiles = parse_profiles(args.profiles)
        required_outputs = parse_required_outputs(args.required_outputs)
        selection_features = parse_selection_features(args.selection_features)
        expanded_count = args.source_cases * len(profiles)
        if expanded_count > args.max_cases:
            parser.error(f"requested {expanded_count} replay rows, exceeding --max-cases={args.max_cases}")
        candidates, metrics = load_candidates(args.results, required_outputs, args.status)
        if len(candidates) < args.source_cases:
            parser.error(f"only {len(candidates)} eligible source case(s), fewer than --source-cases={args.source_cases}")
        selected = select_candidates(candidates, args.source_cases, args.seed, args.selection_mode, selection_features)
        rows = expand_candidates(selected, profiles, args.case_prefix)
        write_rows(args.output, rows)
    except ValueError as exc:
        parser.error(str(exc))

    print(
        "replay_cases "
        f"rows={len(rows)} source_cases={len(selected)} candidates={len(candidates)} "
        f"selection_mode={args.selection_mode} "
        f"scanned_rows={metrics['rows']} status_rejected={metrics['status_rejected']} "
        f"required_output_rejected={metrics['required_output_rejected']} "
        f"physical_sanity_rejected={metrics['physical_sanity_rejected']} "
        f"geometry_rejected={metrics['geometry_rejected']} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
