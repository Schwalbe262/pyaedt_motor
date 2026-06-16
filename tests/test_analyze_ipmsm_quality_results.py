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

    def convergence_rows(self) -> list[dict[str, str]]:
        rows = self.sample_rows()
        rows.append(
            {
                "case_id": "quality_mesh_time_fine_beta_30p0",
                "input_quality_profile": "mesh_time_fine",
                "input_source_case_id": "source_a",
                "input_base_rpm": "1200",
                "input_i_peak_a": "137.8",
                "input_beta_deg": "30",
                "status": "ok",
                "elapsed_s": "150",
                "output_torque_all_avg_nm": "10.6",
                "output_coreloss_all_avg_w": "18.1",
                "output_solidloss_all_avg_w": "5.4",
                "output_efficiency_all_pct": "91.2",
            }
        )
        return rows

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
        self.assertEqual(rows[1]["physical_sanity_violations"], "")

    def test_missing_required_outputs_are_reported(self) -> None:
        row = self.sample_rows()[0]
        row["output_coreloss_all_avg_w"] = ""

        rows = quality_results.build_comparison_rows([row], ("output_torque_all_avg_nm",))

        self.assertEqual(rows[0]["missing_required_outputs"], "output_coreloss_all_avg_w")

    def test_read_rows_normalizes_double_bom_case_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "double_bom.csv"
            path.write_text("\ufeffcase_id,input_quality_profile\ncase_1,baseline\n", encoding="utf-8-sig", newline="")

            rows = quality_results.read_rows(path)

        self.assertEqual(rows[0]["case_id"], "case_1")
        self.assertNotIn("\ufeffcase_id", rows[0])

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

    def test_build_profile_summary_rows_aggregates_runtime_and_metric_deltas(self) -> None:
        comparison_rows = quality_results.build_comparison_rows(
            self.sample_rows(),
            ("output_torque_all_avg_nm", "output_efficiency_all_pct"),
        )

        summary_rows = quality_results.build_profile_summary_rows(
            comparison_rows,
            ("output_torque_all_avg_nm", "output_efficiency_all_pct"),
        )

        summary_by_profile = {row["quality_profile"]: row for row in summary_rows}
        self.assertEqual(summary_by_profile["baseline"]["rows"], "1")
        self.assertEqual(summary_by_profile["baseline"]["avg_elapsed_ratio"], "1")
        self.assertEqual(summary_by_profile["mesh_fine"]["rows_with_baseline"], "1")
        self.assertEqual(summary_by_profile["mesh_fine"]["rows_without_baseline"], "0")
        self.assertEqual(summary_by_profile["mesh_fine"]["avg_elapsed_ratio"], "1.25")
        self.assertEqual(summary_by_profile["mesh_fine"]["output_torque_all_avg_nm_avg_abs_pct_delta"], "5")
        self.assertEqual(summary_by_profile["mesh_fine"]["output_efficiency_all_pct_max_abs_pct_delta"], "1.11111111111")

    def test_build_profile_summary_rows_reports_missing_baselines(self) -> None:
        comparison_rows = quality_results.build_comparison_rows(
            [self.sample_rows()[1]],
            ("output_torque_all_avg_nm",),
        )

        summary_rows = quality_results.build_profile_summary_rows(comparison_rows, ("output_torque_all_avg_nm",))

        self.assertEqual(summary_rows[0]["quality_profile"], "mesh_fine")
        self.assertEqual(summary_rows[0]["rows_with_baseline"], "0")
        self.assertEqual(summary_rows[0]["rows_without_baseline"], "1")
        self.assertEqual(summary_rows[0]["output_torque_all_avg_nm_avg_abs_pct_delta"], "")

    def test_build_convergence_rows_ranks_fastest_profile_within_reference_tolerance(self) -> None:
        rows = quality_results.build_convergence_rows(
            self.convergence_rows(),
            ("output_torque_all_avg_nm", "output_efficiency_all_pct"),
            reference_profile="mesh_time_fine",
            pct_tolerance=2.0,
        )

        rows_by_profile = {row["quality_profile"]: row for row in rows}
        self.assertEqual(rows_by_profile["baseline"]["within_tolerance"], "no")
        self.assertEqual(rows_by_profile["baseline"]["rows_outside_tolerance"], "1")
        self.assertEqual(rows_by_profile["mesh_fine"]["within_tolerance"], "yes")
        self.assertEqual(rows_by_profile["mesh_fine"]["recommended_rank"], "1")
        self.assertEqual(rows_by_profile["mesh_fine"]["avg_elapsed_ratio_vs_reference"], "0.833333333333")
        self.assertEqual(rows_by_profile["mesh_time_fine"]["recommended_rank"], "2")
        self.assertEqual(rows_by_profile["mesh_time_fine"]["max_abs_pct_delta"], "0")

    def test_build_convergence_rows_requires_valid_reference(self) -> None:
        rows = quality_results.build_convergence_rows(
            self.sample_rows(),
            ("output_torque_all_avg_nm",),
            reference_profile="mesh_time_fine",
            pct_tolerance=2.0,
        )

        rows_by_profile = {row["quality_profile"]: row for row in rows}
        self.assertEqual(rows_by_profile["baseline"]["rows_without_reference"], "1")
        self.assertEqual(rows_by_profile["baseline"]["within_tolerance"], "no")
        self.assertEqual(rows_by_profile["mesh_fine"]["recommended_rank"], "")

    def test_incomplete_group_issues_report_missing_successful_profiles(self) -> None:
        rows = self.sample_rows()
        rows[1]["status"] = "failed"

        issues = quality_results.incomplete_group_issues(
            rows,
            ("baseline", "mesh_fine", "time_fine"),
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["group_source_case_id"], "source_a")
        self.assertEqual(issues[0]["present_profiles"], "baseline,mesh_fine")
        self.assertEqual(issues[0]["missing_profiles"], "mesh_fine,time_fine")

    def test_physical_sanity_violations_make_profile_incomplete(self) -> None:
        rows = self.sample_rows()
        rows[1]["output_efficiency_all_pct"] = "120"

        comparison_rows = quality_results.build_comparison_rows(rows, ("output_efficiency_all_pct",))
        issues = quality_results.incomplete_group_issues(rows, ("baseline", "mesh_fine"))

        mesh_row = [row for row in comparison_rows if row["quality_profile"] == "mesh_fine"][0]
        self.assertEqual(mesh_row["physical_sanity_violations"], "output_efficiency_all_pct")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["missing_profiles"], "mesh_fine")

    def test_filter_complete_group_rows_keeps_only_groups_with_required_successful_profiles(self) -> None:
        rows = self.sample_rows()
        rows.append(
            {
                **self.sample_rows()[0],
                "case_id": "source_b_baseline",
                "input_source_case_id": "source_b",
                "input_quality_profile": "baseline",
            }
        )

        filtered = quality_results.filter_complete_group_rows(rows, ("baseline", "mesh_fine"))

        self.assertEqual([row["case_id"] for row in filtered], ["quality_baseline_beta_30p0", "quality_mesh_fine_beta_30p0"])

    def test_cli_rejects_negative_convergence_tolerance(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                quality_results.main(
                    [
                        "--results",
                        "unused.csv",
                        "--output",
                        "unused_out.csv",
                        "--convergence-pct-tolerance",
                        "-1",
                    ]
                )

        self.assertEqual(error.exception.code, 2)

    def test_cli_can_fail_on_incomplete_quality_groups_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "results.csv"
            output_path = Path(tmp) / "comparison.csv"
            rows = self.sample_rows()[:1]
            with results_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    quality_results.main(
                        [
                            "--results",
                            str(results_path),
                            "--output",
                            str(output_path),
                            "--required-profiles",
                            "baseline,mesh_fine",
                            "--fail-on-incomplete-groups",
                        ]
                    )

        self.assertEqual(error.exception.code, 2)
        self.assertIn("incomplete quality group", stderr.getvalue())
        self.assertFalse(output_path.exists())

    def test_cli_complete_groups_only_filters_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "results.csv"
            output_path = Path(tmp) / "comparison.csv"
            rows = self.sample_rows()
            rows.append(
                {
                    **self.sample_rows()[0],
                    "case_id": "source_b_baseline",
                    "input_source_case_id": "source_b",
                    "input_quality_profile": "baseline",
                }
            )
            with results_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = quality_results.main(
                    [
                        "--results",
                        str(results_path),
                        "--output",
                        str(output_path),
                        "--required-profiles",
                        "baseline,mesh_fine",
                        "--complete-groups-only",
                        "--fail-on-incomplete-groups",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("groups 2->1", stdout.getvalue())
            with output_path.open("r", encoding="utf-8-sig", newline="") as file:
                comparison_rows = list(csv.DictReader(file))
            self.assertEqual(len(comparison_rows), 2)
            self.assertEqual({row["group_source_case_id"] for row in comparison_rows}, {"source_a"})

    def test_cli_complete_groups_only_fails_without_writing_when_none_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "results.csv"
            output_path = Path(tmp) / "comparison.csv"
            rows = self.sample_rows()[:1]
            with results_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    quality_results.main(
                        [
                            "--results",
                            str(results_path),
                            "--output",
                            str(output_path),
                            "--required-profiles",
                            "baseline,mesh_fine",
                            "--complete-groups-only",
                        ]
                    )

        self.assertEqual(error.exception.code, 2)
        self.assertIn("no complete quality groups", stderr.getvalue())
        self.assertFalse(output_path.exists())

    def test_cli_writes_filtered_comparison_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_results_path = Path(tmp) / "results_first.csv"
            second_results_path = Path(tmp) / "results_second.csv"
            output_path = Path(tmp) / "comparison.csv"
            summary_path = Path(tmp) / "profile_summary.csv"
            convergence_path = Path(tmp) / "convergence.csv"
            rows = self.convergence_rows()
            with first_results_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows[:2])
            with second_results_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows[2:])

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = quality_results.main(
                    [
                        "--results",
                        str(first_results_path),
                        str(second_results_path),
                        "--output",
                        str(output_path),
                        "--metrics",
                        "output_torque_all_avg_nm,output_efficiency_all_pct",
                        "--profile-summary-output",
                        str(summary_path),
                        "--convergence-output",
                        str(convergence_path),
                        "--reference-profile",
                        "mesh_time_fine",
                        "--convergence-pct-tolerance",
                        "2.0",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("rows=3 comparisons=3", stdout.getvalue())
            self.assertIn("profile summary row(s)", stdout.getvalue())
            self.assertIn("convergence row(s)", stdout.getvalue())
            with output_path.open("r", encoding="utf-8-sig", newline="") as file:
                comparison_rows = list(csv.DictReader(file))
            self.assertEqual(len(comparison_rows), 3)
            self.assertEqual(comparison_rows[1]["quality_profile"], "mesh_fine")
            self.assertIn("output_torque_all_avg_nm_pct_delta", comparison_rows[1])
            with summary_path.open("r", encoding="utf-8-sig", newline="") as file:
                summary_rows = list(csv.DictReader(file))
            self.assertEqual(len(summary_rows), 3)
            self.assertIn("output_torque_all_avg_nm_avg_abs_pct_delta", summary_rows[0])
            with convergence_path.open("r", encoding="utf-8-sig", newline="") as file:
                convergence_rows = list(csv.DictReader(file))
            self.assertEqual(len(convergence_rows), 3)
            self.assertIn("recommended_rank", convergence_rows[0])


if __name__ == "__main__":
    unittest.main()
