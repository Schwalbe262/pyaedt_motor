import tempfile
import unittest
from pathlib import Path

import pandas as pd

from pyaedt_motor.experiments import diagnostic_plan
from tests.test_calibrate_ipmsm_beta import zero_manifest
import run_ipmsm_batch as runner


class ExperimentPlanTests(unittest.TestCase):
    def test_replay_does_not_claim_old_operating_values_or_old_setup_seals(self):
        row = dict(status="ok", case_id="old", geometry_group_id="group1",
                   input_stator_outer_radius=140, input_base_rpm=1200,
                   input_model_extent="full_360", input_symmetry_factor=1,
                   input_beta_convention="dq_current_advance_v2",
                   input_i_peak_a=50, input_setup_fingerprint="old-seal",
                   input_transient_stop_time_s=999, input_commanded_iq_peak_a=999)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            pd.DataFrame([row]).to_csv(source, index=False)
            plan = diagnostic_plan(source, 1, calibration=zero_manifest(), case_prefix="new_profile")
            repeated = diagnostic_plan(source, 1, calibration=zero_manifest(), case_prefix="new_profile")
        self.assertEqual(plan, repeated)
        self.assertEqual(len({case["case_id"] for case in plan}), 14)
        for case in plan:
            self.assertNotIn("transient_stop_time_s", case)
            self.assertNotIn("setup_fingerprint", case)
            self.assertNotIn("commanded_iq_peak_a", case)
            self.assertEqual(case["electrical_zero_deg"], 30)
            spec = runner.build_spec(case)
            self.assertEqual(spec.i_peak_a, 100)
            self.assertEqual(spec.electrical_zero_deg, 30)
        self.assertEqual({case["base_rpm"] for case in plan}, {1200, 5000})

    def test_bad_prefix_is_rejected_before_loading_inputs(self):
        with self.assertRaisesRegex(ValueError, "safe identifier"):
            diagnostic_plan(Path("unused.csv"), 1, case_prefix="../escape")


if __name__ == "__main__":
    unittest.main()
