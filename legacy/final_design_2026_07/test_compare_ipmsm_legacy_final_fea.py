from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import tempfile
import unittest

import compare_ipmsm_legacy_final_fea as comparison


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def prediction(
    case_id: str,
    *,
    torque: float,
    core: float,
    solid: float,
    resistance: float,
) -> dict[str, object]:
    copper = 1.5 * resistance * comparison.CURRENT_PEAK_A**2
    total = core + solid + copper
    mechanical = torque * comparison.SPEED_RPM * 2.0 * math.pi / 60.0
    efficiency = 100.0 * mechanical / (mechanical + total)
    return {
        "case_id": case_id,
        "candidate_id": f"candidate_{case_id}",
        "selection_order": "1",
        "selection_role": "synthetic",
        "design_hash": f"hash_{case_id}",
        "predicted_efficiency_pct": efficiency,
        "predicted_nominal_torque_nm": torque,
        "predicted_core_loss_w": core,
        "predicted_solid_loss_w": solid,
        "predicted_copper_loss_100c_w": copper,
        "predicted_total_loss_physical_w": total,
        "phase_resistance_100c_ohm": resistance,
        "torque_constraint_nm": 71.351,
    }


def result(
    case_id: str,
    *,
    torque: float,
    core: float,
    solid: float,
    native_efficiency: float,
    voltage_margin: object = "",
    voltage_peak: object = "",
    voltage_limit: object = "",
    current: float = comparison.CURRENT_PEAK_A,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "status": "ok",
        "input_base_rpm": comparison.SPEED_RPM,
        "input_i_peak_a": current,
        "input_phase_resistance_ohm": 0.01,
        "output_torque_last_avg_nm": torque,
        "output_coreloss_last_avg_w": core,
        "output_solidloss_last_avg_w": solid,
        "output_efficiency_last_pct": native_efficiency,
        "output_phase_voltage_last_peak_abs_v": voltage_peak,
        "output_phase_voltage_limit_svpwm_v": voltage_limit,
        "output_voltage_margin_last_v": voltage_margin,
    }


PREDICTION_FIELDS = list(prediction("schema", torque=72.0, core=100.0, solid=20.0, resistance=0.015))
RESULT_FIELDS = list(result("schema", torque=72.0, core=100.0, solid=20.0, native_efficiency=90.0))


class CompareLegacyFinalFEATests(unittest.TestCase):
    def test_exact_case_join_recomputes_physical_efficiency_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predictions_path = root / "predictions.csv"
            results_path = root / "results.csv"
            output_dir = root / "comparison"
            predictions = [
                prediction("case_a", torque=72.0, core=100.0, solid=20.0, resistance=0.015),
                prediction("case_b", torque=75.0, core=110.0, solid=25.0, resistance=0.016),
            ]
            # Reverse result order to prove the join is by case_id, not row order.
            results = [
                result(
                    "case_b",
                    torque=74.0,
                    core=120.0,
                    solid=30.0,
                    native_efficiency=89.0,
                    voltage_peak=110.0,
                    voltage_limit=115.0,
                ),
                result(
                    "case_a",
                    torque=70.0,
                    core=90.0,
                    solid=-2.0,
                    native_efficiency=88.0,
                    voltage_margin=5.0,
                ),
            ]
            write_csv(predictions_path, predictions, PREDICTION_FIELDS)
            write_csv(results_path, results, RESULT_FIELDS)

            summary = comparison.compare(predictions_path, [results_path], output_dir)

            self.assertEqual(summary["comparison_rows"], 2)
            self.assertEqual(summary["torque_target"]["fea_pass_count"], 1)
            self.assertEqual(summary["clipped_negative_fea_loss_counts"]["solid"], 1)
            torque_metrics = summary["primary_error_metrics"]["torque_nm"]
            self.assertAlmostEqual(torque_metrics["MAE"], 1.5)
            self.assertAlmostEqual(torque_metrics["RMSE"], math.sqrt(2.5))
            self.assertAlmostEqual(torque_metrics["bias"], 1.5)
            self.assertAlmostEqual(torque_metrics["max_abs"], 2.0)
            self.assertEqual(summary["fea_voltage_margin"]["available_count"], 2)
            self.assertEqual(summary["fea_voltage_margin"]["pass_count"], 2)

            with (output_dir / comparison.CSV_NAME).open(
                encoding="utf-8", newline=""
            ) as handle:
                output_rows = list(csv.DictReader(handle))
            self.assertEqual([row["case_id"] for row in output_rows], ["case_a", "case_b"])
            first = output_rows[0]
            expected_copper = 1.5 * 0.015 * comparison.CURRENT_PEAK_A**2
            self.assertAlmostEqual(float(first["fea_copper_loss_100c_w"]), expected_copper)
            self.assertEqual(float(first["fea_solid_loss_physical_w"]), 0.0)
            self.assertEqual(float(first["fea_efficiency_native_pct"]), 88.0)
            mechanical = 70.0 * comparison.SPEED_RPM * 2.0 * math.pi / 60.0
            physical_total = 90.0 + expected_copper
            expected_efficiency = 100.0 * mechanical / (mechanical + physical_total)
            self.assertAlmostEqual(
                float(first["fea_efficiency_physical_pct"]), expected_efficiency
            )
            persisted = json.loads(
                (output_dir / comparison.SUMMARY_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["status"], "ok")

    def test_double_bom_case_header_and_derived_voltage_margin_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predictions_path = root / "predictions.csv"
            results_path = root / "results.csv"
            prediction_row = prediction(
                "case_bom", torque=73.0, core=100.0, solid=20.0, resistance=0.015
            )
            result_row = result(
                "case_bom",
                torque=72.0,
                core=95.0,
                solid=18.0,
                native_efficiency=90.0,
                voltage_peak=112.0,
                voltage_limit=115.0,
            )
            result_row["\ufeffcase_id"] = result_row.pop("case_id")
            bom_fields = ["\ufeffcase_id", *[name for name in RESULT_FIELDS if name != "case_id"]]
            write_csv(predictions_path, [prediction_row], PREDICTION_FIELDS)
            write_csv(results_path, [result_row], bom_fields)

            summary = comparison.compare(predictions_path, [results_path], root / "out")

            self.assertEqual(summary["comparison_rows"], 1)
            self.assertEqual(summary["fea_voltage_margin"]["minimum_v"], 3.0)

    def test_duplicate_exact_result_and_operating_point_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predictions_path = root / "predictions.csv"
            first_results = root / "first.csv"
            second_results = root / "second.csv"
            prediction_row = prediction(
                "case_dup", torque=73.0, core=100.0, solid=20.0, resistance=0.015
            )
            result_row = result(
                "case_dup", torque=72.0, core=95.0, solid=18.0, native_efficiency=90.0
            )
            write_csv(predictions_path, [prediction_row], PREDICTION_FIELDS)
            write_csv(first_results, [result_row], RESULT_FIELDS)
            write_csv(second_results, [result_row], RESULT_FIELDS)

            with self.assertRaisesRegex(comparison.ComparisonError, "duplicate selected result"):
                comparison.compare(
                    predictions_path, [first_results, second_results], root / "duplicate_out"
                )

            mismatched = dict(result_row, input_i_peak_a=130.0)
            write_csv(second_results, [mismatched], RESULT_FIELDS)
            with self.assertRaisesRegex(comparison.ComparisonError, "does not match 137.8"):
                comparison.compare(predictions_path, [second_results], root / "mismatch_out")


if __name__ == "__main__":
    unittest.main()
