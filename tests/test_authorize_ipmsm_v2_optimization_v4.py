from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from atomic_publish import publish_no_replace
import authorize_ipmsm_v2_optimization_v4 as authorizer
import confirm_ipmsm_v2_optimization_inputs as confirmation
import ipmsm_optimization as optimization
from tests.test_supervise_ipmsm_v2_pipeline_v4 import Fixture as SupervisorV4Fixture


def spec_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "_assumptions": {
            "duty_weights_are_equal_until_a_real_drive_cycle_is_supplied": True,
            "winding_requires_manufacturing_confirmation": True,
        },
        "operating_points": [
            {
                "name": "rated_torque",
                "speed_rpm": 1200,
                "target_torque_nm": 40,
                "duty_weight": 0.4,
            },
            {
                "name": "rated_power",
                "speed_rpm": 3000,
                "target_power_w": 5000,
                "duty_weight": 0.6,
            },
        ],
        "stack_length_bounds_mm": [40, 60],
        "inverter": {
            "vdc_v": 300,
            "phase_peak_current_limit_a": 140,
            "voltage_utilization": 0.95,
        },
        "winding": {
            "series_turns_per_phase": 48,
            "turns_per_coil_side": 12,
            "coils_per_phase": 4,
            "parallel_branches": 1,
            "strand_area_mm2": 0.5,
            "strands_per_turn": 4,
            "fill_factor": 0.8,
            "end_turn_factor": 1.2,
            "overhang_mm": 5,
        },
        "constraints": {"current_density_limit_a_per_mm2": 20},
        "beta_calibration": {
            "electrical_zero_deg": 12.5,
            "calibration_id": "fixture-calibration",
            "convention": "dq_current_advance_v2",
        },
    }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OptimizationAuthorizationV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.contract = self.root / "pipeline_v4.json"
        self.base_contract = self.root / "pipeline_v3.json"
        self.spec_path = self.root / "optimization_spec.json"
        self.implementation = self.root / "ipmsm_optimization.py"
        self.declaration = self.root / "optimization_declaration.json"
        self.confirmation = self.root / "optimization_confirmation.json"
        self.receipt = self.root / "optimization_authorization_receipt.json"
        self.contract.write_bytes(
            authorizer.canonical_json_bytes(
                {"schema_version": "fixture-v4", "contract_sha256": "a" * 64}
            )
        )
        self.base_contract.write_bytes(
            authorizer.canonical_json_bytes(
                {"schema_version": "fixture-v3", "contract_sha256": "b" * 64}
            )
        )
        self.spec_path.write_bytes(authorizer.canonical_json_bytes(spec_mapping()))
        self.implementation.write_text("# immutable optimization fixture\n", encoding="utf-8")
        self.write_authority()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def context(self) -> confirmation.BoundContext:
        mapping = spec_mapping()
        return confirmation.BoundContext(
            contract_path=self.contract,
            contract_file_sha256=file_sha256(self.contract),
            contract_sha256="a" * 64,
            spec_path=self.spec_path,
            spec_sha256=file_sha256(self.spec_path),
            spec_canonical_sha256=confirmation.canonical_sha256(mapping),
            spec=optimization.optimization_spec_from_mapping(mapping),
            spec_assumptions=mapping["_assumptions"],
            implementation_path=self.implementation,
            implementation_sha256=file_sha256(self.implementation),
            snapshots=(
                confirmation.read_stable_snapshot(self.contract, "v4 contract"),
                confirmation.read_stable_snapshot(self.spec_path, "spec"),
                confirmation.read_stable_snapshot(self.implementation, "implementation"),
            ),
        )

    def declaration_mapping(self, *, evidence: str = "requirements/MOTOR-42") -> dict[str, object]:
        value = confirmation.declaration_template(self.context())
        value["authority"] = {
            "confirmed_by": "motor-owner@example.test",
            "confirmed_at_utc": "2026-07-12T01:02:03+09:00",
            "evidence_reference": evidence,
            "attestation_kind": confirmation.ATTESTATION_KIND,
        }
        value["confirmed_inputs"]["duty_cycle"]["basis"] = "owner-approved MOTOR-42"
        value["acknowledgements"] = {
            name: True for name in confirmation.ACKNOWLEDGEMENT_FIELDS
        }
        return value

    def write_authority(self, *, evidence: str = "requirements/MOTOR-42") -> None:
        declaration = self.declaration_mapping(evidence=evidence)
        self.declaration.write_bytes(confirmation.canonical_json_bytes(declaration))
        document = confirmation.build_confirmation(
            self.context(),
            declaration,
            declaration_path=self.declaration,
            declaration_sha256=file_sha256(self.declaration),
        )
        self.confirmation.write_bytes(confirmation.canonical_json_bytes(document))

    def state(self, *, receipt: Path | None = None, confirmation_path: Path | None = None):
        context = self.context()
        config = SimpleNamespace(
            declaration=self.declaration,
            confirmation=confirmation_path or self.confirmation,
            receipt=receipt or self.receipt,
        )
        contract = SimpleNamespace(
            source=self.contract,
            source_sha256=file_sha256(self.contract),
            canonical_sha256=authorizer.supervisor_v4.v3._canonical_sha256(
                json.loads(self.contract.read_text(encoding="utf-8"))
            ),
            contract_sha256="a" * 64,
            base_contract=SimpleNamespace(
                source=self.base_contract,
                contract_sha256="b" * 64,
            ),
            base_contract_binding=SimpleNamespace(
                path=self.base_contract,
                sha256=file_sha256(self.base_contract),
                canonical_sha256=authorizer.supervisor_v4.v3._canonical_sha256(
                    json.loads(self.base_contract.read_text(encoding="utf-8"))
                ),
                contract_sha256="b" * 64,
            ),
            optimization_confirmation=config,
        )
        authorizer_snapshot = authorizer.read_secure_snapshot(
            Path(authorizer.__file__).resolve(), "authorization helper"
        )
        return authorizer._AuthorityState(
            contract=contract,
            confirmation_context=context,
            config=config,
            contract_snapshot=authorizer.read_secure_snapshot(self.contract, "v4 contract"),
            contract_document=json.loads(self.contract.read_text(encoding="utf-8")),
            base_contract_snapshot=authorizer.read_secure_snapshot(
                self.base_contract, "base contract"
            ),
            base_contract_document=json.loads(self.base_contract.read_text(encoding="utf-8")),
            spec_snapshot=authorizer.read_secure_snapshot(self.spec_path, "spec"),
            spec_document=spec_mapping(),
            implementation_snapshot=authorizer.read_secure_snapshot(
                self.implementation, "implementation"
            ),
            helper_snapshot=authorizer.read_secure_snapshot(
                Path(confirmation.__file__).resolve(), "confirmation helper"
            ),
            authorizer_snapshot=authorizer_snapshot,
            immutable_snapshots=(),
        )

    @contextmanager
    def state_patch(self, **kwargs):
        def audit(path, _contract):
            return confirmation._audit_confirmation_with_context(path, self.context())

        with mock.patch.object(
            authorizer,
            "_load_authority_state",
            side_effect=lambda _path: self.state(**kwargs),
        ), mock.patch.object(confirmation, "audit_confirmation", side_effect=audit):
            yield

    def inspect(self) -> authorizer.AuthorizationInspection:
        with self.state_patch():
            inspection = authorizer.inspect_authorization(
                self.contract, self.confirmation, self.receipt
            )
        self.assertIsNotNone(inspection)
        return inspection

    def publish(self) -> authorizer.AuthorizationAudit:
        with self.state_patch():
            inspection = authorizer.inspect_authorization(
                self.contract, self.confirmation, self.receipt
            )
            self.assertIsNotNone(inspection)
            return authorizer.publish_authorization_receipt(
                inspection,
                contract_path=self.contract,
                confirmation_path=self.confirmation,
            )

    def inspection_for(self, output: Path) -> authorizer.AuthorizationInspection:
        with self.state_patch(receipt=output):
            inspection = authorizer.inspect_authorization(
                self.contract, self.confirmation, output
            )
        self.assertIsNotNone(inspection)
        return inspection

    def pending_publication(
        self,
        state: str,
        *,
        output_name: str,
    ) -> tuple[
        authorizer.AuthorizationInspection,
        Path,
        Path,
        Path,
        authorizer.FileIdentity | None,
    ]:
        output = self.root / output_name
        inspection = self.inspection_for(output)
        payload = authorizer.canonical_json_bytes(inspection.document)
        attempt_path = authorizer.authorization_attempt_path(output, payload)
        attempt = authorizer._create_authorization_attempt(attempt_path)
        staged = authorizer.authorization_staged_path(output, payload)
        proof = authorizer.authorization_proof_path(output)
        identity = None
        if state != "pre_stage":
            staged.write_bytes(
                b'{"partial":' if state == "pre_stage_incomplete" else payload
            )
            identity = authorizer.FileIdentity.from_path(staged)
        if state not in {"pre_stage", "pre_stage_incomplete"}:
            attempt = authorizer._create_authorization_stage_ready(attempt)
            self.assertTrue(attempt.stage_ready)
            assert identity is not None
        if state == "pre_commit_proof_incomplete":
            expected = authorizer._proof_json_bytes(
                {
                    "schema_version": authorizer.PROOF_SCHEMA_VERSION,
                    "source": str(staged),
                    "destination": str(output),
                    "identity": identity.as_mapping(),
                }
            )
            proof.write_bytes(expected[: max(1, len(expected) // 3)])
        elif state in {
            "pre_commit_proven",
            "post_commit_stage_linked",
            "post_commit_stage_unlinked",
        }:
            assert identity is not None
            authorizer.atomic_publish._write_proof_exclusive(
                proof,
                source=staged,
                destination=output,
                identity=identity,
            )
            if state in {"post_commit_stage_linked", "post_commit_stage_unlinked"}:
                try:
                    os.link(staged, output)
                except OSError as exc:
                    self.skipTest(f"hardlinks unavailable: {exc}")
            if state == "post_commit_stage_unlinked":
                staged.unlink()
        return inspection, attempt_path, staged, proof, identity

    def transaction_evidence(self, output: Path) -> dict[str, object]:
        prefix = f".{output.name}"
        evidence: dict[str, object] = {}
        for path in sorted(
            item for item in output.parent.rglob("*") if item.name.startswith(prefix)
            or any(parent.name.startswith(prefix) for parent in item.parents)
        ):
            info = os.lstat(path)
            relative = str(path.relative_to(output.parent))
            metadata = (
                int(info.st_dev),
                int(info.st_ino),
                int(info.st_size),
                int(info.st_mtime_ns),
                int(getattr(info, "st_nlink", 1)),
            )
            evidence[relative] = (
                "dir",
                metadata,
            ) if path.is_dir() else ("file", metadata, path.read_bytes())
        return evidence

    def test_missing_confirmation_waits_without_any_write(self) -> None:
        self.confirmation.unlink()
        absent_parent = self.root / "must-not-exist"
        output = absent_parent / "receipt.json"
        missing = self.root / "missing-confirmation.json"
        stream = io.StringIO()
        with self.state_patch(receipt=output, confirmation_path=missing), redirect_stdout(stream):
            code = authorizer.main(
                [
                    "--contract",
                    str(self.contract),
                    "--confirmation",
                    str(missing),
                    "--output",
                    str(output),
                    "--execute",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stream.getvalue())["status"], "waiting_for_optimization_confirmation")
        self.assertFalse(absent_parent.exists())

    def test_missing_confirmation_with_existing_receipt_fails_closed(self) -> None:
        self.confirmation.unlink()
        self.receipt.write_text("foreign receipt must remain\n", encoding="utf-8")
        before = self.receipt.read_bytes()
        missing = self.root / "missing-confirmation.json"
        with self.state_patch(confirmation_path=missing), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError,
            "confirmation is missing while an authorization receipt/proof exists",
        ):
            authorizer.main(
                [
                    "--contract",
                    str(self.contract),
                    "--confirmation",
                    str(missing),
                    "--output",
                    str(self.receipt),
                    "--execute",
                ]
            )
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_v4_loader_adapter_pins_loaded_helpers_and_embedded_base(self) -> None:
        contract_document = json.loads(self.contract.read_text(encoding="utf-8"))
        base_document = json.loads(self.base_contract.read_text(encoding="utf-8"))
        helper_path = Path(confirmation.__file__).resolve()
        authorizer_path = Path(authorizer.__file__).resolve()
        base_binding = SimpleNamespace(
            path=self.base_contract,
            sha256=file_sha256(self.base_contract),
            canonical_sha256=authorizer.supervisor_v4.v3._canonical_sha256(base_document),
            contract_sha256="b" * 64,
        )
        immutable = tuple(
            SimpleNamespace(path=path, sha256=file_sha256(path))
            for path in (self.base_contract, helper_path, authorizer_path)
        )
        fake_contract = SimpleNamespace(
            source=self.contract,
            source_sha256=file_sha256(self.contract),
            canonical_sha256=authorizer.supervisor_v4.v3._canonical_sha256(
                contract_document
            ),
            contract_sha256="a" * 64,
            base_contract=SimpleNamespace(
                source=self.base_contract,
                contract_sha256="b" * 64,
            ),
            base_contract_binding=base_binding,
            source_pins={
                "confirmation_helper": SimpleNamespace(
                    path=helper_path, sha256=file_sha256(helper_path)
                ),
                "optimization_authorizer_v4": SimpleNamespace(
                    path=authorizer_path, sha256=file_sha256(authorizer_path)
                ),
            },
            immutable_inputs=immutable,
            optimization_confirmation=SimpleNamespace(
                declaration=self.declaration,
                confirmation=self.confirmation,
                receipt=self.receipt,
            ),
        )
        with mock.patch.object(
            authorizer.supervisor_v4, "load_contract", return_value=fake_contract
        ), mock.patch.object(
            authorizer.supervisor_v4, "audit_contract"
        ), mock.patch.object(
            confirmation, "load_bound_context", return_value=self.context()
        ):
            state = authorizer._load_authority_state(self.contract)
        self.assertEqual(state.contract_snapshot.sha256, file_sha256(self.contract))
        self.assertEqual(state.base_contract_snapshot.sha256, file_sha256(self.base_contract))
        self.assertEqual(len(state.immutable_snapshots), 3)

    def test_real_v4_confirmation_authorizer_and_audit_cli_round_trip(self) -> None:
        class ValidOptimizationFixture(SupervisorV4Fixture):
            def _write_base(inner_self) -> None:
                super()._write_base()
                inner_self.root.joinpath("spec.json").write_bytes(
                    confirmation.canonical_json_bytes(spec_mapping())
                )
                document = json.loads(inner_self.base_path.read_text(encoding="utf-8"))
                implementation_path = inner_self.root / "ipmsm_optimization.py"
                document["pipeline"]["stage3"]["generate_argv"].extend(
                    ["--spec", "spec.json"]
                )
                document["pipeline"]["immutable_inputs"].extend(
                    [
                        {
                            "path": "spec.json",
                            "sha256": file_sha256(inner_self.root / "spec.json"),
                        },
                        {
                            "path": str(implementation_path),
                            "sha256": file_sha256(implementation_path),
                        },
                    ]
                )
                unsigned = {
                    "schema_version": authorizer.supervisor_v4.v3.CONTRACT_SCHEMA_VERSION,
                    "pipeline": document["pipeline"],
                }
                document = {
                    **unsigned,
                    "contract_sha256": authorizer.supervisor_v4.v3._canonical_sha256(
                        unsigned
                    ),
                }
                inner_self.base_path.write_text(
                    json.dumps(document, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8",
                )

        fixture_root = self.root / "integration"
        fixture_root.mkdir()
        fixture = ValidOptimizationFixture(fixture_root)
        fixture.declaration.parent.mkdir(parents=True)
        environment = dict(os.environ)
        repository = str(
            Path(
                fixture.document["pipeline"]["source_pins"]["supervisor_v3"]["path"]
            ).parent
        )
        environment["PYTHONPATH"] = os.pathsep.join(
            item
            for item in (repository, environment.get("PYTHONPATH", ""))
            if item
        )

        def run(script: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [sys.executable, str(script), *(str(item) for item in arguments)],
                cwd=fixture_root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result

        confirm_script = fixture_root / "confirm_ipmsm_v2_optimization_inputs.py"
        authorize_script = fixture_root / "authorize_ipmsm_v2_optimization_v4.py"
        template = json.loads(
            run(
                confirm_script,
                "--contract",
                fixture.v4_path,
                "--print-declaration-template",
            ).stdout
        )
        template["authority"] = {
            "confirmed_by": "integration-owner@example.test",
            "confirmed_at_utc": "2026-07-12T01:02:03+09:00",
            "evidence_reference": "integration/MOTOR-42",
            "attestation_kind": confirmation.ATTESTATION_KIND,
        }
        template["confirmed_inputs"]["duty_cycle"]["basis"] = "integration duty basis"
        template["acknowledgements"] = {
            name: True for name in confirmation.ACKNOWLEDGEMENT_FIELDS
        }
        fixture.declaration.write_bytes(confirmation.canonical_json_bytes(template))
        run(
            confirm_script,
            "--contract",
            fixture.v4_path,
            "--declaration",
            fixture.declaration,
            "--output",
            fixture.confirmation,
            "--execute",
        )
        dry_run = json.loads(
            run(
                authorize_script,
                "--contract",
                fixture.v4_path,
                "--confirmation",
                fixture.confirmation,
                "--output",
                fixture.receipt,
            ).stdout
        )
        self.assertEqual(dry_run["status"], "ready_to_authorize")
        execute = json.loads(
            run(
                authorize_script,
                "--contract",
                fixture.v4_path,
                "--confirmation",
                fixture.confirmation,
                "--output",
                fixture.receipt,
                "--execute",
            ).stdout
        )
        self.assertEqual(execute["writes_performed"], 1)
        self.assertTrue(execute["authorized"])
        replay = json.loads(
            run(
                authorize_script,
                "--contract",
                fixture.v4_path,
                "--confirmation",
                fixture.confirmation,
                "--audit-receipt",
                fixture.receipt,
            ).stdout
        )
        self.assertEqual(replay["receipt_sha256"], execute["receipt_sha256"])
        repeated = json.loads(
            run(
                authorize_script,
                "--contract",
                fixture.v4_path,
                "--confirmation",
                fixture.confirmation,
                "--output",
                fixture.receipt,
                "--execute",
            ).stdout
        )
        self.assertEqual(repeated["writes_performed"], 0)
        self.assertTrue(repeated["already_present"])

    def test_dry_run_and_publish_bind_every_authority_hash(self) -> None:
        inspection = self.inspect()
        self.assertFalse(self.receipt.exists())
        bindings = inspection.document["bindings"]
        self.assertEqual(bindings["contract"]["raw_sha256"], file_sha256(self.contract))
        self.assertEqual(
            bindings["declaration"]["canonical_sha256"],
            confirmation.canonical_sha256(json.loads(self.declaration.read_text(encoding="utf-8"))),
        )
        self.assertEqual(bindings["confirmation_helper"]["sha256"], file_sha256(Path(confirmation.__file__)))
        audit = self.publish()
        self.assertTrue(audit.authorized)
        self.assertEqual(audit.receipt_sha256, inspection.document["receipt_sha256"])
        self.assertEqual(audit.confirmed_by, "motor-owner@example.test")
        self.assertEqual(audit.duty_basis, "owner-approved MOTOR-42")
        self.assertEqual(audit.attestation_kind, confirmation.ATTESTATION_KIND)
        self.assertEqual(self.receipt.read_bytes(), authorizer.canonical_json_bytes(inspection.document))
        with self.state_patch():
            replay = authorizer.audit_authorization_receipt(
                self.receipt, self.contract, self.confirmation
            )
        self.assertEqual(replay.as_mapping(), audit.as_mapping())

    def test_mutation_between_inspect_and_commit_aborts_without_receipt(self) -> None:
        with self.state_patch():
            inspection = authorizer.inspect_authorization(
                self.contract, self.confirmation, self.receipt
            )
        self.assertIsNotNone(inspection)
        real_inspect = authorizer.inspect_authorization

        def mutate_then_inspect(*args, **kwargs):
            self.write_authority(evidence="requirements/CHANGED")
            return real_inspect(*args, **kwargs)

        with self.state_patch(), mock.patch.object(
            authorizer, "inspect_authorization", side_effect=mutate_then_inspect
        ):
            with self.assertRaisesRegex(
                authorizer.OptimizationAuthorizationError,
                "stale or untrusted|between inspect and commit",
            ):
                authorizer.publish_authorization_receipt(
                    inspection,
                    contract_path=self.contract,
                    confirmation_path=self.confirmation,
                )
        self.assertFalse(self.receipt.exists())
        self.assertFalse(authorizer.authorization_proof_path(self.receipt).exists())
        self.assertEqual(list(self.root.glob(f".{self.receipt.name}.*{authorizer.STAGED_SUFFIX}")), [])

    def test_hard_kill_after_commit_recovers_by_audit_without_replacing_receipt(self) -> None:
        inspection = self.inspect()
        staged = self.receipt.with_name(
            f".{self.receipt.name}.hardkill{authorizer.STAGED_SUFFIX}"
        )
        staged.write_bytes(authorizer.canonical_json_bytes(inspection.document))
        proof = authorizer.authorization_proof_path(self.receipt)
        publish_no_replace(staged, self.receipt, proof_path=proof)
        before = os.stat(self.receipt, follow_symlinks=False)
        with self.state_patch():
            audit = authorizer.recover_authorization_publication(
                self.receipt, self.contract, self.confirmation
            )
        after = os.stat(self.receipt, follow_symlinks=False)
        self.assertIsNotNone(audit)
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertFalse(staged.exists())
        self.assertFalse(proof.exists())
        self.assertTrue(self.receipt.exists())

    def test_partial_stage_and_proof_kill_windows_are_read_only_then_recovered(self) -> None:
        for index, pending in enumerate(
            ("pre_stage", "pre_stage_incomplete", "pre_commit_proof_incomplete")
        ):
            with self.subTest(pending=pending):
                output = self.root / f"partial-{index}.json"
                self.pending_publication(pending, output_name=output.name)
                before = self.transaction_evidence(output)
                dry_stdout = io.StringIO()
                with self.state_patch(receipt=output), redirect_stdout(dry_stdout):
                    self.assertEqual(
                        authorizer.main(
                            [
                                "--contract",
                                str(self.contract),
                                "--confirmation",
                                str(self.confirmation),
                                "--output",
                                str(output),
                            ]
                        ),
                        0,
                    )
                dry = json.loads(dry_stdout.getvalue())
                self.assertEqual(dry["status"], "publication_recovery_pending")
                self.assertEqual(dry["pending_state"], pending)
                self.assertEqual(dry["writes_performed"], 0)
                self.assertEqual(self.transaction_evidence(output), before)

                execute_stdout = io.StringIO()
                with self.state_patch(receipt=output), redirect_stdout(execute_stdout):
                    self.assertEqual(
                        authorizer.main(
                            [
                                "--contract",
                                str(self.contract),
                                "--confirmation",
                                str(self.confirmation),
                                "--output",
                                str(output),
                                "--execute",
                            ]
                        ),
                        0,
                    )
                execute = json.loads(execute_stdout.getvalue())
                self.assertTrue(execute["recovered"])
                self.assertFalse(execute["already_present"])
                self.assertEqual(execute["writes_performed"], 1)
                self.assertTrue(output.is_file())
                self.assertEqual(
                    authorizer.authorization_proof_path(output).exists(), False
                )
                self.assertEqual(authorizer._attempt_candidates(output), ())
                self.assertEqual(authorizer._staged_candidates(output), ())

    def test_same_payload_two_publisher_race_converges_as_already_present(self) -> None:
        output = self.root / "same-payload-race.json"
        real_create = authorizer._create_authorization_attempt_if_absent
        interleaved = False
        winner_identity = None

        def let_winner_finish_then_create_loser_attempt(path: Path):
            nonlocal interleaved, winner_identity
            self.assertFalse(interleaved)
            interleaved = True
            winner_attempt = real_create(path)
            self.assertIsNotNone(winner_attempt)
            authorizer._recover_authorization_publication(
                output, self.contract, self.confirmation
            )
            winner_identity = authorizer.FileIdentity.from_path(output)
            loser_attempt = real_create(path)
            self.assertIsNotNone(loser_attempt)
            return loser_attempt

        stdout = io.StringIO()
        with self.state_patch(receipt=output), mock.patch.object(
            authorizer,
            "_create_authorization_attempt_if_absent",
            side_effect=let_winner_finish_then_create_loser_attempt,
        ), redirect_stdout(stdout):
            self.assertEqual(
                authorizer.main(
                    [
                        "--contract",
                        str(self.contract),
                        "--confirmation",
                        str(self.confirmation),
                        "--output",
                        str(output),
                        "--execute",
                    ]
                ),
                0,
            )
        result = json.loads(stdout.getvalue())
        self.assertTrue(interleaved)
        self.assertEqual(result["writes_performed"], 0)
        self.assertTrue(result["already_present"])
        self.assertFalse(result["recovered"])
        self.assertEqual(authorizer.FileIdentity.from_path(output), winner_identity)
        self.assertEqual(authorizer._attempt_candidates(output), ())
        self.assertEqual(authorizer._staged_candidates(output), ())
        self.assertFalse(authorizer.authorization_proof_path(output).exists())

        repeated_stdout = io.StringIO()
        with self.state_patch(receipt=output), redirect_stdout(repeated_stdout):
            self.assertEqual(
                authorizer.main(
                    [
                        "--contract",
                        str(self.contract),
                        "--confirmation",
                        str(self.confirmation),
                        "--output",
                        str(output),
                        "--execute",
                    ]
                ),
                0,
            )
        repeated = json.loads(repeated_stdout.getvalue())
        self.assertEqual(repeated["writes_performed"], 0)
        self.assertTrue(repeated["already_present"])

    def test_committed_empty_attempt_orphan_survives_cleanup_kill_but_rejects_foreign_content(self) -> None:
        output = self.root / "attempt-cleanup-kill.json"
        inspection = self.inspection_for(output)
        with self.state_patch(receipt=output):
            authorizer.publish_authorization_receipt(
                inspection,
                contract_path=self.contract,
                confirmation_path=self.confirmation,
            )
        payload = authorizer.canonical_json_bytes(inspection.document)
        attempt_path = authorizer.authorization_attempt_path(output, payload)
        attempt = authorizer._create_authorization_attempt(attempt_path)
        with self.state_patch(receipt=output):
            pending = authorizer.inspect_authorization_publication(
                output, self.contract, self.confirmation
            )
        self.assertEqual(pending.pending_state, "post_commit_attempt_orphan")
        before = self.transaction_evidence(output)
        dry_stdout = io.StringIO()
        with self.state_patch(receipt=output), redirect_stdout(dry_stdout):
            self.assertEqual(
                authorizer.main(
                    [
                        "--contract",
                        str(self.contract),
                        "--confirmation",
                        str(self.confirmation),
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
        dry = json.loads(dry_stdout.getvalue())
        self.assertEqual(dry["status"], "publication_recovery_pending")
        self.assertEqual(dry["pending_state"], "post_commit_attempt_orphan")
        self.assertEqual(dry["writes_performed"], 0)
        self.assertEqual(self.transaction_evidence(output), before)

        real_remove = authorizer._remove_authorization_attempt

        def kill_after_attempt_remove(item):
            real_remove(item)
            raise KeyboardInterrupt("simulated hard kill after orphan cleanup")

        with self.state_patch(receipt=output), mock.patch.object(
            authorizer,
            "_remove_authorization_attempt",
            side_effect=kill_after_attempt_remove,
        ), self.assertRaises(KeyboardInterrupt):
            authorizer.recover_authorization_publication(
                output, self.contract, self.confirmation
            )
        self.assertFalse(attempt.path.exists())
        stdout = io.StringIO()
        with self.state_patch(receipt=output), redirect_stdout(stdout):
            self.assertEqual(
                authorizer.main(
                    [
                        "--contract",
                        str(self.contract),
                        "--confirmation",
                        str(self.confirmation),
                        "--output",
                        str(output),
                        "--execute",
                    ]
                ),
                0,
            )
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["writes_performed"], 0)
        self.assertTrue(result["already_present"])

        output = self.root / "attempt-foreign-content.json"
        inspection = self.inspection_for(output)
        with self.state_patch(receipt=output):
            authorizer.publish_authorization_receipt(
                inspection,
                contract_path=self.contract,
                confirmation_path=self.confirmation,
            )
        payload = authorizer.canonical_json_bytes(inspection.document)
        attempt_path = authorizer.authorization_attempt_path(output, payload)
        attempt_path.mkdir()
        (attempt_path / "foreign-entry").mkdir()
        with self.state_patch(receipt=output), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError, "unauthorized entry"
        ):
            authorizer.inspect_authorization_publication(
                output, self.contract, self.confirmation
            )

        output = self.root / "attempt-foreign-name.json"
        inspection = self.inspection_for(output)
        with self.state_patch(receipt=output):
            authorizer.publish_authorization_receipt(
                inspection,
                contract_path=self.contract,
                confirmation_path=self.confirmation,
            )
        foreign_attempt = output.with_name(
            f".{output.name}{authorizer.ATTEMPT_MARKER}{'f' * 64}"
        )
        foreign_attempt.mkdir()
        with self.state_patch(receipt=output), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError, "does not match current authority"
        ):
            authorizer.inspect_authorization_publication(
                output, self.contract, self.confirmation
            )

    def test_repeated_hard_kills_resume_from_each_durable_mutation(self) -> None:
        output = self.root / "repeated-kills.json"
        _, _, staged, proof_path, expected_identity = self.pending_publication(
            "pre_commit_proof_incomplete", output_name=output.name
        )
        self.assertIsNotNone(expected_identity)
        real_write_proof = authorizer.atomic_publish._write_proof_exclusive

        def kill_during_repair(path, *, source, destination, identity):
            payload = authorizer._proof_json_bytes(
                {
                    "schema_version": authorizer.PROOF_SCHEMA_VERSION,
                    "source": str(source),
                    "destination": str(destination),
                    "identity": identity.as_mapping(),
                }
            )
            Path(path).write_bytes(payload[: max(1, len(payload) // 2)])
            raise KeyboardInterrupt("simulated hard kill during proof repair")

        with self.state_patch(receipt=output), mock.patch.object(
            authorizer.atomic_publish,
            "_write_proof_exclusive",
            side_effect=kill_during_repair,
        ), self.assertRaises(KeyboardInterrupt):
            authorizer.recover_authorization_publication(
                output, self.contract, self.confirmation
            )
        with self.state_patch(receipt=output):
            self.assertEqual(
                authorizer.inspect_authorization_publication(
                    output, self.contract, self.confirmation
                ).pending_state,
                "pre_commit_proof_incomplete",
            )

        with self.state_patch(receipt=output), mock.patch.object(
            authorizer.atomic_publish,
            "_write_proof_exclusive",
            side_effect=real_write_proof,
        ):
            authorizer.recover_authorization_publication(
                output, self.contract, self.confirmation
            )
        self.assertTrue(output.is_file())
        self.assertFalse(staged.exists())
        self.assertFalse(proof_path.exists())

        output = self.root / "repeated-commit-kills.json"
        _, _, staged, proof_path, expected_identity = self.pending_publication(
            "pre_commit_proven", output_name=output.name
        )
        real_link = authorizer.os.link

        def kill_after_link(source, destination):
            real_link(source, destination)
            raise KeyboardInterrupt("simulated hard kill after receipt commit")

        with self.state_patch(receipt=output), mock.patch.object(
            authorizer.os, "link", side_effect=kill_after_link
        ), self.assertRaises(KeyboardInterrupt):
            authorizer.recover_authorization_publication(
                output, self.contract, self.confirmation
            )
        with self.state_patch(receipt=output):
            self.assertEqual(
                authorizer.inspect_authorization_publication(
                    output, self.contract, self.confirmation
                ).pending_state,
                "post_commit_stage_linked",
            )

        real_unlink_stage = authorizer._unlink_authorization_stage

        def kill_after_stage_unlink(item):
            real_unlink_stage(item)
            raise KeyboardInterrupt("simulated hard kill after stage unlink")

        with self.state_patch(receipt=output), mock.patch.object(
            authorizer,
            "_unlink_authorization_stage",
            side_effect=kill_after_stage_unlink,
        ), self.assertRaises(KeyboardInterrupt):
            authorizer.recover_authorization_publication(
                output, self.contract, self.confirmation
            )
        with self.state_patch(receipt=output):
            self.assertEqual(
                authorizer.inspect_authorization_publication(
                    output, self.contract, self.confirmation
                ).pending_state,
                "post_commit_stage_unlinked",
            )

        real_unlink_proof = authorizer._unlink_authorization_proof

        def kill_after_proof_unlink(item):
            real_unlink_proof(item)
            raise KeyboardInterrupt("simulated hard kill after proof unlink")

        with self.state_patch(receipt=output), mock.patch.object(
            authorizer,
            "_unlink_authorization_proof",
            side_effect=kill_after_proof_unlink,
        ), self.assertRaises(KeyboardInterrupt):
            authorizer.recover_authorization_publication(
                output, self.contract, self.confirmation
            )
        with self.state_patch(receipt=output):
            committed = authorizer.inspect_authorization_publication(
                output, self.contract, self.confirmation
            )
        self.assertEqual(committed.status, "committed")
        self.assertFalse(staged.exists())
        self.assertFalse(proof_path.exists())
        self.assertEqual(authorizer.FileIdentity.from_path(output), expected_identity)

    def test_valid_legacy_proof_three_states_remain_recoverable(self) -> None:
        for index, state in enumerate(
            ("pre_commit", "post_commit_stage_linked", "post_commit_stage_unlinked")
        ):
            with self.subTest(state=state):
                output = self.root / f"legacy-{index}.json"
                inspection = self.inspection_for(output)
                staged = output.with_name(
                    f".{output.name}.legacy{index}{authorizer.STAGED_SUFFIX}"
                )
                staged.write_bytes(authorizer.canonical_json_bytes(inspection.document))
                identity = authorizer.FileIdentity.from_path(staged)
                proof = authorizer.authorization_proof_path(output)
                authorizer.atomic_publish._write_proof_exclusive(
                    proof, source=staged, destination=output, identity=identity
                )
                if state != "pre_commit":
                    try:
                        os.link(staged, output)
                    except OSError as exc:
                        self.skipTest(f"hardlinks unavailable: {exc}")
                if state == "post_commit_stage_unlinked":
                    staged.unlink()
                with self.state_patch(receipt=output):
                    publication = authorizer.inspect_authorization_publication(
                        output, self.contract, self.confirmation
                    )
                    self.assertEqual(publication.pending_state, state)
                    audit = authorizer.recover_authorization_publication(
                        output, self.contract, self.confirmation
                    )
                self.assertIsNotNone(audit)
                self.assertEqual(authorizer.FileIdentity.from_path(output), identity)
                self.assertFalse(staged.exists())
                self.assertFalse(proof.exists())

    def test_recovery_fails_closed_on_sealed_tamper_foreign_links_and_extra_artifacts(self) -> None:
        output = self.root / "sealed-tamper.json"
        self.pending_publication("pre_commit_proof_incomplete", output_name=output.name)
        staged = authorizer._staged_candidates(output)[0]
        staged.write_bytes(staged.read_bytes() + b" ")
        with self.state_patch(receipt=output), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError,
            "sealed authorization staging bytes differ",
        ):
            authorizer.inspect_authorization_publication(
                output, self.contract, self.confirmation
            )

        output = self.root / "nonprefix-proof.json"
        self.pending_publication("pre_commit_proof_incomplete", output_name=output.name)
        authorizer.authorization_proof_path(output).write_bytes(b"not-an-atomic-prefix")
        with self.state_patch(receipt=output), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError, "durable-write prefix"
        ):
            authorizer.inspect_authorization_publication(
                output, self.contract, self.confirmation
            )

        output = self.root / "foreign-link.json"
        self.pending_publication("pre_commit_proof_incomplete", output_name=output.name)
        staged = authorizer._staged_candidates(output)[0]
        foreign = self.root / "foreign-stage-link.json"
        try:
            os.link(staged, foreign)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.state_patch(receipt=output), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError, "single-link|foreign hardlink"
        ):
            authorizer.inspect_authorization_publication(
                output, self.contract, self.confirmation
            )

        output = self.root / "extra-stage.json"
        self.pending_publication("pre_stage", output_name=output.name)
        extra = output.with_name(f".{output.name}.extra{authorizer.STAGED_SUFFIX}")
        extra.write_bytes(b"foreign")
        with self.state_patch(receipt=output), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError, "staging path"
        ):
            authorizer.inspect_authorization_publication(
                output, self.contract, self.confirmation
            )

        output = self.root / "extra-proof.json"
        self.pending_publication("pre_stage", output_name=output.name)
        extra = output.with_name(f".{output.name}.extra{authorizer.PROOF_SUFFIX}")
        extra.write_bytes(b"foreign")
        with self.state_patch(receipt=output), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError, "foreign.*proof path"
        ):
            authorizer.inspect_authorization_publication(
                output, self.contract, self.confirmation
            )

    def test_execute_with_valid_existing_receipt_is_idempotent_audit_only(self) -> None:
        self.publish()
        before = os.stat(self.receipt, follow_symlinks=False)
        stream = io.StringIO()
        with self.state_patch(), redirect_stdout(stream):
            code = authorizer.main(
                [
                    "--contract",
                    str(self.contract),
                    "--confirmation",
                    str(self.confirmation),
                    "--output",
                    str(self.receipt),
                    "--execute",
                ]
            )
        after = os.stat(self.receipt, follow_symlinks=False)
        result = json.loads(stream.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["writes_performed"], 0)
        self.assertTrue(result["already_present"])
        self.assertFalse(result["recovered"])
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    def test_audit_rejects_coherent_receipt_rehash(self) -> None:
        self.publish()
        document = json.loads(self.receipt.read_text(encoding="utf-8"))
        document["duty_basis"] = "attacker-rewritten duty"
        unsigned = {key: value for key, value in document.items() if key != "receipt_sha256"}
        document["receipt_sha256"] = authorizer.canonical_sha256(unsigned)
        self.receipt.write_bytes(authorizer.canonical_json_bytes(document))
        with self.state_patch(), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError, "differs from live authority"
        ):
            authorizer.audit_authorization_receipt(
                self.receipt, self.contract, self.confirmation
            )

    def test_duplicate_and_nonfinite_json_are_rejected(self) -> None:
        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"x":1,"x":2}\n', encoding="utf-8")
        with self.assertRaisesRegex(authorizer.OptimizationAuthorizationError, "duplicate JSON key"):
            authorizer._strict_json_snapshot(duplicate, "fixture")
        nonfinite = self.root / "nonfinite.json"
        nonfinite.write_text('{"x":NaN}\n', encoding="utf-8")
        with self.assertRaisesRegex(authorizer.OptimizationAuthorizationError, "non-finite"):
            authorizer._strict_json_snapshot(nonfinite, "fixture")

    def test_secure_snapshot_rejects_pathname_swap_during_open(self) -> None:
        victim = self.root / "swap-victim.json"
        replacement = self.root / "swap-replacement.json"
        victim.write_text('{"value":1}\n', encoding="utf-8")
        replacement.write_text('{"value":2}\n', encoding="utf-8")
        real_open = authorizer.os.open
        swapped = False

        def swap_before_open(path, flags, *args):
            nonlocal swapped
            if Path(path) == victim and not swapped:
                swapped = True
                victim.unlink()
                replacement.replace(victim)
            return real_open(path, flags, *args)

        with mock.patch.object(authorizer.os, "open", side_effect=swap_before_open):
            with self.assertRaisesRegex(
                authorizer.OptimizationAuthorizationError,
                "pathname changed before open completed",
            ):
                authorizer.read_secure_snapshot(victim, "swap fixture")

    def test_path_alias_symlink_and_hardlink_are_fail_closed(self) -> None:
        with self.state_patch(receipt=self.declaration):
            with self.assertRaisesRegex(authorizer.OptimizationAuthorizationError, "path alias"):
                authorizer.inspect_authorization(
                    self.contract, self.confirmation, self.declaration
                )

        link = self.root / "confirmation-link.json"
        try:
            link.symlink_to(self.confirmation)
        except OSError:
            link = None
        if link is not None:
            with self.state_patch(confirmation_path=link), self.assertRaisesRegex(
                authorizer.OptimizationAuthorizationError, "symlink/reparse"
            ):
                authorizer.inspect_authorization(self.contract, link, self.receipt)

        hardlink = self.root / "confirmation-hardlink.json"
        try:
            os.link(self.confirmation, hardlink)
        except OSError:
            return
        with self.state_patch(confirmation_path=hardlink), self.assertRaisesRegex(
            authorizer.OptimizationAuthorizationError, "hardlink rejected"
        ):
            authorizer.inspect_authorization(self.contract, hardlink, self.receipt)


if __name__ == "__main__":
    unittest.main()
