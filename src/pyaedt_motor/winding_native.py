"""Separate spec-bound Native observer; legacy v1 protocol remains byte-identical."""
import ast,re,math
from .reference_frames import (_native,_child,_children,_value,_call,_variable,_equal,_scalar,_number,
    SECTOR,PHASE_CURRENT,MOTION)

def observe_native(raw: bytes, *, turns_per_coil_side: int) -> dict:
    if type(turns_per_coil_side)is not int or turns_per_coil_side<=0:raise ValueError("typed positive winding turns required")
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
        _equal(str(_value(node, "Conductor number")), str(turns_per_coil_side), "coil turns")
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
