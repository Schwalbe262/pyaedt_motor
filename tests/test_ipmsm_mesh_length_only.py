"""Explicit spatial mesh policy; inert native objects and small real saved950 fixture."""
import copy
import csv
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest import mock
import module.ipmsm_ppt_setup as ppt
import run_ipmsm_batch as runner
from pyaedt_motor import doe, quality
from tests import test_ipmsm_v3_doe as doe_fixture
from tests.test_ipmsm_v3_quality import cases, result
from tests import test_ipmsm_ppt_setup_contract as contract_fixture

FIXTURE = json.loads((Path(__file__).parent / "fixtures/ipmsm_saved950_mesh_operations.json").read_text())
NAMES = {"magnets": ["PM"], "rotor": ["Rotor"], "stator": ["Stator"], "windings": ["Coil"], "band": ["Band"]}


def assigned(spec):
    calls = []
    def make(**kw):
        calls.append(kw)
        return NS(props={"RestrictElem": kw["maximum_elements"] is not None,
                         "NumMaxElem": str(kw["maximum_elements"] or 1000)}, update=lambda: True)
    m = NS(mesh=NS(assign_length_mesh=make))
    with mock.patch.object(ppt, "_get_group", side_effect=lambda m,g,k,f:g[k]):
        operations = ppt.assign_mesh(m, NAMES, spec)
    return operations, calls


