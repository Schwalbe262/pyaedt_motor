"""Pure, policy-pinned input semantics for v252 full XY Maxwell2D saved states.

No global rounding, cache removal, surface removal or training approval. The
caller must bind the raw preimage/solved records to the actual intent/collection
and separately verify source/frame, ownership, numeric quality and convergence.
"""
from __future__ import annotations
from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re

SCHEMA = "maxwell2d_input_semantics_policy_v1"
SURFACE = '"Steel-oxidised-surface"'
SCIENCE = ("AnalysisSetup", "BoundarySetup", "ComputeIncrementalMatrix", "ComputeTransientInductance",
           "GeometryMode", "MaxwellParameterSetup", "MeshSetup", "ModelDepth", "ModelSetup",
           "Multiplier", "SolutionType", "SolveFraction")
# Complete observed v252 model/definition categories, not a substring exclusion.
# Every additional or removed category fails even when present in both files.
MODEL_KEYS = frozenset(SCIENCE) | {
    "RepRewriteV2", "Name", "DesignID", "Perform Minimal validation", "TemperatureSettings",
    "ObjsEnabledForDeformation()", "PreserveTranSolnAfterDatasetEdit", "PerfectConductorThreshold",
    "InsulatorThreshold", "UseSkewModel", "EnableTranTranLinkWithSimplorer", "BackgroundMaterialName",
    "ObjsEnabledForStressName()", "Enabled2DXYStressXXObjects()", "Enabled2DXYStressYYObjects()",
    "Enabled2DXYStressXYObjects()", "OutputVariable", "3DComponent", "Optimetrics", "Solutions",
    "FieldsReporter", "SolutionManager", "UserDefinedSolutionMgr", "DatasetSolutionMgr",
    "Notes=$begin_cdata$ $end_cdata$", "AnimationSetups", "CacheHeaderFile"}
DEFINITION_KEYS = {"Materials", "SurfaceMaterials", "Scripts", "Symbols", "DefInfo", "Compdefs"}
RESULT_KEYS = {"SolutionManager", "CacheHeaderFile"}
PROJECT_KEYS = {"Created", "Product", "FileOwnedByWorkbench", "Desktop", "UsesAdvancedFeatures", "NextUniqueID",
    "MoveBackwards", "HFSSEnvironment", "PlanarEMEnvironment", "Q3DEnvironment", "2DExtractorEnvironment",
    "NexximEnvironment", "NexximNetlistEnvironment", "Maxwell3DEnvironment", "Maxwell2DEnvironment",
    "RMxprtEnvironment", "MaxCirEnvironment", "IcepakEnvironment", "MechanicalEnvironment", "SchematicEnvironment",
    "geometry3deditor", "ReadVersion", "DesignMgrEnvironment", "ProjectDatasets", "VariableOrders[0:]", "Definitions",
    "DesignIDServer", "Maxwell2DModel", "DataInstances", "WBSystemIDToDesignInstanceIDMap", "WBSysIDSysDetails",
    "WBConnIDConnDetails", "WBMaterialGuidDetails", "MinervaProjectSettingsBlk"}
ASSIGNED = (("AssignedEntities", "AssignedEdge", "EdgeData", "EdgeGeomTopol", "EdTolVts", "TolVt"),
            ("AssignedEntities", "AssignedEdge", "EdgeData", "EdgeGeomTopol", "EdgeMidPoint"))
TAILS = (("GeomTopolBasedOperationIdentityHelper", "NewFaces", "Face", "FaceGeomTopol", "FaceGeometry", "Area"),
         ("GeomTopolBasedOperationIdentityHelper", "NewFaces", "Face", "FaceGeomTopol", "FaceGeometry", "FcTolVts", "TolVt"),
         ("GeomTopolBasedOperationIdentityHelper", "NewFaces", "Face", "FaceGeomTopol", "FaceGeometry", "FcUVMid"),
         ("GeomTopolBasedOperationIdentityHelper", "NewEdges", "Edge", "EdTolVts", "TolVt"),
         ("GeomTopolBasedOperationIdentityHelper", "NewEdges", "Edge", "EdgeMidPoint"),
         ("GeomTopolBasedOperationIdentityHelper", "NewVertices", "Vertex", "VtPos"),
         ("BodyGeomTopoForSeparate", "BoundingBoxHigh"), ("BodyGeomTopoForSeparate", "BoundingBoxLow"))
