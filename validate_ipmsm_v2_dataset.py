"""Validate IPMSM v2 training CSVs without deleting physical extremes.

The v2 gate is intentionally structural and physics-based.  It does not use
output quantiles or IQR rules because those rules can remove the Pareto region
that the optimization workflow needs to preserve.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = "ipmsm_v2"
BETA_CONVENTION = "dq_current_advance_v2"

REQUIRED_COLUMNS = (
    "case_id",
    "status",
    "geometry_group_id",
    "design_hash",
    "doe_split",
    "repeat_of_case_id",
    "beta_calibration_id",
    "execution_host",
    "input_dataset_schema_version",
    "input_operation",
    "input_model_extent",
    "input_symmetry_factor",
    "input_use_periodic_boundary",
    "input_beta_convention",
    "input_beta_calibration_id",
    "input_beta_dq_deg",
    "input_commanded_id_peak_a",
    "input_commanded_iq_peak_a",
    "input_slot_opening_ratio",
    "input_magnet_space_height_ratio",
    "input_stack_length_mm",
    "input_base_rpm",
    "input_i_peak_a",
    "input_phase_resistance_ohm",
    "input_quality_profile",
    "input_setup_fingerprint",
    "input_material_fingerprint",
    "input_aedt_version",
    "output_torque_last_avg_nm",
    "output_torque_last_max_nm",
    "output_coreloss_last_avg_w",
    "output_solidloss_last_avg_w",
    "output_copperloss_last_avg_w",
    "output_ld_last_avg_h",
    "output_lq_last_avg_h",
    "output_phase_current_source",
    "output_phase_voltage_source",
    "output_phase_current_last_rms_a",
    "output_id_current_last_avg_a",
    "output_iq_current_last_avg_a",
    "output_phasea_voltage_last_peak_abs_v",
    "output_phaseb_voltage_last_peak_abs_v",
    "output_phasec_voltage_last_peak_abs_v",
    "output_phase_voltage_last_peak_abs_v",
    "output_total_loss_last_avg_w",
    "output_efficiency_last_pct",
)

FINITE_INPUT_COLUMNS = (
    "input_beta_dq_deg",
    "input_slot_opening_ratio",
    "input_magnet_space_height_ratio",
    "input_stack_length_mm",
    "input_base_rpm",
    "input_i_peak_a",
    "input_phase_resistance_ohm",
    "input_commanded_id_peak_a",
    "input_commanded_iq_peak_a",
)

FINITE_OUTPUT_COLUMNS = (
    "output_torque_last_avg_nm",
    "output_torque_last_max_nm",
    "output_coreloss_last_avg_w",
    "output_solidloss_last_avg_w",
    "output_copperloss_last_avg_w",
    "output_ld_last_avg_h",
    "output_lq_last_avg_h",
    "output_phase_current_last_rms_a",
    "output_id_current_last_avg_a",
    "output_iq_current_last_avg_a",
    "output_phasea_voltage_last_peak_abs_v",
    "output_phaseb_voltage_last_peak_abs_v",
    "output_phasec_voltage_last_peak_abs_v",
    "output_phase_voltage_last_peak_abs_v",
    "output_total_loss_last_avg_w",
)

FINGERPRINT_COLUMNS = (
    "input_model_extent",
    "input_beta_convention",
    "input_quality_profile",
    "input_setup_fingerprint",
    "input_material_fingerprint",
    "input_aedt_version",
    "input_beta_calibration_id",
)

REPEAT_RELATIVE_TOLERANCES = {
    "output_torque_last_avg_nm": (0.02, 0.1),
    "output_torque_last_max_nm": (0.02, 0.1),
    "output_coreloss_last_avg_w": (0.05, 0.1),
    "output_solidloss_last_avg_w": (0.05, 0.1),
    "output_ld_last_avg_h": (0.03, 1e-6),
    "output_lq_last_avg_h": (0.03, 1e-6),
    "output_phase_voltage_last_peak_abs_v": (0.02, 0.1),
}


def finite_float(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def relative_error(actual: float, expected: float) -> float:
    if not math.isfinite(actual) or not math.isfinite(expected):
        return math.inf
    return abs(actual - expected) / max(abs(expected), 1e-12)


def is_current_driven_operation(operation: object) -> bool:
    normalized = str(operation or "").strip().lower().replace("_", "").replace("-", "")
    return normalized not in {"noload", "backemf", "cogging"}


def false_like(value: object) -> bool:
    return str(value or "").strip().lower() in {"", "0", "false", "no", "off"}


def close_with_floor(actual: float, expected: float, relative: float, absolute: float) -> bool:
    if not math.isfinite(actual) or not math.isfinite(expected):
        return False
    return abs(actual - expected) <= max(absolute, relative * max(abs(expected), absolute))


def derived_metrics(row: dict[str, str]) -> dict[str, float]:
    rpm = finite_float(row.get("input_base_rpm"))
    current_peak = finite_float(row.get("input_i_peak_a"))
    resistance = finite_float(row.get("input_phase_resistance_ohm"))
    torque = finite_float(row.get("output_torque_last_avg_nm"))
    core_loss = finite_float(row.get("output_coreloss_last_avg_w"))
    solid_loss = finite_float(row.get("output_solidloss_last_avg_w"))

    copper_loss = finite_float(row.get("output_copperloss_last_avg_w"))
    total_loss = core_loss + solid_loss + copper_loss
    mechanical_power = torque * 2.0 * math.pi * rpm / 60.0
    efficiency = (
        mechanical_power / (mechanical_power + total_loss) * 100.0
        if mechanical_power > 0.0 and total_loss >= 0.0
        else math.nan
    )
    return {
        "copper_loss_w": copper_loss,
        "total_loss_w": total_loss,
        "mechanical_power_w": mechanical_power,
        "efficiency_pct": efficiency,
    }


@dataclass
class ValidationSummary:
    rows: int = 0
    ok_rows: int = 0
    unique_case_ids: int = 0
    unique_geometry_groups: int = 0
    repeat_pairs: int = 0
    issue_counts: dict[str, int] = field(default_factory=dict)
    fingerprint_values: dict[str, set[str]] = field(default_factory=dict)

    @property
    def failures(self) -> int:
        return sum(self.issue_counts.values())

    @property
    def passed(self) -> bool:
        return self.failures == 0

    def add_issue(self, name: str) -> None:
        self.issue_counts[name] = self.issue_counts.get(name, 0) + 1

    def as_row(self) -> dict[str, str | int]:
        return {
            "rows": self.rows,
            "ok_rows": self.ok_rows,
            "unique_case_ids": self.unique_case_ids,
            "unique_geometry_groups": self.unique_geometry_groups,
            "repeat_pairs": self.repeat_pairs,
            "failures": self.failures,
            "status": "pass" if self.passed else "fail",
            "issues": ";".join(f"{key}={value}" for key, value in sorted(self.issue_counts.items())),
        }


def validate_rows(
    rows: Iterable[dict[str, str]],
    *,
    fieldnames: Iterable[str] | None = None,
    max_identity_relative_error: float = 1e-6,
    max_current_relative_error: float = 0.02,
    max_current_absolute_error_a: float = 0.1,
    allow_mixed_fingerprints: bool = False,
) -> ValidationSummary:
    row_list = list(rows)
    summary = ValidationSummary(rows=len(row_list))
    available = set(fieldnames or (row_list[0].keys() if row_list else ()))
    for column in REQUIRED_COLUMNS:
        if column not in available:
            summary.add_issue(f"missing_column:{column}")
    if not row_list:
        summary.add_issue("empty_dataset")
        return summary

    case_ids: set[str] = set()
    rows_by_case_id: dict[str, dict[str, str]] = {}
    geometry_groups: set[str] = set()
    group_splits: dict[str, set[str]] = {}
    group_hashes: dict[str, set[str]] = {}
    hash_groups: dict[str, set[str]] = {}
    for column in FINGERPRINT_COLUMNS:
        if column in available:
            summary.fingerprint_values[column] = set()

    for row in row_list:
        case_id = str(row.get("case_id") or "").strip()
        geometry_group = str(row.get("geometry_group_id") or "").strip()
        design_hash = str(row.get("design_hash") or "").strip()
        doe_split = str(row.get("doe_split") or "").strip().lower()
        if not case_id:
            summary.add_issue("blank_case_id")
        elif case_id in case_ids:
            summary.add_issue("duplicate_case_id")
        else:
            case_ids.add(case_id)
            rows_by_case_id[case_id] = row
        if not geometry_group:
            summary.add_issue("blank_geometry_group_id")
        else:
            geometry_groups.add(geometry_group)
            group_splits.setdefault(geometry_group, set()).add(doe_split)
            group_hashes.setdefault(geometry_group, set()).add(design_hash)
            if design_hash:
                hash_groups.setdefault(design_hash, set()).add(geometry_group)
        if not design_hash:
            summary.add_issue("blank_design_hash")
        if str(row.get("execution_host") or "").strip().lower() in {"", "unknown"}:
            summary.add_issue("unknown_execution_host")
        if doe_split not in {"train", "calibration", "test"}:
            summary.add_issue("invalid_doe_split")

        if str(row.get("status") or "").strip().lower() != "ok":
            summary.add_issue("status_not_ok")
            continue
        summary.ok_rows += 1

        if str(row.get("input_dataset_schema_version") or "").strip() != SCHEMA_VERSION:
            summary.add_issue("schema_version_mismatch")
        if str(row.get("input_beta_convention") or "").strip() != BETA_CONVENTION:
            summary.add_issue("beta_convention_mismatch")
        if str(row.get("input_model_extent") or "").strip() != "full_360":
            summary.add_issue("model_extent_mismatch")
        if finite_float(row.get("input_symmetry_factor")) != 1.0:
            summary.add_issue("symmetry_factor_mismatch")
        if not false_like(row.get("input_use_periodic_boundary")):
            summary.add_issue("periodic_boundary_enabled")
        if str(row.get("input_aedt_version") or "").strip().lower() in {"", "auto", "unknown"}:
            summary.add_issue("unknown_aedt_version")
        calibration_id = str(row.get("input_beta_calibration_id") or "").strip()
        if not calibration_id:
            summary.add_issue("blank_beta_calibration_id")
        top_level_calibration_id = str(row.get("beta_calibration_id") or calibration_id).strip()
        if top_level_calibration_id != calibration_id:
            summary.add_issue("beta_calibration_id_mismatch")
        operation = str(row.get("input_operation") or "").strip()
        current_driven = is_current_driven_operation(operation)
        if not operation:
            summary.add_issue("blank_operation")
        for column in FINGERPRINT_COLUMNS:
            if column in available and not str(row.get(column) or "").strip():
                summary.add_issue(f"blank_fingerprint:{column}")

        for column in FINITE_INPUT_COLUMNS:
            value = finite_float(row.get(column))
            if not math.isfinite(value):
                summary.add_issue(f"nonfinite_input:{column}")
        for column in FINITE_OUTPUT_COLUMNS:
            value = finite_float(row.get(column))
            if not math.isfinite(value):
                summary.add_issue(f"nonfinite_output:{column}")

        efficiency = finite_float(row.get("output_efficiency_last_pct"))
        if current_driven:
            if row.get("output_phase_current_source") != "measured_three_phase":
                summary.add_issue("phase_current_not_measured_three_phase")
            if row.get("output_phase_voltage_source") != "measured_three_phase":
                summary.add_issue("phase_voltage_not_measured_three_phase")
            peak = finite_float(row.get("input_i_peak_a"))
            beta_rad = math.radians(finite_float(row.get("input_beta_dq_deg")))
            expected_id = -peak * math.sin(beta_rad)
            expected_iq = peak * math.cos(beta_rad)
            current_checks = {
                "commanded_id": (finite_float(row.get("input_commanded_id_peak_a")), expected_id),
                "commanded_iq": (finite_float(row.get("input_commanded_iq_peak_a")), expected_iq),
                "measured_id": (finite_float(row.get("output_id_current_last_avg_a")), expected_id),
                "measured_iq": (finite_float(row.get("output_iq_current_last_avg_a")), expected_iq),
                "measured_phase_rms": (
                    finite_float(row.get("output_phase_current_last_rms_a")),
                    peak / math.sqrt(2.0),
                ),
            }
            for name, (actual, expected_current) in current_checks.items():
                if not close_with_floor(
                    actual,
                    expected_current,
                    max_current_relative_error,
                    max_current_absolute_error_a,
                ):
                    summary.add_issue(f"current_contract:{name}")
            if not math.isfinite(efficiency):
                summary.add_issue("nonfinite_output:output_efficiency_last_pct")
            elif not 0.0 <= efficiency <= 100.0:
                summary.add_issue("efficiency_out_of_range")
        else:
            current_peak = finite_float(row.get("input_i_peak_a"))
            if math.isfinite(current_peak) and abs(current_peak) > 1e-12:
                summary.add_issue("no_load_nonzero_current")
            for column in (
                "input_commanded_id_peak_a",
                "input_commanded_iq_peak_a",
                "output_phase_current_last_rms_a",
                "output_id_current_last_avg_a",
                "output_iq_current_last_avg_a",
            ):
                value = finite_float(row.get(column))
                if not math.isfinite(value) or abs(value) > max_current_absolute_error_a:
                    summary.add_issue(f"no_load_current:{column}")
            back_emf_h1 = finite_float(row.get("output_back_emf_phasea_h1_rms_v"))
            if not math.isfinite(back_emf_h1) or back_emf_h1 < 0.0:
                summary.add_issue("invalid_no_load_back_emf_h1")
        for loss_column in (
            "output_coreloss_last_avg_w",
            "output_solidloss_last_avg_w",
            "output_copperloss_last_avg_w",
            "output_total_loss_last_avg_w",
        ):
            loss = finite_float(row.get(loss_column))
            if math.isfinite(loss) and loss < 0.0:
                summary.add_issue(f"negative_loss:{loss_column}")
        phase_voltage_peaks = [
            finite_float(row.get(f"output_phase{phase}_voltage_last_peak_abs_v"))
            for phase in ("a", "b", "c")
        ]
        aggregate_voltage = finite_float(row.get("output_phase_voltage_last_peak_abs_v"))
        if all(math.isfinite(value) and value >= 0.0 for value in phase_voltage_peaks):
            if relative_error(aggregate_voltage, max(phase_voltage_peaks)) > max_identity_relative_error:
                summary.add_issue("phase_voltage_envelope_identity")

        expected = derived_metrics(row)
        reported_copper_loss = finite_float(row.get("output_copperloss_last_avg_w"))
        actual_phase_rms = finite_float(row.get("output_phase_current_last_rms_a"))
        resistance = finite_float(row.get("input_phase_resistance_ohm"))
        expected_copper_loss = 3.0 * resistance * actual_phase_rms * actual_phase_rms
        if relative_error(reported_copper_loss, expected_copper_loss) > max_identity_relative_error:
            summary.add_issue("copper_loss_identity")
        total_loss = finite_float(row.get("output_total_loss_last_avg_w"))
        if relative_error(total_loss, expected["total_loss_w"]) > max_identity_relative_error:
            summary.add_issue("total_loss_identity")
        if current_driven and relative_error(efficiency, expected["efficiency_pct"]) > max_identity_relative_error:
            summary.add_issue("efficiency_identity")

        for column, values in summary.fingerprint_values.items():
            value = str(row.get(column) or "").strip()
            if value:
                values.add(value)

    summary.unique_case_ids = len(case_ids)
    summary.unique_geometry_groups = len(geometry_groups)
    for values in group_splits.values():
        if len(values) != 1:
            summary.add_issue("geometry_group_split_leakage")
    for values in group_hashes.values():
        if len(values) != 1 or "" in values:
            summary.add_issue("geometry_group_design_hash_mismatch")
    for values in hash_groups.values():
        if len(values) != 1:
            summary.add_issue("design_hash_geometry_group_mismatch")
    for row in row_list:
        repeat_of = str(row.get("repeat_of_case_id") or "").strip()
        if not repeat_of:
            continue
        source = rows_by_case_id.get(repeat_of)
        if source is None:
            summary.add_issue("repeat_source_missing")
            continue
        if str(source.get("repeat_of_case_id") or "").strip():
            summary.add_issue("repeat_source_is_repeat")
        for column in ("geometry_group_id", "design_hash", "doe_split", "operating_point_id"):
            if str(row.get(column) or "").strip() != str(source.get(column) or "").strip():
                summary.add_issue(f"repeat_metadata_mismatch:{column}")
        for column in (
            "input_base_rpm",
            "input_i_peak_a",
            "input_beta_dq_deg",
            "input_stack_length_mm",
            "input_phase_resistance_ohm",
        ):
            if not math.isclose(
                finite_float(row.get(column)),
                finite_float(source.get(column)),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                summary.add_issue(f"repeat_input_mismatch:{column}")
        summary.repeat_pairs += 1
        for column, (relative_tolerance, absolute_floor) in REPEAT_RELATIVE_TOLERANCES.items():
            repeated_value = finite_float(row.get(column))
            source_value = finite_float(source.get(column))
            difference = abs(repeated_value - source_value)
            scale = max(abs(repeated_value), abs(source_value), absolute_floor)
            if not math.isfinite(difference) or (
                difference > absolute_floor and difference / scale > relative_tolerance
            ):
                summary.add_issue(f"repeat_drift:{column}")
    if not allow_mixed_fingerprints:
        for column, values in summary.fingerprint_values.items():
            if len(values) > 1:
                summary.add_issue(f"mixed_fingerprint:{column}")
    return summary


def write_summary(path: Path, summary: ValidationSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = summary.as_row()
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a homogeneous IPMSM v2 training CSV.")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--max-identity-relative-error", type=float, default=1e-6)
    parser.add_argument("--max-current-relative-error", type=float, default=0.02)
    parser.add_argument("--max-current-absolute-error-a", type=float, default=0.1)
    parser.add_argument("--allow-mixed-fingerprints", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if min(
        args.max_identity_relative_error,
        args.max_current_relative_error,
        args.max_current_absolute_error_a,
    ) < 0.0:
        raise SystemExit("validation tolerances must be nonnegative")
    with args.data.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        summary = validate_rows(
            rows,
            fieldnames=reader.fieldnames,
            max_identity_relative_error=args.max_identity_relative_error,
            max_current_relative_error=args.max_current_relative_error,
            max_current_absolute_error_a=args.max_current_absolute_error_a,
            allow_mixed_fingerprints=args.allow_mixed_fingerprints,
        )
    write_summary(args.summary, summary)
    print(
        f"validated_ipmsm_v2 rows={summary.rows} groups={summary.unique_geometry_groups} "
        f"failures={summary.failures} status={'pass' if summary.passed else 'fail'}"
    )
    return 0 if summary.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
