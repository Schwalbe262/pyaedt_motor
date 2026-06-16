from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import analyze_ipmsm_dataset_quality as dataset_quality


class AnalyzeIpmsmDatasetQualityTests(unittest.TestCase):
    def write_results(self, path: Path) -> None:
        rows = [
            {
                "case_id": "case_1",
                "status": "ok",
                "elapsed_s": "10",
                "output_torque_all_avg_nm": "1.0",
                "output_coreloss_all_avg_w": "2.0",
                "output_solidloss_all_avg_w": "3.0",
                "output_efficiency_all_pct": "90",
                "error": "",
            },
            {
                "case_id": "case_1",
                "status": "failed",
                "elapsed_s": "4",
                "output_torque_all_avg_nm": "",
                "output_coreloss_all_avg_w": "nan",
                "output_solidloss_all_avg_w": "3.0",
                "output_efficiency_all_pct": "80",
                "error": "RuntimeError('Missing required transient output metrics')",
            },
            {
                "case_id": "case_2",
                "status": "ok",
                "elapsed_s": "12",
                "output_torque_all_avg_nm": "1.2",
                "output_coreloss_all_avg_w": "2.2",
                "output_solidloss_all_avg_w": "3.2",
                "output_efficiency_all_pct": "120",
                "error": "",
            },
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_analyze_file_summarizes_status_missing_outputs_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"
            self.write_results(path)

            summary = dataset_quality.analyze_file(path, dataset_quality.DEFAULT_REQUIRED_OUTPUTS).summary_row("file", str(path))

            self.assertEqual(summary["rows"], "3")
            self.assertEqual(summary["unique_case_ids"], "2")
            self.assertEqual(summary["duplicate_case_ids"], "1")
            self.assertEqual(summary["status_ok"], "2")
            self.assertEqual(summary["status_failed"], "1")
            self.assertEqual(summary["required_complete_rows"], "2")
            self.assertEqual(summary["missing_required_rows"], "1")
            self.assertIn("output_torque_all_avg_nm:1", summary["missing_required_by_column"])
            self.assertEqual(summary["physical_sanity_violation_rows"], "1")
            self.assertEqual(summary["physical_sanity_violations_by_column"], "output_efficiency_all_pct:1")
            self.assertEqual(summary["elapsed_min_s"], "4")
            self.assertEqual(summary["elapsed_avg_s"], "8.66666666667")
            self.assertEqual(summary["elapsed_max_s"], "12")

    def test_analyze_file_normalizes_double_bom_case_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "double_bom.csv"
            path.write_text(
                "\ufeffcase_id,status,output_torque_all_avg_nm,output_coreloss_all_avg_w,output_solidloss_all_avg_w\n"
                "case_1,ok,1,2,3\n"
                "case_1,ok,1,2,3\n",
                encoding="utf-8-sig",
                newline="",
            )

            summary = dataset_quality.analyze_file(path, dataset_quality.DEFAULT_REQUIRED_OUTPUTS).summary_row("file", str(path))

        self.assertEqual(summary["unique_case_ids"], "1")
        self.assertEqual(summary["duplicate_case_ids"], "1")

    def test_quality_gate_failures_report_threshold_misses(self) -> None:
        summary = {
            "required_complete_rows": "2",
            "missing_required_rows": "1",
            "duplicate_case_ids": "1",
            "status_failed": "1",
            "physical_sanity_violation_rows": "1",
        }

        failures = dataset_quality.quality_gate_failures(
            summary,
            min_required_complete_rows=3,
            max_missing_required_rows=0,
            max_duplicate_case_ids=0,
            max_failed_rows=0,
            max_physical_sanity_violation_rows=0,
        )

        self.assertEqual(
            failures,
            [
                "required_complete_rows 2 < 3",
                "missing_required_rows 1 > 0",
                "duplicate_case_ids 1 > 0",
                "status_failed 1 > 0",
                "physical_sanity_violation_rows 1 > 0",
            ],
        )

    def test_cli_writes_file_and_combined_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.csv"
            second = Path(tmp) / "second.csv"
            output = Path(tmp) / "summary.csv"
            self.write_results(first)
            self.write_results(second)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = dataset_quality.main(["--results", str(first), str(second), "--output", str(output)])

            self.assertEqual(code, 0)
            self.assertIn("dataset_quality rows=6", stdout.getvalue())
            with output.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[-1]["scope"], "combined")
            self.assertEqual(rows[-1]["duplicate_case_ids"], "4")

    def test_cli_can_fail_on_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "results.csv"
            output = Path(tmp) / "summary.csv"
            self.write_results(result_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = dataset_quality.main(
                    [
                        "--results",
                        str(result_path),
                        "--output",
                        str(output),
                        "--fail-on-quality",
                        "--min-required-complete-rows",
                        "3",
                        "--max-missing-required-rows",
                        "0",
                        "--max-duplicate-case-ids",
                        "0",
                        "--max-failed-rows",
                        "0",
                        "--max-physical-sanity-violation-rows",
                        "0",
                    ]
                )

            self.assertEqual(code, 1)
            self.assertIn("quality_gate failed", stdout.getvalue())
            self.assertIn("missing_required_rows 1 > 0", stdout.getvalue())
            self.assertIn("physical_sanity_violation_rows 1 > 0", stdout.getvalue())

    def test_cli_passes_quality_gate_when_thresholds_are_met(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "results.csv"
            output = Path(tmp) / "summary.csv"
            self.write_results(result_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = dataset_quality.main(
                    [
                        "--results",
                        str(result_path),
                        "--output",
                        str(output),
                        "--fail-on-quality",
                        "--min-required-complete-rows",
                        "2",
                        "--max-missing-required-rows",
                        "1",
                        "--max-duplicate-case-ids",
                        "1",
                        "--max-failed-rows",
                        "1",
                        "--max-physical-sanity-violation-rows",
                        "1",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("quality_gate passed", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
