from __future__ import annotations

import unittest
from pathlib import Path


class TorqueUnitReplayLauncherTest(unittest.TestCase):
    def test_launcher_pins_sealed_plan_cap_and_forensic_retention(self) -> None:
        text = (
            Path(__file__).resolve().parents[1] / "run_ipmsm_torque_unit_replay.ps1"
        ).read_text(encoding="utf-8")
        required = (
            "torque_unit_replay_plan_sealed.csv",
            "--project-active-cap', '50",
            "--max-plan-cases', '4",
            "--task-prefix', 'ipmsm-v2-torqueunit-replay-v1",
            "--scheduling-profile', 'fea_bursty",
            "--max-workers-per-node', '1",
            "--keep-projects",
            "module load ansys-electronics/v252",
            "--required-capability', 'conda:pyaedt2026v1",
            "--env-profile', 'pyaedt2026v1",
            "--completed-result-settle-seconds', '300",
            "torque_unit_replay_supervisor.pid",
            "--submit",
        )
        for token in required:
            with self.subTest(token=token):
                self.assertIn(token, text)
        self.assertIn("& $python @arguments", text)
        self.assertIn("$ErrorActionPreference = 'Continue'", text)
        self.assertNotIn("Start-Process", text)


if __name__ == "__main__":
    unittest.main()
