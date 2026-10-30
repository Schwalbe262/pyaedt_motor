"""Compare final legacy-p174 predictions with reference-ultra FEA results.

The comparison deliberately joins only by exact ``case_id``.  FEA torque,
core loss, and solid loss are read from the last-period result columns.  The
physical FEA efficiency is then reconstructed with the same geometry-specific
100 C phase resistance and fixed 137.8 A / 1200 rpm contract used by the final
surrogate search.  The FEA-reported native efficiency remains a separate
diagnostic and is never substituted for the reconstructed primary metric.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence


CURRENT_PEAK_A = 137.8
DESIGN_TARGET_TORQUE_NM = 65.1
LEGACY_SYMMETRY_MULTIPLIER = 4.0
SPEED_RPM = 1200.0
CURRENT_TOLERANCE_A = 1.0e-6
SPEED_TOLERANCE_RPM = 1.0e-6
IDENTITY_RELATIVE_TOLERANCE = 1.0e-9
CSV_NAME = "predicted_vs_fea.csv"
SUMMARY_NAME = "comparison_summary.json"

REQUIRED_PREDICTION_COLUMNS = (
    "case_id",
    "predicted_efficiency_pct",
    "predicted_nominal_torque_nm",
    "predicted_core_loss_w",
    "predicted_solid_loss_w",
    "predicted_copper_loss_100c_w",
    "predicted_total_loss_physical_w",
    "phase_resistance_100c_ohm",
    "torque_constraint_nm",
)
REQUIRED_RESULT_COLUMNS = (
    "case_id",
    "status",
    "input_base_rpm",
    "input_i_peak_a",
    "output_torque_last_avg_nm",
    "output_coreloss_last_avg_w",
    "output_solidloss_last_avg_w",
    "output_efficiency_last_pct",
)
DIRECT_VOLTAGE_MARGIN_COLUMNS = (
    "output_voltage_margin_last_v",
    "output_voltage_svpwm_margin_last_v",
    "output_voltage_spwm_margin_last_v",
)

COMPARISON_FIELDNAMES = (
    "case_id",
    "candidate_id",
    "selection_order",
    "selection_role",
    "design_hash",
    "source_result_file",
    "predicted_torque_nm",
    "fea_torque_nm",
    "error_predicted_minus_fea_torque_nm",
    "predicted_to_fea_torque_ratio",
    "legacy_symmetry_multiplier_diagnostic",
    "fea_torque_legacy_symmetry4_diagnostic_nm",
    "error_predicted_minus_fea_legacy_symmetry4_torque_nm",
    "predicted_core_loss_w",
    "fea_core_loss_native_w",
    "fea_core_loss_physical_w",
    "error_predicted_minus_fea_core_loss_w",
    "predicted_solid_loss_w",
    "fea_solid_loss_native_w",
    "fea_solid_loss_physical_w",
    "error_predicted_minus_fea_solid_loss_w",
    "phase_resistance_100c_ohm",
    "predicted_copper_loss_100c_w",
    "fea_copper_loss_100c_w",
    "predicted_total_loss_physical_w",
    "fea_total_loss_physical_w",
    "error_predicted_minus_fea_total_loss_physical_w",
    "predicted_efficiency_physical_pct",
    "fea_efficiency_physical_pct",
    "error_predicted_minus_fea_efficiency_physical_pct_point",
    "fea_efficiency_native_pct",
    "error_predicted_minus_fea_efficiency_native_pct_point",
    "predicted_mechanical_power_w",
    "fea_mechanical_power_w",
    "torque_target_nm",
    "predicted_torque_target_pass",
    "fea_torque_target_pass",
    "design_target_torque_nm",
    "predicted_design_target_pass",
    "fea_design_target_pass",
    "fea_input_base_rpm",
    "fea_input_i_peak_a",
    "fea_input_phase_resistance_ohm",
    "fea_voltage_peak_last_v",
    "fea_voltage_limit_v",
    "fea_voltage_margin_v",
    "fea_voltage_margin_source",
    "fea_voltage_margin_pass",
)


class ComparisonError(RuntimeError):
    """Raised when exact comparison inputs violate their contract."""


@dataclass(frozen=True)
class ResultInventory:
    rows_by_case_id: dict[str, dict[str, str]]
    source_by_case_id: dict[str, Path]
    files_scanned: tuple[Path, ...]
    files_skipped_without_schema: tuple[Path, ...]
    result_rows_scanned: int
    unrelated_result_rows: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_header(value: str) -> str:
    return value.lstrip("\ufeff").strip()


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ComparisonError(f"cannot read CSV {path}: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        original = reader.fieldnames
        if not original:
            raise ComparisonError(f"CSV has no header: {path}")
        normalized = [_normalize_header(name) for name in original]
        if any(not name for name in normalized):
            raise ComparisonError(f"CSV has a blank normalized header: {path}")
        if len(set(normalized)) != len(normalized):
            raise ComparisonError(f"CSV has duplicate normalized headers: {path}")
        rows: list[dict[str, str]] = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise ComparisonError(f"CSV row {row_number} has extra fields: {path}")
            row = {
                normalized[index]: "" if raw.get(name) is None else str(raw[name])
                for index, name in enumerate(original)
            }
            if any(value.strip() for value in row.values()):
                rows.append(row)
    return normalized, rows


def _require_columns(columns: Iterable[str], required: Sequence[str], label: str) -> None:
    available = set(columns)
    missing = [name for name in required if name not in available]
    if missing:
        raise ComparisonError(f"{label} is missing required columns: {missing}")


def _text(row: Mapping[str, str], column: str, label: str) -> str:
    value = str(row.get(column, "")).strip()
    if not value:
        raise ComparisonError(f"{label} has blank {column}")
    return value


def _finite(row: Mapping[str, str], column: str, label: str) -> float:
    text = _text(row, column, label)
    try:
        value = float(text)
    except ValueError as exc:
        raise ComparisonError(f"{label} has nonnumeric {column}: {text!r}") from exc
    if not math.isfinite(value):
        raise ComparisonError(f"{label} has nonfinite {column}: {text!r}")
    return value


def _nonnegative(row: Mapping[str, str], column: str, label: str) -> float:
    value = _finite(row, column, label)
    if value < 0.0:
        raise ComparisonError(f"{label} has negative {column}: {value}")
    return value


def _optional_finite(row: Mapping[str, str], column: str, label: str) -> float | None:
    if not str(row.get(column, "")).strip():
        return None
    return _finite(row, column, label)


def _close(left: float, right: float) -> bool:
    return math.isclose(
        left,
        right,
        rel_tol=IDENTITY_RELATIVE_TOLERANCE,
        abs_tol=1.0e-9,
    )


def _discover_result_files(inputs: Sequence[Path]) -> list[tuple[Path, bool]]:
    if not inputs:
        raise ComparisonError("at least one --results path is required")
    discovered: dict[Path, bool] = {}
    for raw_path in inputs:
        path = raw_path.expanduser().resolve()
        if not path.exists():
            raise ComparisonError(f"results path does not exist: {path}")
        if path.is_file():
            if path.suffix.lower() != ".csv":
                raise ComparisonError(f"explicit results file is not CSV: {path}")
            discovered[path] = True
        elif path.is_dir():
            for child in sorted(path.rglob("*.csv")):
                resolved = child.resolve()
                discovered.setdefault(resolved, False)
        else:
            raise ComparisonError(f"results path is neither file nor directory: {path}")
    if not discovered:
        raise ComparisonError("no result CSV files were discovered")
    return sorted(discovered.items(), key=lambda item: str(item[0]).lower())


def load_predictions(path: Path) -> list[dict[str, str]]:
    columns, rows = read_csv_rows(path)
    _require_columns(columns, REQUIRED_PREDICTION_COLUMNS, f"prediction CSV {path}")
    if not rows:
        raise ComparisonError(f"prediction CSV has no rows: {path}")
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        case_id = _text(row, "case_id", f"prediction row {index}")
        if case_id in seen:
            raise ComparisonError(f"duplicate prediction case_id: {case_id!r}")
        seen.add(case_id)
    return rows


def load_results(inputs: Sequence[Path], selected_case_ids: set[str]) -> ResultInventory:
    rows_by_case_id: dict[str, dict[str, str]] = {}
    source_by_case_id: dict[str, Path] = {}
    scanned: list[Path] = []
    skipped: list[Path] = []
    row_count = 0
    unrelated = 0
    for path, explicit in _discover_result_files(inputs):
        columns, rows = read_csv_rows(path)
        missing = [name for name in REQUIRED_RESULT_COLUMNS if name not in columns]
        if missing:
            if explicit:
                raise ComparisonError(
                    f"explicit result CSV {path} is missing required columns: {missing}"
                )
            skipped.append(path)
            continue
        scanned.append(path)
        row_count += len(rows)
        for row_index, row in enumerate(rows, start=2):
            case_id = _text(row, "case_id", f"result {path} row {row_index}")
            if case_id not in selected_case_ids:
                unrelated += 1
                continue
            if case_id in rows_by_case_id:
                raise ComparisonError(
                    "duplicate selected result case_id across result inputs: "
                    f"{case_id!r} in {source_by_case_id[case_id]} and {path}"
                )
            status = _text(row, "status", f"result case_id={case_id!r}")
            if status.lower() != "ok":
                raise ComparisonError(
                    f"result case_id={case_id!r} has non-ok status: {status!r}"
                )
            rows_by_case_id[case_id] = row
            source_by_case_id[case_id] = path
    missing_ids = sorted(selected_case_ids - set(rows_by_case_id))
    if missing_ids:
        raise ComparisonError(
            f"exact case_id join is missing {len(missing_ids)} selected results: {missing_ids}"
        )
    return ResultInventory(
        rows_by_case_id=rows_by_case_id,
        source_by_case_id=source_by_case_id,
        files_scanned=tuple(scanned),
        files_skipped_without_schema=tuple(skipped),
        result_rows_scanned=row_count,
        unrelated_result_rows=unrelated,
    )


def _voltage_fields(result: Mapping[str, str], label: str) -> dict[str, Any]:
    peak = _optional_finite(result, "output_phase_voltage_last_peak_abs_v", label)
    limit = _optional_finite(result, "output_phase_voltage_limit_svpwm_v", label)
    for column in DIRECT_VOLTAGE_MARGIN_COLUMNS:
        value = _optional_finite(result, column, label)
        if value is not None:
            return {
                "fea_voltage_peak_last_v": peak,
                "fea_voltage_limit_v": limit,
                "fea_voltage_margin_v": value,
                "fea_voltage_margin_source": column,
                "fea_voltage_margin_pass": value >= 0.0,
            }
    if peak is not None and limit is not None:
        margin = limit - peak
        return {
            "fea_voltage_peak_last_v": peak,
            "fea_voltage_limit_v": limit,
            "fea_voltage_margin_v": margin,
            "fea_voltage_margin_source": "derived_svpwm_limit_minus_peak",
            "fea_voltage_margin_pass": margin >= 0.0,
        }
    return {
        "fea_voltage_peak_last_v": peak,
        "fea_voltage_limit_v": limit,
        "fea_voltage_margin_v": None,
        "fea_voltage_margin_source": "",
        "fea_voltage_margin_pass": None,
    }


def build_comparison_row(
    prediction: Mapping[str, str],
    result: Mapping[str, str],
    source_path: Path,
) -> dict[str, Any]:
    case_id = _text(prediction, "case_id", "prediction")
    label = f"case_id={case_id!r}"
    if _text(result, "case_id", label) != case_id:
        raise ComparisonError(f"internal exact case_id join mismatch for {case_id!r}")

    predicted_torque = _finite(prediction, "predicted_nominal_torque_nm", label)
    predicted_core = _nonnegative(prediction, "predicted_core_loss_w", label)
    predicted_solid = _nonnegative(prediction, "predicted_solid_loss_w", label)
    resistance_100c = _nonnegative(prediction, "phase_resistance_100c_ohm", label)
    predicted_copper = _nonnegative(prediction, "predicted_copper_loss_100c_w", label)
    predicted_total = _nonnegative(prediction, "predicted_total_loss_physical_w", label)
    predicted_efficiency = _finite(prediction, "predicted_efficiency_pct", label)
    torque_target = _finite(prediction, "torque_constraint_nm", label)

    expected_copper = 1.5 * resistance_100c * CURRENT_PEAK_A**2
    if not _close(predicted_copper, expected_copper):
        raise ComparisonError(f"{label} violates predicted copper-loss identity")
    if not _close(predicted_total, predicted_core + predicted_solid + predicted_copper):
        raise ComparisonError(f"{label} violates predicted total-loss identity")
    predicted_mechanical = predicted_torque * SPEED_RPM * 2.0 * math.pi / 60.0
    if predicted_mechanical <= 0.0:
        raise ComparisonError(f"{label} has nonpositive predicted mechanical power")
    expected_predicted_efficiency = (
        100.0 * predicted_mechanical / (predicted_mechanical + predicted_total)
    )
    if not _close(predicted_efficiency, expected_predicted_efficiency):
        raise ComparisonError(f"{label} violates predicted physical-efficiency identity")

    result_rpm = _finite(result, "input_base_rpm", label)
    result_current = _finite(result, "input_i_peak_a", label)
    if abs(result_rpm - SPEED_RPM) > SPEED_TOLERANCE_RPM:
        raise ComparisonError(
            f"{label} input_base_rpm={result_rpm} does not match {SPEED_RPM}"
        )
    if abs(result_current - CURRENT_PEAK_A) > CURRENT_TOLERANCE_A:
        raise ComparisonError(
            f"{label} input_i_peak_a={result_current} does not match {CURRENT_PEAK_A}"
        )

    fea_torque = _finite(result, "output_torque_last_avg_nm", label)
    if fea_torque <= 0.0:
        raise ComparisonError(f"{label} has nonpositive FEA torque")
    fea_torque_legacy_scaled = fea_torque * LEGACY_SYMMETRY_MULTIPLIER
    fea_core_native = _finite(result, "output_coreloss_last_avg_w", label)
    fea_solid_native = _finite(result, "output_solidloss_last_avg_w", label)
    fea_native_efficiency = _finite(result, "output_efficiency_last_pct", label)
    if not 0.0 <= fea_native_efficiency <= 100.0:
        raise ComparisonError(f"{label} FEA native efficiency is outside [0, 100]")
    fea_core_physical = max(0.0, fea_core_native)
    fea_solid_physical = max(0.0, fea_solid_native)
    fea_copper = expected_copper
    fea_total = fea_core_physical + fea_solid_physical + fea_copper
    fea_mechanical = fea_torque * SPEED_RPM * 2.0 * math.pi / 60.0
    if fea_mechanical <= 0.0:
        raise ComparisonError(f"{label} has nonpositive FEA mechanical power")
    fea_physical_efficiency = 100.0 * fea_mechanical / (fea_mechanical + fea_total)

    row: dict[str, Any] = {
        "case_id": case_id,
        "candidate_id": str(prediction.get("candidate_id", "")).strip(),
        "selection_order": str(prediction.get("selection_order", "")).strip(),
        "selection_role": str(prediction.get("selection_role", "")).strip(),
        "design_hash": str(prediction.get("design_hash", "")).strip(),
        "source_result_file": str(source_path),
        "predicted_torque_nm": predicted_torque,
        "fea_torque_nm": fea_torque,
        "error_predicted_minus_fea_torque_nm": predicted_torque - fea_torque,
        "predicted_to_fea_torque_ratio": predicted_torque / fea_torque,
        "legacy_symmetry_multiplier_diagnostic": LEGACY_SYMMETRY_MULTIPLIER,
        "fea_torque_legacy_symmetry4_diagnostic_nm": fea_torque_legacy_scaled,
        "error_predicted_minus_fea_legacy_symmetry4_torque_nm": (
            predicted_torque - fea_torque_legacy_scaled
        ),
        "predicted_core_loss_w": predicted_core,
        "fea_core_loss_native_w": fea_core_native,
        "fea_core_loss_physical_w": fea_core_physical,
        "error_predicted_minus_fea_core_loss_w": predicted_core - fea_core_physical,
        "predicted_solid_loss_w": predicted_solid,
        "fea_solid_loss_native_w": fea_solid_native,
        "fea_solid_loss_physical_w": fea_solid_physical,
        "error_predicted_minus_fea_solid_loss_w": predicted_solid - fea_solid_physical,
        "phase_resistance_100c_ohm": resistance_100c,
        "predicted_copper_loss_100c_w": predicted_copper,
        "fea_copper_loss_100c_w": fea_copper,
        "predicted_total_loss_physical_w": predicted_total,
        "fea_total_loss_physical_w": fea_total,
        "error_predicted_minus_fea_total_loss_physical_w": predicted_total - fea_total,
        "predicted_efficiency_physical_pct": predicted_efficiency,
        "fea_efficiency_physical_pct": fea_physical_efficiency,
        "error_predicted_minus_fea_efficiency_physical_pct_point": (
            predicted_efficiency - fea_physical_efficiency
        ),
        "fea_efficiency_native_pct": fea_native_efficiency,
        "error_predicted_minus_fea_efficiency_native_pct_point": (
            predicted_efficiency - fea_native_efficiency
        ),
        "predicted_mechanical_power_w": predicted_mechanical,
        "fea_mechanical_power_w": fea_mechanical,
        "torque_target_nm": torque_target,
        "predicted_torque_target_pass": predicted_torque >= torque_target,
        "fea_torque_target_pass": fea_torque >= torque_target,
        "design_target_torque_nm": DESIGN_TARGET_TORQUE_NM,
        "predicted_design_target_pass": predicted_torque >= DESIGN_TARGET_TORQUE_NM,
        "fea_design_target_pass": fea_torque >= DESIGN_TARGET_TORQUE_NM,
        "fea_input_base_rpm": result_rpm,
        "fea_input_i_peak_a": result_current,
        "fea_input_phase_resistance_ohm": _optional_finite(
            result, "input_phase_resistance_ohm", label
        ),
    }
    row.update(_voltage_fields(result, label))
    return row


def error_metrics(values: Sequence[float], unit: str) -> dict[str, Any]:
    if not values:
        raise ComparisonError("cannot compute metrics for zero rows")
    count = len(values)
    return {
        "count": count,
        "unit": unit,
        "MAE": sum(abs(value) for value in values) / count,
        "RMSE": math.sqrt(sum(value * value for value in values) / count),
        "bias": sum(values) / count,
        "max_abs": max(abs(value) for value in values),
    }


def _write_csv_atomic(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMPARISON_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def compare(
    predictions_path: Path,
    result_inputs: Sequence[Path],
    output_dir: Path,
) -> dict[str, Any]:
    predictions_path = predictions_path.expanduser().resolve()
    if not predictions_path.is_file():
        raise ComparisonError(f"prediction CSV does not exist: {predictions_path}")
    predictions = load_predictions(predictions_path)
    prediction_ids = [_text(row, "case_id", "prediction") for row in predictions]
    inventory = load_results(result_inputs, set(prediction_ids))
    comparison_rows = [
        build_comparison_row(
            prediction,
            inventory.rows_by_case_id[case_id],
            inventory.source_by_case_id[case_id],
        )
        for prediction, case_id in zip(predictions, prediction_ids)
    ]

    targets = {float(row["torque_target_nm"]) for row in comparison_rows}
    if len(targets) != 1:
        raise ComparisonError(f"predictions contain nonuniform torque targets: {sorted(targets)}")
    primary_specs = {
        "torque_nm": ("error_predicted_minus_fea_torque_nm", "N*m"),
        "core_loss_w": ("error_predicted_minus_fea_core_loss_w", "W"),
        "solid_loss_w": ("error_predicted_minus_fea_solid_loss_w", "W"),
        "total_loss_physical_w": (
            "error_predicted_minus_fea_total_loss_physical_w",
            "W",
        ),
        "efficiency_physical_pct": (
            "error_predicted_minus_fea_efficiency_physical_pct_point",
            "percentage_point",
        ),
    }
    primary_metrics = {
        name: error_metrics([float(row[column]) for row in comparison_rows], unit)
        for name, (column, unit) in primary_specs.items()
    }
    native_metrics = error_metrics(
        [
            float(row["error_predicted_minus_fea_efficiency_native_pct_point"])
            for row in comparison_rows
        ],
        "percentage_point",
    )
    symmetry_diagnostic_errors = [
        float(row["error_predicted_minus_fea_legacy_symmetry4_torque_nm"])
        for row in comparison_rows
    ]
    torque_ratios = [
        float(row["predicted_to_fea_torque_ratio"]) for row in comparison_rows
    ]
    voltage_values = [
        float(row["fea_voltage_margin_v"])
        for row in comparison_rows
        if row["fea_voltage_margin_v"] is not None
    ]
    voltage_summary: dict[str, Any] = {
        "available_count": len(voltage_values),
        "missing_count": len(comparison_rows) - len(voltage_values),
        "pass_count": sum(value >= 0.0 for value in voltage_values),
        "fail_count": sum(value < 0.0 for value in voltage_values),
        "minimum_v": min(voltage_values) if voltage_values else None,
        "maximum_v": max(voltage_values) if voltage_values else None,
        "mean_v": sum(voltage_values) / len(voltage_values) if voltage_values else None,
    }

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / CSV_NAME
    summary_path = output_dir / SUMMARY_NAME
    _write_csv_atomic(csv_path, comparison_rows)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "status": "ok",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "case_id_join": "exact one-to-one; no candidate/order fallback",
        "comparison_rows": len(comparison_rows),
        "prediction_rows": len(predictions),
        "result_rows_scanned": inventory.result_rows_scanned,
        "unrelated_result_rows_ignored": inventory.unrelated_result_rows,
        "result_files_scanned": len(inventory.files_scanned),
        "result_files_skipped_without_reference_ultra_schema": len(
            inventory.files_skipped_without_schema
        ),
        "operating_point_contract": {
            "speed_rpm": SPEED_RPM,
            "current_peak_a": CURRENT_PEAK_A,
            "copper_loss_formula": "1.5 * phase_resistance_100c_ohm * current_peak_a^2",
            "mechanical_power_formula": "torque_nm * speed_rpm * 2*pi/60",
            "loss_handling": "FEA core and solid losses clipped to >= 0 for physical metrics",
        },
        "error_definition": "predicted_minus_fea",
        "primary_error_metrics": primary_metrics,
        "auxiliary_native_efficiency_error_metrics": native_metrics,
        "legacy_symmetry4_torque_diagnostic": {
            "warning": (
                "diagnostic only; multiplying full-360 symmetry=1 FEA torque by 4 "
                "is not an accepted physical result"
            ),
            "multiplier": LEGACY_SYMMETRY_MULTIPLIER,
            "predicted_minus_scaled_fea_error_metrics": error_metrics(
                symmetry_diagnostic_errors, "N*m"
            ),
            "predicted_to_raw_fea_ratio": {
                "minimum": min(torque_ratios),
                "mean": sum(torque_ratios) / len(torque_ratios),
                "maximum": max(torque_ratios),
            },
        },
        "torque_target": {
            "target_nm": next(iter(targets)),
            "predicted_pass_count": sum(
                bool(row["predicted_torque_target_pass"]) for row in comparison_rows
            ),
            "fea_pass_count": sum(
                bool(row["fea_torque_target_pass"]) for row in comparison_rows
            ),
            "fea_fail_count": sum(
                not bool(row["fea_torque_target_pass"]) for row in comparison_rows
            ),
        },
        "design_torque_target": {
            "target_nm": DESIGN_TARGET_TORQUE_NM,
            "predicted_pass_count": sum(
                bool(row["predicted_design_target_pass"]) for row in comparison_rows
            ),
            "fea_pass_count": sum(
                bool(row["fea_design_target_pass"]) for row in comparison_rows
            ),
            "fea_fail_count": sum(
                not bool(row["fea_design_target_pass"]) for row in comparison_rows
            ),
        },
        "fea_voltage_margin": voltage_summary,
        "clipped_negative_fea_loss_counts": {
            "core": sum(float(row["fea_core_loss_native_w"]) < 0.0 for row in comparison_rows),
            "solid": sum(float(row["fea_solid_loss_native_w"]) < 0.0 for row in comparison_rows),
        },
        "inputs": {
            "predictions_path": str(predictions_path),
            "predictions_sha256": sha256_file(predictions_path),
            "result_files": [
                {"path": str(path), "sha256": sha256_file(path)}
                for path in inventory.files_scanned
            ],
        },
        "outputs": {
            "per_case_csv": str(csv_path),
            "per_case_csv_sha256": sha256_file(csv_path),
            "summary_json": str(summary_path),
        },
        "native_efficiency_note": (
            "output_efficiency_last_pct is preserved separately; primary efficiency uses "
            "the model-matched geometry R100C and fixed 137.8 A copper loss"
        ),
    }
    _write_json_atomic(summary_path, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument(
        "--results",
        type=Path,
        nargs="+",
        required=True,
        help="One or more merged result CSVs or directories containing result CSVs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Defaults to <predictions directory>/fea_comparison.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir or args.predictions.parent / "fea_comparison"
    try:
        summary = compare(args.predictions, args.results, output_dir)
    except ComparisonError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
