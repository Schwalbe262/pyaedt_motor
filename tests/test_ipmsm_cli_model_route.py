"""Real downstream domain/optimizer dispatch with a deliberately inert FS gate.

The upstream production proof is a test double, never an actual certificate.
Actual raw-index/frame/semantic gates have their separate tests. No Native.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from pyaedt_motor import execution_compatibility as compat, reference_frames as frames
from pyaedt_motor import model_bundle, optimize_physical as optimization
from tests.test_ipmsm_reference_frames import Fixture
from tests.test_ipmsm_physical_bundle import motor_inputs, known_motor
from tests.test_ipmsm_physical_optimizer import validation_profile, baseline


class CliModelRoute(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); fixture = Fixture(self.root)
        self.origin = fixture.evidence()
        self.manifest = json.loads(frames.canonical(frames.draft_reference(fixture.origin, self.origin, assign_identity=True)))
        self.row = copy.deepcopy(fixture.rows[0])
        self.inputs = motor_inputs([55.], [80.], [30.], [1200.])
        self.row.update(self.inputs.iloc[0].to_dict()); self.row.update(known_motor(self.inputs).iloc[0].to_dict())
        self.row.update(case_id="inert_production_case", status="ok", analyze=False,
            input_execution_backend="public_owned_cli_v1", input_beta_calibration_id=self.manifest["reference_frame_id"],
            input_electrical_zero_deg=self.manifest["electrical_zero_deg"], input_operation="sin_current")
        self.row.update(geometry_group_id="inert_geometry", design_hash="inert_design", doe_split="train")
        contract = {"case_id": self.row["case_id"], "purpose": "training", "expected_producer": self.manifest["producer_fingerprint"]}
        cp = self.root / "contract.json"; cp.write_bytes(frames.canonical(contract))
        ip = self.root / "inert_index.json"; ip.write_bytes(frames.canonical({"schema": compat.db2.INDEX_SCHEMA,
            "files": {"contract": {"path": "contract.json", "original_path": str(cp), **compat.pilot.record(cp)}}}))
        declared = {"schema": "owned_cli_production_training_inputs_v2", "cases": {
            self.row["case_id"]: {"contract": {"path": str(cp), **compat.pilot.record(cp)},
              "cli_index": {"index_path": str(ip), "index_sha256": compat.pilot.record(ip)["sha256"], "root": str(self.root)}}}}
        self.dp = self.root / "cli_inputs.json"; self.dp.write_bytes(frames.canonical(declared))
        self.evidence = compat.ProductionFrameEvidence(self.origin, {"path": str(self.dp), **compat.pilot.record(self.dp)})
        self.proof = {"training_eligible": True, "production_eligible": True, "errors": [],
            "solver_context_known": True, "solver_state_uncertain": False, "production_row": self.row,
            "exact_contract_sha256": "a" * 64, "physics_key": "inert_physics",
            "actual_family_frame": {"reference_frame_id": self.manifest["reference_frame_id"]}}

    def test_domain_then_optimizer_predict_uses_explicit_cli_audits_not_sdk(self):
        old, _, _ = validation_profile(self.root)
        spec = frames.apply_reference(old.source_spec, self.manifest, self.evidence)
        frame = pd.DataFrame([self.row])
        original_audit = frames._case_audit
        def audit_origin_only(bundle, *args, **kwargs):
            if bundle["input"][0]["case_id"] == self.row["case_id"]:
                raise AssertionError("SDK-only production case audit must not execute")
            return original_audit(bundle, *args, **kwargs)
        with patch.object(compat.db2, "verify_production_cli_evidence", return_value=self.proof), \
             patch.object(frames, "_case_audit", side_effect=audit_origin_only):
            # Real model-domain path; no conditional skip after the FS gate.
            domain = model_bundle.domain_from_training(self.inputs, spec, case_frame=frame, frame_evidence=self.evidence)
            context = domain["reference_frame_context"]
            self.assertEqual(context["binding_method"], "actual_owned_CLI_production_rows_v2")
            frames.verify_domain(domain, self.manifest, self.evidence)
            bundle = old.bundle; fps = copy.deepcopy(old.fingerprints)
            fps.update(input_producer_fingerprint=self.manifest["producer_fingerprint"],
                       input_beta_calibration_id=self.manifest["reference_frame_id"])
            bundle.metadata.update(fingerprints=fps, spec_sha256=optimization._digest(spec))
            # Analytic predictive domain remains synthetic. Its admission
            # context comes from the actual downstream domain call above.
            bundle.metadata["domain"]["reference_frame_context"] = context
            adapter = optimization.PhysicalBundleAdapter(bundle, spec, fps, self.manifest, frame_evidence=self.evidence)
            batch = adapter.predict_controls(baseline(), adapter.spec.operating_points[0], [(80., 30.)])
            self.assertEqual(len(batch.point), 1)
            self.assertFalse(self.row["analyze"])
            with patch.object(optimization.doe, "load_selected_profile", side_effect=ValueError("inert real-profile gate stops here")) as loader:
                with self.assertRaisesRegex(ValueError, "real-profile gate"):
                    optimization.build_fea_validation_rows(adapter, [], self.root / "not_certified.json")
                loader.assert_called_once_with(self.root / "not_certified.json", execution_backend="public_owned_cli_v1")

    def test_every_optimization_input_target_group_and_frame_is_cross_bound(self):
        from ipmsm_optimization import GEOMETRY_VARIABLE_NAMES
        keys = [*model_bundle.ALL_TARGETS, *("input_"+name for name in GEOMETRY_VARIABLE_NAMES), "input_stack_length_mm", "input_i_peak_a", "input_beta_dq_deg",
                "input_base_rpm", "input_steps_per_period", "case_id", "input_beta_calibration_id",
                "input_producer_fingerprint", "geometry_group_id", "design_hash", "doe_split"]
        with patch.object(compat.db2, "verify_production_cli_evidence", return_value=self.proof):
            self.evidence.verify_rows([self.row], self.manifest)
            for key in keys:
                row = copy.deepcopy(self.row)
                row[key] = float(row[key]) + 1 if isinstance(row.get(key), (float, int)) else "tampered"
                with self.subTest(key=key), self.assertRaises((ValueError, KeyError)):
                    self.evidence.verify_rows([row], self.manifest)

    def test_raw_case_evidence_is_rechecked_at_model_publication(self):
        with patch.object(compat.db2, "verify_production_cli_evidence", return_value=self.proof):
            self.evidence.verify_rows([self.row], self.manifest)
        (self.root / "contract.json").write_bytes(b"changed after verification")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.evidence.assert_unchanged()

    def test_case_authority_cannot_be_replaced_in_memory_after_sealed_read(self):
        self.evidence.cases[self.row["case_id"]]["cli_index"]["index_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "authority changed in memory"):
            self.evidence.proof(self.row["case_id"])
        with self.assertRaisesRegex(ValueError, "authority changed in memory"):
            self.evidence.assert_unchanged()

    def test_mixed_physics_keys_cannot_share_model_domain(self):
        other = {**self.row, "case_id": "second_inert_case"}
        proofs = [self.proof, {**self.proof, "production_row": other, "physics_key": "different_inert_physics"}]
        # This isolates the downstream homogeneous-cohort rule. It does not
        # manufacture an upstream actual production certificate.
        with patch.object(self.evidence, "proof", side_effect=proofs):
            with self.assertRaisesRegex(ValueError, "mixed production"):
                self.evidence.verify_rows([self.row, other], self.manifest)

    def test_false_admission_and_missing_evidence_cannot_enter_model_domain(self):
        self.proof["training_eligible"] = False
        with patch.object(compat.db2, "verify_production_cli_evidence", return_value=self.proof):
            with self.assertRaisesRegex(ValueError, "remains false"):
                self.evidence.verify_rows([self.row], self.manifest)
        with self.assertRaisesRegex(ValueError, "case audit"):
            frames.verify_rows([self.row], self.manifest, self.origin)


if __name__ == "__main__": unittest.main()
