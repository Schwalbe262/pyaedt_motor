from __future__ import annotations

import csv
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import subprocess_run


class SubprocessRunTests(unittest.TestCase):
    def test_build_command_forwards_full_model_and_canonical_beta_contract(self) -> None:
        args = SimpleNamespace(
            python="python",
            script=Path("run_ipmsm_batch.py"),
            cores_per_process=4,
            simulation_dir=Path("simulation"),
            result_csv=Path("results.csv"),
            symmetry_factor=1,
            model_extent="full_360",
            beta_convention="dq_current_advance_v2",
            electrical_zero_deg=7.5,
            analyze=False,
            non_graphical=True,
            cleanup_linux=False,
            periodic_boundary=False,
        )

        command = subprocess_run.build_command(args, process_index=1, count=1, cases_path=None)

        self.assertEqual(command[command.index("--symmetry-factor") + 1], "1")
        self.assertEqual(command[command.index("--model-extent") + 1], "full_360")
        self.assertEqual(command[command.index("--beta-convention") + 1], "dq_current_advance_v2")
        self.assertEqual(command[command.index("--electrical-zero-deg") + 1], "7.5")

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

    def test_validate_explicit_case_plan_rejects_bad_inputs_before_split(self) -> None:
        rows = [{"case_id": "bad_mesh", "mesh_band_elements": "0"}]

        with self.assertRaisesRegex(RuntimeError, "case plan row bad_mesh has invalid inputs"):
            subprocess_run.validate_explicit_case_plan(rows, max_cases=200, allow_over_budget=False)

    def test_read_cases_normalizes_blank_explicit_case_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id", "id", "beta_deg"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"case_id": "", "id": "", "beta_deg": "10"},
                        {"case_id": "", "id": "legacy_id", "beta_deg": "20"},
                        {"case_id": "explicit_id", "id": "ignored", "beta_deg": "30"},
                    ]
                )

            rows = subprocess_run.read_cases(cases_path)

        self.assertEqual([row["case_id"] for row in rows], ["case_0001", "legacy_id", "explicit_id"])
        subprocess_run.validate_explicit_case_plan(rows, max_cases=200, allow_over_budget=False)

    def test_split_cases_can_distribute_duplicates_across_chunks(self) -> None:
        rows = [
            {"case_id": "dup"},
            {"case_id": "dup"},
            {"case_id": "other"},
        ]

        chunks = subprocess_run.split_cases(rows, processes=2)

        self.assertEqual([row["case_id"] for row in chunks[0]], ["dup", "other"])
        self.assertEqual([row["case_id"] for row in chunks[1]], ["dup"])

    def test_simulation_id_from_env_requires_positive_integer(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires SIMULATION_ID"):
            subprocess_run.simulation_id_from_env({})

        with self.assertRaisesRegex(RuntimeError, "positive integer"):
            subprocess_run.simulation_id_from_env({"SIMULATION_ID": "0"})

        self.assertEqual(subprocess_run.simulation_id_from_env({"SIMULATION_ID": "12"}), 12)

    def test_select_case_for_simulation_id_uses_one_based_index(self) -> None:
        rows = [{"case_id": "first"}, {"case_id": "second"}]

        self.assertEqual(subprocess_run.select_case_for_simulation_id(rows, 2)["case_id"], "second")
        with self.assertRaisesRegex(RuntimeError, "outside the explicit case plan"):
            subprocess_run.select_case_for_simulation_id(rows, 3)


if __name__ == "__main__":
    unittest.main()
