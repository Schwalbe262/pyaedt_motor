from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import run_ipmsm_v4_pipeline_supervisor as launcher


class FakeSnapshot:
    def __init__(self, report: dict[str, object], *, exit_code: int = 0) -> None:
        self._report = report
        self.exit_code = exit_code
        self.report_calls: list[tuple[object, str]] = []

    def report(self, contract: object, *, mode: str) -> dict[str, object]:
        self.report_calls.append((contract, mode))
        return dict(self._report)


def canonical(document: dict[str, object]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


class InactiveV4LauncherTests(unittest.TestCase):
    def test_existing_contract_uses_only_load_audit_and_inspection(self) -> None:
        contract_path = Path("pipeline-v4.json")
        contract = object()
        snapshot = FakeSnapshot(
            {
                "branch": "stage1",
                "contract": str(contract_path),
                "mode": "dry-run",
                "next_action": "publish_stage1_official",
                "schema_version": "ipmsm-v4-pipeline-report-v1",
                "writes_performed": 0,
            },
            exit_code=7,
        )
        api = SimpleNamespace(
            load_contract=mock.Mock(return_value=contract),
            audit_contract=mock.Mock(),
            inspect_pipeline=mock.Mock(return_value=snapshot),
            execute_action=mock.Mock(),
            publish_contract=mock.Mock(),
            publish_contract_with_outcome=mock.Mock(),
            main=mock.Mock(),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = launcher.main(
            ["--contract", str(contract_path)],
            api=api,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(code, 7)
        self.assertEqual(stderr.getvalue(), "")
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["operation"], "inspect_existing_contract")
        self.assertTrue(report["read_only"])
        self.assertFalse(report["execution_allowed"])
        self.assertEqual(report["writes_performed"], 0)
        self.assertEqual(stdout.getvalue(), canonical(report))
        api.load_contract.assert_called_once_with(contract_path)
        api.audit_contract.assert_called_once_with(contract)
        api.inspect_pipeline.assert_called_once_with(contract)
        self.assertEqual(snapshot.report_calls, [(contract, "dry-run")])
        api.execute_action.assert_not_called()
        api.publish_contract.assert_not_called()
        api.publish_contract_with_outcome.assert_not_called()
        api.main.assert_not_called()

    def test_builder_validates_in_memory_and_inspects_without_publication(self) -> None:
        document = {
            "contract_sha256": "a" * 64,
            "pipeline": {},
            "schema_version": "ipmsm-v2-foundation-pipeline-contract-v4",
        }
        payload = b"canonical intended contract\n"
        output = Path("intended-v4.json")
        inspection = SimpleNamespace(
            destination=output,
            expected_payload=payload,
            pending_state="",
            status="absent",
        )
        api = SimpleNamespace(
            build_contract_document=mock.Mock(return_value=document),
            _inspect_contract_publication_state=mock.Mock(return_value=inspection),
            execute_action=mock.Mock(),
            publish_contract=mock.Mock(),
            publish_contract_with_outcome=mock.Mock(),
            main=mock.Mock(),
        )
        argv = [
            "--build-base-contract",
            "base-v3.json",
            "--output-contract",
            str(output),
            "--stage1-workspace",
            "official-stage1",
            "--declaration",
            "declaration.json",
            "--confirmation",
            "confirmation.json",
            "--receipt",
            "receipt.json",
            "--optimization-runner",
            "continue-v4.py",
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = launcher.main(argv, api=api, stdout=stdout, stderr=stderr)

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "validated")
        self.assertEqual(report["publication_state"], "absent")
        self.assertEqual(report["operation"], "inspect_intended_contract")
        self.assertEqual(report["transaction_mutations"], 0)
        self.assertEqual(report["writes_performed"], 0)
        self.assertTrue(report["read_only"])
        self.assertFalse(report["execution_allowed"])
        self.assertEqual(stdout.getvalue(), canonical(report))
        api.build_contract_document.assert_called_once_with(
            base_contract_path=Path("base-v3.json"),
            output_path=output,
            stage1_workspace=Path("official-stage1"),
            declaration=Path("declaration.json"),
            confirmation=Path("confirmation.json"),
            receipt=Path("receipt.json"),
            optimization_runner=Path("continue-v4.py"),
        )
        api._inspect_contract_publication_state.assert_called_once_with(output, document)
        api.execute_action.assert_not_called()
        api.publish_contract.assert_not_called()
        api.publish_contract_with_outcome.assert_not_called()
        api.main.assert_not_called()

    def test_builder_reports_pending_recovery_without_recovering_it(self) -> None:
        document = {
            "contract_sha256": "b" * 64,
            "schema_version": "contract-v4",
        }
        inspection = SimpleNamespace(
            destination=Path("intended-v4.json"),
            expected_payload=b"fixed\n",
            pending_state="pre_commit_no_proof",
            status="pending",
        )
        api = SimpleNamespace(
            build_contract_document=mock.Mock(return_value=document),
            _inspect_contract_publication_state=mock.Mock(return_value=inspection),
        )
        stdout = io.StringIO()

        code = launcher.main(
            [
                "--build-base-contract",
                "base.json",
                "--output-contract",
                "intended-v4.json",
                "--stage1-workspace",
                "official",
                "--declaration",
                "declaration.json",
                "--confirmation",
                "confirmation.json",
                "--receipt",
                "receipt.json",
            ],
            api=api,
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "publication_recovery_pending")
        self.assertEqual(report["publication_state"], "pre_commit_no_proof")
        self.assertEqual(report["writes_performed"], 0)

    def test_mutating_and_operational_flags_are_not_in_the_cli(self) -> None:
        option_strings = {
            option
            for action in launcher.build_parser()._actions
            for option in action.option_strings
        }
        self.assertTrue(
            {
                "--execute",
                "--write-contract",
                "--pid-file",
                "--stdout-log",
                "--stderr-log",
            }.isdisjoint(option_strings)
        )

        api = SimpleNamespace(
            load_contract=mock.Mock(),
            audit_contract=mock.Mock(),
            inspect_pipeline=mock.Mock(),
        )
        for forbidden in ("--execute", "--write-contract", "--pid-file"):
            parse_errors = io.StringIO()
            with self.subTest(flag=forbidden), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    launcher.main(
                        ["--contract", "pipeline-v4.json", forbidden],
                        api=api,
                        stdout=io.StringIO(),
                        stderr=parse_errors,
                    )
                self.assertEqual(raised.exception.code, 2)
                report = json.loads(parse_errors.getvalue())
                self.assertEqual(report["status"], "usage_error")
                self.assertEqual(report["writes_performed"], 0)
                self.assertEqual(parse_errors.getvalue(), canonical(report))
                self.assertLessEqual(
                    len(parse_errors.getvalue().encode("utf-8")),
                    launcher.MAX_REPORT_BYTES,
                )
        api.load_contract.assert_not_called()
        api.audit_contract.assert_not_called()
        api.inspect_pipeline.assert_not_called()

    def test_mode_specific_arguments_fail_before_any_supervisor_call(self) -> None:
        api = SimpleNamespace(load_contract=mock.Mock())
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                launcher.main(
                    [
                        "--contract",
                        "pipeline-v4.json",
                        "--output-contract",
                        "other.json",
                    ],
                    api=api,
                )
        self.assertEqual(raised.exception.code, 2)
        api.load_contract.assert_not_called()

    def test_oversized_inspection_is_rejected_with_bounded_canonical_error(self) -> None:
        contract = object()
        snapshot = FakeSnapshot(
            {
                "detail": "x" * launcher.MAX_REPORT_BYTES,
                "mode": "dry-run",
                "writes_performed": 0,
            }
        )
        api = SimpleNamespace(
            load_contract=mock.Mock(return_value=contract),
            audit_contract=mock.Mock(),
            inspect_pipeline=mock.Mock(return_value=snapshot),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = launcher.main(
            ["--contract", "pipeline-v4.json"],
            api=api,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        report = json.loads(stderr.getvalue())
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["writes_performed"], 0)
        self.assertLessEqual(len(stderr.getvalue().encode("utf-8")), launcher.MAX_REPORT_BYTES)
        self.assertEqual(stderr.getvalue(), canonical(report))


if __name__ == "__main__":
    unittest.main()
