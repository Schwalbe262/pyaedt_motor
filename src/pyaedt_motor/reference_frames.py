"""Opt-in shared reference origins; never a claim that every motor has zero q flux.

Physical producer files remain unchanged. Collected local files are verified
through an immutable mapping, not through remote artifact paths in result rows.
"""
from __future__ import annotations

import argparse
import ast
import copy
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping

SCHEMA = "ipmsm_family_reference_frame_v1"
METHOD = "fixed_empirical_family_reference_v1"
PREFIX = "family-reference-frame:sha256:"
EVIDENCE_SCHEMA = "ipmsm_frame_local_evidence_v1"
CONVENTION = "dq_current_advance_v2"
PHYSICAL_FILES = ("run_ipmsm_batch.py", "module/ipmsm_geometry.py", "module/ipmsm_ppt_setup.py",
                  "module/ipmsm_waveform_quality.py", "module/variable.py")
SECTOR = [("PhaseA", "Negative"), ("PhaseB", "Positive"), ("PhaseB", "Negative"),
          ("PhaseC", "Positive"), ("PhaseC", "Negative"), ("PhaseA", "Positive")]
MOTION = {"Move Type": "Rotate", "Coordinate System": "Global", "Axis": "Z", "Is Positive": True,
          "InitPos": "InitialPositionWrapped", "Consider Mechanical Transient": False, "Angular Velocity": "MachineRPM"}
PHASE_CURRENT = {"PhaseA": "IdCommand*cos(2*pi*frq*time + ElectricalZero) - IqCommand*sin(2*pi*frq*time + ElectricalZero)",
                 "PhaseB": "IdCommand*cos(2*pi*frq*time + ElectricalZero - 120deg) - IqCommand*sin(2*pi*frq*time + ElectricalZero - 120deg)",
                 "PhaseC": "IdCommand*cos(2*pi*frq*time + ElectricalZero + 120deg) - IqCommand*sin(2*pi*frq*time + ElectricalZero + 120deg)"}
CSV_INTEGER_IDENTITIES = {"standalone_owned_pid", "exit_code", "runner_exit_code", "return_code", "Analyze_calls", "FEAs",
    "input_pole_number", "input_slot_number", "input_slot_num", "input_pole_num", "input_symmetry_factor",
    "input_series_turns_per_phase", "input_turns_per_coil_side", "input_transient_periods", "input_steps_per_period",
    "input_transient_total_steps", "input_circle_segments", "input_band_circle_segments", "input_core_bh_curve_points",
    "output_winding_geometry_count", *(f"input_mesh_{group}_elements" for group in ("magnet", "rotor", "stator", "winding", "band"))}

def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()

