from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

import build_ipmsm_v2_stage3_acquisition_v4r7 as builder
import continue_ipmsm_v2_stage3_acquisition_v4r7 as runner


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(builder.authority.canonical_json_bytes(value))


def record(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class Stage3AcquisitionV4r7Tests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.old_builder_root = builder.EXPECTED_RUNTIME_ROOT
        self.old_runner_root = runner.contract_builder.EXPECTED_RUNTIME_ROOT
        builder.EXPECTED_RUNTIME_ROOT = self.root
        runner.contract_builder.EXPECTED_RUNTIME_ROOT = self.root
        self.addCleanup(setattr, builder, "EXPECTED_RUNTIME_ROOT", self.old_builder_root)
        self.addCleanup(
            setattr,
            runner.contract_builder,
            "EXPECTED_RUNTIME_ROOT",
            self.old_runner_root,
        )

        self.acquisition_root = self.root / builder.RELATIVE_ROOT
        self.source_root = self.root / builder.SOURCE_RELATIVE_ROOT
        self.source_root.mkdir(parents=True)
        repository_root = Path(__file__).parents[1]
        for filename in (
            builder.CAMPAIGN_FILENAME,
            builder.SUBMIT_FILENAME,
            builder.COLLECTOR_FILENAME,
        ):
            payload = (repository_root / filename).read_bytes().replace(b"\r\n", b"\n")
            (self.source_root / filename).write_bytes(payload)

        self.prior_contract = self.root / builder.EXPECTED_PRIOR_CONTRACT
        self.prior_contract.parent.mkdir(parents=True, exist_ok=True)
        self.prior_contract.write_bytes(b"prior contract\n")
        self.plan = self.root / "simul_log_smoke/v4r4/stage3.csv"
        self.manifest = self.root / "simul_log_smoke/v4r4/stage3.manifest.json"
        self.plan_completion = self.root / "simul_log_smoke/v4r6_stage3_activation/plan_completion.json"
        self.decision_path = self.root / "simul_log_smoke/v4r4/stage3.decision.json"
        for path, payload in (
            (self.plan, b"case_id\ncase-001\n"),
            (self.manifest, b"{}\n"),
            (self.plan_completion, b"completion\n"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.output_dir = self.root / "collected/stage3"
        self.output_dir.parent.mkdir(parents=True)
        self.base_argv = [
            "--cases",
            str(self.plan),
            "--project",
            "PYAEDT_MOTOR_IPMSM_V2",
            "--project-active-cap",
            "50",
            "--task-prefix",
            "ipmsm-v2-foundation-s3-v4r4",
            "--output-dir",
            str(self.output_dir),
            "--merged-output",
            "merged_results.csv",
            "--terminal-retry-limit",
            "1",
            "--submit",
        ]
        execution_contract = {"stage2": {"runner_argv": list(self.base_argv)}}
        self.decision = {
            "schema_version": builder.stage2_continuation.SCHEMA_VERSION,
            "decision": "run_stage2",
            "status": "stage2_started",
            "mode": "execute",
            "contract_sha256": builder.stage2_continuation._contract_sha256(execution_contract),
            "execution_contract": execution_contract,
            "stage2": {
                "output_dir": str(self.output_dir),
                "runner_argv": list(self.base_argv),
            },
        }
        write_json(self.decision_path, self.decision)

        actual_sources = {
            "builder": Path(builder.prior_builder.__file__).resolve(),
            "runner": Path(builder.prior_runner.__file__).resolve(),
            "authority": Path(builder.authority.__file__).resolve(),
            "runner_executable": Path(sys.executable).resolve(),
        }
        self.prior_sources = {name: record(path) for name, path in actual_sources.items()}
        parent_sources = {
            "optimization_source_collect_ipmsm_v2_campaign": record(
                Path(runner.sealed_collector.__file__).resolve()
            ),
            "optimization_source_continue_ipmsm_v2_stage2": record(
                Path(builder.stage2_continuation.__file__).resolve()
            ),
            "optimization_source_merge_ipmsm_v2_results": record(
                Path(runner.sealed_merger.__file__).resolve()
            ),
            "optimization_source_run_ipmsm_v2_campaign": record(
                Path(runner.sealed_campaign.__file__).resolve()
            ),
            "optimization_source_submit_ipmsm_v2_campaign": record(
                Path(runner.sealed_submit.__file__).resolve()
            ),
            "supervisor_v3": record(Path(runner.v3.__file__).resolve()),
        }
        self.prior_snapshot = builder.authority.read_single_link_snapshot(
            self.prior_contract, "test prior activation"
        )
        self.context = SimpleNamespace(
            snapshot=self.prior_snapshot,
            contract_sha256="c" * 64,
            document={"activation": {"parent": {"source_pins": parent_sources}}},
            outputs={
                "plan": self.plan,
                "manifest": self.manifest,
                "plan_completion": self.plan_completion,
                "decision": self.decision_path,
            },
            expected={"dry_manifest": {"summary": {"rows": 300}}},
            sources=self.prior_sources,
            scheduler={
                "project": "PYAEDT_MOTOR_IPMSM_V2",
                "scheduler_url": "http://127.0.0.1:8000",
                "task_prefix": "ipmsm-v2-foundation-s3-v4r4",
                "project_active_cap": "50",
            },
            shared_lock=self.root / "simul_log_smoke/v4r4/pipeline.lock",
            root=self.root,
            authority_snapshots=(),
        )
        self.prior_binding = {
            "activation_contract": {
                "path": str(self.prior_contract),
                "raw_sha256": self.prior_snapshot.sha256,
                "contract_sha256": self.context.contract_sha256,
            },
            "plan_completion": record(self.plan_completion),
            "plan": record(self.plan),
            "manifest": record(self.manifest),
            "decision": {
                **record(self.decision_path),
                "status": "stage2_started",
                "contract_sha256": self.decision["contract_sha256"],
                "execution_contract_sha256": self.decision["contract_sha256"],
            },
            "shared_lock": str(self.context.shared_lock),
        }
        self.prior_audit = {"binding": self.prior_binding, "decision": self.decision}
        self.prior_audit_result = (
            self.context,
            self.prior_audit,
            (
                self.prior_snapshot,
                builder.authority.read_single_link_snapshot(
                    self.plan_completion, "test completion"
                ),
                builder.authority.read_single_link_snapshot(self.plan, "test plan"),
                builder.authority.read_single_link_snapshot(self.manifest, "test manifest"),
                builder.authority.read_single_link_snapshot(self.decision_path, "test decision"),
            ),
        )

        self.config_path = self.acquisition_root / builder.BUILD_CONFIG_FILENAME
        self.contract_path = self.acquisition_root / builder.CONTRACT_FILENAME
        self.config = {
            "schema_version": builder.BUILD_CONFIG_SCHEMA_VERSION,
            "root": str(self.root),
            "prior_activation_contract": record(self.prior_contract),
            "campaign_source": record(self.source_root / builder.CAMPAIGN_FILENAME),
            "submit_source": record(self.source_root / builder.SUBMIT_FILENAME),
            "collector_source": record(self.source_root / builder.COLLECTOR_FILENAME),
            "builder_source": record(Path(builder.__file__).resolve()),
            "runner_source": record(Path(runner.__file__).resolve()),
            "authority_source": record(Path(builder.authority.__file__).resolve()),
            "runner_executable": record(Path(sys.executable).resolve()),
            "output_contract": str(self.contract_path),
        }
        write_json(self.config_path, self.config)

    def build_document(self) -> dict[str, object]:
        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=self.prior_audit_result,
        ):
            document, _ = builder.build_contract_document(self.config_path)
        return document

    def publish_contract(self) -> dict[str, object]:
        document = self.build_document()
        self.contract_path.write_bytes(builder.contract_bytes(document))
        return document

    def test_builder_seals_only_bounded_prefix_acquisition(self) -> None:
        document = self.build_document()
        acquisition = document["acquisition"]
        execution = acquisition["execution"]
        self.assertTrue(execution["acquisition_only"])
        self.assertFalse(execution["may_write_decision"])
        self.assertFalse(execution["may_enter_optimization"])
        self.assertEqual(execution["project_active_cap"], 50)
        self.assertEqual(execution["history_limit"], 601)
        self.assertGreaterEqual(execution["scheduler_timeout_seconds"], 300)
        argv = execution["campaign_argv"]
        self.assertEqual(argv.count("--history-limit"), 1)
        self.assertEqual(argv[argv.index("--history-limit") + 1], "601")
        self.assertEqual(argv.count("--timeout"), 1)
        self.assertEqual(argv[argv.index("--timeout") + 1], "300.0")
        self.assertEqual(Path(argv[2]), self.source_root / builder.CAMPAIGN_FILENAME)
        self.assertNotIn("continue_ipmsm_v2_stage2.py", " ".join(argv))
        self.assertNotIn("optimization", " ".join(argv).lower())
        self.assertEqual(
            set(acquisition["sources"]),
            {
                "campaign",
                "submit",
                "collector",
                "builder",
                "runner",
                "authority",
                "runner_executable",
                "inherited",
            },
        )

    def test_approved_source_hashes_are_exact_committed_lf_git_blobs(self) -> None:
        repository_root = Path(__file__).parents[1]
        for name, filename in (
            ("campaign", builder.CAMPAIGN_FILENAME),
            ("submit", builder.SUBMIT_FILENAME),
            ("collector", builder.COLLECTOR_FILENAME),
        ):
            payload = subprocess.check_output(
                ["git", "cat-file", "blob", f"HEAD:{filename}"],
                cwd=repository_root,
            )
            self.assertNotIn(b"\r", payload)
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                builder.APPROVED_PATCHED_SOURCE_SHA256[name],
            )

    def test_builder_rejects_import_shadow_and_existing_output_dir(self) -> None:
        shadow = self.source_root / "calibrate_ipmsm_beta.py"
        shadow.write_bytes(b"# shadow\n")
        with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "exactly the patched"):
            builder._load_config(self.config_path)
        shadow.unlink()
        self.output_dir.mkdir(parents=True)
        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=self.prior_audit_result,
        ):
            with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "must be absent"):
                builder.build_contract_document(self.config_path)

    def test_builder_rejects_attempt_bound_above_six_hundred(self) -> None:
        changed = json.loads(json.dumps(self.decision))
        retry_index = changed["stage2"]["runner_argv"].index("--terminal-retry-limit") + 1
        changed["stage2"]["runner_argv"][retry_index] = "2"
        changed_audit = {"binding": self.prior_binding, "decision": changed}
        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=(self.context, changed_audit, self.prior_audit_result[2]),
        ):
            with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "exactly one"):
                builder.build_contract_document(self.config_path)

    def test_builder_rejects_config_authorized_but_unreviewed_campaign_bytes(self) -> None:
        campaign = self.source_root / builder.CAMPAIGN_FILENAME
        campaign.write_bytes(b"# unreviewed acquisition code\n")
        self.config["campaign_source"] = record(campaign)
        write_json(self.config_path, self.config)
        with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "reviewed LF authority"):
            builder._load_config(self.config_path)

    def test_publish_requires_confirmed_dry_run_hash(self) -> None:
        with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "requires"):
            builder.build_or_publish(
                self.config_path,
                publish=True,
                expected_output_raw_sha256=None,
            )

    def test_process_authority_preserves_python_dash_b_in_real_subprocess(self) -> None:
        probe = Path(__file__).with_name("process_authority_probe_v4r7.py")
        for suffix in ((), ("--execute",)):
            completed = subprocess.run(
                [sys.executable, "-B", str(probe), *suffix],
                cwd=Path(__file__).parents[1],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_frozen_authority_allows_real_patched_source_import(self) -> None:
        self.publish_contract()
        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=self.prior_audit_result,
        ):
            context = runner.load_contract(self.contract_path)
            with runner._frozen_authority(context):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(self.source_root / builder.CAMPAIGN_FILENAME),
                        "--help",
                    ],
                    cwd=self.root,
                    env={
                        **os.environ,
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONPATH": os.pathsep.join((str(self.source_root), str(Path(__file__).parents[1]))),
                    },
                    check=False,
                    capture_output=True,
                    text=True,
                )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_runner_dry_run_is_read_only_and_decision_bound(self) -> None:
        self.publish_contract()
        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=self.prior_audit_result,
        ):
            context = runner.load_contract(self.contract_path)
            with mock.patch.object(runner, "_audit_process_authority"):
                report = runner.dry_run(context)
        self.assertEqual(report["action"], "resume_acquisition_only")
        self.assertEqual(report["writes_performed"], 0)
        self.assertFalse(self.output_dir.exists())
        self.decision_path.write_bytes(b"changed\n")
        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=self.prior_audit_result,
        ):
            with self.assertRaisesRegex(runner.Stage3AcquisitionError, "decision"):
                runner.load_contract(self.contract_path)

    def test_execute_calls_only_campaign_and_preserves_decision(self) -> None:
        self.publish_contract()
        original_decision = self.decision_path.read_bytes()
        calls: list[list[str]] = []

        def process(argv: list[str], **_: object) -> SimpleNamespace:
            calls.append(list(argv))
            self.output_dir.mkdir(parents=True)
            (self.output_dir / "merged_results.csv").write_bytes(b"case_id\ncase-001\n")
            return SimpleNamespace(returncode=0)

        def provenance(context: object) -> dict[str, object]:
            merged = self.output_dir / "merged_results.csv"
            return {
                "selected_plan": record(self.plan),
                "merged_result": record(merged),
                "result_count": 300,
                "result_set_sha256": "d" * 64,
            }

        scheduler_provenance = {
            "history_count": 300,
            "selected_task_count": 300,
            "selected_task_set_sha256": "e" * 64,
        }

        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=self.prior_audit_result,
        ), mock.patch.object(
            runner.stage2_continuation,
            "_validate_result_coverage",
        ), mock.patch.object(
            runner,
            "_audit_process_authority",
        ), mock.patch.object(
            runner,
            "_audit_output_provenance",
            side_effect=provenance,
        ), mock.patch.object(
            runner,
            "_audit_scheduler_provenance",
            return_value=scheduler_provenance,
        ):
            context = runner.load_contract(self.contract_path)
            report = runner.execute(context, runner=process)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], list(context.campaign_argv))
        self.assertNotIn("decision", " ".join(calls[0]).lower())
        self.assertNotIn("optimization", " ".join(calls[0]).lower())
        self.assertEqual(self.decision_path.read_bytes(), original_decision)
        self.assertEqual(report["status"], "acquisition_complete")
        self.assertTrue((self.acquisition_root / builder.COMPLETION_FILENAME).is_file())

    def test_execute_rejects_partial_output_before_campaign(self) -> None:
        self.publish_contract()
        self.output_dir.mkdir(parents=True)
        process = mock.Mock()
        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=self.prior_audit_result,
        ), mock.patch.object(runner, "_audit_process_authority"):
            context = runner.load_contract(self.contract_path)
            with self.assertRaisesRegex(runner.Stage3AcquisitionError, "partial"):
                runner.execute(context, runner=process)
        process.assert_not_called()
        self.assertFalse((self.acquisition_root / builder.COMPLETION_FILENAME).exists())

    def test_existing_merged_without_collector_provenance_is_rejected(self) -> None:
        self.publish_contract()
        self.output_dir.mkdir(parents=True)
        (self.output_dir / "merged_results.csv").write_bytes(b"case_id,status\ncase-001,ok\n")
        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=self.prior_audit_result,
        ), mock.patch.object(
            runner.stage2_continuation,
            "_validate_result_coverage",
        ):
            context = runner.load_contract(self.contract_path)
            with self.assertRaisesRegex(
                runner.Stage3AcquisitionError, "results directory|contents changed"
            ):
                runner._output_state(context)

    def test_scheduler_provenance_binds_exact_three_hundred_successes(self) -> None:
        self.publish_contract()
        tasks = [
            SimpleNamespace(
                row_number=index,
                case_id=f"case-{index:03d}",
                dedupe_key=f"dedupe-{index:03d}",
                task_name=f"ipmsm-v2-foundation-s3-v4r4-case-{index:03d}",
            )
            for index in range(1, 301)
        ]
        history = [
            {
                "id": 30_000 + index,
                "name": task.task_name,
                "project": "PYAEDT_MOTOR_IPMSM_V2",
                "status": "completed",
                "exit_code": 0,
                "dedupe_key": task.dedupe_key,
            }
            for index, task in enumerate(tasks, start=1)
        ]
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(history).encode("utf-8")
        parsed = SimpleNamespace(
            max_plan_cases=5000,
            case_start_index=1,
            case_limit=300,
        )
        parser = mock.Mock()
        parser.parse_args.return_value = parsed
        with mock.patch.object(
            builder,
            "_audit_prior_activation",
            return_value=self.prior_audit_result,
        ), mock.patch.object(
            runner.sealed_campaign,
            "build_parser",
            return_value=parser,
        ), mock.patch.object(
            runner.sealed_submit,
            "load_and_validate_cases",
            return_value=[{}] * 300,
        ), mock.patch.object(
            runner.sealed_submit,
            "select_case_rows",
            return_value=[{}] * 300,
        ), mock.patch.object(
            runner.sealed_submit,
            "build_campaign_tasks",
            return_value=tasks,
        ), mock.patch.object(
            runner.url_request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            context = runner.load_contract(self.contract_path)
            proof = runner._audit_scheduler_provenance(context)
        self.assertEqual(proof["history_count"], 300)
        self.assertEqual(proof["selected_task_count"], 300)
        called_url = urlopen.call_args.args[0]
        self.assertIn("limit=601", called_url)
        self.assertIn("project=PYAEDT_MOTOR_IPMSM_V2", called_url)
        self.assertIn("name_prefix=ipmsm-v2-foundation-s3-v4r4", called_url)


if __name__ == "__main__":
    unittest.main()
