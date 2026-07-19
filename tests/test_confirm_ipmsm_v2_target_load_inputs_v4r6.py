from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority


SHA = "a" * 64


class TargetLoadAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.results_dir = self.root / "pareto_fea" / "results"
        self.results_dir.mkdir(parents=True)
        self.model_dir = self.root / "models"
        self.model_dir.mkdir()
        self.upstream_artifact = self.model_dir / "frozen_model.bin"
        self.upstream_artifact.write_bytes(b"frozen model bytes\n")
        self.builder_source = self.root / "authority_builder.py"
        self.builder_source.write_bytes(b"# frozen builder source\n")
        self.build_config = self.root / "authority_build_config.json"
        self.build_config.write_bytes(b"{}\n")
        self.selected_candidate_ids = [f"cand_{index:02d}" for index in range(1, 13)]
        self.per_case_records: list[dict[str, object]] = []
        for candidate_id in self.selected_candidate_ids:
            for point in ("rated_torque", "rated_power"):
                for role in ("center", "lower"):
                    case_id = f"{candidate_id}_{point}_{role}"
                    relative = f"{case_id}.csv"
                    payload = f"case_id,status\n{case_id},ok\n".encode()
                    (self.results_dir / relative).write_bytes(payload)
                    self.per_case_records.append(
                        {
                            "candidate_id": candidate_id,
                            "case_id": case_id,
                            "relative_path": relative,
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    )
        self.pyaedt = self.root / "pydesktop.py"
        self.pyaedt.write_bytes(b"# frozen local pyaedt source\n")
        self.base_contract_path = self.root / "base_v4r5.json"
        self.contract_path = self.root / "pipeline_v6.json"
        self.declaration_path = self.root / "target_load_declaration.json"
        self.confirmation_path = self.root / "target_load_confirmation.json"
        self.receipt_path = self.root / "target_load_authorization.json"

    def _target_load(self) -> dict[str, object]:
        return {
            "objective": deepcopy(authority.FINAL_OBJECTIVE),
            "candidate_scope": {
                "source": "all_fea_filtered_front_candidates",
                "allow_subset": False,
                "expected_candidate_count": 12,
                "max_candidates": 12,
                "worst_case_fea_bound": 432,
            },
            "operating_points": [
                {
                    "name": "rated_torque",
                    "speed_rpm": 3000.0,
                    "target_kind": "torque",
                    "required_torque_nm": 20.0,
                    "required_power_w": 6283.185307179586,
                },
                {
                    "name": "rated_power",
                    "speed_rpm": 6000.0,
                    "target_kind": "power",
                    "required_torque_nm": 15.915494309189533,
                    "required_power_w": 10000.0,
                },
            ],
            "duty_cycle": {
                "basis": "operator-confirmed rated duty",
                "weights": [
                    {"name": "rated_torque", "duty_weight": 0.4},
                    {"name": "rated_power", "duty_weight": 0.6},
                ],
            },
            "current_matching": {
                "independent_per_candidate_operating_point_beta": True,
                "relative_tolerance": 0.01,
                "minimum_current_peak_a": 0.0,
                "maximum_current_peak_a": 120.0,
                "max_attempts": 6,
                "monotonic_relative_tolerance": 0.005,
                "minimum_step_relative": 0.01,
                "maximum_scale_per_attempt": 1.5,
            },
            "beta_validation": {
                "roles": ["center", "lower", "upper"],
                "offset_deg": 2.0,
                "fixed_current_mtpa_required": True,
                "matched_load_loss_minimum_required": True,
                "independent_current_match_per_role": True,
            },
            "scheduler": {
                "endpoint": "/api/tasks",
                "scheduling_profile": "fea_bursty",
                "required_capability": "conda:pyaedt2026v1",
                "env_profile": "pyaedt2026v1",
                "env_setup": "module load ansys-electronics/v252",
                "project_active_cap": 300,
                "max_workers_per_node": 4,
            },
            "result_settle_seconds": 60,
            "upstream_authority": {
                "binding_schema_version": authority.UPSTREAM_BINDING_SCHEMA_VERSION,
                "binding_hash_algorithm": authority.UPSTREAM_BINDING_HASH_ALGORITHM,
                "upstream_binding_sha256": "b" * 64,
                "selected_candidate_ids": list(self.selected_candidate_ids),
                "filtered_plan_sha256": "c" * 64,
                "builder_source": {
                    "path": str(self.builder_source),
                    "sha256": hashlib.sha256(self.builder_source.read_bytes()).hexdigest(),
                },
                "build_config": {
                    "path": str(self.build_config),
                    "sha256": hashlib.sha256(self.build_config.read_bytes()).hexdigest(),
                },
                "upstream_artifact_count": 1,
                "upstream_artifacts_manifest_sha256": authority.canonical_sha256(
                    {
                        "schema_version": authority.UPSTREAM_ARTIFACTS_MANIFEST_SCHEMA_VERSION,
                        "artifacts": [
                            {
                                "label": "frozen_model",
                                "path": str(self.upstream_artifact),
                                "size": self.upstream_artifact.stat().st_size,
                                "sha256": hashlib.sha256(
                                    self.upstream_artifact.read_bytes()
                                ).hexdigest(),
                            }
                        ],
                    }
                ),
                "upstream_artifacts": [
                    {
                        "label": "frozen_model",
                        "path": str(self.upstream_artifact),
                        "size": self.upstream_artifact.stat().st_size,
                        "sha256": hashlib.sha256(
                            self.upstream_artifact.read_bytes()
                        ).hexdigest(),
                    }
                ],
                "protected_input_directories": [
                    str(self.root / "pareto_fea"),
                    str(self.model_dir),
                ],
                "continuation_replay_requirement": authority.CONTINUATION_REPLAY_REQUIREMENT,
            },
            "upstream_results": {
                "pareto_fea_results_dir": str(self.results_dir),
                "path_policy": "derive_and_audit_from_v4r5_decision",
                "original_per_case_files_required": True,
                "per_case_result_count": len(self.per_case_records),
                "per_case_results_manifest_sha256": authority.canonical_sha256(
                    {
                        "schema_version": authority.PER_CASE_RESULTS_MANIFEST_SCHEMA_VERSION,
                        "results": self.per_case_records,
                    }
                ),
                "per_case_results": deepcopy(self.per_case_records),
            },
            "pyaedt_core_snapshot": {
                "path": str(self.pyaedt),
                "sha256": hashlib.sha256(self.pyaedt.read_bytes()).hexdigest(),
                "single_link_required": True,
            },
        }

    @staticmethod
    def _now_utc() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _write_contract(self, *, target_load: dict[str, object] | None = None) -> None:
        source = Path(authority.__file__).resolve()
        executable = Path(sys.executable).resolve()
        base_unsigned = {
            "schema_version": "ipmsm-v2-pipeline-contract-v4",
            "pipeline": {},
        }
        base_document = {
            **base_unsigned,
            "contract_sha256": authority.contract_logical_sha256(base_unsigned),
        }
        self.base_contract_path.write_bytes(authority.canonical_json_bytes(base_document))
        pipeline = {
            "base_v4r5_contract": {
                "path": str(self.base_contract_path),
                "raw_sha256": hashlib.sha256(self.base_contract_path.read_bytes()).hexdigest(),
                "canonical_sha256": authority.contract_logical_sha256(base_document),
                "contract_sha256": base_document["contract_sha256"],
            },
            "target_load": target_load or self._target_load(),
            "target_load_confirmation": {
                "declaration_path": str(self.declaration_path),
                "confirmation_path": str(self.confirmation_path),
                "authorization_receipt_path": str(self.receipt_path),
                "declaration_schema_version": authority.DECLARATION_SCHEMA_VERSION,
                "confirmation_schema_version": authority.CONFIRMATION_SCHEMA_VERSION,
                "authorization_receipt_schema_version": authority.RECEIPT_SCHEMA_VERSION,
                "authorizer_argv": [
                    str(executable),
                    str(source),
                    "authorize",
                    "--contract",
                    str(self.contract_path),
                    "--declaration",
                    str(self.declaration_path),
                    "--confirmation",
                    str(self.confirmation_path),
                    "--authorization-receipt",
                    str(self.receipt_path),
                    "--execute",
                ],
                "authorizer_executable": {
                    "path": str(executable),
                    "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                },
                "authorizer_source": {
                    "path": str(source),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                },
            },
        }
        unsigned = {
            "schema_version": authority.CONTRACT_SCHEMA_VERSION,
            "pipeline": pipeline,
        }
        document = {
            **unsigned,
            "contract_sha256": authority.contract_logical_sha256(unsigned),
        }
        self.contract_path.write_bytes(authority.canonical_json_bytes(document))

    def _rewrite_contract(self, mutator: object) -> None:
        document = json.loads(self.contract_path.read_text(encoding="utf-8"))
        mutator(document)  # type: ignore[operator]
        unsigned = {key: value for key, value in document.items() if key != "contract_sha256"}
        document["contract_sha256"] = authority.contract_logical_sha256(unsigned)
        self.contract_path.write_bytes(authority.canonical_json_bytes(document))

    def _confirmed_declaration(
        self, context: authority.TargetLoadAuthorityContext
    ) -> dict[str, object]:
        declaration = authority.declaration_template(context)
        declaration["authority"] = {
            "confirmed_by": "operator-1",
            "confirmed_at_utc": self._now_utc(),
            "evidence_reference": "change-control-42",
            "attestation_kind": authority.ATTESTATION_KIND,
        }
        declaration["acknowledgements"] = {
            name: True for name in authority.ACKNOWLEDGEMENT_FIELDS
        }
        return declaration

    def _publish_positive_chain(self) -> authority.AuthorizationAudit:
        context = authority.load_authority_context(self.contract_path)
        declaration = self._confirmed_declaration(context)
        authority.publish_canonical_no_replace(
            declaration, self.declaration_path, context=context
        )
        declaration_sha = hashlib.sha256(self.declaration_path.read_bytes()).hexdigest()
        confirmation = authority.build_confirmation(
            context, declaration, declaration_sha256=declaration_sha
        )
        declaration_snapshot = authority.read_single_link_snapshot(
            self.declaration_path, "test declaration"
        )
        authority.publish_canonical_no_replace(
            confirmation,
            self.confirmation_path,
            context=context,
            additional_snapshots=(declaration_snapshot,),
        )
        receipt = authority.build_authorization_receipt(
            context, authority.audit_confirmation(context)
        )
        confirmation_audit = authority.audit_confirmation(context)
        authority.publish_canonical_no_replace(
            receipt,
            self.receipt_path,
            context=context,
            additional_snapshots=(
                confirmation_audit.declaration_snapshot,
                confirmation_audit.snapshot,
            ),
        )
        return authority.audit_authorization_receipt(self.contract_path)

    def test_positive_chain_binds_new_measured_objective_and_exact_receipt(self) -> None:
        self._write_contract()
        audit = self._publish_positive_chain()
        self.assertEqual(audit.path, self.receipt_path)
        self.assertEqual(len(audit.receipt_sha256), 64)
        self.assertTrue(self.declaration_path.is_file())
        self.assertTrue(self.confirmation_path.is_file())

    def test_old_surrogate_ucb_objective_is_rejected(self) -> None:
        target = self._target_load()
        target["objective"] = {
            **deepcopy(authority.FINAL_OBJECTIVE),
            "loss_basis": "total_loss_ucb_w",
        }
        self._write_contract(target_load=target)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "objective"):
            authority.load_authority_context(self.contract_path)

    def test_subset_or_incorrect_worst_case_bound_is_rejected(self) -> None:
        for field, value in (("allow_subset", True), ("worst_case_fea_bound", 431)):
            with self.subTest(field=field):
                target = self._target_load()
                target["candidate_scope"][field] = value  # type: ignore[index]
                self._write_contract(target_load=target)
                with self.assertRaises(authority.TargetLoadAuthorityError):
                    authority.load_authority_context(self.contract_path)

    def test_integer_beta_offset_cannot_alias_the_confirmed_float(self) -> None:
        target = self._target_load()
        target["beta_validation"]["offset_deg"] = 2  # type: ignore[index]
        self._write_contract(target_load=target)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "beta validation offset"):
            authority.load_authority_context(self.contract_path)

    def test_y_drive_pyaedt_candidate_is_rejected_before_it_is_read(self) -> None:
        target = self._target_load()
        target["pyaedt_core_snapshot"] = {
            "path": "Y:/git/pyaedt_library/src/pyaedt_module/core/pydesktop.py",
            "sha256": "d" * 64,
            "single_link_required": True,
        }
        self._write_contract(target_load=target)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "C-local"):
            authority.load_authority_context(self.contract_path)

    def test_confirmation_requires_every_new_acknowledgement(self) -> None:
        self._write_contract()
        context = authority.load_authority_context(self.contract_path)
        declaration = authority.declaration_template(context)
        declaration["authority"] = {
            "confirmed_by": "operator-1",
            "confirmed_at_utc": self._now_utc(),
            "evidence_reference": "change-control-42",
            "attestation_kind": authority.ATTESTATION_KIND,
        }
        declaration["acknowledgements"] = {
            name: True for name in authority.ACKNOWLEDGEMENT_FIELDS
        }
        declaration["acknowledgements"]["measured_efficiency_objective_confirmed"] = False
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "explicitly true"):
            authority.build_confirmation(context, declaration, declaration_sha256=SHA)

    def test_receipt_audit_fails_after_confirmation_bytes_change(self) -> None:
        self._write_contract()
        self._publish_positive_chain()
        document = json.loads(self.confirmation_path.read_text(encoding="utf-8"))
        document["authority"]["confirmed_by"] = "somebody-else"
        self.confirmation_path.write_bytes(authority.canonical_json_bytes(document))
        with self.assertRaises(authority.TargetLoadAuthorityError):
            authority.audit_authorization_receipt(self.contract_path)

    def test_no_replace_publication_rejects_existing_destination(self) -> None:
        destination = self.root / "fresh.json"
        authority.publish_canonical_no_replace({"value": 1}, destination)
        identity = authority.read_single_link_snapshot(destination, "existing output").identity
        repeated = authority.publish_canonical_no_replace({"value": 1}, destination)
        self.assertEqual(repeated.outcome, "already_present")
        with self.assertRaisesRegex(
            authority.TargetLoadAuthorityError,
            "foreign publication|existing output differs",
        ):
            authority.publish_canonical_no_replace({"value": 2}, destination)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {"value": 1})
        self.assertEqual(
            authority.read_single_link_snapshot(destination, "existing output").identity,
            identity,
        )

    def test_unknown_schema_field_and_power_mismatch_are_rejected(self) -> None:
        for mutation, message in (
            (lambda target: target.update({"unknown": True}), "target_load fields mismatch"),
            (
                lambda target: target["operating_points"][0].update(  # type: ignore[index]
                    {"required_power_w": 6200.0}
                ),
                "violates P",
            ),
        ):
            with self.subTest(message=message):
                target = self._target_load()
                mutation(target)
                self._write_contract(target_load=target)
                with self.assertRaisesRegex(authority.TargetLoadAuthorityError, message):
                    authority.load_authority_context(self.contract_path)

    def test_operating_point_and_weight_names_are_exact_safe_identifiers(self) -> None:
        for mutation in (
            lambda target: target["operating_points"][0].update(  # type: ignore[index]
                {"name": " rated_torque"}
            ),
            lambda target: target["operating_points"][0].update(  # type: ignore[index]
                {"name": "rated torque"}
            ),
            lambda target: target["duty_cycle"]["weights"][0].update(  # type: ignore[index]
                {"name": "rated_torque "}
            ),
        ):
            with self.subTest(mutation=mutation):
                target = self._target_load()
                mutation(target)
                self._write_contract(target_load=target)
                with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "A-Za-z0-9"):
                    authority.load_authority_context(self.contract_path)

    def test_live_base_four_hash_binding_is_not_advisory(self) -> None:
        self._write_contract()
        self._rewrite_contract(
            lambda document: document["pipeline"]["base_v4r5_contract"].update(  # type: ignore[index]
                {"raw_sha256": "f" * 64}
            )
        )
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "live strict JSON"):
            authority.load_authority_context(self.contract_path)

    def test_outputs_stay_under_contract_parent_and_y_is_rejected_without_io(self) -> None:
        self._write_contract()
        outside = self.root.parent / "outside-target-load.json"
        self._rewrite_contract(
            lambda document: document["pipeline"]["target_load_confirmation"].update(  # type: ignore[index]
                {"declaration_path": str(outside)}
            )
        )
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "contract parent"):
            authority.load_authority_context(self.contract_path)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "C-local"):
            authority.publish_canonical_no_replace(
                {"must_not_write": True}, Path("Y:/codex-must-not-write.json")
            )

    def test_fake_authorizer_executable_is_rejected(self) -> None:
        self._write_contract()
        fake = self.root / "fake-python.exe"
        fake.write_bytes(b"not python")
        fake_hash = hashlib.sha256(fake.read_bytes()).hexdigest()

        def mutate(document: dict[str, object]) -> None:
            config = document["pipeline"]["target_load_confirmation"]  # type: ignore[index]
            config["authorizer_executable"] = {"path": str(fake), "sha256": fake_hash}
            config["authorizer_argv"][0] = str(fake)

        self._rewrite_contract(mutate)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "sys.executable"):
            authority.load_authority_context(self.contract_path)

    def test_missing_or_reparse_upstream_results_directory_is_rejected(self) -> None:
        self._write_contract()
        results = self.root / "pareto_fea" / "results"
        hidden_results = self.root / "pareto_fea" / "results-hidden"
        results.rename(hidden_results)
        try:
            with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "cannot inspect"):
                authority.load_authority_context(self.contract_path)
        finally:
            hidden_results.rename(results)
        link_parent = self.root / "linked-parent"
        try:
            os.symlink(self.root, link_parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable on this host")
        linked_results = link_parent / "pareto_fea" / "results"
        target = self._target_load()
        target["upstream_results"]["pareto_fea_results_dir"] = str(linked_results)  # type: ignore[index]
        self._write_contract(target_load=target)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "reparse"):
            authority.load_authority_context(self.contract_path)

    def test_upstream_and_per_case_bytes_are_live_manifest_bound(self) -> None:
        self._write_contract()
        original_artifact = self.upstream_artifact.read_bytes()
        self.upstream_artifact.write_bytes(b"tampered model bytes\n")
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "live file"):
            authority.load_authority_context(self.contract_path)
        self.upstream_artifact.write_bytes(original_artifact)

        original_builder = self.builder_source.read_bytes()
        self.builder_source.write_bytes(b"# changed builder source\n")
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "builder source"):
            authority.load_authority_context(self.contract_path)
        self.builder_source.write_bytes(original_builder)

        first = self.per_case_records[0]
        result_path = self.results_dir / str(first["relative_path"])
        original_result = result_path.read_bytes()
        result_path.write_bytes(original_result + b"tamper")
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "live file"):
            authority.load_authority_context(self.contract_path)

    def test_manifest_digest_and_protected_output_subtree_are_rejected(self) -> None:
        target = self._target_load()
        target["upstream_results"]["per_case_results_manifest_sha256"] = "f" * 64  # type: ignore[index]
        self._write_contract(target_load=target)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "manifest SHA256"):
            authority.load_authority_context(self.contract_path)

        target = self._target_load()
        first = target["upstream_results"]["per_case_results"][0]  # type: ignore[index]
        second = target["upstream_results"]["per_case_results"][1]  # type: ignore[index]
        second["relative_path"] = str(first["relative_path"]).upper()
        second["size"] = first["size"]
        second["sha256"] = first["sha256"]
        target["upstream_results"]["per_case_results_manifest_sha256"] = (  # type: ignore[index]
            authority.canonical_sha256(
                {
                    "schema_version": authority.PER_CASE_RESULTS_MANIFEST_SCHEMA_VERSION,
                    "results": target["upstream_results"]["per_case_results"],  # type: ignore[index]
                }
            )
        )
        self._write_contract(target_load=target)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "unique CSV"):
            authority.load_authority_context(self.contract_path)

        self._write_contract()
        nested = self.results_dir / "authority.json"

        def mutate(document: dict[str, object]) -> None:
            config = document["pipeline"]["target_load_confirmation"]  # type: ignore[index]
            config["declaration_path"] = str(nested)
            argv = config["authorizer_argv"]
            argv[argv.index("--declaration") + 1] = str(nested)

        self._rewrite_contract(mutate)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "protected input"):
            authority.load_authority_context(self.contract_path)

    def test_legacy_hardlink_late_success_is_preserved_fail_closed(self) -> None:
        document = {"value": 7}
        destination = self.root / "late-success.json"
        payload = authority.canonical_json_bytes(document)
        staged, proof = authority._publication_paths(destination, payload)
        staged.write_bytes(payload)
        publish_receipt = authority.atomic_publish.publish_no_replace(
            staged, destination, proof_path=proof
        )
        identity = publish_receipt.identity
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "manual recovery"):
            authority.publish_canonical_no_replace(document, destination)
        self.assertTrue(staged.exists())
        self.assertTrue(proof.exists())
        self.assertEqual(authority.atomic_publish.FileIdentity.from_path(destination), identity)

    def test_commit_then_exception_is_adopted_as_late_success(self) -> None:
        document = {"value": 8}
        destination = self.root / "late-exception.json"
        original = authority.atomic_publish._windows_rename_no_replace

        def commit_then_raise(*args: object, **kwargs: object) -> object:
            original(*args, **kwargs)
            raise OSError("simulated post-commit exception")

        with mock.patch.object(
            authority.atomic_publish,
            "_windows_rename_no_replace",
            side_effect=commit_then_raise,
        ):
            result = authority.publish_canonical_no_replace(document, destination)
        self.assertEqual(result.outcome, "published")
        self.assertEqual(destination.read_bytes(), authority.canonical_json_bytes(document))

    def test_partial_or_foreign_proof_is_fail_closed_and_preserved(self) -> None:
        document = {"value": 9}
        payload = authority.canonical_json_bytes(document)
        destination = self.root / "partial-proof.json"
        staged, proof = authority._publication_paths(destination, payload)
        staged.write_bytes(payload)
        identity = authority.atomic_publish.FileIdentity.from_path(staged)
        expected_proof = authority._expected_proof_bytes(staged, destination, identity)
        proof.write_bytes(expected_proof[:17])
        with self.assertRaises(authority.TargetLoadAuthorityError):
            authority.publish_canonical_no_replace(document, destination)
        self.assertEqual(staged.read_bytes(), payload)
        self.assertEqual(proof.read_bytes(), expected_proof[:17])

        foreign_destination = self.root / "foreign-proof.json"
        foreign_staged, foreign_proof = authority._publication_paths(
            foreign_destination, payload
        )
        foreign_staged.write_bytes(payload)
        foreign_proof.write_bytes(b"foreign proof bytes")
        with self.assertRaises(authority.TargetLoadAuthorityError):
            authority.publish_canonical_no_replace(document, foreign_destination)
        self.assertEqual(foreign_staged.read_bytes(), payload)
        self.assertEqual(foreign_proof.read_bytes(), b"foreign proof bytes")

    def test_use_before_publish_race_preserves_visible_proof_authority(self) -> None:
        self._write_contract()
        context = authority.load_authority_context(self.contract_path)
        declaration = self._confirmed_declaration(context)
        authority.publish_canonical_no_replace(
            declaration, self.declaration_path, context=context
        )
        declaration_snapshot = authority.read_single_link_snapshot(
            self.declaration_path, "declaration before race"
        )
        confirmation = authority.build_confirmation(
            context,
            declaration,
            declaration_sha256=declaration_snapshot.sha256,
        )
        original = authority.atomic_publish._windows_rename_no_replace

        def mutate_authority_after_commit(*args: object, **kwargs: object) -> object:
            receipt = original(*args, **kwargs)
            self.declaration_path.write_bytes(b"{}\n")
            return receipt

        with mock.patch.object(
            authority.atomic_publish,
            "_windows_rename_no_replace",
            side_effect=mutate_authority_after_commit,
        ):
            with self.assertRaises(authority.TargetLoadAuthorityError):
                authority.publish_canonical_no_replace(
                    confirmation,
                    self.confirmation_path,
                    context=context,
                    additional_snapshots=(declaration_snapshot,),
                )
        self.assertTrue(self.confirmation_path.exists())
        payload = authority.canonical_json_bytes(confirmation)
        staged, proof = authority._publication_paths(self.confirmation_path, payload)
        self.assertFalse(staged.exists())
        self.assertTrue(proof.exists())

    def test_same_size_stage_tamper_is_rejected_before_resume_link(self) -> None:
        document = {"value": 10}
        payload = authority.canonical_json_bytes(document)
        destination = self.root / "tampered-stage.json"
        staged, proof = authority._publication_paths(destination, payload)
        staged.write_bytes(payload)
        identity = authority.atomic_publish.FileIdentity.from_path(staged)
        proof.write_bytes(authority._expected_proof_bytes(staged, destination, identity))
        bad_payload = b"X" * len(payload)
        original = authority._resume_proven_commit

        def tamper_then_resume(*args: object, **kwargs: object) -> object:
            staged.write_bytes(bad_payload)
            return original(*args, **kwargs)

        with mock.patch.object(
            authority, "_resume_proven_commit", side_effect=tamper_then_resume
        ):
            with self.assertRaises(authority.TargetLoadAuthorityError):
                authority.recover_canonical_publication(document, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(staged.read_bytes(), bad_payload)
        self.assertTrue(proof.exists())

    def test_output_tamper_is_rejected_while_proof_evidence_is_retained(self) -> None:
        document = {"value": 11}
        payload = authority.canonical_json_bytes(document)
        destination = self.root / "tampered-output.json"
        staged, proof = authority._publication_paths(destination, payload)
        staged.write_bytes(payload)
        authority.atomic_publish.publish_no_replace(staged, destination, proof_path=proof)
        staged.unlink()
        bad_payload = b"Z" * len(payload)
        destination.write_bytes(bad_payload)
        with self.assertRaises(authority.TargetLoadAuthorityError):
            authority.recover_canonical_publication(document, destination)
        self.assertEqual(destination.read_bytes(), bad_payload)
        self.assertTrue(proof.exists())

    def test_concurrent_adopter_prevents_losing_writer_from_rollback(self) -> None:
        document = {"value": 12}
        destination = self.root / "concurrent-adopter.json"
        extra = self.root / "additional-authority.txt"
        extra.write_bytes(b"before")
        extra_snapshot = authority.read_single_link_snapshot(extra, "additional authority")
        original = authority.atomic_publish._windows_rename_no_replace
        adopted: list[authority.PublicationResult | None] = []

        def publish_adopt_then_invalidate(*args: object, **kwargs: object) -> object:
            receipt = original(*args, **kwargs)
            adopted.append(authority.recover_canonical_publication(document, destination))
            extra.write_bytes(b"after!")
            return receipt

        with mock.patch.object(
            authority.atomic_publish,
            "_windows_rename_no_replace",
            side_effect=publish_adopt_then_invalidate,
        ):
            with self.assertRaises(authority.TargetLoadAuthorityError):
                authority.publish_canonical_no_replace(
                    document,
                    destination,
                    additional_snapshots=(extra_snapshot,),
                )
        self.assertEqual(adopted[0].outcome, "already_present")  # type: ignore[union-attr]
        self.assertEqual(destination.read_bytes(), authority.canonical_json_bytes(document))

    def test_initial_stage_o_excl_race_is_foreign_and_fail_closed(self) -> None:
        document = {"value": 13}
        payload = authority.canonical_json_bytes(document)
        destination = self.root / "stage-race.json"
        staged, _ = authority._publication_paths(destination, payload)
        original_open = authority.os.open
        injected = False

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal injected
            if Path(path) == staged and flags & os.O_EXCL and not injected:
                injected = True
                staged.write_bytes(payload)
                raise FileExistsError("simulated exact stage race")
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(authority.os, "open", side_effect=racing_open):
            with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "foreign"):
                authority.publish_canonical_no_replace(document, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(staged.read_bytes(), payload)

    def test_precreated_exact_proofless_stage_is_preserved_without_output(self) -> None:
        document = {"value": 16}
        payload = authority.canonical_json_bytes(document)
        destination = self.root / "precreated-stage.json"
        staged, _ = authority._publication_paths(destination, payload)
        staged.write_bytes(payload)
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "current-publisher"):
            authority.publish_canonical_no_replace(document, destination)
        self.assertEqual(staged.read_bytes(), payload)
        self.assertFalse(destination.exists())

    def test_partial_stage_and_noncanonical_complete_proof_are_preserved(self) -> None:
        document = {"value": 14}
        payload = authority.canonical_json_bytes(document)
        partial_destination = self.root / "partial-stage.json"
        partial_stage, _ = authority._publication_paths(partial_destination, payload)
        partial_stage.write_bytes(payload[:5])
        with self.assertRaises(authority.TargetLoadAuthorityError):
            authority.publish_canonical_no_replace(document, partial_destination)
        self.assertEqual(partial_stage.read_bytes(), payload[:5])

        destination = self.root / "noncanonical-proof.json"
        staged, proof = authority._publication_paths(destination, payload)
        staged.write_bytes(payload)
        identity = authority.atomic_publish.FileIdentity.from_path(staged)
        proof_document = json.loads(
            authority._expected_proof_bytes(staged, destination, identity).decode("utf-8")
        )
        proof.write_text(json.dumps(proof_document), encoding="utf-8")
        with self.assertRaisesRegex(authority.TargetLoadAuthorityError, "exact atomic format"):
            authority.publish_canonical_no_replace(document, destination)
        self.assertTrue(staged.exists())
        self.assertTrue(proof.exists())

    def test_directory_fsync_hook_covers_publication_transitions(self) -> None:
        destination = self.root / "fsync-transitions.json"
        with mock.patch.object(
            authority,
            "_fsync_directory",
            wraps=authority._fsync_directory,
        ) as fsync:
            authority.publish_canonical_no_replace({"value": 15}, destination)
        self.assertGreaterEqual(fsync.call_count, 3)

    def test_authorize_main_rejects_reordered_but_parseable_argv(self) -> None:
        self._write_contract()
        reordered = [
            "authorize",
            "--declaration",
            str(self.declaration_path),
            "--contract",
            str(self.contract_path),
            "--confirmation",
            str(self.confirmation_path),
            "--authorization-receipt",
            str(self.receipt_path),
            "--execute",
        ]
        errors = io.StringIO()
        with redirect_stderr(errors):
            status = authority.main(reordered)
        self.assertEqual(status, 2)
        self.assertIn("exact direct contracted process argv", errors.getvalue())

        context = authority.load_authority_context(self.contract_path)
        errors = io.StringIO()
        with redirect_stderr(errors):
            exact_programmatic = authority.main(list(context.authorizer_argv[2:]))
        self.assertEqual(exact_programmatic, 2)
        self.assertIn("exact direct contracted process argv", errors.getvalue())

    def test_authorize_subprocess_requires_exact_direct_argv(self) -> None:
        self._write_contract()
        context = authority.load_authority_context(self.contract_path)
        declaration = self._confirmed_declaration(context)
        authority.publish_canonical_no_replace(
            declaration, self.declaration_path, context=context
        )
        declaration_snapshot = authority.read_single_link_snapshot(
            self.declaration_path, "subprocess declaration"
        )
        confirmation = authority.build_confirmation(
            context,
            declaration,
            declaration_sha256=declaration_snapshot.sha256,
        )
        authority.publish_canonical_no_replace(
            confirmation,
            self.confirmation_path,
            context=context,
            additional_snapshots=(declaration_snapshot,),
        )
        exact = list(context.authorizer_argv)
        bypass_b = [exact[0], "-B", *exact[1:]]
        bypass_code = (
            "import runpy,sys;"
            f"sys.argv={exact[1:]!r};"
            f"runpy.run_path({exact[1]!r},run_name='__main__')"
        )
        bypass_c = [exact[0], "-c", bypass_code]
        reordered = exact[:2] + [
            "authorize",
            "--declaration",
            str(self.declaration_path),
            "--contract",
            str(self.contract_path),
            "--confirmation",
            str(self.confirmation_path),
            "--authorization-receipt",
            str(self.receipt_path),
            "--execute",
        ]
        for bypass in (bypass_b, bypass_c):
            rejected_bypass = subprocess.run(
                bypass,
                cwd=Path(authority.__file__).resolve().parent,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(rejected_bypass.returncode, 2)
            self.assertFalse(self.receipt_path.exists())
        rejected = subprocess.run(
            reordered,
            cwd=Path(authority.__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertFalse(self.receipt_path.exists())
        accepted = subprocess.run(
            exact,
            cwd=Path(authority.__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(self.receipt_path.exists())

    def test_committed_return_rechecks_output_and_additional_authority(self) -> None:
        document = {"value": 17}
        payload = authority.canonical_json_bytes(document)
        destination = self.root / "committed-final-recheck.json"
        destination.write_bytes(payload)
        bad_payload = b"Q" * len(payload)
        original = authority._publication_payload
        calls = 0

        def mutate_after_first_read(*args: object, **kwargs: object) -> object:
            nonlocal calls
            result = original(*args, **kwargs)
            if Path(args[0]) == destination:
                calls += 1
                if calls == 1:
                    destination.write_bytes(bad_payload)
            return result

        with mock.patch.object(
            authority, "_publication_payload", side_effect=mutate_after_first_read
        ):
            with self.assertRaises(authority.TargetLoadAuthorityError):
                authority.inspect_canonical_publication(document, destination)
        self.assertEqual(destination.read_bytes(), bad_payload)

        destination.write_bytes(payload)
        additional = self.root / "committed-additional.txt"
        additional.write_bytes(b"before")
        snapshot = authority.read_single_link_snapshot(additional, "committed additional")
        calls = 0

        def mutate_additional_after_first_read(*args: object, **kwargs: object) -> object:
            nonlocal calls
            result = original(*args, **kwargs)
            if Path(args[0]) == destination:
                calls += 1
                if calls == 1:
                    additional.write_bytes(b"after!")
            return result

        with mock.patch.object(
            authority,
            "_publication_payload",
            side_effect=mutate_additional_after_first_read,
        ):
            with self.assertRaises(authority.TargetLoadAuthorityError):
                authority.inspect_canonical_publication(
                    document,
                    destination,
                    additional_snapshots=(snapshot,),
                )

    def test_rename_then_corrupt_never_returns_authorized_commit(self) -> None:
        document = {"value": 18}
        payload = authority.canonical_json_bytes(document)
        destination = self.root / "rename-corrupt.json"
        bad_payload = b"R" * len(payload)
        original = authority.atomic_publish._windows_rename_no_replace

        def rename_then_corrupt(*args: object, **kwargs: object) -> None:
            original(*args, **kwargs)
            destination.write_bytes(bad_payload)

        with mock.patch.object(
            authority.atomic_publish,
            "_windows_rename_no_replace",
            side_effect=rename_then_corrupt,
        ):
            with self.assertRaises(authority.TargetLoadAuthorityError):
                authority.publish_canonical_no_replace(document, destination)
        self.assertEqual(destination.read_bytes(), bad_payload)
        _, proof = authority._publication_paths(destination, payload)
        self.assertTrue(proof.exists())

    def test_receipt_publication_binds_declaration_and_confirmation_snapshots(self) -> None:
        self._write_contract()
        context = authority.load_authority_context(self.contract_path)
        declaration = self._confirmed_declaration(context)
        authority.publish_canonical_no_replace(
            declaration, self.declaration_path, context=context
        )
        declaration_snapshot = authority.read_single_link_snapshot(
            self.declaration_path, "receipt declaration"
        )
        confirmation = authority.build_confirmation(
            context,
            declaration,
            declaration_sha256=declaration_snapshot.sha256,
        )
        authority.publish_canonical_no_replace(
            confirmation,
            self.confirmation_path,
            context=context,
            additional_snapshots=(declaration_snapshot,),
        )
        confirmation_audit = authority.audit_confirmation(context)
        receipt = authority.build_authorization_receipt(context, confirmation_audit)
        original = authority.atomic_publish._windows_rename_no_replace

        def mutate_declaration_after_receipt_commit(*args: object, **kwargs: object) -> object:
            published = original(*args, **kwargs)
            self.declaration_path.write_bytes(b"{}\n")
            return published

        with mock.patch.object(
            authority.atomic_publish,
            "_windows_rename_no_replace",
            side_effect=mutate_declaration_after_receipt_commit,
        ):
            with self.assertRaises(authority.TargetLoadAuthorityError):
                authority.publish_canonical_no_replace(
                    receipt,
                    self.receipt_path,
                    context=context,
                    additional_snapshots=(
                        confirmation_audit.declaration_snapshot,
                        confirmation_audit.snapshot,
                    ),
                )
        self.assertTrue(self.receipt_path.exists())


if __name__ == "__main__":
    unittest.main()
