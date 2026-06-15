from __future__ import annotations

from argparse import Namespace
import csv
from pathlib import Path
import tempfile
import unittest

import controller


def controller_args(**overrides: object) -> Namespace:
    values = {
        "jobs": 1,
        "cases": "",
        "repeat_every_hours": 0.0,
        "allow_duplicate_cases": False,
        "processes": 10,
        "loops_per_process": 1000,
        "total_count": 0,
        "max_cases": 200,
        "setup_only": False,
        "allow_over_budget": False,
    }
    values.update(overrides)
    return Namespace(**values)


class ControllerCasesGuardTests(unittest.TestCase):
    def test_accepts_random_generation_multiple_jobs(self) -> None:
        controller.validate_args(
            controller_args(
                jobs=10,
                cases="",
                repeat_every_hours=12.0,
                allow_over_budget=True,
            )
        )

    def test_rejects_explicit_cases_across_multiple_slurm_jobs(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "same explicit case CSV to multiple Slurm jobs"):
            controller.validate_args(controller_args(jobs=2, cases="replay.csv", repeat_every_hours=0.0))

    def test_rejects_explicit_cases_across_repeated_submit_cycles(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "same explicit case CSV in later submit cycles"):
            controller.validate_args(controller_args(jobs=1, cases="replay.csv", repeat_every_hours=12.0))

    def test_allows_explicit_duplicate_cases_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "replay.csv"
            with cases.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "one"})
            controller.validate_args(
                controller_args(
                    jobs=2,
                    cases=str(cases),
                    repeat_every_hours=12.0,
                    allow_duplicate_cases=True,
                    allow_over_budget=True,
                )
            )

    def test_rejects_non_positive_job_count(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--jobs must be at least 1"):
            controller.validate_args(controller_args(jobs=0))

    def test_rejects_default_random_generation_over_budget(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "planned cases per submit cycle=100000"):
            controller.validate_args(controller_args(jobs=10, processes=10, loops_per_process=1000))

    def test_rejects_repeated_analyze_cycle_even_within_single_cycle_budget(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--repeat-every-hours with analyze mode"):
            controller.validate_args(
                controller_args(
                    jobs=1,
                    processes=1,
                    loops_per_process=1,
                    repeat_every_hours=12.0,
                )
            )

    def test_accepts_repeated_setup_only_cycle_within_budget(self) -> None:
        controller.validate_args(
            controller_args(
                jobs=1,
                processes=1,
                loops_per_process=1,
                repeat_every_hours=12.0,
                setup_only=True,
            )
        )

    def test_counts_explicit_case_rows_against_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "replay.csv"
            with cases.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerows({"case_id": f"case_{index}"} for index in range(3))

            with self.assertRaisesRegex(RuntimeError, "planned cases per submit cycle=3"):
                controller.validate_args(
                    controller_args(
                        jobs=1,
                        cases=str(cases),
                        max_cases=2,
                    )
                )


if __name__ == "__main__":
    unittest.main()