VIEW = ("Report2D", "PlotDisplayDataManager", "PerViewPlotAttributeStoreMap", "MapItem")
MAIN = VIEW + ("PlotAttributeStoreMap", "MainMapItem")
DOCK = MAIN + ("SubMapItem", "DockableOverlayLayoutAttribute")
FIELD_UNITS = {"TolVt_xyz": "mm", "TolVt_tolerance": "mm", "EdgeMidPoint": "mm", "VtPos": "mm",
               "BoundingBoxHigh": "mm", "BoundingBoxLow": "mm", "FcUVMid": "mm", "Area": "mm2"}


def _need(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def record(raw):
    return {"bytes": len(raw), "sha256": digest(raw)}


def _valid_record(value):
    return (type(value) is dict and set(value) == {"bytes", "sha256"} and type(value["bytes"]) is int and
            value["bytes"] >= 0 and isinstance(value["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]))


def _policy(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, "duplicate policy key: " + key)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite policy: " + value)))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _equal(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _at(value, path):
    for item in path:
        value = value[item]
    return value


def _pattern(path):
    return tuple(item for item in path if type(item) is not int)


def _leaves(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, path + (key,))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _leaves(item, path + (i,))
    else:
        yield path, value


def _walk(a, b, callback, path=()):
    if _equal(a, b):
        return
    if callback(a, b, path):
        return
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        for key in a:
            _walk(a[key], b[key], callback, path + (key,))
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            _walk(x, y, callback, path + (i,))
    else:
        raise ValueError("unknown/structural/strict input change: " + repr(path))


def read_state(path):
    """Generic saved-file parser only; no Desktop/session/API is constructed."""
    path = Path(path)
    _need(path.is_file() and not path.is_symlink(), "regular raw AEDT required")
    raw = path.read_bytes()
    from ansys.aedt.core.internal.load_aedt_file import load_entire_aedt_file
    project = load_entire_aedt_file(str(path))["AnsoftProject"]
    rows = re.findall(r"(?m)^\s*VariableProp\([^\r\n]+", raw.decode("utf-8-sig"))
    names = re.findall(r"VariableProp\('([^']+)'", "\n".join(rows))
    _need(len(names) == len(rows) and len(names) == len(set(names)), "duplicate/unparsed raw variable rows")
    _need(path.read_bytes() == raw, "raw AEDT changed during parse")
    return {"kind": "raw_aedt", "record": record(raw), "project": project, "raw_variable_rows": rows}


def _extract(state):
    _need(state.get("kind") == "raw_aedt", "same-instance full raw AEDT is required, not a selected snapshot/reference")
    project = state["project"]
    _need(isinstance(project, dict) and set(project) == PROJECT_KEYS, "unknown/missing project input category")
    _need(_equal(project["Desktop"]["Version"], [2025, 2]), "native v252 header required")
    model = project["Maxwell2DModel"]
    _need(isinstance(model, dict) and set(model) == MODEL_KEYS, "unknown/missing model input category")
    _need(isinstance(project["Definitions"], dict) and set(project["Definitions"]) == DEFINITION_KEYS,
          "unknown/missing project definition category")
    _need(model["UseSkewModel"] is False and model["EnableTranTranLinkWithSimplorer"] is False and
          model["3DComponent"] == {} and all(model[k] is None for k in
          ("ObjsEnabledForDeformation()", "ObjsEnabledForStressName()", "Enabled2DXYStressXXObjects()",
           "Enabled2DXYStressYYObjects()", "Enabled2DXYStressXYObjects()")),
          "skew/external-circuit/component/deformation/stress scope unsupported")
    symmetry = [re.match(r"\s*VariableProp\('SymmetryFactor',\s*'[^']*',\s*'[^']*',\s*'([^']*)'", row)
                for row in state["raw_variable_rows"]]
    symmetry = [match.group(1) for match in symmetry if match]
    full = (type(model["Multiplier"]) is int and model["Multiplier"] == 1 or
            model["Multiplier"] == "SymmetryFactor" and symmetry == ["1"])
    _need(isinstance(model, dict) and model.get("Name") == "IPMSM" and model["GeometryMode"] == "XY" and
          model["SolutionType"] == "Transient" and full,
          "one full XY Maxwell2D Transient/Sym1 model required")
    data = project["DataInstances"]
    _need(set(data) == {"DesignEditor", "Refdes", "CompInstances", "Instance", "SODInfo"},
          "unknown/missing data-instance category")
    instance = data["Instance"]
    _need(set(instance) == {"DesignEditor", "ID", "Maxwell2DDesignInstance"}, "unknown/missing instance input category")
    design_instance = instance["Maxwell2DDesignInstance"]
    _need(set(design_instance) == {"DesignInstanceID", "ReportSetup", "Properties", "UserDefinedDocument"},
          "unknown/missing design-instance input category")
    kinds = [key for key in instance if key.endswith("DesignInstance")]
    _need(kinds == ["Maxwell2DDesignInstance"], "thermal/other design coupling scope is unsupported")
    _need(not any(("coupl" in key.lower() or "thermal" in key.lower()) and bool(value)
                  for key, value in model.items()), "unreviewed coupling/thermal model field")
    thermal = model["TemperatureSettings"]
    _need(thermal.get("EnableFeedback") is False and thermal.get("IncludeTemperatureDependence") is False,
          "thermal feedback/dependence must explicitly be disabled")
    setup_types = [v for p, v in _leaves(model["AnalysisSetup"]) if p and p[-1] == "SetupType"]
    _need(setup_types and all(value == "Transient" for value in setup_types), "non-transient/thermal setup is unsupported")
    geometry = model["ModelSetup"]
    core = geometry["GeometryCore"]
    _need(core["Units"] == "mm" and core["NativeKernel"] == "PARASOLID" and
          type(core["NativeKernelVersionID"]) is int and core["NativeKernelVersionID"] == 25,
          "reviewed v252 Parasolid25/mm geometry scope required")
    _need(_equal(core["ValidationOptions"], {"EntityCheckLevel": "Strict", "IgnoreUnclassifiedObjects": False,
                                           "SkipIntersectionChecks": False}), "strict native geometry validation required")
    return {"science": {key: model[key] for key in SCIENCE}, "thermal": thermal,
            "project_inputs": {k: v for k, v in project.items() if k not in {"Maxwell2DModel", "Definitions", "DataInstances"}},
            "instance_inputs": [{k: v for k, v in data.items() if k != "Instance"},
                                {k: v for k, v in instance.items() if k != "Maxwell2DDesignInstance"},
                                {k: v for k, v in design_instance.items() if k != "ReportSetup"}],
            "model_inputs": {k: v for k, v in model.items() if k not in set(SCIENCE) | RESULT_KEYS | {"TemperatureSettings"}},
            "result_state": {k: model[k] for k in RESULT_KEYS}, "definitions": project["Definitions"],
            "bulk": project["Definitions"]["Materials"], "surface_defs": project["Definitions"]["SurfaceMaterials"],
            "variables": state["raw_variable_rows"], "reports": instance["Maxwell2DDesignInstance"]["ReportSetup"]}


def _result_channels(a, b, raw_rows, limit, result):
    """Allow only observed extractor ordering/display counters and header basename.

    Physical variable expressions and full query definitions remain strict elsewhere.
    Other SolutionManager fields are compared exactly; no generic result-tree drop.
    """
    for state in (a, b):
        _need(isinstance(state["CacheHeaderFile"], str) and
              re.fullmatch(r"HDR[A-Za-z0-9]+\.tmp", state["CacheHeaderFile"]), "unknown cache-header format")
    if a["CacheHeaderFile"] != b["CacheHeaderFile"]:
        result["allowed_changes"].append({"path": ["CacheHeaderFile"], "category": "native_cache_header_basename"})
    managers = [a["SolutionManager"], b["SolutionManager"]]
    variable_names = {re.match(r"\s*VariableProp\('([^']+)'", row).group(1) for row in raw_rows} | {"Time"}
    sweep_maps = []
    for manager in managers:
        _need(set(manager) == {"SimSetup", "Version ID Map", "ID Map", "ValidationCacheHeader"},
              "unknown SolutionManager category")
        setup = manager["SimSetup"]
        _need(set(setup) == {"TypeName", "ID", "Name", "Solution"}, "unknown result setup category")
        solution = setup["Solution"]
        _need(set(solution) == {"ID", "Name", "SimDataExtractor"}, "unknown result solution category")
        extractor = solution["SimDataExtractor"]
        _need(set(extractor) == {"QuantityIDs", "SimValue", "Sweeps"} and
              set(extractor["QuantityIDs"]) == {"NextUniqueID", "MoveBackwards", "IDMap()"},
              "unknown extractor/quantity category")
        _need(isinstance(extractor["SimValue"], list) and len(extractor["SimValue"]) == 19 and
              isinstance(extractor["SimValue"][4], str) and
              re.fullmatch(r"SimValueID=[0-9]+", extractor["SimValue"][4]), "unknown SimValue format")
        sweeps = extractor["Sweeps"]
        _need(set(sweeps) == {"PostprocessSweep", "Sweep"} and isinstance(sweeps["Sweep"], list),
              "unknown sweep-result category")
        rows = {}
        for row in sweeps["Sweep"]:
            _need(isinstance(row, dict) and set(row) == {"Variable", "Column", "Units"} and
                  all(isinstance(v, str) for v in row.values()) and row["Variable"] not in rows,
                  "unknown/duplicate evaluated-variable display row")
            rows[row["Variable"]] = row
        _need(set(rows) == variable_names, "evaluated-variable names differ from raw inputs plus Time")
        sweep_maps.append(rows)
    # Walk the complete result tree: only the two exact paths below may differ.
    def output_change(x, y, path):
        if path == ("SimSetup", "Solution", "SimDataExtractor", "SimValue", 4):
            result["allowed_changes"].append({"path": ["SolutionManager", *path], "category": "native_result_value_ID"})
            return True
        if path != ("SimSetup", "Solution", "SimDataExtractor", "Sweeps", "Sweep"):
            return False
        for name, row in sweep_maps[0].items():
            other = sweep_maps[1][name]
            if _equal(row, other):
                continue
            _need(row["Units"] == other["Units"] == "mm" and name != "Time",
                  "non-length evaluated-variable display changed")
            values = []
            for column in (row["Column"], other["Column"]):
                _need(re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?mm", column),
                      "unknown evaluated-length display format")
                values.append(Decimal(column[:-2]))
                _need(_finite(float(values[-1])), "nonfinite evaluated-length display")
            delta = abs(values[1] - values[0])
            _need(delta <= Decimal(str(limit)), "evaluated length display exceeds dimensional policy")
            result["allowed_changes"].append({"path": ["SolutionManager", *path, name],
                "category": "bounded_evaluated_length_display", "delta_mm": float(delta), "limit_mm": limit})
        result["allowed_changes"].append({"path": ["SolutionManager", *path], "category": "native_variable_display_order"})
        return True
    _walk(managers[0], managers[1], output_change)


def _surface_definition(definitions):
    definition = definitions["Steel-oxidised-surface"]
    expected = {"BulkOrSurfaceType": 2, "CoordinateSystemType": "Cartesian", "LibLocation": "SysLibrary",
                "Library": "SurfaceMaterials", "ModSinceLib": False, "ModTime": 1461288057,
                "PhysicsTypes": {"set": ["Thermal"]}, "surface_emissivity": "0.8"}
    _need(_equal(definition, expected), "exact installed Thermal-only Steel definition/emissivity .8 required")


def _geometry_role(path, value, tree):
    if not _finite(value):
        return None
    key = _pattern(path)
    allowed = key in ASSIGNED
    for group in ("ToplevelParts", "OperandParts"):
        base = ("GeometryCore", "GeometryOperations", group, "GeometryPart", "Operations", "Operation", "OperationIdentity")
        allowed |= any(key == base + tail for tail in TAILS)
    if not allowed:
        return None
    if key[-1] == "Area":
        return "area_mm2" if value > 0 else None
    if not path or type(path[-1]) is not int:
        return None
    parent = _at(tree, path[:-1])
    if key[-1] == "TolVt":
        # XYZ and the native tolerance occupy distinct positions. Tolerance is
        # never a numeric-cache exception and must remain bit/value exact.
        return "length_mm" if len(parent) == 4 and path[-1] in (0, 1, 2) and _finite(parent[3]) and parent[3] > 0 else None
    return "length_mm" if len(parent) == 3 and path[-1] in (0, 1, 2) else None


def _precision_witness(tree):
    tolerances, points = [], []
    for path, value in _leaves(tree):
        key = _pattern(path)
        if key and key[-1] == "TolVt" and path[-1] == 3:
            parent = _at(tree, path[:-1])
            if isinstance(parent, list) and len(parent) == 4 and all(_finite(v) for v in parent):
                _need(value > 0, "native point tolerance must be positive")
                tolerances.append(value)
        if _geometry_role(path, value, tree) == "length_mm":
            points.append(abs(value))
    _need(tolerances and points, "native point-tolerance/coordinate witness missing")
    return min(tolerances), max(points)


def compare_states(before, after, *, prepared_record, solved_record, renderer_producer,
                   policy_bytes, policy_sha256):
    """A valid explicit dimensional policy can pass; no policy means no waiver.

    Root selects/pins policy bounds and their precision basis outside this pure
    function. It never derives acceptance bounds from observed output deltas.
    """
    result = {"semantic_equivalence_proven": False, "errors": [], "allowed_changes": [],
              "before_record": before.get("record"), "after_record": after.get("record"),
              "policy_sha256": policy_sha256, "renderer_producer": renderer_producer}
    try:
        _need(all(_valid_record(v) for v in (before.get("record"), after.get("record"), prepared_record, solved_record)) and
              before.get("record") == prepared_record and after.get("record") == solved_record,
              "raw files do not match the actual intent/solved collection records")
        _need(isinstance(policy_bytes, bytes) and digest(policy_bytes) == policy_sha256, "explicit policy SHA differs/missing")
        policy = _policy(policy_bytes)
        _need(policy["schema"] == SCHEMA and policy["renderer_producer"] == renderer_producer and
              policy["implementation_sha256"] == digest(Path(__file__).read_bytes()), "policy source/schema/renderer binding differs")
        _need(policy["precision_basis"]["kind"] in {"native_geometry_tolerance", "declared_numeric_expression_precision"}
              and policy["precision_basis"]["evidence"], "reviewed dimensional precision basis is required")
        _need(_equal(policy["field_units"], FIELD_UNITS), "explicit reviewed per-field dimensional scope is required")
        _need(type(policy["surface_attribute_records"]) is int and policy["surface_attribute_records"] > 0 and
              type(policy["surface_transition_count"]) is int and 0 < policy["surface_transition_count"] <= policy["surface_attribute_records"] and
              isinstance(policy["report_names"], list) and policy["report_names"] and
              all(isinstance(n, str) and n for n in policy["report_names"]) and
              len(policy["report_names"]) == len(set(policy["report_names"])), "invalid policy coverage/identities")
        a, b = _extract(before), _extract(after)
        definitions_b, instance_b = b["definitions"], b["instance_inputs"]
        registration_policy = policy.get("output_registration_rule")
        if registration_policy is not None:
            from . import compdefs_transition as registration
            _need(isinstance(registration_policy, dict) and set(registration_policy) ==
                  {"rule_id", "implementation_sha256", "approved"} and registration_policy["approved"] is True and
                  registration_policy["rule_id"] == registration.RULE_ID and
                  registration_policy["implementation_sha256"] == digest(Path(registration.__file__).read_bytes()),
                  "unapproved/unpinned output-registration rule")
            if not (_equal(a["definitions"], definitions_b) and _equal(a["instance_inputs"], instance_b)):
                checked = registration.inspect_transition(before, after, include_projection=True)
                _need(checked["candidate_rule_matched"], "typed output-registration rule rejected: " + repr(checked["errors"]))
                definitions_b, instance_b = checked["projected_definitions"], checked["projected_instance_inputs"]
                result["allowed_changes"].extend(dict(item, category="typed_empty_default_output_registration")
                                                 for item in checked["changed_paths"])
        _need(_equal(a["project_inputs"], b["project_inputs"]) and _equal(a["instance_inputs"], instance_b),
              "strict project/dataset/environment/design-instance input changed")
        _need(_equal(a["model_inputs"], b["model_inputs"]), "strict additional model input changed")
        _need(_equal(a["definitions"], definitions_b), "project definitions changed (including scripts/symbols/Compdefs)")
        _need(_equal(a["bulk"], b["bulk"]) and _equal(a["surface_defs"], b["surface_defs"]) and
              _equal(a["thermal"], b["thermal"]) and _equal(a["variables"], b["variables"]),
              "bulk/surface definitions, thermal settings or raw variable inputs changed")
        _surface_definition(a["surface_defs"])
        for key in SCIENCE:
            if key != "ModelSetup":
                _need(_equal(a["science"][key], b["science"][key]), "strict EM/setup/current/frame input changed: " + key)
        ga, gb = a["science"]["ModelSetup"], b["science"]["ModelSetup"]
        tol_a, extent = _precision_witness(ga)
        tol_b, _ = _precision_witness(gb)
        limits = policy["limits"]
        _need(all(_finite(limits[k]) and limits[k] > 0 for k in ("length_mm", "area_mm2")) and
              policy["native_tolerance_mm"] == tol_a == tol_b and limits["length_mm"] <= tol_a and
              limits["area_mm2"] <= tol_a * extent, "dimensional limits exceed the native precision witness")
        result["precision_witness"] = {"native_point_tolerance_mm": tol_a, "coordinate_scale_mm": extent,
                                       "limits": limits, "basis": policy["precision_basis"], "field_units": policy["field_units"]}
        _result_channels(a["result_state"], b["result_state"], a["variables"], limits["length_mm"], result)
        changed_surfaces = []
        def geometry(x, y, path):
            key = _pattern(path)
            if key in [("GeometryCore", "GeometryOperations", group, "GeometryPart", "Attributes", "SurfaceMaterialValue")
                       for group in ("ToplevelParts", "OperandParts")]:
                _need(x == SURFACE and y == '""', "unsupported surface assignment transition")
                changed_surfaces.append(path)
                result["allowed_changes"].append({"path": ["ModelSetup", *path], "category": "thermal_only_surface_assignment",
                                                   "before": x, "after": y})
                return True
            role = _geometry_role(path, x, ga)
            if role and role == _geometry_role(path, y, gb):
                delta = abs(y - x)
                _need(delta <= limits[role], "cache delta exceeds explicit dimensional policy: " + repr(path))
                result["allowed_changes"].append({"path": ["ModelSetup", *path], "category": "bounded_numeric_cache",
                                                   "unit": role, "delta": delta, "limit": limits[role], "before": x, "after": y})
                return True
            return False
        _walk(ga, gb, geometry)
        surface_paths = [p for p, v in _leaves(ga) if _pattern(p) in [
            ("GeometryCore", "GeometryOperations", group, "GeometryPart", "Attributes", "SurfaceMaterialValue")
            for group in ("ToplevelParts", "OperandParts")]]
        _need(len(surface_paths) == policy["surface_attribute_records"] and
              len(changed_surfaces) in (0, policy["surface_transition_count"]), "surface record/transition coverage differs")
        reports = set(policy["report_names"])
        _need(set(a["reports"]["Reports"]) == reports == set(b["reports"]["Reports"]) and
              _equal(a["reports"]["ReportManager"], b["reports"]["ReportManager"]), "report/query manager changed")
        def report_ui(x, y, path):
            key = _pattern(path)
            if len(key) < 3 or key[0] != "Reports" or key[1] not in reports:
                return False
            tail, category = key[2:], None
            if tail == ("Report2D", "Migration", "MigVersion") and isinstance(x, list) and isinstance(y, list):
                _need(len(x) == len(y) == 3 and type(x[0]) is int and type(y[0]) is int and
                      x[0] == 0 and y[0] == 1 and type(x[1]) is int and x[1] == y[1] and
                      type(y[1]) is int and x[2] == "mig(0.0)" and y[2] == "2021R1 mig(1.0)", "unknown report migration")
                category = "known_report_migration"
            elif tail == MAIN and isinstance(x, list) and isinstance(y, list) and len(x) == len(y) + 1 and x[-1] == {}:
                _walk(x[:-1], y, report_ui, path)
                category = "removed_trailing_empty_layout_slot"
            elif tail == VIEW + ("ItemID",):
                _need(type(x) is int and type(y) is int, "nonintegral GUI-only identifier/dock change")
                category = "known_report_view_layout"
            elif tail == DOCK and isinstance(x, dict) and isinstance(y, dict):
                added = set(y) - set(x)
                _need(added and added <= {"Dock_1", "Dock_2"} and set(x) <= set(y) and
                      all(_equal(x[k], y[k]) for k in x), "unknown dock layout structure/change")
                for key in added:
                    entry = y[key]
                    _need(isinstance(entry, dict) and set(entry) == {"OverlayLayoutAttribute"}, "unknown dock payload")
                    layout = entry["OverlayLayoutAttribute"]
                    _need(set(layout) == {"BoundingRect", "ModifyPosition", "ModifySize"} and
                          layout["ModifyPosition"] is False and layout["ModifySize"] is False and
                          set(layout["BoundingRect"]) == {"Left", "Right", "Top", "Bottom"} and
                          all(type(v) is int for v in layout["BoundingRect"].values()), "unknown dock layout attributes")
                category = "known_added_dock_layout"
            if category:
                result["allowed_changes"].append({"path": list(path), "category": category, "before": x, "after": y})
                return True
            return False
        _walk(a["reports"], b["reports"], report_ui)
        result["semantic_equivalence_proven"] = True
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError) as exc:
        result["errors"].append(str(exc))
    result["change_categories"] = dict(Counter(item["category"] for item in result["allowed_changes"]))
    result["scope"] = "Policy-bounded full raw input semantics; no bitwise kernel identity, physics/profile/model or training certificate."
    return result
