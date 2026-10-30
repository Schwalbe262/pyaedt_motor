"""Selected numerical profiles retain the material/wire identity of real rows."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from pyaedt_motor.doe import load_selected_profile
from pyaedt_motor.quality import compare_profiles, selected_profile_manifest
from tests.test_ipmsm_v3_quality import cases, result, summary


MATERIAL = {"core_material_model": "m19_29g_reference_y2_v1",
            "core_material": "M19_29G_reference_y2"}
LEGACY = {"core_material_model": "legacy_pnf_hybrid_v1",
          "core_material": "27PNF1500_CustomCoreLoss"}
WIRE = {"series_turns_per_phase": 48, "turns_per_coil_side": 12,
        "conductor_area_per_branch_mm2": 15.3206469, "slot_fill_limit": 0.45}


def material_cases(group=1):
    plan = cases(group, refined=True)
    for row in plan:
        row.update(MATERIAL, **WIRE)
    return plan


def compare(plan, rows=None):
    counts = {row["case_id"]: 40000 if row["quality_profile"] == "refined" else 20000 for row in plan}
    return compare_profiles([result(row) for row in plan] if rows is None else rows, plan,
                            reference_profile="reference", refined_reference_profile="refined", mesh_counts=counts)


class QualityMaterialContextTests(unittest.TestCase):
    def export(self, report, root):
        path = root / "comparison.json"
        path.write_text(json.dumps(report, allow_nan=False), encoding="utf-8")
        selected = selected_profile_manifest(report, path, "fast")
        selected_path = root / "selected.json"
        selected_path.write_text(json.dumps(selected, allow_nan=False), encoding="utf-8")
        return selected, load_selected_profile(selected_path)

    def test_reference_export_and_loader_roundtrip_retain_actual_context(self):
        report = compare(material_cases())
        self.assertTrue(summary(report)["production_profile_eligible"])
        with tempfile.TemporaryDirectory() as tmp:
            selected, loaded = self.export(report, Path(tmp))
        self.assertEqual(selected["material_context"], MATERIAL)
        self.assertEqual(selected["winding_context"], WIRE)
        self.assertEqual(selected["material_fingerprint"], "materials_v2:sha256:" + "b"*64)
        self.assertEqual(loaded.material_context, MATERIAL)
        self.assertEqual(loaded.winding_context, WIRE)
        self.assertEqual(loaded.material_fingerprint, selected["material_fingerprint"])
        self.assertTrue(all(row["source"] == "explicit_row_metadata" for row in report["material_context_provenance"]))
        self.assertFalse(set(selected["settings"]) & set(MATERIAL | WIRE))

    def test_context_is_derived_from_actual_rows_when_plan_has_no_new_fields(self):
        plan = cases(refined=True)
        rows = [result(row) for row in plan]
        for row in rows:
            row.update({"input_" + key: value for key, value in (MATERIAL | WIRE).items()})
        report = compare(plan, rows)
        self.assertEqual(report["material_context"], MATERIAL)
        self.assertEqual(report["winding_context"], WIRE)
        self.assertTrue(summary(report)["production_profile_eligible"])

    def test_missing_reference_material_or_winding_metadata_blocks_export(self):
        plan = material_cases()
        for field in ["core_material_model", "core_material", *WIRE]:
            with self.subTest(field=field):
                rows = [result(row) for row in plan]
                rows[1].pop("input_" + field)
                report = compare(plan, rows)
                self.assertFalse(summary(report)["production_profile_eligible"])
                self.assertTrue(report["global_issues"])
                self.assertEqual(len(report["comparisons"][0]["metrics"]), 10)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "bad.json"
                    path.write_text(json.dumps(report), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        selected_profile_manifest(report, path, "fast")

    def test_unknown_name_only_grade_never_infers_reference_or_legacy_identity(self):
        for name in ["M19_29G_reference_y2", "30PNF1600", "custom_steel"]:
            plan = cases(refined=True)
            rows = [result(row) for row in plan]
            for row in rows:
                row["input_core_material"] = name
            with self.subTest(material=name):
                report = compare(plan, rows)
                self.assertFalse(summary(report)["production_profile_eligible"])
                self.assertIsNone(report["material_context"])
                self.assertTrue(any("missing_core_material_model" in issue for issue in report["global_issues"]))

    def test_mixed_context_cannot_hide_behind_identical_fingerprints(self):
        plan = material_cases(1) + material_cases(2)
        for row in plan[3:]:
            row.update(LEGACY)
        rows = [result(row) for row in plan]
        report = compare(plan, rows)
        self.assertIn("mixed_material_context", report["global_issues"])
        self.assertFalse(summary(report)["production_profile_eligible"])
        self.assertTrue(all(len(pair["metrics"]) == 10 for pair in report["comparisons"]))

    def test_uniform_context_requires_same_declared_wire_across_geometry_groups(self):
        plan = material_cases(1) + material_cases(2)
        for row in plan[3:]:
            row["conductor_area_per_branch_mm2"] = 15.5
        report = compare(plan)
        self.assertIn("mixed_winding_context", report["global_issues"])
        self.assertFalse(summary(report)["production_profile_eligible"])

    def test_plan_wire_and_model_claims_must_match_actual_rows(self):
        for key, value in [("conductor_area_per_branch_mm2", 15.5),
                           ("slot_fill_limit", 0.44),
                           ("core_material_model", "legacy_pnf_hybrid_v1")]:
            plan = material_cases()
            rows = [result(row) for row in plan]
            plan[1][key] = value
            with self.subTest(field=key):
                report = compare(plan, rows)
                self.assertFalse(summary(report)["production_profile_eligible"])
                self.assertIn("plan_mismatch:" + key, report["comparisons"][0]["issues"])

    def test_reference_requires_valid_uniform_actual_material_fingerprint(self):
        plan = material_cases()
        for value in ["", "materials_v1:unknown", "materials_v2:sha256:" + "c"*64]:
            rows = [result(row) for row in plan]
            rows[1]["input_material_fingerprint"] = value
            with self.subTest(fingerprint=value):
                report = compare(plan, rows)
                self.assertFalse(summary(report)["production_profile_eligible"])
                self.assertTrue(report["global_issues"])

    def test_inconsistent_model_name_and_invalid_wire_values_are_rejected(self):
        plan = material_cases()
        for key, value in [("core_material", "27PNF1500_CustomCoreLoss"),
                           ("core_material_model", "unknown_reference_v1"),
                           ("slot_fill_limit", True), ("slot_fill_limit", 1.1),
                           ("conductor_area_per_branch_mm2", 0),
                           ("conductor_area_per_branch_mm2", float("nan"))]:
            rows = [result(row) for row in plan]
            rows[1]["input_" + key] = value
            with self.subTest(field=key, value=value):
                report = compare(plan, rows)
                self.assertFalse(summary(report)["production_profile_eligible"])
                json.dumps(report, allow_nan=False)

    def test_legacy_name_only_roundtrip_records_absent_model_without_inventing_wire(self):
        plan = cases(refined=True)
        rows = [result(row) for row in plan]
        for row in rows:
            row["input_core_material"] = LEGACY["core_material"]
        report = compare(plan, rows)
        self.assertTrue(summary(report)["production_profile_eligible"])
        self.assertEqual(report["material_context"], LEGACY)
        self.assertTrue(all(p["source"] == "legacy_name_only_model_id_absent" for p in report["material_context_provenance"]))
        with tempfile.TemporaryDirectory() as tmp:
            selected, loaded = self.export(report, Path(tmp))
        self.assertEqual(loaded.material_context, LEGACY)
        self.assertIsNone(loaded.winding_context)
        self.assertNotIn("winding_context", selected)

    def test_truly_old_legacy_roundtrip_and_partial_new_wire_rejection(self):
        plan = cases(refined=True)
        report = compare(plan)
        self.assertTrue(summary(report)["production_profile_eligible"])
        self.assertTrue(all(p["source"] == "legacy_default_both_fields_absent" for p in report["material_context_provenance"]))
        with tempfile.TemporaryDirectory() as tmp:
            selected, loaded = self.export(report, Path(tmp))
        self.assertEqual(selected["material_context"], LEGACY)
        self.assertIsNone(loaded.winding_context)
        rows = [result(row) for row in plan]
        rows[1]["input_conductor_area_per_branch_mm2"] = 15.3206469
        rejected = compare(plan, rows)
        self.assertFalse(summary(rejected)["production_profile_eligible"])

    def test_export_requires_complete_reference_context_even_in_supplied_report(self):
        accepted = compare(material_cases())
        for key in ["material_context", "material_fingerprint", "winding_context", "material_context_provenance"]:
            report = dict(accepted)
            report.pop(key)
            with self.subTest(field=key), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "incomplete.json"
                path.write_text(json.dumps(report), encoding="utf-8")
                with self.assertRaises(ValueError):
                    selected_profile_manifest(report, path, "fast")


if __name__ == "__main__":
    unittest.main()
