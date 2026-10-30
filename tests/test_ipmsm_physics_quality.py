from __future__ import annotations

import math
import sys
import csv
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import module.ipmsm_ppt_setup as setup
from module.ipmsm_waveform_quality import cycle_statistics, periodic_derivative, analyze_exported_waveforms
import run_ipmsm_batch as runner


class PhysicsQualityTests(unittest.TestCase):
    def test_periodic_endpoint_is_integrated_once(self):
        t = np.linspace(0, 1, 101)
        s = cycle_statistics(t, 3 + np.cos(2 * np.pi * t), 0, 1)
        self.assertAlmostEqual(s["avg"], 3, places=12)
        self.assertAlmostEqual(s["rms"], math.sqrt(9.5), places=12)

    def test_incomplete_cycle_and_nonfinite_samples_are_not_repaired(self):
        self.assertTrue(math.isnan(cycle_statistics([0, .2, .4], [1, 1, 1], 0, 1)["avg"]))
        self.assertTrue(math.isnan(cycle_statistics([0, .5, 1], [1, math.inf, 1], 0, 1)["avg"]))

    def test_periodic_derivative_matches_known_harmonics_without_phase_delay(self):
        t = np.linspace(0, 4, 361)
        flux = .1 * np.cos(2 * np.pi * t) + .02 * np.sin(6 * np.pi * t)
        grid, derivative, closure = periodic_derivative(t, flux, 3, 4, 90)
        expected = -.2 * np.pi * np.sin(2 * np.pi * grid) + .12 * np.pi * np.cos(6 * np.pi * grid)
        np.testing.assert_allclose(derivative, expected, atol=2e-12)
        self.assertLess(closure, 1e-12)
        with self.assertRaisesRegex(ValueError, "not closed"):
            periodic_derivative(t, flux + t, 3, 4, 90)

    def test_flux_voltage_restores_rl_power_balance_and_preserves_solver_power(self):
        t = np.linspace(0, 4, 361)
        exported = {}
        with tempfile.TemporaryDirectory() as tmp:
            arrays = {"PPT_Phase_Currents": {}, "PPT_Phase_Voltages": {}, "PPT_Flux_Linkages": {}}
            for phase, offset in zip("ABC", [0, -2 * np.pi / 3, 2 * np.pi / 3]):
                current = 100 * np.cos(2 * np.pi * t + offset)
                flux = .01 * current
                backward = np.r_[0, np.diff(flux) / np.diff(t)]
                arrays["PPT_Phase_Currents"][f"InputCurrent(Phase{phase}) [A]"] = current
                arrays["PPT_Phase_Voltages"][f"InducedVoltage(Phase{phase})+R*I [V]"] = backward + .1 * current
                arrays["PPT_Flux_Linkages"][f"FluxLinkage(Phase{phase}) [Wb]"] = flux
            for name, values in arrays.items():
                path = Path(tmp) / (name + ".csv")
                pd.DataFrame({"Time [s]": t, **values}).to_csv(path, index=False)
                exported["artifact_report_" + name] = str(path)
            result = analyze_exported_waveforms(exported, setup.IPMSMPPTSpec(base_rpm=15, transient_periods=4, phase_resistance_ohm=.1))
        self.assertAlmostEqual(result["output_electrical_input_last_w"], 1500, places=8)
        self.assertGreater(result["output_solver_electrical_input_last_w"], 1500)
        self.assertEqual(result["output_phase_voltage_source"], "flux_derivative_three_phase")

    def test_missing_loss_cannot_make_valid_total_or_efficiency(self):
        values = {"output_torque_last_avg_nm": 10, "output_coreloss_last_avg_w": 20,
                  "output_phase_current_last_rms_a": 10}
        runner.add_derived_motor_metrics(values, setup.IPMSMPPTSpec())
        self.assertTrue(math.isnan(values["output_total_loss_last_avg_w"]))
        self.assertTrue(math.isnan(values["output_efficiency_last_pct"]))

    def test_inductance_omission_is_only_allowed_for_screening(self):
        with self.assertRaisesRegex(ValueError, "only screening"):
            runner.build_spec({"compute_inductance": "false"})
        spec = runner.build_spec({"compute_inductance": "false", "analysis_purpose": "screening"})
        self.assertFalse(spec.compute_inductance)
        self.assertEqual(spec.analysis_purpose, "screening")

    def test_essential_skipped_operations_and_false_validation_are_detected(self):
        problems = setup.critical_setup_issues({"mesh": {"band": "skipped: bad mesh"},
                                               "windings": {"errors": ["PhaseA failed"]},
                                               "validation": 0})
        self.assertTrue(any("mesh.band" in item for item in problems))
        self.assertTrue(any("windings.errors" in item for item in problems))
        self.assertTrue(any("validation" in item for item in problems))

    def test_failed_validation_stops_before_solver(self):
        from tests.test_ipmsm_ppt_setup_contract import IPMSMPPTSetupContractTests
        m2d = mock.Mock()
        m2d.validate_simple.return_value = 0
        with IPMSMPPTSetupContractTests().patched_configure_helpers(m2d):
            with self.assertRaisesRegex(RuntimeError, "Essential AEDT setup failed"):
                setup.configure_ipmsm_from_ppt(object(), create_reports=False, analyze=True)
        m2d.analyze.assert_not_called()

    def test_local_mesh_and_inductance_change_provenance(self):
        a = runner.build_spec({})
        b = runner.build_spec({"mesh_band_max_length_mm": ".2"})
        self.assertNotEqual(runner.setup_fingerprint(a, "test", "sin_current", False),
                            runner.setup_fingerprint(b, "test", "sin_current", False))

    def test_batch_process_failure_is_visible_to_scheduler(self):
        with mock.patch.object(sys, "argv", ["run_ipmsm_batch.py", "--setup-only"]), \
             mock.patch.object(runner, "run_one_case", return_value={"case_id": "bad", "status": "failed"}):
            self.assertEqual(runner.main(), 1)

    def test_laminated_core_does_not_double_count_bulk_eddy_loss(self):
        groups = {"stator": ["stator"], "rotor": ["rotor"], "magnets": ["magnet_N"], "windings": ["coil"]}
        flags = {name: True for names in groups.values() for name in names}
        m2d = mock.Mock()
        m2d.oboundary.GetEddyEffect.side_effect = lambda name: flags[name]
        def set_eddy(names, enable_eddy_effects):
            flags.update({name: enable_eddy_effects for name in names})
            return True
        m2d.eddy_effects_on.side_effect = set_eddy
        m2d.set_core_losses.return_value = True
        with mock.patch.object(setup, "_m2d", return_value=m2d), \
             mock.patch.object(setup, "_get_group", side_effect=lambda m, g, key, prefixes: groups.get(key, [])):
            result = setup.assign_losses(object(), groups)
        self.assertEqual(result["eddy_readback"], {"stator": "off", "rotor": "off", "magnet_N": "on", "coil": "off"})
        self.assertNotIn("eddy_readback_error", result)

    def test_training_and_dataset_validator_reject_unsettled_screening_and_mixed_producers(self):
        import train_ipmsm_lightgbm as training
        import validate_ipmsm_v2_dataset as validator
        from tests.test_validate_ipmsm_v2_dataset import valid_row
        row = valid_row()
        for key in training.V2_FINGERPRINT_COLUMNS:
            row.setdefault(key, "fixture")
        row.update(input_metric_contract_version="motor_metrics_v3", input_analysis_purpose="training",
                   input_compute_inductance="True", input_strict_setup="True", validation="True",
                   output_steady_state_passed="True", input_loss_policy="laminated_core", input_circle_segments="0",
                   output_phase_voltage_source="flux_derivative_three_phase", output_power_balance_residual_last_pct="0.1",
                   input_producer_fingerprint="producer_v3:sha256:" + "a" * 64, setup_issues="",
                   output_torque_cycle_drift_pct="0.1", output_coreloss_cycle_drift_pct="0.2",
                   output_solidloss_cycle_drift_pct="0.3")
        training.validate_v2_fingerprints(pd.DataFrame([row]))
        self.assertTrue(validator.validate_rows([row]).passed)
        row["output_coreloss_cycle_drift_pct"] = "42"
        with self.assertRaisesRegex(ValueError, "physics gate"):
            training.validate_v2_fingerprints(pd.DataFrame([row]))
        self.assertFalse(validator.validate_rows([row]).passed)
        row["output_coreloss_cycle_drift_pct"] = "0.2"
        row["input_analysis_purpose"] = "screening"
        with self.assertRaisesRegex(ValueError, "screening"):
            training.validate_v2_fingerprints(pd.DataFrame([row]))
        row["input_analysis_purpose"] = "training"
        second = dict(row, input_producer_fingerprint="producer_v3:sha256:" + "b" * 64)
        with self.assertRaisesRegex(ValueError, "mixed v3"):
            training.validate_v2_fingerprints(pd.DataFrame([row, second]))

    def test_exported_design_variables_cannot_masquerade_as_phase_currents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing_currents.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["BaseRPM []", "Beta [deg]", "Imax [A]", "Time [s]"])
                writer.writerow([1200, 30, 100, 0])
                writer.writerow([1200, 30, 100, .01])
            values = runner.summarize_transient_outputs({"artifact_report_PPT_Phase_Currents": str(path)}, setup.IPMSMPPTSpec())
            self.assertNotEqual(values["output_phase_current_source"], "measured_three_phase")
            self.assertIn("output_phase_current_source", runner.missing_required_output_metrics(values, require_v2=True))


if __name__ == "__main__":
    unittest.main()
