from __future__ import annotations

import unittest

import run_ipmsm_batch


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
        fixed = run_ipmsm_batch.extract_fixed_geometry(
            {
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
        )

        self.assertEqual(fixed["slot_num"], 12)
        self.assertEqual(fixed["pole_num"], 8)
        self.assertAlmostEqual(fixed["stator_teeth_width_ratio"], 0.722, places=3)
        self.assertEqual(fixed["slot_opening_ratio"], 0.09)
        self.assertEqual(fixed["magnet_space_height_ratio"], 1.0)

    def test_extract_fixed_geometry_rejects_incomplete_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed geometry columns are incomplete"):
            run_ipmsm_batch.extract_fixed_geometry({"input_slot_num": "12"})

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


if __name__ == "__main__":
    unittest.main()
