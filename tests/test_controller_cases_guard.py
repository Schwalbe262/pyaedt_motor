from __future__ import annotations

from argparse import Namespace
import unittest

import controller


def controller_args(**overrides: object) -> Namespace:
    values = {
        "jobs": 1,
        "cases": "",
        "repeat_every_hours": 0.0,
        "allow_duplicate_cases": False,
    }
    values.update(overrides)
    return Namespace(**values)


class ControllerCasesGuardTests(unittest.TestCase):
    def test_accepts_random_generation_multiple_jobs(self) -> None:
        controller.validate_args(controller_args(jobs=10, cases="", repeat_every_hours=12.0))

    def test_rejects_explicit_cases_across_multiple_slurm_jobs(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "same explicit case CSV to multiple Slurm jobs"):
            controller.validate_args(controller_args(jobs=2, cases="replay.csv", repeat_every_hours=0.0))

    def test_rejects_explicit_cases_across_repeated_submit_cycles(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "same explicit case CSV in later submit cycles"):
            controller.validate_args(controller_args(jobs=1, cases="replay.csv", repeat_every_hours=12.0))

    def test_allows_explicit_duplicate_cases_when_requested(self) -> None:
        controller.validate_args(
            controller_args(
                jobs=2,
                cases="replay.csv",
                repeat_every_hours=12.0,
                allow_duplicate_cases=True,
            )
        )

    def test_rejects_non_positive_job_count(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--jobs must be at least 1"):
            controller.validate_args(controller_args(jobs=0))


if __name__ == "__main__":
    unittest.main()
