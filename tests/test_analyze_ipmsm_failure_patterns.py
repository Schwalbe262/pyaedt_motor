from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import analyze_ipmsm_failure_patterns as failure_patterns


class AnalyzeIpmsmFailurePatternsTests(unittest.TestCase):
    def write_cases(self, path: Path) -> None:
        rows = [
            {"case_id": "ok_low", "magnet_height_ratio": "0.82", "magnet_setback_ratio": "0.10", "constant": "1"},
            {"case_id": "failed_high", "magnet_height_ratio": "0.98", "magnet_setback_ratio": "0.16", "constant": "1"},
            {"case_id": "ok_mid", "magnet_height_ratio": "0.86", "magnet_setback_ratio": "0.14", "constant": "1"},
            {"case_id": "failed_high2", "magnet_height_ratio": "0.99", "magnet_setback_ratio": "0.17", "constant": "1"},
        ]
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_build_summary_rows_ranks_separated_numeric_features(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "cases.csv"
            self.write_cases(cases)
            rows = failure_patterns.read_rows(cases)

            summary = failure_patterns.build_summary_rows(rows, {2, 4})

            self.assertEqual(summary[0]["feature"], "magnet_height_ratio")
            self.assertNotIn("constant", {row["feature"] for row in summary})
            self.assertEqual(summary[0]["failed_min"], "0.98")
            self.assertEqual(summary[0]["ok_max"], "0.86")

    def test_evaluate_rules_reports_coverage_and_false_positives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "cases.csv"
            self.write_cases(cases)
            rows = failure_patterns.read_rows(cases)

            result = failure_patterns.evaluate_rules(
                rows,
                {2, 4},
                ["magnet_height_ratio>0.95,magnet_setback_ratio>0.15", "magnet_setback_ratio<0.11"],
            )

            self.assertEqual(result[0]["matched_rows"], "2")
            self.assertEqual(result[0]["matched_failed_rows"], "2")
            self.assertEqual(result[0]["matched_ok_rows"], "0")
            self.assertEqual(result[0]["failed_coverage"], "1")
            self.assertEqual(result[-1]["rule"], "__any__")
            self.assertEqual(result[-1]["matched_rows"], "3")
            self.assertEqual(result[-1]["matched_failed_rows"], "2")

    def test_cli_writes_summary_and_rule_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "cases.csv"
            summary = Path(tmp) / "summary.csv"
            rules = Path(tmp) / "rules.csv"
            self.write_cases(cases)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = failure_patterns.main(
                    [
                        "--cases",
                        str(cases),
                        "--failed-row-indexes",
                        "2,4",
                        "--summary-output",
                        str(summary),
                        "--rule-output",
                        str(rules),
                        "--evaluate-rule",
                        "magnet_height_ratio>0.95",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("failure_patterns rows=4 failed_rows=2", stdout.getvalue())
            self.assertTrue(summary.exists())
            self.assertTrue(rules.exists())
            with summary.open(encoding="utf-8") as file:
                self.assertEqual(len(list(csv.DictReader(file))), 2)
            with rules.open(encoding="utf-8") as file:
                self.assertEqual(len(list(csv.DictReader(file))), 1)

    def test_parse_indexes_rejects_zero(self) -> None:
        with self.assertRaisesRegex(ValueError, "1-based"):
            failure_patterns.parse_indexes("0,2")


if __name__ == "__main__":
    unittest.main()
