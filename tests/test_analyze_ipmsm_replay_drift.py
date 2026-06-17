from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

import analyze_ipmsm_replay_drift as replay_drift


class AnalyzeIpmsmReplayDriftTests(unittest.TestCase):
    def source_row(self) -> dict[str, str]:
        return {
            "case_id": "source_1",
            "status": "ok",
            "output_torque_last_avg_nm": "100",
            "output_efficiency_last_pct": "80",
        }

    def replay_row(self) -> dict[str, str]:
        return {
            "case_id": "replay_1",
            "status": "ok",
            "input_quality_profile": "mesh_time_fine",
            "input_source_case_id": "source_1",
            "input_base_rpm": "3000",
            "input_i_peak_a": "100",
            "input_beta_deg": "30",
            "output_torque_last_avg_nm": "110",
            "output_efficiency_last_pct": "76",
        }

    def test_build_drift_records_matches_replay_to_source_case_id(self) -> None:
        records, counters = replay_drift.build_drift_records(
            [self.source_row(), self.replay_row()],
            ("output_torque_last_avg_nm", "output_efficiency_last_pct"),
        )

        self.assertEqual(counters["replay_rows"], 1)
        self.assertEqual(counters["matched_replay_rows"], 1)
        self.assertEqual(counters["records"], 2)
        by_metric = {record.metric: record for record in records}
        self.assertAlmostEqual(by_metric["output_torque_last_avg_nm"].pct_delta, 10.0)
        self.assertAlmostEqual(by_metric["output_efficiency_last_pct"].pct_delta, -5.0)
        self.assertEqual(by_metric["output_torque_last_avg_nm"].group_values["input_base_rpm"], "3000")

    def test_build_drift_records_counts_unmatched_replay_rows(self) -> None:
        replay = self.replay_row()
        replay["input_source_case_id"] = "missing"

        records, counters = replay_drift.build_drift_records(
            [self.source_row(), replay],
            ("output_torque_last_avg_nm",),
        )

        self.assertEqual(records, [])
        self.assertEqual(counters["unmatched_replay_rows"], 1)

    def test_summary_and_outlier_rows_report_target_specific_drift(self) -> None:
        records, _ = replay_drift.build_drift_records(
            [self.source_row(), self.replay_row()],
            ("output_torque_last_avg_nm", "output_efficiency_last_pct"),
        )

        summary = replay_drift.build_summary_rows(records, pct_threshold=7.0)
        by_metric = {row["metric"]: row for row in summary}
        self.assertEqual(by_metric["output_torque_last_avg_nm"]["over_threshold_rows"], "1")
        self.assertEqual(by_metric["output_torque_last_avg_nm"]["mean_abs_delta"], "10")
        self.assertEqual(by_metric["output_efficiency_last_pct"]["over_threshold_rows"], "0")

        outliers = replay_drift.build_outlier_rows(records, pct_threshold=7.0, max_rows=10)
        self.assertEqual(len(outliers), 1)
        self.assertEqual(outliers[0]["metric"], "output_torque_last_avg_nm")
        self.assertEqual(outliers[0]["source_case_id"], "source_1")

    def test_cli_writes_summary_and_outlier_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.csv"
            summary_path = Path(tmp) / "summary.csv"
            outlier_path = Path(tmp) / "outliers.csv"
            fieldnames = [
                "case_id",
                "status",
                "input_quality_profile",
                "input_source_case_id",
                "input_base_rpm",
                "input_i_peak_a",
                "input_beta_deg",
                "output_torque_last_avg_nm",
                "output_efficiency_last_pct",
            ]
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(self.source_row())
                writer.writerow(self.replay_row())

            code = replay_drift.main(
                [
                    "--results",
                    str(path),
                    "--metrics",
                    "output_torque_last_avg_nm,output_efficiency_last_pc",
                    "--pct-threshold",
                    "7",
                    "--summary-output",
                    str(summary_path),
                    "--outliers-output",
                    str(outlier_path),
                ]
            )

            self.assertEqual(code, 0)
            with summary_path.open("r", encoding="utf-8-sig", newline="") as file:
                summary_rows = list(csv.DictReader(file))
            with outlier_path.open("r", encoding="utf-8-sig", newline="") as file:
                outlier_rows = list(csv.DictReader(file))
            self.assertEqual(len(summary_rows), 2)
            self.assertEqual(outlier_rows[0]["metric"], "output_torque_last_avg_nm")


if __name__ == "__main__":
    unittest.main()
