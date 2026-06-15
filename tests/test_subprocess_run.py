from __future__ import annotations

import unittest

import subprocess_run


class SubprocessRunTests(unittest.TestCase):
    def test_validate_explicit_case_plan_rejects_duplicate_ids_before_split(self) -> None:
        rows = [
            {"case_id": "dup"},
            {"case_id": "unique"},
            {"case_id": "dup"},
        ]

        with self.assertRaisesRegex(RuntimeError, "duplicate case_id"):
            subprocess_run.validate_explicit_case_plan(rows, max_cases=200, allow_over_budget=False)

    def test_validate_explicit_case_plan_rejects_over_budget_rows(self) -> None:
        rows = [{"case_id": f"case_{index}"} for index in range(3)]

        with self.assertRaisesRegex(RuntimeError, "exceeding --max-cases=2"):
            subprocess_run.validate_explicit_case_plan(rows, max_cases=2, allow_over_budget=False)

        subprocess_run.validate_explicit_case_plan(rows, max_cases=2, allow_over_budget=True)

    def test_split_cases_can_distribute_duplicates_across_chunks(self) -> None:
        rows = [
            {"case_id": "dup"},
            {"case_id": "dup"},
            {"case_id": "other"},
        ]

        chunks = subprocess_run.split_cases(rows, processes=2)

        self.assertEqual([row["case_id"] for row in chunks[0]], ["dup", "other"])
        self.assertEqual([row["case_id"] for row in chunks[1]], ["dup"])


if __name__ == "__main__":
    unittest.main()
