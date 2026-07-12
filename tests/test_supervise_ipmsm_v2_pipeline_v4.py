from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import continue_ipmsm_v2_stage2 as stage2
import continue_ipmsm_v2_optimization as legacy_optimizer
import authorize_ipmsm_v2_optimization_v4 as authorizer
import supervise_ipmsm_v2_pipeline as v3
import supervise_ipmsm_v2_pipeline_v4 as v4


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def gate(decision: str = "skip_stage2") -> stage2.GateResult:
    failures = () if decision == "skip_stage2" else ("output_torque_last_avg_nm",)
    return stage2.GateResult(
        decision=decision,
        validation={},
        primary_test_r2={"output_torque_last_avg_nm": 0.96 if not failures else 0.91},
        primary_failures=failures,
        voltage_test_r2=0.96,
        voltage_failed=False,
        fingerprints={},
    )


class Fixture:
    def __init__(self, root: Path, *, publish: bool = True) -> None:
        self.root = root
        self.base_path = root / "base.json"
        self.v4_path = root / "pipeline-v4.json"
        self.workspace = root / "official"
        self.declaration = root / "authority" / "declaration.json"
        self.confirmation = root / "authority" / "confirmation.json"
        self.receipt = root / "authority" / "receipt.json"
        self.immutable = root / "immutable.txt"
        self.immutable.write_text("fixed\n", encoding="utf-8")
        for filename in (
            "publish_ipmsm_v2_stage1_official_v4.py",
            "confirm_ipmsm_v2_optimization_inputs.py",
            "authorize_ipmsm_v2_optimization_v4.py",
            *v4.LEGACY_OPTIMIZATION_SOURCE_FILENAMES,
        ):
            destination = root / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(v4.__file__).parent / filename, destination)
        self.runner = root / "continue_ipmsm_v2_optimization_v4.py"
        self.runner.write_text("# inactive test runner\n", encoding="utf-8")
        self._write_base()
        self.document = v4.build_contract_document(
            base_contract_path=self.base_path,
            output_path=self.v4_path,
            stage1_workspace=self.workspace,
            declaration=self.declaration,
            confirmation=self.confirmation,
            receipt=self.receipt,
            optimization_runner=self.runner,
        )
        self.contract = (
            v4.publish_contract(self.v4_path, self.document) if publish else None
        )

    def _write_base(self) -> None:
        py = sys.executable
        write_csv(
            self.root / "stage1.csv",
            [
                {"case_id": "a", "design_hash": "d1"},
                {"case_id": "b", "design_hash": "d1"},
            ],
        )
        pipeline: dict[str, object] = {
            "workdir": str(self.root),
            "lock_path": "pipeline.lock",
            "immutable_inputs": [
                {"path": "immutable.txt", "sha256": sha256(self.immutable)}
            ],
            "stage1": {
                "case_plan": "stage1.csv",
                "output_dir": "stage1-out",
                "result": "stage1-out/merged_results.csv",
                "validation": "legacy-validation.csv",
                "model_dir": "legacy-models",
                "metadata": "legacy-models/metadata.json",
                "r2": "legacy-r2.csv",
                "expected_rows": 2,
                "expected_groups": 1,
                "expected_repeats": 1,
                "r2_threshold": 0.95,
                "ensemble_size": 5,
                "conformal_coverage": 0.95,
                "campaign_argv": [
                    py,
                    "run_ipmsm_v2_campaign.py",
                    "--cases",
                    "stage1.csv",
                    "--output-dir",
                    "stage1-out",
                    "--submit",
                ],
                "validation_argv": [
                    py,
                    "validate_ipmsm_v2_dataset.py",
                    "--data",
                    "stage1-out/merged_results.csv",
                    "--summary",
                    "legacy-validation.csv",
                ],
                "training_argv": [
                    py,
                    "train_ipmsm_lightgbm.py",
                    "--v2",
                    "--data",
                    "stage1-out/merged_results.csv",
                    "--model-dir",
                    "legacy-models",
                    "--verification-output",
                    "legacy-r2.csv",
                ],
            },
            "stage2": {
                "decision": "stage2.json",
                "argv": [
                    py,
                    "continue_ipmsm_v2_stage2.py",
                    "--stage1-validation",
                    "legacy-validation.csv",
                    "--stage1-metadata",
                    "legacy-models/metadata.json",
                    "--stage1-r2",
                    "legacy-r2.csv",
                    "--decision-output",
                    "stage2.json",
                ],
            },
            "stage3": {
                "prior_plan": "stage12.csv",
                "prior_manifest": "stage12.manifest.json",
                "plan": "stage3.csv",
                "manifest": "stage3.manifest.json",
                "decision": "stage3.json",
                "expected_rows": 2,
                "merge_argv": [
                    py,
                    "merge_ipmsm_v2_case_plans.py",
                    "--output",
                    "stage12.csv",
                    "--manifest-output",
                    "stage12.manifest.json",
                ],
                "generate_argv": [
                    py,
                    "generate_ipmsm_v2_cases.py",
                    "--stage3-fallback",
                    "--output",
                    "stage3.csv",
                    "--stage3-manifest-output",
                    "stage3.manifest.json",
                    "--stage2-failed-decision",
                    "stage2.json",
                ],
                "continuation_argv": [
                    py,
                    "continue_ipmsm_v2_stage2.py",
                    "--decision-output",
                    "stage3.json",
                ],
            },
            "optimization": {
                "decision": "optimization.json",
                "argv_template": [
                    py,
                    "continue_ipmsm_v2_optimization.py",
                    "--stage2-decision",
                    v3.UPSTREAM_PLACEHOLDER,
                    "--optimization-spec",
                    "spec.json",
                    "--beta-summary",
                    "beta-summary.json",
                    "--beta-case-plan",
                    "beta-plan.csv",
                    "--beta-results",
                    "beta-results.csv",
                    "--beta-calibration-manifest",
                    "beta-manifest.json",
                    "--output-dir",
                    "optimization-output",
                    "--checkpoint-dir",
                    "optimization-checkpoints",
                    "--decision-output",
                    "optimization.json",
                    "--project",
                    "PYAEDT_MOTOR_IPMSM_V2",
                ],
            },
            "speed": {
                "plan": "speed.csv",
                "output_dir": "speed-out",
                "result": "speed-out/merged_results.csv",
                "rank": "speed-rank.csv",
                "top": "speed-top.csv",
                "marker": "speed-complete.json",
                "expected_rows": 2,
                "minimum_top_profiles": 2,
                "plan_argv": [
                    py,
                    "generate_ipmsm_second_pass_cases.py",
                    "--output",
                    "speed.csv",
                ],
                "campaign_argv": [
                    py,
                    "run_ipmsm_v2_campaign.py",
                    "--cases",
                    "speed.csv",
                    "--output-dir",
                    "speed-out",
                    "--submit",
                ],
                "rank_argv": [
                    py,
                    "rank_ipmsm_second_pass_profiles.py",
                    "--strict-speed-plan",
                    "speed.csv",
                    "--strict-candidate-results",
                    "speed-out/merged_results.csv",
                    "--output",
                    "speed-rank.csv",
                    "--top-profiles-output",
                    "speed-top.csv",
                ],
            },
        }
        unsigned = {"schema_version": v3.CONTRACT_SCHEMA_VERSION, "pipeline": pipeline}
        document = {**unsigned, "contract_sha256": v3._canonical_sha256(unsigned)}
        self.base_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        (self.root / "spec.json").write_text("{}\n", encoding="utf-8")

    def campaign(self) -> None:
        write_csv(
            self.root / "stage1-out" / "merged_results.csv",
            [
                {"case_id": "a", "status": "ok", "design_hash": "d1"},
                {"case_id": "b", "status": "ok", "design_hash": "d1"},
            ],
        )

    def official(self) -> v4.AuditedOfficialStage1:
        validation = self.workspace / "attempts" / "a" / "validation.csv"
        model_dir = self.workspace / "attempts" / "a" / "models"
        metadata = model_dir / "metadata.json"
        r2 = self.workspace / "attempts" / "a" / "r2.csv"
        for path in (validation, metadata, r2):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("official\n", encoding="utf-8")
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "completion.json").write_text("{}\n", encoding="utf-8")
        base_stage1 = self.contract.base_contract.stage1
        official_stage1 = v4.dataclasses.replace(
            base_stage1,
            validation=validation,
            model_dir=model_dir,
            metadata=metadata,
            r2=r2,
        )
        bundle = types.SimpleNamespace(
            completion_path=self.workspace / "completion.json",
            completion_sha256="c" * 64,
            result_sha256=sha256(base_stage1.result),
            validation=validation,
            model_dir=model_dir,
            metadata=metadata,
            r2=r2,
            gate=gate(),
        )
        return v4.AuditedOfficialStage1(bundle, official_stage1, gate())

    def decision(
        self,
        path: Path,
        schema: str,
        status: str,
        execution_contract: dict[str, object],
    ) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": schema,
            "mode": "execute",
            "status": status,
            "decision_output": str(path),
            "execution_contract": execution_contract,
        }
        document["contract_sha256"] = v3._canonical_sha256(execution_contract)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return document

    def stage2_complete(self, official: v4.AuditedOfficialStage1) -> None:
        stage1 = official.stage1
        self.decision(
            self.root / "stage2.json",
            v3.STAGE2_DECISION_SCHEMA_VERSION,
            "complete",
            {
                "stage1": {
                    "result": v4._artifact_record(stage1.result),
                    "validation": v4._artifact_record(stage1.validation),
                    "metadata": v4._artifact_record(stage1.metadata),
                    "r2": v4._artifact_record(stage1.r2),
                }
            },
        )


