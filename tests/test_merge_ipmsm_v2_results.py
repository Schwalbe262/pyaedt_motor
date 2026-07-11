from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

import merge_ipmsm_v2_results as merger


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for column in row:
            if column not in fieldnames:
                fieldnames.append(column)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class MergeIpmsmV2ResultsTests(unittest.TestCase):
    def test_merge_requires_exact_coverage_and_preserves_plan_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.csv"
            first = root / "first.csv"
            second = root / "second.csv"
            write_rows(plan, [{"case_id": "b"}, {"case_id": "a"}])
            write_rows(first, [{"case_id": "a", "status": "ok", "value": "1"}])
            write_rows(second, [{"case_id": "b", "status": "ok", "other": "2"}])

            headers, rows = merger.merge_complete_results(plan, [first, second])

        self.assertEqual([row["case_id"] for row in rows], ["b", "a"])
        self.assertIn("value", headers)
        self.assertIn("other", headers)

    def test_merge_rejects_overlap_missing_and_non_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.csv"
            first = root / "first.csv"
            second = root / "second.csv"
            write_rows(plan, [{"case_id": "a"}, {"case_id": "b"}])
            write_rows(first, [{"case_id": "a", "status": "failed"}])
            write_rows(second, [{"case_id": "a", "status": "ok"}])

            with self.assertRaisesRegex(ValueError, "overlap"):
                merger.merge_complete_results(plan, [first, second])
            with self.assertRaisesRegex(ValueError, "missing=1.*non_ok=1"):
                merger.merge_complete_results(plan, [first])

    def test_multiple_case_plans_append_in_order_and_cli_remains_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage1 = root / "stage1.csv"
            stage2 = root / "stage2.csv"
            first = root / "first.csv"
            second = root / "second.csv"
            output = root / "merged.csv"
            write_rows(stage1, [{"case_id": "s1-b"}, {"case_id": "s1-a"}])
            write_rows(stage2, [{"case_id": "s2-a"}])
            write_rows(
                first,
                [
                    {"case_id": "s1-a", "status": "ok"},
                    {"case_id": "s1-b", "status": "ok"},
                ],
            )
            write_rows(second, [{"case_id": "s2-a", "status": "ok"}])

            result = merger.main(
                [
                    "--case-plan",
                    str(stage1),
                    "--case-plan",
                    str(stage2),
                    "--input",
                    str(first),
                    str(second),
                    "--output",
                    str(output),
                ]
            )
            _, rows = merger.read_csv(output)

        self.assertEqual(result, 0)
        self.assertEqual([row["case_id"] for row in rows], ["s1-b", "s1-a", "s2-a"])

    def test_multiple_case_plans_must_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage1 = root / "stage1.csv"
            stage2 = root / "stage2.csv"
            results = root / "results.csv"
            write_rows(stage1, [{"case_id": "same"}])
            write_rows(stage2, [{"case_id": "same"}])
            write_rows(results, [{"case_id": "same", "status": "ok"}])

            with self.assertRaisesRegex(ValueError, "case plans overlap"):
                merger.merge_complete_results([stage1, stage2], [results])

    def test_duplicate_or_overflow_result_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.csv"
            duplicate = root / "duplicate.csv"
            overflow = root / "overflow.csv"
            write_rows(plan, [{"case_id": "a"}])
            duplicate.write_text(
                "case_id,status,status\na,failed,ok\n",
                encoding="utf-8",
            )
            overflow.write_text(
                "case_id,status\na,ok,ignored\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate header"):
                merger.merge_complete_results(plan, [duplicate])
            with self.assertRaisesRegex(ValueError, "beyond its header"):
                merger.merge_complete_results(plan, [overflow])

    def test_write_csv_is_atomic_and_refuses_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "merged.csv"
            merger.write_csv(output, ["case_id", "status"], [{"case_id": "a", "status": "ok"}])
            before = output.read_bytes()

            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                merger.write_csv(
                    output,
                    ["case_id", "status"],
                    [{"case_id": "b", "status": "ok"}],
                )

            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
