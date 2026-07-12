from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import run_ipmsm_pipeline_supervisor as entrypoint


class PipelineSupervisorEntrypointTests(unittest.TestCase):
    def test_guard_rejects_legacy_optimization_and_speed_actions(self) -> None:
        executor = mock.Mock()
        for action in sorted(entrypoint.LEGACY_UNAUTHORIZED_DOWNSTREAM_ACTIONS):
            with self.subTest(action=action), self.assertRaises(
                entrypoint.supervisor.PipelineStateError
            ):
                entrypoint._execute_with_authorization_guard(
                    executor,
                    object(),
                    SimpleNamespace(next_action=action),
                )
        executor.assert_not_called()

    def test_guard_allows_stage1_stage2_and_stage3_actions(self) -> None:
        executor = mock.Mock(return_value="ok")
        for action in ("run_stage1_campaign", "run_stage2_fresh", "run_stage3_resume"):
            with self.subTest(action=action):
                result = entrypoint._execute_with_authorization_guard(
                    executor,
                    "contract",
                    SimpleNamespace(next_action=action),
                )
                self.assertEqual(result, "ok")
        self.assertEqual(executor.call_count, 3)

    def test_main_installs_and_restores_authorization_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "contract.json"
            contract.write_text("{}", encoding="utf-8")
            original = entrypoint.supervisor.execute_action

            def fake_main(_: list[str]) -> int:
                with self.assertRaises(entrypoint.supervisor.PipelineStateError):
                    entrypoint.supervisor.execute_action(
                        object(),
                        SimpleNamespace(next_action="run_optimization_fresh"),
                    )
                return 0

            with mock.patch.object(entrypoint.supervisor, "main", side_effect=fake_main):
                code = entrypoint.main(
                    [
                        "--contract",
                        str(contract),
                        "--pid-file",
                        str(root / "supervisor.pid"),
                        "--stdout-log",
                        str(root / "stdout.log"),
                        "--stderr-log",
                        str(root / "stderr.log"),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIs(entrypoint.supervisor.execute_action, original)


if __name__ == "__main__":
    unittest.main()
