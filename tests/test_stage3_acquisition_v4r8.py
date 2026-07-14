from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

import build_ipmsm_v2_stage3_acquisition_v4r8 as builder
import continue_ipmsm_v2_stage3_acquisition_v4r8 as runner


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(builder.authority.canonical_json_bytes(value))


def record(path: Path) -> dict[str, str]:
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


class Stage3AcquisitionV4r8Tests(unittest.TestCase):
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

        # Model the deployed LF source copies even though this Y: checkout is
        # configured to materialize committed LF blobs with CRLF endings.
        control_source_root = self.root / "sealed_control_sources"
        control_source_root.mkdir()
        for module, filename in (
            (builder, builder.BUILDER_FILENAME),
            (runner, builder.RUNNER_FILENAME),
            (builder.authority, builder.AUTHORITY_FILENAME),
        ):
            old_file = module.__file__
            destination = control_source_root / filename
            destination.write_bytes(Path(old_file).read_bytes().replace(b"\r\n", b"\n"))
            module.__file__ = str(destination)
            self.addCleanup(setattr, module, "__file__", old_file)

        # Production still requires C-local authorities.  The checkout used by
        # this test suite is on Y:, so permit only existing Y: source pins while
        # retaining the real path guard for the temporary contract tree.
        real_require_c_local = builder.authority._require_c_local
        checkout_root = Path(__file__).parents[1].resolve()

        def allow_checkout_source(path: Path, label: str) -> Path:
            candidate = Path(path).absolute()
            try:
                candidate.relative_to(checkout_root)
            except ValueError:
                pass
            else:
                if candidate.is_file():
                    return candidate
            return real_require_c_local(candidate, label)

        path_guard = mock.patch.object(
            builder.authority,
            "_require_c_local",
            side_effect=allow_checkout_source,
        )
        path_guard.start()
        self.addCleanup(path_guard.stop)

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
        runtime_relative_paths = {
            "run_batch": Path(builder.RUN_BATCH_FILENAME),
            "scheduler_job": Path(builder.SCHEDULER_JOB_FILENAME),
            "scheduler_task": Path(builder.SCHEDULER_TASK_FILENAME),
            "ppt_setup": Path("module") / builder.PPT_SETUP_FILENAME,
            "aedt_attach_client": Path("module") / builder.AEDT_ATTACH_CLIENT_FILENAME,
            "subprocess_runner": Path(builder.SUBPROCESS_RUN_FILENAME),
        }
        self.runtime_sources: dict[str, Path] = {}
        for name, relative_path in runtime_relative_paths.items():
            destination = self.root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                (repository_root / relative_path).read_bytes().replace(b"\r\n", b"\n")
            )
            self.runtime_sources[name] = destination

        self.prior_contract = self.root / builder.EXPECTED_PRIOR_CONTRACT
        self.prior_contract.parent.mkdir(parents=True, exist_ok=True)
        self.prior_contract.write_bytes(b"prior contract\n")
        self.plan = self.root / "simul_log_smoke/v4r4/stage3.csv"
        self.manifest = self.root / "simul_log_smoke/v4r4/stage3.manifest.json"
        self.plan_completion = (
            self.root / "simul_log_smoke/v4r6_stage3_activation/plan_completion.json"
        )
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
        # The deployed upstream authority remains sealed at 50.  v4r8 must
        # bridge this one value to 100 without broadening any other argv.
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
            "contract_sha256": builder.stage2_continuation._contract_sha256(
                execution_contract
            ),
            "execution_contract": execution_contract,
            "stage2": {
                "output_dir": str(self.output_dir),
                "runner_argv": list(self.base_argv),
            },
        }
        write_json(self.decision_path, self.decision)

        prior_source_root = self.root / builder.prior_acquisition_builder.SOURCE_RELATIVE_ROOT
        prior_source_root.mkdir(parents=True)
        for filename in (
            builder.CAMPAIGN_FILENAME,
            builder.SUBMIT_FILENAME,
            builder.COLLECTOR_FILENAME,
        ):
            (prior_source_root / filename).write_bytes(
                (repository_root / filename).read_bytes().replace(b"\r\n", b"\n")
            )
        self.prior_sources = {
            "campaign": record(prior_source_root / builder.CAMPAIGN_FILENAME),
            "submit": record(prior_source_root / builder.SUBMIT_FILENAME),
            "collector": record(prior_source_root / builder.COLLECTOR_FILENAME),
            "builder": record(
                repository_root / "build_ipmsm_v2_stage3_acquisition_v4r7.py"
            ),
            "runner": record(
                repository_root / "continue_ipmsm_v2_stage3_acquisition_v4r7.py"
            ),
            "authority": record(Path(builder.authority.__file__).resolve()),
            "runner_executable": record(Path(sys.executable).resolve()),
            "inherited": {},
        }
        self.prior_snapshot = builder.authority.read_single_link_snapshot(
            self.prior_contract, "test prior v4r7 acquisition"
        )
        activation_contract = (
            self.root / builder.prior_acquisition_builder.EXPECTED_PRIOR_CONTRACT
        )
        activation_contract.parent.mkdir(parents=True, exist_ok=True)
        activation_contract.write_bytes(b"activation contract\n")
        activation_snapshot = builder.authority.read_single_link_snapshot(
            activation_contract,
            "test activation",
        )
        shared_lock = self.root / "simul_log_smoke/v4r4/pipeline.lock"
        self.upstream_binding = {
            "activation_contract": {
                "path": str(activation_contract),
                "raw_sha256": activation_snapshot.sha256,
                "contract_sha256": "a" * 64,
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
            "shared_lock": str(shared_lock),
        }
        prior_campaign_argv = (
            str(Path(sys.executable).resolve()),
            "-B",
            str(prior_source_root / builder.CAMPAIGN_FILENAME),
            *self.base_argv,
            "--history-limit",
            str(builder.HISTORY_LIMIT),
            "--timeout",
            str(builder.SCHEDULER_TIMEOUT_SECONDS),
        )
        self.context = builder.PriorAcquisitionContext(
            snapshot=self.prior_snapshot,
            contract_sha256="c" * 64,
            document={
                "schema_version": builder.prior_acquisition_builder.CONTRACT_SCHEMA_VERSION,
                "contract_sha256": "c" * 64,
                "acquisition": {
                    "prior": self.upstream_binding,
                    "sources": self.prior_sources,
                },
            },
            root=self.root,
            prior=self.upstream_binding,
            sources=self.prior_sources,
            campaign_argv=prior_campaign_argv,
            project="PYAEDT_MOTOR_IPMSM_V2",
            scheduler_url="http://127.0.0.1:8000",
            task_prefix="ipmsm-v2-foundation-s3-v4r4",
            project_active_cap=50,
            history_limit=builder.HISTORY_LIMIT,
            scheduler_timeout_seconds=builder.SCHEDULER_TIMEOUT_SECONDS,
            expected_rows=builder.EXPECTED_ROWS,
            shared_lock=shared_lock,
            plan=self.plan,
            outputs={
                "campaign_output_dir": self.output_dir,
                "merged_result": self.output_dir / "merged_results.csv",
                "completion": self.root
                / builder.prior_acquisition_builder.RELATIVE_ROOT
                / builder.prior_acquisition_builder.COMPLETION_FILENAME,
            },
        )
        self.prior_binding = {
            "acquisition_contract": {
                "path": str(self.prior_contract),
                "raw_sha256": self.prior_snapshot.sha256,
                "contract_sha256": self.context.contract_sha256,
            },
            **self.upstream_binding,
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
                builder.authority.read_single_link_snapshot(
                    self.manifest, "test manifest"
                ),
                builder.authority.read_single_link_snapshot(
                    self.decision_path, "test decision"
                ),
                activation_snapshot,
            ),
        )

        self.config_path = self.acquisition_root / builder.BUILD_CONFIG_FILENAME
        self.contract_path = self.acquisition_root / builder.CONTRACT_FILENAME
        self.config = {
            "schema_version": builder.BUILD_CONFIG_SCHEMA_VERSION,
            "root": str(self.root),
            "prior_v4r7_contract": record(self.prior_contract),
            "campaign_source": record(self.source_root / builder.CAMPAIGN_FILENAME),
            "submit_source": record(self.source_root / builder.SUBMIT_FILENAME),
            "collector_source": record(self.source_root / builder.COLLECTOR_FILENAME),
            "run_batch_source": record(self.runtime_sources["run_batch"]),
            "scheduler_job_source": record(self.runtime_sources["scheduler_job"]),
            "scheduler_task_source": record(self.runtime_sources["scheduler_task"]),
            "ppt_setup_source": record(self.runtime_sources["ppt_setup"]),
            "aedt_attach_client_source": record(
                self.runtime_sources["aedt_attach_client"]
            ),
            "subprocess_runner_source": record(
                self.runtime_sources["subprocess_runner"]
            ),
            "builder_source": record(Path(builder.__file__).resolve()),
            "runner_source": record(Path(runner.__file__).resolve()),
            "authority_source": record(Path(builder.authority.__file__).resolve()),
            "runner_executable": record(Path(sys.executable).resolve()),
            "output_contract": str(self.contract_path),
        }
        write_json(self.config_path, self.config)

    def build_document(self, aedt_backend: str = "standalone") -> dict[str, object]:
        with mock.patch.object(
            builder,
            "_audit_prior_acquisition",
            return_value=self.prior_audit_result,
        ):
            document, _ = builder.build_contract_document(
                self.config_path,
                aedt_backend=aedt_backend,
            )
        return document

    def publish_contract(self, aedt_backend: str = "standalone") -> dict[str, object]:
        document = self.build_document(aedt_backend)
        self.contract_path.write_bytes(builder.contract_bytes(document))
        return document

    def resign_contract(self, document: dict[str, object]) -> None:
        unsigned = {
            "schema_version": document["schema_version"],
            "acquisition": document["acquisition"],
        }
        document["contract_sha256"] = builder.authority.canonical_sha256(unsigned)
        self.contract_path.write_bytes(builder.contract_bytes(document))

    def test_builder_seals_cap_backend_and_bounded_acquisition(self) -> None:
        document = self.build_document()
        acquisition = document["acquisition"]
        execution = acquisition["execution"]
        self.assertTrue(execution["acquisition_only"])
        self.assertFalse(execution["may_write_decision"])
        self.assertFalse(execution["may_enter_optimization"])
        self.assertEqual(execution["project_active_cap"], 100)
        self.assertEqual(execution["aedt_backend"], "standalone")
        self.assertEqual(execution["history_limit"], 601)
        self.assertGreaterEqual(execution["scheduler_timeout_seconds"], 300)

        argv = execution["campaign_argv"]
        self.assertEqual(argv.count("--project-active-cap"), 1)
        self.assertEqual(argv[argv.index("--project-active-cap") + 1], "100")
        self.assertEqual(argv.count("--aedt-backend"), 1)
        self.assertEqual(argv[argv.index("--aedt-backend") + 1], "standalone")
        self.assertEqual(argv.count("--history-limit"), 1)
        self.assertEqual(argv[argv.index("--history-limit") + 1], "601")
        self.assertEqual(argv.count("--timeout"), 1)
        self.assertEqual(argv[argv.index("--timeout") + 1], "300.0")
        self.assertEqual(Path(argv[2]), self.source_root / builder.CAMPAIGN_FILENAME)
        self.assertEqual(
            self.base_argv[self.base_argv.index("--project-active-cap") + 1],
            "50",
        )
        self.assertNotIn("optimization", " ".join(argv).lower())
        self.assertEqual(
            set(acquisition["sources"]),
            {
                "campaign",
                "submit",
                "collector",
                "run_batch",
                "scheduler_job",
                "scheduler_task",
                "ppt_setup",
                "aedt_attach_client",
                "subprocess_runner",
                "builder",
                "runner",
                "authority",
                "runner_executable",
                "inherited",
            },
        )

    def test_builder_uses_hash_bound_supplied_v4r7_contract_path(self) -> None:
        with mock.patch.object(
            builder,
            "_audit_prior_acquisition",
            return_value=self.prior_audit_result,
        ) as audit:
            builder.build_contract_document(self.config_path)
        audit.assert_called_with(self.prior_contract)
        self.assertEqual(
            self.config["prior_v4r7_contract"],
            record(self.prior_contract),
        )

        self.prior_contract.write_bytes(b"changed prior contract\n")
        with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "bytes changed"):
            builder._load_config(self.config_path)

    def test_backend_choice_is_sealed_and_verified_by_driver(self) -> None:
        for backend in ("standalone", "pooled"):
            with self.subTest(backend=backend):
                document = self.publish_contract(backend)
                with mock.patch.object(
                    builder,
                    "_audit_prior_acquisition",
                    return_value=self.prior_audit_result,
                ):
                    context = runner.load_contract(self.contract_path)
                execution = document["acquisition"]["execution"]
                self.assertEqual(execution["aedt_backend"], backend)
                self.assertEqual(context.aedt_backend, backend)
                self.assertEqual(
                    context.campaign_argv[
                        context.campaign_argv.index("--aedt-backend") + 1
                    ],
                    backend,
                )
                self.contract_path.unlink()

        parsed = builder.build_parser().parse_args(
            ["--build-config", str(self.config_path)]
        )
        self.assertEqual(parsed.aedt_backend, "standalone")
        pooled = builder.build_parser().parse_args(
            [
                "--build-config",
                str(self.config_path),
                "--aedt-backend",
                "pooled",
            ]
        )
        self.assertEqual(pooled.aedt_backend, "pooled")
        with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "one of"):
            builder.build_contract_document(self.config_path, aedt_backend="shared")

    def test_driver_rejects_backend_or_campaign_argv_tampering(self) -> None:
        for mutation in ("field", "argv", "cap"):
            with self.subTest(mutation=mutation):
                document = self.publish_contract("standalone")
                execution = document["acquisition"]["execution"]
                if mutation == "field":
                    execution["aedt_backend"] = "pooled"
                elif mutation == "argv":
                    index = execution["campaign_argv"].index("--aedt-backend") + 1
                    execution["campaign_argv"][index] = "pooled"
                else:
                    execution["project_active_cap"] = 99
                self.resign_contract(document)
                with mock.patch.object(
                    builder,
                    "_audit_prior_acquisition",
                    return_value=self.prior_audit_result,
                ):
                    with self.assertRaisesRegex(
                        runner.Stage3AcquisitionError,
                        "backend|campaign argv|project cap",
                    ):
                        runner.load_contract(self.contract_path)
                self.contract_path.unlink()

    def test_approved_source_hashes_are_current_committed_lf_blobs(self) -> None:
        repository_root = Path(__file__).parents[1]
        sources = (
            (
                builder.APPROVED_PATCHED_SOURCE_SHA256,
                "campaign",
                Path(builder.CAMPAIGN_FILENAME),
            ),
            (
                builder.APPROVED_PATCHED_SOURCE_SHA256,
                "submit",
                Path(builder.SUBMIT_FILENAME),
            ),
            (
                builder.APPROVED_PATCHED_SOURCE_SHA256,
                "collector",
                Path(builder.COLLECTOR_FILENAME),
            ),
            (
                builder.APPROVED_RUNTIME_SOURCE_SHA256,
                "run_batch",
                Path(builder.RUN_BATCH_FILENAME),
            ),
            (
                builder.APPROVED_RUNTIME_SOURCE_SHA256,
                "scheduler_job",
                Path(builder.SCHEDULER_JOB_FILENAME),
            ),
            (
                builder.APPROVED_RUNTIME_SOURCE_SHA256,
                "scheduler_task",
                Path(builder.SCHEDULER_TASK_FILENAME),
            ),
            (
                builder.APPROVED_RUNTIME_SOURCE_SHA256,
                "ppt_setup",
                Path("module") / builder.PPT_SETUP_FILENAME,
            ),
            (
                builder.APPROVED_RUNTIME_SOURCE_SHA256,
                "aedt_attach_client",
                Path("module") / builder.AEDT_ATTACH_CLIENT_FILENAME,
            ),
            (
                builder.APPROVED_RUNTIME_SOURCE_SHA256,
                "subprocess_runner",
                Path(builder.SUBPROCESS_RUN_FILENAME),
            ),
        )
        for authority_hashes, name, filename in sources:
            payload = subprocess.check_output(
                ["git", "cat-file", "blob", f"HEAD:{filename.as_posix()}"],
                cwd=repository_root,
            )
            self.assertNotIn(b"\r", payload)
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                authority_hashes[name],
            )

    def test_builder_rejects_unreviewed_source_even_when_config_hash_matches(self) -> None:
        campaign = self.source_root / builder.CAMPAIGN_FILENAME
        campaign.write_bytes(b"# unreviewed acquisition code\n")
        self.config["campaign_source"] = record(campaign)
        write_json(self.config_path, self.config)
        with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "reviewed LF authority"):
            builder._load_config(self.config_path)

    def test_builder_rejects_unreviewed_pooled_runtime_source(self) -> None:
        run_batch = self.runtime_sources["run_batch"]
        run_batch.write_bytes(b"# unreviewed pooled worker\n")
        self.config["run_batch_source"] = record(run_batch)
        write_json(self.config_path, self.config)
        with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "runtime run_batch"):
            builder._load_config(self.config_path)

    def test_builder_rejects_import_shadow_and_existing_output_dir(self) -> None:
        shadow = self.source_root / "calibrate_ipmsm_beta.py"
        shadow.write_bytes(b"# shadow\n")
        with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "exactly the patched"):
            builder._load_config(self.config_path)
        shadow.unlink()

        self.output_dir.mkdir(parents=True)
        with mock.patch.object(
            builder,
            "_audit_prior_acquisition",
            return_value=self.prior_audit_result,
        ):
            with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "must be absent"):
                builder.build_contract_document(self.config_path)

    def test_publish_is_dry_run_first_and_no_replace(self) -> None:
        with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "requires"):
            builder.build_or_publish(
                self.config_path,
                publish=True,
                expected_output_raw_sha256=None,
            )

        with mock.patch.object(
            builder,
            "_audit_prior_acquisition",
            return_value=self.prior_audit_result,
        ):
            dry = builder.build_or_publish(
                self.config_path,
                publish=False,
                expected_output_raw_sha256=None,
                aedt_backend="standalone",
            )
            self.assertEqual(dry["writes_performed"], 0)
            self.assertFalse(self.contract_path.exists())
            published = builder.build_or_publish(
                self.config_path,
                publish=True,
                expected_output_raw_sha256=dry["output_raw_sha256"],
                aedt_backend="standalone",
            )
            before = self.contract_path.read_bytes()
            existing = builder.build_or_publish(
                self.config_path,
                publish=True,
                expected_output_raw_sha256=dry["output_raw_sha256"],
                aedt_backend="standalone",
            )
            with self.assertRaisesRegex(builder.Stage3AcquisitionBuildError, "backend"):
                builder.build_or_publish(
                    self.config_path,
                    publish=False,
                    expected_output_raw_sha256=None,
                    aedt_backend="pooled",
                )
        self.assertEqual(published["writes_performed"], 1)
        self.assertEqual(existing["writes_performed"], 0)
        self.assertEqual(self.contract_path.read_bytes(), before)

    def test_runner_dry_run_is_read_only_and_decision_bound(self) -> None:
        self.publish_contract("pooled")
        with mock.patch.object(
            builder,
            "_audit_prior_acquisition",
            return_value=self.prior_audit_result,
        ):
            context = runner.load_contract(self.contract_path)
            with mock.patch.object(runner, "_audit_process_authority"):
                report = runner.dry_run(context)
        self.assertEqual(report["action"], "resume_acquisition_only")
        self.assertEqual(report["writes_performed"], 0)
        self.assertEqual(report["aedt_backend"], "pooled")
        self.assertFalse(self.output_dir.exists())

        self.decision_path.write_bytes(b"changed\n")
        with mock.patch.object(
            builder,
            "_audit_prior_acquisition",
            return_value=self.prior_audit_result,
        ):
            with self.assertRaisesRegex(runner.Stage3AcquisitionError, "decision"):
                runner.load_contract(self.contract_path)

    def test_runner_rejects_source_change_after_sealing(self) -> None:
        self.publish_contract()
        campaign = self.source_root / builder.CAMPAIGN_FILENAME
        evidence = campaign.read_bytes()
        campaign.write_bytes(evidence + b"# changed after sealing\n")
        with mock.patch.object(
            builder,
            "_audit_prior_acquisition",
            return_value=self.prior_audit_result,
        ):
            with self.assertRaisesRegex(runner.Stage3AcquisitionError, "bytes changed"):
                runner.load_contract(self.contract_path)
        self.assertTrue(campaign.read_bytes().endswith(b"# changed after sealing\n"))

    def test_persisted_permanent_failure_decision_is_terminal_evidence(self) -> None:
        case_ids = [f"case-{index:03d}" for index in range(1, 301)]
        plan_payload = "case_id\n" + "".join(f"{case_id}\n" for case_id in case_ids)
        self.plan.write_text(plan_payload, encoding="utf-8", newline="")
        self.output_dir.mkdir()
        (self.output_dir / runner.sealed_collector.SELECTED_PLAN_NAME).write_text(
            plan_payload,
            encoding="utf-8",
            newline="",
        )
        (self.output_dir / runner.sealed_collector.SUCCESSFUL_PLAN_NAME).write_text(
            "case_id\n",
            encoding="utf-8",
            newline="",
        )
        (self.output_dir / "merged_results.csv").write_text(
            "case_id,status\n",
            encoding="utf-8",
            newline="",
        )
        (self.output_dir / "results").mkdir()

        failed_dir = self.output_dir / runner.sealed_collector.FAILED_RESULTS_DIR_NAME
        failed_dir.mkdir()
        failed_result = failed_dir / "case-001_attempt_01.csv"
        failed_payload = b"case_id,status\ncase-001,failed\n"
        failed_result.write_bytes(failed_payload)
        failures: list[dict[str, object]] = []
        for index, case_id in enumerate(case_ids, start=1):
            evidence: list[dict[str, object]] = [
                {
                    "kind": "result",
                    "retry_index": retry_index,
                    "task_id": index * 10 + retry_index,
                    "dedupe_key": (
                        f"dedupe-{case_id}"
                        if retry_index == 0
                        else f"dedupe-{case_id}-retry-01"
                    ),
                    "scheduler_status": "completed",
                    "result_status": "failed",
                    "remote_result": (
                        f"remote/{case_id}.csv"
                        if retry_index == 0
                        else f"remote/{case_id}_retry_01.csv"
                    ),
                }
                for retry_index in (0, 1)
            ]
            if index == 1:
                evidence[-1].update(
                    {
                        "local_result": str(failed_result),
                        "local_result_sha256": hashlib.sha256(failed_payload).hexdigest(),
                    }
                )
            failures.append(
                {
                    "case_id": case_id,
                    "attempts": 2,
                    "failure_evidence": evidence,
                }
            )
        case_records = [
            {
                "case_id": failure["case_id"],
                "outcome": "permanent_failure",
                "attempts": failure["attempts"],
                "failure_evidence": failure["failure_evidence"],
            }
            for failure in failures
        ]
        summary_path = self.output_dir / runner.sealed_collector.CAMPAIGN_SUMMARY_NAME
        decision_path = self.output_dir / runner.sealed_collector.CAMPAIGN_DECISION_NAME
        summary = {
            "schema_version": runner.sealed_collector.CAMPAIGN_SUMMARY_SCHEMA_VERSION,
            "status": "completed_with_permanent_failures",
            "project": "PYAEDT_MOTOR_IPMSM_V2",
            "history_rows": 600,
            "history_campaign_tasks": 600,
            "selected_cases": 300,
            "successful_cases": 0,
            "permanently_failed_cases": 300,
            "selected_plan": str(
                self.output_dir / runner.sealed_collector.SELECTED_PLAN_NAME
            ),
            "successful_plan": str(
                self.output_dir / runner.sealed_collector.SUCCESSFUL_PLAN_NAME
            ),
            "merged_output": str(self.output_dir / "merged_results.csv"),
            "output_dir": str(self.output_dir),
            "cases": case_records,
            "permanent_failures": failures,
        }
        write_json(summary_path, summary)
        decision = {
            "schema_version": runner.sealed_collector.CAMPAIGN_DECISION_SCHEMA_VERSION,
            "status": "completed_with_permanent_failures",
            "selected_cases": 300,
            "successful_cases": 0,
            "permanently_failed_cases": 300,
            "summary": {
                "path": str(summary_path),
                "sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
            },
            "permanent_failures": failures,
        }
        write_json(decision_path, decision)
        context = SimpleNamespace(
            outputs={
                "campaign_output_dir": self.output_dir,
                "merged_result": self.output_dir / "merged_results.csv",
                "campaign_summary": summary_path,
                "campaign_decision": decision_path,
            },
            plan=self.plan,
            root=self.root,
            project="PYAEDT_MOTOR_IPMSM_V2",
            expected_rows=300,
            campaign_argv=("--terminal-retry-limit", "1"),
        )

        proof = runner._audit_output_provenance(context)
        self.assertEqual(proof["status"], "completed_with_permanent_failures")
        self.assertEqual(proof["result_count"], 0)
        self.assertEqual(proof["permanently_failed_count"], 300)
        self.assertEqual(failed_result.read_bytes(), failed_payload)

    def test_scheduler_provenance_accepts_recovered_and_legacy_retry_lineages(self) -> None:
        document = self.build_document()
        execution = document["acquisition"]["execution"]
        campaign_argv = tuple(str(item) for item in execution["campaign_argv"])
        args = runner.sealed_campaign.build_parser().parse_args(list(campaign_argv[3:]))
        rows = runner.sealed_submit.load_and_validate_cases(
            self.plan,
            args.max_plan_cases,
            False,
        )
        base = runner.sealed_submit.build_campaign_tasks(
            args,
            rows,
            first_row_number=args.case_start_index,
        )[0]
        retry = runner.sealed_submit.build_campaign_task_lineages(
            args,
            rows,
            first_row_number=args.case_start_index,
            terminal_retry_limit=1,
        )[base.dedupe_key][1]
        context = SimpleNamespace(
            campaign_argv=campaign_argv,
            plan=self.plan,
            expected_rows=1,
            project=execution["project"],
            scheduler_url=execution["scheduler_url"],
            history_limit=execution["history_limit"],
            task_prefix=execution["task_prefix"],
            scheduler_timeout_seconds=execution["scheduler_timeout_seconds"],
        )

        scenarios = (
            (
                "result_level_fresh_retry",
                [
                    {
                        "id": 1,
                        "project": context.project,
                        "name": base.task_name,
                        "status": "completed",
                        "exit_code": 0,
                        "dedupe_key": base.dedupe_key,
                    },
                    {
                        "id": 2,
                        "project": context.project,
                        "name": retry.task_name,
                        "status": "completed",
                        "exit_code": 0,
                        "dedupe_key": retry.dedupe_key,
                    },
                ],
                [
                    {
                        "kind": "result_level_terminal",
                        "retry_index": 0,
                        "task_id": 1,
                        "dedupe_key": base.dedupe_key,
                        "scheduler_status": "completed",
                        "result_status": "failed",
                        "remote_result": base.result_csv,
                    },
                    {
                        "kind": "result",
                        "retry_index": 1,
                        "task_id": 2,
                        "dedupe_key": retry.dedupe_key,
                        "scheduler_status": "completed",
                        "result_status": "ok",
                        "remote_result": retry.result_csv,
                    },
                ],
            ),
            (
                "legacy_same_dedupe_retry",
                [
                    {
                        "id": 1,
                        "project": context.project,
                        "name": base.task_name,
                        "status": "failed",
                        "exit_code": 1,
                        "dedupe_key": base.dedupe_key,
                    },
                    {
                        "id": 2,
                        "project": context.project,
                        "name": base.task_name,
                        "status": "completed",
                        "exit_code": 0,
                        "dedupe_key": base.dedupe_key,
                    },
                ],
                [
                    {
                        "kind": "scheduler_terminal",
                        "retry_index": 0,
                        "task_id": 1,
                        "dedupe_key": base.dedupe_key,
                        "scheduler_status": "failed",
                        "result_status": None,
                        "remote_result": base.result_csv,
                    },
                    {
                        "kind": "result",
                        "retry_index": 0,
                        "task_id": 2,
                        "dedupe_key": base.dedupe_key,
                        "scheduler_status": "completed",
                        "result_status": "ok",
                        "remote_result": base.result_csv,
                    },
                ],
            ),
        )
        for label, history, evidence in scenarios:
            with self.subTest(label=label):
                response = mock.MagicMock()
                response.read.return_value = json.dumps(history).encode("utf-8")
                response.__enter__.return_value = response
                provenance = {
                    "status": "complete",
                    "successful_case_ids": [base.case_id],
                    "failed_case_ids": [],
                    "case_records": [
                        {
                            "case_id": base.case_id,
                            "outcome": "success",
                            "attempts": 2,
                            "attempt_evidence": evidence,
                        }
                    ],
                }
                with mock.patch.object(
                    runner.url_request,
                    "urlopen",
                    return_value=response,
                ):
                    proof = runner._audit_scheduler_provenance(context, provenance)
                self.assertEqual(proof["successful_case_count"], 1)
                self.assertEqual(proof["audited_attempt_count"], 2)

    def test_execute_records_terminal_failures_without_rerunning_campaign(self) -> None:
        self.publish_contract()
        self.output_dir.mkdir()
        merged = self.output_dir / "merged_results.csv"
        merged.write_bytes(b"case_id,status\ncase-001,ok\n")
        terminal_provenance = {
            "status": "completed_with_permanent_failures",
            "selected_plan": record(self.plan),
            "successful_plan": record(self.plan),
            "merged_result": record(merged),
            "result_count": 299,
            "permanently_failed_count": 1,
            "permanent_failures": [
                {
                    "case_id": "case-300",
                    "attempts": 2,
                    "failure_evidence": [],
                }
            ],
            "campaign_summary": {"path": "summary", "sha256": "d" * 64},
            "campaign_decision": {"path": "decision", "sha256": "e" * 64},
            "result_set_sha256": "f" * 64,
        }
        scheduler_provenance = {
            "history_count": 301,
            "selected_task_count": 300,
            "selected_task_set_sha256": "9" * 64,
        }
        process = mock.Mock()
        with mock.patch.object(
            builder,
            "_audit_prior_acquisition",
            return_value=self.prior_audit_result,
        ), mock.patch.object(
            runner,
            "_audit_process_authority",
        ), mock.patch.object(
            runner,
            "_audit_output_provenance",
            return_value=terminal_provenance,
        ), mock.patch.object(
            runner,
            "_audit_scheduler_provenance",
            return_value=scheduler_provenance,
        ):
            context = runner.load_contract(self.contract_path)
            first = runner.execute(context, runner=process)
            second = runner.execute(context, runner=process)

        process.assert_not_called()
        self.assertEqual(
            first["status"],
            "acquisition_completed_with_permanent_failures",
        )
        self.assertEqual(first["action"], "record_permanent_failures")
        self.assertEqual(first["successful_cases"], 299)
        self.assertEqual(first["permanently_failed_cases"], 1)
        self.assertEqual(first["writes_performed"], 1)
        self.assertEqual(second["writes_performed"], 0)
        completion = json.loads(
            (self.acquisition_root / builder.COMPLETION_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(completion["status"], "completed_with_permanent_failures")

    def test_execute_calls_only_exact_campaign_and_preserves_decision(self) -> None:
        self.publish_contract("pooled")
        original_decision = self.decision_path.read_bytes()
        calls: list[list[str]] = []

        def process(argv: list[str], **_: object) -> SimpleNamespace:
            calls.append(list(argv))
            self.output_dir.mkdir(parents=True)
            (self.output_dir / "merged_results.csv").write_bytes(
                b"case_id,status\ncase-001,ok\n"
            )
            return SimpleNamespace(returncode=0)

        def provenance(context: object) -> dict[str, object]:
            merged = self.output_dir / "merged_results.csv"
            return {
                "status": "complete",
                "selected_plan": record(self.plan),
                "merged_result": record(merged),
                "result_count": 300,
                "permanently_failed_count": 0,
                "permanent_failures": [],
                "result_set_sha256": "d" * 64,
            }

        scheduler_provenance = {
            "history_count": 300,
            "selected_task_count": 300,
            "selected_task_set_sha256": "e" * 64,
        }
        with mock.patch.object(
            builder,
            "_audit_prior_acquisition",
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

        self.assertEqual(calls, [list(context.campaign_argv)])
        self.assertEqual(
            calls[0][calls[0].index("--aedt-backend") + 1],
            "pooled",
        )
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
            "_audit_prior_acquisition",
            return_value=self.prior_audit_result,
        ), mock.patch.object(runner, "_audit_process_authority"):
            context = runner.load_contract(self.contract_path)
            with self.assertRaisesRegex(runner.Stage3AcquisitionError, "partial"):
                runner.execute(context, runner=process)
        process.assert_not_called()
        self.assertFalse((self.acquisition_root / builder.COMPLETION_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
