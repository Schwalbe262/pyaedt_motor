from __future__ import annotations

from contextlib import redirect_stdout
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import atomic_publish
import confirm_ipmsm_v2_optimization_inputs as confirmation
import ipmsm_optimization as optimization
import supervise_ipmsm_v2_pipeline as supervisor


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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OptimizationInputConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.contract_path = self.root / "contract.json"
        self.spec_path = self.root / "optimization_spec.json"
        self.implementation_path = self.root / "ipmsm_optimization.py"
        self.contract_path.write_text('{"fixture":true}\n', encoding="utf-8")
        self.spec_path.write_text(
            json.dumps(spec_mapping(), sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        self.implementation_path.write_text("# immutable fixture\n", encoding="utf-8")
        self.spec = optimization.optimization_spec_from_mapping(spec_mapping())
        self.context = confirmation.BoundContext(
            contract_path=self.contract_path.resolve(),
            contract_file_sha256=sha256(self.contract_path),
            contract_sha256="a" * 64,
            spec_path=self.spec_path.resolve(),
            spec_sha256=sha256(self.spec_path),
            spec_canonical_sha256=confirmation.canonical_sha256(spec_mapping()),
            spec=self.spec,
            spec_assumptions=spec_mapping()["_assumptions"],
            implementation_path=self.implementation_path.resolve(),
            implementation_sha256=sha256(self.implementation_path),
            snapshots=(
                confirmation.read_stable_snapshot(self.contract_path, "contract"),
                confirmation.read_stable_snapshot(self.spec_path, "spec"),
                confirmation.read_stable_snapshot(self.implementation_path, "implementation"),
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def context_for_spec(self, mapping: dict[str, object]) -> confirmation.BoundContext:
        self.spec_path.write_text(
            json.dumps(mapping, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        spec = optimization.optimization_spec_from_mapping(mapping)
        return confirmation.BoundContext(
            contract_path=self.contract_path.resolve(),
            contract_file_sha256=sha256(self.contract_path),
            contract_sha256="a" * 64,
            spec_path=self.spec_path.resolve(),
            spec_sha256=sha256(self.spec_path),
            spec_canonical_sha256=confirmation.canonical_sha256(mapping),
            spec=spec,
            spec_assumptions=mapping.get("_assumptions", {}),
            implementation_path=self.implementation_path.resolve(),
            implementation_sha256=sha256(self.implementation_path),
            snapshots=(
                confirmation.read_stable_snapshot(self.contract_path, "contract"),
                confirmation.read_stable_snapshot(self.spec_path, "spec"),
                confirmation.read_stable_snapshot(
                    self.implementation_path, "implementation"
                ),
            ),
        )

    def declaration(self) -> dict[str, object]:
        value = confirmation.declaration_template(self.context)
        value["authority"] = {
            "confirmed_by": "motor-owner@example.test",
            "confirmed_at_utc": "2026-07-12T01:02:03+09:00",
            "evidence_reference": "requirements/MOTOR-42",
            "attestation_kind": confirmation.ATTESTATION_KIND,
        }
        value["confirmed_inputs"]["duty_cycle"]["basis"] = (
            "owner-approved engineering requirement MOTOR-42"
        )
        value["acknowledgements"] = {
            name: True for name in confirmation.ACKNOWLEDGEMENT_FIELDS
        }
        return value

    def write_declaration(self, value: dict[str, object] | None = None) -> Path:
        path = self.root / "declaration.json"
        path.write_bytes(confirmation.canonical_json_bytes(value or self.declaration()))
        return path

    def build(self, value: dict[str, object] | None = None) -> tuple[dict[str, object], Path]:
        declaration_path = self.write_declaration(value)
        document = confirmation.build_confirmation(
            self.context,
            value or self.declaration(),
            declaration_path=declaration_path,
            declaration_sha256=sha256(declaration_path),
        )
        return document, declaration_path

    def pending_publication(
        self,
        state: str,
        *,
        output_name: str = "confirmation.json",
    ) -> tuple[
        dict[str, object],
        Path,
        confirmation.FileSnapshot,
        Path,
        Path,
        Path,
        atomic_publish.FileIdentity | None,
    ]:
        document, declaration_path = self.build()
        declaration_snapshot = confirmation.read_stable_snapshot(
            declaration_path, "declaration"
        )
        output = self.root / output_name
        payload = confirmation.canonical_json_bytes(document)
        attempt = confirmation._create_confirmation_attempt(
            confirmation.confirmation_attempt_path(output, payload)
        )
        staged = confirmation.confirmation_staged_path(output, payload)
        proof = confirmation.confirmation_proof_path(output)
        if state == "pre_stage":
            return (
                document,
                declaration_path,
                declaration_snapshot,
                output,
                staged,
                proof,
                None,
            )
        staged.write_bytes(payload if state != "pre_stage_incomplete" else payload[:17])
        identity = atomic_publish.FileIdentity.from_path(staged)
        if state == "pre_stage_incomplete":
            return (
                document,
                declaration_path,
                declaration_snapshot,
                output,
                staged,
                proof,
                identity,
            )
        confirmation._create_confirmation_stage_ready(attempt)
        if state == "pre_commit_no_proof":
            return (
                document,
                declaration_path,
                declaration_snapshot,
                output,
                staged,
                proof,
                identity,
            )
        if state == "pre_commit_proof_incomplete":
            proof_payload = confirmation._proof_json_bytes(
                {
                    "schema_version": atomic_publish.PROOF_SCHEMA_VERSION,
                    "source": str(staged),
                    "destination": str(output),
                    "identity": identity.as_mapping(),
                }
            )
            proof.write_bytes(proof_payload[:19])
            return (
                document,
                declaration_path,
                declaration_snapshot,
                output,
                staged,
                proof,
                identity,
            )
        atomic_publish._write_proof_exclusive(
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
        return (
            document,
            declaration_path,
            declaration_snapshot,
            output,
            staged,
            proof,
            identity,
        )

    def test_template_is_read_only_and_requires_explicit_section_acknowledgements(self) -> None:
        template = confirmation.declaration_template(self.context)
        self.assertEqual(
            set(template["confirmed_inputs"]),
            {
                "operating_points",
                "duty_cycle",
                "inverter",
                "winding",
                "design_space",
                "constraints_and_derived_limits",
                "beta_calibration_and_control",
                "topology",
                "nsga2",
                "objectives",
            },
        )
        self.assertEqual(template["confirmed_inputs"]["duty_cycle"]["basis"], "")
        self.assertTrue(all(value is False for value in template["acknowledgements"].values()))
        self.assertEqual(
            template["confirmed_inputs"]["objectives"]["volume"],
            confirmation.VOLUME_DEFINITION,
        )
        self.assertEqual(
            template["confirmed_inputs"]["objectives"]["efficiency"],
            confirmation.EFFICIENCY_OBJECTIVE_DEFINITION,
        )
        self.assertEqual(template["bindings"], confirmation._context_bindings(self.context))
        self.assertFalse(any(self.root.glob("*confirmation*")))

    def test_every_acknowledgement_must_be_exact_boolean_true(self) -> None:
        for field in confirmation.ACKNOWLEDGEMENT_FIELDS:
            with self.subTest(field=field):
                value = self.declaration()
                value["acknowledgements"][field] = 1
                with self.assertRaisesRegex(
                    confirmation.OptimizationInputConfirmationError,
                    rf"{field} must be explicitly true",
                ):
                    self.build(value)

    def test_each_confirmed_section_must_match_effective_immutable_values(self) -> None:
        mutations = {
            "operating_points": lambda value: value["confirmed_inputs"]["operating_points"][0].__setitem__(
                "speed_rpm", 999
            ),
            "duty_cycle": lambda value: value["confirmed_inputs"]["duty_cycle"]["weights"][0].__setitem__(
                "duty_weight", 0.5
            ),
            "inverter": lambda value: value["confirmed_inputs"]["inverter"].__setitem__(
                "vdc_v", 301
            ),
            "winding": lambda value: value["confirmed_inputs"]["winding"].__setitem__(
                "series_turns_per_phase", 52
            ),
            "design_space": lambda value: value["confirmed_inputs"]["design_space"][
                "geometry"
            ][0].__setitem__("lower", -1),
            "constraints": lambda value: value["confirmed_inputs"][
                "constraints_and_derived_limits"
            ].__setitem__("effective_peak_current_limit_a", 1),
            "beta_control": lambda value: value["confirmed_inputs"][
                "beta_calibration_and_control"
            ]["calibration"].__setitem__("electrical_zero_deg", 0),
            "topology": lambda value: value["confirmed_inputs"]["topology"].__setitem__(
                "pole_number", 10
            ),
            "nsga2": lambda value: value["confirmed_inputs"]["nsga2"].__setitem__(
                "max_generations", 1
            ),
            "volume_definition": lambda value: value["confirmed_inputs"]["objectives"][
                "volume"
            ].__setitem__("end_windings_included", True),
            "efficiency_objective": lambda value: value["confirmed_inputs"]["objectives"][
                "efficiency"
            ].__setitem__("objective", "minimize"),
            "binding": lambda value: value["bindings"]["optimization_spec"].__setitem__(
                "sha256", "0" * 64
            ),
        }
        for section, mutate in mutations.items():
            with self.subTest(section=section):
                value = self.declaration()
                mutate(value)
                with self.assertRaisesRegex(
                    confirmation.OptimizationInputConfirmationError,
                    "does not match the immutable spec",
                ):
                    self.build(value)

    def test_boolean_cannot_masquerade_as_numeric_spec_value(self) -> None:
        value = self.declaration()
        value["confirmed_inputs"]["inverter"]["vdc_v"] = True
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "must be a finite number"
        ):
            self.build(value)

    def test_authority_is_explicit_self_attestation_and_future_time_is_rejected(self) -> None:
        wrong_kind = self.declaration()
        wrong_kind["authority"]["attestation_kind"] = "digital_signature"
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "attestation_kind"
        ):
            self.build(wrong_kind)

        future = self.declaration()
        future["authority"]["confirmed_at_utc"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "future clock skew"
        ):
            self.build(future)

    def test_declaration_cannot_be_rebound_across_unseen_spec_context_changes(self) -> None:
        original_declaration = self.declaration()
        declaration_path = self.write_declaration(original_declaration)
        mutations = {
            "design_space": lambda value: value.__setitem__(
                "design_space", {"stator_outer_radius": [100.0, 151.0]}
            ),
            "constraint": lambda value: value["constraints"].__setitem__(
                "current_density_limit_a_per_mm2", 21
            ),
            "beta": lambda value: value["beta_calibration"].__setitem__(
                "electrical_zero_deg", 13.0
            ),
            "nsga": lambda value: value.__setitem__(
                "nsga2", {"max_generations": 301}
            ),
            "assumption": lambda value: value["_assumptions"].__setitem__(
                "winding_requires_manufacturing_confirmation", False
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(spec_mapping())
                mutate(changed)
                changed_context = self.context_for_spec(changed)
                with self.assertRaisesRegex(
                    confirmation.OptimizationInputConfirmationError,
                    "bindings.optimization_spec|bindings.spec_assumptions",
                ):
                    confirmation.build_confirmation(
                        changed_context,
                        original_declaration,
                        declaration_path=declaration_path,
                        declaration_sha256=sha256(declaration_path),
                    )
        self.context = self.context_for_spec(spec_mapping())

    def test_build_binds_contract_spec_implementation_declaration_and_assumptions(self) -> None:
        document, declaration_path = self.build()
        unsigned = {key: item for key, item in document.items() if key != "confirmation_sha256"}
        self.assertEqual(document["confirmation_sha256"], confirmation.canonical_sha256(unsigned))
        self.assertEqual(document["bindings"]["contract"]["contract_sha256"], "a" * 64)
        self.assertEqual(
            document["bindings"]["optimization_spec"]["sha256"], sha256(self.spec_path)
        )
        self.assertEqual(
            document["bindings"]["optimization_implementation"]["sha256"],
            sha256(self.implementation_path),
        )
        self.assertEqual(document["declaration_source"]["sha256"], sha256(declaration_path))
        self.assertEqual(
            document["bindings"]["spec_assumptions"], spec_mapping()["_assumptions"]
        )
        self.assertEqual(document["authority"]["confirmed_at_utc"], "2026-07-11T16:02:03Z")

    def test_dry_run_main_validates_but_performs_no_write(self) -> None:
        declaration_path = self.write_declaration()
        output = self.root / "confirmation.json"
        stdout = io.StringIO()
        with mock.patch.object(
            confirmation, "load_bound_context", return_value=self.context
        ), redirect_stdout(stdout):
            code = confirmation.main(
                [
                    "--contract",
                    str(self.contract_path),
                    "--declaration",
                    str(declaration_path),
                    "--output",
                    str(output),
                ]
            )
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["writes_performed"], 0)
        self.assertFalse(output.exists())

    def test_publish_is_canonical_no_replace_and_read_only_auditable(self) -> None:
        document, declaration_path = self.build()
        declaration_snapshot = confirmation.read_stable_snapshot(declaration_path, "declaration")
        output = self.root / "confirmation.json"
        audit = confirmation.publish_confirmation(
            output, document, self.context, declaration_snapshot
        )
        self.assertEqual(output.read_bytes(), confirmation.canonical_json_bytes(document))
        self.assertEqual(audit.confirmation_sha256, document["confirmation_sha256"])
        with mock.patch.object(
            confirmation, "load_bound_context", return_value=self.context
        ) as load_context:
            second = confirmation.audit_confirmation(output, self.contract_path)
        load_context.assert_called_once_with(self.contract_path)
        self.assertEqual(second.as_mapping()["status"], "confirmed")
        self.assertTrue(second.as_mapping()["authorized_for_production_optimization"])

        original = output.read_bytes()
        original_identity = atomic_publish.FileIdentity.from_path(output)
        repeated = confirmation.publish_confirmation(
            output, document, self.context, declaration_snapshot
        )
        self.assertEqual(repeated.confirmation_sha256, document["confirmation_sha256"])
        self.assertEqual(atomic_publish.FileIdentity.from_path(output), original_identity)
        self.assertEqual(output.read_bytes(), original)

        for execute, expected_status in ((False, "already_confirmed"), (True, "confirmed")):
            with self.subTest(execute=execute):
                stdout = io.StringIO()
                argv = [
                    "--contract",
                    str(self.contract_path),
                    "--declaration",
                    str(declaration_path),
                    "--output",
                    str(output),
                ]
                if execute:
                    argv.append("--execute")
                with mock.patch.object(
                    confirmation, "load_bound_context", return_value=self.context
                ), redirect_stdout(stdout):
                    self.assertEqual(confirmation.main(argv), 0)
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["writes_performed"], 0)
                self.assertEqual(
                    atomic_publish.FileIdentity.from_path(output), original_identity
                )

    def test_audit_rejects_noncanonical_and_hash_tampered_confirmation(self) -> None:
        document, _ = self.build()
        output = self.root / "confirmation.json"
        output.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "not canonical"
        ):
            confirmation._audit_confirmation_with_context(output, self.context)

        document["authority"]["confirmed_by"] = "attacker"
        output.write_bytes(confirmation.canonical_json_bytes(document))
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "confirmation_sha256 mismatch"
        ):
            confirmation._audit_confirmation_with_context(output, self.context)

    def test_audit_rejects_rehashed_binding_or_confirmed_value_tamper(self) -> None:
        for label, mutate in {
            "contract": lambda value: value["bindings"]["contract"].__setitem__(
                "contract_sha256", "b" * 64
            ),
            "spec": lambda value: value["bindings"]["optimization_spec"].__setitem__(
                "sha256", "b" * 64
            ),
            "implementation": lambda value: value["bindings"][
                "optimization_implementation"
            ].__setitem__("sha256", "b" * 64),
            "input": lambda value: value["confirmed_inputs"]["winding"].__setitem__(
                "fill_factor", 0.7
            ),
            "assumption": lambda value: value["bindings"]["spec_assumptions"].__setitem__(
                "winding_requires_manufacturing_confirmation", False
            ),
        }.items():
            with self.subTest(label=label):
                document, _ = self.build()
                mutate(document)
                unsigned = {
                    key: item for key, item in document.items() if key != "confirmation_sha256"
                }
                document["confirmation_sha256"] = confirmation.canonical_sha256(unsigned)
                output = self.root / f"tampered-{label}.json"
                output.write_bytes(confirmation.canonical_json_bytes(document))
                with self.assertRaises(confirmation.OptimizationInputConfirmationError):
                    confirmation._audit_confirmation_with_context(output, self.context)

    def test_audit_requires_exact_live_recorded_declaration_source(self) -> None:
        document, declaration_path = self.build()
        output = self.root / "confirmation.json"
        output.write_bytes(confirmation.canonical_json_bytes(document))

        original = declaration_path.read_bytes()
        declaration_path.unlink()
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError,
            "cannot resolve recorded optimization-input declaration",
        ):
            confirmation._audit_confirmation_with_context(output, self.context)

        declaration_path.write_bytes(original + b" \n")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError,
            "recorded declaration raw SHA256 mismatch",
        ):
            confirmation._audit_confirmation_with_context(output, self.context)

        changed_source = json.loads(original)
        changed_source["authority"]["confirmed_by"] = "different-owner@example.test"
        declaration_path.write_bytes(confirmation.canonical_json_bytes(changed_source))
        document["declaration_source"]["sha256"] = sha256(declaration_path)
        unsigned = {
            key: item for key, item in document.items() if key != "confirmation_sha256"
        }
        document["confirmation_sha256"] = confirmation.canonical_sha256(unsigned)
        output.write_bytes(confirmation.canonical_json_bytes(document))
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError,
            "authority.confirmed_by does not match",
        ):
            confirmation._audit_confirmation_with_context(output, self.context)

    def test_audit_rechecks_declaration_and_confirmation_for_toctou(self) -> None:
        document, declaration_path = self.build()
        output = self.root / "confirmation.json"
        output.write_bytes(confirmation.canonical_json_bytes(document))
        real_validate = confirmation._validate_declaration
        calls = 0

        def mutate_after_live_validation(context, declaration):
            nonlocal calls
            result = real_validate(context, declaration)
            calls += 1
            if calls == 2:
                declaration_path.write_bytes(declaration_path.read_bytes() + b" ")
            return result

        with mock.patch.object(
            confirmation,
            "_validate_declaration",
            side_effect=mutate_after_live_validation,
        ):
            with self.assertRaisesRegex(
                confirmation.OptimizationInputConfirmationError,
                "bound input changed during confirmation",
            ):
                confirmation._audit_confirmation_with_context(output, self.context)

    def test_audit_rechecks_confirmation_for_toctou(self) -> None:
        document, declaration_path = self.build()
        output = self.root / "confirmation.json"
        output.write_bytes(confirmation.canonical_json_bytes(document))
        real_validate = confirmation._validate_declaration
        calls = 0

        def mutate_confirmation_after_live_validation(context, declaration):
            nonlocal calls
            result = real_validate(context, declaration)
            calls += 1
            if calls == 2:
                output.write_bytes(output.read_bytes() + b" ")
            return result

        with mock.patch.object(
            confirmation,
            "_validate_declaration",
            side_effect=mutate_confirmation_after_live_validation,
        ):
            with self.assertRaisesRegex(
                confirmation.OptimizationInputConfirmationError,
                "bound input changed during confirmation",
            ):
                confirmation._audit_confirmation_with_context(output, self.context)
        self.assertTrue(declaration_path.is_file())

    def test_strict_json_reader_rejects_duplicate_keys_and_nonfinite_constants(self) -> None:
        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"a":1,"a":2}', encoding="utf-8")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "duplicate JSON key"
        ):
            confirmation._read_json_snapshot(duplicate, "fixture")
        nonfinite = self.root / "nonfinite.json"
        nonfinite.write_text('{"a":NaN}', encoding="utf-8")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "non-finite JSON constant"
        ):
            confirmation._read_json_snapshot(nonfinite, "fixture")

    def test_post_audit_failure_preserves_proven_publication_for_retry(self) -> None:
        document, declaration_path = self.build()
        output = self.root / "confirmation.json"
        declaration_snapshot = confirmation.read_stable_snapshot(declaration_path, "declaration")
        with mock.patch.object(
            confirmation,
            "_audit_confirmation_with_context",
            side_effect=confirmation.OptimizationInputConfirmationError("post-audit failed"),
        ):
            with self.assertRaisesRegex(
                confirmation.OptimizationInputConfirmationError, "post-audit failed"
            ):
                confirmation.publish_confirmation(
                    output, document, self.context, declaration_snapshot
                )
        pending = confirmation.inspect_confirmation_publication(
            output, document, self.context, declaration_snapshot
        )
        self.assertEqual(pending.status, "publication_recovery_pending")
        self.assertEqual(pending.pending_state, "post_commit_stage_unlinked")
        recovered = confirmation.publish_confirmation(
            output, document, self.context, declaration_snapshot
        )
        self.assertEqual(recovered.confirmation_sha256, document["confirmation_sha256"])

    def test_read_only_inspection_reports_all_pending_kill_windows_without_writes(self) -> None:
        for index, (fixture_state, pending_state) in enumerate(
            (
                ("pre_stage", "pre_stage"),
                ("pre_stage_incomplete", "pre_stage_incomplete"),
                ("pre_commit_no_proof", "pre_commit"),
                ("pre_commit_proof_incomplete", "pre_commit_proof_incomplete"),
                ("pre_commit", "pre_commit"),
                ("post_commit_stage_linked", "post_commit_stage_linked"),
                ("post_commit_stage_unlinked", "post_commit_stage_unlinked"),
            )
        ):
            with self.subTest(pending_state=pending_state):
                (
                    document,
                    declaration_path,
                    declaration_snapshot,
                    output,
                    staged,
                    proof,
                    _,
                ) = self.pending_publication(
                    fixture_state, output_name=f"pending-{index}.json"
                )
                attempt = confirmation.confirmation_attempt_path(
                    output, confirmation.canonical_json_bytes(document)
                )
                ready = attempt / confirmation.CONFIRMATION_STAGE_READY_NAME
                paths = tuple(
                    path
                    for path in (attempt, ready, staged, output, proof)
                    if os.path.lexists(path)
                )

                def evidence(path: Path) -> tuple[bytes, tuple[int, int, int, int, int]]:
                    info = os.lstat(path)
                    payload = b"<directory>" if path.is_dir() else path.read_bytes()
                    return payload, (
                        int(info.st_dev),
                        int(info.st_ino),
                        int(info.st_size),
                        int(info.st_mtime_ns),
                        int(info.st_nlink),
                    )

                before = {path: evidence(path) for path in paths}
                inspection = confirmation.inspect_confirmation_publication(
                    output, document, self.context, declaration_snapshot
                )
                self.assertEqual(inspection.status, "publication_recovery_pending")
                self.assertEqual(inspection.pending_state, pending_state)
                self.assertEqual({path: evidence(path) for path in paths}, before)

                stdout = io.StringIO()
                with mock.patch.object(
                    confirmation, "load_bound_context", return_value=self.context
                ), redirect_stdout(stdout):
                    self.assertEqual(
                        confirmation.main(
                            [
                                "--contract",
                                str(self.contract_path),
                                "--declaration",
                                str(declaration_path),
                                "--output",
                                str(output),
                            ]
                        ),
                        0,
                    )
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["status"], "publication_recovery_pending")
                self.assertEqual(result["pending_state"], pending_state)
                self.assertEqual(result["writes_performed"], 0)
                self.assertEqual({path: evidence(path) for path in paths}, before)

    def test_execute_recovers_each_kill_window_in_place_and_is_idempotent(self) -> None:
        for index, pending_state in enumerate(
            (
                "pre_stage",
                "pre_stage_incomplete",
                "pre_commit_no_proof",
                "pre_commit_proof_incomplete",
                "pre_commit",
                "post_commit_stage_linked",
                "post_commit_stage_unlinked",
            )
        ):
            with self.subTest(pending_state=pending_state):
                (
                    document,
                    _,
                    declaration_snapshot,
                    output,
                    staged,
                    proof,
                    identity,
                ) = self.pending_publication(
                    pending_state, output_name=f"recover-{index}.json"
                )
                result = confirmation.publish_confirmation_with_outcome(
                    output, document, self.context, declaration_snapshot
                )
                audit = result.audit
                self.assertTrue(result.recovered)
                self.assertEqual(result.writes_performed, 1)
                self.assertEqual(audit.confirmation_sha256, document["confirmation_sha256"])
                if pending_state not in {"pre_stage", "pre_stage_incomplete"}:
                    self.assertEqual(atomic_publish.FileIdentity.from_path(output), identity)
                self.assertEqual(os.lstat(output).st_nlink, 1)
                self.assertFalse(os.path.lexists(staged))
                self.assertFalse(os.path.lexists(proof))
                self.assertFalse(
                    os.path.lexists(
                        confirmation.confirmation_attempt_path(
                            output, confirmation.canonical_json_bytes(document)
                        )
                    )
                )
                committed_identity = atomic_publish.FileIdentity.from_path(output)

                repeated = confirmation.publish_confirmation(
                    output, document, self.context, declaration_snapshot
                )
                self.assertEqual(repeated.file_sha256, audit.file_sha256)
                self.assertEqual(
                    atomic_publish.FileIdentity.from_path(output), committed_identity
                )

    def test_execute_main_reports_actual_recovery_and_publish_mutations(self) -> None:
        (
            _,
            declaration_path,
            _,
            pending_output,
            _,
            _,
            _,
        ) = self.pending_publication("pre_stage", output_name="main-pending.json")
        for output, expected_outcome, expected_recovered in (
            (pending_output, "recovered", True),
            (self.root / "main-fresh.json", "published", False),
        ):
            with self.subTest(expected_outcome=expected_outcome):
                stdout = io.StringIO()
                with mock.patch.object(
                    confirmation, "load_bound_context", return_value=self.context
                ), redirect_stdout(stdout):
                    self.assertEqual(
                        confirmation.main(
                            [
                                "--contract",
                                str(self.contract_path),
                                "--declaration",
                                str(declaration_path),
                                "--output",
                                str(output),
                                "--execute",
                            ]
                        ),
                        0,
                    )
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["publication_outcome"], expected_outcome)
                self.assertEqual(result["recovered"], expected_recovered)
                self.assertEqual(result["writes_performed"], 1)
                self.assertTrue(output.is_file())

    def test_two_publishers_same_payload_race_converges_to_already_present(self) -> None:
        document, declaration_path = self.build()
        declaration_snapshot = confirmation.read_stable_snapshot(
            declaration_path, "declaration"
        )
        output = self.root / "same-payload-race.json"
        real_create = confirmation._create_confirmation_attempt
        winner: dict[str, object] = {}
        interleaved = False

        def complete_winner_before_loser_acquires(path: Path):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                result = confirmation.publish_confirmation_with_outcome(
                    output, document, self.context, declaration_snapshot
                )
                winner["result"] = result
                winner["identity"] = atomic_publish.FileIdentity.from_path(output)
            return real_create(path)

        stdout = io.StringIO()
        with mock.patch.object(
            confirmation,
            "_create_confirmation_attempt",
            side_effect=complete_winner_before_loser_acquires,
        ), mock.patch.object(
            confirmation, "load_bound_context", return_value=self.context
        ), redirect_stdout(stdout):
            self.assertEqual(
                confirmation.main(
                    [
                        "--contract",
                        str(self.contract_path),
                        "--declaration",
                        str(declaration_path),
                        "--output",
                        str(output),
                        "--execute",
                    ]
                ),
                0,
            )
        winner_result = winner["result"]
        self.assertIsInstance(winner_result, confirmation.ConfirmationPublicationResult)
        self.assertEqual(winner_result.outcome, "published")
        loser = json.loads(stdout.getvalue())
        self.assertEqual(loser["publication_outcome"], "already_present")
        self.assertTrue(loser["already_present"])
        self.assertFalse(loser["recovered"])
        self.assertEqual(loser["writes_performed"], 0)
        self.assertEqual(
            atomic_publish.FileIdentity.from_path(output), winner["identity"]
        )
        payload = confirmation.canonical_json_bytes(document)
        self.assertFalse(
            os.path.lexists(confirmation.confirmation_attempt_path(output, payload))
        )
        self.assertFalse(os.path.lexists(confirmation.confirmation_staged_path(output, payload)))
        self.assertFalse(os.path.lexists(confirmation.confirmation_proof_path(output)))

    def test_committed_attempt_cleanup_is_repeatable_and_foreign_content_fails_closed(self) -> None:
        document, declaration_path = self.build()
        snapshot = confirmation.read_stable_snapshot(declaration_path, "declaration")
        output = self.root / "committed-cleanup-kill.json"
        confirmation.publish_confirmation(output, document, self.context, snapshot)
        output_identity = atomic_publish.FileIdentity.from_path(output)
        attempt_path = confirmation.confirmation_attempt_path(
            output, confirmation.canonical_json_bytes(document)
        )
        confirmation._create_confirmation_attempt(attempt_path)
        inspection = confirmation.inspect_confirmation_publication(
            output, document, self.context, snapshot
        )
        self.assertEqual(inspection.pending_state, "committed_attempt_cleanup")

        real_remove = confirmation._remove_confirmation_attempt

        def kill_after_attempt_removal(item) -> None:
            real_remove(item)
            raise KeyboardInterrupt("simulated kill after committed-attempt cleanup")

        with mock.patch.object(
            confirmation,
            "_remove_confirmation_attempt",
            side_effect=kill_after_attempt_removal,
        ):
            with self.assertRaises(KeyboardInterrupt):
                confirmation.publish_confirmation_with_outcome(
                    output, document, self.context, snapshot
                )
        self.assertFalse(os.path.lexists(attempt_path))
        repeated = confirmation.publish_confirmation_with_outcome(
            output, document, self.context, snapshot
        )
        self.assertEqual(repeated.outcome, "already_present")
        self.assertEqual(repeated.writes_performed, 0)
        self.assertEqual(atomic_publish.FileIdentity.from_path(output), output_identity)

        foreign_output = self.root / "committed-foreign-attempt.json"
        confirmation.publish_confirmation(
            foreign_output, document, self.context, snapshot
        )
        foreign_attempt = confirmation.confirmation_attempt_path(
            foreign_output, confirmation.canonical_json_bytes(document)
        )
        confirmation._create_confirmation_attempt(foreign_attempt)
        (foreign_attempt / "foreign").write_text("tamper", encoding="utf-8")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "unauthorized entry"
        ):
            confirmation.inspect_confirmation_publication(
                foreign_output, document, self.context, snapshot
            )

        sealed_output = self.root / "committed-sealed-attempt.json"
        confirmation.publish_confirmation(sealed_output, document, self.context, snapshot)
        sealed_attempt_path = confirmation.confirmation_attempt_path(
            sealed_output, confirmation.canonical_json_bytes(document)
        )
        sealed_attempt = confirmation._create_confirmation_attempt(sealed_attempt_path)
        confirmation._create_confirmation_stage_ready(sealed_attempt)
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "sealed foreign attempt"
        ):
            confirmation.inspect_confirmation_publication(
                sealed_output, document, self.context, snapshot
            )

    def test_repeated_kill_recovery_continues_from_each_applied_mutation(self) -> None:
        document, _, snapshot, output, staged, proof, identity = self.pending_publication(
            "pre_commit", output_name="repeat-pre.json"
        )
        real_link = os.link

        def kill_after_link(source: Path, destination: Path) -> None:
            real_link(source, destination)
            raise KeyboardInterrupt("simulated hard kill after commit")

        with mock.patch.object(confirmation.os, "link", side_effect=kill_after_link):
            with self.assertRaises(KeyboardInterrupt):
                confirmation.publish_confirmation(output, document, self.context, snapshot)
        self.assertEqual(
            confirmation.inspect_confirmation_publication(
                output, document, self.context, snapshot
            ).pending_state,
            "post_commit_stage_linked",
        )
        confirmation.publish_confirmation(output, document, self.context, snapshot)
        self.assertEqual(atomic_publish.FileIdentity.from_path(output), identity)

        document, _, snapshot, output, staged, proof, identity = self.pending_publication(
            "post_commit_stage_linked", output_name="repeat-stage.json"
        )
        real_unlink_stage = confirmation._unlink_confirmation_stage

        def kill_after_stage_unlink(item) -> None:
            real_unlink_stage(item)
            raise KeyboardInterrupt("simulated hard kill after stage unlink")

        with mock.patch.object(
            confirmation, "_unlink_confirmation_stage", side_effect=kill_after_stage_unlink
        ):
            with self.assertRaises(KeyboardInterrupt):
                confirmation.publish_confirmation(output, document, self.context, snapshot)
        self.assertEqual(
            confirmation.inspect_confirmation_publication(
                output, document, self.context, snapshot
            ).pending_state,
            "post_commit_stage_unlinked",
        )
        confirmation.publish_confirmation(output, document, self.context, snapshot)
        self.assertEqual(atomic_publish.FileIdentity.from_path(output), identity)

        document, _, snapshot, output, staged, proof, identity = self.pending_publication(
            "post_commit_stage_unlinked", output_name="repeat-proof.json"
        )
        real_unlink_proof = confirmation._unlink_confirmation_proof

        def kill_after_proof_unlink(item) -> None:
            real_unlink_proof(item)
            raise KeyboardInterrupt("simulated hard kill after proof unlink")

        with mock.patch.object(
            confirmation, "_unlink_confirmation_proof", side_effect=kill_after_proof_unlink
        ):
            with self.assertRaises(KeyboardInterrupt):
                confirmation.publish_confirmation(output, document, self.context, snapshot)
        self.assertEqual(
            confirmation.inspect_confirmation_publication(
                output, document, self.context, snapshot
            ).status,
            "committed",
        )
        confirmation.publish_confirmation(output, document, self.context, snapshot)
        self.assertEqual(atomic_publish.FileIdentity.from_path(output), identity)

    def test_recovery_rejects_foreign_links_and_proof_path_identity_or_byte_tamper(self) -> None:
        document, _, snapshot, output, staged, _, _ = self.pending_publication(
            "post_commit_stage_linked", output_name="foreign-link.json"
        )
        foreign = self.root / "foreign-link-alias.json"
        try:
            os.link(output, foreign)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "hardlink ownership"
        ):
            confirmation.inspect_confirmation_publication(
                output, document, self.context, snapshot
            )

        for index, tamper in enumerate(("path", "identity", "bytes", "proof_bytes")):
            with self.subTest(tamper=tamper):
                document, _, snapshot, output, staged, proof, _ = self.pending_publication(
                    "pre_commit", output_name=f"tamper-{index}.json"
                )
                if tamper in {"path", "identity"}:
                    raw = json.loads(proof.read_text(encoding="utf-8"))
                    if tamper == "path":
                        raw["source"] = str(
                            output.with_name(
                                f".{output.name}.invalid{confirmation.CONFIRMATION_STAGED_SUFFIX}"
                            )
                        )
                    else:
                        raw["identity"]["inode"] += 1
                    proof.write_bytes(confirmation._proof_json_bytes(raw))
                elif tamper == "bytes":
                    payload = bytearray(staged.read_bytes())
                    payload[0] = ord("[")
                    staged.write_bytes(payload)
                else:
                    proof.write_bytes(proof.read_bytes() + b" ")
                with self.assertRaises(confirmation.OptimizationInputConfirmationError):
                    confirmation.inspect_confirmation_publication(
                        output, document, self.context, snapshot
                    )

    def test_attempt_recovery_rejects_extra_entries_and_partial_artifact_hardlinks(self) -> None:
        document, _, snapshot, output, staged, _, _ = self.pending_publication(
            "pre_stage_incomplete", output_name="partial-stage-link.json"
        )
        try:
            os.link(staged, self.root / "partial-stage-foreign.json")
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "foreign hardlink"
        ):
            confirmation.inspect_confirmation_publication(
                output, document, self.context, snapshot
            )

        document, _, snapshot, output, _, proof, _ = self.pending_publication(
            "pre_commit_proof_incomplete", output_name="partial-proof-link.json"
        )
        os.link(proof, self.root / "partial-proof-foreign.json")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "foreign hardlink|hardlink"
        ):
            confirmation.inspect_confirmation_publication(
                output, document, self.context, snapshot
            )

        document, _, snapshot, output, _, _, _ = self.pending_publication(
            "pre_commit_no_proof", output_name="attempt-entry.json"
        )
        attempt = confirmation.confirmation_attempt_path(
            output, confirmation.canonical_json_bytes(document)
        )
        (attempt / "foreign-entry").write_text("tamper", encoding="utf-8")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "unauthorized entry"
        ):
            confirmation.inspect_confirmation_publication(
                output, document, self.context, snapshot
            )

    def test_recovery_rejects_proofless_stage_and_changed_bound_declaration(self) -> None:
        document, declaration_path = self.build()
        snapshot = confirmation.read_stable_snapshot(declaration_path, "declaration")
        output = self.root / "proofless.json"
        staged = output.with_name(
            f".{output.name}.{'2' * 32}{confirmation.CONFIRMATION_STAGED_SUFFIX}"
        )
        staged.write_bytes(confirmation.canonical_json_bytes(document))
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "staging path"
        ):
            confirmation.inspect_confirmation_publication(
                output, document, self.context, snapshot
            )

        staged.unlink()
        atomic_publish._write_proof_exclusive(
            confirmation.confirmation_proof_path(output),
            source=staged,
            destination=output,
            identity=atomic_publish.FileIdentity(1, 1, len(confirmation.canonical_json_bytes(document))),
        )
        staged.write_bytes(confirmation.canonical_json_bytes(document))
        declaration_path.write_bytes(declaration_path.read_bytes() + b" ")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "bound input changed"
        ):
            confirmation.inspect_confirmation_publication(
                output, document, self.context, snapshot
            )

    def test_same_path_accepts_drive_unc_parent_alias_but_not_different_hardlink_name(self) -> None:
        with mock.patch.object(confirmation.os.path, "samefile", return_value=True):
            self.assertTrue(
                confirmation._same_path(
                    Path(r"Y:\git\pyaedt_motor\confirmation.json"),
                    Path(r"\\RaiDrive-peets\ANSYS\git\pyaedt_motor\confirmation.json"),
                )
            )
            self.assertFalse(
                confirmation._same_path(
                    Path(r"Y:\git\pyaedt_motor\confirmation.json"),
                    Path(r"\\RaiDrive-peets\ANSYS\git\pyaedt_motor\alias.json"),
                )
            )

    def test_load_context_requires_exact_stage3_spec_and_immutable_source_hashes(self) -> None:
        contract_path = self.root / "pipeline-contract.json"
        contract_path.write_text('{"fixture":"pipeline"}\n', encoding="utf-8")
        source_path = Path(optimization.__file__).resolve()
        if source_path.suffix.lower() == ".pyc":
            source_path = source_path.with_suffix(".py").resolve()
        fake_contract = SimpleNamespace(
            source=contract_path.resolve(),
            workdir=self.root,
            contract_sha256="c" * 64,
            stage3=SimpleNamespace(
                generate_argv=("python", "generate_ipmsm_v2_cases.py", "--spec", str(self.spec_path))
            ),
            optimization=SimpleNamespace(
                argv_template=(
                    "python",
                    "continue_ipmsm_v2_optimization.py",
                    "--optimization-spec",
                    str(self.spec_path),
                )
            ),
            immutable_inputs=(
                supervisor.Artifact(self.spec_path, sha256(self.spec_path)),
                supervisor.Artifact(source_path, sha256(source_path)),
            ),
        )
        with mock.patch.object(supervisor, "load_contract", return_value=fake_contract):
            context = confirmation.load_bound_context(contract_path)
        self.assertEqual(context.spec_sha256, sha256(self.spec_path))
        self.assertEqual(context.implementation_sha256, sha256(source_path))

        other_spec = self.root / "other-spec.json"
        other_spec.write_bytes(self.spec_path.read_bytes())
        mismatched_reference = copy.copy(fake_contract)
        mismatched_reference.optimization = SimpleNamespace(
            argv_template=(
                "python",
                "continue_ipmsm_v2_optimization.py",
                "--optimization-spec",
                str(other_spec),
            )
        )
        with mock.patch.object(supervisor, "load_contract", return_value=mismatched_reference):
            with self.assertRaisesRegex(
                confirmation.OptimizationInputConfirmationError,
                "reference different specs",
            ):
                confirmation.load_bound_context(contract_path)

        duplicate = copy.copy(fake_contract)
        duplicate.immutable_inputs = (
            *fake_contract.immutable_inputs,
            supervisor.Artifact(self.spec_path, sha256(self.spec_path)),
        )
        with mock.patch.object(supervisor, "load_contract", return_value=duplicate):
            with self.assertRaisesRegex(
                confirmation.OptimizationInputConfirmationError,
                "optimization spec must occur exactly once",
            ):
                confirmation.load_bound_context(contract_path)

    def test_v4_context_binds_envelope_base_and_pinned_helper(self) -> None:
        v4_path = self.root / "pipeline-v4.json"
        raw = {
            "schema_version": "ipmsm-v2-pipeline-contract-v4",
            "contract_sha256": "d" * 64,
            "pipeline": {"fixture": True},
        }
        v4_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
        v4_snapshot, v4_document = confirmation._read_json_snapshot(
            v4_path, "v4 fixture"
        )
        _, base_document = confirmation._read_json_snapshot(
            self.contract_path, "base fixture"
        )
        helper_path = Path(confirmation.__file__).resolve()
        fake_contract = SimpleNamespace(
            source=v4_path.resolve(),
            source_sha256=sha256(v4_path),
            canonical_sha256=supervisor._canonical_sha256(v4_document),
            contract_sha256="d" * 64,
            base_contract=SimpleNamespace(source=self.contract_path.resolve()),
            base_contract_binding=SimpleNamespace(
                path=self.contract_path.resolve(),
                sha256=sha256(self.contract_path),
                canonical_sha256=supervisor._canonical_sha256(base_document),
                contract_sha256="a" * 64,
            ),
            source_pins={
                "confirmation_helper": SimpleNamespace(
                    path=helper_path,
                    sha256=sha256(helper_path),
                )
            },
        )
        fake_v4 = SimpleNamespace(
            load_contract=mock.Mock(return_value=fake_contract),
            audit_contract=mock.Mock(),
            v3=supervisor,
        )
        with mock.patch.object(
            confirmation.importlib, "import_module", return_value=fake_v4
        ), mock.patch.object(
            confirmation, "load_bound_context", return_value=self.context
        ):
            context = confirmation._load_v4_bound_context(v4_snapshot, v4_document)
        bindings = confirmation._context_bindings(context)
        self.assertEqual(bindings["contract"]["path"], str(v4_path.resolve()))
        self.assertEqual(
            bindings["contract"]["canonical_sha256"],
            supervisor._canonical_sha256(v4_document),
        )
        self.assertEqual(
            bindings["base_contract"]["canonical_sha256"],
            supervisor._canonical_sha256(base_document),
        )
        self.assertEqual(context.spec_sha256, self.context.spec_sha256)

    def test_secure_snapshot_rejects_hardlink_alias(self) -> None:
        source = self.root / "hardlink-source.json"
        alias = self.root / "hardlink-alias.json"
        source.write_text('{"value":1}\n', encoding="utf-8")
        try:
            os.link(source, alias)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaisesRegex(
            confirmation.OptimizationInputConfirmationError, "hardlink"
        ):
            confirmation.read_stable_snapshot(alias, "hardlink fixture")


if __name__ == "__main__":
    unittest.main()
