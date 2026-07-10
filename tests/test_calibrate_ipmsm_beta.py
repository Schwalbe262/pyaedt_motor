from __future__ import annotations

import math
import json
from pathlib import Path
import tempfile
import unittest

import calibrate_ipmsm_beta as calibration
import run_ipmsm_batch
from module.ipmsm_ppt_setup import canonical_dq_current_components


def source_row() -> dict[str, str]:
    return {
        "case_id": "source-v2",
        "geometry_group_id": "geometry-v2",
        "design_hash": "hash-v2",
        "input_slot_num": "12",
        "input_pole_num": "8",
        "input_stator_outer_radius": "155",
        "input_stator_back_yoke_thick_ratio": "0.14",
        "input_stator_inner_ratio": "0.51",
        "input_stator_shoe_thick": "1.5",
        "input_stator_teeth_length_ratio": "0.85",
        "input_stator_teeth_width_ratio": "0.6",
        "input_stator_gap": "2",
        "input_slot_opening_ratio": "0.09",
        "input_rotator_gap": "2",
        "input_shaft_ratio": "0.5",
        "input_magnet_shield_thick": "2",
        "input_magnet_setback_ratio": "0.15",
        "input_magnet_thick_ratio": "0.3",
        "input_magnet_space_height_ratio": "0.9",
        "input_magnet_height_ratio": "0.9",
        "input_stack_length_mm": "50",
        "input_phase_resistance_ohm": "0.05",
        "input_vdc_v": "300",
    }


def motor_spec_mapping() -> dict:
    return {
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
    }


def zero_result(speed: float, zero_deg: float, amplitude: float = 100.0) -> dict[str, str]:
    angle = math.radians(zero_deg)
    return {
        "case_id": f"zero-{speed:g}",
        "status": "ok",
        "geometry_group_id": "geometry-v2",
        "design_hash": "hash-v2",
        "input_source_case_id": "source-v2",
        "input_dataset_schema_version": calibration.DATASET_SCHEMA_VERSION,
        "input_operation": "no_load",
        "input_i_peak_a": "0",
        "input_base_rpm": str(speed),
        "input_model_extent": "full_360",
        "input_symmetry_factor": "1",
        "input_use_periodic_boundary": "False",
        "input_beta_convention": calibration.BETA_CONVENTION,
        "input_electrical_zero_deg": "0",
        "input_quality_profile": "reference_ultra",
        "input_setup_fingerprint": "setup-v2",
        "input_material_fingerprint": "materials-v2",
        "input_aedt_version": "2025.2",
        "input_initial_position_deg": "-22.5",
        "output_back_emf_phasea_h1_cos_peak_v": str(-amplitude * math.sin(angle)),
        "output_back_emf_phasea_h1_sin_peak_v": str(-amplitude * math.cos(angle)),
    }


def zero_manifest(zero_deg: float = 30.0) -> dict:
    return calibration.analyze_zero_calibration_rows(
        [
            zero_result(600.0, zero_deg, amplitude=50.0),
            zero_result(1200.0, zero_deg, amplitude=100.0),
        ]
    )


class SimpleFrame(dict[str, list[float]]):
    @property
    def columns(self) -> list[str]:
        return list(self)


