"""Case parsing and provenance for opt-in annular air-gap mapping."""
import unittest

import run_ipmsm_batch as runner


class AirgapCaseInputTests(unittest.TestCase):
    def test_absent_and_blank_mapping_preserve_default_setup_identity(self):
        baseline = runner.build_spec({})
        self.assertIsNone(baseline.airgap_band_mapping_angle_deg)
        expected = runner.setup_fingerprint(baseline, "default", "sin_current", False)
        for case in ({"airgap_band_mapping_angle_deg": ""},
                     {"input_airgap_band_mapping_angle_deg": ""}):
            with self.subTest(case=case):
                actual = runner.build_spec(case)
                self.assertIsNone(actual.airgap_band_mapping_angle_deg)
                self.assertEqual(runner.setup_fingerprint(actual, "default", "sin_current", False), expected)

    def test_csv_and_result_alias_parse_same_mapping(self):
        for key in ("airgap_band_mapping_angle_deg", "input_airgap_band_mapping_angle_deg"):
            with self.subTest(key=key):
                spec = runner.build_spec({key: "0.3", "band_circle_segments": "0"})
                self.assertEqual(spec.airgap_band_mapping_angle_deg, 0.3)
                self.assertEqual(spec.band_circle_segments, 0)

    def test_invalid_mapping_rejected_before_native_creation(self):
        for value in ("0", "-0.1", "3.0001", "nan", "inf", "true", True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runner.build_spec({"airgap_band_mapping_angle_deg": value, "band_circle_segments": "0"})

    def test_mapping_requires_curved_band(self):
        with self.assertRaises(ValueError):
            runner.build_spec({"airgap_band_mapping_angle_deg": "0.3"})

    def test_mapping_is_in_setup_fingerprint_and_export_columns(self):
        specs = [runner.build_spec({"airgap_band_mapping_angle_deg": str(angle), "band_circle_segments": "0"})
                 for angle in (0.3, 0.6)]
        fingerprints = [runner.setup_fingerprint(spec, "same_profile", "sin_current", False) for spec in specs]
        self.assertNotEqual(*fingerprints)
        self.assertEqual(runner.INPUT_SPEC_COLUMNS.count("input_airgap_band_mapping_angle_deg"), 1)


if __name__ == "__main__":
    unittest.main()
