from __future__ import annotations

import unittest

import generate_ipmsm_v2_cases as generator
from ipmsm_optimization import optimization_spec_from_mapping


def valid_spec():
    return optimization_spec_from_mapping(
        {
            "schema_version": 1,
            "operating_points": [
                {"name": "torque", "speed_rpm": 1200, "target_torque_nm": 40, "duty_weight": 0.4},
                {"name": "rated", "speed_rpm": 3000, "target_power_w": 5000, "duty_weight": 0.6},
            ],
            "stack_length_bounds_mm": [40, 60],
            "inverter": {"vdc_v": 300, "phase_peak_current_limit_a": 140},
            "winding": {
                "series_turns_per_phase": 48,
                "turns_per_coil_side": 12,
                "coils_per_phase": 4,
                "parallel_branches": 1,
                "strand_area_mm2": 0.5,
                "strands_per_turn": 4,
                "fill_factor": 0.8,
                "end_turn_factor": 1.2,
                "overhang_mm": 5,
            },
            "constraints": {"current_density_limit_a_per_mm2": 20},
            "beta_calibration": {
                "electrical_zero_deg": 12.5,
                "calibration_id": "fixture-calibration",
                "convention": "dq_current_advance_v2",
            },
        }
    )


class GenerateIpmsmV2CasesTests(unittest.TestCase):
    def test_foundation_rows_are_grouped_complete_and_deterministic(self) -> None:
        spec = valid_spec()
        first = generator.generate_foundation_rows(
            spec,
            geometry_count=5,
            samples_per_operating_point=2,
            repeat_count=3,
            seed=17,
            electrical_zero_deg=12.5,
        )
        second = generator.generate_foundation_rows(
            spec,
            geometry_count=5,
            samples_per_operating_point=2,
            repeat_count=3,
            seed=17,
            electrical_zero_deg=12.5,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 5 * (2 * 2) + 3)
        self.assertEqual(len({row["case_id"] for row in first}), len(first))
        self.assertEqual({row["beta_convention"] for row in first}, {generator.BETA_CONVENTION})
        self.assertEqual({row["symmetry_factor"] for row in first}, {1})
        self.assertEqual({row["beta_calibration_id"] for row in first}, {spec.beta_calibration.calibration_id})
        self.assertTrue(all(float(row["phase_resistance_ohm"]) > 0 for row in first))

        split_by_group: dict[str, set[str]] = {}
        for row in first:
            split_by_group.setdefault(row["geometry_group_id"], set()).add(row["doe_split"])
        self.assertTrue(all(len(values) == 1 for values in split_by_group.values()))
        train_groups = {
            row["geometry_group_id"] for row in first if row["doe_split"] == "train"
        }
        repeated_groups = {
            row["geometry_group_id"] for row in first if row["repeat_of_case_id"]
        }
        self.assertEqual(len(repeated_groups), min(3, len(train_groups)))
        for split_name in ("train", "calibration", "test"):
            split_rows = [row for row in first if row["doe_split"] == split_name]
            self.assertAlmostEqual(
                min(float(row["i_peak_a"]) for row in split_rows),
                0.25 * spec.effective_peak_current_limit_a,
            )
            self.assertAlmostEqual(
                max(float(row["i_peak_a"]) for row in split_rows),
                spec.effective_peak_current_limit_a,
            )
            self.assertEqual(
                {min(float(row["beta_dq_deg"]) for row in split_rows), max(float(row["beta_dq_deg"]) for row in split_rows)},
                set(spec.beta_bounds_deg),
            )

    def test_rows_record_previously_hidden_geometry_variables(self) -> None:
        rows = generator.generate_foundation_rows(
            valid_spec(),
            geometry_count=3,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=5,
            electrical_zero_deg=12.5,
        )
        self.assertTrue(all("slot_opening_ratio" in row for row in rows))
        self.assertTrue(all("magnet_space_height_ratio" in row for row in rows))
        self.assertEqual({row["operation"] for row in rows}, {"sin_current"})

    def test_nonfinite_electrical_zero_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "electrical_zero_deg"):
            generator.generate_foundation_rows(
                valid_spec(),
                geometry_count=1,
                samples_per_operating_point=1,
                repeat_count=0,
                electrical_zero_deg=float("nan"),
            )

    def test_next_batch_prefix_and_design_exclusions_prevent_overlap(self) -> None:
        spec = valid_spec()
        first = generator.generate_foundation_rows(
            spec,
            geometry_count=3,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=11,
            case_prefix="batch1",
        )
        excluded = {row["design_hash"] for row in first}

        second = generator.generate_foundation_rows(
            spec,
            geometry_count=3,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=12,
            case_prefix="batch2",
            excluded_design_hashes=excluded,
        )

        self.assertTrue(all(str(row["case_id"]).startswith("batch2_") for row in second))
        self.assertFalse(excluded & {row["design_hash"] for row in second})


if __name__ == "__main__":
    unittest.main()
