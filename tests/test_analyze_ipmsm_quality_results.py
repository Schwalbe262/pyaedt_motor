from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import analyze_ipmsm_quality_results as quality_results


class AnalyzeIpmsmQualityResultsTests(unittest.TestCase):
    def sample_rows(self) -> list[dict[str, str]]:
        return [
            {
                "case_id": "quality_baseline_beta_30p0",
                "input_quality_profile": "baseline",
                "input_source_case_id": "source_a",
                "input_base_rpm": "1200",
                "input_i_peak_a": "137.8",
                "input_beta_deg": "30",
                "status": "ok",
                "elapsed_s": "100",
                "output_torque_all_avg_nm": "10",
                "output_coreloss_all_avg_w": "20",
                "output_solidloss_all_avg_w": "5",
                "output_efficiency_all_pct": "90",
            },
            {
                "case_id": "quality_mesh_fine_beta_30p0",
                "input_quality_profile": "mesh_fine",
                "input_source_case_id": "source_a",
                "input_base_rpm": "1200",
                "input_i_peak_a": "137.8",
                "input_beta_deg": "30",
                "status": "ok",
                "elapsed_s": "125",
                "output_torque_all_avg_nm": "10.5",
                "output_coreloss_all_avg_w": "18",
                "output_solidloss_all_avg_w": "5.5",
                "output_efficiency_all_pct": "91",
            },
        ]

    def test_build_comparison_rows_adds_deltas_against_baseline(self) -> None:
        rows = quality_results.build_comparison_rows(
            self.sample_rows(),
            ("output_torque_all_avg_nm", "output_efficiency_all_pct"),
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["group_source_case_id"], "source_a")
        self.assertEqual(rows[1]["quality_profile"], "mesh_fine")
        self.assertEqual(rows[1]["baseline_case_id"], "quality_baseline_beta_30p0")
        self.assertEqual(rows[1]["elapsed_delta_s"], "25")
        self.assertEqual(rows[1]["elapsed_ratio"], "1.25")
        self.assertEqual(rows[1]["output_torque_all_avg_nm_delta"], "0.5")
        self.assertEqual(rows[1]["output_torque_all_avg_nm_pct_delta"], "5")
        self.assertEqual(rows[1]["output_efficiency_all_pct_delta"], "1")
        self.assertEqual(rows[1]["missing_required_outputs"], "")

    def test_missing_required_outputs_are_reported(self) -> None:
        row = self.sample_rows()[0]
        row["output_coreloss_all_avg_w"] = ""

        rows = quality_results.build_comparison_rows([row], ("output_torque_all_avg_nm",))

        self.assertEqual(rows[0]["missing_required_outputs"], "output_coreloss_all_avg_w")

    def test_replay_rows_compare_against_matching_source_geometry(self) -> None:
        rows = [
            {
                "case_id": "source_a_baseline",
                "input_quality_profile": "baseline",
                "input_source_case_id": "source_a",
                "input_base_rpm": "1200",
                "input_i_peak_a": "137.8",
                "input_beta_deg": "30",
                "elapsed_s": "100",
                "output_torque_all_avg_nm": "10",
                "output_coreloss_all_avg_w": "20",
                "output_solidloss_all_avg_w": "5",
            },
            {
                "case_id": "source_b_baseline",
                "input_quality_profile": "baseline",
                "input_source_case_id": "source_b",
                "input_base_rpm": "1200",
                "input_i_peak_a": "137.8",
                "input_beta_deg": "30",
                "elapsed_s": "110",
                "output_torque_all_avg_nm": "100",
                "output_coreloss_all_avg_w": "25",
                "output_solidloss_all_avg_w": "6",
            },
            {
                "case_id": "source_b_mesh_fine",
                "input_quality_profile": "mesh_fine",
                "input_source_case_id": "source_b",
                "input_base_rpm": "1200",
                "input_i_peak_a": "137.8",
                "input_beta_deg": "30",
                "elapsed_s": "120",
                "output_torque_all_avg_nm": "105",
                "output_coreloss_all_avg_w": "26",
                "output_solidloss_all_avg_w": "6",
            },
        ]

        comparison_rows = quality_results.build_comparison_rows(rows, ("output_torque_all_avg_nm",))
        source_b_mesh = [row for row in comparison_rows if row["case_id"] == "source_b_mesh_fine"][0]

        self.assertEqual(source_b_mesh["baseline_case_id"], "source_b_baseline")
        self.assertEqual(source_b_mesh["output_torque_all_avg_nm_baseline"], "100")
        self.assertEqual(source_b_mesh["output_torque_all_avg_nm_delta"], "5")

    def test_cli_writes_filtered_comparison_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "results.csv"
            output_path = Path(tmp) / "comparison.csv"
            with results_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(self.sample_rows()[0]))
                writer.writeheader()
                writer.writerows(self.sample_rows())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = quality_results.main(
                    [
                        "--results",
                        str(results_path),
                        "--output",
                        str(output_path),
                        "--metrics",
                        "output_torque_all_avg_nm,output_efficiency_all_pct",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("rows=2 comparisons=2", stdout.getvalue())
            with output_path.open("r", encoding="utf-8-sig", newline="") as file:
                comparison_rows = list(csv.DictReader(file))
            self.assertEqual(len(comparison_rows), 2)
            self.assertEqual(comparison_rows[1]["quality_profile"], "mesh_fine")
            self.assertIn("output_torque_all_avg_nm_pct_delta", comparison_rows[1])


if __name__ == "__main__":
    unittest.main()
