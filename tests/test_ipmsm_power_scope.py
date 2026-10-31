"""Output-definition metadata without changing physical values or admission."""
import csv
import json
import math
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd

import run_ipmsm_batch as runner
from module.ipmsm_ppt_setup import IPMSMPPTSpec
from module.ipmsm_waveform_quality import analyze_exported_waveforms


SCOPES = {
    "output_mech_power_scope",
    "output_efficiency_scope",
    "output_power_balance_residual_normalization",
    "output_power_balance_shared_copper_term",
}
FIXTURE = Path(__file__).parent / "fixtures/ipmsm_power_scope_legacy_outputs.json"


class PowerScopeTests(unittest.TestCase):
    def test_all_legacy_values_remain_bitwise_identical_to_sealed_preimage(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                output = dict(case["input"])
                runner.add_derived_motor_metrics(output, SimpleNamespace(**case["spec"]))
                self.assertEqual(set(output), set(case["expected_numeric_bits"]) | set(case["expected_other"]) | SCOPES)
                for key, bits in case["expected_numeric_bits"].items():
                    self.assertEqual(struct.pack(">d", output[key]).hex(), bits, key)
                for key, value in case["expected_other"].items():
                    self.assertEqual(output[key], value)
                self.assertIn("net shaft power unverified", output["output_mech_power_scope"])
                self.assertIn("Estimated", output["output_efficiency_scope"])

    def test_no_load_keeps_signed_watts_floor_percentage_and_undefined_efficiency(self):
        case = next(x for x in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"] if x["name"] == "no_load")
        output = dict(case["input"])
        runner.add_derived_motor_metrics(output, SimpleNamespace(**case["spec"]))
        self.assertEqual(output["output_electrical_input_last_w"], 0.0)
        self.assertEqual(output["output_power_balance_residual_last_w"], -6.440323385880566)
        self.assertEqual(output["output_power_balance_residual_last_pct"], -644.0323385880566)
        self.assertLess(output["output_mech_power_last_w"], 0)
        self.assertTrue(math.isnan(output["output_efficiency_last_pct"]))
        self.assertIn("max(abs(Pin),1W)", output["output_power_balance_residual_normalization"])
        self.assertEqual(runner.output_physics_issues(output, operation="no_load"), [])
        self.assertFalse(any("no_load_loss_normalized" in key for key in output))

    def test_resistance_perturbation_changes_pin_and_cu_without_validating_resistance(self):
        times = np.linspace(0, 4, 361)
        reports = {}
        with tempfile.TemporaryDirectory() as tmp:
            arrays = {"PPT_Phase_Currents": {}, "PPT_Flux_Linkages": {}}
            for phase, angle in zip("ABC", [0, -2*np.pi/3, 2*np.pi/3]):
                current = 100*np.cos(2*np.pi*times+angle)
                arrays["PPT_Phase_Currents"][f"InputCurrent(Phase{phase}) [A]"] = current
                arrays["PPT_Flux_Linkages"][f"FluxLinkage(Phase{phase}) [Wb]"] = .01*current
            for name, columns in arrays.items():
                path = Path(tmp)/(name+".csv")
                pd.DataFrame({"Time [s]": times, **columns}).to_csv(path, index=False)
                reports["artifact_report_"+name] = str(path)
            results = []
            for resistance in [.1, .2]:
                spec = IPMSMPPTSpec(base_rpm=15, transient_periods=4, phase_resistance_ohm=resistance)
                output = analyze_exported_waveforms(reports, spec)
                output.update(output_torque_last_avg_nm=0., output_torque_last_min_nm=0.,
                              output_torque_last_max_nm=0., output_coreloss_last_avg_w=0., output_solidloss_last_avg_w=0.)
                runner.add_derived_motor_metrics(output, spec)
                results.append(output)
        first, second = results
        self.assertAlmostEqual(second["output_electrical_input_last_w"]-first["output_electrical_input_last_w"], 1500., places=8)
        self.assertAlmostEqual(second["output_copperloss_last_avg_w"]-first["output_copperloss_last_avg_w"], 1500., places=8)
        for output in results:
            self.assertAlmostEqual(output["output_electrical_input_last_w"]-output["output_mech_power_last_w"]-output["output_total_loss_last_avg_w"], 0., places=8)
            self.assertIn("cannot independently validate", output["output_power_balance_shared_copper_term"])

    def test_csv_preserves_definition_metadata_and_failed_diagnostic_values(self):
        case = next(x for x in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"] if x["name"] == "no_load")
        output = dict(case["input"])
        runner.add_derived_motor_metrics(output, SimpleNamespace(**case["spec"]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"diagnostic.csv"
            runner.append_result_row(path, {"case_id": "scope_only", "status": "failed", **output})
            with path.open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(float(row["output_power_balance_residual_last_w"]), -6.440323385880566)
        for key in SCOPES:
            self.assertEqual(row[key], output[key])


if __name__ == "__main__":
    unittest.main()
