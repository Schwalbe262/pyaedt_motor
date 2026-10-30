"""Compare complete, plan-defined last-cycle FEA pairs without certifying a baseline.

Matching an uncertified reference is a relative result. A production profile
also needs a compatible, independently finer mesh/time reference and complete
planned coverage. Timing ratios from a mixed wave remain exploratory.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from dataclasses import asdict, dataclass, replace
import hashlib
import io
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Mapping, Sequence

from module.ipmsm_waveform_quality import METRIC_CONTRACT_VERSION, v3_training_row_issues
from .io import atomic_write_bytes


@dataclass(frozen=True)
class MetricTolerance:
    column: str
    limit: float
    kind: str = "relative_pct"
    denominator_floor: float = 1.0
    assumption: bool = False


DEFAULT_TOLERANCES = (
    MetricTolerance("output_torque_last_avg_nm", 1.0),
    MetricTolerance("output_torque_last_max_nm", 2.0, assumption=True),
    MetricTolerance("output_coreloss_last_avg_w", 5.0),
    MetricTolerance("output_solidloss_last_avg_w", 5.0),
    MetricTolerance("output_total_loss_last_avg_w", 3.0),
    MetricTolerance("output_efficiency_last_pct", .5, "absolute_pp"),
    MetricTolerance("output_ld_last_avg_h", 2.0, denominator_floor=1e-9, assumption=True),
    MetricTolerance("output_lq_last_avg_h", 2.0, denominator_floor=1e-9, assumption=True),
    MetricTolerance("output_phase_voltage_last_peak_abs_v", 2.0, assumption=True),
    MetricTolerance("output_voltage_span_last_peak_v", 2.0, assumption=True),
)
HISTORICAL_TOLERANCES = DEFAULT_TOLERANCES[:-1]
GEOMETRY = (
    "stator_outer_radius", "stator_back_yoke_thick_ratio", "stator_inner_ratio",
    "stator_teeth_length_ratio", "stator_teeth_width_ratio", "stator_gap",
    "slot_opening_ratio", "rotator_gap", "shaft_ratio", "magnet_shield_thick",
    "magnet_setback_ratio", "magnet_thick_ratio", "magnet_space_height_ratio",
    "magnet_height_ratio", "stack_length_mm",
)
NUMERIC_FRAME = ("base_rpm", "i_peak_a", "beta_dq_deg", "electrical_zero_deg",
                 "initial_position_deg", "phase_resistance_ohm", "vdc_v",
                 "series_turns_per_phase", "turns_per_coil_side", "pole_number",
                 "slot_number", "symmetry_factor")
TEXT_FRAME = ("beta_convention", "beta_calibration_id", "model_extent")
PAIR_SOURCE_FIELDS = ("input_metric_contract_version", "input_producer_fingerprint",
                      "input_material_fingerprint", "input_aedt_version",
                      "input_dataset_schema_version")
ALIASES = {"beta_dq_deg": ("beta_deg",), "slot_number": ("slot_num",),
           "pole_number": ("pole_num",)}
PROFILE_NUMERIC = ("transient_periods", "steps_per_period", "circle_segments",
                   *("mesh_" + group + "_elements" for group in ("magnet", "rotor", "stator", "winding", "band")),
                   *("mesh_" + group + "_max_length_mm" for group in ("magnet", "rotor", "stator", "winding", "band")),
                   "band_circle_segments")
PROFILE_BOOLEAN = ("compute_inductance", "strict_setup")
PROFILE_TEXT = ("loss_policy", "analysis_purpose")


def _boolean(row, name):
    value = _input_text(row, name).lower()
    if value in {"true", "1", "1.0"}:
        return True
    if value in {"false", "0", "0.0"}:
        return False
    raise ValueError("invalid_boolean:" + name)


def _profile_settings(row):
    settings = {"quality_profile": _profile(row)}
    for key in PROFILE_NUMERIC:
        if _values(row, key):
            value = _number(row, key)
            settings[key] = int(value) if "max_length" not in key and value.is_integer() else value
    for key in PROFILE_BOOLEAN:
        if _values(row, key):
            settings[key] = _boolean(row, key)
    for key in PROFILE_TEXT:
        if _values(row, key):
            settings[key] = _input_text(row, key)
    return settings



def _text(value):
    return "" if value is None else str(value).strip()


def _values(row, name):
    keys = (name, *ALIASES.get(name, ()))
    return [row[key] for item in keys for key in ("input_" + item, item)
            if key in row and _text(row[key])]


def _number(row, name):
    values = _values(row, name)
    if not values:
        raise ValueError("missing:" + name)
    try:
        if any(isinstance(value, bool) for value in values):
            raise ValueError
        numbers = [float(value) for value in values]
    except (ValueError, TypeError):
        raise ValueError("nonfinite:" + name) from None
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("nonfinite:" + name)
    if not all(math.isclose(value, numbers[0], rel_tol=1e-10, abs_tol=1e-9) for value in numbers):
        raise ValueError("conflicting_aliases:" + name)
    return numbers[0]


def _input_text(row, name):
    values = {_text(value) for value in _values(row, name)}
    if len(values) != 1:
        raise ValueError(("missing:" if not values else "conflicting_aliases:") + name)
    return values.pop()


def _profile(row):
    return _input_text(row, "quality_profile")


def _point_key(row):
    # Inactive stator_shoe_thick does not change the physical geometry identity.
    numeric = tuple((key, _number(row, key)) for key in (*GEOMETRY, *NUMERIC_FRAME))
    texts = tuple((key, _input_text(row, key)) for key in TEXT_FRAME)
    return numeric, texts


def _identity_issues(row, expected):
    problems = []
    for key in (*GEOMETRY, *NUMERIC_FRAME):
        try:
            if not math.isclose(_number(row, key), _number(expected, key), rel_tol=1e-10, abs_tol=1e-9):
                problems.append("plan_mismatch:" + key)
        except ValueError as exc:
            problems.append(str(exc))
    for key in (*TEXT_FRAME, "quality_profile"):
        try:
            if _input_text(row, key) != _input_text(expected, key):
                problems.append("plan_mismatch:" + key)
        except ValueError as exc:
            problems.append(str(exc))
    for key in PROFILE_NUMERIC:
        if _values(expected, key):
            try:
                if not math.isclose(_number(row, key), _number(expected, key), rel_tol=1e-10, abs_tol=1e-9):
                    problems.append("plan_mismatch:" + key)
            except ValueError as exc:
                problems.append(str(exc))
    for key in PROFILE_BOOLEAN:
        if _values(expected, key):
            try:
                if _boolean(row, key) != _boolean(expected, key):
                    problems.append("plan_mismatch:" + key)
            except ValueError as exc:
                problems.append(str(exc))
    for key in (*PROFILE_TEXT, "core_material", "magnet_material", "winding_material", "shaft_material", "air_material"):
        if _values(expected, key):
            try:
                if _input_text(row, key) != _input_text(expected, key):
                    problems.append("plan_mismatch:" + key)
            except ValueError as exc:
                problems.append(str(exc))
    for key in ("geometry_group_id", "design_hash", "source_case_id"):
        planned = _values(expected, key)
        if planned:
            try:
                if _input_text(row, key) != _input_text(expected, key):
                    problems.append("plan_mismatch:" + key)
            except ValueError as exc:
                problems.append(str(exc))
    return problems


def _row_issues(row, rules):
    problems = []
    if _text(row.get("status")).lower() != "ok":
        problems.append("result_not_ok")
    if _text(row.get("analyze")).lower() not in {"true", "1", "1.0"}:
        problems.append("not_analyzed")
    if row.get("input_metric_contract_version") != METRIC_CONTRACT_VERSION:
        problems.append("not_v3_metrics")
    problems.extend(v3_training_row_issues(row))
    if row.get("input_dataset_schema_version") != "ipmsm_v2":
        problems.append("unsupported_dataset_schema")
    if row.get("input_model_extent") != "full_360" or row.get("input_beta_convention") != "dq_current_advance_v2":
        problems.append("unsupported_physical_frame")
    try:
        if _number(row, "symmetry_factor") != 1:
            problems.append("nonphysical_full_model_multiplier")
    except ValueError as exc:
        problems.append(str(exc))
    for key in PAIR_SOURCE_FIELDS:
        if not _text(row.get(key)):
            problems.append("missing_source:" + key)
    if any(rule.column == "output_voltage_span_last_peak_v" for rule in rules) and row.get("output_voltage_span_source") != "physical_three_phase_waveform":
        problems.append("unverified_physical_voltage_span")
    for rule in rules:
        try:
            value = float(row.get(rule.column, ""))
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value):
            problems.append("nonfinite_output:" + rule.column)
        elif "efficiency" in rule.column and not 0 < value <= 100:
            problems.append("physical_range:" + rule.column)
        elif "loss" in rule.column and value < 0:
            problems.append("physical_range:" + rule.column)
        elif any(name in rule.column for name in ("torque", "_ld_", "_lq_", "voltage")) and value <= 0:
            problems.append("physical_range:" + rule.column)
    try:
        torque = float(row["output_torque_last_avg_nm"])
        peak = float(row["output_torque_last_max_nm"])
        loss = float(row["output_total_loss_last_avg_w"])
        efficiency = float(row["output_efficiency_last_pct"])
        mechanical = torque * _number(row, "base_rpm") * 2 * math.pi / 60
        if peak < torque - 1e-9:
            problems.append("torque_max_below_mean")
        if mechanical > 0 and loss >= 0 and abs(efficiency - 100 * mechanical / (mechanical + loss)) > 1e-4:
            problems.append("efficiency_identity")
        copper = row.get("output_copperloss_last_avg_w")
        if copper is not None and _text(copper):
            parts = [float(copper), float(row["output_coreloss_last_avg_w"]), float(row["output_solidloss_last_avg_w"])]
            if not all(math.isfinite(value) and value >= 0 for value in parts) or not math.isclose(sum(parts), loss, rel_tol=1e-6, abs_tol=1e-6):
                problems.append("total_loss_identity")
    except (ValueError, TypeError, KeyError):
        pass  # Individual required fields already carry explicit nonfinite issues.
    return sorted(set(problems))


def _deltas(row, reference, rules):
    metrics = {}
    for rule in rules:
        try:
            actual, baseline = float(row.get(rule.column, "")), float(reference.get(rule.column, ""))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(actual) or not math.isfinite(baseline):
            continue
        delta = actual - baseline
        error = abs(delta) if rule.kind == "absolute_pp" else 100 * abs(delta) / max(abs(baseline), rule.denominator_floor)
        metrics[rule.column] = {"candidate": actual, "reference": baseline, "delta": delta,
                                "error": error, "kind": rule.kind, "limit": rule.limit,
                                "passed": error <= rule.limit}
    return metrics


def _source_issues(candidate, reference):
    issues = ["incompatible_source:" + key for key in PAIR_SOURCE_FIELDS
              if not _text(candidate.get(key)) or _text(candidate.get(key)) != _text(reference.get(key))]
    for key in ("source_case_id", "geometry_group_id", "design_hash"):
        if _values(candidate, key) or _values(reference, key):
            try:
                if _input_text(candidate, key) != _input_text(reference, key):
                    issues.append("incompatible_source_identity:" + key)
            except ValueError as exc:
                issues.append(str(exc))
    return issues


def _mesh_count(mesh_counts, case_id):
    value = mesh_counts.get(case_id)
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return int(number) if math.isfinite(number) and number > 0 and number.is_integer() else None


def compare_profiles(results: Sequence[Mapping[str, Any]], case_plan: Sequence[Mapping[str, Any]], *,
                     reference_profile: str, refined_reference_profile: str | None = None,
                     tolerances: Sequence[MetricTolerance] = DEFAULT_TOLERANCES,
                     mesh_counts: Mapping[str, int] | None = None) -> dict:
    """Return explicit coverage and diagnostics; absent refinement cannot promote."""
    rules = tuple(tolerances)
    columns = {rule.column for rule in rules}
    modern_contract = columns == {rule.column for rule in DEFAULT_TOLERANCES}
    if len(columns) != len(rules) or not (modern_contract or columns == {rule.column for rule in HISTORICAL_TOLERANCES}):
        raise ValueError("a complete unique nine- or ten-target last-cycle metric contract is required")
    for rule in rules:
        if rule.kind not in {"relative_pct", "absolute_pp"} or isinstance(rule.limit, bool) or not math.isfinite(rule.limit) or rule.limit < 0:
            raise ValueError("invalid metric tolerance")
        if not math.isfinite(rule.denominator_floor) or rule.denominator_floor <= 0:
            raise ValueError("invalid metric denominator floor")
    if not case_plan:
        raise ValueError("case plan must not be empty")
    planned, groups = {}, defaultdict(lambda: defaultdict(list))
    for row in case_plan:
        case = _text(row.get("case_id"))
        if not case or case in planned:
            raise ValueError("case plan has missing/duplicate case identity")
        planned[case] = dict(row)
        groups[_point_key(row)][_profile(row)].append(case)
    family = {profile for group in groups.values() if reference_profile in group for profile in group}
    global_issues = []
    if not family:
        global_issues.append("reference_profile_missing_from_plan")
    index = defaultdict(list)
    for row in results:
        index[_text(row.get("case_id"))].append(dict(row))
    duplicate_results = sorted(case for case, rows in index.items() if len(rows) != 1)
    unplanned = sorted(set(index) - set(planned))
    if duplicate_results:
        global_issues.append("duplicate_result_case_ids")
    if unplanned:
        global_issues.append("unplanned_result_case_ids")
    producers = {_text(row.get("input_producer_fingerprint")) for rows in index.values() for row in rows
                 if _text(row.get("input_producer_fingerprint"))}
    if len(producers) != 1:
        global_issues.append("missing_or_mixed_producers")
    mesh_counts = mesh_counts or {}
    unpaired, comparisons, certifications, coverage = [], [], [], []
    for point, profiles in groups.items():
        if not set(profiles) & family:
            unpaired.extend(case for cases in profiles.values() for case in cases)
            continue
        group_id = hashlib.sha256(repr(point).encode()).hexdigest()[:16]
        reference_cases = profiles.get(reference_profile, [])
        group_errors = []
        if len(reference_cases) != 1:
            group_errors.append("missing_or_ambiguous_reference_in_plan")
        reference_id = reference_cases[0] if len(reference_cases) == 1 else None
        reference_rows = index.get(reference_id, [])
        reference = reference_rows[0] if len(reference_rows) == 1 else None
        if reference is None:
            group_errors.append("missing_or_ambiguous_reference_result")
        else:
            group_errors.extend("reference:" + issue for issue in _identity_issues(reference, planned[reference_id]))
            group_errors.extend("reference:" + issue for issue in _row_issues(reference, rules))
        refined_ids = profiles.get(refined_reference_profile, []) if refined_reference_profile else []
        refined_rows = index.get(refined_ids[0], []) if len(refined_ids) == 1 else []
        refined = refined_rows[0] if len(refined_rows) == 1 else None
        cert_errors = list(group_errors)
        if not modern_contract:
            cert_errors.append("modern_voltage_span_contract_unavailable")
        if refined is None:
            cert_errors.append("independently_refined_reference_unavailable")
            if refined_reference_profile:
                cert_errors.append("missing_or_ambiguous_refined_reference_in_plan" if len(refined_ids) != 1
                                   else "missing_or_ambiguous_refined_reference_result")
        elif reference is not None:
            cert_errors.extend("refined:" + issue for issue in _identity_issues(refined, planned[refined_ids[0]]))
            cert_errors.extend("refined:" + issue for issue in _row_issues(refined, rules))
            cert_errors.extend(_source_issues(refined, reference))
            try:
                if _number(refined, "steps_per_period") <= _number(reference, "steps_per_period"):
                    cert_errors.append("time_refinement_unproven")
            except ValueError as exc:
                cert_errors.append("refinement_metadata:" + str(exc))
            base_mesh, fine_mesh = _mesh_count(mesh_counts, reference_id), _mesh_count(mesh_counts, refined_ids[0])
            if base_mesh is None or fine_mesh is None or fine_mesh <= base_mesh:
                cert_errors.append("actual_mesh_refinement_unproven")
            delta = _deltas(reference, refined, rules)
            if len(delta) != len(rules) or not all(value["passed"] for value in delta.values()):
                cert_errors.append("reference_not_converged_against_refinement")
        certified = not cert_errors and not global_issues
        certifications.append({"group": group_id, "reference_case_id": reference_id,
                               "refined_case_id": refined_ids[0] if len(refined_ids) == 1 else None,
                               "certified": certified, "issues": sorted(set(cert_errors))})
        coverage.append({"group": group_id, "reference_case_id": reference_id,
                         "reference_available": reference is not None, "issues": sorted(set(group_errors))})
        for profile in sorted(family - {reference_profile, refined_reference_profile}):
            cases = profiles.get(profile, [])
            case_id = cases[0] if len(cases) == 1 else None
            actual_rows = index.get(case_id, [])
            candidate = actual_rows[0] if len(actual_rows) == 1 else None
            errors = list(group_errors)
            if len(cases) != 1:
                errors.append("missing_or_ambiguous_candidate_in_plan")
            if candidate is None:
                errors.append("missing_or_ambiguous_candidate_result")
            metrics, ratio = {}, None
            if candidate is not None:
                errors.extend(_identity_issues(candidate, planned[case_id]))
                errors.extend(_row_issues(candidate, rules))
                if reference is not None:
                    errors.extend(_source_issues(candidate, reference))
                    metrics = _deltas(candidate, reference, rules)
                    if len(metrics) != len(rules) or not all(value["passed"] for value in metrics.values()):
                        errors.append("metric_tolerance_exceeded_or_missing")
                    try:
                        a, b = float(candidate["timing_solve_s"]), float(reference["timing_solve_s"])
                        if math.isfinite(a) and math.isfinite(b) and min(a, b) > 0:
                            ratio = a / b
                    except (ValueError, TypeError, KeyError):
                        pass
            comparisons.append({"group": group_id, "profile": profile, "case_id": case_id,
                                "reference_case_id": reference_id, "candidate_available": candidate is not None,
                                "candidate_relative_pass": not errors and not global_issues,
                                "reference_certified": certified, "issues": sorted(set(errors)),
                                "metrics": metrics, "paired_solve_time_ratio": ratio,
                                "actual_mesh_elements": _mesh_count(mesh_counts, case_id)})
    profile_settings, setting_errors = {}, {}
    for profile in family:
        variants = {json.dumps(_profile_settings(row), sort_keys=True) for row in planned.values() if _profile(row) == profile}
        profile_settings[profile] = json.loads(next(iter(variants))) if len(variants) == 1 else None
        setting_errors[profile] = [] if len(variants) == 1 else ["mixed_profile_settings"]
    profile_summary = []
    for profile in sorted(family - {reference_profile, refined_reference_profile}):
        pairs = [pair for pair in comparisons if pair["profile"] == profile]
        relative_pass = bool(pairs) and all(pair["candidate_relative_pass"] for pair in pairs)
        certified = bool(pairs) and all(pair["reference_certified"] for pair in pairs)
        ratios = [pair["paired_solve_time_ratio"] for pair in pairs if pair["paired_solve_time_ratio"] is not None]
        profile_summary.append({"profile": profile, "role": "candidate", "settings": profile_settings[profile],
                                "issues": setting_errors[profile], "planned_pairs": len(pairs),
                                "observed_pairs": sum(pair["candidate_available"] for pair in pairs),
                                "relative_pass_pairs": sum(pair["candidate_relative_pass"] for pair in pairs),
                                "paired_runtime_pairs": len(ratios),
                                "paired_solve_time_ratio_median": statistics.median(ratios) if ratios else None,
                                "candidate_relative_pass": relative_pass, "reference_certified": certified,
                                "production_profile_eligible": relative_pass and certified and not setting_errors[profile]})
    if reference_profile in family:
        reference_ok = bool(coverage) and not global_issues and all(not group["issues"] for group in coverage)
        certified = bool(certifications) and all(group["certified"] for group in certifications)
        profile_summary.append({"profile": reference_profile, "role": "reference", "settings": profile_settings[reference_profile],
                                "issues": setting_errors[reference_profile], "planned_pairs": len(coverage),
                                "observed_pairs": sum(group["reference_available"] for group in coverage),
                                "relative_pass_pairs": sum(not group["issues"] for group in coverage),
                                "candidate_relative_pass": reference_ok, "relative_to_self": True,
                                "reference_certified": certified,
                                "production_profile_eligible": reference_ok and certified and not setting_errors[reference_profile]})
    return {"schema_version": "ipmsm_v3_profile_comparison_v1", "reference_profile": reference_profile,
            "refined_reference_profile": refined_reference_profile, "planned_cases": len(planned),
            "production_metric_contract_complete": modern_contract,
            "planned_reference_groups": len(coverage), "result_rows": len(results),
            "producer_fingerprint": next(iter(producers)) if len(producers) == 1 else None,
            "global_issues": global_issues, "duplicate_result_case_ids": duplicate_results,
            "unplanned_result_case_ids": unplanned, "unpaired_plan_case_ids": sorted(unpaired),
            "reference_coverage": coverage, "reference_certification": certifications,
            "tolerances": [asdict(rule) for rule in rules], "comparisons": comparisons,
            "profiles": profile_summary,
            "exploratory_runtime_ranking": [row["profile"] for row in sorted(
                [item for item in profile_summary if item.get("paired_solve_time_ratio_median") is not None],
                key=lambda item: (item["paired_solve_time_ratio_median"], item["profile"]))],
            "runtime_interpretation": "exploratory_mixed_wave",
            "runtime_resource_confounding": True,
            "mesh_refinement_note": "Certification requires increased actual elements and steps per period; requested caps are not mesh evidence."}



def selected_profile_manifest(report, report_path: Path, profile: str) -> dict:
    if report.get("schema_version") != "ipmsm_v3_profile_comparison_v1":
        raise ValueError("unsupported comparison report")
    report_bytes = report_path.read_bytes()
    if json.loads(report_bytes.decode("utf-8-sig")) != report:
        raise ValueError("comparison report bytes do not match the supplied decision")
    if report.get("global_issues") or not re.fullmatch(r"producer_v3:sha256:[0-9a-f]{64}", str(report.get("producer_fingerprint", ""))):
        raise ValueError("comparison report has incompatible source evidence")
    if {item.get("column") for item in report.get("tolerances", [])} != {item.column for item in DEFAULT_TOLERANCES}:
        raise ValueError("comparison report omitted required last-cycle targets")
    if report.get("production_metric_contract_complete") is not True:
        raise ValueError("comparison report lacks the modern physical voltage-span contract")
    selected = [item for item in report["profiles"] if item["profile"] == profile]
    if len(selected) != 1 or not selected[0]["production_profile_eligible"]:
        raise ValueError("profile has not passed complete certified-reference comparison")
    row = selected[0]
    expected = report["planned_reference_groups"]
    certifications = report["reference_certification"]
    if not expected or row["observed_pairs"] != expected or row["planned_pairs"] != expected or len(certifications) != expected or not all(item["certified"] is True for item in certifications):
        raise ValueError("selected profile lacks complete independently certified reference coverage")
    flat = row["settings"]
    required = ["transient_periods", "steps_per_period", "compute_inductance", "strict_setup",
                "circle_segments", "loss_policy", *("mesh_" + group + "_elements" for group in
                ("magnet", "rotor", "stator", "winding", "band"))]
    if any(key not in flat for key in required):
        raise ValueError("selected profile lacks explicit reproducible settings")
    if not flat["compute_inductance"] or not flat["strict_setup"] or flat["circle_segments"] != 0 or flat["loss_policy"] != "laminated_core":
        raise ValueError("selected profile is not a canonical loaded training profile")
    settings = {key: flat[key] for key in ["transient_periods", "steps_per_period", "compute_inductance",
                                         "strict_setup", "circle_segments", "loss_policy"]}
    settings.update(quality_profile=profile, analysis_purpose="training", require_steady_state=True)
    settings["mesh_elements"] = {group: flat["mesh_" + group + "_elements"] for group in
                                 ("magnet", "rotor", "stator", "winding", "band")}
    local = {group: flat["mesh_" + group + "_max_length_mm"] for group in
             ("magnet", "rotor", "stator", "winding", "band") if "mesh_" + group + "_max_length_mm" in flat}
    if local:
        settings["mesh_maximum_lengths_mm"] = local
    if "band_circle_segments" in flat:
        settings["band_circle_segments"] = flat["band_circle_segments"]
    return {"schema_version": "ipmsm_v3_selected_profile_v1", "metric_contract_version": METRIC_CONTRACT_VERSION,
            "producer_fingerprint": report["producer_fingerprint"], "settings": settings,
            "evidence": [{"path": str(report_path.resolve()),
                          "sha256": hashlib.sha256(report_bytes).hexdigest()}]}


def read_mesh_counts(root: Path) -> dict[str, int]:
    values = {}
    for path in root.glob("*/mesh_summary.json"):
        records = json.loads(path.read_text(encoding="utf-8"))
        counts = [_mesh_count({"mesh": record.get("elements")}, "mesh") for record in records]
        if len(counts) == 1 and counts[0] is not None:
            values[path.parent.name] = counts[0]
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--reference-profile", required=True)
    parser.add_argument("--refined-reference-profile")
    parser.add_argument("--mesh-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ld-lq-pct", type=float, default=2.0)
    parser.add_argument("--voltage-pct", type=float, default=2.0)
    parser.add_argument("--historical-nine-targets", action="store_true", help="diagnostic comparison only; cannot export a production profile")
    parser.add_argument("--tolerances", type=Path, help="JSON mapping last-cycle metric column to numeric limit")
    parser.add_argument("--require-production-profile", action="store_true")
    parser.add_argument("--selected-profile")
    parser.add_argument("--selected-output", type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(args.output)
    if bool(args.selected_profile) != bool(args.selected_output):
        raise ValueError("--selected-profile and --selected-output must be supplied together")
    if args.selected_output and args.selected_output.exists():
        raise FileExistsError(args.selected_output)
    overrides = json.loads(args.tolerances.read_text(encoding="utf-8-sig")) if args.tolerances else {}
    if not isinstance(overrides, dict) or set(overrides) - {rule.column for rule in DEFAULT_TOLERANCES}:
        raise ValueError("unknown metric tolerance overrides")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in overrides.values()):
        raise ValueError("metric tolerance overrides must be numeric")
    rules = [replace(rule, limit=float(overrides.get(rule.column,
                     args.ld_lq_pct if rule.column in {"output_ld_last_avg_h", "output_lq_last_avg_h"} else
                     args.voltage_pct if "voltage" in rule.column else rule.limit))) for rule in
             (HISTORICAL_TOLERANCES if args.historical_nine_targets else DEFAULT_TOLERANCES)]
    # The collector replaces its live combined CSV. Parse and hash the same
    # immutable bytes, and retain them so an interim comparison remains replayable.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    snapshots = args.output.parent / (args.output.stem + "_inputs")
    snapshots.mkdir(exist_ok=False)
    comparison_source = Path(__file__).read_bytes()
    comparison_snapshot = snapshots / "comparison_source.py"
    atomic_write_bytes(comparison_snapshot, comparison_source)
    gate_source = Path(v3_training_row_issues.__code__.co_filename).read_bytes()
    gate_snapshot = snapshots / "waveform_quality_source.py"
    atomic_write_bytes(gate_snapshot, gate_source)
    payloads = [(path, path.read_bytes()) for path in [args.plan, *args.results]]
    input_files = []
    parsed = []
    for index, (path, payload) in enumerate(payloads):
        target = snapshots / f"input_{index:03d}.csv"
        atomic_write_bytes(target, payload)
        input_files.append({"path": str(path), "snapshot": str(target.resolve()),
                            "sha256": hashlib.sha256(payload).hexdigest()})
        parsed.append(list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))))
    mesh_counts, mesh_files = {}, []
    if args.mesh_root:
        for index, path in enumerate(sorted(args.mesh_root.glob("*/mesh_summary.json"))):
            payload = path.read_bytes()
            records = json.loads(payload.decode("utf-8-sig"))
            counts = [_mesh_count({"mesh": record.get("elements")}, "mesh") for record in records]
            if len(counts) == 1 and counts[0] is not None:
                mesh_counts[path.parent.name] = counts[0]
            target = snapshots / f"mesh_{index:03d}.json"
            atomic_write_bytes(target, payload)
            mesh_files.append({"case_id": path.parent.name, "path": str(path),
                               "snapshot": str(target.resolve()), "sha256": hashlib.sha256(payload).hexdigest()})
    results = [row for rows in parsed[1:] for row in rows]
    report = compare_profiles(results, parsed[0], reference_profile=args.reference_profile,
                              refined_reference_profile=args.refined_reference_profile, tolerances=rules,
                              mesh_counts=mesh_counts)
    report["case_plan_sha256"] = input_files[0]["sha256"]
    report["case_plan_file"] = input_files[0]
    report["result_files"] = input_files[1:]
    report["mesh_files"] = mesh_files
    report["comparison_source_sha256"] = hashlib.sha256(comparison_source).hexdigest()
    report["comparison_source_snapshot"] = str(comparison_snapshot.resolve())
    report["metric_gate_source"] = {"snapshot": str(gate_snapshot.resolve()), "sha256": hashlib.sha256(gate_source).hexdigest()}
    atomic_write_bytes(args.output, (json.dumps(report, indent=2, allow_nan=False) + "\n").encode())
    if args.selected_output:
        selected = selected_profile_manifest(report, args.output, args.selected_profile)
        args.selected_output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(args.selected_output, (json.dumps(selected, indent=2, allow_nan=False) + "\n").encode())
    print(json.dumps({"groups": report["planned_reference_groups"], "result_rows": len(results),
                      "issues": report["global_issues"], "profiles": report["profiles"]}))
    return int(args.require_production_profile and not any(profile["production_profile_eligible"] for profile in report["profiles"]))


if __name__ == "__main__":
    raise SystemExit(main())
