"""Synthetic complete-raw records and unapproved test bounds, never FEA approval."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from pyaedt_motor import native_state_semantics as s

PRODUCER = "producer_v3:sha256:" + "a" * 64


def fixture():
    def parts(count, empty):
        face = {"ID": 7, "FaceGeomTopol": {"FaceGeometry": {"Area": 10., "FcUVMid": [1., 2., 0.],
                                                          "FcTolVts": {"TolVt": [1., 2., 0., 5e-7]}}}}
        identity = {"ID": 99, "GeomTopolBasedOperationIdentityHelper": {"NewFaces": {"Face": face}}}
        return [{"ID": i + 1, "Attributes": {"SurfaceMaterialValue": s.SURFACE if i < count else '""'},
                 "Operations": {"Operation": {"PrimitiveOrigin": [1., 2., 0.], "OperationIdentity": copy.deepcopy(identity)}}}
                for i in range(count + empty)]
    geometry = {"AssignedEntities": {"AssignedEdge": [{"ID": 3, "EdgeData": {"EdgeGeomTopol": {
        "EdTolVts": {"TolVt": [1., 2., 0., 5e-7]}, "EdgeMidPoint": [1., 2., 0.]}}}]},
        "GeometryCore": {"Units": "mm", "NativeKernel": "PARASOLID", "NativeKernelVersionID": 25,
            "ValidationOptions": {"EntityCheckLevel": "Strict", "IgnoreUnclassifiedObjects": False, "SkipIntersectionChecks": False},
            "GeometryOperations": {"ToplevelParts": {"GeometryPart": parts(33, 20)},
                                   "OperandParts": {"GeometryPart": parts(28, 21)}}}}
    reports = {}
    for i in range(9):
        reports["report" + str(i)] = {"Report2D": {"Migration": {"MigVersion": [0, 0, "mig(0.0)"]},
             "Query": {"Expression": "torque", "Unit": "Nm"}, "PlotDisplayDataManager": {
             "PerViewPlotAttributeStoreMap": {"MapItem": {"ItemID": 0, "PlotAttributeStoreMap": {
             "MainMapItem": [{"SubMapItem": {"DockableOverlayLayoutAttribute": {"Dock_0": {"untouched": True}}}}, {}]}}}}}}
    full_reports = {"ReportManager": {"Reports": {name: {"TraceDef": "torque"} for name in reports}}, "Reports": reports}
    model = {key: {} for key in s.SCIENCE}
    model.update(Name="IPMSM", GeometryMode="XY", SolutionType="Transient", Multiplier="SymmetryFactor", ModelDepth="StackLength",
                 AnalysisSetup={"PPT_Transient": {"SetupType": "Transient", "TimeStep": "TimeStep"}}, ModelSetup=geometry,
                 TemperatureSettings={"IncludeTemperatureDependence": False, "EnableFeedback": False, "Temperatures": [1, "22cel"]})
    for key in s.MODEL_KEYS - set(model):
        model[key] = {}
    for key in ("ObjsEnabledForDeformation()", "ObjsEnabledForStressName()", "Enabled2DXYStressXXObjects()",
                "Enabled2DXYStressYYObjects()", "Enabled2DXYStressXYObjects()"):
        model[key] = None
    model.update(UseSkewModel=False, EnableTranTranLinkWithSimplorer=False, CacheHeaderFile="HDRFixture.tmp",
                 PerfectConductorThreshold=1e30, InsulatorThreshold=1, BackgroundMaterialName="vacuum")
    model["SolutionManager"] = {"SimSetup": {"TypeName": "BaseSetup", "ID": 141, "Name": "PPT_Transient",
        "Solution": {"ID": 142, "Name": "Transient", "SimDataExtractor": {
            "QuantityIDs": {"NextUniqueID": 0, "MoveBackwards": False, "IDMap()": None},
            "SimValue": ["FluxLinkage(PhaseC)", 1, 152, False, "SimValueID=163", 3, 0, 2, 0, False, False, -1, 1, 0, 1, 1, "", 0, 0],
            "Sweeps": {"PostprocessSweep": [], "Sweep": [
                {"Variable": "BaseRPM", "Column": "1200rpm", "Units": "rpm"},
                {"Variable": "SymmetryFactor", "Column": "1", "Units": ""},
                {"Variable": "Time", "Column": "", "Units": "s"}]}}}},
        "Version ID Map": {}, "ID Map": {}, "ValidationCacheHeader": "fixture"}
    definition = {"BulkOrSurfaceType": 2, "CoordinateSystemType": "Cartesian", "LibLocation": "SysLibrary",
                  "Library": "SurfaceMaterials", "ModSinceLib": False, "ModTime": 1461288057,
                  "PhysicsTypes": {"set": ["Thermal"]}, "surface_emissivity": "0.8"}
    project = {"Desktop": {"Version": [2025, 2]}, "Maxwell2DModel": model,
        "Definitions": {"Materials": {"M19": {"BH": [0, 1], "Conductivity": 1}},
                        "SurfaceMaterials": {"Steel-oxidised-surface": definition},
                        "Scripts": {}, "Symbols": {}, "DefInfo": {}, "Compdefs": {}},
        "DataInstances": {"DesignEditor": "", "Refdes": "", "CompInstances": {}, "SODInfo": {}, "Instance": {
            "DesignEditor": "", "ID": "1", "Maxwell2DDesignInstance": {"ReportSetup": full_reports,
            "DesignInstanceID": 1, "Properties": {}, "UserDefinedDocument": {}}}}}
    for key in s.PROJECT_KEYS - set(project):
        project[key] = {}
    before = {"kind": "raw_aedt", "record": {"bytes": 1, "sha256": "0" * 64}, "project": project,
              "raw_variable_rows": ["VariableProp('BaseRPM', 'D', '', '1200rpm')", "VariableProp('SymmetryFactor', 'UD', '', '1')"]}
    after = copy.deepcopy(before)
    after["record"] = {"bytes": 2, "sha256": "1" * 64}
    for group in ("ToplevelParts", "OperandParts"):
        for part in after["project"]["Maxwell2DModel"]["ModelSetup"]["GeometryCore"]["GeometryOperations"][group]["GeometryPart"]:
            if part["Attributes"]["SurfaceMaterialValue"] == s.SURFACE:
                part["Attributes"]["SurfaceMaterialValue"] = '""'
    report_setup = after["project"]["DataInstances"]["Instance"]["Maxwell2DDesignInstance"]["ReportSetup"]
    for report in report_setup["Reports"].values():
        body = report["Report2D"]
        body["Migration"]["MigVersion"] = [1, 0, "2021R1 mig(1.0)"]
        view = body["PlotDisplayDataManager"]["PerViewPlotAttributeStoreMap"]["MapItem"]
        view["ItemID"] = 1
        view["PlotAttributeStoreMap"]["MainMapItem"].pop()
        view["PlotAttributeStoreMap"]["MainMapItem"][0]["SubMapItem"]["DockableOverlayLayoutAttribute"]["Dock_1"] = {
            "OverlayLayoutAttribute": {"BoundingRect": {"Left": 0, "Right": 10000, "Top": 0, "Bottom": 5000},
                                       "ModifyPosition": False, "ModifySize": False}}
    policy = {"schema": s.SCHEMA, "renderer_producer": PRODUCER,
        "implementation_sha256": hashlib.sha256(Path(s.__file__).read_bytes()).hexdigest(),
        "precision_basis": {"kind": "native_geometry_tolerance", "evidence": ["INERT TEST ONLY; no actual policy approved"]},
        "field_units": copy.deepcopy(s.FIELD_UNITS),
        "native_tolerance_mm": 5e-7, "limits": {"length_mm": 1e-10, "area_mm2": 1e-8},
        "surface_attribute_records": 102, "surface_transition_count": 61, "report_names": sorted(reports)}
    return before, after, policy


def compare(before, after, policy):
    raw = json.dumps(policy, sort_keys=True).encode()
    return s.compare_states(before, after, prepared_record=before["record"], solved_record=after["record"],
                            renderer_producer=PRODUCER, policy_bytes=raw, policy_sha256=s.digest(raw))


class SemanticTests(unittest.TestCase):
    def test_complete_policy_can_pass_without_forcing_global_false(self):
        a, b, policy = fixture()
        b["project"]["Maxwell2DModel"]["ModelSetup"]["AssignedEntities"]["AssignedEdge"][0]["EdgeData"]["EdgeGeomTopol"]["EdgeMidPoint"][0] += 1e-12
        face=b["project"]["Maxwell2DModel"]["ModelSetup"]["GeometryCore"]["GeometryOperations"]["ToplevelParts"]["GeometryPart"][0]["Operations"]["Operation"]["OperationIdentity"]["GeomTopolBasedOperationIdentityHelper"]["NewFaces"]["Face"]["FaceGeomTopol"]["FaceGeometry"]
        face["Area"] += 1e-10
        result = compare(a, b, policy)
        self.assertTrue(result["semantic_equivalence_proven"], result["errors"])
        self.assertEqual(result["change_categories"]["thermal_only_surface_assignment"], 61)
        self.assertEqual(result["change_categories"]["bounded_numeric_cache"], 2)

    def test_missing_raw_external_reference_and_policy_binding_fail(self):
        a, b, policy = fixture()
        a["kind"] = "selected_snapshot"
        self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])
        a, b, policy = fixture()
        result = s.compare_states(a, b, prepared_record={"bytes": 9, "sha256": "2" * 64}, solved_record=b["record"],
                  renderer_producer=PRODUCER, policy_bytes=b"{}", policy_sha256=s.digest(b"{}"))
        self.assertFalse(result["semantic_equivalence_proven"])
        raw = json.dumps(policy).encode()
        self.assertFalse(s.compare_states(a, b, prepared_record=a["record"], solved_record=b["record"], renderer_producer=PRODUCER,
                         policy_bytes=raw, policy_sha256="0"*64)["semantic_equivalence_proven"])
        self.assertFalse(s.compare_states(a,b,prepared_record=a["record"],solved_record=b["record"],renderer_producer=PRODUCER,
                         policy_bytes=None,policy_sha256=None)["semantic_equivalence_proven"])
        with self.assertRaises(ValueError):s._policy(b'{"same":1,"same":2}')

    def test_thermal_material_coupling_and_definition_changes_fail(self):
        changes = [lambda p: p["Maxwell2DModel"]["TemperatureSettings"].update(EnableFeedback=True),
            lambda p: p["Maxwell2DModel"]["TemperatureSettings"].update(IncludeTemperatureDependence=True),
            lambda p: p["Definitions"]["SurfaceMaterials"]["Steel-oxidised-surface"]["PhysicsTypes"].update(set=["Thermal", "Electromagnetic"]),
            lambda p: p["Definitions"]["SurfaceMaterials"]["Steel-oxidised-surface"].update(surface_emissivity="0.9"),
            lambda p: p["Definitions"]["Materials"]["M19"].update(Conductivity=2),
            lambda p: p["DataInstances"]["Instance"].update(IcepakDesignInstance={})]
        for change in changes:
            with self.subTest(change=change):
                a, b, policy = fixture(); change(b["project"])
                self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])
        # Consistently wrong before/after definitions cannot evade the explicit
        # Thermal-only rule just by satisfying the equality check.
        a, b, policy = fixture()
        for value in (a, b):
            value["project"]["Definitions"]["SurfaceMaterials"]["Steel-oxidised-surface"]["PhysicsTypes"]["set"].append("Electromagnetic")
        self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])

    def test_construction_identity_current_frame_and_query_changes_fail(self):
        for key in ("BoundarySetup", "AnalysisSetup", "ComputeTransientInductance", "ModelDepth", "Multiplier"):
            a, b, policy = fixture(); b["project"]["Maxwell2DModel"][key] = "changed"
            self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"], key)
        a, b, policy = fixture()
        b["project"]["Maxwell2DModel"]["ModelSetup"]["AssignedEntities"]["AssignedEdge"][0]["ID"] = 4
        self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])
        a,b,policy=fixture()
        b["project"]["Maxwell2DModel"]["ModelSetup"]["GeometryCore"]["GeometryOperations"]["ToplevelParts"]["GeometryPart"][0]["Operations"]["Operation"]["PrimitiveOrigin"][0] += 1e-12
        self.assertFalse(compare(a,b,policy)["semantic_equivalence_proven"])
        a, b, policy = fixture();reports=b["project"]["DataInstances"]["Instance"]["Maxwell2DDesignInstance"]["ReportSetup"]
        reports["Reports"]["report0"]["Report2D"]["Query"]["Expression"] = "different_torque"
        self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])

    def test_unknown_nonfinite_large_and_tolerance_cache_changes_fail(self):
        for value in (1000., float("nan")):
            a, b, policy = fixture()
            b["project"]["Maxwell2DModel"]["ModelSetup"]["AssignedEntities"]["AssignedEdge"][0]["EdgeData"]["EdgeGeomTopol"]["EdgeMidPoint"][0] = value
            self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])
        a, b, policy = fixture()
        b["project"]["Maxwell2DModel"]["ModelSetup"]["AssignedEntities"]["AssignedEdge"][0]["EdgeData"]["EdgeGeomTopol"]["EdTolVts"]["TolVt"][3] *= 2
        self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])
        a, b, policy = fixture();b["project"]["Maxwell2DModel"]["ModelSetup"]["unknown_numeric_cache"] = 1e-15
        self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])

    def test_other_surface_transition_structural_change_or_missing_one_of61_fails(self):
        for value in ('"different_surface"', s.SURFACE):
            a, b, policy = fixture()
            b["project"]["Maxwell2DModel"]["ModelSetup"]["GeometryCore"]["GeometryOperations"]["ToplevelParts"]["GeometryPart"][0]["Attributes"]["SurfaceMaterialValue"] = value
            self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])
        a, b, policy = fixture()
        b["project"]["Maxwell2DModel"]["ModelSetup"]["GeometryCore"]["GeometryOperations"]["ToplevelParts"]["GeometryPart"].pop()
        self.assertFalse(compare(a, b, policy)["semantic_equivalence_proven"])

    def test_gui_only_scope_rejects_new_dock_physics_query_and_layout_payload(self):
        a, b, policy = fixture()
        body=b["project"]["DataInstances"]["Instance"]["Maxwell2DDesignInstance"]["ReportSetup"]["Reports"]["report0"]["Report2D"]
        dock=body["PlotDisplayDataManager"]["PerViewPlotAttributeStoreMap"]["MapItem"]["PlotAttributeStoreMap"]["MainMapItem"][0]["SubMapItem"]["DockableOverlayLayoutAttribute"]
        dock["Dock_1"]["OverlayLayoutAttribute"]["unexpected_physics"] = "current"
        self.assertFalse(compare(a,b,policy)["semantic_equivalence_proven"])
        a,b,policy=fixture();policy["limits"]["length_mm"]=1
        self.assertFalse(compare(a,b,policy)["semantic_equivalence_proven"])
        a,b,policy=fixture();policy["field_units"]["FcUVMid"]="unknown"
        self.assertFalse(compare(a,b,policy)["semantic_equivalence_proven"])


if __name__ == "__main__": unittest.main()
