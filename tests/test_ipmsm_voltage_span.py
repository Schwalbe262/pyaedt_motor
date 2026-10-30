import math
import unittest

import numpy as np

from module.ipmsm_waveform_quality import three_phase_voltage_metrics


class VoltageSpanTests(unittest.TestCase):
    def angles(self):
        return np.arange(3600) * 2 * np.pi / 3600 - np.arange(3)[:, None] * 2 * np.pi / 3

    def test_balanced_sine_reproduces_line_voltage_relation(self):
        result = three_phase_voltage_metrics(100 * np.cos(self.angles()))
        self.assertAlmostEqual(result["output_voltage_span_last_peak_v"], 100 * math.sqrt(3), places=9)
        self.assertAlmostEqual(result["output_svm_centered_phase_last_peak_abs_v"], 50 * math.sqrt(3), places=9)

    def test_common_mode_does_not_consume_three_wire_line_voltage_capacity(self):
        angles = self.angles()
        phase = 80 * np.cos(angles)
        shifted = phase + 100 * np.cos(3 * angles[0])
        original = three_phase_voltage_metrics(phase)
        with_offset = three_phase_voltage_metrics(shifted)
        self.assertAlmostEqual(original["output_voltage_span_last_peak_v"],
                               with_offset["output_voltage_span_last_peak_v"], places=9)
        self.assertGreater(np.max(np.abs(shifted)), 200 / math.sqrt(3))
        self.assertLess(with_offset["output_voltage_span_last_peak_v"], .95 * 200)

    def test_phase_peak_proxy_can_accept_an_unrealizable_harmonic_request(self):
        angles = self.angles()
        phases = 110 * np.cos(angles) - 10 * np.cos(5 * angles)
        # Each phase fits the old sinusoidal-amplitude test.
        self.assertLess(np.max(np.abs(phases)), .95 * 200 / math.sqrt(3))
        result = three_phase_voltage_metrics(phases)
        self.assertGreater(result["output_voltage_span_last_peak_v"], 200)
        # Independent duty calculation proves saturation even without a reserve.
        offset = (np.max(phases, axis=0) + np.min(phases, axis=0)) / 2
        duties = .5 + (phases - offset) / 200
        self.assertGreater(duties.max(), 1)
        self.assertLess(duties.min(), 0)

    def test_incomplete_or_nonfinite_voltage_phases_are_rejected(self):
        for values in [np.ones((2, 20)), np.ones((3, 1)), np.full((3, 20), np.nan)]:
            with self.subTest(shape=values.shape), self.assertRaises(ValueError):
                three_phase_voltage_metrics(values)


if __name__ == "__main__":
    unittest.main()
