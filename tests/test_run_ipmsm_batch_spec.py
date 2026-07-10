from __future__ import annotations

import builtins
import csv
import math
from pathlib import Path
import sys
import tempfile
import types
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
    def test_safe_path_exists_treats_permission_errors_as_missing(self) -> None:
        class PermissionDeniedPath:
            def exists(self) -> bool:
                raise PermissionError("denied")

        self.assertFalse(run_ipmsm_batch.safe_path_exists(PermissionDeniedPath()))

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

    def test_result_schema_includes_transient_setup_metadata(self) -> None:
        self.assertIn("input_transient_total_steps", run_ipmsm_batch.RESULT_COLUMN_ORDER)
        self.assertIn("input_electric_frequency_hz", run_ipmsm_batch.RESULT_COLUMN_ORDER)
        self.assertIn("input_electrical_period_s", run_ipmsm_batch.RESULT_COLUMN_ORDER)
        self.assertIn("input_transient_stop_time_s", run_ipmsm_batch.RESULT_COLUMN_ORDER)
        self.assertIn("input_transient_time_step_s", run_ipmsm_batch.RESULT_COLUMN_ORDER)

    def test_result_schema_includes_v2_contract_and_fingerprints(self) -> None:
        for column in (
            "input_dataset_schema_version",
            "input_model_extent",
            "input_beta_dq_deg",
            "input_beta_convention",
            "input_electrical_zero_deg",
            "input_commanded_id_peak_a",
            "input_commanded_iq_peak_a",
            "input_setup_fingerprint",
            "input_material_fingerprint",
            "input_aedt_version",
        ):
            self.assertIn(column, run_ipmsm_batch.RESULT_COLUMN_ORDER)

    def test_result_schema_preserves_geometry_and_operating_point_identity(self) -> None:
        for column in (
            "geometry_group_id",
            "design_hash",
            "operating_point_id",
            "doe_split",
        ):
            self.assertIn(column, run_ipmsm_batch.RESULT_COLUMN_ORDER)

    def test_build_spec_defaults_to_full_360_canonical_v2(self) -> None:
        spec = run_ipmsm_batch.build_spec({})

        self.assertEqual(spec.model_extent, "full_360")
        self.assertEqual(spec.symmetry_factor, 1)
        self.assertEqual(spec.beta_convention, "dq_current_advance_v2")

    def test_build_spec_prefers_canonical_beta_column(self) -> None:
        spec = run_ipmsm_batch.build_spec(
            {"beta_dq_deg": "42", "beta_deg": "7", "electrical_zero_deg": "12.5"}
        )

        self.assertEqual(spec.beta_deg, 42.0)
        self.assertEqual(spec.electrical_zero_deg, 12.5)

    def test_build_spec_rejects_unsafe_extent_combinations(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires symmetry_factor=1"):
            run_ipmsm_batch.build_spec({"model_extent": "full_360", "symmetry_factor": "4"})
        with self.assertRaisesRegex(ValueError, "real sector geometry builder"):
            run_ipmsm_batch.build_spec({"model_extent": "sector_90", "symmetry_factor": "4"})

    def test_case_plan_rejects_periodic_boundary_on_full_model(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "periodic boundary is invalid"):
            run_ipmsm_batch.validate_case_plan(
                [{"case_id": "bad_periodic", "use_periodic_boundary": "true"}],
                max_cases=200,
            )

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

    def test_build_spec_rejects_non_positive_transient_settings(self) -> None:
        with self.assertRaisesRegex(ValueError, "steps_per_period must be >= 1"):
            run_ipmsm_batch.build_spec({"steps_per_period": "0"})

        with self.assertRaisesRegex(ValueError, "transient_periods must be >= 1"):
            run_ipmsm_batch.build_spec({"transient_periods": "0"})

        with self.assertRaisesRegex(ValueError, "base_rpm must be > 0"):
            run_ipmsm_batch.build_spec({"base_rpm": "0"})

    def test_transient_setup_metadata_records_effective_time_step(self) -> None:
        spec = run_ipmsm_batch.build_spec({"base_rpm": "1200", "pole_number": "8", "transient_periods": "10", "steps_per_period": "90"})

        metadata = run_ipmsm_batch.transient_setup_metadata(spec)

        self.assertEqual(metadata["transient_total_steps"], 900)
        self.assertAlmostEqual(metadata["electric_frequency_hz"], 80.0)
        self.assertAlmostEqual(metadata["electrical_period_s"], 0.0125)
        self.assertAlmostEqual(metadata["transient_stop_time_s"], 0.125)
        self.assertAlmostEqual(metadata["transient_time_step_s"], 0.125 / 900.0)

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

    def test_v2_required_output_gate_includes_measured_dq_and_voltage(self) -> None:
        missing = run_ipmsm_batch.missing_required_output_metrics(
            {
                "output_torque_all_avg_nm": 10.0,
                "output_coreloss_all_avg_w": 20.0,
                "output_solidloss_all_avg_w": 2.0,
            },
            require_v2=True,
            operation="sin_current",
        )

        self.assertIn("output_phase_voltage_last_peak_abs_v", missing)
        self.assertIn("output_id_current_last_avg_a", missing)
        self.assertIn("output_iq_current_last_avg_a", missing)
        self.assertIn("output_phase_current_source", missing)
        self.assertIn("output_phase_voltage_source", missing)

    def test_v2_required_output_gate_rejects_numeric_fallback_reports(self) -> None:
        values = {
            "output_torque_all_avg_nm": 10.0,
            "output_coreloss_all_avg_w": 20.0,
            "output_solidloss_all_avg_w": 2.0,
            "output_phase_voltage_last_peak_abs_v": 100.0,
            "output_phasea_voltage_last_peak_abs_v": 100.0,
            "output_phaseb_voltage_last_peak_abs_v": 100.0,
            "output_phasec_voltage_last_peak_abs_v": 100.0,
            "output_ld_last_avg_h": 0.003,
            "output_lq_last_avg_h": 0.004,
            "output_phase_current_last_rms_a": 10.0,
            "output_id_current_last_avg_a": -2.0,
            "output_iq_current_last_avg_a": 13.0,
            "output_phase_current_source": "commanded_fallback",
            "output_phase_voltage_source": "phasea_fallback",
        }

        missing = run_ipmsm_batch.missing_required_output_metrics(
            values, require_v2=True, operation="sin_current"
        )

        self.assertEqual(
            missing,
            ["output_phase_voltage_source", "output_phase_current_source"],
        )

    def test_measured_phase_currents_round_trip_to_canonical_dq(self) -> None:
        import pandas as pd
        from module.ipmsm_ppt_setup import inverse_park_phase_currents

        spec = types.SimpleNamespace(
            pole_number=4,
            base_rpm=60.0,
            initial_position_deg=-22.5,
            electrical_zero_deg=17.0,
        )
        times = [index / 360.0 for index in range(361)]
        phase_rows = [
            inverse_park_phase_currents(
                -10.0,
                20.0,
                math.degrees(4.0 * math.pi * time_s) + spec.electrical_zero_deg,
            )
            for time_s in times
        ]
        df = pd.DataFrame(
            {
                "Time [s]": times,
                "InputCurrent(PhaseA) [A]": [row["PhaseA"] for row in phase_rows],
                "InputCurrent(PhaseB) [A]": [row["PhaseB"] for row in phase_rows],
                "InputCurrent(PhaseC) [A]": [row["PhaseC"] for row in phase_rows],
            }
        )

        summary = run_ipmsm_batch.summarize_dq_current_report(
            df,
            (
                "InputCurrent(PhaseA) [A]",
                "InputCurrent(PhaseB) [A]",
                "InputCurrent(PhaseC) [A]",
            ),
            spec,
            period_s=0.5,
            stop_s=1.0,
        )

        self.assertAlmostEqual(summary["output_id_current_last_avg_a"], -10.0, places=9)
        self.assertAlmostEqual(summary["output_iq_current_last_avg_a"], 20.0, places=9)

    def test_inductance_matrix_uses_same_calibrated_dq_frame(self) -> None:
        import pandas as pd

        spec = types.SimpleNamespace(
            pole_number=4,
            base_rpm=60.0,
            initial_position_deg=-22.5,
            electrical_zero_deg=17.0,
        )
        time_s = 0.25
        theta = run_ipmsm_batch.canonical_electrical_frame_angle_rad(spec, time_s)
        angles = (theta, theta - 2.0 * math.pi / 3.0, theta + 2.0 * math.pi / 3.0)
        park = [
            [(2.0 / 3.0) * math.cos(angle) for angle in angles],
            [-(2.0 / 3.0) * math.sin(angle) for angle in angles],
        ]
        inverse_park = [[math.cos(angle), -math.sin(angle)] for angle in angles]
        ld_h, lq_h = 0.003, 0.007
        labc = [
            [
                inverse_park[i][0] * ld_h * park[0][j]
                + inverse_park[i][1] * lq_h * park[1][j]
                for j in range(3)
            ]
            for i in range(3)
        ]
        phases = ("PhaseA", "PhaseB", "PhaseC")
        data = {"Time [s]": [time_s]}
        for i, source in enumerate(phases):
            for j, target in enumerate(phases):
                data[f"L({source},{target}) [H]"] = [labc[i][j]]

        summary = run_ipmsm_batch.summarize_inductance_matrix(
            pd.DataFrame(data), spec, period_s=0.25, stop_s=0.25
        )

        self.assertAlmostEqual(summary["output_ld_last_avg_h"], ld_h, places=12)
        self.assertAlmostEqual(summary["output_lq_last_avg_h"], lq_h, places=12)

    def test_derived_motor_efficiency_is_nan_for_negative_mechanical_power(self) -> None:
        spec = types.SimpleNamespace(base_rpm=1200.0, phase_resistance_ohm=0.01, vdc_v=200.0)
        output_summary = {
            "output_torque_first_avg_nm": -10.0,
            "output_torque_first_min_nm": -11.0,
            "output_torque_first_max_nm": -9.0,
            "output_phase_current_first_rms_a": 10.0,
            "output_coreloss_first_avg_w": 20.0,
            "output_solidloss_first_avg_w": 30.0,
            "output_phase_voltage_first_peak_abs_v": 100.0,
            "output_torque_last_avg_nm": 10.0,
            "output_torque_last_min_nm": 9.0,
            "output_torque_last_max_nm": 11.0,
            "output_phase_current_last_rms_a": 10.0,
            "output_coreloss_last_avg_w": 20.0,
            "output_solidloss_last_avg_w": 30.0,
            "output_phase_voltage_last_peak_abs_v": 100.0,
            "output_torque_all_avg_nm": 10.0,
            "output_torque_all_min_nm": 9.0,
            "output_torque_all_max_nm": 11.0,
            "output_phase_current_all_rms_a": 10.0,
            "output_coreloss_all_avg_w": 20.0,
            "output_solidloss_all_avg_w": 30.0,
            "output_phase_voltage_all_peak_abs_v": 100.0,
        }

        run_ipmsm_batch.add_derived_motor_metrics(output_summary, spec)

        self.assertLess(output_summary["output_mech_power_first_w"], 0.0)
        self.assertTrue(math.isnan(output_summary["output_efficiency_first_pct"]))
        self.assertGreaterEqual(output_summary["output_efficiency_last_pct"], 0.0)
        self.assertLessEqual(output_summary["output_efficiency_last_pct"], 100.0)

    def test_motor_efficiency_rejects_negative_loss_inputs(self) -> None:
        self.assertTrue(math.isnan(run_ipmsm_batch.motor_efficiency_pct(100.0, -1.0)))
        self.assertAlmostEqual(run_ipmsm_batch.motor_efficiency_pct(100.0, 25.0), 80.0)

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

    def test_validate_case_plan_rejects_bad_mesh_before_aedt(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "case plan row bad_mesh has invalid inputs"):
            run_ipmsm_batch.validate_case_plan(
                [{"case_id": "bad_mesh", "mesh_band_elements": "0"}],
                max_cases=200,
            )

    def test_validate_case_plan_rejects_bad_fixed_geometry_before_aedt(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "case plan row bad_geometry has invalid inputs"):
            run_ipmsm_batch.validate_case_plan(
                [
                    {
                        **fixed_geometry_result_row(input_rotator_gap="90"),
                        "case_id": "bad_geometry",
                    }
                ],
                max_cases=200,
            )

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
                symmetry_factor=1,
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
        self.assertEqual(rows[0]["input_transient_total_steps"], "900")
        self.assertEqual(rows[0]["input_electric_frequency_hz"], "80.0")
        self.assertEqual(rows[0]["input_dataset_schema_version"], "ipmsm_v2")
        self.assertEqual(rows[0]["input_model_extent"], "full_360")
        self.assertEqual(rows[0]["input_beta_convention"], "dq_current_advance_v2")
        self.assertEqual(rows[0]["input_beta_dq_deg"], "30.0")
        self.assertTrue(rows[0]["input_setup_fingerprint"].startswith("setup_v2:sha256:"))
        self.assertTrue(rows[0]["input_material_fingerprint"].startswith("materials_v2:sha256:"))
        self.assertNotEqual(rows[0]["input_aedt_version"], "")

    def test_run_one_case_writes_clear_error_when_desktop_startup_returns_none(self) -> None:
        core_module = types.ModuleType("pyaedt_module.core")
        ansys_core_module = types.ModuleType("ansys.aedt.core")
        settings = types.SimpleNamespace(enable_error_handler=True, skip_license_check=False, wait_for_license=True)

        def fail_desktop_startup(**_kwargs: object) -> object:
            raise AttributeError("'NoneType' object has no attribute 'EnableAutoSave'")

        core_module.pyDesktop = fail_desktop_startup
        package_module = types.ModuleType("pyaedt_module")
        package_module.core = core_module
        ansys_core_module.settings = settings

        with tempfile.TemporaryDirectory() as tmp:
            result_csv = Path(tmp) / "results.csv"
            options = run_ipmsm_batch.RunnerOptions(
                simulation_dir=str(Path(tmp) / "simulation"),
                result_csv=str(result_csv),
                analyze=False,
                non_graphical=True,
                cleanup_linux=False,
                symmetry_factor=1,
                use_periodic_boundary=False,
                cores=1,
            )

            with mock.patch.dict(
                sys.modules,
                {
                    "pyaedt_module": package_module,
                    "pyaedt_module.core": core_module,
                    "ansys.aedt.core": ansys_core_module,
                },
            ):
                with mock.patch("logging.exception"):
                    row = run_ipmsm_batch.run_one_case(({"case_id": "desktop_none"}, options.__dict__))

        self.assertEqual(row["case_id"], "desktop_none")
        self.assertEqual(row["status"], "failed")
        self.assertIn("AEDT desktop startup failed before project creation", row["error"])
        self.assertFalse(settings.enable_error_handler)
        self.assertTrue(settings.skip_license_check)
        self.assertFalse(settings.wait_for_license)

    def test_run_one_case_reports_analysis_false_before_missing_reports(self) -> None:
        core_module = types.ModuleType("pyaedt_module.core")
        ansys_core_module = types.ModuleType("ansys.aedt.core")
        settings = types.SimpleNamespace(enable_error_handler=True, skip_license_check=False, wait_for_license=True)

        class FakeProject:
            def __init__(self, path: Path) -> None:
                self.path = str(path)

            def save(self) -> None:
                return None

        class FakeDesktop:
            def create_project(self, path: str, name: str) -> FakeProject:
                return FakeProject(Path(path) / name)

            def release_desktop(self, **_kwargs: object) -> None:
                return None

        core_module.pyDesktop = lambda **_kwargs: FakeDesktop()
        package_module = types.ModuleType("pyaedt_module")
        package_module.core = core_module
        ansys_core_module.settings = settings

        def fake_create_ipmsm_design(_project: object, _sim: object) -> tuple[object, None, dict[str, object]]:
            return object(), None, {}

        def fake_configure_ipmsm_from_ppt(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {"analysis": False, "validation": False}

        with tempfile.TemporaryDirectory() as tmp:
            result_csv = Path(tmp) / "results.csv"
            options = run_ipmsm_batch.RunnerOptions(
                simulation_dir=str(Path(tmp) / "simulation"),
                result_csv=str(result_csv),
                analyze=True,
                non_graphical=True,
                cleanup_linux=False,
                symmetry_factor=1,
                use_periodic_boundary=False,
                cores=1,
            )

            with mock.patch.dict(
                sys.modules,
                {
                    "pyaedt_module": package_module,
                    "pyaedt_module.core": core_module,
                    "ansys.aedt.core": ansys_core_module,
                },
            ), mock.patch(
                "module.ipmsm_geometry.create_ipmsm_design",
                side_effect=fake_create_ipmsm_design,
            ), mock.patch(
                "module.ipmsm_ppt_setup.configure_ipmsm_from_ppt",
                side_effect=fake_configure_ipmsm_from_ppt,
            ), mock.patch(
                "logging.exception"
            ):
                row = run_ipmsm_batch.run_one_case(({"case_id": "analysis_false"}, options.__dict__))

            with result_csv.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))

        self.assertEqual(row["case_id"], "analysis_false")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["analysis_returned_false"], True)
        self.assertEqual(row["validation"], "False")
        self.assertIn("AEDT analysis returned False", row["error"])
        self.assertEqual(row.get("missing_required_outputs", ""), "")
        self.assertEqual(len(rows), 1)
        self.assertIn("AEDT analysis returned False", rows[0]["error"])
        self.assertEqual(rows[0]["analysis_returned_false"], "True")


if __name__ == "__main__":
    unittest.main()
