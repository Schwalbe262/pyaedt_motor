from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import summarize_ipmsm_partial_replay as partial_summary
import train_ipmsm_lightgbm as trainer


class SummarizeIpmsmPartialReplayTests(unittest.TestCase):
    def training_row(self, **overrides: str) -> dict[str, str]:
        row = {"case_id": "case_1", "status": "ok"}
        row.update({column: "1.0" for column in trainer.RAW_INPUT_COLUMNS})
        for column in trainer.REQUESTED_OUTPUT_COLUMNS:
            actual = trainer.OUTPUT_ALIASES.get(column, column)
            row[actual] = "2.0"
        row.update(
            {
                "output_torque_all_avg_nm": "2.0",
                "output_coreloss_all_avg_w": "2.0",
                "output_solidloss_all_avg_w": "2.0",
                "output_efficiency_all_pct": "50.0",
            }
        )
        row.update(overrides)
        return row

    def write_rows(self, path: Path, rows: list[dict[str, str]]) -> None:
        fieldnames: list[str] = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_summarize_partial_replay_computes_gate_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.csv"
            results = Path(tmp) / "results.csv"
            self.write_rows(base, [self.training_row(case_id="base_1"), self.training_row(case_id="base_2")])
            self.write_rows(
                results,
                [
                    self.training_row(case_id="new_1"),
                    self.training_row(case_id="duplicate"),
                    self.training_row(case_id="duplicate"),
                    self.training_row(
                        case_id="failed",
                        status="failed",
                        output_torque_all_avg_nm="",
                        output_torque_last_avg_nm="",
                    ),
                ],
            )

            summary = partial_summary.summarize_partial_replay([results], base_training=base)

        self.assertEqual(summary["result_rows"], 4)
        self.assertEqual(summary["result_ok_rows"], 3)
        self.assertEqual(summary["result_failed_rows"], 1)
        self.assertEqual(summary["result_missing_required_rows"], 1)
        self.assertEqual(summary["result_duplicate_case_id_rows"], 1)
        self.assertEqual(summary["combined_rows_read"], 6)
        self.assertEqual(summary["combined_rows_after_dedup"], 5)
        self.assertEqual(summary["combined_kept_rows"], 4)
        self.assertEqual(summary["combined_rejected_rows"], 1)
        self.assertEqual(summary["new_kept_rows"], 2)
        self.assertEqual(summary["quality_min_required_complete_rows"], 3)
        self.assertEqual(summary["filter_min_kept_rows"], 4)
        self.assertEqual(summary["filter_max_duplicate_case_id_rows"], 1)

    def test_cli_writes_summary_and_threshold_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.csv"
            results = Path(tmp) / "results.csv"
            output = Path(tmp) / "summary.csv"
            self.write_rows(base, [self.training_row(case_id="base_1")])
            self.write_rows(results, [self.training_row(case_id="new_1")])

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = partial_summary.main(
                    [
                        "--results",
                        str(results),
                        "--base-training",
                        str(base),
                        "--summary-output",
                        str(output),
                    ]
                )

            with output.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))

        self.assertEqual(code, 0)
        self.assertIn("partial_replay_summary result_rows=1", stdout.getvalue())
        self.assertIn("partial_replay_thresholds", stdout.getvalue())
        self.assertEqual(rows[0]["combined_kept_rows"], "2")
        self.assertEqual(rows[0]["filter_min_kept_rows"], "2")


if __name__ == "__main__":
    unittest.main()
