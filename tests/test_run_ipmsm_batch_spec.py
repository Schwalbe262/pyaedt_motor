from __future__ import annotations

import builtins
import csv
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import run_ipmsm_batch


def fixed_geometry_result_row(**overrides: str) -> dict[str, str]:
    row = {
        "input_slot_num": "12",
        "input_pole_num": "8",
        "input_stator_outer_radius": "155.0",
        "input_stator_back_yoke_thick_ratio": "0.142",
        "input_stator_back_yoke_thick": "22.01",
        "input_stator_inner_ratio": "0.513",
        "input_stator_inner_radius": "79.515",
        "input_stator_shoe_thick": "1.1",
        "input_stator_teeth_length_ratio": "0.847",
        "input_stator_teeth_length": "45.293325",
        "input_stator_teeth_width": "33.931477685988845",
        "input_stator_gap": "2.43",
        "input_rotator_gap": "1.54",
        "input_shaft_ratio": "0.516",
        "input_magnet_shield_thick": "1.435",
        "input_magnet_setback_ratio": "0.163",
        "input_magnet_thick_ratio": "0.313",
        "input_magnet_height_ratio": "1.0",
    }
    row.update(overrides)
    return row


class RunIpmsmBatchSpecTests(unittest.TestCase):
    def test_result_schema_includes_quality_profile(self) -> None:
        self.assertIn("input_quality_profile", run_ipmsm_batch.RESULT_COLUMN_ORDER)

    def test_result_schema_includes_geometry_replay_columns(self) -> None:
        self.assertIn("input_geometry_mode", run_ipmsm_batch.RESULT_COLUMN_ORDER)
        self.assertIn("input_source_case_id", run_ipmsm_batch.RESULT_COLUMN_ORDER)
        self.assertIn("input_source_result_path", run_ipmsm_batch.RESULT_COLUMN_ORDER)
        self.assertIn("input_stator_teeth_width_ratio", run_ipmsm_batch.RESULT_COLUMN_ORDER)
        self.assertIn("input_slot_opening_ratio", run_ipmsm_batch.RESULT_COLUMN_ORDER)
        self.assertIn("input_magnet_space_height_ratio", run_ipmsm_batch.RESULT_COLUMN_ORDER)

    def test_result_schema_includes_missing_required_outputs(self) -> None:
        self.assertIn("missing_required_outputs", run_ipmsm_batch.RESULT_COLUMN_ORDER)

    def test_build_spec_accepts_mesh_override_columns(self) -> None:
        spec = run_ipmsm_batch.build_spec(
            {
                "steps_per_period": "120",
                "transient_periods": "8",
                "mesh_magnet_elements": "80",
                "mesh_rotor_elements": "700",
                "mesh_stator_elements": "720",
                "mesh_winding_elements": "90",
                "mesh_band_elements": "1400",
            }
        )

        self.assertEqual(spec.steps_per_period, 120)
        self.assertEqual(spec.transient_periods, 8)
        self.assertEqual(
            spec.mesh_elements,
            {
                "magnet": 80,
                "rotor": 700,
                "stator": 720,
                "winding": 90,
                "band": 1400,
            },
        )

    def test_build_spec_rejects_non_positive_mesh_counts(self) -> None:
        with self.assertRaisesRegex(ValueError, "mesh element count must be >= 1"):
            run_ipmsm_batch.build_spec({"mesh_band_elements": "0"})

    def test_extract_fixed_geometry_from_existing_result_columns(self) -> None:
        fixed = run_ipmsm_batch.extract_fixed_geometry(fixed_geometry_result_row())

        self.assertEqual(fixed["slot_num"], 12)
        self.assertEqual(fixed["pole_num"], 8)
        self.assertAlmostEqual(fixed["stator_teeth_width_ratio"], 0.722, places=3)
        self.assertEqual(fixed["slot_opening_ratio"], 0.09)
        self.assertEqual(fixed["magnet_space_height_ratio"], 1.0)

    def test_extract_fixed_geometry_rejects_incomplete_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed geometry columns are incomplete"):
            run_ipmsm_batch.extract_fixed_geometry({"input_slot_num": "12"})

    def test_extract_fixed_geometry_rejects_non_integer_topology(self) -> None:
        with self.assertRaisesRegex(ValueError, "slot_num must be a positive integer"):
            run_ipmsm_batch.extract_fixed_geometry(fixed_geometry_result_row(input_slot_num="12.5"))

    def test_extract_fixed_geometry_rejects_infeasible_rotor_radius(self) -> None:
        with self.assertRaisesRegex(ValueError, "rotor_radius must be > 0"):
            run_ipmsm_batch.extract_fixed_geometry(fixed_geometry_result_row(input_rotator_gap="90"))

    def test_extract_fixed_geometry_rejects_stator_gap_overlap(self) -> None:
        with self.assertRaisesRegex(ValueError, "stator_airgap_clearance must be > 0"):
            run_ipmsm_batch.extract_fixed_geometry(fixed_geometry_result_row(input_stator_gap="20"))

    def test_extract_fixed_geometry_rejects_magnet_radial_overlap(self) -> None:
        with self.assertRaisesRegex(ValueError, "magnet_radial_clearance must be > 0"):
            run_ipmsm_batch.extract_fixed_geometry(
                fixed_geometry_result_row(
                    input_magnet_setback_ratio="0.8",
                    input_magnet_thick_ratio="0.5",
                )
            )

    def test_extract_fixed_geometry_rejects_infeasible_magnet_height(self) -> None:
        with self.assertRaisesRegex(ValueError, "magnet_height must be > 0"):
            run_ipmsm_batch.extract_fixed_geometry(fixed_geometry_result_row(input_magnet_shield_thick="80"))

    def test_missing_required_output_metrics_detects_missing_and_nan_values(self) -> None:
        missing = run_ipmsm_batch.missing_required_output_metrics(
            {
                "output_torque_all_avg_nm": "nan",
                "output_coreloss_all_avg_w": "12.5",
            }
        )

        self.assertEqual(
            missing,
            ["output_torque_all_avg_nm", "output_solidloss_all_avg_w"],
        )

    def test_validate_case_plan_rejects_duplicate_case_ids(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duplicate case_id"):
            run_ipmsm_batch.validate_case_plan(
                [{"case_id": "dup"}, {"case_id": "dup"}],
                max_cases=200,
            )

    def test_validate_case_plan_rejects_over_budget_rows(self) -> None:
        cases = [{"case_id": f"case_{index}"} for index in range(3)]

        with self.assertRaisesRegex(RuntimeError, "exceeding --max-cases=2"):
            run_ipmsm_batch.validate_case_plan(cases, max_cases=2)

        run_ipmsm_batch.validate_case_plan(cases, max_cases=2, allow_over_budget=True)

    def test_load_cases_normalizes_blank_explicit_case_ids(self) -> None:
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

            cases = run_ipmsm_batch.load_cases(str(cases_path), count=99)

        self.assertEqual([case["case_id"] for case in cases], ["case_0001", "legacy_id", "explicit_id"])
        run_ipmsm_batch.validate_case_plan(cases, max_cases=200)

    def test_create_simulation_name_ignores_stale_low_counter(self) -> None:
        original_base_dir = run_ipmsm_batch.BASE_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "base"
            simulation_dir = Path(tmp) / "simulation"
            base_dir.mkdir()
            (simulation_dir / "simulation1").mkdir(parents=True)
            (simulation_dir / "simulation2").mkdir()
            (base_dir / "simulation_num.txt").write_text("1", encoding="utf-8")
            try:
                run_ipmsm_batch.BASE_DIR = base_dir
                sim = run_ipmsm_batch.Simulation(desktop=None)
                sim.create_simulation_name(simulation_dir)
            finally:
                run_ipmsm_batch.BASE_DIR = original_base_dir

            self.assertEqual(sim.PROJECT_NAME, "simulation3")
            self.assertEqual((base_dir / "simulation_num.txt").read_text(encoding="utf-8"), "4")

    def test_create_simulation_name_honors_future_counter(self) -> None:
        original_base_dir = run_ipmsm_batch.BASE_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "base"
            simulation_dir = Path(tmp) / "simulation"
            base_dir.mkdir()
            (simulation_dir / "simulation1").mkdir(parents=True)
            (base_dir / "simulation_num.txt").write_text("9", encoding="utf-8")
            try:
                run_ipmsm_batch.BASE_DIR = base_dir
                sim = run_ipmsm_batch.Simulation(desktop=None)
                sim.create_simulation_name(simulation_dir)
            finally:
                run_ipmsm_batch.BASE_DIR = original_base_dir

            self.assertEqual(sim.PROJECT_NAME, "simulation9")
            self.assertEqual((base_dir / "simulation_num.txt").read_text(encoding="utf-8"), "10")

    def test_run_one_case_writes_failed_row_when_pyaedt_import_fails(self) -> None:
        original_import = builtins.__import__

        def fail_pyaedt_import(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("pyaedt_module"):
                raise ModuleNotFoundError("forced missing pyaedt_module")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            result_csv = Path(tmp) / "results.csv"
            options = run_ipmsm_batch.RunnerOptions(
                simulation_dir=str(Path(tmp) / "simulation"),
                result_csv=str(result_csv),
                analyze=False,
                non_graphical=True,
                cleanup_linux=False,
                symmetry_factor=4,
                use_periodic_boundary=False,
                cores=1,
            )

            with mock.patch("builtins.__import__", side_effect=fail_pyaedt_import), mock.patch("logging.exception"):
                row = run_ipmsm_batch.run_one_case(({"case_id": "missing_import"}, options.__dict__))

            with result_csv.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))

        self.assertEqual(row["case_id"], "missing_import")
        self.assertEqual(row["status"], "failed")
        self.assertIn("forced missing pyaedt_module", row["error"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["case_id"], "missing_import")
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("input_quality_profile", rows[0])


if __name__ == "__main__":
    unittest.main()
