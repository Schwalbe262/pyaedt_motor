from __future__ import annotations

import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import atomic_publish
import prepare_ipmsm_torque_unit_recovery_plans as recovery_plans
import revise_ipmsm_v2_torque_recovery_base_v4r4 as revision
import supervise_ipmsm_v2_pipeline as supervisor


REPO = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TorqueRecoveryBaseRevisionTests(unittest.TestCase):
    def source_document(self) -> dict[str, object]:
        return json.loads((REPO / revision.SOURCE_BASE).read_text(encoding="utf-8"))

    def bindings(self) -> revision.RevisionBindings:
        sources = tuple(
            revision.ArtifactBinding(path, sha(REPO / path))
            for path in revision.LEGACY_SOURCE_INPUTS + revision.RECOVERY_SOURCE_INPUTS
        )
        return revision.RevisionBindings(
            stage1_plan=revision.ArtifactBinding(revision.STAGE1_PLAN, "1" * 64),
            stage2_plan=revision.ArtifactBinding(revision.STAGE2_PLAN, "2" * 64),
            stage1_output=revision.STAGE1_OUTPUT,
            evidence=tuple(
                revision.ArtifactBinding(path, str(index + 3) * 64)
                for index, path in enumerate(revision.EVIDENCE_PATHS)
            ),
            sources=sources,
        )

    def rehash(self, document: dict[str, object], schema: str) -> None:
        document["contract_sha256"] = supervisor._canonical_sha256(
            {"schema_version": schema, "pipeline": document["pipeline"]}
        )

    def test_revision_has_exact_recursive_diff_cap50_and_stage2_identity(self) -> None:
        source = self.source_document()
        revised, allowed = revision.build_revision(source, self.bindings())
        self.assertEqual(revision._changed_paths(source, revised), set(allowed))

        pipeline = revised["pipeline"]
        cap_argvs = (
            pipeline["stage1"]["campaign_argv"],
            pipeline["stage2"]["argv"],
            pipeline["stage3"]["continuation_argv"],
            pipeline["optimization"]["argv_template"],
            pipeline["speed"]["campaign_argv"],
        )
        for argv in cap_argvs:
            position = argv.index("--project-active-cap")
            self.assertEqual(argv[position + 1], "50")
            self.assertNotEqual(argv[position + 1], "100")

        stage2 = pipeline["stage2"]["argv"]
        expected = {
            "--project": revision.PROJECT,
            "--scheduler-url": revision.SCHEDULER_URL,
            "--stage2-task-prefix": "ipmsm-v2-foundation-s2",
            "--stage2-remote-cases-dir": "remote/ipmsm_v2_foundation_s2",
            "--stage2-result-dir": "simul_log/ipmsm_v2_foundation_s2",
            "--stage2-simulation-dir": "simulation/ipmsm_v2_foundation_s2",
            "--stage2-log-dir": "simul_log_scheduler/ipmsm_v2_foundation_s2_logs",
        }
        for flag, value in expected.items():
            self.assertEqual(stage2[stage2.index(flag) + 1], value)

        immutable = pipeline["immutable_inputs"]
        self.assertEqual(
            [item["path"] for item in immutable[-9:]],
            list(revision.EVIDENCE_PATHS + revision.RECOVERY_SOURCE_INPUTS),
        )
        self.assertEqual(len({item["path"] for item in immutable}), len(immutable))
        self.assertEqual(
            revised["contract_sha256"],
            supervisor._canonical_sha256(
                {
                    "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
                    "pipeline": pipeline,
                }
            ),
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "base.json"
            path.write_text(json.dumps(revised), encoding="utf-8")
            loaded = supervisor.load_contract(path)
            self.assertEqual(loaded.contract_sha256, revised["contract_sha256"])

        fresh = revision._configured_fresh_paths(revised, REPO)
        old = revision._configured_old_output_roots(source, REPO)
        for index, path in enumerate(fresh):
            for other in fresh[index + 1 :]:
                self.assertFalse(revision._within(path, other))
                self.assertFalse(revision._within(other, path))
            for prior in old:
                self.assertFalse(revision._within(path, prior))
                self.assertFalse(revision._within(prior, path))

    def test_recursive_allowlist_detects_one_non_authorized_change(self) -> None:
        source = self.source_document()
        revised, allowed = revision.build_revision(source, self.bindings())
        escaped = copy.deepcopy(revised)
        escaped["pipeline"]["stage1"]["expected_rows"] = 701
        with self.assertRaisesRegex(revision.RevisionError, "recursive diff allowlist"):
            revision._assert_exact_diff(source, escaped, set(allowed))

    def test_source_pair_is_exactly_bound_but_allows_expected_source_hash_drift(self) -> None:
        wrapper = revision._read_stable_snapshot(
            REPO / revision.SOURCE_WRAPPER, "v4r3 wrapper"
        )
        wrapper_document = revision._decode_json(wrapper.payload, "v4r3 wrapper")
        bound_base = Path(wrapper_document["pipeline"]["base_contract"]["path"])
        if not bound_base.is_file():
            self.skipTest("the sealed v4r3 base path is unavailable")
        base = revision._read_stable_snapshot(bound_base, "v4r3 base")
        base_document = revision._decode_json(base.payload, "v4r3 base")
        self.assertTrue(
            revision.validate_source_pair(
                base, base_document, wrapper, wrapper_document
            ).is_dir()
        )

        changed = copy.deepcopy(wrapper_document)
        changed["pipeline"]["base_contract"]["raw_sha256"] = "0" * 64
        self.rehash(changed, revision.supervisor_v4.CONTRACT_SCHEMA_VERSION)
        with self.assertRaisesRegex(revision.RevisionError, "base hash binding"):
            revision.validate_source_pair(base, base_document, wrapper, changed)

    def test_stage2_recovery_recomputes_299_dedupes_and_quarantine(self) -> None:
        stage1_payload, stage2_payload, manifest = recovery_plans.build_recovery_bundle(
            REPO / recovery_plans.DEFAULT_STAGE1_PLAN,
            REPO / recovery_plans.DEFAULT_STAGE2_PLAN,
            REPO / recovery_plans.DEFAULT_REPLAY_PLAN,
            REPO / recovery_plans.DEFAULT_REPLAY_MANIFEST,
            Path(revision.STAGE1_PLAN),
            Path(revision.STAGE2_PLAN),
        )
        self.assertTrue(stage1_payload)
        with tempfile.TemporaryDirectory() as temporary:
            revised_path = Path(temporary) / "stage2.csv"
            revised_path.write_bytes(stage2_payload)
            source = revision._read_stable_snapshot(
                REPO / recovery_plans.DEFAULT_STAGE2_PLAN, "source Stage2"
            )
            revised = revision._read_stable_snapshot(revised_path, "revised Stage2")
            evidence = revision._audit_stage2_recovery_rows(
                source, revised, manifest
            )
            self.assertEqual(evidence["unchanged_rows"], 299)
            self.assertTrue(evidence["all_unchanged_dedupe_keys_preserved"])
            self.assertNotEqual(
                evidence["replacement_source_dedupe_key"],
                evidence["replacement_revised_dedupe_key"],
            )
            self.assertEqual(
                manifest["quarantine"]["scheduler_task_ids"], [28880]
            )

            tampered = copy.deepcopy(manifest)
            tampered["stage2_scheduler_dedupe"]["identity"]["result_dir"] = (
                "simul_log/changed"
            )
            with self.assertRaisesRegex(
                revision.RevisionError, "dedupe evidence changed"
            ):
                revision._audit_stage2_recovery_rows(source, revised, tampered)

    def test_stage2_receipt_requires_complete_readiness_and_known_suspect(self) -> None:
        identity = {"sealed": "identity"}
        case_ids = {f"case_{index:04d}" for index in range(299)} | {
            revision.SOURCE_STAGE2_CASE_ID
        }
        observations = [
            {
                "case_id": case_id,
                "selected_task_id": 10 + index,
                "classification": "physics_ok",
            }
            for index, case_id in enumerate(sorted(case_ids))
        ]
        suspect = next(
            item
            for item in observations
            if item["case_id"] == revision.SOURCE_STAGE2_CASE_ID
        )
        suspect.update(
            {
                "selected_task_id": revision.QUARANTINED_STAGE2_TASK_ID,
                "classification": "torque_unit_suspect",
            }
        )
        receipt = {
            "schema_version": revision.stage2_audit.SCHEMA_VERSION,
            "audit_identity": identity,
            "audit_identity_sha256": revision.stage2_audit.canonical_sha256(identity),
            "summary": {
                "plan_rows": 300,
                "task_identity_queries": 300,
                "coverage_complete": True,
                "active_task_count": 0,
                "successful_result_pending_count": 0,
                "replacement_set_ready_to_seal": True,
            },
            "observations": observations,
        }
        revision._validate_stage2_receipt_document(
            receipt, expected_identity=identity, expected_case_ids=case_ids
        )

        blocked = copy.deepcopy(receipt)
        blocked["summary"]["active_task_count"] = 1
        blocked["summary"]["replacement_set_ready_to_seal"] = False
        with self.assertRaisesRegex(revision.RevisionError, "not complete"):
            revision._validate_stage2_receipt_document(
                blocked, expected_identity=identity, expected_case_ids=case_ids
            )
        wrong_suspect = copy.deepcopy(receipt)
        next(
            item
            for item in wrong_suspect["observations"]
            if item["case_id"] == revision.SOURCE_STAGE2_CASE_ID
        )["selected_task_id"] = 1
        with self.assertRaisesRegex(revision.RevisionError, "quarantined"):
            revision._validate_stage2_receipt_document(
                wrong_suspect,
                expected_identity=identity,
                expected_case_ids=case_ids,
            )
        extra_contamination = copy.deepcopy(receipt)
        next(
            item
            for item in extra_contamination["observations"]
            if item["case_id"] != revision.SOURCE_STAGE2_CASE_ID
        )["classification"] = "physics_failed"
        with self.assertRaisesRegex(revision.RevisionError, "outside the sealed"):
            revision._validate_stage2_receipt_document(
                extra_contamination,
                expected_identity=identity,
                expected_case_ids=case_ids,
            )

    def test_live_project_authority_requires_exact_id_and_cap50(self) -> None:
        self.assertEqual(
            revision._validate_project_document(
                {"id": 2, "name": revision.PROJECT, "max_active_tasks": 50}
            ),
            (2, 50),
        )
        for field, value in (("id", 3), ("max_active_tasks", 100)):
            document = {
                "id": 2,
                "name": revision.PROJECT,
                "max_active_tasks": 50,
            }
            document[field] = value
            with self.subTest(field=field), self.assertRaises(
                revision.RevisionError
            ):
                revision._validate_project_document(document)

    def test_forensic_authority_requires_one_mib_remote_file_bound(self) -> None:
        revision._validate_forensic_scheduler_authority(
            {"scheduler": {"remote_file_max_bytes": 1_048_576}}
        )
        for value in (262_144, None):
            with self.subTest(value=value), self.assertRaisesRegex(
                revision.RevisionError, "1 MiB"
            ):
                revision._validate_forensic_scheduler_authority(
                    {"scheduler": {"remote_file_max_bytes": value}}
                )

    def test_default_dry_run_path_performs_no_publication_or_file_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_file = root / "base.json"
            wrapper_file = root / "wrapper.json"
            base_file.write_bytes(b"base")
            wrapper_file.write_bytes(b"wrapper")
            base_snapshot = revision._read_stable_snapshot(base_file, "base")
            wrapper_snapshot = revision._read_stable_snapshot(wrapper_file, "wrapper")
            context = revision.AuthorityContext(
                source_base=base_snapshot,
                source_wrapper=wrapper_snapshot,
                base_document={},
                wrapper_document={},
                bindings=self.bindings(),
                snapshots=(base_snapshot, wrapper_snapshot),
                directories=(),
                project_id=2,
                project_cap=50,
                fingerprint="f" * 64,
            )
            output = root / "base_v4r4.json"
            revised = {
                "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
                "contract_sha256": "c" * 64,
                "pipeline": {},
            }
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            captured = io.StringIO()
            with (
                mock.patch.object(revision, "load_authority_context", return_value=context),
                mock.patch.object(
                    revision, "build_revision", return_value=(revised, frozenset())
                ),
                mock.patch.object(revision, "_guard_output_scope"),
                mock.patch.object(revision, "publish_revision_payload") as publish,
                redirect_stdout(captured),
            ):
                code = revision.main(["--output", str(output)])
            self.assertEqual(code, 0)
            publish.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(
                before, sorted(path.relative_to(root) for path in root.rglob("*"))
            )
            self.assertEqual(json.loads(captured.getvalue())["mode"], "dry-run")

    def test_missing_prerequisite_fails_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_document = self.source_document()
            base_document["pipeline"]["workdir"] = str(root)
            for index, reference in enumerate(revision.STATIC_IMMUTABLE_PATHS):
                path = root / reference
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = f"static-{index}\n".encode()
                path.write_bytes(payload)
                base_document["pipeline"]["immutable_inputs"][index][
                    "sha256"
                ] = hashlib.sha256(payload).hexdigest()
            self.rehash(base_document, supervisor.CONTRACT_SCHEMA_VERSION)
            base_path = root / revision.SOURCE_BASE
            base_path.parent.mkdir(parents=True, exist_ok=True)
            base_payload = (json.dumps(base_document, indent=2) + "\n").encode()
            base_path.write_bytes(base_payload)

            wrapper_document = json.loads(
                (REPO / revision.SOURCE_WRAPPER).read_text(encoding="utf-8")
            )
            wrapper_document["pipeline"]["workdir"] = str(root)
            wrapper_document["pipeline"]["shared_lock"] = str(
                root / base_document["pipeline"]["lock_path"]
            )
            wrapper_document["pipeline"]["base_contract"] = {
                "path": str(base_path),
                "raw_sha256": hashlib.sha256(base_payload).hexdigest(),
                "canonical_sha256": supervisor._canonical_sha256(base_document),
                "contract_sha256": base_document["contract_sha256"],
            }
            pins = wrapper_document["pipeline"]["source_pins"]
            wrapper_document["pipeline"]["immutable_inputs"] = [
                {"path": str(base_path), "sha256": hashlib.sha256(base_payload).hexdigest()},
                *(pins[name] for name in sorted(pins)),
            ]
            self.rehash(wrapper_document, revision.supervisor_v4.CONTRACT_SCHEMA_VERSION)
            wrapper_path = root / revision.SOURCE_WRAPPER
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)
            wrapper_path.write_text(json.dumps(wrapper_document), encoding="utf-8")

            output = root / revision.OUTPUT_BASE
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            argv = [
                "--source-base",
                str(base_path),
                "--source-wrapper",
                str(wrapper_path),
                "--recovery-manifest",
                str(root / revision.RECOVERY_MANIFEST),
                "--forensic-receipt",
                str(root / revision.FORENSIC_RECEIPT),
                "--stage1-rebuild-receipt",
                str(root / revision.STAGE1_REBUILD_RECEIPT),
                "--stage2-audit-receipt",
                str(root / revision.STAGE2_AUDIT_RECEIPT),
                "--output",
                str(output),
            ]
            loaded = {
                revision.atomic_publish: "atomic_publish.py",
                revision.recovery_plans: "prepare_ipmsm_torque_unit_recovery_plans.py",
                revision.stage1_rebuild: "rebuild_ipmsm_v2_stage1_torque_unit_fix.py",
                revision.stage2_audit: "audit_ipmsm_stage2_v4r3_results.py",
                revision.supervisor: "supervise_ipmsm_v2_pipeline.py",
                revision.supervisor_v4: "supervise_ipmsm_v2_pipeline_v4.py",
            }
            patches = [
                mock.patch.object(module, "__file__", str(root / filename))
                for module, filename in loaded.items()
            ]
            patches.append(mock.patch.object(revision, "__file__", str(root / Path(revision.__file__).name)))
            for patcher in patches:
                patcher.start()
                self.addCleanup(patcher.stop)
            with self.assertRaisesRegex(revision.RevisionError, "recovery manifest"):
                revision.main(argv)
            self.assertFalse(output.exists())
            self.assertEqual(
                before, sorted(path.relative_to(root) for path in root.rglob("*"))
            )

    def test_publish_is_no_replace_idempotent_and_rolls_back_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "base.json"
            payload = b'{"contract":"v4r4"}\n'
            self.assertEqual(
                revision.publish_revision_payload(output, payload, lambda: None),
                "published",
            )
            self.assertEqual(output.read_bytes(), payload)
            self.assertEqual(
                revision.publish_revision_payload(output, payload, lambda: None),
                "existing_verified",
            )
            with self.assertRaises(FileExistsError):
                revision.publish_revision_payload(output, b"different\n", lambda: None)
            self.assertEqual(output.read_bytes(), payload)

            failed = root / "failed.json"
            calls = 0

            def validation() -> None:
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise RuntimeError("authority changed")

            with self.assertRaisesRegex(RuntimeError, "authority changed"):
                revision.publish_revision_payload(failed, payload, validation)
            self.assertFalse(failed.exists())
            self.assertFalse(revision._proof_path(failed).exists())
            self.assertFalse(revision._stage_path(failed, payload).exists())

            invalid = root / "invalid.json"

            def reject_staged(_path: Path) -> None:
                raise RuntimeError("staged contract rejected")

            with self.assertRaisesRegex(RuntimeError, "staged contract rejected"):
                revision.publish_revision_payload(
                    invalid,
                    payload,
                    lambda: None,
                    reject_staged,
                )
            self.assertFalse(invalid.exists())
            self.assertFalse(revision._proof_path(invalid).exists())
            self.assertFalse(revision._stage_path(invalid, payload).exists())

    def test_publish_recovers_hard_kill_and_late_success_exception(self) -> None:
        payload = b'{"contract":"v4r4"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hard_kill = root / "hard-kill.json"
            stage = revision._stage_path(hard_kill, payload)
            proof = revision._proof_path(hard_kill)
            revision._write_exclusive(stage, payload)
            atomic_publish.publish_no_replace(stage, hard_kill, proof_path=proof)
            self.assertEqual(
                revision.publish_revision_payload(hard_kill, payload, lambda: None),
                "recovered_late_success",
            )
            self.assertEqual(hard_kill.read_bytes(), payload)
            self.assertFalse(stage.exists())
            self.assertFalse(proof.exists())

            proof_removed = root / "proof-removed.json"
            stage = revision._stage_path(proof_removed, payload)
            proof = revision._proof_path(proof_removed)
            revision._write_exclusive(stage, payload)
            atomic_publish.publish_no_replace(
                stage, proof_removed, proof_path=proof
            )
            proof.unlink()
            self.assertEqual(
                revision.publish_revision_payload(
                    proof_removed, payload, lambda: None
                ),
                "existing_verified",
            )
            self.assertEqual(proof_removed.read_bytes(), payload)
            self.assertFalse(stage.exists())

            late = root / "late.json"
            real_publish = atomic_publish.publish_no_replace

            def publish_then_raise(source: Path, destination: Path, *, proof_path: Path):
                real_publish(source, destination, proof_path=proof_path)
                raise OSError("injected late success")

            with mock.patch.object(
                revision.atomic_publish,
                "publish_no_replace",
                side_effect=publish_then_raise,
            ):
                self.assertEqual(
                    revision.publish_revision_payload(late, payload, lambda: None),
                    "recovered_late_success",
                )
            self.assertEqual(late.read_bytes(), payload)
            self.assertFalse(revision._proof_path(late).exists())
            self.assertFalse(revision._stage_path(late, payload).exists())

    def test_stable_snapshot_rejects_hardlink_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            alias = root / "alias.json"
            source.write_bytes(b"sealed\n")
            os.link(source, alias)
            with self.assertRaisesRegex(revision.RevisionError, "exactly one hard link"):
                revision._read_stable_snapshot(source, "sealed source")


if __name__ == "__main__":
    unittest.main()
