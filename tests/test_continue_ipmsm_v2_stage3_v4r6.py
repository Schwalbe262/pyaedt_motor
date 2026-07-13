from __future__ import annotations

import hashlib
import json
import os
import dataclasses
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

import continue_ipmsm_v2_stage3_v4r6 as runner


class Stage3ActivationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.original_runtime_root = runner.contract_builder.EXPECTED_RUNTIME_ROOT
        runner.contract_builder.EXPECTED_RUNTIME_ROOT = self.root
        self.original_authority_file = runner.authority.__file__
        self.plan_bytes = b"header\nrow\n"
        self.dry_manifest = {
            "schema_version": "ipmsm_v2_stage3_fallback_plan_v2",
            "mode": "dry-run",
            "case_plan": str(self.root / "stage3.csv"),
            "case_plan_sha256": hashlib.sha256(self.plan_bytes).hexdigest(),
            "summary": {"rows": 300},
        }
        self.write_manifest = dict(self.dry_manifest, mode="write")
        self.manifest_bytes = runner.contract_builder._manifest_bytes(self.write_manifest)
        contract_bytes = b"contract\n"
        self.contract_path = self.root / "contract.json"
        self.contract_path.write_bytes(contract_bytes)
        self.generator_source = self.root / "generator.py"
        self.generator_source.write_bytes(b"# sealed generator\n")
        self.authority_source = self.root / runner.contract_builder.AUTHORITY_FILENAME
        self.authority_source.write_bytes(Path(self.original_authority_file).read_bytes())
        runner.authority.__file__ = str(self.authority_source)
        builder_source = Path(runner.contract_builder.__file__).resolve()
        runner_source = Path(runner.__file__).resolve()

        def source_record(path: Path) -> dict[str, str]:
            return {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

        self.outputs = {
            "plan": self.root / "stage3.csv",
            "manifest": self.root / "stage3.manifest.json",
            "decision": self.root / "stage3.decision.json",
            "plan_completion": self.root / "plan_completion.json",
            "claim": self.root / "runner.claim.json",
            "recovery": self.root / "runner.claim.recovery.json",
            "stdout_log": self.root / "runner.stdout.log",
            "stderr_log": self.root / "runner.stderr.log",
            "log_receipt": self.root / "runner.logs.receipt.json",
        }
        self.context = runner.ActivationContext(
            path=self.contract_path,
            snapshot=SimpleNamespace(
                path=self.contract_path,
                payload=contract_bytes,
                sha256=hashlib.sha256(contract_bytes).hexdigest(),
            ),
            contract_sha256="c" * 64,
            document={},
            root=self.root,
            parent_contract=self.root / "parent.json",
            pipeline=SimpleNamespace(),
            sources={
                "generator": source_record(self.generator_source),
                "builder": source_record(builder_source),
                "runner": source_record(runner_source),
                "authority": source_record(self.authority_source),
                "runner_executable": source_record(Path(sys.executable)),
            },
            dry_argv=("python", "-B", "generator.py"),
            write_argv=("python", "-B", "generator.py", "--write-stage3"),
            continuation_argv=("python", "continue_ipmsm_v2_stage2.py"),
            runner_dry_argv=(
                sys.executable,
                "-B",
                str(Path(runner.__file__).resolve()),
                "--activation-contract",
                str(self.contract_path),
            ),
            runner_execute_argv=(
                sys.executable,
                "-B",
                str(Path(runner.__file__).resolve()),
                "--activation-contract",
                str(self.contract_path),
                "--execute",
            ),
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            scheduler={"project": "PYAEDT_MOTOR_IPMSM_V2", "project_active_cap": "50"},
            outputs=self.outputs,
            expected={
                "dry_manifest": self.dry_manifest,
                "write_manifest": self.write_manifest,
                "plan_sha256": hashlib.sha256(self.plan_bytes).hexdigest(),
                "manifest_sha256": hashlib.sha256(self.manifest_bytes).hexdigest(),
            },
            shared_lock=self.root / "pipeline.lock",
            authority_snapshots=(
                runner.authority.read_single_link_snapshot(
                    self.generator_source, "test generator"
                ),
                runner.authority.read_single_link_snapshot(builder_source, "test builder"),
                runner.authority.read_single_link_snapshot(runner_source, "test runner"),
                runner.authority.read_single_link_snapshot(
                    self.authority_source, "test authority helper"
                ),
                runner.authority.read_single_link_snapshot(
                    Path(sys.executable),
                    "test executable",
                    require_single_link=False,
                ),
            ),
        )

    def tearDown(self) -> None:
        runner.contract_builder.EXPECTED_RUNTIME_ROOT = self.original_runtime_root
        runner.authority.__file__ = self.original_authority_file
        self.temp.cleanup()

    @staticmethod
    def _completed(value: dict[str, object], returncode: int = 0) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=returncode,
            stdout=json.dumps(value, sort_keys=True) + "\n",
            stderr="",
        )

    def test_execute_generates_exact_pair_completion_then_only_stage3(self) -> None:
        calls: list[list[str]] = []

        def process(argv: list[str], **_: object) -> SimpleNamespace:
            calls.append(list(argv))
            if argv == list(self.context.dry_argv):
                return self._completed(self.dry_manifest)
            if argv == list(self.context.write_argv):
                self.outputs["plan"].write_bytes(self.plan_bytes)
                self.outputs["manifest"].write_bytes(self.manifest_bytes)
                return self._completed(self.write_manifest)
            if argv == list(self.context.continuation_argv):
                return self._completed({"status": "planned"})
            if argv == [*self.context.continuation_argv, "--execute"]:
                return self._completed({"status": "stage2_started"})
            raise AssertionError(argv)

        with mock.patch.object(runner, "_audit_process_authority"), mock.patch.object(
            runner, "load_activation_context", return_value=self.context
        ), mock.patch.object(runner, "_decision_status", side_effect=[None, "stage2_started"]):
            result = runner.execute(self.context, runner=process)
        self.assertEqual(result["status"], "stage3_running")
        self.assertTrue(self.outputs["plan_completion"].is_file())
        self.assertFalse(self.outputs["claim"].exists())
        self.assertEqual(
            calls,
            [
                list(self.context.dry_argv),
                list(self.context.write_argv),
                list(self.context.continuation_argv),
                [*self.context.continuation_argv, "--execute"],
            ],
        )
        flattened = " ".join(item for call in calls for item in call).lower()
        self.assertNotIn("scheduledtask", flattened)
        self.assertNotIn("schtasks", flattened)
        self.assertNotIn("optimization", flattened)

    def test_dry_write_divergence_fails_before_completion(self) -> None:
        changed = dict(self.write_manifest, selection={"changed": True})

        def process(argv: list[str], **_: object) -> SimpleNamespace:
            if argv == list(self.context.dry_argv):
                return self._completed(self.dry_manifest)
            if argv == list(self.context.write_argv):
                return self._completed(changed)
            raise AssertionError(argv)

        with mock.patch.object(runner, "_audit_process_authority"), mock.patch.object(
            runner, "load_activation_context", return_value=self.context
        ):
            with self.assertRaisesRegex(runner.Stage3ActivationError, "write differs"):
                runner.execute(self.context, runner=process)
        self.assertFalse(self.outputs["plan_completion"].exists())
        self.assertFalse(self.outputs["claim"].exists())

    def test_stale_claim_is_adopted_and_original_owner_preserved(self) -> None:
        original = {
            "hostname": runner.socket.gethostname(),
            "pid": 999991,
            "invocation_id": "old",
            "mode": "execute",
            "started_at_utc": "2026-01-01T00:00:00+00:00",
        }
        runner._publish_exact(
            self.outputs["claim"],
            runner._claim_value(self.context, original, original),
            "activation claim",
        )
        new = dict(original, pid=999992, invocation_id="new")
        with mock.patch.object(runner, "_pid_is_running", return_value=False):
            runner._acquire_claim(self.context, new)
        claim = runner._read_claim(self.outputs["claim"], self.context, "adopted claim")
        self.assertEqual(claim["owner"], new)
        self.assertEqual(claim["original_owner"], original)
        self.assertFalse(self.outputs["recovery"].exists())

    def test_stage3_resume_adds_resume_to_dry_and_execute_only(self) -> None:
        calls: list[list[str]] = []
        self.outputs["plan"].write_bytes(self.plan_bytes)
        self.outputs["manifest"].write_bytes(self.manifest_bytes)

        def process(argv: list[str], **_: object) -> SimpleNamespace:
            calls.append(list(argv))
            status = "stage2_started"
            return self._completed({"status": status}, returncode=0)

        with mock.patch.object(runner, "load_activation_context", return_value=self.context):
            runner._run_continuation(
                self.context,
                "stage2_started",
                runner=process,
                log_token="a" * 32,
            )
        self.assertEqual(
            calls,
            [
                [*self.context.continuation_argv, "--resume"],
                [*self.context.continuation_argv, "--resume", "--execute"],
            ],
        )

    def test_plan_completion_is_no_replace_and_exact(self) -> None:
        self.outputs["plan"].write_bytes(self.plan_bytes)
        self.outputs["manifest"].write_bytes(self.manifest_bytes)
        artifacts = runner._audit_plan_pair(self.context)
        with mock.patch.object(runner, "load_activation_context", return_value=self.context):
            self.assertTrue(
                runner._audit_or_publish_plan_completion(self.context, artifacts, publish=True)
            )
        self.assertFalse(
            runner._audit_or_publish_plan_completion(self.context, artifacts, publish=True)
        )
        self.outputs["plan_completion"].write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(runner.Stage3ActivationError, "differs"):
            runner._audit_or_publish_plan_completion(self.context, artifacts, publish=False)

    def test_plan_completion_final_replay_failure_rolls_back(self) -> None:
        self.outputs["plan"].write_bytes(self.plan_bytes)
        self.outputs["manifest"].write_bytes(self.manifest_bytes)
        artifacts = runner._audit_plan_pair(self.context)
        with mock.patch.object(
            runner, "load_activation_context", return_value=self.context
        ), mock.patch.object(
            runner,
            "_audit_plan_pair",
            side_effect=[artifacts, runner.Stage3ActivationError("third replay changed")],
        ):
            with self.assertRaisesRegex(runner.Stage3ActivationError, "third replay"):
                runner._audit_or_publish_plan_completion(
                    self.context, artifacts, publish=True
                )
        self.assertFalse(self.outputs["plan_completion"].exists())

    def test_dry_run_never_writes_or_calls_continuation(self) -> None:
        calls: list[list[str]] = []

        def process(argv: list[str], **_: object) -> SimpleNamespace:
            calls.append(list(argv))
            return self._completed(self.dry_manifest)

        with mock.patch.object(runner, "_audit_process_authority"), mock.patch.object(
            runner, "load_activation_context", return_value=self.context
        ):
            result = runner.dry_run(self.context, runner=process)
        self.assertEqual(result["action"], "generate_stage3_plan")
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(calls, [list(self.context.dry_argv)])
        self.assertFalse(any(path.exists() for path in self.outputs.values()))

    def test_cli_is_contract_only(self) -> None:
        parsed = runner.build_parser().parse_args(
            ["--activation-contract", str(self.contract_path), "--execute"]
        )
        self.assertEqual(parsed.activation_contract, self.contract_path)
        with self.assertRaises(SystemExit):
            runner.build_parser().parse_args(
                [
                    "--activation-contract",
                    str(self.contract_path),
                    "--project-active-cap",
                    "100",
                ]
            )

    def test_source_contains_no_old_task_activation_api(self) -> None:
        source = Path(runner.__file__).read_text(encoding="utf-8").casefold()
        self.assertNotIn("start-scheduledtask", source)
        self.assertNotIn("enable-scheduledtask", source)
        self.assertNotIn("schtasks", source)

    def test_live_argv_rejects_parseable_reordering_or_extra_flags(self) -> None:
        reordered = [
            sys.executable,
            "-B",
            str(Path(runner.__file__).resolve()),
            "--execute",
            "--activation-contract",
            str(self.contract_path),
        ]
        with mock.patch.object(runner.sys, "orig_argv", reordered):
            with self.assertRaisesRegex(runner.Stage3ActivationError, "live runner argv"):
                runner._audit_process_authority(self.context, execute=True)
        with mock.patch.object(runner.sys, "orig_argv", list(self.context.runner_execute_argv)):
            runner._audit_process_authority(self.context, execute=True)

    def test_build_config_cannot_substitute_generator_binding(self) -> None:
        parent = self.root / "simul_log_smoke/v4r5_native/contract.json"
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_bytes(b"{}\n")
        configured_generator = self.root / runner.contract_builder.GENERATOR_FILENAME
        configured_generator.write_bytes(b"# configured generator\n")
        configured_runner = self.root / runner.contract_builder.RUNNER_FILENAME
        configured_runner.write_bytes(Path(runner.__file__).read_bytes())
        configured_authority = self.root / runner.contract_builder.AUTHORITY_FILENAME
        if not configured_authority.exists():
            configured_authority.write_bytes(Path(self.original_authority_file).read_bytes())
        output_contract = (
            self.root
            / runner.contract_builder.ACTIVATION_RELATIVE_ROOT
            / runner.contract_builder.CONTRACT_FILENAME
        )
        output_contract.parent.mkdir(parents=True, exist_ok=True)
        config_path = self.root / "build.json"
        def record(path: Path) -> dict[str, str]:
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        config = {
            "schema_version": runner.contract_builder.BUILD_CONFIG_SCHEMA_VERSION,
            "root": str(self.root),
            "parent_contract": str(parent),
            "generator_source": record(configured_generator),
            "builder_source": dict(self.context.sources["builder"]),
            "runner_source": record(configured_runner),
            "authority_source": record(configured_authority),
            "output_contract": str(output_contract),
        }
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8", newline="\n")
        activation = {
            "root": str(self.root),
            "build_config": {
                "path": str(config_path),
                "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            },
            "parent": {"wrapper": {"path": str(parent)}},
            "sources": self.context.sources,
        }
        with self.assertRaisesRegex(runner.Stage3ActivationError, "generator_source"):
            runner._load_build_config_binding(activation, output_contract)

    def test_source_change_during_child_is_rejected(self) -> None:
        def process(argv: list[str], **_: object) -> SimpleNamespace:
            self.generator_source.write_bytes(b"# changed during child\n")
            return self._completed(self.dry_manifest)

        with mock.patch.object(runner, "load_activation_context", return_value=self.context):
            with self.assertRaisesRegex(runner.Stage3ActivationError, "freeze failed|Permission denied"):
                runner._run_generator_dry(self.context, runner=process)

    def test_non_source_parent_artifact_is_frozen_across_child(self) -> None:
        artifact = self.root / "combined-model.pkl"
        artifact.write_bytes(b"model\n")
        artifact_snapshot = runner.authority.read_single_link_snapshot(
            artifact, "test parent artifact"
        )
        context = dataclasses.replace(
            self.context,
            authority_snapshots=(*self.context.authority_snapshots, artifact_snapshot),
        )

        def process(argv: list[str], **_: object) -> SimpleNamespace:
            artifact.write_bytes(b"mutated\n")
            return self._completed(self.dry_manifest)

        with mock.patch.object(runner, "load_activation_context", return_value=context):
            with self.assertRaisesRegex(runner.Stage3ActivationError, "freeze failed"):
                runner._run_generator_dry(context, runner=process)

    def test_partial_member_mismatch_or_missing_proof_is_rejected_before_writer(self) -> None:
        self.outputs["plan"].write_bytes(b"foreign\n")
        with self.assertRaisesRegex(runner.Stage3ActivationError, "differs from sealed"):
            runner._plan_state(self.context)
        self.outputs["plan"].write_bytes(self.plan_bytes)
        with self.assertRaisesRegex(runner.Stage3ActivationError, "no ownership proof"):
            runner._plan_state(self.context)

    def test_plan_change_during_continuation_dry_is_rejected(self) -> None:
        self.outputs["plan"].write_bytes(self.plan_bytes)
        self.outputs["manifest"].write_bytes(self.manifest_bytes)

        def process(argv: list[str], **_: object) -> SimpleNamespace:
            self.outputs["plan"].write_bytes(b"changed\n")
            return self._completed({"status": "planned"})

        with self.assertRaisesRegex(runner.Stage3ActivationError, "freeze failed|Permission denied"):
            runner._run_continuation(
                self.context,
                None,
                runner=process,
                log_token="b" * 32,
            )

    def test_precreated_log_without_receipt_is_rejected(self) -> None:
        token = "c" * 32
        paths = runner._invocation_log_paths(self.context, token)
        paths["stdout_log"].write_text("foreign\n", encoding="utf-8")
        with self.assertRaisesRegex(runner.Stage3ActivationError, "precreated"):
            with runner._open_log_streams(self.context, token):
                pass

    def test_log_authority_is_exclusive_single_link_and_reopenable(self) -> None:
        first = "d" * 32
        second = "e" * 32
        first_paths = runner._invocation_log_paths(self.context, first)
        second_paths = runner._invocation_log_paths(self.context, second)
        with runner._open_log_streams(self.context, first) as (stdout, stderr):
            stdout.write("one\n")
            stderr.write("two\n")
        self.assertTrue(first_paths["log_receipt"].is_file())
        for name in ("stdout_log", "stderr_log", "log_receipt"):
            info = first_paths[name].stat()
            self.assertEqual(info.st_nlink, 1)
        with runner._open_log_streams(self.context, second) as (stdout, _):
            stdout.write("three\n")
        self.assertEqual(
            first_paths["stdout_log"].read_text(encoding="utf-8"),
            "one\n",
        )
        self.assertEqual(second_paths["stdout_log"].read_text(encoding="utf-8"), "three\n")

    def test_hardlinked_log_alias_to_plan_is_rejected(self) -> None:
        token = "f" * 32
        paths = runner._invocation_log_paths(self.context, token)
        self.outputs["plan"].write_bytes(self.plan_bytes)
        os.link(self.outputs["plan"], paths["stdout_log"])
        paths["stderr_log"].write_bytes(b"")
        identities = {}
        for name in ("stdout_log", "stderr_log"):
            info = paths[name].stat()
            identities[name] = {
                "device": int(info.st_dev),
                "inode": int(info.st_ino),
                "file_type": runner.stat.S_IFMT(info.st_mode),
            }
        runner._publish_exact(
            paths["log_receipt"],
            runner._log_receipt_value(self.context, identities, paths, token),
            "test log receipt",
        )
        with self.assertRaisesRegex(runner.Stage3ActivationError, "single-link|aliases"):
            runner._audit_log_receipt(self.context, paths, token)


if __name__ == "__main__":
    unittest.main()
