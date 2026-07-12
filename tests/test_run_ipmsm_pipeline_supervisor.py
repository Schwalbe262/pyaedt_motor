from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import run_ipmsm_pipeline_supervisor as entrypoint


class PipelineSupervisorEntrypointTests(unittest.TestCase):
    def test_guard_rejects_every_legacy_post_campaign_action(self) -> None:
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

    def test_guard_allows_only_the_stage1_campaign_action(self) -> None:
        executor = mock.Mock(return_value="ok")
        result = entrypoint._execute_with_authorization_guard(
            executor,
            "contract",
            SimpleNamespace(next_action="run_stage1_campaign"),
        )
        self.assertEqual(result, "ok")
        executor.assert_called_once()

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
