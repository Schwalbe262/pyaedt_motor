from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

import analyze_ipmsm_output_outliers as output_outliers


class AnalyzeIpmsmOutputOutliersTests(unittest.TestCase):
    def test_build_outlier_summary_reports_metric_and_combined_counts(self) -> None:
        rows = [{"target_a": str(value), "target_b": str(value)} for value in [1, 2, 3, 4, 100]]

        summary, combined = output_outliers.build_outlier_summary(
            rows,
            ("target_a", "target_b"),
            outlier_iqr_weight=1.5,
        )

        self.assertEqual(combined["rows"], "5")
        self.assertEqual(combined["rows_with_any_output_outlier"], "1")
        self.assertEqual(combined["rows_without_output_outliers"], "4")
        by_metric = {row["metric"]: row for row in summary}
        self.assertEqual(by_metric["target_a"]["outlier_rows"], "1")
        self.assertEqual(by_metric["target_b"]["outlier_rows"], "1")

    def test_cli_writes_summary_and_combined_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "rows.csv"
            summary_path = Path(tmp) / "summary.csv"
            combined_path = Path(tmp) / "combined.csv"
            with input_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["target_a"])
                writer.writeheader()
                for value in [1, 2, 3, 4, 100]:
                    writer.writerow({"target_a": str(value)})

            code = output_outliers.main(
                [
                    "--results",
                    str(input_path),
                    "--metrics",
                    "target_a",
                    "--summary-output",
                    str(summary_path),
                    "--combined-output",
                    str(combined_path),
                ]
            )

            self.assertEqual(code, 0)
            with summary_path.open("r", encoding="utf-8-sig", newline="") as file:
                summary_rows = list(csv.DictReader(file))
            with combined_path.open("r", encoding="utf-8-sig", newline="") as file:
                combined_rows = list(csv.DictReader(file))
            self.assertEqual(summary_rows[0]["metric"], "target_a")
            self.assertEqual(summary_rows[0]["outlier_rows"], "1")
            self.assertEqual(combined_rows[0]["rows_with_any_output_outlier"], "1")


if __name__ == "__main__":
    unittest.main()
