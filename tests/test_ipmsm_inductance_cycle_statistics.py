from __future__ import annotations

import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd

import run_ipmsm_batch as runner


SPEC = SimpleNamespace(base_rpm=60., pole_number=2, electrical_zero_deg=17., initial_position_deg=-22.5)


def isotropic_report(times, values, *, nanoseconds=False):
    times, values = np.asarray(times), np.asarray(values)
    frame = pd.DataFrame({"Time [ns]" if nanoseconds else "Time [s]": times * (1e9 if nanoseconds else 1)})
    for _, source, target in runner.INDUCTANCE_MATRIX_ENTRIES:
        frame[f"L({source},{target}) (Real) [nH]"] = (values if source == target else np.zeros(len(times))) * 1e9
        # A nonzero imaginary decoy must not be used for the real transient matrix.
        frame[f"L({source},{target}) (Imag) [nH]"] = np.full(len(times), 9e9)
    return frame


class InductanceCycleStatisticsTests(unittest.TestCase):
    def test_uniform_closed_cycle_integrates_endpoint_once_for_all_outputs(self):
        t = np.linspace(0., 2., 9)
        frame = isotropic_report(t, .002 + .001 * np.cos(2*np.pi*t))
        result = runner.summarize_inductance_matrix(frame, SPEC, 1., 2.)
        for window in ("first", "last", "all"):
            for prefix in ("output_inductance_aa", "output_inductance_bb", "output_inductance_cc", "output_ld", "output_lq"):
                self.assertAlmostEqual(result[f"{prefix}_{window}_avg_h"], .002, places=14)
                self.assertAlmostEqual(result[f"{prefix}_{window}_rms_h"], math.sqrt(4.5)*.001, places=14)
                self.assertAlmostEqual(result[f"{prefix}_{window}_min_h"], .001, places=14)
                self.assertAlmostEqual(result[f"{prefix}_{window}_max_h"], .003, places=14)
            for key, source, target in runner.INDUCTANCE_MATRIX_ENTRIES:
                if source != target:
                    self.assertEqual(result[f"output_inductance_{key}_{window}_avg_h"], 0.)
            self.assertAlmostEqual(result[f"output_saliency_ratio_{window}_avg_ratio"], 1., places=14)
        sampled = runner.summarize_inductance_matrix(frame, SPEC, 1., 2., integrate_cycles=False)
        self.assertAlmostEqual(sampled["output_ld_last_avg_h"], .0022, places=14)

    def test_nonuniform_samples_are_time_weighted(self):
        t = np.array([0., .1, .7, 1., 1.1, 1.8, 2.])
        frame = isotropic_report(t, .001 * (1+t))
        result = runner.summarize_inductance_matrix(frame, SPEC, 1., 2.)
        for window, expected in [("first", .0015), ("last", .0025), ("all", .002)]:
            for prefix in ("output_inductance_aa", "output_ld", "output_lq"):
                self.assertAlmostEqual(result[f"{prefix}_{window}_avg_h"], expected, places=14)
        sampled = runner.summarize_inductance_matrix(frame, SPEC, 1., 2., integrate_cycles=False)
        self.assertNotAlmostEqual(sampled["output_inductance_aa_last_avg_h"], result["output_inductance_aa_last_avg_h"], places=8)

    def test_windows_interpolate_both_boundaries_between_samples(self):
        t = np.array([-.2, .2, .8, 1.3, 1.9])
        result = runner.summarize_inductance_matrix(isotropic_report(t, .001*(1+t)), SPEC, 1., 1.7)
        for window, avg, lo, hi in [("first", .0015, .001, .002), ("last", .0022, .0017, .0027), ("all", .00185, .001, .0027)]:
            for prefix in ("output_inductance_aa", "output_ld", "output_lq"):
                self.assertAlmostEqual(result[f"{prefix}_{window}_avg_h"], avg, places=14)
                self.assertAlmostEqual(result[f"{prefix}_{window}_min_h"], lo, places=14)
                self.assertAlmostEqual(result[f"{prefix}_{window}_max_h"], hi, places=14)

    def test_rotating_anisotropic_apparent_matrix_uses_calibrated_dq_frame(self):
        t = np.linspace(0., 2., 41)
        data = {"Time [s]": t}
        ld, lq = .003+.001*t, .007-.0005*t
        for _, source, target in runner.INDUCTANCE_MATRIX_ENTRIES:
            i, j = "ABC".index(source[-1]), "ABC".index(target[-1])
            values = []
            for k, time in enumerate(t):
                theta = 2*np.pi*time + math.radians(SPEC.electrical_zero_deg)
                angles = theta + np.array([0., -2*np.pi/3, 2*np.pi/3])
                inverse = np.stack([np.cos(angles), -np.sin(angles)], axis=1)
                park = (2/3)*inverse.T
                values.append((inverse @ np.array([[ld[k], .0002], [.0002, lq[k]]]) @ park)[i,j])
            data[f"L({source},{target}) [H]"] = values
        result = runner.summarize_inductance_matrix(pd.DataFrame(data), SPEC, 1., 2.)
        for window, expected_d, expected_q in [("first", .0035, .00675), ("last", .0045, .00625), ("all", .004, .0065)]:
            self.assertAlmostEqual(result[f"output_ld_{window}_avg_h"], expected_d, places=14)
            self.assertAlmostEqual(result[f"output_lq_{window}_avg_h"], expected_q, places=14)
        self.assertNotAlmostEqual(result["output_saliency_ratio_all_avg_ratio"], .0065/.004, places=3)

    def test_real_nH_ns_semicolon_export_uses_integrated_default(self):
        t = np.linspace(0., 2., 9)
        frame = isotropic_report(t, .002+.001*np.cos(2*np.pi*t), nanoseconds=True)
        frame.insert(0, "BaseRPM", 60.)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"matrix.csv"
            frame.to_csv(path, sep=";", index=False)
            parsed = runner.read_report_csv(str(path))
        result = runner.summarize_inductance_matrix(parsed, SPEC, 1., 2.)
        self.assertAlmostEqual(result["output_ld_last_avg_h"], .002, places=14)
        self.assertAlmostEqual(result["output_lq_last_avg_h"], .002, places=14)
        self.assertAlmostEqual(result["output_inductance_aa_all_avg_h"], .002, places=14)

    def test_one_sample_requires_explicit_diagnostic_opt_out(self):
        frame = isotropic_report([.25], [.003])
        strict = runner.summarize_inductance_matrix(frame, SPEC, 1., 1.)
        self.assertTrue(all(math.isnan(value) for value in strict.values()))
        sampled = runner.summarize_inductance_matrix(frame, SPEC, 1., 1., integrate_cycles=False)
        self.assertAlmostEqual(sampled["output_ld_last_avg_h"], .003, places=14)
        self.assertAlmostEqual(sampled["output_lq_last_avg_h"], .003, places=14)

    def test_missing_first_cycle_coverage_does_not_invalidate_complete_last_cycle(self):
        t = np.array([.5, 1., 1.5, 2.])
        result = runner.summarize_inductance_matrix(isotropic_report(t, .001*(1+t)), SPEC, 1., 2.)
        self.assertTrue(math.isnan(result["output_ld_first_avg_h"]))
        self.assertTrue(math.isnan(result["output_ld_all_avg_h"]))
        self.assertAlmostEqual(result["output_ld_last_avg_h"], .0025, places=14)

    def test_missing_last_cycle_coverage_propagates_nan_required_targets(self):
        t = np.array([0., .5, 1., 1.5])
        result = runner.summarize_inductance_matrix(isotropic_report(t, np.full(4, .003)), SPEC, 1., 2.)
        for prefix in ("output_inductance_aa", "output_ld", "output_lq"):
            self.assertTrue(math.isnan(result[f"{prefix}_last_avg_h"]))
            self.assertTrue(math.isnan(result[f"{prefix}_all_avg_h"]))
            self.assertAlmostEqual(result[f"{prefix}_first_avg_h"], .003, places=14)

    def test_nonfinite_matrix_sample_is_not_dropped_or_repaired(self):
        for bad in (math.nan, math.inf):
            with self.subTest(bad=bad):
                frame = isotropic_report([0., .5, 1.], [.003, .003, .003])
                frame.loc[1, "L(PhaseA,PhaseA) (Real) [nH]"] = bad
                result = runner.summarize_inductance_matrix(frame, SPEC, 1., 1.)
                for prefix in ("output_inductance_aa", "output_ld", "output_lq"):
                    self.assertTrue(math.isnan(result[f"{prefix}_last_avg_h"]))

    def test_nonfinite_timestamp_cannot_prove_any_cycle_coverage(self):
        frame = isotropic_report([0., .5, 1.], [.003, .003, .003])
        frame.loc[1, "Time [s]"] = math.nan
        result = runner.summarize_inductance_matrix(frame, SPEC, 1., 1.)
        self.assertTrue(all(math.isnan(value) for value in result.values()))


if __name__ == "__main__":
    unittest.main()
