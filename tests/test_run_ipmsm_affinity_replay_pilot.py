from __future__ import annotations

from pathlib import Path
import re
import unittest


class AffinityReplayPilotWrapperTests(unittest.TestCase):
    def test_wrapper_is_opt_in_and_uses_isolated_low_concurrency_paths(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "run_ipmsm_affinity_replay_pilot.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("[switch]$Submit", script)
        self.assertRegex(
            script,
            re.compile(
                r"if \(\$Submit\) \{\s*\$campaignArguments \+= '--submit'\s*\}",
                re.MULTILINE,
            ),
        )
        self.assertEqual(script.count("$campaignArguments += '--submit'"), 1)
        self.assertIn("run_ipmsm_profile_scoped_campaign.py", script)
        self.assertIn("'time_138_p12_baseline=1'", script)
        self.assertIn("'time_135_p12_iron525=1'", script)
        self.assertIn("'PYAEDT_MOTOR_IPMSM_V2'", script)
        self.assertIn("'--project-active-cap', '100'", script)
        self.assertIn("verified plan contains exactly two rows", script)
        self.assertIn("'--max-workers-per-node', '1'", script)
        self.assertIn("'--scheduling-profile', 'fea_bursty'", script)
        self.assertIn("'--required-capability', 'conda:pyaedt2026v1'", script)
        self.assertIn("'--env-profile', 'pyaedt2026v1'", script)
        self.assertIn("'--env-setup', 'module load ansys-electronics/v252'", script)
        self.assertIn("ipmsm-v2-affinityfix-replay-v1", script)
        self.assertIn("remote/ipmsm_v2_affinityfix_replay_v1", script)
        self.assertIn("simul_log/ipmsm_v2_affinityfix_replay_v1", script)
        self.assertIn("simulation/ipmsm_v2_affinityfix_replay_v1", script)
        self.assertIn("simul_log_scheduler/ipmsm_v2_affinityfix_replay_v1_logs", script)
        self.assertNotIn("finalize_ipmsm", script)


if __name__ == "__main__":
    unittest.main()