class SupervisorV4Tests(unittest.TestCase):
    @staticmethod
    def _kill_after(function: object, message: str):
        def killed(*args: object, **kwargs: object):
            result = function(*args, **kwargs)  # type: ignore[operator]
            raise KeyboardInterrupt(message)

        return killed

    @staticmethod
    def _build_cli_argv(fixture: Fixture, *, write: bool) -> list[str]:
        argv = [
            "--build-base-contract",
            str(fixture.base_path),
            "--output-contract",
            str(fixture.v4_path),
            "--stage1-workspace",
            str(fixture.workspace),
            "--declaration",
            str(fixture.declaration),
            "--confirmation",
            str(fixture.confirmation),
            "--receipt",
            str(fixture.receipt),
            "--optimization-runner",
            str(fixture.runner),
        ]
        if write:
            argv.append("--write-contract")
        return argv

    def test_contract_build_cli_recovers_link_kill_and_reports_exact_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)
            real_link = v4._link_contract_destination
            with mock.patch.object(
                v4,
                "_link_contract_destination",
                side_effect=self._kill_after(real_link, "cli-link-kill"),
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "cli-link-kill"):
                    v4.main(self._build_cli_argv(fixture, write=True))
            payload = v4._contract_document_bytes(fixture.document)
            staged = v4.contract_staged_path(fixture.v4_path, payload)
            proof = v4.contract_proof_path(fixture.v4_path)
            self.assertTrue(os.path.samefile(staged, fixture.v4_path))
            self.assertEqual(fixture.v4_path.stat().st_nlink, 2)
            inode = fixture.v4_path.stat().st_dev, fixture.v4_path.stat().st_ino
            before = {
                path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino, path.stat().st_nlink)
                for path in (staged, fixture.v4_path, proof)
            }

            dry_stdout = io.StringIO()
            with contextlib.redirect_stdout(dry_stdout):
                self.assertEqual(
                    v4.main(self._build_cli_argv(fixture, write=False)), 0
                )
            dry = json.loads(dry_stdout.getvalue())
            self.assertEqual(dry["status"], "publication_recovery_pending")
            self.assertEqual(dry["publication_state"], "post_commit_stage_linked")
            self.assertEqual(dry["writes_performed"], 0)
            self.assertEqual(dry["transaction_mutations"], 0)
            self.assertEqual(
                {
                    path: (
                        path.read_bytes(),
                        path.stat().st_dev,
                        path.stat().st_ino,
                        path.stat().st_nlink,
                    )
                    for path in (staged, fixture.v4_path, proof)
                },
                before,
            )

            recovered_stdout = io.StringIO()
            with contextlib.redirect_stdout(recovered_stdout):
                self.assertEqual(
                    v4.main(self._build_cli_argv(fixture, write=True)), 0
                )
            recovered = json.loads(recovered_stdout.getvalue())
            self.assertEqual(recovered["status"], "recovered")
            self.assertEqual(
                recovered["publication_state"], "post_commit_stage_linked"
            )
            self.assertEqual(recovered["writes_performed"], 1)
            self.assertGreater(recovered["transaction_mutations"], 0)
            self.assertEqual(
                (fixture.v4_path.stat().st_dev, fixture.v4_path.stat().st_ino),
                inode,
            )

            replay_stdout = io.StringIO()
            with contextlib.redirect_stdout(replay_stdout):
                self.assertEqual(
                    v4.main(self._build_cli_argv(fixture, write=True)), 0
                )
            replay = json.loads(replay_stdout.getvalue())
            self.assertEqual(replay["status"], "already_present")
            self.assertEqual(replay["publication_state"], "committed")
            self.assertEqual(replay["writes_performed"], 0)
            self.assertEqual(replay["transaction_mutations"], 0)

    def test_contract_build_cli_created_and_foreign_output_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(v4.main(self._build_cli_argv(fixture, write=True)), 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["status"], "created")
            self.assertEqual(report["publication_state"], "absent")
            self.assertEqual(report["writes_performed"], 1)
            self.assertGreater(report["transaction_mutations"], 0)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)
            fixture.v4_path.write_bytes(b"foreign contract bytes\n")
            original = fixture.v4_path.read_bytes()
            identity = fixture.v4_path.stat().st_dev, fixture.v4_path.stat().st_ino
            for write in (False, True):
                with self.subTest(write=write), self.assertRaises(FileExistsError):
                    v4.main(self._build_cli_argv(fixture, write=write))
                self.assertEqual(fixture.v4_path.read_bytes(), original)
                self.assertEqual(
                    (fixture.v4_path.stat().st_dev, fixture.v4_path.stat().st_ino),
                    identity,
                )

    def test_contract_publication_recovers_every_repeated_kill_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)
            hooks = (
                "_create_contract_attempt",
                "_stage_contract_publication",
                "_write_contract_proof",
                "_link_contract_destination",
                "_unlink_contract_stage",
                "_remove_contract_attempt",
                "_unlink_contract_proof",
            )
            committed_identity: tuple[int, int] | None = None
            for hook in hooks:
                original = getattr(v4, hook)
                with mock.patch.object(
                    v4,
                    hook,
                    side_effect=self._kill_after(original, hook),
                ):
                    with self.assertRaisesRegex(KeyboardInterrupt, hook):
                        v4.publish_contract(fixture.v4_path, fixture.document)
                if hook == "_link_contract_destination":
                    staged = v4.contract_staged_path(
                        fixture.v4_path,
                        v4._contract_document_bytes(fixture.document),
                    )
                    self.assertTrue(os.path.samefile(staged, fixture.v4_path))
                    self.assertEqual(fixture.v4_path.stat().st_nlink, 2)
                    committed_identity = (
                        fixture.v4_path.stat().st_dev,
                        fixture.v4_path.stat().st_ino,
                    )
                if hook == "_unlink_contract_stage":
                    self.assertEqual(fixture.v4_path.stat().st_nlink, 1)
            recovered = v4.publish_contract(fixture.v4_path, fixture.document)
            self.assertEqual(recovered.contract_sha256, fixture.document["contract_sha256"])
            self.assertIsNotNone(committed_identity)
            self.assertEqual(
                (fixture.v4_path.stat().st_dev, fixture.v4_path.stat().st_ino),
                committed_identity,
            )
            payload = v4._contract_document_bytes(fixture.document)
            self.assertFalse(os.path.lexists(v4.contract_attempt_path(fixture.v4_path, payload)))
            self.assertFalse(os.path.lexists(v4.contract_staged_path(fixture.v4_path, payload)))
            self.assertFalse(os.path.lexists(v4.contract_proof_path(fixture.v4_path)))

    def test_same_payload_late_attempt_concurrency_converges_without_inode_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)
            real_create = v4._create_contract_attempt
            waiting = threading.Event()
            release = threading.Event()
            results: list[dict[str, object]] = []
            failures: list[BaseException] = []

            def delayed_create(path: Path):
                if threading.current_thread().name == "late-contract-publisher":
                    waiting.set()
                    if not release.wait(20):
                        raise RuntimeError("concurrency test release timed out")
                return real_create(path)

            def publish_late() -> None:
                try:
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        code = v4.main(self._build_cli_argv(fixture, write=True))
                    if code != 0:
                        raise RuntimeError(f"late publisher returned {code}")
                    results.append(json.loads(stdout.getvalue()))
                except BaseException as exc:
                    failures.append(exc)

            with mock.patch.object(
                v4, "_create_contract_attempt", side_effect=delayed_create
            ):
                worker = threading.Thread(
                    target=publish_late,
                    name="late-contract-publisher",
                    daemon=True,
                )
                worker.start()
                self.assertTrue(waiting.wait(20))
                winner = v4.publish_contract(fixture.v4_path, fixture.document)
                winner_identity = (
                    fixture.v4_path.stat().st_dev,
                    fixture.v4_path.stat().st_ino,
                )
                release.set()
                worker.join(20)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(results), 1)
            self.assertEqual(
                results[0]["contract_sha256"], winner.contract_sha256
            )
            self.assertEqual(results[0]["status"], "already_present")
            self.assertEqual(results[0]["publication_state"], "absent")
            self.assertEqual(results[0]["writes_performed"], 0)
            self.assertEqual(results[0]["transaction_mutations"], 2)
            self.assertEqual(
                (fixture.v4_path.stat().st_dev, fixture.v4_path.stat().st_ino),
                winner_identity,
            )
            payload = v4._contract_document_bytes(fixture.document)
            attempt_path = v4.contract_attempt_path(fixture.v4_path, payload)
            self.assertFalse(os.path.lexists(attempt_path))

            v4._create_contract_attempt(attempt_path)
            real_remove = v4._remove_contract_attempt
            with mock.patch.object(
                v4,
                "_remove_contract_attempt",
                side_effect=self._kill_after(real_remove, "late-attempt-cleanup"),
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt, "late-attempt-cleanup"
                ):
                    v4.publish_contract(fixture.v4_path, fixture.document)
            replayed = v4.publish_contract(fixture.v4_path, fixture.document)
            self.assertEqual(replayed.contract_sha256, winner.contract_sha256)
            self.assertEqual(
                (fixture.v4_path.stat().st_dev, fixture.v4_path.stat().st_ino),
                winner_identity,
            )

    def test_committed_contract_rejects_nonempty_or_foreign_late_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            payload = v4._contract_document_bytes(fixture.document)
            attempt = v4.contract_attempt_path(fixture.v4_path, payload)
            v4._create_contract_attempt(attempt)
            (attempt / "foreign-entry").write_text("tamper", encoding="utf-8")
            with self.assertRaisesRegex(v4.PipelineStateError, "unauthorized entry"):
                v4.publish_contract(fixture.v4_path, fixture.document)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            foreign = v4.contract_attempt_path(fixture.v4_path, b"foreign-authority")
            foreign.mkdir()
            with self.assertRaisesRegex(
                v4.PipelineStateError, "differs from current authority"
            ):
                v4.publish_contract(fixture.v4_path, fixture.document)

    def test_contract_publication_repairs_partial_stage_and_partial_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)

            def partial_stage(path: Path, payload: bytes) -> None:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(
                    getattr(os, "O_BINARY", 0)
                )
                descriptor = os.open(path, flags, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload[:31])
                    stream.flush()
                    os.fsync(stream.fileno())
                raise KeyboardInterrupt("partial-stage")

            with mock.patch.object(
                v4, "_write_contract_stage_payload", side_effect=partial_stage
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "partial-stage"):
                    v4.publish_contract(fixture.v4_path, fixture.document)
            state = v4._inspect_contract_publication_state(
                fixture.v4_path, fixture.document
            )
            self.assertEqual(state.pending_state, "pre_stage_incomplete")

            def partial_proof(path: Path, payload: bytes) -> None:
                path.write_bytes(payload[:23])
                raise KeyboardInterrupt("partial-proof")

            with mock.patch.object(v4, "_write_contract_proof", side_effect=partial_proof):
                with self.assertRaisesRegex(KeyboardInterrupt, "partial-proof"):
                    v4.publish_contract(fixture.v4_path, fixture.document)
            state = v4._inspect_contract_publication_state(
                fixture.v4_path, fixture.document
            )
            self.assertEqual(state.pending_state, "pre_commit_proof_incomplete")
            recovered = v4.publish_contract(fixture.v4_path, fixture.document)
            self.assertEqual(recovered.contract_sha256, fixture.document["contract_sha256"])

    def test_contract_publication_rejects_sealed_tamper_foreign_links_and_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)
            payload = v4._contract_document_bytes(fixture.document)
            fixture.v4_path.parent.mkdir(parents=True, exist_ok=True)
            attempt = v4._create_contract_attempt(
                v4.contract_attempt_path(fixture.v4_path, payload)
            )
            v4._stage_contract_publication(fixture.v4_path, payload, attempt)
            staged = v4.contract_staged_path(fixture.v4_path, payload)
            staged.write_bytes(b"x" * len(payload))
            with self.assertRaisesRegex(v4.PipelineStateError, "staging bytes changed"):
                v4.publish_contract(fixture.v4_path, fixture.document)

        if hasattr(os, "link"):
            with tempfile.TemporaryDirectory() as tmp:
                fixture = Fixture(Path(tmp), publish=False)
                payload = v4._contract_document_bytes(fixture.document)
                fixture.v4_path.parent.mkdir(parents=True, exist_ok=True)
                attempt = v4._create_contract_attempt(
                    v4.contract_attempt_path(fixture.v4_path, payload)
                )
                v4._stage_contract_publication(fixture.v4_path, payload, attempt)
                staged = v4.contract_staged_path(fixture.v4_path, payload)
                foreign = fixture.root / "foreign-stage-link"
                try:
                    os.link(staged, foreign)
                except OSError:
                    pass
                else:
                    with self.assertRaisesRegex(
                        v4.PipelineStateError, "ambiguous hardlink ownership"
                    ):
                        v4.publish_contract(fixture.v4_path, fixture.document)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)
            payload = v4._contract_document_bytes(fixture.document)
            fixture.v4_path.parent.mkdir(parents=True, exist_ok=True)
            attempt_path = v4.contract_attempt_path(fixture.v4_path, payload)
            v4._create_contract_attempt(attempt_path)
            (attempt_path / "foreign-entry").write_text("tamper", encoding="utf-8")
            with self.assertRaisesRegex(v4.PipelineStateError, "unauthorized entry"):
                v4.publish_contract(fixture.v4_path, fixture.document)

    def test_contract_publication_rejects_proof_tamper_and_reparse_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)
            payload = v4._contract_document_bytes(fixture.document)
            fixture.v4_path.parent.mkdir(parents=True, exist_ok=True)
            attempt = v4._create_contract_attempt(
                v4.contract_attempt_path(fixture.v4_path, payload)
            )
            v4._stage_contract_publication(fixture.v4_path, payload, attempt)
            proof = v4.contract_proof_path(fixture.v4_path)
            proof.write_bytes(b"not-a-proof")
            with self.assertRaisesRegex(
                v4.PipelineStateError, "not a durable-write prefix"
            ):
                v4.publish_contract(fixture.v4_path, fixture.document)

        if hasattr(os, "link"):
            with tempfile.TemporaryDirectory() as tmp:
                fixture = Fixture(Path(tmp), publish=False)
                payload = v4._contract_document_bytes(fixture.document)
                fixture.v4_path.parent.mkdir(parents=True, exist_ok=True)
                attempt = v4._create_contract_attempt(
                    v4.contract_attempt_path(fixture.v4_path, payload)
                )
                v4._stage_contract_publication(fixture.v4_path, payload, attempt)
                staged = v4.contract_staged_path(fixture.v4_path, payload)
                identity = v4._contract_file_identity_at(staged, "test stage")
                assert identity is not None
                proof = v4.contract_proof_path(fixture.v4_path)
                proof.write_bytes(
                    v4._contract_proof_bytes(staged, fixture.v4_path, identity)
                )
                foreign = fixture.root / "foreign-proof-link"
                try:
                    os.link(proof, foreign)
                except OSError:
                    pass
                else:
                    with self.assertRaisesRegex(
                        v4.PipelineStateError, "ambiguous hardlink ownership"
                    ):
                        v4.publish_contract(fixture.v4_path, fixture.document)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), publish=False)
            payload = v4._contract_document_bytes(fixture.document)
            fixture.v4_path.parent.mkdir(parents=True, exist_ok=True)
            attempt = v4._create_contract_attempt(
                v4.contract_attempt_path(fixture.v4_path, payload)
            )
            staged = v4.contract_staged_path(fixture.v4_path, payload)
            target = fixture.root / "foreign-stage-target"
            target.write_bytes(payload)
            try:
                staged.symlink_to(target)
            except (OSError, NotImplementedError):
                pass
            else:
                with self.assertRaisesRegex(
                    v4.PipelineContractError, "link or reparse"
                ):
                    v4.publish_contract(fixture.v4_path, fixture.document)

    def test_contract_publication_never_replaces_another_committed_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            original = fixture.v4_path.read_bytes()
            identity = fixture.v4_path.stat().st_dev, fixture.v4_path.stat().st_ino
            changed = json.loads(json.dumps(fixture.document))
            changed["contract_sha256"] = "0" * 64
            with self.assertRaises(FileExistsError):
                v4.publish_contract(fixture.v4_path, changed)
            self.assertEqual(fixture.v4_path.read_bytes(), original)
            self.assertEqual(
                (fixture.v4_path.stat().st_dev, fixture.v4_path.stat().st_ino),
                identity,
            )

    def test_builder_is_deterministic_no_replace_and_shares_v3_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            again = v4.build_contract_document(
                base_contract_path=fixture.base_path,
                output_path=fixture.v4_path,
                stage1_workspace=fixture.workspace,
                declaration=fixture.declaration,
                confirmation=fixture.confirmation,
                receipt=fixture.receipt,
                optimization_runner=fixture.runner,
            )
            self.assertEqual(again, fixture.document)
            self.assertEqual(fixture.contract.lock_path, fixture.contract.base_contract.lock_path)
            self.assertEqual(
                fixture.contract.base_contract_binding.canonical_sha256,
                v3._canonical_sha256(json.loads(fixture.base_path.read_text(encoding="utf-8"))),
            )
            self.assertEqual(
                legacy_optimizer.SOURCE_CONTRACT_FILES,
                v4._frozen_legacy_optimizer_declared_source_filenames(),
            )
            immutable = {
                (item.path, item.sha256) for item in fixture.contract.immutable_inputs
            }
            for module_name, filename in v4.LEGACY_OPTIMIZATION_SOURCE_MODULES:
                key = v4.LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[module_name]
                pin = fixture.contract.source_pins[key]
                self.assertEqual(pin.path, fixture.root / filename)
                self.assertEqual(pin.sha256, sha256(pin.path))
                self.assertIn((pin.path, pin.sha256), immutable)
            committed_stat = os.stat(fixture.v4_path, follow_symlinks=False)
            committed_identity = committed_stat.st_dev, committed_stat.st_ino
            replayed = v4.publish_contract(fixture.v4_path, fixture.document)
            self.assertEqual(replayed.contract_sha256, fixture.contract.contract_sha256)
            self.assertEqual(
                (
                    os.stat(fixture.v4_path, follow_symlinks=False).st_dev,
                    os.stat(fixture.v4_path, follow_symlinks=False).st_ino,
                ),
                committed_identity,
            )

            dry_output = fixture.root / "dry-v4.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = v4.main(
                    [
                        "--build-base-contract",
                        str(fixture.base_path),
                        "--output-contract",
                        str(dry_output),
                        "--stage1-workspace",
                        str(fixture.root / "dry-official"),
                        "--declaration",
                        str(fixture.root / "dry-authority" / "declaration.json"),
                        "--confirmation",
                        str(fixture.root / "dry-authority" / "confirmation.json"),
                        "--receipt",
                        str(fixture.root / "dry-authority" / "receipt.json"),
                        "--optimization-runner",
                        str(fixture.runner),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["writes_performed"], 0)
            self.assertFalse(dry_output.exists())
            self.assertFalse(
                tuple(
                    fixture.root.glob(
                        f".{dry_output.name}{v4.CONTRACT_ATTEMPT_MARKER}*"
                    )
                )
            )
            self.assertFalse(
                tuple(
                    fixture.root.glob(
                        f".{dry_output.name}.*{v4.CONTRACT_STAGED_SUFFIX}"
                    )
                )
            )
            self.assertFalse(os.path.lexists(v4.contract_proof_path(dry_output)))

    def test_missing_legacy_optimizer_pin_is_a_contract_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            document = json.loads(json.dumps(fixture.document))
            key = v4.LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[
                "submit_ipmsm_scheduler_job"
            ]
            removed = document["pipeline"]["source_pins"].pop(key)
            document["pipeline"]["immutable_inputs"] = [
                item
                for item in document["pipeline"]["immutable_inputs"]
                if item["path"] != removed["path"]
            ]
            unsigned = {
                "schema_version": document["schema_version"],
                "pipeline": document["pipeline"],
            }
            document["contract_sha256"] = v3._canonical_sha256(unsigned)
            fixture.v4_path.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(v4.PipelineContractError, "source_pins"):
                v4.load_contract(fixture.v4_path)

    def test_all_v4_children_use_base_python_and_exact_wrapper_transformation(self) -> None:
        argv_locations = (
            ("stage1_official", "publisher_argv"),
            ("optimization_confirmation", "authorizer_argv"),
            ("optimization", "wrapper_argv_template"),
        )
        for section, field in argv_locations:
            with self.subTest(section=section), tempfile.TemporaryDirectory() as tmp:
                fixture = Fixture(Path(tmp))
                document = json.loads(json.dumps(fixture.document))
                document["pipeline"][section][field][0] = str(
                    fixture.root / "attacker" / "evil-python.exe"
                )
                unsigned = {
                    "schema_version": document["schema_version"],
                    "pipeline": document["pipeline"],
                }
                document["contract_sha256"] = v3._canonical_sha256(unsigned)
                fixture.v4_path.write_text(
                    json.dumps(document, indent=2) + "\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    v4.PipelineContractError, "deterministic argv template"
                ):
                    v4.load_contract(fixture.v4_path)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            document = json.loads(json.dumps(fixture.document))
            document["pipeline"]["optimization"]["wrapper_argv_template"].append(
                "--help"
            )
            unsigned = {
                "schema_version": document["schema_version"],
                "pipeline": document["pipeline"],
            }
            document["contract_sha256"] = v3._canonical_sha256(unsigned)
            fixture.v4_path.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                v4.PipelineContractError, "deterministic argv template"
            ):
                v4.load_contract(fixture.v4_path)

    def test_legacy_optimizer_required_argument_manifest_is_complete(self) -> None:
        complete = [
            "--stage2-decision",
            v3.UPSTREAM_PLACEHOLDER,
            "--optimization-spec",
            "spec.json",
            "--beta-summary",
            "beta-summary.json",
            "--beta-case-plan",
            "beta-plan.csv",
            "--beta-results",
            "beta-results.csv",
            "--beta-calibration-manifest",
            "beta-manifest.json",
            "--output-dir",
            "optimization-output",
            "--checkpoint-dir",
            "optimization-checkpoints",
            "--decision-output",
            "optimization.json",
            "--project",
            "PYAEDT_MOTOR_IPMSM_V2",
        ]
        v4._validate_legacy_optimizer_arguments(complete)
        missing = complete[:]
        index = missing.index("--beta-summary")
        del missing[index : index + 2]
        with self.assertRaisesRegex(v4.PipelineContractError, "lack required flags"):
            v4._validate_legacy_optimizer_arguments(missing)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            base = json.loads(fixture.base_path.read_text(encoding="utf-8"))
            argv = base["pipeline"]["optimization"]["argv_template"]
            index = argv.index("--beta-summary")
            del argv[index : index + 2]
            unsigned = {
                "schema_version": base["schema_version"],
                "pipeline": base["pipeline"],
            }
            base["contract_sha256"] = v3._canonical_sha256(unsigned)
            fixture.base_path.write_text(
                json.dumps(base, indent=2) + "\n", encoding="utf-8"
            )
            output = fixture.root / "invalid-v4.json"
            with self.assertRaisesRegex(
                v4.PipelineContractError, "lack required flags"
            ):
                v4.main(
                    [
                        "--build-base-contract",
                        str(fixture.base_path),
                        "--output-contract",
                        str(output),
                        "--stage1-workspace",
                        str(fixture.root / "invalid-official"),
                        "--declaration",
                        str(fixture.root / "invalid-auth" / "declaration.json"),
                        "--confirmation",
                        str(fixture.root / "invalid-auth" / "confirmation.json"),
                        "--receipt",
                        str(fixture.root / "invalid-auth" / "receipt.json"),
                        "--optimization-runner",
                        str(fixture.runner),
                    ]
                )
            self.assertFalse(output.exists())

    def test_in_memory_source_manifest_shrink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            with mock.patch.object(
                v4,
                "LEGACY_OPTIMIZATION_SOURCE_MODULES",
                v4.LEGACY_OPTIMIZATION_SOURCE_MODULES[:-1],
            ), mock.patch.object(
                v4,
                "LEGACY_OPTIMIZATION_SOURCE_FILENAMES",
                v4.LEGACY_OPTIMIZATION_SOURCE_FILENAMES[:-1],
            ):
                with self.assertRaisesRegex(
                    v4.PipelineContractError, "manifest changed in memory"
                ):
                    v4.audit_contract(fixture.contract)

    def test_hashes_links_and_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.immutable.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(v4.PipelineStateError, "immutable input"):
                v4.audit_contract(fixture.contract)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            legacy_source = fixture.contract.source_pins[
                v4.LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[
                    "submit_ipmsm_scheduler_job"
                ]
            ].path
            legacy_source.write_text("# changed after v4 publication\n", encoding="utf-8")
            with self.assertRaisesRegex(v4.PipelineStateError, "v4 immutable input"):
                v4.audit_contract(fixture.contract)

        if hasattr(os, "link"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                original = root / "base-source.json"
                original.write_text("{}\n", encoding="utf-8")
                alias = root / "base.json"
                os.link(original, alias)
                with self.assertRaisesRegex(v4.PipelineContractError, "hard link"):
                    v4._strict_document(alias, "hardlinked base")

    def test_reparse_component_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "linked" / "file.json"
            ordinary = os.stat_result((0o40755, 1, 1, 1, 0, 0, 0, 0, 0, 0))
            reparse = types.SimpleNamespace(
                st_mode=ordinary.st_mode,
                st_file_attributes=v4.FILE_ATTRIBUTE_REPARSE_POINT,
            )
            with mock.patch.object(v4.os, "lstat", return_value=reparse):
                with self.assertRaisesRegex(v4.PipelineContractError, "reparse"):
                    v4._reject_link_components(path, "test path")

    def test_symlinked_authority_file_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            link = root / "link.json"
            target.write_text("{}\n", encoding="utf-8")
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaisesRegex(v4.PipelineContractError, "link or reparse"):
                v4._strict_document(link, "linked authority")

    def test_kill_states_ignore_all_partial_legacy_outputs_until_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            self.assertEqual(v4.inspect_pipeline(fixture.contract).next_action, "run_stage1_campaign")
            fixture.campaign()
            partials = (
                fixture.root / "legacy-validation.csv",
                fixture.root / "legacy-models" / "metadata.json",
                fixture.root / "legacy-r2.csv",
                fixture.workspace / "attempts" / "dead" / "ready.json",
            )
            for path in partials:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("partial\n", encoding="utf-8")
                self.assertEqual(
                    v4.inspect_pipeline(fixture.contract).next_action,
                    "publish_stage1_official",
                )

    def test_pending_completion_hardlink_routes_to_publication_recovery(self) -> None:
        if not hasattr(os, "link"):
            self.skipTest("hardlink creation is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.campaign()
            fixture.workspace.mkdir(parents=True)
            staged = fixture.workspace / ".completion.json.recovery-stage"
            staged.write_text("pending completion\n", encoding="utf-8")
            try:
                os.link(staged, fixture.workspace / "completion.json")
            except OSError as exc:
                self.skipTest(f"hardlink creation is unavailable: {exc}")
            official = mock.Mock()
            with mock.patch.object(
                v4,
                "inspect_pending_official_publications",
                return_value=("completion.json",),
            ) as pending, mock.patch.object(
                v4, "audit_official_stage1", official
            ):
                snapshot = v4.inspect_pipeline(fixture.contract)
            self.assertEqual(snapshot.next_action, "publish_stage1_official")
            self.assertEqual(snapshot.branch, "stage1_official")
            self.assertEqual(
                snapshot.detail, {"pending_publications": ["completion.json"]}
            )
            pending.assert_called_once_with(fixture.contract)
            official.assert_not_called()

    def test_stage2_uses_only_completion_resolved_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.campaign()
            official = fixture.official()
            argv = v4.official_stage2_argv(fixture.contract, official)
            rendered = "\n".join(argv)
            self.assertIn(str(official.stage1.validation), rendered)
            self.assertIn(str(official.stage1.metadata), rendered)
            self.assertIn(str(official.stage1.r2), rendered)
            self.assertNotIn("legacy-validation.csv", rendered)
            self.assertNotIn("legacy-models/metadata.json", rendered)
            self.assertNotIn("legacy-r2.csv", rendered)

    def test_missing_confirmation_waits_exit_zero_without_lock_or_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.campaign()
            official = fixture.official()
            fixture.stage2_complete(official)
            stdout = io.StringIO()
            with (
                mock.patch.object(v4, "audit_official_stage1", return_value=official),
                mock.patch.object(v3, "run_child") as child,
                contextlib.redirect_stdout(stdout),
            ):
                code = v4.main(["--contract", str(fixture.v4_path), "--execute"])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(report["next_action"], "wait_optimization_confirmation")
            self.assertEqual(report["writes_performed"], 0)
            self.assertFalse((fixture.root / "pipeline.lock").exists())
            child.assert_not_called()

    def test_confirmation_then_receipt_state_machine_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.campaign()
            official = fixture.official()
            fixture.stage2_complete(official)
            fixture.declaration.parent.mkdir(parents=True, exist_ok=True)
            fixture.declaration.write_text("{}\n", encoding="utf-8")
            fixture.confirmation.write_text("{}\n", encoding="utf-8")
            confirmation_module = types.SimpleNamespace(audit_confirmation=lambda *_: object())
            with (
                mock.patch.object(v4, "audit_official_stage1", return_value=official),
                mock.patch.object(v4, "_loaded_module", return_value=confirmation_module),
            ):
                snapshot = v4.inspect_pipeline(fixture.contract)
            self.assertEqual(snapshot.next_action, "commit_optimization_authorization")

            fixture.receipt.write_text("{}\n", encoding="utf-8")
            auth = v4.AuditedAuthorization(object(), {}, {"binding": "exact"})
            with (
                mock.patch.object(v4, "audit_official_stage1", return_value=official),
                mock.patch.object(v4, "audit_authorization", return_value=auth),
            ):
                snapshot = v4.inspect_pipeline(fixture.contract)
            self.assertEqual(snapshot.next_action, "run_optimization_fresh")

    def test_authorization_record_consumes_the_exact_authorizer_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            for path in (fixture.declaration, fixture.confirmation, fixture.receipt):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            audit = authorizer.AuthorizationAudit(
                receipt_path=fixture.receipt,
                receipt_raw_sha256=sha256(fixture.receipt),
                receipt_sha256="1" * 64,
                confirmation_path=fixture.confirmation,
                confirmation_raw_sha256=sha256(fixture.confirmation),
                confirmation_canonical_sha256="2" * 64,
                confirmation_sha256="3" * 64,
                declaration_path=fixture.declaration,
                declaration_raw_sha256=sha256(fixture.declaration),
                declaration_canonical_sha256="4" * 64,
                contract_path=fixture.v4_path,
                contract_raw_sha256=fixture.contract.source_sha256,
                contract_canonical_sha256=fixture.contract.canonical_sha256,
                contract_sha256=fixture.contract.contract_sha256,
                base_contract_path=fixture.base_path,
                base_contract_raw_sha256=fixture.contract.base_contract_binding.sha256,
                base_contract_canonical_sha256=(
                    fixture.contract.base_contract_binding.canonical_sha256 or ""
                ),
                base_contract_sha256=(
                    fixture.contract.base_contract_binding.contract_sha256 or ""
                ),
                optimization_spec_path=fixture.root / "spec.json",
                optimization_spec_raw_sha256=sha256(fixture.root / "spec.json"),
                optimization_spec_canonical_sha256="5" * 64,
                optimization_spec_schema_version=1,
                optimization_implementation_path=fixture.root
                / "continue_ipmsm_v2_optimization.py",
                optimization_implementation_sha256="6" * 64,
                confirmation_helper_path=fixture.contract.source_pins[
                    "confirmation_helper"
                ].path,
                confirmation_helper_sha256=fixture.contract.source_pins[
                    "confirmation_helper"
                ].sha256,
                confirmed_by="operator",
                confirmed_at_utc="2026-07-12T00:00:00Z",
                evidence_reference="ticket",
                attestation_kind="filesystem_acl_self_attestation",
                duty_basis="equal_weighted_operating_points",
                authorization_effective_at_utc="2026-07-12T00:00:00Z",
            )
            record = v4.authorization_record(fixture.contract, audit)
            self.assertEqual(
                record["schema_version"],
                "ipmsm-v2-optimization-authorization-binding-v1",
            )
            self.assertEqual(record["audit"], audit.as_mapping())
            self.assertRegex(record["binding_sha256"], r"^[0-9a-f]{64}$")

    def test_optimization_resume_requires_exact_record_and_reaudits_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.campaign()
            official = fixture.official()
            fixture.stage2_complete(official)
            for path in (fixture.declaration, fixture.confirmation, fixture.receipt):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            auth = v4.AuditedAuthorization(object(), {}, {"binding": "exact"})
            fixture.decision(
                fixture.root / "optimization.json",
                v3.OPTIMIZATION_DECISION_SCHEMA_VERSION,
                "optimization_started",
                {"authorization": {"binding": "wrong"}},
            )
            with (
                mock.patch.object(v4, "audit_official_stage1", return_value=official),
                mock.patch.object(v4, "audit_authorization", return_value=auth),
            ):
                with self.assertRaisesRegex(v4.PipelineStateError, "exact v4 authorization"):
                    v4.inspect_pipeline(fixture.contract)

            fixture.decision(
                fixture.root / "optimization.json",
                v3.OPTIMIZATION_DECISION_SCHEMA_VERSION,
                "optimization_started",
                {"authorization": auth.record},
            )
            events: list[str] = []

            def audit_now(_contract: v4.V4Contract) -> v4.AuditedAuthorization:
                events.append("audit")
                return auth

            def run_now(*_args: object, **_kwargs: object) -> None:
                events.append("run")

            with (
                mock.patch.object(v4, "audit_authorization", side_effect=audit_now),
                mock.patch.object(v3, "_run_dry_then_execute", side_effect=run_now),
                mock.patch.object(v3, "audit_decision", return_value={
                    "execution_contract": {"authorization": auth.record}
                }),
            ):
                v4.execute_action(
                    fixture.contract,
                    v4.PipelineSnapshot(
                        "run_optimization_resume",
                        "stage2_complete",
                        upstream_decision=fixture.root / "stage2.json",
                    ),
                )
            self.assertEqual(events[:2], ["audit", "run"])


if __name__ == "__main__":
    unittest.main()
