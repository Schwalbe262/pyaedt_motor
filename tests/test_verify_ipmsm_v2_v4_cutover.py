from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_supervise_ipmsm_v2_pipeline_v4 import Fixture
import verify_ipmsm_v2_v4_cutover as verifier


class CutoverVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = Fixture(self.root)
        self.fixture.campaign()

        self.v3_action = verifier.TaskAction(
            executable="python.exe",
            arguments=(
                'run_ipmsm_pipeline_supervisor.py --contract '
                f'"{self.fixture.base_path}" --pid-file "{self.root / "v3.pid"}"'
            ),
            working_directory=str(self.root),
        )
        self.v4_action = verifier.TaskAction(
            executable="python.exe",
            arguments=(
                'run_ipmsm_v4_pipeline_supervisor.py --contract '
                f'"{self.fixture.v4_path}"'
            ),
            working_directory=str(self.root),
        )
        self.v3_task = verifier.TaskSnapshot(
            name="V3",
            exists=True,
            definition_sha256="1" * 64,
            actions=(self.v3_action,),
        )
        self.v4_task = verifier.TaskSnapshot(
            name="V4",
            exists=True,
            definition_sha256="2" * 64,
            actions=(self.v4_action,),
        )
        self.family_task = verifier.TaskSnapshot(name="FAMILY", exists=True)
        self.tasks = {
            "V3": self.v3_task,
            "V4": self.v4_task,
            "FAMILY": self.family_task,
        }
        self.process = verifier.ProcessSnapshot(pid=731, exists=False)
        self.lock = verifier.LockSnapshot(
            path=self.root / "pipeline.lock",
            exists=True,
            safe_regular_file=True,
            held=False,
            detail="injected unlocked fixture",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def policy(
        self,
        *,
        expected_v3: verifier.TaskSnapshot | None = None,
        expected_v4: verifier.TaskSnapshot | None = None,
    ) -> verifier.CutoverPolicy:
        v3_task = expected_v3 or self.v3_task
        v4_task = expected_v4 or self.v4_task
        return verifier.CutoverPolicy(
            base_contract=self.fixture.base_path,
            v4_contract=self.fixture.v4_path,
            expected_base_contract_sha256=(
                self.fixture.contract.base_contract.contract_sha256
            ),
            expected_v4_contract_sha256=self.fixture.contract.contract_sha256,
            v3_task_name="V3",
            v4_task_name="V4",
            family_task_name="FAMILY",
            expected_v3_definition_sha256=v3_task.definition_sha256,
            expected_v3_action_sha256=v3_task.action_sha256,
            expected_v4_definition_sha256=v4_task.definition_sha256,
            expected_v4_action_sha256=v4_task.action_sha256,
            v3_pid_file=self.root / "v3.pid",
            expected_v3_process_fragment="run_ipmsm_pipeline_supervisor.py",
            expected_v3_action_fragments=(
                "run_ipmsm_pipeline_supervisor.py",
                str(self.fixture.base_path),
            ),
            expected_v4_action_fragments=(
                "run_ipmsm_v4_pipeline_supervisor.py",
                str(self.fixture.v4_path),
            ),
        )

    def verify(
        self,
        policy: verifier.CutoverPolicy | None = None,
        *,
        pipeline_probe: verifier.PipelineProbe | None = None,
    ) -> dict[str, object]:
        def task_probe(name: str) -> verifier.TaskSnapshot:
            return self.tasks[name]

        def process_probe(pid: int) -> verifier.ProcessSnapshot:
            self.assertEqual(pid, self.process.pid)
            return self.process

        return verifier.verify_cutover(
            policy or self.policy(),
            task_probe=task_probe,
            process_probe=process_probe,
            lock_probe=lambda _path: self.lock,
            pipeline_probe=pipeline_probe,
        )

    def cli_argv(self) -> list[str]:
        return [
            "--base-contract",
            str(self.fixture.base_path),
            "--v4-contract",
            str(self.fixture.v4_path),
            "--expected-base-contract-sha256",
            self.fixture.contract.base_contract.contract_sha256,
            "--expected-v4-contract-sha256",
            self.fixture.contract.contract_sha256,
            "--v3-task-name",
            "V3",
            "--v4-task-name",
            "V4",
            "--family-task-name",
            "FAMILY",
            "--expected-v3-definition-sha256",
            self.v3_task.definition_sha256,
            "--expected-v3-action-sha256",
            self.v3_task.action_sha256,
            "--expected-v4-definition-sha256",
            self.v4_task.definition_sha256,
            "--expected-v4-action-sha256",
            self.v4_task.action_sha256,
            "--v3-pid-file",
            str(self.root / "v3.pid"),
            "--expected-v3-process-fragment",
            "run_ipmsm_pipeline_supervisor.py",
            "--expected-v3-action-fragment",
            str(self.fixture.base_path),
            "--expected-v4-action-fragment",
            str(self.fixture.v4_path),
        ]

    @staticmethod
    def blocker_codes(report: dict[str, object]) -> set[str]:
        return {item["code"] for item in report["blockers"]}  # type: ignore[index]

    def tree_payloads(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def test_ready_report_is_bounded_canonical_and_performs_no_writes(self) -> None:
        before = self.tree_payloads()

        report = self.verify()

        self.assertEqual(report["status"], "ready")
        self.assertTrue(report["ready"])
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["writes_performed"], 0)
        self.assertEqual(
            report["v4_contract"]["next_action"],  # type: ignore[index]
            "publish_stage1_official",
        )
        payload = verifier.canonical_json_bytes(report)
        self.assertLessEqual(len(payload), verifier.MAX_REPORT_BYTES)
        self.assertEqual(json.loads(payload), report)
        self.assertEqual(self.tree_payloads(), before)

    def test_incomplete_stage1_fails_closed(self) -> None:
        (self.root / "stage1-out" / "merged_results.csv").unlink()

        report = self.verify()

        self.assertEqual(report["status"], "not_ready")
        self.assertIn("stage1_incomplete", self.blocker_codes(report))
        self.assertEqual(report["writes_performed"], 0)

    def test_v3_enabled_running_task_fails_closed(self) -> None:
        self.tasks["V3"] = dataclasses.replace(
            self.v3_task, enabled=True, running=True
        )

        codes = self.blocker_codes(self.verify())

        self.assertIn("v3_task_enabled", codes)
        self.assertIn("v3_task_running", codes)

    def test_queued_or_unknown_task_state_fails_closed(self) -> None:
        scenarios = (
            ("V3", self.v3_task, "Queued", "v3_task_state_not_inert"),
            ("V4", self.v4_task, "Unknown", "v4_task_state_not_inert"),
            (
                "FAMILY",
                self.family_task,
                "Queued",
                "legacy_family_task_state_not_inert",
            ),
        )
        for name, snapshot, state, expected_code in scenarios:
            with self.subTest(name=name, state=state):
                original = self.tasks[name]
                self.tasks[name] = dataclasses.replace(snapshot, state=state)
                try:
                    self.assertIn(expected_code, self.blocker_codes(self.verify()))
                finally:
                    self.tasks[name] = original

    def test_missing_v4_contract_and_task_fail_closed(self) -> None:
        self.fixture.v4_path.unlink()
        self.tasks["V4"] = verifier.TaskSnapshot(name="V4", exists=False)

        codes = self.blocker_codes(self.verify())

        self.assertIn("v4_contract_missing", codes)
        self.assertIn("v4_task_missing", codes)

    def test_v4_contract_tamper_fails_closed(self) -> None:
        document = json.loads(self.fixture.v4_path.read_text(encoding="utf-8"))
        document["schema_version"] = "tampered"
        self.fixture.v4_path.write_text(json.dumps(document), encoding="utf-8")

        codes = self.blocker_codes(self.verify())

        self.assertIn("v4_contract_invalid", codes)

    def test_contract_policy_hash_mismatches_fail_closed(self) -> None:
        policy = dataclasses.replace(
            self.policy(),
            expected_base_contract_sha256="a" * 64,
            expected_v4_contract_sha256="b" * 64,
        )

        codes = self.blocker_codes(self.verify(policy))

        self.assertIn("base_contract_policy_hash_mismatch", codes)
        self.assertIn("v4_contract_hash_mismatch", codes)

    def test_v4_task_definition_action_and_binding_drift_fail_closed(self) -> None:
        drifted_action = dataclasses.replace(
            self.v4_action,
            arguments="run_ipmsm_v4_pipeline_supervisor.py --contract foreign.json",
        )
        self.tasks["V4"] = dataclasses.replace(
            self.v4_task,
            definition_sha256="9" * 64,
            actions=(drifted_action,),
        )

        codes = self.blocker_codes(self.verify())

        self.assertIn("v4_task_definition_drift", codes)
        self.assertIn("v4_task_action_drift", codes)
        self.assertIn("v4_task_action_binding", codes)
        self.assertIn("v4_task_contract_mismatch", codes)

    def test_v3_pid_binding_is_semantic_not_just_hash_or_fragment(self) -> None:
        foreign_action = dataclasses.replace(
            self.v3_action,
            arguments=self.v3_action.arguments.replace("v3.pid", "foreign.pid"),
        )
        expected = dataclasses.replace(self.v3_task, actions=(foreign_action,))
        self.tasks["V3"] = expected

        codes = self.blocker_codes(self.verify(self.policy(expected_v3=expected)))

        self.assertIn("v3_task_pid_mismatch", codes)

    def test_relative_task_path_with_blank_workdir_fails_closed(self) -> None:
        relative_action = dataclasses.replace(
            self.v4_action,
            arguments=(
                "run_ipmsm_v4_pipeline_supervisor.py "
                "--contract pipeline-v4.json"
            ),
            working_directory="",
        )
        expected = dataclasses.replace(self.v4_task, actions=(relative_action,))
        self.tasks["V4"] = expected
        policy = dataclasses.replace(
            self.policy(expected_v4=expected),
            expected_v4_action_fragments=(
                "run_ipmsm_v4_pipeline_supervisor.py",
                "pipeline-v4.json",
            ),
        )

        codes = self.blocker_codes(self.verify(policy))

        self.assertIn("v4_task_contract_mismatch", codes)

    def test_quoted_mutating_v4_action_is_rejected_even_when_hash_matches(self) -> None:
        mutating_action = dataclasses.replace(
            self.v4_action,
            arguments=self.v4_action.arguments + ' "--execute"',
        )
        expected = dataclasses.replace(self.v4_task, actions=(mutating_action,))
        self.tasks["V4"] = expected

        codes = self.blocker_codes(self.verify(self.policy(expected_v4=expected)))

        self.assertIn("v4_task_mutating_action", codes)

    def test_enabled_or_running_legacy_family_watcher_fails_closed(self) -> None:
        self.tasks["FAMILY"] = dataclasses.replace(
            self.family_task, enabled=True, running=True
        )

        codes = self.blocker_codes(self.verify())

        self.assertIn("legacy_family_task_enabled", codes)
        self.assertIn("legacy_family_task_running", codes)

    def test_v4_external_process_snapshot_fails_closed(self) -> None:
        snapshot = SimpleNamespace(
            next_action="wait_external_process",
            branch="external_live_chain",
        )

        codes = self.blocker_codes(
            self.verify(pipeline_probe=lambda _contract: snapshot)
        )

        self.assertIn("v4_external_process_active", codes)

    def test_live_v3_pid_and_command_mismatch_fail_closed(self) -> None:
        (self.root / "v3.pid").write_text("731\n", encoding="ascii")
        self.process = verifier.ProcessSnapshot(
            pid=731,
            exists=True,
            command_line="python unrelated_worker.py",
        )

        codes = self.blocker_codes(self.verify())

        self.assertIn("v3_pid_present", codes)
        self.assertIn("v3_pid_process_running", codes)
        self.assertIn("v3_pid_command_mismatch", codes)

    def test_stale_v3_pid_file_still_fails_closed(self) -> None:
        (self.root / "v3.pid").write_text("731\n", encoding="ascii")

        codes = self.blocker_codes(self.verify())

        self.assertIn("v3_pid_present", codes)
        self.assertNotIn("v3_pid_process_running", codes)

    def test_invalid_v3_pid_file_fails_closed_without_process_probe(self) -> None:
        (self.root / "v3.pid").write_text("not-a-pid\n", encoding="ascii")

        codes = self.blocker_codes(self.verify())

        self.assertIn("v3_pid_invalid", codes)

    def test_oversized_v3_pid_file_fails_closed(self) -> None:
        (self.root / "v3.pid").write_text("7" * 33, encoding="ascii")

        self.assertIn("v3_pid_invalid", self.blocker_codes(self.verify()))

    def test_wrong_pid_process_probe_fails_closed(self) -> None:
        (self.root / "v3.pid").write_text("731\n", encoding="ascii")
        report = verifier.verify_cutover(
            self.policy(),
            task_probe=lambda name: self.tasks[name],
            process_probe=lambda _pid: verifier.ProcessSnapshot(pid=999, exists=False),
            lock_probe=lambda _path: self.lock,
        )

        self.assertIn("v3_process_probe_invalid", self.blocker_codes(report))

    def test_lock_mismatch_unsafe_or_held_each_fail_closed(self) -> None:
        scenarios = (
            (
                dataclasses.replace(self.lock, path=self.root / "foreign.lock"),
                "shared_lock_probe_path_mismatch",
            ),
            (
                dataclasses.replace(self.lock, safe_regular_file=False),
                "shared_lock_unsafe",
            ),
            (dataclasses.replace(self.lock, held=True), "shared_lock_held"),
            (
                dataclasses.replace(self.lock, held=None),
                "shared_lock_state_unproven",
            ),
            (dataclasses.replace(self.lock, exists=False), "shared_lock_missing"),
        )
        for snapshot, expected_code in scenarios:
            with self.subTest(expected_code=expected_code):
                self.lock = snapshot
                self.assertIn(expected_code, self.blocker_codes(self.verify()))

    def test_default_lock_probe_opens_read_only_and_never_locks(self) -> None:
        path = self.root / "observational.lock"
        path.write_bytes(b"\0")
        before = path.read_bytes()
        real_open = verifier.os.open

        with mock.patch.object(verifier.os, "open", wraps=real_open) as opening:
            if os.name == "nt":
                import msvcrt

                with mock.patch.object(
                    msvcrt,
                    "locking",
                    side_effect=AssertionError("lock mutation is forbidden"),
                ):
                    snapshot = verifier.nonmutating_lock_probe(path)
            else:
                snapshot = verifier.nonmutating_lock_probe(path)

        self.assertTrue(snapshot.safe_regular_file)
        self.assertEqual(path.read_bytes(), before)
        flags = opening.call_args.args[1]
        self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR), 0)

    def test_duplicate_task_names_are_invalid(self) -> None:
        policy = dataclasses.replace(self.policy(), family_task_name="v4")

        with self.assertRaises(verifier.CutoverVerificationError):
            self.verify(policy)

    def test_invalid_probe_type_is_rejected(self) -> None:
        with self.assertRaises(verifier.CutoverVerificationError):
            verifier.verify_cutover(
                self.policy(),
                task_probe=lambda _name: None,  # type: ignore[return-value]
                process_probe=lambda _pid: self.process,
                lock_probe=lambda _path: self.lock,
            )

    def test_cli_has_no_mutating_flags_and_invalid_input_is_canonical(self) -> None:
        options = {
            option
            for action in verifier.build_parser()._actions
            for option in action.option_strings
        }
        self.assertTrue(
            {"--execute", "--write-contract", "--enable", "--start"}.isdisjoint(
                options
            )
        )
        argv = self.cli_argv()
        argv[argv.index("--expected-v3-definition-sha256") + 1] = "invalid"
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = verifier.main(
                argv,
                task_probe=lambda name: self.tasks[name],
                process_probe=lambda _pid: self.process,
                lock_probe=lambda _path: self.lock,
            )

        self.assertEqual(code, 2)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["writes_performed"], 0)
        self.assertEqual(stdout.getvalue().encode("utf-8"), verifier.canonical_json_bytes(report))

    def test_cli_not_ready_uses_nonzero_exit_and_zero_writes(self) -> None:
        self.tasks["V3"] = dataclasses.replace(self.v3_task, enabled=True)
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = verifier.main(
                self.cli_argv(),
                task_probe=lambda name: self.tasks[name],
                process_probe=lambda _pid: self.process,
                lock_probe=lambda _path: self.lock,
            )

        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(report["writes_performed"], 0)

    def test_cli_rejects_mutating_flag_with_bounded_canonical_json(self) -> None:
        stdout = io.StringIO()
        forbidden_probe = mock.Mock(side_effect=AssertionError("probe must not run"))

        with contextlib.redirect_stdout(stdout):
            code = verifier.main(
                ["--execute"],
                task_probe=forbidden_probe,
                process_probe=forbidden_probe,
                lock_probe=forbidden_probe,
            )

        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["writes_performed"], 0)
        self.assertLessEqual(len(stdout.getvalue().encode("utf-8")), verifier.MAX_REPORT_BYTES)
        self.assertEqual(stdout.getvalue().encode("utf-8"), verifier.canonical_json_bytes(report))
        forbidden_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