class CalibrateIpmsmBetaTests(unittest.TestCase):
    def test_calibration_source_can_be_created_before_beta_manifest_exists(self) -> None:
        raw = motor_spec_mapping()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "motor.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            first = calibration.calibration_source_from_spec(path, geometry_seed=17)
            second = calibration.calibration_source_from_spec(path, geometry_seed=17)

        self.assertEqual(first, second)
        self.assertIn("slot_opening_ratio", first)
        self.assertIn("magnet_space_height_ratio", first)
        self.assertEqual((first["slot_num"], first["pole_num"]), (12, 8))
        self.assertGreater(first["phase_resistance_ohm"], 0.0)

    def test_apply_zero_manifest_makes_spec_optimization_ready(self) -> None:
        manifest = zero_manifest()

        updated = calibration.apply_zero_manifest_to_spec(motor_spec_mapping(), manifest)

        self.assertEqual(updated["beta_calibration"]["calibration_id"], manifest["calibration_id"])
        self.assertEqual(updated["beta_calibration"]["electrical_zero_deg"], 30.0)

    def test_signed_harmonic_coefficients_preserve_known_phase(self) -> None:
        count = 360
        cos_peak = -3.0
        sin_peak = 4.0
        times = [index / count for index in range(count)]
        frame = SimpleFrame(
            {
                "Time [s]": times,
                "Ea [V]": [
                    cos_peak * math.cos(2.0 * math.pi * time_s)
                    + sin_peak * math.sin(2.0 * math.pi * time_s)
                    for time_s in times
                ],
            }
        )

        summary = run_ipmsm_batch.summarize_last_cycle_harmonics(
            frame,
            "Ea [V]",
            "output_back_emf_phasea",
            fundamental_hz=1.0,
            period_s=1.0,
            stop_s=1.0,
            unit_suffix="v",
        )

        self.assertAlmostEqual(summary["output_back_emf_phasea_h1_cos_peak_v"], cos_peak, places=12)
        self.assertAlmostEqual(summary["output_back_emf_phasea_h1_sin_peak_v"], sin_peak, places=12)
        self.assertAlmostEqual(
            summary["output_back_emf_phasea_h1_phase_deg"],
            math.degrees(math.atan2(cos_peak, sin_peak)),
            places=12,
        )

    def test_zero_generator_is_no_load_and_full_model(self) -> None:
        rows = calibration.generate_zero_calibration_rows(
            source_row(), speeds_rpm=(600.0, 1200.0)
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["operation"] for row in rows}, {"no_load"})
        self.assertEqual({row["i_peak_a"] for row in rows}, {0.0})
        self.assertEqual({row["electrical_zero_deg"] for row in rows}, {0.0})
        self.assertEqual({row["symmetry_factor"] for row in rows}, {1})
        self.assertEqual({row["model_extent"] for row in rows}, {"full_360"})

    def test_zero_analysis_uses_signed_back_emf_phase_and_circular_mean(self) -> None:
        manifest = calibration.analyze_zero_calibration_rows(
            [zero_result(600.0, 29.5), zero_result(1200.0, 30.5)],
            max_circular_deviation_deg=1.0,
        )

        self.assertAlmostEqual(manifest["electrical_zero_deg"], 30.0, places=9)
        self.assertEqual(manifest["method"], calibration.ZERO_CALIBRATION_METHOD)
        self.assertTrue(manifest["calibration_id"].startswith("beta-calibration:sha256:"))

    def test_zero_analysis_rejects_loaded_torque_max_rows(self) -> None:
        row = zero_result(1200.0, 30.0)
        row["input_operation"] = "sin_current"
        row["input_i_peak_a"] = "100"
        row["output_torque_last_avg_nm"] = "50"

        with self.assertRaisesRegex(ValueError, "loaded torque-max"):
            calibration.analyze_zero_calibration_rows([row])
        with self.assertRaisesRegex(ValueError, "legacy loaded torque-max"):
            calibration.generate_calibration_rows()

    def test_zero_analysis_rejects_inconsistent_speed_phases(self) -> None:
        with self.assertRaisesRegex(ValueError, "observations differ"):
            calibration.analyze_zero_calibration_rows(
                [zero_result(600.0, 10.0), zero_result(1200.0, 30.0)],
                max_circular_deviation_deg=3.0,
            )

    def test_zero_analysis_requires_two_distinct_successful_speeds(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 2 distinct successful speeds"):
            calibration.analyze_zero_calibration_rows(
                [zero_result(600.0, 30.0), zero_result(600.0, 30.0)]
            )

    def test_zero_analysis_rejects_nonpositive_min_distinct_speeds(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            calibration.analyze_zero_calibration_rows(
                [zero_result(600.0, 30.0), zero_result(1200.0, 30.0)],
                min_distinct_speeds=0,
            )

    def test_zero_analysis_requires_v2_schema_and_nonblank_fingerprints(self) -> None:
        rows = [zero_result(600.0, 30.0), zero_result(1200.0, 30.0)]
        rows[1]["input_dataset_schema_version"] = "legacy"
        with self.assertRaisesRegex(ValueError, "input_dataset_schema_version"):
            calibration.analyze_zero_calibration_rows(rows)

        for column in (
            "design_hash",
            "input_quality_profile",
            "input_setup_fingerprint",
            "input_material_fingerprint",
            "input_aedt_version",
        ):
            with self.subTest(column=column):
                rows = [zero_result(600.0, 30.0), zero_result(1200.0, 30.0)]
                rows[1].pop(column)
                with self.assertRaisesRegex(ValueError, f"nonblank {column}"):
                    calibration.analyze_zero_calibration_rows(rows)

    def test_zero_analysis_requires_finite_homogeneous_initial_position(self) -> None:
        rows = [zero_result(600.0, 30.0), zero_result(1200.0, 30.0)]
        rows[1]["input_initial_position_deg"] = "nan"
        with self.assertRaisesRegex(ValueError, "finite input_initial_position_deg"):
            calibration.analyze_zero_calibration_rows(rows)

        rows = [zero_result(600.0, 30.0), zero_result(1200.0, 30.0)]
        rows[1]["input_initial_position_deg"] = "-22.5000000005"
        manifest = calibration.analyze_zero_calibration_rows(rows)
        self.assertEqual(manifest["initial_position_deg"], -22.5)

        rows = [zero_result(600.0, 30.0), zero_result(1200.0, 30.0)]
        rows[1]["input_initial_position_deg"] = "-20"
        with self.assertRaisesRegex(ValueError, "input_initial_position_deg"):
            calibration.analyze_zero_calibration_rows(rows)

    def test_zero_analyze_cli_defaults_to_two_distinct_speeds(self) -> None:
        args = calibration.build_parser().parse_args(
            ["zero-analyze", "--results", "results.csv", "--manifest", "manifest.json"]
        )

        self.assertEqual(args.min_distinct_speeds, 2)

    def test_zero_analyze_cli_forwards_min_distinct_speeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results.csv"
            manifest = Path(tmp) / "manifest.json"
            calibration.write_rows(
                results,
                [zero_result(600.0, 30.0), zero_result(1200.0, 30.0)],
            )

            with self.assertRaisesRegex(ValueError, "at least 3 distinct successful speeds"):
                calibration.main(
                    [
                        "zero-analyze",
                        "--results",
                        str(results),
                        "--manifest",
                        str(manifest),
                        "--min-distinct-speeds",
                        "3",
                    ]
                )

    def test_loaded_beta_sweep_keeps_zero_fixed_and_finds_mtpa(self) -> None:
        manifest = zero_manifest()
        cases = calibration.generate_beta_sweep_rows(
            source_row(),
            manifest,
            rpm=1200.0,
            current_peak_a=100.0,
            beta_values=(-10.0, 0.0, 20.0, 40.0),
        )
        torque_by_beta = {-10.0: 20.0, 0.0: 30.0, 20.0: 42.0, 40.0: 35.0}
        results = []
        for case in cases:
            beta = float(case["beta_dq_deg"])
            id_a, iq_a = canonical_dq_current_components(100.0, beta)
            results.append(
                {
                    **case,
                    "status": "ok",
                    "input_model_extent": case["model_extent"],
                    "input_symmetry_factor": str(case["symmetry_factor"]),
                    "input_use_periodic_boundary": str(case["use_periodic_boundary"]),
                    "input_beta_convention": case["beta_convention"],
                    "input_electrical_zero_deg": str(case["electrical_zero_deg"]),
                    "input_beta_dq_deg": str(beta),
                    "input_i_peak_a": "100",
                    "input_base_rpm": "1200",
                    "input_setup_fingerprint": "setup-v2",
                    "output_torque_last_avg_nm": str(torque_by_beta[beta]),
                    "output_id_current_last_avg_a": str(id_a),
                    "output_iq_current_last_avg_a": str(iq_a),
                }
            )

        summary = calibration.analyze_beta_sweep_rows(results, manifest)

        self.assertEqual({case["electrical_zero_deg"] for case in cases}, {30.0})
        self.assertEqual({case["beta_calibration_id"] for case in cases}, {manifest["calibration_id"]})
        self.assertEqual(summary["electrical_zero_deg"], 30.0)
        self.assertEqual(summary["best_beta_dq_deg"], 20.0)
        self.assertTrue(summary["sweep_id"].startswith("beta-mtpa:sha256:"))

    def test_loaded_beta_sweep_rejects_tampered_zero_manifest(self) -> None:
        manifest = zero_manifest()
        manifest["electrical_zero_deg"] = 45.0

        with self.assertRaisesRegex(ValueError, "does not match calibration_id"):
            calibration.generate_beta_sweep_rows(
                source_row(),
                manifest,
                rpm=1200.0,
                current_peak_a=100.0,
                beta_values=(-10.0, 0.0, 20.0),
            )


if __name__ == "__main__":
    unittest.main()
