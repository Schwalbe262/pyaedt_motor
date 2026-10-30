"""Check physical scaling on independent operating points, not noisy FEA labels."""
import unittest

import numpy as np
import pandas as pd

from pyaedt_motor.surrogate import (
    GEOMETRY, PrimitiveModel, derive_performance, physical_features,
)


def inputs(currents, lengths):
    size = len(currents)
    frame = pd.DataFrame({"input_" + name: np.ones(size) for name in GEOMETRY})
    frame["input_stator_outer_radius"] = 140.0
    frame["input_i_peak_a"] = currents
    frame["input_beta_dq_deg"] = 30.0
    frame["input_stack_length_mm"] = lengths
    frame["input_base_rpm"] = 1200.0
    frame["input_phase_resistance_ohm"] = .02
    return frame


class PhysicalSurrogateTests(unittest.TestCase):
    def test_scaled_motor_generalizes_across_unseen_current_and_stack_length(self):
        fit = inputs([50, 75, 100, 125], [40, 50, 60, 70])
        # Unsaturated PM torque = flux constant per stack length * L * Iq.
        target = .01 * fit.input_stack_length_mm * fit.input_i_peak_a * np.cos(np.pi / 6)
        model = PrimitiveModel.fit(fit, "output_torque_last_avg_nm", target, "physical_extra", jobs=1)
        unseen = inputs([20, 140], [45, 65])
        expected = .01 * unseen.input_stack_length_mm * unseen.input_i_peak_a * np.cos(np.pi / 6)
        np.testing.assert_allclose(model.predict(unseen), expected, rtol=1e-12)

    def test_features_cannot_leak_measured_fea_currents_or_targets(self):
        frame = inputs([100, 120], [50, 60])
        expected = physical_features(frame)
        frame["input_stator_shoe_thick"] = [1, 999]
        frame["output_id_current_last_avg_a"] = [999, -999]
        frame["output_torque_last_avg_nm"] = [1e6, -1e6]
        pd.testing.assert_frame_equal(physical_features(frame), expected)

    def test_derived_performance_closes_loss_and_power_identities(self):
        frame = inputs([100], [50])
        primitive = pd.DataFrame({"output_torque_last_avg_nm": [40.0],
                                  "output_coreloss_last_avg_w": [100.0],
                                  "output_solidloss_last_avg_w": [20.0]})
        result = derive_performance(frame, primitive)
        self.assertAlmostEqual(result.output_total_loss_last_avg_w.iloc[0], 420.0)
        pout = 40 * 1200 * 2 * np.pi / 60
        self.assertAlmostEqual(result.output_efficiency_last_pct.iloc[0], 100 * pout / (pout + 420))

    def test_zero_current_is_not_silently_used_for_loaded_model(self):
        frame = inputs([0], [50])
        with self.assertRaisesRegex(ValueError, "q-axis current"):
            PrimitiveModel.fit(frame, "output_torque_last_avg_nm", [1], "physical_extra", jobs=1)


if __name__ == "__main__":
    unittest.main()
