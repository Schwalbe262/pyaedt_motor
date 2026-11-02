"""New production/profile-only boundaries; no Native or network."""
import ast
import copy
import json
from pathlib import Path
import unittest

from pyaedt_motor import execution_evidence as evidence, execution_contracts as contracts, execution_compatibility as compatibility

class MemoryFiles:
    def __init__(self, values): self.values = values
    def json(self, key): return self.values[key]
    def reference(self, item): return item["path"]


class ProductionEvidence(unittest.TestCase):
    def test_actual_public_builder_input_reconstruction_and_control_tamper(self):
        import run_ipmsm_batch as runner
        from ipmsm_optimization import GEOMETRY_VARIABLE_NAMES
        from tests._archive_numeric_fixture import raw_fixture
        case = json.loads(raw_fixture("case.json")); builder = json.loads(raw_fixture("builder.json"))
        spec = runner.build_spec(case)
        self.assertEqual(evidence._verify_production_builder_inputs(runner, case, spec, builder), 117)
        keys = [*("input_"+name for name in GEOMETRY_VARIABLE_NAMES), "input_i_peak_a", "input_phase_resistance_ohm",
                "input_steps_per_period", "input_transient_periods", "input_source_case_id", "geometry_group_id",
                "input_save_fields_start_step", "input_save_fields_stop_step", "input_save_fields_every_n_steps"]
        for key in keys:
            changed = dict(builder)
            value = changed.get(key)
            changed[key] = (1 if value is None else float(value) + 1) if key.startswith("input_") and key != "input_source_case_id" else "tampered"
            with self.subTest(key=key), self.assertRaises(ValueError):
                evidence._verify_production_builder_inputs(runner, case, spec, changed)
        changed = dict(builder); changed["input_mesh_elements"] = "{'band': 2000, 'magnet': 1}"
        with self.assertRaisesRegex(ValueError, "structured input"):
            evidence._verify_production_builder_inputs(runner, case, spec, changed)

    def test_old_diagnostic_context_and_sdk_dispatch_unchanged(self):
        from tests import _legacy_cli_fixture as legacy
        old = ast.parse(legacy.raw("legacy_db2_helper.py"))
        new = ast.parse(Path(evidence.__file__).read_bytes())
        for name in ["_context", "solver_context", "_replay_metrics", "_provenance"]:
            a = next(n for n in old.body if isinstance(n, ast.FunctionDef) and n.name == name)
            b = next(n for n in new.body if isinstance(n, ast.FunctionDef) and n.name == name)
            self.assertEqual(ast.dump(a, include_attributes=False), ast.dump(b, include_attributes=False), name)
        old_context = next(n for n in old.body if isinstance(n, ast.FunctionDef) and n.name == "_context")
        prod_context = next(n for n in new.body if isinstance(n, ast.FunctionDef) and n.name == "_production_context")
        # Exact existing complete owner/lineage/intent/quiet suffix, not a weaker substitute.
        start = next(i for i,n in enumerate(old_context.body) if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
                     and any(isinstance(x,ast.Constant) and x.value=="pilot does not prove separate one-shot CLI completion" for x in ast.walk(n)))
        suffix = old_context.body[start:]
        self.assertEqual([ast.dump(n,include_attributes=False) for n in suffix],
                         [ast.dump(n,include_attributes=False) for n in prod_context.body[-len(suffix):]])

    def test_production_entry_rejects_old_schema_before_owner_or_any_native(self):
        data = {"contract": {"schema": evidence.PILOT_SCHEMA, "mode": "solve", "case_id": "fixture"}, "pilot_result": {}}
        with self.assertRaisesRegex(ValueError, "production contract"):
            evidence._production_context(MemoryFiles(data), "fixture", "producer_v3:sha256:" + "a" * 64)

    def test_typed_attempt_rejected_before_owned_suffix(self):
        producer = "producer_v3:sha256:" + "a" * 64
        contract = {"schema": contracts.SCHEMA, "backend": evidence.CLI, "mode": "solve", "stage": "training", "purpose": "training",
                    "case_id": "fixture", "expected_producer": producer, "options": contracts.OPTION_POLICY,
                    "source_files": {"src/pyaedt_motor/batch_ownership.py": {"sha256": evidence.__dict__.get("OWNER_SHA", "9c0a5c364d19e062f40cb6337a0df60b04c8f16eff00f5250245ea82bd7066f5")}},
                    "case": {"path": "case", "sha256": "b" * 64}, "reservation_receipt": {"path": "receipt"}}
        receipt = {"execution_authorized": True, "case_id": "fixture", "producer_fingerprint": producer, "category": "training",
                   "stage": "training", "reserved_attempt": True, "budget_attempt": 1, "case_sha256": "b" * 64,
                   "execution_contract_sha256": contracts.contract_sha256(contract)}
        data = {"contract": contract, "pilot_result": {}, "case": {"case_id": "fixture", "aedt_version": "2025.2"}, "receipt": receipt}
        with self.assertRaisesRegex(ValueError, "whole acyclic authority"):
            evidence._production_context(MemoryFiles(data), "fixture", producer)

    def test_profile_only_semantic_scope_never_grants_training_or_rewrites_failure(self):
        value = {"known_normal": True, "numeric_physics_passed": True, "full_saved_passed": True,
                 "complete_Native_installation": True, "physics_key": "fixture", "original_status": "solved_saved_closure_unproven",
                 "profile_reuse_scope_approved": True, "training_admission": False, "raw_full_saved_passed": False}
        self.assertEqual(compatibility.profile_case_issues(value, "fixture"), [])
        self.assertEqual(value["original_status"], "solved_saved_closure_unproven")
        self.assertFalse(value["training_admission"] or value["raw_full_saved_passed"])
        value["profile_reuse_scope_approved"] = False
        self.assertIn("CLI_profile_evidence:original_status", compatibility.profile_case_issues(value, "fixture"))


if __name__ == "__main__": unittest.main()
