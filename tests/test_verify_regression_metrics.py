from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import verify_regression_metrics


class VerifyRegressionMetricsTests(unittest.TestCase):
    def write_metrics(self, path: Path) -> None:
        rows = [
            {
                "target": "output_torque_last_avg_nm",
                "split": "test",
                "MAE": "1.0",
                "RMSE": "2.0",
                "R2": "0.96",
                "MAPE_pct": "3.0",
                "best_iteration": "100",
            },
            {
                "target": "output_coreloss_last_avg_w",
                "split": "test",
                "MAE": "5.0",
                "RMSE": "6.0",
                "R2": "0.72",
                "MAPE_pct": "8.0",
                "best_iteration": "80",
            },
            {
                "target": "output_torque_last_avg_nm",
                "split": "train",
                "MAE": "0.5",
                "RMSE": "1.0",
                "R2": "0.99",
                "MAPE_pct": "1.0",
                "best_iteration": "100",
            },
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_summarize_split_marks_failures_and_gaps(self) -> None:
        rows = [
            {"target": "ok", "split": "test", "R2": "0.97"},
            {"target": "bad", "split": "test", "R2": "0.80"},
        ]

        summary_rows, summary, failures = verify_regression_metrics.summarize_split(rows, 0.95)

        self.assertEqual(failures, 1)
        self.assertIn("targets=2 failures=1", summary)
        self.assertEqual(summary_rows[0]["target"], "bad")
        self.assertEqual(summary_rows[0]["status"], "fail")
        self.assertEqual(summary_rows[1]["status"], "pass")

    def test_cli_writes_summary_and_can_fail_on_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metrics_path = Path(tmp) / "metrics.csv"
            output_path = Path(tmp) / "verification.csv"
            self.write_metrics(metrics_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = verify_regression_metrics.main(
                    [
                        "--metrics",
                        str(metrics_path),
                        "--output",
                        str(output_path),
                        "--split",
                        "test",
                        "--r2-threshold",
                        "0.95",
                        "--fail-on-threshold",
                    ]
                )

            self.assertEqual(code, 1)
            self.assertIn("targets=2 failures=1", stdout.getvalue())
            with output_path.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["target"], "output_coreloss_last_avg_w")
            self.assertEqual(rows[0]["status"], "fail")
            self.assertEqual(rows[1]["status"], "pass")

    def test_missing_split_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metrics_path = Path(tmp) / "metrics.csv"
            output_path = Path(tmp) / "verification.csv"
            self.write_metrics(metrics_path)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    verify_regression_metrics.main(
                        [
                            "--metrics",
                            str(metrics_path),
                            "--output",
                            str(output_path),
                            "--split",
                            "holdout",
                        ]
                    )

            self.assertIn("no metric rows found", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
