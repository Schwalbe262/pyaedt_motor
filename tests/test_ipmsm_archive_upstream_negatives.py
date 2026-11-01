"""Full-code positive/negative graph tests. All authorities/OS are INERT.

No verify_* function is stubbed; actual original data and tasks are untouched.
"""
from pathlib import Path
import copy,json,sys,tempfile,unittest
D=Path(__file__).resolve().parents[2]
from tests import _owned_cli_fixture as f

class UpstreamGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix="upstream_",dir=D)
        cls.root=Path(cls.temporary.name)
        cls.profile=f.build(cls.root/"profile")
        cls.production=f.production(cls.profile,cls.root/"production")
        cls.positive=cls.production.verify_production()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def negative(self,name):
        obj=copy.deepcopy(self.production);obj.root=self.root/name;obj.root.mkdir();return obj

    def test_genuinely_full_upstream_production_positive_without_stubs(self):
        result=self.positive
        self.assertEqual(result["errors"],[])
        self.assertEqual(result["status"],"APPROVED_EXPLICIT_PRODUCTION_CASE")
        self.assertTrue(result["training_eligible"]and result["production_eligible"]and result["physical_quality_passed"])
        self.assertTrue(result["actual_semantic_validation"]["semantic_equivalence_proven"])
        self.assertTrue(result["certified_selected_profile"]["full_spatial_and_time_certification"])
        self.assertTrue(result["isolated_numeric_imports"])
        self.assertFalse(result["production_row"]["analyze"])
        self.assertFalse(result["raw_pilot_training_allowed"])
        self.assertFalse(result["raw_public_training_admission"])
        self.assertTrue(self.production.case_id.startswith("INERT_"))

    def test_typed_receipt_attempt_rejected_after_all_dependent_seals_updated(self):
        obj=self.negative("bad_receipt");obj.data["receipt"]["budget_attempt"]=True
        ref=obj.ref("receipt");obj.data["contract"]["reservation_receipt"]=ref
        obj.data["source_inputs"]["reservation_receipt"]=ref;obj.data["cli_intent"]["reservation_receipt"]=ref;obj.reseal()
        result=obj.verify_production()
        self.assertFalse(result["training_eligible"])
        self.assertTrue(any("production receipt"in str(x)for x in result["errors"]),result["errors"])

    def test_builder_control_drift_rejected_after_resealing(self):
        obj=self.negative("bad_current");obj.data["builder_result"]["input_i_peak_a"]=101.;obj.reseal()
        result=obj.verify_production()
        self.assertFalse(result["production_eligible"])
        self.assertTrue(any("input_i_peak_a"in str(x)for x in result["errors"]),result["errors"])

    def test_missing_same_instance_preimage_never_becomes_approval(self):
        obj=self.negative("missing_before");obj.data.pop("prepared_full");obj.original.pop("prepared_full");obj.reseal()
        result=obj.verify_production()
        self.assertFalse(result["training_eligible"])
        self.assertTrue(result["errors"])

    def test_reference_self_is_not_spatial_time_refinement(self):
        root=self.profile["root"];import csv
        with (root/"comparison_rows.csv").open(encoding="utf-8",newline="")as stream:
            rows=list(csv.DictReader(stream))
        with (root/"comparison_plan.csv").open(encoding="utf-8",newline="")as stream:
            plan=list(csv.DictReader(stream))
        reference=self.profile["cases"][1];identity=f.ec.comparison_view(f.ec.derive_case(
            f.ec.verify_fs_capture(json.loads((root/"INERT_profile_inputs.json").read_bytes())["FS_capture"],
                json.loads((root/"INERT_profile_inputs.json").read_bytes())["FS_capture"]["sha256"])[reference.case_id],
                json.loads((root/"INERT_profile_inputs.json").read_bytes())["local_roles"][reference.case_id]))["profile_id"]
        report=f.quality.compare_profiles(rows,plan,reference_profile=identity,refined_reference_profile=identity,
            frame_manifest=self.profile["manifest"],frame_evidence=self.profile["origin_evidence"],
            cli_execution=self.profile["declaration"],cli_expected_key=self.profile["physics_key"])
        self.assertFalse(any(x["production_profile_eligible"]for x in report["profiles"]))
        self.assertTrue(any("time_refinement_unproven"in x["issues"]for x in report["reference_certification"]))

if __name__=="__main__":unittest.main()