class MeshLengthOnlyTests(unittest.TestCase):
    def test_absent_default_and_explicit_length_only_API_contract(self):
        spec = runner.build_spec({})
        self.assertEqual(spec.mesh_refinement_modes, {})
        operations, calls = assigned(spec)
        self.assertEqual([c["maximum_elements"] for c in calls], list(spec.mesh_elements.values()))
        self.assertTrue(all(o.props["RestrictElem"] for o in operations.values()))
        self.assertTrue(all(o.props["RestrictLength"] is False and o.props["MaxLength"] == "1mm" for o in operations.values()))
        chosen = runner.build_spec({"mesh_band_mode": "length_only", "mesh_band_max_length_mm": ".2"})
        operations, calls = assigned(chosen)
        self.assertIsNone(calls[-1]["maximum_elements"])
        self.assertEqual(calls[-1]["maximum_length"], "0.2mm")
        self.assertEqual(operations["band"].props, {"RestrictElem": False, "NumMaxElem": "1000", "RestrictLength": True, "MaxLength": "0.2mm"})
        self.assertTrue(all(c["maximum_elements"] is not None for c in calls[:-1]))
        replay = runner.build_spec({"input_mesh_band_mode": "length_only", "input_mesh_band_max_length_mm": ".2"})
        self.assertEqual(replay, chosen)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            runner.build_spec({"mesh_band_mode": "length_only", "mesh_band_max_length_mm": ".2", "input_mesh_band_max_length_mm": ".3"})

    def test_bad_groups_modes_missing_lengths_and_invalid_numbers_fail(self):
        invalid = [{"mesh_wrong_max_length_mm": ".2"}, {"mesh_wrong_mode": "length_only"}, {"mesh_band_mode": "cap"}, {"mesh_band_mode": True}, {"mesh_band_mode": "length_only"}]
        invalid += [{"mesh_band_mode": "length_only", "mesh_band_max_length_mm": x} for x in [True, False, 0, -1, "nan", "inf", "bad"]]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError): runner.build_spec(row)
        for modes in [{"wrong": "length_only"}, {"band": "cap"}, None]:
            with self.assertRaises(ValueError): ppt.validate_ppt_spec_contract(ppt.IPMSMPPTSpec(mesh_refinement_modes=modes))

    def test_real_saved950_budget_replay_and_independent_tamper_rejection(self):
        spec = ppt.IPMSMPPTSpec(mesh_elements=FIXTURE["mesh_elements"])
        audit = ppt.audit_mesh_operation_settings(FIXTURE["operations"], FIXTURE["objects_by_group"], spec, source="saved_AEDT")
        self.assertTrue(audit["passed"])
        self.assertTrue(audit["inactive_placeholders_verified"])
        self.assertFalse(audit["actual_mesh_length_verified"])
        for key, value in [("RestrictElem", False), ("RestrictLength", True), ("NumMaxElem", "1"), ("ApplyToInitialMesh", True), ("Objects", [999]), ("MaxLength", "Nonemm")]:
            bad = copy.deepcopy(FIXTURE["operations"]); bad["Mesh_band"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): ppt.audit_mesh_operation_settings(bad, FIXTURE["objects_by_group"], spec, source="saved_AEDT")

    def test_length_only_saved_flags_units_and_inactive_placeholder(self):
        spec = ppt.IPMSMPPTSpec(mesh_elements=FIXTURE["mesh_elements"], mesh_refinement_modes={"band": "length_only"}, mesh_maximum_lengths_mm={"band": .2})
        operations = copy.deepcopy(FIXTURE["operations"])
        operations["Mesh_band"].update(RestrictElem=False, NumMaxElem="1000", RestrictLength=True, MaxLength="0.2mm")
        self.assertTrue(ppt.audit_mesh_operation_settings(operations, FIXTURE["objects_by_group"], spec, source="saved_AEDT")["passed"])
        for key, value in [("MaxLength", "0.2cm"), ("MaxLength", "0.3mm"), ("NumMaxElem", "2000"), ("RestrictElem", True)]:
            bad = copy.deepcopy(operations); bad["Mesh_band"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): ppt.audit_mesh_operation_settings(bad, FIXTURE["objects_by_group"], spec, source="saved_AEDT")

    def test_native_OO_reads_assignment_and_flags_independent_of_cached_props(self):
        spec = ppt.IPMSMPPTSpec(mesh_elements=FIXTURE["mesh_elements"], mesh_refinement_modes={"band": "length_only"}, mesh_maximum_lengths_mm={"band": .2})
        props = {}
        for group in spec.mesh_elements:
            name = {"magnet": "PM", "winding": "Coil"}.get(group, group.title())
            props["Mesh_" + group] = {"Type": "Length Based", "Enabled": True, "Assignment": name,
                "Region": "Inside Selection", "Restrict Max Elems": group != "band", "Restrict Length": group == "band",
                "Apply To Initial Mesh": False, "Max Elems": str(spec.mesh_elements[group])}
        props["Mesh_band"].pop("Max Elems")  # Inactive fields can be hidden by OO.
        props["Mesh_band"]["Max Length"] = "0.2mm"
        class Node:
            def __init__(self, p): self.p = p
            def GetPropNames(self): return list(self.p)
            def GetPropValue(self, k): return self.p[k]
        mesh = NS(GetChildObject=lambda name: Node(props[name]))
        m = NS(odesign=NS(GetChildObject=lambda name: mesh))
        with mock.patch.object(ppt, "_get_group", side_effect=lambda m,g,k,f:g[k]):
            audit = ppt.audit_native_mesh_settings(m, NAMES, spec)
            self.assertTrue(audit["passed"])
            self.assertFalse(audit["inactive_placeholders_verified"])
            props["Mesh_band"]["Assignment"] = "Foreign"
            with self.assertRaises(ValueError): ppt.audit_native_mesh_settings(m, NAMES, spec)

    def test_validation_cannot_mutate_mesh_before_Analyze_or_setup_only_return(self):
        fixture = contract_fixture.IPMSMPPTSetupContractTests()
        spec = ppt.IPMSMPPTSpec(mesh_refinement_modes={"band": "length_only"}, mesh_maximum_lengths_mm={"band": .2})
        for analyze in [False, True]:
            app = mock.Mock(); app.validate_simple.return_value = 1
            with fixture.patched_configure_helpers(app), mock.patch.object(ppt, "audit_native_mesh_settings", side_effect=[{"passed": True}, ValueError("native mesh changed")]) as audit:
                with self.assertRaisesRegex(ValueError, "mesh changed"): ppt.configure_ipmsm_from_ppt(app, spec=spec, analyze=analyze)
                self.assertEqual(audit.call_count, 2)
                app.analyze.assert_not_called()

    def test_selected_profile_DOE_case_spec_roundtrip_and_omitted_rows_unchanged(self):
        fixture = doe_fixture.V3DoeTests(); fixture.setUp()
        try:
            rows0, _ = fixture.plan()
            fixture.profile_document["settings"]["mesh_refinement_modes"] = {}
            fixture.profile_path.write_text(json.dumps(fixture.profile_document))
            profile0 = doe.load_selected_profile(fixture.profile_path)
            rows_empty, _ = doe.generate_training_plan(fixture.spec, fixture.calibration, profile0, geometries=10, seed=123)
            self.assertEqual(rows0, rows_empty)
            self.assertFalse(any(k.endswith("_mode") and k.startswith("mesh_") for k in rows0[0]))
            fixture.profile_document["settings"]["mesh_refinement_modes"] = {"band": "length_only"}
            fixture.profile_path.write_text(json.dumps(fixture.profile_document))
            profile = doe.load_selected_profile(fixture.profile_path)
            rows, manifest = doe.generate_training_plan(fixture.spec, fixture.calibration, profile, geometries=10, seed=123)
            self.assertEqual(manifest["profile_settings"]["mesh_refinement_modes"], {"band": "length_only"})
            for row in rows:
                self.assertEqual(row["mesh_band_mode"], "length_only")
                spec = runner.build_spec(row)
                self.assertEqual(spec.mesh_refinement_modes, {"band": "length_only"})
                self.assertIsNone(assigned(spec)[1][-1]["maximum_elements"])
            self.assertNotEqual(runner.setup_fingerprint(runner.build_spec(rows0[0]), "same", "sin_current", False), runner.setup_fingerprint(runner.build_spec(rows[0]), "same", "sin_current", False))
        finally: fixture.tearDown()

    def test_optimizer_approved_profile_case_and_spec_keep_length_only(self):
        from ipmsm_optimization import fixed_case_context_fields
        from pyaedt_motor import optimize_physical as optimizer
        from tests import test_ipmsm_physical_optimizer as fixture
        with tempfile.TemporaryDirectory() as name:
            bound0, path, document = fixture.validation_profile(Path(name))
            document["settings"].update(mesh_refinement_modes={"band": "length_only"},
                                         mesh_maximum_lengths_mm={"band": .2})
            path.write_text(json.dumps(document), encoding="utf-8")
            fields = fixed_case_context_fields(bound0.spec.material_context, bound0.spec.winding)
            settings = doe.profile_settings(document["settings"])
            flat = {key: value for key, value in settings.items() if key not in
                    {"mesh_elements", "mesh_maximum_lengths_mm", "mesh_refinement_modes"}}
            flat.update({"mesh_" + group + "_elements": value for group, value in settings["mesh_elements"].items()})
            expected_case = {**fixture.baseline(), **fields, **flat, "mesh_band_mode": "length_only",
                             "mesh_band_max_length_mm": .2, "model_extent": "full_360", "symmetry_factor": 1,
                             "beta_convention": "dq_current_advance_v2", "electrical_zero_deg": fixture.zero_manifest()["electrical_zero_deg"]}
            expected = dict(bound0.fingerprints)
            expected["input_setup_fingerprint"] = runner.setup_fingerprint(runner.build_spec(expected_case), "analytic", "sin_current", False)
            bundle = fixture.AnalyticBundle()
            bundle.metadata["fingerprints"] = expected
            bound = optimizer.PhysicalBundleAdapter(bundle, fixture.source_spec(), expected, fixture.zero_manifest())
            proposal = optimizer.evaluate_design(bound, fixture.baseline())
            rows, plan = optimizer.build_fea_validation_rows(bound, [proposal], path)
            self.assertEqual(len(rows), 2)
            self.assertFalse(plan["submitted"])
            for row in rows:
                self.assertEqual(row["mesh_band_mode"], "length_only")
                self.assertNotIn("mesh_refinement_modes", row)
                applied = runner.build_spec(row)
                self.assertEqual(applied.mesh_refinement_modes, {"band": "length_only"})
                self.assertEqual(runner.setup_fingerprint(applied, "analytic", "sin_current", False), expected["input_setup_fingerprint"])
                fallback = runner.build_spec({key: value for key, value in row.items() if key != "mesh_band_mode"})
                self.assertNotEqual(runner.setup_fingerprint(fallback, "analytic", "sin_current", False), expected["input_setup_fingerprint"])
            original = runner.build_spec
            with mock.patch.object(runner, "build_spec", side_effect=lambda case: original({k: v for k, v in case.items() if k != "mesh_band_mode"})):
                with self.assertRaisesRegex(ValueError, "does not replay selected profile setting"):
                    optimizer.build_fea_validation_rows(bound, [proposal], path)

    def test_profile_and_quality_export_keep_mode(self):
        fixture = doe_fixture.V3DoeTests(); fixture.setUp()
        try:
            for modes in [{"wrong": "length_only"}, {"band": "unknown"}, {"rotor": "length_only"}]:
                with self.assertRaises(ValueError): doe.profile_settings(dict(fixture.profile_document["settings"], mesh_refinement_modes=modes))
        finally: fixture.tearDown()
        plan = cases(refined=True)
        for row in plan: row.update(mesh_band_mode="length_only", mesh_band_max_length_mm=.2)
        report = quality.compare_profiles([result(row) for row in plan], plan, reference_profile="reference", refined_reference_profile="refined", mesh_counts={"g1_reference": 20000, "g1_refined": 40000})
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "comparison.json"; path.write_text(json.dumps(report))
            selected = quality.selected_profile_manifest(report, path, "fast")
            self.assertEqual(selected["settings"]["mesh_refinement_modes"], {"band": "length_only"})
            selected_path = Path(t) / "selected.json"; selected_path.write_text(json.dumps(selected))
            self.assertEqual(doe.load_selected_profile(selected_path).settings["mesh_refinement_modes"], {"band": "length_only"})

if __name__ == "__main__": unittest.main()
