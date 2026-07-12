from __future__ import annotations

from contextlib import redirect_stdout
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

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
        with self.assertRaises(FileExistsError):
            confirmation.publish_confirmation(
                output, document, self.context, declaration_snapshot
            )
        self.assertEqual(output.read_bytes(), original)

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

    def test_publication_rolls_back_only_owned_output_when_post_audit_fails(self) -> None:
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
        self.assertFalse(output.exists())

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


if __name__ == "__main__":
    unittest.main()
