"""Portable full upstream admission with genuine numerical bytes and INERT authority.

No upstream validator is replaced. The downstream predictor is analytic unit data;
this is not an actual campaign certificate, model accuracy result, or issued grant.
"""
from pathlib import Path
import copy
import json
import shutil
import tempfile
import unittest
import pandas as pd
from tests import _owned_cli_fixture as fixture
from pyaedt_motor import execution_archive as archive, execution_evidence as evidence
from pyaedt_motor import execution_compatibility as compatibility, public_batch_pilot as pilot
from pyaedt_motor import reference_frames as frames, train_physical, model_bundle, optimize_physical


class PortableFullPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="af_", dir=Path(__file__).resolve().parents[2])
        cls.root = Path(cls.temporary.name)
        live = cls.root / "live"; live.mkdir()
        cls.profile = fixture.build(live / "profile")
        cls.production = fixture.production(cls.profile, live / "production", legal_model_constant=True)
        ip, sha, root = cls.production.write()
        entry = {"contract": cls.production.ref("contract"), "cli_index":
                 {"index_path": str(ip), "index_sha256": sha, "root": str(root)}}
        cls.original = evidence.verify_production_cli_evidence(**entry["cli_index"],
            case_id=cls.production.case_id, producer=cls.production.producer)
        if cls.original["errors"]: raise AssertionError(cls.original["errors"])
        cls.packed = archive.pack_production_inputs({cls.production.case_id: entry}, cls.root / "packed")
        cls.moved = cls.root / "moved"; shutil.copytree(cls.root / "packed", cls.moved)
        assert live.resolve().is_relative_to(cls.root.resolve()) and live.resolve() != cls.root.resolve()
        shutil.rmtree(live)  # Only this test's synthetic live graph, after complete transport.
        cls.declaration = cls.moved / "production_inputs.json"
        cls.inputs = archive.production_inputs_document({"path": str(cls.declaration), **pilot.record(cls.declaration)}, compatibility.Reads())
        cls.entry = cls.inputs["cases"][cls.production.case_id]
        cls.after = evidence.verify_production_cli_evidence(**cls.entry["cli_index"],
            case_id=cls.production.case_id, producer=cls.production.producer)
        if cls.after["errors"]: raise AssertionError(cls.after["errors"])
        cls.frame = pd.DataFrame([cls.after["production_row"]])

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def test_full_relocated_upstream_train_domain_and_optimizer_dispatch(self):
        self.assertEqual(self.after["production_row"], self.original["production_row"])
        self.assertTrue(self.after["production_eligible"] and self.after["training_eligible"])
        self.assertFalse(self.after["SDK_Analyze_return_synthesized"])
        self.assertFalse(self.after["production_row"]["analyze"])
        self.assertFalse(self.after["raw_pilot_training_allowed"])
        self.assertFalse(self.after["raw_public_training_admission"])
        self.assertTrue(self.production.fixture_inactive_constant_scope["active_geometry_hash_unchanged"])
        self.assertFalse((self.root / "live").exists())
        train_physical.validate_new_data(self.frame, cli_inputs=self.inputs)
        location = self.packed["family_evidence_locations"][0]
        origin = frames.LocalEvidence(self.moved / location["mapping"], self.moved / location["root"])
        typed = compatibility.ProductionFrameEvidence(origin, {"path": str(self.declaration), **pilot.record(self.declaration)})
        from tests.test_ipmsm_physical_bundle import motor_inputs
        from tests.test_ipmsm_physical_optimizer import AnalyticBundle, source_spec
        columns = list(motor_inputs([43.61808465793729], [100.], [30.], [1200.]).columns)
        numeric = self.frame[columns].apply(pd.to_numeric)
        raw = source_spec(); raw["material_context"] = self.profile["manifest"]["material_context"]
        spec = frames.apply_reference(raw, self.profile["manifest"], typed)
        domain = model_bundle.domain_from_training(numeric, spec, case_frame=self.frame, frame_evidence=typed)
        class MutatingAnalyticBundle(AnalyticBundle):
            tamper = None
            def predict(self, *args, **kwargs):
                result = super().predict(*args, **kwargs)
                if self.tamper is not None:
                    self.tamper.write_bytes(self.tamper.read_bytes() + b" ")
                return result
        bundle = MutatingAnalyticBundle()
        fps = {name: str(self.frame.iloc[0][name]) for name in model_bundle.REQUIRED_FINGERPRINTS}
        bundle.metadata.update(fingerprints=fps, spec_sha256=optimize_physical._digest(spec), domain=domain)
        adapter = optimize_physical.PhysicalBundleAdapter(bundle, spec, fps, self.profile["manifest"], frame_evidence=typed)
        geometry = {name: float(self.frame.iloc[0]["input_" + name]) for name in raw["design_space"]}
        geometry.update(stator_shoe_thick=1.5, stack_length_mm=float(self.frame.iloc[0]["input_stack_length_mm"]))
        prediction = adapter.predict_controls(geometry, adapter.spec.operating_points[0], [(100., 30.)])
        self.assertEqual(len(prediction.point), 1)
        # Public prediction must recheck the collected archive itself. An
        # external assert after prediction would hide a missing runtime guard.
        index = json.loads(Path(self.entry["cli_index"]["index_path"]).read_bytes())
        role = index["files"]["cli_completion"]
        path = Path(self.entry["cli_index"]["root"]) / role["path"]
        raw_completion = path.read_bytes()
        try:
            path.write_bytes(raw_completion + b" ")
            with self.assertRaises(ValueError):
                adapter.predict_controls(geometry, adapter.spec.operating_points[0], [(100., 30.)])
            with self.assertRaises(ValueError):
                adapter.build_inputs(geometry, adapter.spec.operating_points[0], [(100., 30.)])
        finally: path.write_bytes(raw_completion)
        # A model callback cannot change authority during inference and return
        # apparently valid numbers before the public method's closing recheck.
        bundle.tamper = path
        try:
            with self.assertRaises(ValueError):
                adapter.predict_controls(geometry, adapter.spec.operating_points[0], [(100., 30.)])
        finally:
            bundle.tamper = None
            path.write_bytes(raw_completion)
        from tests._checked_search_tests import exercise
        selected = self.moved / self.packed["selected_profile_locations"][0]["path"]
        exercise(self, adapter, geometry, typed, self.entry, selected)
        from tests._checked_search_tests import public_cli_dispatch
        public_cli_dispatch(self, adapter, geometry, typed, selected, self.root / "public_cli")

    def test_collected_bytes_tamper_is_rejected_with_original_live_tree_absent(self):
        index = json.loads(Path(self.entry["cli_index"]["index_path"]).read_bytes())
        role = index["files"]["cli_completion"]
        path = Path(self.entry["cli_index"]["root"]) / role["path"]
        raw = path.read_bytes()
        try:
            path.write_bytes(raw + b" ")
            result = evidence.verify_production_cli_evidence(**self.entry["cli_index"],
                case_id=self.production.case_id, producer=self.production.producer)
            self.assertFalse(result["training_eligible"])
            self.assertTrue(result["errors"])
        finally: path.write_bytes(raw)

    def test_cli_cannot_be_laundered_as_sdk_or_mixed_with_another_backend(self):
        row = self.frame.iloc[0].to_dict()
        disguised = {**row, "input_execution_backend": "standalone"}
        with self.assertRaisesRegex(ValueError, "SDK row carries CLI"):
            train_physical.validate_cli_training_inputs(pd.DataFrame([disguised]))
        with self.assertRaisesRegex(ValueError, "mixed SDK/CLI"):
            train_physical.validate_cli_training_inputs(pd.DataFrame([row, disguised]), self.inputs)
        bad = copy.deepcopy(self.inputs)
        bad["cases"]["other_case"] = bad["cases"].pop(self.production.case_id)
        with self.assertRaisesRegex(ValueError, "case-specific"):
            train_physical.validate_cli_training_inputs(self.frame, bad)


if __name__ == "__main__": unittest.main()
