from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import filter_ipmsm_training_dataset as training_filter
import train_ipmsm_lightgbm as trainer


class FilterIpmsmTrainingDatasetTests(unittest.TestCase):
    def training_row(self, **overrides: str) -> dict[str, str]:
        row = {"case_id": "case_1", "status": "ok"}
        row.update({column: "1.0" for column in trainer.RAW_INPUT_COLUMNS})
        for column in trainer.REQUESTED_OUTPUT_COLUMNS:
            actual = trainer.OUTPUT_ALIASES.get(column, column)
            row[actual] = "2.0"
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

    def test_filter_training_rows_keeps_last_duplicate_and_rejects_bad_rows(self) -> None:
        rows = [
            self.training_row(case_id="duplicate", status="failed"),
            self.training_row(case_id="duplicate"),
            self.training_row(case_id="failed", status="failed"),
            self.training_row(case_id="bad_input", input_slot_num=""),
            self.training_row(case_id="bad_output", output_torque_last_avg_nm="nan"),
            self.training_row(case_id="bad_efficiency", output_efficiency_last_pct="120"),
        ]
        fieldnames = list(rows[0])

        kept_rows, summary = training_filter.filter_training_rows(rows, fieldnames)

        self.assertEqual([row["case_id"] for row in kept_rows], ["duplicate"])
        self.assertEqual(summary["rows_read"], 6)
        self.assertEqual(summary["rows_after_dedup"], 5)
        self.assertEqual(summary["duplicate_case_id_rows"], 1)
        self.assertEqual(summary["status_rejected_rows"], 1)
        self.assertEqual(summary["nonfinite_input_rows"], 1)
        self.assertEqual(summary["nonfinite_output_rows"], 1)
        self.assertEqual(summary["physical_sanity_rejected_rows"], 1)
        self.assertEqual(summary["rejected_rows"], 4)

    def test_filter_training_rows_rejects_legacy_efficiency_alias_out_of_range(self) -> None:
        rows = [
            self.training_row(case_id="ok_alias", output_efficiency_last_pct="", output_efficiency_last_pc="99"),
            self.training_row(case_id="bad_alias", output_efficiency_last_pct="", output_efficiency_last_pc="-1"),
        ]
        fieldnames = list(rows[0])

        kept_rows, summary = training_filter.filter_training_rows(rows, fieldnames)

        self.assertEqual([row["case_id"] for row in kept_rows], ["ok_alias"])
        self.assertEqual(summary["physical_sanity_rejected_rows"], 1)

    def test_filter_training_rows_reports_missing_input_columns(self) -> None:
        rows = [self.training_row()]
        fieldnames = [column for column in rows[0] if column != "input_slot_num"]

        with self.assertRaisesRegex(ValueError, "missing input columns"):
            training_filter.filter_training_rows(rows, fieldnames)

    def test_read_rows_normalizes_double_bom_fieldnames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "double_bom.csv"
            path.write_text("\ufeffcase_id,status\ncase_1,ok\n", encoding="utf-8-sig", newline="")

            rows, fieldnames = training_filter.read_rows([path])

        self.assertIn("case_id", fieldnames)
        self.assertNotIn("\ufeffcase_id", fieldnames)
        self.assertEqual(rows[0]["case_id"], "case_1")

    def test_cli_writes_filtered_dataset_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "results.csv"
            output_path = Path(tmp) / "training.csv"
            summary_path = Path(tmp) / "summary.csv"
            self.write_rows(
                input_path,
                [
                    self.training_row(case_id="ok"),
                    self.training_row(case_id="failed", status="failed"),
                ],
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = training_filter.main(
                    [
                        "--results",
                        str(input_path),
                        "--output",
                        str(output_path),
                        "--summary-output",
                        str(summary_path),
                        "--fail-on-filter",
                        "--min-kept-rows",
                        "1",
                        "--max-rejected-rows",
                        "1",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("training_dataset_filter passed", stdout.getvalue())
            with output_path.open("r", encoding="utf-8-sig", newline="") as file:
                output_rows = list(csv.DictReader(file))
            self.assertEqual([row["case_id"] for row in output_rows], ["ok"])
            with summary_path.open("r", encoding="utf-8-sig", newline="") as file:
                summary_rows = list(csv.DictReader(file))
            self.assertEqual(summary_rows[0]["kept_rows"], "1")
            self.assertEqual(summary_rows[0]["physical_sanity_rejected_rows"], "0")

    def test_cli_can_fail_on_filter_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "results.csv"
            output_path = Path(tmp) / "training.csv"
            self.write_rows(input_path, [self.training_row(case_id="failed", status="failed")])

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = training_filter.main(
                    [
                        "--results",
                        str(input_path),
                        "--output",
                        str(output_path),
                        "--fail-on-filter",
                        "--min-kept-rows",
                        "1",
                        "--max-rejected-rows",
                        "0",
                    ]
                )

            self.assertEqual(code, 1)
            self.assertIn("training_dataset_filter failed", stdout.getvalue())
            self.assertIn("kept_rows 0 < 1", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
