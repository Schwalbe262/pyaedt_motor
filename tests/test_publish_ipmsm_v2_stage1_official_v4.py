from __future__ import annotations

import json
import io
import os
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout

import atomic_publish
import continue_ipmsm_v2_stage2 as stage2
import publish_ipmsm_v2_stage1_official_v4 as official
import train_ipmsm_lightgbm as trainer


class Stage1OfficialV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workdir = self.base / "repo"
        self.workdir.mkdir()
        self.result = self.workdir / "stage1.csv"
        self.result.write_text("case_id,status\na,ok\n", encoding="utf-8")
        self.contract_source = self.workdir / "base-contract.json"
        self.contract_source.write_text("{}\n", encoding="utf-8")
        self.pipeline_contract_source = self.workdir / "v4-contract.json"
        self.pipeline_contract_source.write_text("{}\n", encoding="utf-8")
        validation_source = self.workdir / "validate_ipmsm_v2_dataset.py"
        training_source = self.workdir / "train_ipmsm_lightgbm.py"
        validation_source.write_text("# validator\n", encoding="utf-8")
        training_source.write_text("# trainer\n", encoding="utf-8")
        stage1_contract = SimpleNamespace(
            result=self.result,
            case_plan=self.workdir / "cases.csv",
            expected_rows=700,
            expected_groups=112,
            expected_repeats=28,
            r2_threshold=0.95,
            ensemble_size=5,
            conformal_coverage=0.95,
            validation_argv=(
                sys.executable,
                str(validation_source),
                "--data",
                str(self.result),
                "--summary",
                str(self.workdir / "old-validation.csv"),
            ),
            training_argv=(
                sys.executable,
                str(training_source),
                "--v2",
                "--data",
                str(self.result),
                "--model-dir",
                str(self.workdir / "old-models"),
                "--verification-output",
                str(self.workdir / "old-r2.csv"),
                "--r2-threshold",
                "0.95",
                "--fail-on-threshold",
            ),
        )
        contract = SimpleNamespace(
            source=self.contract_source,
            workdir=self.workdir,
            stage1=stage1_contract,
            immutable_inputs=(),
        )
        self.context = official._OfficialContext(
            pipeline_contract=SimpleNamespace(
                source=self.pipeline_contract_source,
                immutable_inputs=(),
            ),
            pipeline_contract_binding={
                "canonical_sha256": "4" * 64,
                "contract_sha256": "5" * 64,
                "path": str(self.pipeline_contract_source),
                "raw_sha256": "6" * 64,
                "schema_version": "ipmsm-v2-pipeline-contract-v4",
                "size": 3,
            },
            contract=contract,
            contract_binding={
                "canonical_sha256": "0" * 64,
                "contract_sha256": "2" * 64,
                "path": str(self.contract_source),
                "raw_sha256": "1" * 64,
                "schema_version": "ipmsm-v2-pipeline-contract-v1",
                "size": 3,
            },
            result_binding={
                "path": str(self.result),
                "sha256": official._sha256_bytes(self.result.read_bytes()),
                "size": self.result.stat().st_size,
            },
            sources={
                role: {
                    "path": str(self.workdir / f"{role}.py"),
                    "sha256": character * 64,
                    "size": 10,
                }
                for role, character in (
                    ("atomic_publisher", "e"),
                    ("contract_loader", "f"),
                    ("gate_evaluator", "9"),
                    ("pipeline_contract_loader", "a"),
                    ("publisher", "3"),
                    ("trainer", "8"),
                    ("validator", "7"),
                    ("verification_helper", "b"),
                )
            },
        )

    def _mutated_context(self, kind: str) -> official._OfficialContext:
        if kind == "v4_contract":
            binding = dict(self.context.pipeline_contract_binding)
            binding["raw_sha256"] = "a" * 64
            return replace(self.context, pipeline_contract_binding=binding)
        if kind == "base_contract":
            binding = dict(self.context.contract_binding)
            binding["raw_sha256"] = "b" * 64
            return replace(self.context, contract_binding=binding)
        if kind == "stage1_result":
            binding = dict(self.context.result_binding)
            binding["sha256"] = "c" * 64
            return replace(self.context, result_binding=binding)
        sources = {name: dict(value) for name, value in self.context.sources.items()}
        sources[kind]["sha256"] = "d" * 64
        return replace(self.context, sources=sources)

    def _gate(self, passed: bool) -> stage2.GateResult:
        failures = () if passed else (stage2.PRIMARY_TARGETS[0],)
        primary = {
            target: (0.96 if target not in failures else 0.80)
            for target in stage2.PRIMARY_TARGETS
        }
        return stage2.GateResult(
            decision="skip_stage2" if passed else "run_stage2",
            validation={
                "failures": 0,
                "issues": "",
                "ok_rows": 700,
                "repeat_pairs": 28,
                "rows": 700,
                "status": "pass",
                "unique_case_ids": 700,
                "unique_geometry_groups": 112,
            },
            primary_test_r2=primary,
            primary_failures=failures,
            voltage_test_r2=0.96,
            voltage_failed=False,
            fingerprints={"input_dataset_schema_version": "ipmsm_v2"},
        )

    @staticmethod
    def _trainer_record(path: Path, *, members: int | None = None) -> dict[str, object]:
        record: dict[str, object] = {
            "path": str(path),
            "sha256": official._sha256_bytes(path.read_bytes()),
        }
        if members is not None:
            record["ensemble_members"] = members
        return record

    def _make_ready(
        self,
        root: Path,
        *,
        attempt_id: str = "a" * 32,
        passed: bool = True,
    ) -> official._ReadyAudit:
        attempt_dir = root / official.ATTEMPTS_NAME / attempt_id
        model_dir = attempt_dir / "models"
        model_dir.mkdir(parents=True)
        attempt_document = official._expected_attempt_document(
            self.context, root, attempt_id
        )
        attempt_bytes = official._canonical_json_bytes(attempt_document)
        (attempt_dir / "attempt.json").write_bytes(attempt_bytes)
        (attempt_dir / "validation.csv").write_text(
            "rows,ok_rows,unique_case_ids,unique_geometry_groups,repeat_pairs,failures,status,issues\n"
            "700,700,700,112,28,0,pass,\n",
            encoding="utf-8",
        )
        (attempt_dir / "r2.csv").write_text(
            "target,split,R2,R2_threshold,status\nx,test,0.96,0.95,pass\n",
            encoding="utf-8",
        )

        model_paths: dict[str, str] = {}
        model_records: dict[str, dict[str, object]] = {}
        targets = (*trainer.V2_PRIMITIVE_OUTPUT_COLUMNS, *trainer.V2_AUXILIARY_OUTPUT_COLUMNS)
        for index, target in enumerate(targets):
            path = model_dir / f"{trainer.safe_model_name(target)}_lgbm.pkl"
            path.write_bytes(f"model-{index}".encode("ascii"))
            model_paths[target] = str(path)
            model_records[target] = self._trainer_record(path, members=5)

        gate = self._gate(passed)
        metrics = model_dir / "metrics.csv"
        auxiliary = model_dir / "auxiliary_metrics.csv"
        metrics.write_text(
            "target,split,R2\n"
            + "".join(
                f"{target},test,{value}\n"
                for target, value in gate.primary_test_r2.items()
            ),
            encoding="utf-8",
        )
        auxiliary.write_text(
            f"target,split,R2\nvoltage,test,{gate.voltage_test_r2}\n",
            encoding="utf-8",
        )
        metadata = {
            "auxiliary_metrics_path": str(auxiliary),
            "auxiliary_model_paths": {
                target: model_paths[target]
                for target in trainer.V2_AUXILIARY_OUTPUT_COLUMNS
            },
            "data_paths": [str(self.result)],
            "metrics_path": str(metrics),
            "model_artifacts": model_records,
            "model_paths": model_paths,
            "training_artifacts": {
                "auxiliary_metrics": self._trainer_record(auxiliary),
                "metrics": self._trainer_record(metrics),
            },
            "tuning_trials_path": "",
        }
        (model_dir / "metadata.json").write_text(
            json.dumps(metadata, sort_keys=True), encoding="utf-8"
        )
        exit_code = 0 if passed else 1
        with mock.patch.object(stage2, "evaluate_gate", return_value=gate):
            artifacts, gate_summary, gate_passed = official._audit_outputs(
                self.context,
                root,
                attempt_dir,
                trainer_exit_code=exit_code,
            )
        ready_payload = official._ready_payload(
            self.context,
            root,
            attempt_id,
            official._sha256_bytes(attempt_bytes),
            artifacts,
            gate_summary,
            gate_passed,
            exit_code,
        )
        ready_path = attempt_dir / "ready.json"
        ready_path.write_bytes(
            official._canonical_json_bytes(
                official._envelope(official.READY_SCHEMA_VERSION, ready_payload)
            )
        )
        with (
            mock.patch.object(stage2, "evaluate_gate", return_value=gate),
            mock.patch.object(official, "_replay_context"),
        ):
            return official._audit_ready(self.context, root, ready_path)

    def _write_completion(
        self, root: Path, ready: official._ReadyAudit
    ) -> Path:
        completion = root / official.COMPLETION_NAME
        completion.write_bytes(
            official._canonical_json_bytes(
                official._envelope(
                    official.COMPLETION_SCHEMA_VERSION,
                    official._completion_payload(self.context, ready),
                )
            )
        )
        return completion

    def _seed_pending_publication(
        self,
        destination: Path,
        payload: bytes,
        state: str,
    ) -> tuple[Path, Path, atomic_publish.FileIdentity]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = destination.with_name(f".{destination.name}.{'d' * 32}.tmp")
        staged.write_bytes(payload)
        identity = atomic_publish.FileIdentity.from_path(staged)
        proof = official._publication_proof_path(destination)
        atomic_publish._write_proof_exclusive(
            proof,
            source=staged,
            destination=destination,
            identity=identity,
        )
        if state in {"after_hardlink", "after_staging_unlink"}:
            try:
                os.link(staged, destination)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
        if state == "after_staging_unlink":
            staged.unlink()
        return staged, proof, identity

    def _seed_durable_publication_attempt(
        self,
        root: Path,
        destination: Path,
        payload: bytes,
    ) -> tuple[
        official._PublicationAttempt,
        Path,
        atomic_publish.FileIdentity,
        bytes,
    ]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        attempt = official._create_publication_attempt(root, destination, payload)
        staged, sealed = official._stage_bytes(
            root, destination, payload, attempt
        )
        identity = atomic_publish.FileIdentity.from_path(staged)
        proof_payload = official._proof_json_bytes(
            {
                "schema_version": atomic_publish.PROOF_SCHEMA_VERSION,
                "source": str(staged),
                "destination": str(destination),
                "identity": identity.as_mapping(),
            }
        )
        return sealed, staged, identity, proof_payload

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_values(self) -> None:
        with self.assertRaisesRegex(official.OfficialStage1Error, "strict JSON"):
            official._decode_json(b'{"a":1,"a":2}', "duplicate", canonical=False)
        with self.assertRaisesRegex(official.OfficialStage1Error, "strict JSON"):
            official._decode_json(b'{"a":NaN}', "nan", canonical=False)

    def test_relative_artifact_paths_reject_traversal_absolute_and_backslash(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        for value in ("../escape", "/absolute", "models\\metadata.json", "C:escape"):
            with self.subTest(value=value):
                with self.assertRaises(official.OfficialStage1Error):
                    official._resolve_relative(root, value, "artifact")

    def test_workspace_rejects_hardlinks(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        original = root / "original"
        alias = root / "alias"
        original.write_bytes(b"same inode")
        try:
            os.link(original, alias)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaisesRegex(official.OfficialStage1Error, "hardlink"):
            official._secure_workspace(root, create=False)

    def test_authoritative_publications_recover_all_three_kill_windows(self) -> None:
        states = ("before_commit", "after_hardlink", "after_staging_unlink")
        destinations = (
            ("attempt", lambda root: root / "attempts" / ("a" * 32) / "attempt.json"),
            ("ready", lambda root: root / "attempts" / ("b" * 32) / "ready.json"),
            ("completion", lambda root: root / official.COMPLETION_NAME),
        )
        for destination_name, destination_factory in destinations:
            for state in states:
                with self.subTest(destination=destination_name, state=state):
                    root = self.base / f"recover-{destination_name}-{state}"
                    root.mkdir()
                    destination = destination_factory(root)
                    payload = f"{destination_name}:{state}\n".encode("ascii")
                    staged, proof_path, committed_identity = self._seed_pending_publication(
                        destination, payload, state
                    )
                    proofs = official._scan_workspace(root)
                    self.assertEqual(len(proofs), 1)
                    official._recover_publication(root, proofs[0], payload)
                    self.assertEqual(destination.read_bytes(), payload)
                    self.assertEqual(destination.stat().st_nlink, 1)
                    self.assertFalse(staged.exists())
                    self.assertFalse(proof_path.exists())
                    recovered_identity = atomic_publish.FileIdentity.from_path(destination)
                    self.assertEqual(recovered_identity, committed_identity)

    def test_partial_proof_repair_covers_all_authoritative_artifacts(self) -> None:
        destinations = (
            ("attempt", lambda root: root / "attempts" / ("1" * 32) / "attempt.json"),
            ("ready", lambda root: root / "attempts" / ("2" * 32) / "ready.json"),
            ("completion", lambda root: root / official.COMPLETION_NAME),
        )
        for name, destination_factory in destinations:
            with self.subTest(destination=name):
                root = self.base / f"partial-proof-{name}"
                root.mkdir()
                destination = destination_factory(root)
                payload = f"sealed-{name}\n".encode("ascii")
                attempt, staged, identity, proof_payload = (
                    self._seed_durable_publication_attempt(
                        root, destination, payload
                    )
                )
                proof_path = official._publication_proof_path(destination)
                proof_path.write_bytes(proof_payload[:18])
                state = official._scan_workspace_state(root)
                self.assertEqual(len(state.incomplete_proofs), 1)
                self.assertEqual(state.incomplete_proofs[0].attempt, attempt)

                official._recover_publication_transaction(
                    root, destination, payload, create=False
                )
                self.assertEqual(destination.read_bytes(), payload)
                self.assertEqual(
                    atomic_publish.FileIdentity.from_path(destination), identity
                )
                self.assertFalse(staged.exists())
                self.assertFalse(proof_path.exists())
                self.assertFalse(attempt.path.exists())

    def test_partial_proof_repair_survives_repeated_hard_kills(self) -> None:
        class SimulatedHardKill(BaseException):
            pass

        root = self.base / "partial-proof-repeat-kill"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"repeated partial proof\n"
        attempt, staged, identity, proof_payload = self._seed_durable_publication_attempt(
            root, destination, payload
        )
        proof_path = official._publication_proof_path(destination)
        proof_path.write_bytes(proof_payload[:18])
        real_repair = official._recover_incomplete_publication_proof

        def repair_then_kill(
            workspace: Path,
            incomplete: official._IncompletePublicationProof,
            expected_payload: bytes,
        ) -> None:
            real_repair(workspace, incomplete, expected_payload)
            raise SimulatedHardKill()

        with mock.patch.object(
            official,
            "_recover_incomplete_publication_proof",
            side_effect=repair_then_kill,
        ):
            with self.assertRaises(SimulatedHardKill):
                official._recover_publication_transaction(
                    root, destination, payload, create=False
                )
        state = official._scan_workspace_state(root)
        self.assertEqual(state.attempts, (attempt,))
        self.assertEqual(state.incomplete_proofs, ())
        self.assertTrue(staged.exists())
        self.assertTrue(attempt.stage_ready)

        def partial_proof_then_kill(
            source: Path, output: Path, *, proof_path: Path
        ) -> None:
            Path(proof_path).write_bytes(proof_payload[:18])
            raise SimulatedHardKill()

        with mock.patch.object(
            official, "publish_no_replace", side_effect=partial_proof_then_kill
        ):
            with self.assertRaises(SimulatedHardKill):
                official._recover_publication_transaction(
                    root, destination, payload, create=False
                )
        self.assertEqual(
            len(official._scan_workspace_state(root).incomplete_proofs), 1
        )
        official._recover_publication_transaction(
            root, destination, payload, create=False
        )
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(atomic_publish.FileIdentity.from_path(destination), identity)
        self.assertFalse(staged.exists())
        self.assertFalse(proof_path.exists())
        self.assertFalse(attempt.path.exists())

    def test_unsealed_partial_stage_is_rewritten_but_sealed_tamper_is_rejected(self) -> None:
        root = self.base / "stage-ready-boundary"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"authoritative stage bytes\n"
        attempt = official._create_publication_attempt(root, destination, payload)
        staged = official._deterministic_staged_path(destination, payload)
        staged.write_bytes(payload[:7])
        official._recover_publication_transaction(
            root, destination, payload, create=False
        )
        self.assertEqual(destination.read_bytes(), payload)

        second_root = self.base / "sealed-stage-tamper"
        second_root.mkdir()
        second_destination = second_root / official.COMPLETION_NAME
        sealed, sealed_stage, _, _ = self._seed_durable_publication_attempt(
            second_root, second_destination, payload
        )
        sealed_stage.write_bytes(b"X" * len(payload))
        with self.assertRaisesRegex(
            official.OfficialStage1Error, "sealed publication staging bytes"
        ):
            official._recover_publication_transaction(
                second_root,
                second_destination,
                payload,
                create=False,
            )
        self.assertTrue(sealed.path.exists())
        self.assertTrue((sealed.path / official.PUBLISH_STAGE_READY_NAME).is_dir())
        self.assertTrue(sealed_stage.exists())
        self.assertFalse(second_destination.exists())

    def test_partial_proof_tamper_and_extra_stage_fail_closed(self) -> None:
        root = self.base / "partial-proof-tamper"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"sealed proof authority\n"
        attempt, staged, _, proof_payload = self._seed_durable_publication_attempt(
            root, destination, payload
        )
        proof_path = official._publication_proof_path(destination)
        proof_path.write_bytes(b"not-an-atomic-proof-prefix")
        with self.assertRaisesRegex(
            official.OfficialStage1Error, "durable-write prefix"
        ):
            official._scan_workspace_state(root)
        proof_path.unlink()
        proof_path.write_bytes(b"{}\n")
        with self.assertRaises(official.OfficialStage1Error):
            official._scan_workspace_state(root)
        proof_path.unlink()
        proof_path.write_text(
            json.dumps(json.loads(proof_payload.decode("utf-8"))),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            official.OfficialStage1Error, "not canonical"
        ):
            official._scan_workspace_state(root)
        proof_path.unlink()
        extra_stage = destination.with_name(
            f".{destination.name}.{'f' * 32}.tmp"
        )
        extra_stage.write_bytes(payload)
        with self.assertRaisesRegex(
            official.OfficialStage1Error, "multiple publication staging"
        ):
            official._scan_workspace_state(root)
        self.assertTrue(attempt.path.exists())
        self.assertTrue(staged.exists())
        self.assertTrue(extra_stage.exists())

    def test_unsealed_stage_foreign_hardlink_fails_closed(self) -> None:
        root = self.base / "unsealed-stage-hardlink"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"unsealed stage\n"
        attempt = official._create_publication_attempt(root, destination, payload)
        staged = official._deterministic_staged_path(destination, payload)
        staged.write_bytes(payload[:5])
        foreign = self.base / "unsealed-stage-foreign"
        try:
            os.link(staged, foreign)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaisesRegex(official.OfficialStage1Error, "hardlink"):
            official._scan_workspace_state(root)
        self.assertTrue(attempt.path.exists())
        self.assertTrue(staged.exists())
        self.assertTrue(foreign.exists())

    def test_unsealed_stage_reparse_fails_closed(self) -> None:
        root = self.base / "unsealed-stage-reparse"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"unsealed reparse\n"
        attempt = official._create_publication_attempt(root, destination, payload)
        staged = official._deterministic_staged_path(destination, payload)
        target = self.base / "unsealed-stage-reparse-target"
        target.write_bytes(payload)
        try:
            staged.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(
            official.OfficialStage1Error, "symlink/reparse"
        ):
            official._scan_workspace_state(root)
        self.assertTrue(attempt.path.exists())
        self.assertTrue(staged.is_symlink())

    def test_orphan_recovery_survives_repeated_kills_without_losing_intent(self) -> None:
        class SimulatedHardKill(BaseException):
            pass

        root = self.base / "recursive-recovery-workspace"
        root.mkdir()
        attempt_id = "9" * 32
        destination = root / official.ATTEMPTS_NAME / attempt_id / "attempt.json"
        payload = official._canonical_json_bytes(
            official._expected_attempt_document(self.context, root, attempt_id)
        )
        staged, proof_path, identity = self._seed_pending_publication(
            destination, payload, "before_commit"
        )
        proof = official._scan_workspace(root)[0]
        real_resume = official._resume_proof_owned_commit

        def commit_then_kill(workspace: Path, pending: official._PublicationProof) -> None:
            real_resume(workspace, pending)
            raise SimulatedHardKill()

        with mock.patch.object(
            official, "_resume_proof_owned_commit", side_effect=commit_then_kill
        ):
            with self.assertRaises(SimulatedHardKill):
                official._recover_publication(root, proof, payload)
        with mock.patch.object(official, "_replay_context"):
            self.assertEqual(
                official._audit_pending_publications(self.context, root),
                (f"{official.ATTEMPTS_NAME}/{attempt_id}/attempt.json",),
            )
        self.assertTrue(proof_path.exists())
        self.assertTrue(destination.exists())

        proof = official._scan_workspace(root)[0]

        def kill_before_proof_cleanup(
            workspace: Path,
            pending: official._PublicationProof,
            expected_payload: bytes,
        ) -> None:
            raise SimulatedHardKill()

        with mock.patch.object(
            official, "_remove_publication_proof", side_effect=kill_before_proof_cleanup
        ):
            with self.assertRaises(SimulatedHardKill):
                official._recover_publication(root, proof, payload)
        with mock.patch.object(official, "_replay_context"):
            self.assertEqual(
                official._audit_pending_publications(self.context, root),
                (f"{official.ATTEMPTS_NAME}/{attempt_id}/attempt.json",),
            )
        self.assertFalse(staged.exists())
        self.assertTrue(destination.exists())
        self.assertTrue(proof_path.exists())

        official._recover_publication(root, official._scan_workspace(root)[0], payload)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(atomic_publish.FileIdentity.from_path(destination), identity)
        self.assertEqual(destination.stat().st_nlink, 1)
        self.assertFalse(proof_path.exists())

    def test_foreign_hardlink_injected_after_scan_fails_closed(self) -> None:
        root = self.base / "toctou-hardlink-workspace"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"proof-owned\n"
        staged, proof_path, _ = self._seed_pending_publication(
            destination, payload, "before_commit"
        )
        foreign = self.base / "toctou-foreign-hardlink"
        proof = official._scan_workspace(root)[0]
        real_read = official._read_proof_owned_payload
        injected = False

        def inject_then_read(
            path: Path,
            identity: atomic_publish.FileIdentity,
            *,
            expected_links: int,
        ) -> bytes:
            nonlocal injected
            if not injected:
                try:
                    os.link(staged, foreign)
                except OSError as exc:
                    self.skipTest(f"hardlinks unavailable: {exc}")
                injected = True
            return real_read(path, identity, expected_links=expected_links)

        with mock.patch.object(
            official, "_read_proof_owned_payload", side_effect=inject_then_read
        ):
            with self.assertRaisesRegex(official.OfficialStage1Error, "hardlink ownership"):
                official._recover_publication(root, proof, payload)
        self.assertTrue(staged.exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(proof_path.exists())
        self.assertFalse(destination.exists())

    def test_foreign_hardlink_injected_before_proof_cleanup_fails_closed(self) -> None:
        root = self.base / "cleanup-toctou-hardlink-workspace"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"committed-owned\n"
        _, proof_path, _ = self._seed_pending_publication(
            destination, payload, "after_staging_unlink"
        )
        foreign = self.base / "cleanup-toctou-foreign-hardlink"
        proof = official._scan_workspace(root)[0]
        real_remove = official._remove_publication_proof

        def inject_then_remove(
            workspace: Path,
            pending: official._PublicationProof,
            expected_payload: bytes,
        ) -> None:
            try:
                os.link(destination, foreign)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
            real_remove(workspace, pending, expected_payload)

        with mock.patch.object(
            official, "_remove_publication_proof", side_effect=inject_then_remove
        ):
            with self.assertRaisesRegex(
                official.OfficialStage1Error, "ownership is ambiguous"
            ):
                official._recover_publication(root, proof, payload)
        self.assertTrue(destination.exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(proof_path.exists())

    def test_foreign_hardlink_injected_after_cleanup_scan_restores_proof(self) -> None:
        root = self.base / "post-scan-toctou-workspace"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"post-scan-owned\n"
        _, proof_path, _ = self._seed_pending_publication(
            destination, payload, "after_staging_unlink"
        )
        foreign = self.base / "post-scan-toctou-foreign"
        proof = official._scan_workspace(root)[0]
        real_scan = official._scan_workspace
        scan_count = 0

        def scan_then_inject(workspace: Path):
            nonlocal scan_count
            result = real_scan(workspace)
            scan_count += 1
            if scan_count == 2:
                try:
                    os.link(destination, foreign)
                except OSError as exc:
                    self.skipTest(f"hardlinks unavailable: {exc}")
            return result

        with mock.patch.object(
            official, "_scan_workspace", side_effect=scan_then_inject
        ):
            with self.assertRaisesRegex(
                official.OfficialStage1Error, "across final proof cleanup"
            ):
                official._recover_publication(root, proof, payload)
        self.assertTrue(destination.exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(proof_path.exists())
        foreign.unlink()
        official._recover_publication(root, official._scan_workspace(root)[0], payload)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(destination.stat().st_nlink, 1)
        self.assertFalse(proof_path.exists())

    def test_pending_publication_rejects_foreign_extra_hardlink(self) -> None:
        root = self.base / "foreign-hardlink-workspace"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        staged, proof, _ = self._seed_pending_publication(
            destination, b"owned\n", "after_hardlink"
        )
        foreign = self.base / "foreign-hardlink"
        try:
            os.link(destination, foreign)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaisesRegex(official.OfficialStage1Error, "ownership is ambiguous"):
            official._scan_workspace(root)
        self.assertTrue(staged.exists())
        self.assertTrue(destination.exists())
        self.assertTrue(proof.exists())

    def test_pending_publication_rejects_tampered_hardlink_bytes(self) -> None:
        root = self.base / "tampered-hardlink-workspace"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        expected = b"original\n"
        staged, proof_path, _ = self._seed_pending_publication(
            destination, expected, "after_hardlink"
        )
        destination.write_bytes(b"tampered\n")
        proof = official._scan_workspace(root)[0]
        with self.assertRaisesRegex(official.OfficialStage1Error, "bytes differ"):
            official._recover_publication(root, proof, expected)
        self.assertTrue(staged.exists())
        self.assertTrue(destination.exists())
        self.assertTrue(proof_path.exists())

    def test_no_replace_publication_preserves_windows_rename_strategy(self) -> None:
        root = self.base / "rename-workspace"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"rename fallback\n"

        def rename_no_replace(
            source: Path, output: Path, *, proof_path: Path
        ) -> atomic_publish.PublishReceipt:
            source = Path(source)
            output = Path(output)
            identity = atomic_publish.FileIdentity.from_path(source)
            atomic_publish._write_proof_exclusive(
                Path(proof_path),
                source=source,
                destination=output,
                identity=identity,
            )
            os.rename(source, output)
            return atomic_publish.PublishReceipt(
                source=source,
                destination=output,
                identity=identity,
                strategy="windows_rename",
                proof_path=Path(proof_path),
            )

        with mock.patch.object(official, "publish_no_replace", side_effect=rename_no_replace):
            official._publish_no_replace(root, destination, payload)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(destination.stat().st_nlink, 1)
        self.assertFalse(official._publication_proof_path(destination).exists())

    def test_orphan_recovery_preserves_windows_rename_strategy(self) -> None:
        root = self.base / "rename-recovery-workspace"
        root.mkdir()
        destination = root / official.COMPLETION_NAME
        payload = b"rename recovery\n"
        staged, proof_path, identity = self._seed_pending_publication(
            destination, payload, "before_commit"
        )

        def rename_no_replace(source: Path, output: Path) -> None:
            os.rename(source, output)

        with mock.patch.object(
            atomic_publish, "_is_windows_remote_path", return_value=True
        ), mock.patch.object(
            atomic_publish,
            "_windows_rename_no_replace",
            side_effect=rename_no_replace,
        ) as rename:
            official._recover_publication(
                root, official._scan_workspace(root)[0], payload
            )
        rename.assert_called_once_with(staged, destination)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(atomic_publish.FileIdentity.from_path(destination), identity)
        self.assertFalse(staged.exists())
        self.assertFalse(proof_path.exists())

    def test_pending_ready_is_finalized_before_bundle_recovery_without_retraining(self) -> None:
        root = self.base / "ready-recovery-workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        payload = ready.ready_path.read_bytes()
        ready.ready_path.unlink()
        staged, proof, committed_identity = self._seed_pending_publication(
            ready.ready_path, payload, "after_hardlink"
        )
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_new_attempt") as run_attempt,
        ):
            bundle = official.publish_official_bundle(
                self.pipeline_contract_source, self.contract_source, root
            )
        run_attempt.assert_not_called()
        self.assertEqual(bundle.attempt_dir, ready.attempt_dir)
        self.assertTrue(bundle.completion_path.is_file())
        self.assertEqual(
            atomic_publish.FileIdentity.from_path(ready.ready_path), committed_identity
        )
        self.assertFalse(staged.exists())
        self.assertFalse(proof.exists())

    def test_partial_ready_proof_recovers_bundle_without_retraining(self) -> None:
        root = self.base / "partial-ready-bundle-recovery"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        payload = ready.ready_path.read_bytes()
        ready.ready_path.unlink()
        attempt, staged, committed_identity, proof_payload = (
            self._seed_durable_publication_attempt(
                root, ready.ready_path, payload
            )
        )
        proof_path = official._publication_proof_path(ready.ready_path)
        proof_path.write_bytes(proof_payload[:18])
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_new_attempt") as run_attempt,
        ):
            bundle = official.publish_official_bundle(
                self.pipeline_contract_source, self.contract_source, root
            )
        run_attempt.assert_not_called()
        self.assertEqual(bundle.attempt_dir, ready.attempt_dir)
        self.assertTrue(bundle.completion_path.is_file())
        self.assertEqual(
            atomic_publish.FileIdentity.from_path(ready.ready_path),
            committed_identity,
        )
        self.assertFalse(staged.exists())
        self.assertFalse(proof_path.exists())
        self.assertFalse(attempt.path.exists())

    def test_pending_attempt_is_rebuilt_from_current_authority(self) -> None:
        root = self.base / "attempt-recovery-workspace"
        root.mkdir()
        attempt_id = "c" * 32
        destination = root / official.ATTEMPTS_NAME / attempt_id / "attempt.json"
        payload = official._canonical_json_bytes(
            official._expected_attempt_document(self.context, root, attempt_id)
        )
        staged, proof, _ = self._seed_pending_publication(
            destination, payload, "before_commit"
        )
        with mock.patch.object(official, "_replay_context"):
            official._recover_pending_publications(self.context, root)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(destination.stat().st_nlink, 1)
        self.assertFalse(staged.exists())
        self.assertFalse(proof.exists())

    def test_pending_completion_is_finalized_and_replayed_without_replacement(self) -> None:
        root = self.base / "completion-recovery-workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        destination = root / official.COMPLETION_NAME
        payload = official._canonical_json_bytes(
            official._envelope(
                official.COMPLETION_SCHEMA_VERSION,
                official._completion_payload(self.context, ready),
            )
        )
        staged, proof, committed_identity = self._seed_pending_publication(
            destination, payload, "after_staging_unlink"
        )
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_new_attempt") as run_attempt,
        ):
            bundle = official.publish_official_bundle(
                self.pipeline_contract_source, self.contract_source, root
            )
        run_attempt.assert_not_called()
        self.assertEqual(bundle.completion_sha256, official._sha256_bytes(payload))
        self.assertEqual(
            atomic_publish.FileIdentity.from_path(destination), committed_identity
        )
        self.assertFalse(staged.exists())
        self.assertFalse(proof.exists())

    def test_workspace_rejects_symlink_entries(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        target = self.base / "target"
        target.write_bytes(b"target")
        try:
            (root / "alias").symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(official.OfficialStage1Error, "symlink/reparse"):
            official._secure_workspace(root, create=False)

    def test_windows_reparse_attribute_is_treated_as_a_link(self) -> None:
        info = SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)
        with mock.patch.object(official.os, "lstat", return_value=info):
            self.assertTrue(official._path_is_link_or_reparse(self.base / "reparse"))

    def test_workspace_lock_rejects_concurrent_publisher(self) -> None:
        root = self.base / "workspace"
        with official._workspace_lock(root):
            with self.assertRaisesRegex(official.OfficialStage1Error, "holds the lock"):
                with official._workspace_lock(root):
                    self.fail("second publisher unexpectedly acquired the lock")

    @unittest.skipUnless(os.name == "nt", "mapped-drive alias behavior is Windows-specific")
    def test_secure_workspace_accepts_mapped_drive_and_returns_canonical_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as name:
            lexical = Path(name)
            secured = official._secure_workspace(lexical, create=False)
            self.assertEqual(secured, lexical.resolve(strict=True))

    def test_attempt_commands_put_all_outputs_and_cwd_inside_attempt(self) -> None:
        root = self.base / "workspace"
        attempt = root / "attempts" / ("b" * 32)
        attempt.mkdir(parents=True)
        validation, training = official._attempt_commands(self.context, attempt)
        self.assertEqual(validation[validation.index("--summary") + 1], str(attempt / "validation.csv"))
        self.assertEqual(training[training.index("--model-dir") + 1], str(attempt / "models"))
        self.assertEqual(training[training.index("--verification-output") + 1], str(attempt / "r2.csv"))
        self.assertEqual(training[training.index("--data") + 1], str(self.result))
        self.assertEqual(training.count("--fail-on-threshold"), 1)

    def test_stage1_result_coverage_is_independent_and_exact(self) -> None:
        plan = self.base / "plan.csv"
        result = self.base / "result.csv"
        plan.write_text("case_id\na\nb\n", encoding="utf-8")
        result.write_text("case_id,status\nb,ok\na,ok\n", encoding="utf-8")
        plan_sha = official._sha256_bytes(plan.read_bytes())
        official._audit_stage1_result_coverage(
            plan,
            result,
            2,
            expected_case_plan_sha256=plan_sha,
            expected_result_sha256=official._sha256_bytes(result.read_bytes()),
        )
        for payload in (
            "case_id,status\na,ok\na,ok\n",
            "case_id,status\na,ok\n",
            "case_id,status\na,ok\nb,failed\n",
        ):
            with self.subTest(payload=payload):
                result.write_text(payload, encoding="utf-8")
                with self.assertRaises(official.OfficialStage1Error):
                    official._audit_stage1_result_coverage(
                        plan,
                        result,
                        2,
                        expected_case_plan_sha256=plan_sha,
                        expected_result_sha256=official._sha256_bytes(result.read_bytes()),
                    )

    def test_v4_stage1_authority_exactly_binds_cli_workspace_and_completion(self) -> None:
        root = self.base / "workspace"
        publisher = official._resolved_absolute(Path(official.__file__))
        authority = SimpleNamespace(
            source=self.pipeline_contract_source,
            workdir=self.workdir,
            stage1_official=SimpleNamespace(
                workspace=root,
                completion=root / official.COMPLETION_NAME,
                publisher_argv=(
                    sys.executable,
                    str(publisher),
                    "--pipeline-contract",
                    str(self.pipeline_contract_source),
                    "--base-contract",
                    str(self.contract_source),
                    "--workspace",
                    str(root),
                ),
            ),
        )
        context = replace(self.context, pipeline_contract=authority)
        official._audit_stage1_authority_config(context, root)
        changed_stage = SimpleNamespace(
            workspace=root,
            completion=root / "other.json",
            publisher_argv=authority.stage1_official.publisher_argv,
        )
        with self.assertRaisesRegex(official.OfficialStage1Error, "completion"):
            official._audit_stage1_authority_config(
                replace(
                    context,
                    pipeline_contract=SimpleNamespace(
                        source=self.pipeline_contract_source,
                        workdir=self.workdir,
                        stage1_official=changed_stage,
                    ),
                ),
                root,
            )

    def test_valid_ready_and_completion_replay_pass_gate(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        completion = self._write_completion(root, ready)
        with (
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
        ):
            bundle = official._bundle_from_completion(self.context, root, completion)
        self.assertEqual(bundle.trainer_exit_code, 0)
        self.assertEqual(bundle.model_dir, ready.attempt_dir / "models")
        self.assertEqual(bundle.gate.decision, "skip_stage2")

    def test_r2_failure_is_a_valid_ready_and_completion(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=False)
        completion = self._write_completion(root, ready)
        with (
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(False)),
            mock.patch.object(official, "_replay_context"),
        ):
            bundle = official._bundle_from_completion(self.context, root, completion)
        self.assertEqual(bundle.trainer_exit_code, 1)
        self.assertEqual(bundle.gate.decision, "run_stage2")

    def test_trainer_exit_and_gate_disagreement_fails(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        with mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(False)):
            with self.assertRaisesRegex(official.OfficialStage1Error, "exit code"):
                official._audit_outputs(
                    self.context,
                    root,
                    ready.attempt_dir,
                    trainer_exit_code=0,
                )

    def test_artifact_tamper_after_ready_is_rejected(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        (ready.attempt_dir / "r2.csv").write_bytes(b"tampered")
        with (
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
        ):
            with self.assertRaisesRegex(official.OfficialStage1Error, "replay"):
                official._audit_ready(self.context, root, ready.ready_path)

    def test_each_authority_class_drift_before_ready_prevents_ready_commit(self) -> None:
        kinds = (
            "v4_contract",
            "base_contract",
            "stage1_result",
            *self.context.sources,
        )
        for index, kind in enumerate(kinds):
            with self.subTest(kind=kind):
                root = self.base / f"before-ready-{index}"
                root.mkdir()
                changed = self._mutated_context(kind)
                with (
                    mock.patch.object(
                        official,
                        "_build_context",
                        side_effect=[self.context, self.context, self.context, changed],
                    ),
                    mock.patch.object(official, "_run_child", return_value=0),
                    mock.patch.object(
                        official, "_audit_validation_summary"
                    ),
                    mock.patch.object(
                        official,
                        "_audit_outputs",
                        return_value=({}, self._gate(True).summary(), True),
                    ),
                ):
                    with self.assertRaisesRegex(official.OfficialStage1Error, "changed"):
                        official._run_new_attempt(self.context, root)
                self.assertFalse(any(root.rglob("ready.json")))

    def test_each_authority_class_drift_before_completion_prevents_commit(self) -> None:
        kinds = (
            "v4_contract",
            "base_contract",
            "stage1_result",
            *self.context.sources,
        )
        for index, kind in enumerate(kinds):
            with self.subTest(kind=kind):
                root = self.base / f"before-completion-{index}"
                root.mkdir()
                attempt_id = f"{index + 1:032x}"
                attempt_dir = root / official.ATTEMPTS_NAME / attempt_id
                attempt_dir.mkdir(parents=True)
                ready_path = attempt_dir / "ready.json"
                ready_path.write_bytes(b"ready")
                ready = official._ReadyAudit(
                    attempt_id=attempt_id,
                    attempt_dir=attempt_dir,
                    attempt_sha256="1" * 64,
                    ready_path=ready_path,
                    ready_sha256="2" * 64,
                    artifacts={},
                    gate=self._gate(True).summary(),
                    gate_passed=True,
                    trainer_exit_code=0,
                )
                changed = self._mutated_context(kind)
                with mock.patch.object(official, "_build_context", return_value=changed):
                    with self.assertRaisesRegex(official.OfficialStage1Error, "changed"):
                        official._publish_completion(self.context, root, ready)
                self.assertFalse((root / official.COMPLETION_NAME).exists())

    def test_invalid_ready_fails_instead_of_starting_another_attempt(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        ready.ready_path.write_bytes(b"not-json")
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(official, "_run_new_attempt") as run_attempt,
        ):
            with self.assertRaises(official.OfficialStage1Error):
                official.publish_official_bundle(
                    self.pipeline_contract_source, self.contract_source, root
                )
        run_attempt.assert_not_called()

    def test_multiple_ready_attempts_fail_before_any_salvage(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        self._make_ready(root, attempt_id="a" * 32)
        self._make_ready(root, attempt_id="b" * 32)
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
        ):
            with self.assertRaisesRegex(official.OfficialStage1Error, "multiple ready"):
                official.publish_official_bundle(
                    self.pipeline_contract_source, self.contract_source, root
                )

    def test_one_ready_is_salvaged_without_retraining(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_new_attempt") as run_attempt,
        ):
            bundle = official.publish_official_bundle(
                self.pipeline_contract_source, self.contract_source, root
            )
        run_attempt.assert_not_called()
        self.assertTrue((root / official.COMPLETION_NAME).is_file())
        self.assertEqual(bundle.attempt_dir, ready.attempt_dir)

    def test_completed_attempt_without_ready_is_salvaged_without_trainer_replay(self) -> None:
        root = self.base / "completed-attempt-salvage"
        root.mkdir()
        completed = self._make_ready(root, attempt_id="3" * 32, passed=True)
        expected_ready_bytes = completed.ready_path.read_bytes()
        completed.ready_path.unlink()
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_child") as trainer,
            mock.patch.object(official, "_run_new_attempt") as run_new_attempt,
        ):
            bundle = official.publish_official_bundle(
                self.pipeline_contract_source, self.contract_source, root
            )
            replay = official._audit_ready(
                self.context, root, completed.ready_path
            )
        self.assertEqual(trainer.call_count, 0)
        run_new_attempt.assert_not_called()
        self.assertEqual(bundle.attempt_dir, completed.attempt_dir)
        self.assertEqual(completed.ready_path.read_bytes(), expected_ready_bytes)
        self.assertEqual(replay.attempt_sha256, completed.attempt_sha256)
        self.assertEqual(replay.artifacts, completed.artifacts)
        self.assertEqual(replay.gate, completed.gate)
        self.assertEqual(replay.trainer_exit_code, completed.trainer_exit_code)
        self.assertTrue(bundle.completion_path.is_file())

    def test_multiple_completed_attempts_without_ready_fail_as_ambiguous(self) -> None:
        root = self.base / "multiple-completed-attempts"
        root.mkdir()
        first = self._make_ready(root, attempt_id="4" * 32, passed=True)
        second = self._make_ready(root, attempt_id="5" * 32, passed=True)
        first.ready_path.unlink()
        second.ready_path.unlink()
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_child") as trainer,
            mock.patch.object(official, "_run_new_attempt") as run_new_attempt,
        ):
            with self.assertRaisesRegex(
                official.OfficialStage1Error, "multiple completed attempts"
            ):
                official.publish_official_bundle(
                    self.pipeline_contract_source, self.contract_source, root
                )
        self.assertEqual(trainer.call_count, 0)
        run_new_attempt.assert_not_called()
        self.assertFalse((root / official.COMPLETION_NAME).exists())

    def test_completed_failed_gate_salvage_infers_trainer_exit_one(self) -> None:
        root = self.base / "completed-failed-gate-salvage"
        root.mkdir()
        completed = self._make_ready(root, attempt_id="7" * 32, passed=False)
        expected_ready_bytes = completed.ready_path.read_bytes()
        completed.ready_path.unlink()
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(False)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_child") as trainer,
            mock.patch.object(official, "_run_new_attempt") as run_new_attempt,
        ):
            bundle = official.publish_official_bundle(
                self.pipeline_contract_source, self.contract_source, root
            )
        self.assertEqual(trainer.call_count, 0)
        run_new_attempt.assert_not_called()
        self.assertEqual(bundle.trainer_exit_code, 1)
        self.assertFalse(bundle.gate.passed)
        self.assertEqual(completed.ready_path.read_bytes(), expected_ready_bytes)
        self.assertTrue(bundle.completion_path.is_file())

    def test_changed_completed_attempt_output_fails_instead_of_retraining(self) -> None:
        root = self.base / "changed-completed-attempt"
        root.mkdir()
        completed = self._make_ready(root, attempt_id="6" * 32, passed=True)
        completed.ready_path.unlink()
        model = next((completed.attempt_dir / "models").glob("*_lgbm.pkl"))
        model.write_bytes(b"changed model bytes")
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_child") as trainer,
            mock.patch.object(official, "_run_new_attempt") as run_new_attempt,
        ):
            with self.assertRaises(official.OfficialStage1Error):
                official.publish_official_bundle(
                    self.pipeline_contract_source, self.contract_source, root
                )
        self.assertEqual(trainer.call_count, 0)
        run_new_attempt.assert_not_called()
        self.assertFalse((root / official.COMPLETION_NAME).exists())

    def test_partial_attempt_is_ignored_and_fresh_attempt_can_win(self) -> None:
        root = self.base / "workspace"
        partial = root / official.ATTEMPTS_NAME / ("1" * 32)
        partial.mkdir(parents=True)
        (partial / "attempt.json").write_bytes(b"partial")

        def create_ready(context: object, workspace: Path) -> official._ReadyAudit:
            return self._make_ready(workspace, attempt_id="2" * 32, passed=True)

        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_new_attempt", side_effect=create_ready),
        ):
            bundle = official.publish_official_bundle(
                self.pipeline_contract_source, self.contract_source, root
            )
        self.assertEqual(bundle.attempt_dir.name, "2" * 32)
        self.assertTrue((root / official.COMPLETION_NAME).is_file())

    def test_existing_completion_is_replayed_without_replacement(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        completion = self._write_completion(root, ready)
        before = completion.read_bytes()
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
            mock.patch.object(official, "_run_new_attempt") as run_attempt,
        ):
            bundle = official.publish_official_bundle(
                self.pipeline_contract_source, self.contract_source, root
            )
        run_attempt.assert_not_called()
        self.assertEqual(completion.read_bytes(), before)
        self.assertEqual(bundle.completion_sha256, official._sha256_bytes(before))

    def test_invalid_existing_completion_is_never_replaced(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        completion = root / official.COMPLETION_NAME
        original = b"{}\n"
        completion.write_bytes(original)
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(official, "_run_new_attempt") as run_attempt,
        ):
            with self.assertRaises(official.OfficialStage1Error):
                official.publish_official_bundle(
                    self.pipeline_contract_source, self.contract_source, root
                )
        run_attempt.assert_not_called()
        self.assertEqual(completion.read_bytes(), original)

    def test_inspection_of_absent_workspace_is_read_only(self) -> None:
        root = self.base / "absent"
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
        ):
            result = official.inspect_official_workspace(
                self.pipeline_contract_source, self.contract_source, root
            )
            pending = official.inspect_pending_publications(
                self.pipeline_contract_source, self.contract_source, root
            )
        self.assertEqual(result, {"partial_attempts": 0, "status": "needs_run"})
        self.assertEqual(pending, ())
        self.assertFalse(root.exists())

    def test_inspection_reports_pending_completion_without_mutation(self) -> None:
        root = self.base / "pending-inspection-workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        destination = root / official.COMPLETION_NAME
        payload = official._canonical_json_bytes(
            official._envelope(
                official.COMPLETION_SCHEMA_VERSION,
                official._completion_payload(self.context, ready),
            )
        )
        staged, proof, identity = self._seed_pending_publication(
            destination, payload, "after_hardlink"
        )
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
        ):
            result = official.inspect_official_workspace(
                self.pipeline_contract_source, self.contract_source, root
            )
            pending = official.inspect_pending_publications(
                self.pipeline_contract_source, self.contract_source, root
            )
        self.assertEqual(
            result,
            {
                "pending_publications": 1,
                "publication_destinations": [official.COMPLETION_NAME],
                "status": "publication_recovery_pending",
            },
        )
        self.assertEqual(pending, (official.COMPLETION_NAME,))
        self.assertEqual(atomic_publish.FileIdentity.from_path(destination), identity)
        self.assertEqual(destination.stat().st_nlink, 2)
        self.assertTrue(staged.exists())
        self.assertTrue(proof.exists())

    def test_inspection_reports_partial_proof_without_mutation(self) -> None:
        root = self.base / "partial-proof-inspection-workspace"
        root.mkdir()
        ready = self._make_ready(root, passed=True)
        destination = root / official.COMPLETION_NAME
        payload = official._canonical_json_bytes(
            official._envelope(
                official.COMPLETION_SCHEMA_VERSION,
                official._completion_payload(self.context, ready),
            )
        )
        attempt, staged, _, proof_payload = self._seed_durable_publication_attempt(
            root, destination, payload
        )
        proof = official._publication_proof_path(destination)
        partial = proof_payload[:18]
        proof.write_bytes(partial)
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(stage2, "evaluate_gate", return_value=self._gate(True)),
            mock.patch.object(official, "_replay_context"),
        ):
            result = official.inspect_official_workspace(
                self.pipeline_contract_source, self.contract_source, root
            )
            pending = official.inspect_pending_publications(
                self.pipeline_contract_source, self.contract_source, root
            )
        self.assertEqual(pending, (official.COMPLETION_NAME,))
        self.assertEqual(result["status"], "publication_recovery_pending")
        self.assertEqual(result["publication_destinations"], [official.COMPLETION_NAME])
        self.assertEqual(proof.read_bytes(), partial)
        self.assertTrue(staged.exists())
        self.assertTrue(attempt.path.exists())
        self.assertTrue((attempt.path / official.PUBLISH_STAGE_READY_NAME).is_dir())
        self.assertFalse(destination.exists())

    def test_inspection_reports_orphan_stage_before_commit_without_mutation(self) -> None:
        root = self.base / "orphan-stage-inspection-workspace"
        root.mkdir()
        attempt_id = "e" * 32
        destination = root / official.ATTEMPTS_NAME / attempt_id / "attempt.json"
        payload = official._canonical_json_bytes(
            official._expected_attempt_document(self.context, root, attempt_id)
        )
        staged, proof, _ = self._seed_pending_publication(
            destination, payload, "before_commit"
        )
        expected_path = f"{official.ATTEMPTS_NAME}/{attempt_id}/attempt.json"
        with (
            mock.patch.object(official, "_build_context", return_value=self.context),
            mock.patch.object(official, "_audit_stage1_authority_config"),
            mock.patch.object(official, "_replay_context"),
        ):
            result = official.inspect_official_workspace(
                self.pipeline_contract_source, self.contract_source, root
            )
            pending = official.inspect_pending_publications(
                self.pipeline_contract_source, self.contract_source, root
            )
        self.assertEqual(pending, (expected_path,))
        self.assertEqual(result["status"], "publication_recovery_pending")
        self.assertEqual(result["publication_destinations"], [expected_path])
        self.assertFalse(destination.exists())
        self.assertTrue(staged.exists())
        self.assertTrue(proof.exists())

    def test_cli_uses_frozen_v4_and_base_contract_flags(self) -> None:
        root = self.base / "workspace"
        with mock.patch.object(
            official,
            "inspect_official_workspace",
            return_value={"partial_attempts": 0, "status": "needs_run"},
        ) as inspect:
            with redirect_stdout(io.StringIO()):
                code = official.main(
                    [
                        "--pipeline-contract",
                        str(self.pipeline_contract_source),
                        "--base-contract",
                        str(self.contract_source),
                        "--workspace",
                        str(root),
                    ]
                )
        self.assertEqual(code, 0)
        inspect.assert_called_once_with(
            self.pipeline_contract_source,
            self.contract_source,
            root,
        )

    def test_cli_returns_zero_for_complete_strict_r2_failure(self) -> None:
        root = self.base / "workspace"
        attempt = root / official.ATTEMPTS_NAME / ("f" * 32)
        bundle = official.OfficialBundle(
            completion_path=root / official.COMPLETION_NAME,
            completion_sha256="1" * 64,
            attempt_dir=attempt,
            validation=attempt / "validation.csv",
            model_dir=attempt / "models",
            metadata=attempt / "models" / "metadata.json",
            r2=attempt / "r2.csv",
            stage1_result=self.result,
            result_sha256="2" * 64,
            trainer_exit_code=1,
            gate=self._gate(False),
        )
        with mock.patch.object(
            official, "publish_official_bundle", return_value=bundle
        ):
            with redirect_stdout(io.StringIO()):
                code = official.main(
                    [
                        "--pipeline-contract",
                        str(self.pipeline_contract_source),
                        "--base-contract",
                        str(self.contract_source),
                        "--workspace",
                        str(root),
                        "--execute",
                    ]
                )
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