def _number(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " must be finite numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(label + " must be finite numeric") from exc
    if not math.isfinite(result):
        raise ValueError(label + " must be finite numeric")
    return result

def _equal(actual, expected, label):
    if isinstance(expected, bool):
        passed = type(actual) is bool and actual is expected
    elif isinstance(expected, (int, float)):
        passed = math.isclose(_number(actual, label), expected, rel_tol=1e-9, abs_tol=1e-12)
    else:
        passed = actual == expected
    if not passed:
        raise ValueError("reference-frame mismatch: " + label)

def is_family(manifest: Mapping) -> bool:
    return manifest.get("schema_version") == SCHEMA or manifest.get("method") == METHOD

def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or PurePosixPath(value).is_absolute() or any(
            part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("local evidence requires a safe relative path")
    return Path(*PurePosixPath(value).parts)

def _read(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
        raise ValueError("nonregular evidence file")
    raw = path.read_bytes()
    after = path.lstat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("evidence changed while reading")
    return raw

class LocalEvidence:
    """Explicit collected-local mapping. Semantic IDs contain no local paths."""
    def __init__(self, mapping_path: Path, root: Path):
        self.path, self.root = Path(mapping_path).absolute(), Path(root).absolute()
        self._records = {}
        self._aliases = {}
        self.raw = _read(self.path)
        self.sha256 = hashlib.sha256(self.raw).hexdigest()
        self.mapping = json.loads(self.raw.decode("utf-8-sig"))
        if self.mapping.get("schema_version") != EVIDENCE_SCHEMA:
            raise ValueError("unsupported collected-local frame evidence mapping")
        self._mapping_digest = digest(self.mapping)

    def path_for(self, relative):
        path = self.root / _relative(relative)
        for node in (path, *path.parents):
            info = node.lstat()
            if node.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("local evidence link/reparse ancestry forbidden")
            if node == self.root:
                break
        return path

    def read(self, record):
        if set(record) != {"path", "bytes", "sha256"} or type(record["bytes"]) is not int or record["bytes"] < 0:
            raise ValueError("evidence requires exact path/bytes/SHA256")
        path = self.path_for(record["path"]).resolve(strict=True)
        first_path = self._aliases.setdefault(record["path"], path)
        if first_path != path:
            raise ValueError("local evidence alias changed its canonical target")
        expected = {key: record[key] for key in ("bytes", "sha256")}
        first = self._records.setdefault(path, expected)
        if first != expected:
            raise ValueError("conflicting evidence seal for the same canonical file")
        raw = _read(path)
        observed = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if observed != first:
            raise ValueError("collected-local evidence SHA256 mismatch: " + record["path"])
        return raw

    def assert_unchanged(self):
        if digest(self.mapping) != self._mapping_digest or hashlib.sha256(_read(self.path)).hexdigest() != self.sha256:
            raise ValueError("local evidence mapping changed")
        for name, canonical in self._aliases.items():
            if self.path_for(name).resolve(strict=True) != canonical:
                raise ValueError("local evidence alias changed its canonical target")
        for path, expected in self._records.items():
            raw = _read(path)
            if {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} != expected:
                raise ValueError("frame evidence changed after validation")

    def bundle(self, section, case_id):
        entry = self.mapping.get(section, {}).get(case_id)
        if not isinstance(entry, dict) or set(entry) != {"collection_root", "task_id", "files"}:
            raise ValueError("missing explicit collected-local case audit: " + case_id)
        if type(entry["task_id"]) is not int or entry["task_id"] <= 0:
            raise ValueError("case audit requires an actual positive task ID")
        required = {"task", "result", "input", "setup", "project", "execution", "source_seal", "seal"}
        if set(entry["files"]) != required:
            raise ValueError("incomplete collected-local audit binding")
        files = entry["files"]
        values = {name: self.read(record) for name, record in files.items()}
        seal = json.loads(values["seal"].decode("utf-8-sig"))["files"]
        collection_root = self.path_for(entry["collection_root"])
        for name in required - {"seal", "task"}:
            relative = self.path_for(files[name]["path"]).relative_to(collection_root).as_posix()
            if seal.get(relative) != {key: files[name][key] for key in ("bytes", "sha256")}:
                raise ValueError("row/audit is not in the same collected seal")
        task = json.loads(values["task"].decode("utf-8-sig"))
        if task.get("task_id", task.get("id")) != entry["task_id"] or str(task.get("status")).lower() != "completed" or type(task.get("exit_code")) is not int or task["exit_code"] != 0:
            raise ValueError("origin/case task is not actually completed successfully")
        parsed = {name: json.loads(values[name].decode("utf-8-sig")) for name in ("setup", "execution", "source_seal")}
        parsed.update({name: list(csv.DictReader(io.StringIO(values[name].decode("utf-8-sig")))) for name in ("input", "result")})
        parsed["project"] = values["project"]
        parsed["records"] = {name: {key: record[key] for key in ("bytes", "sha256")} for name, record in files.items()}
        if len(parsed["input"]) != 1 or len(parsed["result"]) != 1 or parsed["input"][0].get("case_id") != case_id or parsed["result"][0].get("case_id") != case_id:
            raise ValueError("case/row/audit binding identity mismatch")
        return parsed

def _native(raw):
    """Read only serialized blocks/scalars; never load a geometry kernel or SDK."""
    root = {"tag": "ROOT", "rows": [], "children": []}; stack = [root]
    for line in raw.decode("utf-8-sig", errors="strict").splitlines():
        text = line.strip()
        begin, end = re.fullmatch(r"\$begin '([^']+)'", text), re.fullmatch(r"\$end '([^']+)'", text)
        if begin:
            node = {"tag": begin[1], "rows": [], "children": []}; stack[-1]["children"].append(node); stack.append(node)
        elif end:
            if stack[-1]["tag"] != end[1]:
                raise ValueError("saved Native block mismatch")
            stack.pop()
        else:
            stack[-1]["rows"].append(text)
    if len(stack) != 1:
        raise ValueError("incomplete saved Native serialization")
    return root

def _children(node, tag):
    return [value for value in node["children"] if value["tag"] == tag]

def _child(node, tag):
    values = _children(node, tag)
    if len(values) != 1:
        raise ValueError("missing/ambiguous saved Native block: " + tag)
    return values[0]

def _value(node, key, default=None):
    prefix = (repr(key) if " " in key else key) + "="
    values = [line[len(prefix):] for line in node["rows"] if line.startswith(prefix)]
    if not values:
        return default
    if len(set(values)) != 1:
        raise ValueError("ambiguous saved Native property")
    text = values[0]
    return {"true": True, "false": False}[text] if text in {"true", "false"} else ast.literal_eval(text)

def _call(node, key):
    values = [line[len(key)+1:-1] for line in node["rows"] if line.startswith(key+"(")]
    if len(values) != 1:
        raise ValueError("missing/ambiguous Native tuple: " + key)
    return list(ast.literal_eval("["+values[0]+"]"))

def _variable(model, name):
    properties = _child(_child(model, "ModelSetup"), "Properties")
    matches = [line for line in properties["rows"] if line.startswith("VariableProp('"+name+"',")]
    if len(matches) != 1:
        raise ValueError("missing/ambiguous saved design variable: " + name)
    # Read only the first four quoted fields; later optimizer metadata is not Python syntax.
    match = re.match(r"VariableProp\('([^']+)', '([^']*)', '([^']*)', '([^']*)'", matches[0])
    if not match:
        raise ValueError("unsupported saved variable serialization")
    return match[4]


def _scalar(text):
    match = re.fullmatch(r"([-+0-9.eE]+)(mm|deg|A|rpm)?", str(text))
    if not match:
        raise ValueError("unsupported saved literal: " + str(text))
    return _number(match[1], "saved literal")


def observe_native(raw: bytes) -> dict:
    """Observe actual saved assignments; names alone never prove a coil or PM binding."""
    project = _child(_native(raw), "AnsoftProject")
    model = _child(project, "Maxwell2DModel")
    for key, expected in {"Name": "IPMSM", "SolutionType": "Transient", "GeometryMode": "XY",
                          "Multiplier": "SymmetryFactor", "ModelDepth": "StackLength"}.items():
        _equal(_value(model, key), expected, "saved " + key)
    ms = _child(model, "ModelSetup")
    geometry = _child(ms, "GeometryCore")
    _equal(_value(geometry, "Units"), "mm", "saved model units")
    _equal(_scalar(_variable(model, "SymmetryFactor")), 1, "saved symmetry multiplier")
    operations = _child(geometry, "GeometryOperations")
    parts, body_names = {}, {}
    for part in _children(_child(operations, "ToplevelParts"), "GeometryPart"):
        attrs = _child(part, "Attributes")
        name = _value(attrs, "Name")
        if name in parts:
            raise ValueError("ambiguous saved part name")
        parts[name] = attrs
        for operation in _children(_child(part, "Operations"), "Operation"):
            ids = _children(operation, "OperationIdentity")
            for number in ([_value(ids[0], "BodyID")] if ids else []) + [_value(operation, "ParentPartID")]:
                if type(number) is int and number > 0:
                    if number in body_names and body_names[number] != name:
                        raise ValueError("ambiguous saved body assignment")
                    body_names[number] = name
    boundaries = _child(_child(model, "BoundarySetup"), "Boundaries")
    phase_ids, windings = {}, {}
    for node in boundaries["children"]:
        if _value(node, "BoundType") == "Winding Group":
            name = node["tag"]
            if name not in PHASE_CURRENT or name in phase_ids:
                raise ValueError("unexpected phase winding")
            _equal(_value(node, "Current"), PHASE_CURRENT[name], "native phase current expression")
            _equal(_value(node, "IsSolid"), False, "native stranded winding")
            _equal(str(_value(node, "ParallelBranchesNum")), "1", "parallel branches")
            group_id = _value(node, "ID")
            if type(group_id) is not int or group_id <= 0:
                raise ValueError("Native phase group ID must be a positive integer")
            phase_ids[name] = group_id
            windings[name] = PHASE_CURRENT[name]
    if set(phase_ids) != set(PHASE_CURRENT) or len(set(phase_ids.values())) != 3:
        raise ValueError("three actual winding groups are required")
    coils = []
    for node in boundaries["children"]:
        if _value(node, "BoundType") != "Coil":
            continue
        objects = _call(node, "Objects")
        if len(objects) != 1 or objects[0] not in body_names:
            raise ValueError("coil body ID is not resolved")
        body = body_names[objects[0]]
        match = re.fullmatch(r"winding(\d+)", body)
        if not match or not 1 <= int(match[1]) <= 24:
            raise ValueError("coil is not bound to an expected winding body")
        i = int(match[1]); phase, polarity = SECTOR[(i - 1) % 6]
        _equal(node["tag"], f"{phase}_{polarity}_{i:02d}", "coil name/order")
        _equal(_value(node, "PolarityType"), polarity, "coil polarity")
        winding_id = _value(node, "Winding")
        if type(winding_id) is not int or winding_id <= 0 or winding_id != phase_ids[phase]:
            raise ValueError("Native coil winding reference must match its positive integer phase ID")
        _equal(str(_value(node, "Conductor number")), "12", "coil turns")
        coils.append((i, phase, polarity))
    if sorted(i for i, _, _ in coils) != list(range(1, 25)):
        raise ValueError("24 unique actual coil bindings required")
    cs = {}
    for operation in _children(_child(operations, "CoordinateSystems"), "Operation"):
        attrs, parameters = _child(operation, "Attributes"), _child(operation, "RelativeCSParameters")
        _equal(_value(operation, "ReferenceCoordSystemID"), 1, "PM coordinate Global reference")
        cs[_value(operation, "ID")] = (_value(attrs, "Name"), parameters)
    pm = []
    for name, attrs in parts.items():
        if not re.fullmatch(r"magnet_[NS]_0[1-4]", name):
            continue
        _equal(_value(attrs, "MaterialValue"), '"NdFeB_1.25T"', "PM material assignment")
        binding = _value(attrs, "PartCoordinateSystem")
        if binding not in cs or cs[binding][0] != name + "_PM_CS":
            raise ValueError("PM local coordinate assignment mismatch")
        p = cs[binding][1]
        def mm(key):
            text = str(_value(p, key))
            if not text.endswith("mm"):
                raise ValueError("saved PM coordinates require explicit mm")
            return _number(text[:-2], "saved PM mm")
        center = [mm(key) for key in ("OriginX", "OriginY", "OriginZ")]
        xaxis = [mm(key) for key in ("XAxisXvec", "XAxisYvec", "XAxisZvec")]
        yaxis = [mm(key) for key in ("YAxisXvec", "YAxisYvec", "YAxisZvec")]
        radius = math.hypot(*center[:2])
        if radius <= 0 or abs(center[2]) > 1e-12:
            raise ValueError("PM center is not planar/radial")
        sign = -1 if "_S_" in name else 1
        # Check the source's local-axis rule at the observed center, not equality
        # of every geometry's magnetic axis or pole-center angles to the origin.
        for actual, expected in zip(xaxis, [sign*center[0]/radius, sign*center[1]/radius, 0]):
            _equal(actual, expected, "PM source orientation")
        for actual, expected in zip(yaxis, [-xaxis[1], xaxis[0], 0]):
            _equal(actual, expected, "PM right handed axes")
        pm.append({"name": name, "center_mm": center, "xaxis": xaxis, "yaxis": yaxis})
    if len(pm) != 8:
        raise ValueError("eight actual PM assignments required")
    material = _child(_child(_child(project, "Definitions"), "Materials"), "NdFeB_1.25T")
    for key, expected in {"permeability": "1.05", "conductivity": "909090", "mass_density": "7500"}.items():
        _equal(str(_value(material, key)), expected, "PM material " + key)
    coercivity = _child(material, "magnetic_coercivity")
    for key, expected in {"Magnitude": "947000A_per_meter", "DirComp1": "1", "DirComp2": "0", "DirComp3": "0"}.items():
        _equal(str(_value(coercivity, key)), expected, "PM coercivity " + key)
    motion = _child(_child(ms, "MotionSetupList"), "MotionSetup1")
    for key, expected in MOTION.items():
        _equal(_value(motion, key), 1 if key == "Coordinate System" else expected, "native motion " + key)
    moving_node = _child(_child(ms, "MotionSetupList"), "Moving1")
    _equal(_value(moving_node, "BandName"), _value(motion, "ID"), "moving/band binding")
    moving = {body_names.get(number) for number in _call(moving_node, "Objects")}
    if not ({value["name"] for value in pm} | {"rotator", "shaft", "magnet_space"}) <= moving:
        raise ValueError("a PM is absent from the rotating assignment")
    _equal(_variable(model, "NumPoles"), "pole_num", "pole variable reference")
    _equal(_variable(model, "NumSlots"), "slot_num", "slot variable reference")
    return {"units": "mm", "depth_expression": _value(model, "ModelDepth"),
            "stack_expression": _variable(model, "StackLength"), "zero_expression": _variable(model, "ElectricalZero"),
            "initial_expression": _variable(model, "InitialPositionMD"),
            "initial_wrapped_expression": _variable(model, "InitialPositionWrapped"),
            "poles": _scalar(_variable(model, "pole_num")), "slots": _scalar(_variable(model, "slot_num")),
            "coil_map": sorted(coils), "phase_group_ids": phase_ids,
            "phase_currents": windings, "magnets": sorted(pm, key=lambda value: value["name"]),
            "material_assignments": {name: _value(attrs, "MaterialValue") for name, attrs in parts.items()},
            "motion": MOTION}


def _frame_source_closure(raw_files):
    """Explicit coordinate implementation projection; raw producers remain separately pinned.

    Mesh controls may change without changing the definition of an encoder origin.
    Numerical solver equivalence and BEMF alignment are deliberately not inferred.
    """
    ppt = ast.parse(raw_files["module/ipmsm_ppt_setup.py"])
    excluded = {"assign_mesh", "audit_mesh_operation_settings", "audit_native_mesh_settings", "validate_ppt_spec_contract"}
    members = {}
    for node in ppt.body:
        if isinstance(node, ast.FunctionDef) and node.name in excluded:
            continue
        if isinstance(node, ast.ClassDef) and node.name == "IPMSMPPTSpec":
            node = copy.deepcopy(node)
            node.body = [value for value in node.body if not (isinstance(value, ast.AnnAssign)
                         and isinstance(value.target, ast.Name) and value.target.id in
                         {"mesh_elements", "mesh_maximum_lengths_mm", "mesh_refinement_modes",
                          "airgap_band_mapping_angle_deg"})]
        if isinstance(node, ast.FunctionDef) and node.name == "clear_previous_ppt_setup":
            node = copy.deepcopy(node)
            for section in node.body:
                if not (isinstance(section, ast.Try) and section.body and
                        ast.unparse(section.body[0]) == "module = m2d.odesign.GetModule('MeshSetup')"):
                    continue
                additions = {
                    "mesh_type": (ast.Tuple, ("Length Based", "Skin Depth Based", "Surface Approximation Based", "Cylindrical Gap Based")),
                    "name": (ast.List, ("Mesh_magnet", "Mesh_rotor", "Mesh_stator", "Mesh_winding", "Mesh_band", "Mesh_AirGap")),
                }
                for loop in section.body:
                    if isinstance(loop, ast.For) and isinstance(loop.target, ast.Name) and loop.target.id in additions:
                        kind, values = additions[loop.target.id]
                        if (isinstance(loop.iter, kind) and all(isinstance(item, ast.Constant) for item in loop.iter.elts)
                                and tuple(item.value for item in loop.iter.elts) == values):
                            loop.iter.elts.pop()  # Only the exact BMA addition; retain the entire cleanup body.
        if isinstance(node, ast.FunctionDef) and node.name == "configure_ipmsm_from_ppt":
            node = copy.deepcopy(node)
            # The two added conditional native mesh readbacks do not assign an axis/current/PM.
            node.body = [value for value in node.body if not (isinstance(value, ast.If)
                         and ast.unparse(value.test) in {"spec.mesh_refinement_modes",
                             "spec.mesh_refinement_modes or spec.airgap_band_mapping_angle_deg is not None"})]
        members[getattr(node, "name", f"top_{len(members)}")] = digest(ast.dump(node, include_attributes=False))
    runner = ast.parse(raw_files["run_ipmsm_batch.py"])
    build = copy.deepcopy(next(node for node in runner.body if isinstance(node, ast.FunctionDef) and node.name == "build_spec"))
    for node in ast.walk(build):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "IPMSMPPTSpec":
            node.keywords = [item for item in node.keywords if item.arg not in
                             {"mesh_elements", "mesh_maximum_lengths_mm", "mesh_refinement_modes",
                              "airgap_band_mapping_angle_deg"}]
    members["runner_build_non_mesh_spec"] = digest(ast.dump(build, include_attributes=False))
    # Helpers used to read the retained spec fields are pinned in their entirety.
    for node in runner.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"case_value", "case_float", "case_int", "case_bool",
                "case_strict_positive_float", "case_str", "validate_transient_spec"}:
            members["runner_" + node.name] = digest(ast.dump(node, include_attributes=False))
    return {"schema_version": "ipmsm_coordinate_source_projection_v1", "ppt_and_input_functions": members,
            "whole_files": {name: hashlib.sha256(raw_files[name]).hexdigest() for name in
                            ("module/ipmsm_geometry.py", "module/variable.py", "module/ipmsm_waveform_quality.py")}}


def source_scope(evidence: LocalEvidence, section=None) -> dict:
    """Pin renderer bytes, imported geometry library, and material pack separately from local paths."""
    mapping = evidence.mapping if section is None else evidence.mapping[section]
    seal = json.loads(evidence.read(mapping["source_seal"]))
    physical = mapping["physical_files"]
    library = mapping["library_files"]
    if set(physical) != set(PHYSICAL_FILES) or len(library) != 23:
        raise ValueError("frame scope requires the five renderer files and the 23-file library")
    raw_files = {}
    for label, records in (("files", physical), ("library_files", library)):
        for name, record in records.items():
            raw = evidence.read(record)
            if label == "files":
                raw_files[name] = raw
            if seal[label].get(name) != {key: record[key] for key in ("bytes", "sha256")}:
                raise ValueError("source/library identity is not in the runtime seal")
    pack_record = mapping["material_pack"]
    json.loads(evidence.read(pack_record))
    pack_semantic = {key: pack_record[key] for key in ("bytes", "sha256")}
    if seal["files"].get("configs/material_m19_29g_reference_y2.json") != pack_semantic:
        raise ValueError("material pack is outside the sealed renderer source")
    producer = "producer_v3:sha256:" + digest({name: record["sha256"] for name, record in physical.items()})
    return {"producer_fingerprint": producer, "coordinate_source_projection": _frame_source_closure(raw_files),
            "physical_files": {name: {key: record[key] for key in ("bytes", "sha256")} for name, record in physical.items()},
            "library_files": {name: {key: record[key] for key in ("bytes", "sha256")} for name, record in library.items()},
            "source_declaration_sha256": mapping["source_seal"]["sha256"], "material_pack": pack_semantic,
            "phase_sector": [list(value) for value in SECTOR], "phase_currents": PHASE_CURRENT,
            "motion": MOTION, "poles": 8, "slots": 12, "model_extent": "full_360", "symmetry_factor": 1,
            "fixed_materials": {"magnet_material": "NdFeB_1.25T", "winding_material": "copper",
                                "shaft_material": "vacuum", "air_material": "air"},
            "convention": CONVENTION, "aedt_version": "2025.2"}


def _row_value(row, key):
    if "input_" + key not in row:
        raise ValueError("actual row lacks frame context: " + key)
    return row["input_" + key]


def _case_audit(bundle, scope, *, angle, identity, material_context, winding_context, material_fingerprint,
                initial_position_deg):
    from .remote_case import solver_row_context, _flag
    row, planned, setup, execution = bundle["result"][0], bundle["input"][0], bundle["setup"], bundle["execution"]
    producer = scope["producer_fingerprint"]
    quiet = solver_row_context(row, producer)
    if (row.get("status") != "ok" or quiet["solver_context_known"] is not True or quiet["solver_state_uncertain"] is not False or
            _flag(row.get("analyze")) is not True or _flag(row.get("standalone_analyze_return_received")) is not True):
        raise ValueError("family evidence requires a successfully solved, quiescent actual case")
    for key, expected in {"case_id": row["case_id"], "producer_fingerprint": producer, "phase": "complete",
                          "runner_exit_code": 0, "solver_context_known": True, "solver_state_uncertain": False,
                          "cases_sha256": bundle["records"]["input"]["sha256"],
                          "source_sha256": bundle["records"]["source_seal"]["sha256"]}.items():
        _equal(execution.get(key), expected, "case execution " + key)
    for label in ("physical_files", "library_files"):
        records = bundle["source_seal"]["files" if label == "physical_files" else label]
        if any(records.get(name) != value for name, value in scope[label].items()):
            raise ValueError("actual case renderer/library closure differs from the reference family")
    if setup.get("validation") is not True or setup.get("analysis") is not True or setup.get("setup_issues") != []:
        raise ValueError("actual setup/analysis did not pass")
    expected = {**material_context, **winding_context, **scope["fixed_materials"], "electrical_zero_deg": angle,
                "initial_position_deg": initial_position_deg, "beta_convention": CONVENTION,
                "model_extent": "full_360", "symmetry_factor": 1, "pole_number": 8, "slot_number": 12}
    spec = setup["requested_spec"]
    for key, value in expected.items():
        _equal(spec.get(key), value, "requested/actual scope " + key)
    for key, value in {**material_context, **winding_context, **scope["fixed_materials"], "material_fingerprint": material_fingerprint,
                       "producer_fingerprint": producer, "metric_contract_version": "motor_metrics_v3",
                       "aedt_version": scope["aedt_version"],
                       "beta_convention": CONVENTION, "electrical_zero_deg": angle,
                       "initial_position_deg": initial_position_deg, "beta_calibration_id": identity}.items():
        _equal(_row_value(row, key), value, "actual row " + key)
    for key, value in planned.items():
        actual = row.get("input_" + key, row.get(key))
        if actual is None:  # Metadata is not all exported; the original CSV is itself sealed and case-bound.
            continue
        if str(actual) != str(value):
            try:
                _equal(actual, _number(value, key), "plan/actual " + key)
            except ValueError:
                if _flag(actual) is None or _flag(actual) is not _flag(value):
                    raise ValueError("plan/actual context mismatch: " + key) from None
    observed = observe_native(bundle["project"])
    from ipmsm_optimization import GEOMETRY_VARIABLE_NAMES
    model = _child(_child(_native(bundle["project"]), "AnsoftProject"), "Maxwell2DModel")
    for key in GEOMETRY_VARIABLE_NAMES:
        _equal(_scalar(_variable(model, key)), _number(planned[key], key), "saved geometry " + key)
    from .doe import physical_geometry_hash
    _equal(physical_geometry_hash(planned), row.get("design_hash"), "actual geometry identity")
    _equal(observed["poles"], scope["poles"], "actual poles")
    _equal(observed["slots"], scope["slots"], "actual slots")
    _equal(observed["zero_expression"], f"{angle:g}deg", "saved ElectricalZero")
    _equal(observed["initial_expression"], f"{initial_position_deg:g}deg", "saved initial position")
    _equal(observed["initial_wrapped_expression"], f"{initial_position_deg % 360:g}deg", "wrapped initial position")
    stack = _number(spec["stack_length_mm"], "stack")
    _equal(observed["stack_expression"], f"{stack:g}mm", "saved StackLength")
    for name in ("native_model_depth_audit", "native_model_depth_audit_post_validation", "native_model_depth_audit_pre_analysis"):
        audit = setup.get(name, {})
        for key, value in {"passed": True, "model_units_live": "mm", "native_depth_expression": "StackLength",
                           "native_depth_SI_m": float(f"{stack:g}") / 1000}.items():
            _equal(audit.get(key), value, "live depth " + key)
    defs = setup["material_definitions"]
    for key, value in {"material_pack_sha256": scope["material_pack"]["sha256"], "bh_curve_points": 23,
                       "native_readback_verified": True, "native_definition_unchanged_by_cache_bridge": True}.items():
        _equal(defs.get(key), value, "material readback " + key)
    materials = setup["materials"]
    _equal(materials.get("errors"), [], "material assignment errors")
    expected_materials = {"stator": material_context["core_material"], "rotator": material_context["core_material"],
        **{f"winding{i}": scope["fixed_materials"]["winding_material"] for i in range(1, 25)},
        **{f"magnet_{pole}_{i:02d}": scope["fixed_materials"]["magnet_material"] for pole in "NS" for i in range(1, 5)},
        "shaft": scope["fixed_materials"]["shaft_material"],
        **{name: scope["fixed_materials"]["air_material"] for name in ("Band", "Region", "magnet_space")}}
    live = materials.get("material_readback")
    if not isinstance(live, dict) or set(live) != set(expected_materials) or set(observed["material_assignments"]) != set(expected_materials):
        raise ValueError("complete live/saved material assignment coverage is required")
    for name, required in expected_materials.items():
        _equal(live[name], required, "live material " + name)
        _equal(observed["material_assignments"][name], '"' + required + '"', "saved material " + name)
    audit = setup["winding_geometry_audit"]
    for key, value in {"passed": True, "count": 24, "turns_per_coil_side": winding_context["turns_per_coil_side"],
                       "conductor_area_per_branch_mm2": winding_context["conductor_area_per_branch_mm2"],
                       "slot_fill_limit": winding_context["slot_fill_limit"]}.items():
        _equal(audit.get(key), value, "actual winding audit " + key)
    return row, {"case_id": row["case_id"], "design_hash": row.get("design_hash"),
                 "records": bundle["records"], "native_frame": observed}


def draft_reference(origin: Mapping, evidence: LocalEvidence, *, assign_identity=False) -> dict:
    """Default dry: validate an observed origin, return an unissued declaration, write nothing."""
    from calibrate_ipmsm_beta import validated_zero_manifest, analyze_zero_calibration_rows
    implementation_sha256()
    angle, _ = validated_zero_manifest(origin)
    origin_scope = source_scope(evidence)
    scope = source_scope(evidence, "renderer") if "renderer" in evidence.mapping else origin_scope
    for key in ("coordinate_source_projection", "library_files", "material_pack", "phase_sector", "phase_currents", "motion", "fixed_materials"):
        if origin_scope[key] != scope[key]:
            raise ValueError("explicit origin-to-renderer coordinate closure differs: " + key)
    if origin.get("producer_fingerprint") != origin_scope["producer_fingerprint"]:
        raise ValueError("two-speed origin does not belong to this renderer source")
    if origin.get("successful_speeds_rpm") != [1200.0, 5000.0] or origin.get("successful_rows") != 2:
        raise ValueError("family origin needs exactly the two completed reference speeds")
    rows, observations = [], []
    if len(evidence.mapping.get("origin_cases", {})) != 2:
        raise ValueError("exactly two explicitly mapped origin cases required")
    for case_id in evidence.mapping["origin_cases"]:
        bundle = evidence.bundle("origin_cases", case_id)
        row, observation = _case_audit(bundle, origin_scope, angle=0, identity="",
                material_context=origin["material_context"], winding_context=origin["winding_context"],
                material_fingerprint=origin["material_fingerprint"], initial_position_deg=origin["initial_position_deg"])
        if _number(row["input_i_peak_a"], "origin current") != 0 or _number(row["input_beta_dq_deg"], "origin beta") != 0:
            raise ValueError("empirical reference origin must be measured at no load")
        rows.append(row); observations.append(observation)
    # Mapping serialization order is not the origin measurement order. Reproduce
    # the signed manifest's actual observation order, even for unrelated case IDs.
    speed_order = {float(value["speed_rpm"]): index for index, value in enumerate(origin["observations"])}
    if len(speed_order) != 2:
        raise ValueError("origin observation speeds are not unique")
    rows.sort(key=lambda row: speed_order.get(float(row["input_base_rpm"]), 2))
    recomputed = analyze_zero_calibration_rows(rows)
    if recomputed != dict(origin):
        raise ValueError("origin manifest is not reproduced by its actual two-speed observations")
    payload = {"schema_version": SCHEMA, "method": METHOD, "convention": CONVENTION,
               "protocol_implementation_sha256": _IMPORTED_SOURCE_SHA256,
               "electrical_zero_deg": angle, "initial_position_deg": origin["initial_position_deg"],
               "producer_fingerprint": scope["producer_fingerprint"],
               "origin_producer_fingerprint": origin_scope["producer_fingerprint"],
               "metric_contract": origin["metric_contract"], "back_emf_source": origin["back_emf_source"],
               "material_fingerprint": origin["material_fingerprint"],
               "material_context": origin["material_context"], "winding_context": origin["winding_context"], "scope": scope,
               "origin_scope": origin_scope, "origin_zero_manifest": dict(origin), "origin_evidence": observations,
               "transfer_policy": "explicit_coordinate_closure_v1",
               "claims": {"all_geometries_BEMF_aligned": False, "lambda_q_zero": False,
                          "absolute_geometric_axis_certified": False, "profile_or_training_certified": False}}
    evidence.assert_unchanged()
    return {**payload, "reference_frame_id": PREFIX + digest(payload) if assign_identity else None}


def validate_reference(manifest: Mapping, evidence: LocalEvidence) -> tuple[float, str]:
    expected = draft_reference(manifest["origin_zero_manifest"], evidence, assign_identity=True)
    if canonical(dict(manifest)) != canonical(expected):
        raise ValueError("family reference content, scope, or actual origin evidence mismatch")
    return expected["electrical_zero_deg"], expected["reference_frame_id"]


def resolve_reference(manifest: Mapping, evidence: LocalEvidence | None = None) -> tuple[float, str]:
    if is_family(manifest):
        if evidence is None:
            raise ValueError("family reference requires explicit collected-local evidence")
        return validate_reference(manifest, evidence)
    from calibrate_ipmsm_beta import validated_zero_manifest
    return validated_zero_manifest(manifest)


def semantic_context(manifest):
    return {"method": METHOD, "reference_frame_id": manifest["reference_frame_id"],
            "protocol_implementation_sha256": manifest["protocol_implementation_sha256"],
            "manifest_sha256": digest(manifest), "scope_sha256": digest(manifest["scope"]),
            "electrical_zero_deg": manifest["electrical_zero_deg"], "initial_position_deg": manifest["initial_position_deg"],
            "origin_producer_fingerprint": manifest["origin_producer_fingerprint"],
            "renderer_producer_fingerprint": manifest["producer_fingerprint"]}


def implementation_sha256():
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != _IMPORTED_SOURCE_SHA256:
        raise ValueError("reference-frame implementation changed after import")
    return _IMPORTED_SOURCE_SHA256


def frame_fields(manifest):
    return {"reference_frame_method": METHOD, "reference_frame_id": manifest["reference_frame_id"]} if is_family(manifest) else {}


def evidence_arguments(parser):
    parser.add_argument("--frame-evidence", type=Path, help="Immutable collected-local frame audit mapping")
    parser.add_argument("--frame-evidence-root", type=Path, help="Explicit local root for mapping-relative files")
    parser.add_argument("--owned-cli-production-evidence", type=Path, help="Explicit new production case-evidence declaration")
    parser.add_argument("--owned-cli-production-evidence-sha256")


def evidence_from_arguments(args):
    if bool(args.frame_evidence) != bool(args.frame_evidence_root):
        raise ValueError("frame evidence mapping and root must be supplied together")
    result = LocalEvidence(args.frame_evidence, args.frame_evidence_root) if args.frame_evidence else None
    cli_path = getattr(args, "owned_cli_production_evidence", None)
    cli_sha = getattr(args, "owned_cli_production_evidence_sha256", None)
    if bool(cli_path) != bool(cli_sha):
        raise ValueError("production CLI evidence path and SHA must be supplied together")
    if cli_path:
        if result is None:
            raise ValueError("production CLI evidence also requires actual family origin evidence")
        from .execution_compatibility import ProductionFrameEvidence
        raw = _read(cli_path)
        if hashlib.sha256(raw).hexdigest() != cli_sha:
            raise ValueError("production CLI evidence declaration SHA differs")
        result = ProductionFrameEvidence(result, {"path": str(cli_path), "bytes": len(raw), "sha256": cli_sha})
    return result


def apply_reference(spec: Mapping, manifest: Mapping, evidence: LocalEvidence | None = None) -> dict:
    """Separate opt-in path; the per-geometry calibration verifier is unchanged."""
    from calibrate_ipmsm_beta import apply_zero_manifest_to_spec, fixed_case_context_fields, _require_zero_context
    if not is_family(manifest):
        if spec.get("reference_frame") is not None:
            raise ValueError("an explicit family spec cannot use a per-geometry calibration manifest")
        return apply_zero_manifest_to_spec(spec, manifest)
    if evidence is None:
        raise ValueError("family reference requires explicit collected-local evidence")
    angle, identity = validate_reference(manifest, evidence)
    if spec.get("reference_frame") is not None and spec["reference_frame"] != dict(manifest):
        raise ValueError("source spec reference-frame declaration differs")
    updated = copy.deepcopy(spec)
    updated["beta_calibration"] = {"electrical_zero_deg": angle, "calibration_id": identity, "convention": CONVENTION}
    updated["reference_frame"] = dict(manifest)
    from ipmsm_optimization import optimization_spec_from_mapping
    parsed = optimization_spec_from_mapping(updated)
    _require_zero_context(fixed_case_context_fields(parsed.material_context, parsed.winding), manifest["origin_zero_manifest"])
    return updated


def verify_rows(rows, manifest: Mapping, evidence: LocalEvidence) -> dict:
    from .execution_compatibility import ProductionFrameEvidence
    if isinstance(evidence, ProductionFrameEvidence):
        return evidence.verify_rows(rows, manifest)
    angle, identity = validate_reference(manifest, evidence)
    admitted, seen = [], set()
    for row in rows:
        if str(row.get("status", "ok")).lower() != "ok":
            continue  # Failure is preserved by existing gates, never promoted by frame admission.
        case_id = str(row.get("case_id", ""))
        if case_id in seen:
            raise ValueError("duplicate family-frame result case")
        seen.add(case_id)
        bundle = evidence.bundle("case_audits", case_id)
        actual, observation = _case_audit(bundle, manifest["scope"], angle=angle, identity=identity,
                material_context=manifest["material_context"], winding_context=manifest["winding_context"],
                material_fingerprint=manifest["material_fingerprint"], initial_position_deg=manifest["initial_position_deg"])
        planned = bundle["input"][0]
        _equal(planned.get("reference_frame_method"), METHOD, "flat case reference method")
        _equal(planned.get("reference_frame_id"), identity, "flat case reference identity")
        _verify_collected_row(row, actual, frame_fields(manifest))
        admitted.append(observation)
    evidence.assert_unchanged()
    admitted.sort(key=lambda value: value["case_id"])
    return {**semantic_context(manifest),
            "admitted_cases": len(admitted), "admitted_case_ids": [value["case_id"] for value in admitted],
            "case_audits_sha256": digest(admitted),
            }


def _missing_csv(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {"", "nan", "none", "null", "<na>"}
    try:
        return math.isnan(value)
    except (TypeError, ValueError):
        return False


def _verify_collected_row(row, actual, explicit_fields):
    """Bind targets, diagnostics, owner state and inputs, not merely the coordinate labels.

    source_file is the sole unrelated extra added by prepare_training_data.
    Empty union columns, CSV numeric/boolean serialization and JSON key order
    may differ. No target replacement or new physics tolerance is introduced.
    """
    for key in set(row) | set(actual):
        if key == "source_file":
            continue
        value, expected = row.get(key), actual.get(key)
        if key in explicit_fields and key not in actual:
            _equal(value, explicit_fields[key], "explicit row frame " + key)
            continue
        if _missing_csv(value) and _missing_csv(expected):
            continue
        passed = False
        if key in CSV_INTEGER_IDENTITIES:
            try:
                passed = _exact_integer_identity(value) == _exact_integer_identity(expected)
            except ValueError:
                pass
        elif isinstance(expected, str) and expected.strip().casefold() in {"true", "false"}:
            from .remote_case import _flag
            passed = _flag(value) is _flag(expected)
        elif str(value) == str(expected):
            passed = True
        elif isinstance(expected, str) and expected.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value) if isinstance(value, str) else value
                passed = canonical(parsed) == canonical(json.loads(expected))
            except (TypeError, ValueError):
                pass
        else:
            try:
                passed = math.isclose(_number(value, key), _number(expected, key), rel_tol=1e-9, abs_tol=1e-12)
            except ValueError:
                pass
        if not passed:
            raise ValueError("row is not its collected actual audit: " + key)


def _exact_integer_identity(value):
    """CSV 123 and 123.0 are equivalent; a fractional PID/count is never close enough."""
    if isinstance(value, bool):
        raise ValueError("integer identity cannot be boolean")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("integer identity must be finite and integral") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("integer identity must be finite and integral")
    return number


def verify_domain(domain, manifest, evidence):
    from .execution_compatibility import ProductionFrameEvidence
    if isinstance(evidence, ProductionFrameEvidence):
        return evidence.verify_domain(domain, manifest)
    expected = domain.get("reference_frame_context")
    if not isinstance(expected, dict) or not expected.get("admitted_case_ids"):
        raise ValueError("approved model lacks actual family-frame case admission evidence")
    rows = [evidence.bundle("case_audits", case)["result"][0] for case in expected["admitted_case_ids"]]
    if verify_rows(rows, manifest, evidence) != expected:
        raise ValueError("approved model and supplied local case-audit mapping differ")


def verify_spec_rows(spec, rows, evidence=None):
    manifest = spec.get("reference_frame")
    successful = [row for row in rows if str(row.get("status", "ok")).lower() == "ok"]
    family_ids = [str(row.get("input_beta_calibration_id", "")).startswith(PREFIX) for row in successful]
    if manifest is None:
        if any(family_ids):
            raise ValueError("family rows require an explicit source-spec reference-frame declaration")
        return None
    if not is_family(manifest) or evidence is None or not all(family_ids):
        raise ValueError("family spec requires matching actual family rows and collected-local audits")
    angle, identity = validate_reference(manifest, evidence)
    if spec.get("beta_calibration") != {"electrical_zero_deg": angle, "calibration_id": identity, "convention": CONVENTION}:
        raise ValueError("source spec beta block differs from its explicit family frame")
    return verify_rows(rows, manifest, evidence)


def profile_context(profile, manifest: Mapping) -> None:
    expected = semantic_context(manifest)
    for record in profile.evidence:
        raw = _read(Path(record["path"]))
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("selected profile evidence changed")
        try:
            context = json.loads(raw).get("reference_frame_context", {})
        except (ValueError, UnicodeDecodeError, AttributeError):
            continue
        if all(context.get(key) == value for key, value in expected.items()):
            return
    raise ValueError("selected profile lacks this actual family-reference quality evidence")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate an unissued empirical family reference; no publication")
    parser.add_argument("--origin-manifest", type=Path, required=True)
    evidence_arguments(parser)
    args = parser.parse_args(argv)
    evidence = evidence_from_arguments(args)
    if evidence is None:
        raise ValueError("explicit evidence required")
    draft = draft_reference(json.loads(args.origin_manifest.read_bytes()), evidence)
    print(json.dumps({"status": "prepared_unissued", "reference_frame_id": None,
                      "origin_producer": draft["origin_producer_fingerprint"],
                      "renderer_producer": draft["producer_fingerprint"], "claims": draft["claims"]}, sort_keys=True))
    return 0


_IMPORTED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

if __name__ == "__main__":
    raise SystemExit(main())
