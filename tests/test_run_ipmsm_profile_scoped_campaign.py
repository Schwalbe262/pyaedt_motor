from __future__ import annotations

import unittest
from unittest import mock

import collect_ipmsm_v2_campaign as collector
import run_ipmsm_profile_scoped_campaign as scoped


EXPECTED = {
    "time_138_p12_baseline": 1,
    "time_135_p12_iron525": 1,
}


def rows() -> list[dict[str, str]]:
    return [
        {
            "input_quality_profile": "time_138_p12_baseline",
            "input_setup_fingerprint": "setup-baseline",
            "input_material_fingerprint": "material-v2",
            "input_aedt_version": "2025.2",
        },
        {
            "input_quality_profile": "time_135_p12_iron525",
            "input_setup_fingerprint": "setup-iron525",
            "input_material_fingerprint": "material-v2",
            "input_aedt_version": "2025.2",
        },
    ]


class ProfileScopedCampaignTests(unittest.TestCase):
    def test_explicit_counts_and_scoped_fingerprints_accept_exact_pair(self) -> None:
        parsed = scoped.parse_expected_profile_counts(
            ["time_138_p12_baseline=1", "time_135_p12_iron525=1"]
        )
        self.assertEqual(parsed, EXPECTED)
        scoped.validate_profile_scoped_fingerprints(rows(), parsed)

    def test_counts_and_fingerprints_fail_closed(self) -> None:
        for values in (None, [], ["blank"], ["=1"], ["profile=0"], ["profile=01"]):
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                scoped.parse_expected_profile_counts(values)
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            scoped.parse_expected_profile_counts(["profile=1", "profile=2"])

        mutations = {
            "wrong_count": (0, "input_quality_profile", "time_135_p12_iron525"),
            "blank_setup": (0, "input_setup_fingerprint", ""),
            "material_drift": (0, "input_material_fingerprint", "material-other"),
            "aedt_drift": (0, "input_aedt_version", "2026.1"),
        }
        for label, (index, column, value) in mutations.items():
            with self.subTest(label=label), self.assertRaises(RuntimeError):
                candidate = rows()
                candidate[index][column] = value
                scoped.validate_profile_scoped_fingerprints(candidate, EXPECTED)

        repeated = rows() + [dict(rows()[0]), dict(rows()[1])]
        repeated[2]["input_setup_fingerprint"] = "setup-drift"
        with self.assertRaisesRegex(RuntimeError, "mix or omit input_setup_fingerprint"):
            scoped.validate_profile_scoped_fingerprints(
                repeated,
                {profile: 2 for profile in EXPECTED},
            )

        reused = rows()
        reused[1]["input_setup_fingerprint"] = reused[0]["input_setup_fingerprint"]
        with self.assertRaisesRegex(RuntimeError, "reuse input_setup_fingerprint"):
            scoped.validate_profile_scoped_fingerprints(reused, EXPECTED)

    def test_main_scopes_and_restores_collector_hook(self) -> None:
        original = collector.validate_homogeneous_fingerprints

        def campaign_main(argv: list[str]) -> int:
            self.assertEqual(argv, ["--cases", "pilot.csv", "--project", "project"])
            self.assertIsNot(collector.validate_homogeneous_fingerprints, original)
            collector.validate_homogeneous_fingerprints(rows())
            return 7

        argv = [
            "--expected-profile-count",
            "time_138_p12_baseline=1",
            "--cases",
            "pilot.csv",
            "--expected-profile-count",
            "time_135_p12_iron525=1",
            "--project",
            "project",
        ]
        with mock.patch.object(scoped.campaign, "main", side_effect=campaign_main):
            self.assertEqual(scoped.main(argv), 7)
        self.assertIs(collector.validate_homogeneous_fingerprints, original)

        with mock.patch.object(
            scoped.campaign, "main", side_effect=RuntimeError("boom")
        ), self.assertRaisesRegex(RuntimeError, "boom"):
            scoped.main(
                [
                    "--expected-profile-count",
                    "time_138_p12_baseline=1",
                    "--expected-profile-count",
                    "time_135_p12_iron525=1",
                ]
            )
        self.assertIs(collector.validate_homogeneous_fingerprints, original)


if __name__ == "__main__":
    unittest.main()
