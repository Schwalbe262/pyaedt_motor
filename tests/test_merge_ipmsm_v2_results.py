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


if __name__ == "__main__":
    unittest.main()
