from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import run_ipmsm_v4_pipeline_executor as executor


class ContractError(RuntimeError):
    pass


class StateError(RuntimeError):
    pass


class V4ExecutorTests(unittest.TestCase):
    def fake_api(self, run: mock.Mock, *, result: int = 0) -> SimpleNamespace:
        return SimpleNamespace(
            MAX_TRANSITIONS=16,
            PipelineContractError=ContractError,
            PipelineStateError=StateError,
            main=mock.Mock(return_value=result),
            os=SimpleNamespace(getpid=lambda: 1234),
            v3=SimpleNamespace(subprocess=SimpleNamespace(run=run)),
        )

    def test_executes_v4_supervisor_with_persistent_logs_and_removes_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "contract.json"
            contract.write_text("{}", encoding="utf-8")
            pid = root / "executor.pid"
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            child_run = mock.Mock(return_value=object())
            api = self.fake_api(child_run, result=7)

            def execute(argv: list[str]) -> int:
                api.v3.subprocess.run(["child"])
                return 7

            api.main.side_effect = execute
            code = executor.main(
                [
                    "--contract",
                    str(contract),
                    "--pid-file",
                    str(pid),
                    "--stdout-log",
                    str(stdout),
                    "--stderr-log",
                    str(stderr),
                    "--max-transitions",
                    "9",
                ],
                api=api,
            )

            self.assertEqual(code, 7)
            self.assertFalse(pid.exists())
            self.assertIn("pipeline_v4_executor_start", stdout.read_text(encoding="utf-8"))
            self.assertEqual(stderr.read_text(encoding="utf-8"), "")
            api.main.assert_called_once_with(
                [
                    "--contract",
                    str(contract.resolve()),
                    "--execute",
                    "--max-transitions",
                    "9",
                ]
            )
            self.assertIs(api.v3.subprocess.run, child_run)
            self.assertIsNot(
                child_run.call_args.kwargs["stdout"],
                child_run.call_args.kwargs["stderr"],
            )

    def test_expected_pipeline_error_is_logged_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "contract.json"
            contract.write_text("{}", encoding="utf-8")
            api = self.fake_api(mock.Mock())
            api.main.side_effect = StateError("blocked")

            code = executor.main(
                [
                    "--contract",
                    str(contract),
                    "--pid-file",
                    str(root / "pid"),
                    "--stdout-log",
                    str(root / "stdout.log"),
                    "--stderr-log",
                    str(root / "stderr.log"),
                ],
                api=api,
            )

            self.assertEqual(code, 2)
            self.assertIn("ERROR: blocked", (root / "stderr.log").read_text(encoding="utf-8"))

    def test_rejects_transition_count_before_creating_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "contract.json"
            contract.write_text("{}", encoding="utf-8")
            api = self.fake_api(mock.Mock())
            with self.assertRaisesRegex(ValueError, "between 1 and 16"):
                executor.main(
                    [
                        "--contract",
                        str(contract),
                        "--pid-file",
                        str(root / "pid"),
                        "--stdout-log",
                        str(root / "stdout.log"),
                        "--stderr-log",
                        str(root / "stderr.log"),
                        "--max-transitions",
                        "17",
                    ],
                    api=api,
                )
            self.assertFalse((root / "pid").exists())


if __name__ == "__main__":
    unittest.main()
