"""Expand fixed source geometries across second-pass quality profiles."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable

import rank_ipmsm_quality_profiles as profile_rank
from generate_ipmsm_quality_cases import (
    MESH_ELEMENT_KEYS,
    THIRD_PASS_SPEED_PROFILE_NAMES,
    QualityProfile,
    parse_profiles,
    safe_name_part,
)


REQUIRED_COLUMNS = (
    "case_id",
    "source_case_id",
    "quality_profile",
    "transient_periods",
    "steps_per_period",
    *(f"mesh_{key}_elements" for key in MESH_ELEMENT_KEYS),
)

STRICT_SCHEMA_VERSION = "ipmsm_v2"
STRICT_REFERENCE_PROFILE = "reference_ultra"
STRICT_BETA_CONVENTION = "dq_current_advance_v2"
STRICT_MODEL_EXTENT = "full_360"
STRICT_SOURCE_COUNT = 12
STRICT_FINGERPRINT_COLUMNS = (
    "input_setup_fingerprint",
    "input_material_fingerprint",
    "input_aedt_version",
)
DESIGN_COLUMNS = (
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
CONTROL_COLUMNS = ("base_rpm", "i_peak_a", "beta_dq_deg")
DIVERSITY_COLUMNS = (*DESIGN_COLUMNS, *CONTROL_COLUMNS)
STRICT_PAIR_COLUMNS = (
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
    "slot_num",
    "pole_num",
    *DESIGN_COLUMNS,
    *CONTROL_COLUMNS,
    "stack_length_mm",
    "phase_resistance_ohm",
    "vdc_v",
    "transient_periods",
    "steps_per_period",
    *(f"mesh_{key}_elements" for key in MESH_ELEMENT_KEYS),
)
STRICT_MUTABLE_PROFILE_COLUMNS = (
    "case_id",
    "source_case_id",
    "reference_case_id",
    "quality_profile",
    "transient_periods",
    "steps_per_period",
    *(f"mesh_{key}_elements" for key in MESH_ELEMENT_KEYS),
)
STRICT_AUDIT_COLUMNS = (
    "reference_case_id",
    "reference_identity_sha256",
    "reference_setup_fingerprint",
    "reference_material_fingerprint",
    "reference_aedt_version",
)


def false_like(value: object) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def finite_float(value: object) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def equivalent_value(expected: object, actual: object) -> bool:
    expected_text = str(expected or "").strip()
    actual_text = str(actual or "").strip()
    if expected_text.lower() in {"true", "false"} or actual_text.lower() in {"true", "false"}:
        return expected_text.lower() == actual_text.lower()
    expected_number = finite_float(expected_text)
    actual_number = finite_float(actual_text)
    if math.isfinite(expected_number) or math.isfinite(actual_number):
        return math.isfinite(expected_number) and math.isfinite(actual_number) and math.isclose(
            expected_number, actual_number, rel_tol=1e-10, abs_tol=1e-12
        )
    return expected_text == actual_text


def canonical_identity_value(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() in {"true", "false"}:
        return text.lower()
    number = finite_float(text)
    if math.isfinite(number):
        return f"{number:.12g}"
    return text


def result_value_for_plan_column(result: dict[str, str], column: str) -> str:
    aliases = {
        "case_id": ("case_id",),
        "geometry_group_id": ("geometry_group_id",),
        "design_hash": ("design_hash", "input_design_hash"),
        "operating_point_id": ("operating_point_id",),
        "doe_split": ("doe_split",),
        "repeat_of_case_id": ("repeat_of_case_id",),
        "beta_calibration_id": ("input_beta_calibration_id", "beta_calibration_id"),
        "dataset_schema_version": ("input_dataset_schema_version",),
        "quality_profile": ("input_quality_profile", "quality_profile"),
    }.get(column, (f"input_{column}", column))
    for alias in aliases:
        if alias in result:
            return str(result.get(alias) or "").strip()
    return ""


def reference_identity_sha256_from_plan(plan_row: dict[str, str]) -> str:
    payload = {column: canonical_identity_value(plan_row.get(column, "")) for column in STRICT_PAIR_COLUMNS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reference_identity_sha256_from_result(result_row: dict[str, str]) -> str:
    payload = {
        column: canonical_identity_value(result_value_for_plan_column(result_row, column))
        for column in STRICT_PAIR_COLUMNS
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_id(row: dict[str, str]) -> str:
    return str(row.get("source_case_id") or row.get("input_source_case_id") or row.get("case_id") or "").strip()


def ordered_unique_sources(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        identifier = source_id(row)
        if not identifier:
            raise ValueError("source row is missing source_case_id/input_source_case_id/case_id")
        if identifier in seen:
            continue
        seen.add(identifier)
        selected.append(dict(row))
    return selected


def parse_source_case_ids(text: str) -> list[str]:
    identifiers = [part.strip() for part in text.split(",") if part.strip()]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("--source-case-ids must not contain duplicate IDs")
    return identifiers


def select_sources_by_ids(
    sources: Iterable[dict[str, str]],
    requested_ids: Iterable[str],
) -> list[dict[str, str]]:
    source_by_id = {source_id(row): dict(row) for row in sources}
    requested = list(requested_ids)
    missing = [identifier for identifier in requested if identifier not in source_by_id]
    if missing:
        raise ValueError(f"source case IDs not found: {','.join(missing)}")
    return [source_by_id[identifier] for identifier in requested]


def load_source_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return ordered_unique_sources(dict(row) for row in reader)


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        return [dict(row) for row in reader]


def rows_by_unique_case_id(rows: Iterable[dict[str, str]], label: str) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            raise ValueError(f"{label} contains a blank case_id")
        if case_id in indexed:
            raise ValueError(f"{label} contains duplicate case_id={case_id!r}")
        indexed[case_id] = dict(row)
    return indexed


def validate_strict_source_plan_row(row: dict[str, str]) -> None:
    case_id = str(row.get("case_id") or "").strip()
    failures: list[str] = []
    expected_text = {
        "dataset_schema_version": STRICT_SCHEMA_VERSION,
        "quality_profile": STRICT_REFERENCE_PROFILE,
        "model_extent": STRICT_MODEL_EXTENT,
        "beta_convention": STRICT_BETA_CONVENTION,
    }
    for column, expected in expected_text.items():
        if str(row.get(column) or "").strip() != expected:
            failures.append(column)
    if not math.isclose(finite_float(row.get("symmetry_factor")), 1.0, abs_tol=1e-12):
        failures.append("symmetry_factor")
    if not false_like(row.get("use_periodic_boundary")):
        failures.append("use_periodic_boundary")
    for column in ("case_id", "design_hash", "geometry_group_id", "beta_calibration_id", "operation"):
        if not str(row.get(column) or "").strip():
            failures.append(column)
    for column in (*DIVERSITY_COLUMNS, "electrical_zero_deg"):
        if not math.isfinite(finite_float(row.get(column))):
            failures.append(column)
    if failures:
        raise ValueError(
            f"strict source plan contract failed for case_id={case_id!r}: " + ", ".join(sorted(set(failures)))
        )


def strict_result_is_completed(row: dict[str, str]) -> bool:
    return (
        str(row.get("status") or "").strip().lower() == "ok"
        and not str(row.get("missing_required_outputs") or "").strip()
        and profile_rank.row_is_complete(row)
    )


def validate_strict_result_contract(row: dict[str, str], expected_profile: str) -> None:
    case_id = str(row.get("case_id") or "").strip()
    failures: list[str] = []
    expected_text = {
        "input_dataset_schema_version": STRICT_SCHEMA_VERSION,
        "input_quality_profile": expected_profile,
        "input_model_extent": STRICT_MODEL_EXTENT,
        "input_beta_convention": STRICT_BETA_CONVENTION,
    }
    for column, expected in expected_text.items():
        if str(row.get(column) or "").strip() != expected:
            failures.append(column)
    if not math.isclose(finite_float(row.get("input_symmetry_factor")), 1.0, abs_tol=1e-12):
        failures.append("input_symmetry_factor")
    if not false_like(row.get("input_use_periodic_boundary")):
        failures.append("input_use_periodic_boundary")
    if not math.isfinite(finite_float(row.get("input_electrical_zero_deg"))):
        failures.append("input_electrical_zero_deg")
    if str(row.get("input_geometry_mode") or "").strip() != "fixed":
        failures.append("input_geometry_mode")
    calibration_id = str(row.get("input_beta_calibration_id") or "").strip()
    if not calibration_id:
        failures.append("input_beta_calibration_id")
    top_calibration_id = str(row.get("beta_calibration_id") or calibration_id).strip()
    if top_calibration_id != calibration_id:
        failures.append("beta_calibration_id")
    for column in STRICT_FINGERPRINT_COLUMNS:
        value = str(row.get(column) or "").strip()
        if not value or (column == "input_aedt_version" and value.lower() in {"auto", "unknown"}):
            failures.append(column)
    if failures:
        raise ValueError(
            f"strict result contract failed for case_id={case_id!r}: " + ", ".join(sorted(set(failures)))
        )


def validate_reference_matches_plan(plan_row: dict[str, str], result_row: dict[str, str]) -> None:
    validate_strict_source_plan_row(plan_row)
    validate_strict_result_contract(result_row, STRICT_REFERENCE_PROFILE)
    mismatches = [
        column
        for column in STRICT_PAIR_COLUMNS
        if not equivalent_value(plan_row.get(column, ""), result_value_for_plan_column(result_row, column))
    ]
    if mismatches:
        raise ValueError(
            f"strict reference result does not match source plan for case_id={plan_row.get('case_id')!r}: "
            + ", ".join(mismatches)
        )
    plan_digest = reference_identity_sha256_from_plan(plan_row)
    result_digest = reference_identity_sha256_from_result(result_row)
    if plan_digest != result_digest:
        raise ValueError(f"strict reference identity digest mismatch for case_id={plan_row.get('case_id')!r}")


def normalized_diversity_vectors(rows: list[dict[str, str]]) -> dict[str, tuple[float, ...]]:
    values_by_column = {
        column: [finite_float(row.get(column)) for row in rows]
        for column in DIVERSITY_COLUMNS
    }
    vectors: dict[str, tuple[float, ...]] = {}
    for row in rows:
        vector: list[float] = []
        for column in DIVERSITY_COLUMNS:
            values = values_by_column[column]
            low = min(values)
            high = max(values)
            value = finite_float(row.get(column))
            vector.append((value - low) / (high - low) if high > low else 0.0)
        vectors[str(row["case_id"])] = tuple(vector)
    return vectors


def squared_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum((lhs - rhs) ** 2 for lhs, rhs in zip(left, right))


def select_diverse_rows(rows: list[dict[str, str]], count: int) -> list[dict[str, str]]:
    if count < 1:
        raise ValueError("strict source count must be >= 1")
    unique_designs = {str(row.get("design_hash") or "").strip() for row in rows}
    if len(unique_designs) < count:
        raise ValueError(f"strict completed cohort has only {len(unique_designs)} unique designs; need {count}")
    ordered = sorted(rows, key=lambda row: str(row["case_id"]))
    vectors = normalized_diversity_vectors(ordered)
    dimension = len(DIVERSITY_COLUMNS)
    centroid = tuple(
        sum(vector[index] for vector in vectors.values()) / len(vectors)
        for index in range(dimension)
    )
    first = max(ordered, key=lambda row: (squared_distance(vectors[str(row["case_id"])], centroid), str(row["case_id"])))
    selected = [first]
    selected_designs = {str(first["design_hash"]).strip()}
    while len(selected) < count:
        available = [row for row in ordered if str(row["design_hash"]).strip() not in selected_designs]
        if not available:
            raise ValueError(f"strict completed cohort cannot provide {count} distinct designs")
        scored = []
        for row in available:
            vector = vectors[str(row["case_id"])]
            min_distance = min(squared_distance(vector, vectors[str(item["case_id"])]) for item in selected)
            scored.append((min_distance, str(row["case_id"]), row))
        best_score = max(item[0] for item in scored)
        chosen = min((item for item in scored if math.isclose(item[0], best_score, abs_tol=1e-15)), key=lambda item: item[1])[2]
        selected.append(chosen)
        selected_designs.add(str(chosen["design_hash"]).strip())
    return selected


def select_strict_speed_sources(
    plan_rows: list[dict[str, str]],
    result_rows: list[dict[str, str]],
    count: int = STRICT_SOURCE_COUNT,
) -> list[tuple[dict[str, str], dict[str, str]]]:
    result_by_case = rows_by_unique_case_id(result_rows, "strict source results")
    eligible_plan_rows: list[dict[str, str]] = []
    paired_results: dict[str, dict[str, str]] = {}
    for plan_row in plan_rows:
        validate_strict_source_plan_row(plan_row)
        if str(plan_row.get("repeat_of_case_id") or "").strip():
            continue
        case_id = str(plan_row["case_id"]).strip()
        result_row = result_by_case.get(case_id)
        if result_row is None or str(result_row.get("status") or "").strip().lower() != "ok":
            continue
        validate_strict_result_contract(result_row, STRICT_REFERENCE_PROFILE)
        if not strict_result_is_completed(result_row):
            continue
        validate_reference_matches_plan(plan_row, result_row)
        eligible_plan_rows.append(dict(plan_row))
        paired_results[case_id] = dict(result_row)
    selected = select_diverse_rows(eligible_plan_rows, count)
    return [(row, paired_results[str(row["case_id"])]) for row in selected]


def merged_fieldnames(
    source_fieldnames: Iterable[str] | None,
    *,
    include_strict_audit: bool = False,
) -> list[str]:
    fieldnames = list(source_fieldnames or [])
    appended = (*REQUIRED_COLUMNS, *STRICT_AUDIT_COLUMNS) if include_strict_audit else REQUIRED_COLUMNS
    for column in appended:
        if column not in fieldnames:
            fieldnames.append(column)
    return fieldnames


def expand_rows(
    sources: Iterable[dict[str, str]],
    profiles: Iterable[QualityProfile],
    case_prefix: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source_index, source in enumerate(sources, start=1):
        identifier = source_id(source)
        source_name = safe_name_part(identifier)
        for profile in profiles:
            row = dict(source)
            row["case_id"] = f"{safe_name_part(case_prefix)}_{source_index:04d}_{source_name}_{profile.name}"
            row["source_case_id"] = identifier
            row["quality_profile"] = profile.name
            row["transient_periods"] = str(profile.transient_periods)
            row["steps_per_period"] = str(profile.steps_per_period)
            for key in MESH_ELEMENT_KEYS:
                row[f"mesh_{key}_elements"] = str(profile.mesh_elements[key])
            rows.append(row)
    return rows


def expand_strict_speed_rows(
    sources: list[tuple[dict[str, str], dict[str, str]]],
    profiles: Iterable[QualityProfile],
    case_prefix: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for profile in profiles:
        for source_index, (source, reference_result) in enumerate(sources, start=1):
            reference_case_id = str(source["case_id"]).strip()
            row = dict(source)
            row["case_id"] = (
                f"{safe_name_part(case_prefix)}_{source_index:04d}_"
                f"{safe_name_part(reference_case_id)}_{profile.name}"
            )
            row["source_case_id"] = reference_case_id
            row["reference_case_id"] = reference_case_id
            row["reference_identity_sha256"] = reference_identity_sha256_from_plan(source)
            row["reference_setup_fingerprint"] = str(reference_result["input_setup_fingerprint"]).strip()
            row["reference_material_fingerprint"] = str(reference_result["input_material_fingerprint"]).strip()
            row["reference_aedt_version"] = str(reference_result["input_aedt_version"]).strip()
            row["quality_profile"] = profile.name
            row["transient_periods"] = str(profile.transient_periods)
            row["steps_per_period"] = str(profile.steps_per_period)
            for key in MESH_ELEMENT_KEYS:
                row[f"mesh_{key}_elements"] = str(profile.mesh_elements[key])
            rows.append(row)
    return rows


def source_fieldnames(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return list(reader.fieldnames or [])


def write_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate second-pass profile cases from fixed source geometry rows.")
    parser.add_argument("--source-cases", type=Path, required=True, help="Existing expanded case CSV containing source rows.")
    parser.add_argument(
        "--source-results",
        action="append",
        type=Path,
        default=[],
        help="Completed strict-v2 result CSV. Required for the audited third-pass speed pair; repeatable.",
    )
    parser.add_argument("--output", type=Path, required=True, help="CSV path for generated second-pass rows.")
    parser.add_argument("--profiles", required=True, help="Comma-separated second-pass profile names.")
    parser.add_argument(
        "--source-case-ids",
        default="",
        help="Optional comma-separated source IDs to select in exactly the supplied order.",
    )
    parser.add_argument("--source-count", type=int, default=STRICT_SOURCE_COUNT)
    parser.add_argument("--case-prefix", default="secondpass")
    parser.add_argument("--max-cases", type=int, default=200)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        profiles = parse_profiles(args.profiles)
        profile_names = tuple(profile.name for profile in profiles)
        uses_strict_speed_profile = bool(set(profile_names) & set(THIRD_PASS_SPEED_PROFILE_NAMES))
        strict_speed_mode = profile_names == THIRD_PASS_SPEED_PROFILE_NAMES
        if uses_strict_speed_profile and not strict_speed_mode:
            raise ValueError(
                "audited third-pass speed generation requires exactly "
                + ",".join(THIRD_PASS_SPEED_PROFILE_NAMES)
            )
        if strict_speed_mode:
            if not args.source_results:
                raise ValueError(
                    "audited third-pass speed generation requires --source-results from a completed strict-v2 cohort"
                )
            if args.source_case_ids:
                raise ValueError("audited third-pass speed generation selects diverse sources; --source-case-ids is forbidden")
            if args.source_count != STRICT_SOURCE_COUNT:
                raise ValueError(f"audited third-pass speed generation requires --source-count={STRICT_SOURCE_COUNT}")
            if args.output.exists():
                raise ValueError(f"audited third-pass speed output must be fresh: {args.output}")
            source_plan_rows = load_rows(args.source_cases)
            source_result_rows = [row for path in args.source_results for row in load_rows(path)]
            strict_sources = select_strict_speed_sources(source_plan_rows, source_result_rows, args.source_count)
            rows = expand_strict_speed_rows(strict_sources, profiles, args.case_prefix)
            sources_count = len(strict_sources)
        else:
            sources = load_source_rows(args.source_cases)
            requested_ids = parse_source_case_ids(args.source_case_ids)
            if requested_ids:
                sources = select_sources_by_ids(sources, requested_ids)
            rows = expand_rows(sources, profiles, args.case_prefix)
            sources_count = len(sources)
        if len(rows) > args.max_cases:
            parser.error(f"generated {len(rows)} rows, exceeding --max-cases={args.max_cases}")
        write_rows(
            args.output,
            rows,
            merged_fieldnames(source_fieldnames(args.source_cases), include_strict_audit=strict_speed_mode),
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(
        "second_pass_cases "
        f"rows={len(rows)} source_cases={sources_count} strict_speed={str(strict_speed_mode).lower()} "
        f"profiles={','.join(profile.name for profile in profiles)} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
