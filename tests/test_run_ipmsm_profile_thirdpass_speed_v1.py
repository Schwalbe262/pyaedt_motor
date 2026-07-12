from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

import collect_ipmsm_v2_campaign as collector
import run_ipmsm_profile_thirdpass_speed_v1 as profile_runner


def result_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for profile, setup in (
        ("time_138_p12_baseline", "setup-baseline"),
        ("time_135_p12_iron525", "setup-iron525"),
    ):
        rows.extend(
            {
                "input_quality_profile": profile,
                "input_setup_fingerprint": setup,
                "input_material_fingerprint": "material-v2",
                "input_aedt_version": "2025.2",
            }
            for _ in range(12)
        )
    return rows


class ProfileThirdpassSpeedRunnerTests(unittest.TestCase):
    def test_powershell_wrapper_uses_scoped_runner_and_strict_finalizer(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "run_ipmsm_profile_thirdpass_speed_v1.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("run_ipmsm_profile_thirdpass_speed_v1.py", script)
        self.assertIn("finalize_ipmsm_profile_thirdpass_speed_v1.py", script)
        self.assertIn("'--collection-dir', $outputDir", script)
        self.assertIn("'--output-dir', $analysisDir", script)
        self.assertIn("'--project-active-cap', '50'", script)
        self.assertIn("$finalizerArguments += '--execute'", script)
        self.assertIn("if ($DryRun)", script)
        self.assertIn("'profile_thirdpass_speed_v1.dryrun'", script)
        self.assertNotIn("$resultCount -eq 24", script)

    def test_profile_scoped_fingerprints_accept_exact_pair(self) -> None:
        profile_runner.validate_profile_scoped_fingerprints(result_rows())

    def test_profile_scoped_fingerprints_reject_drift_and_wrong_scope(self) -> None:
        mutations = {
            "profile_count": (0, "input_quality_profile", "time_135_p12_iron525"),
            "setup_blank": (0, "input_setup_fingerprint", ""),
            "setup_drift": (0, "input_setup_fingerprint", "setup-other"),
            "material_drift": (0, "input_material_fingerprint", "material-other"),
            "aedt_drift": (0, "input_aedt_version", "2026.1"),
        }
        for label, (index, column, value) in mutations.items():
            with self.subTest(label=label), self.assertRaises(RuntimeError):
                rows = result_rows()
                rows[index][column] = value
                profile_runner.validate_profile_scoped_fingerprints(rows)

        with self.assertRaisesRegex(RuntimeError, "reuse input_setup_fingerprint"):
            rows = result_rows()
            for row in rows:
                row["input_setup_fingerprint"] = "shared-setup"
            profile_runner.validate_profile_scoped_fingerprints(rows)

    def test_main_installs_validator_only_for_campaign_call(self) -> None:
        original = collector.validate_homogeneous_fingerprints

        def run(argv: list[str] | None) -> int:
            self.assertIs(
                collector.validate_homogeneous_fingerprints,
                profile_runner.validate_profile_scoped_fingerprints,
            )
            self.assertEqual(argv, ["--probe"])
            return 7

        with mock.patch.object(profile_runner.campaign, "main", side_effect=run):
            self.assertEqual(profile_runner.main(["--probe"]), 7)
        self.assertIs(collector.validate_homogeneous_fingerprints, original)

    def test_main_restores_validator_after_failure(self) -> None:
        original = collector.validate_homogeneous_fingerprints
        with mock.patch.object(
            profile_runner.campaign, "main", side_effect=RuntimeError("boom")
        ), self.assertRaisesRegex(RuntimeError, "boom"):
            profile_runner.main([])
        self.assertIs(collector.validate_homogeneous_fingerprints, original)


if __name__ == "__main__":
    unittest.main()
