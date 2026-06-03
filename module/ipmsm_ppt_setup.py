"""
PyAEDT setup helpers for the 20231012 IPMSM practice deck.

The geometry is expected to exist already. These helpers configure the Maxwell
2D transient setup flow from the deck: materials, moving band, boundaries,
three-phase current windings, mesh operations, core/eddy loss flags, and the
multi-period transient setup.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from typing import Any


DEFAULT_CORE_MATERIAL_NAME = "27PNF1500_CustomCoreLoss"
DEFAULT_CORE_RESISTIVITY_OHM_M = 58e-8
DEFAULT_CORE_CONDUCTIVITY_S_PER_M = 1.0 / DEFAULT_CORE_RESISTIVITY_OHM_M
DEFAULT_CORE_MASS_DENSITY_KG_PER_M3 = 7650.0
DEFAULT_CORE_LOSS_COEFFICIENTS = {
    "Kh": 114.6274726,
    "Kc": 0.39387732,
    "Ke": 0.0,
    "Y": 1.66,
    "Kdc": 0.0,
}


@dataclass
class IPMSMPPTSpec:
    pole_number: int = 8
    slot_number: int = 12
    symmetry_factor: int = 1
    base_rpm: float = 1200.0
    i_peak_a: float = 137.8
    beta_deg: float = 0.0
    series_turns_per_phase: int = 48
    turns_per_coil_side: int = 12
    stack_length_mm: float = 49.45
    phase_resistance_ohm: float = 0.01
    vdc_v: float = 200.0
    initial_position_deg: float = -22.5
    transient_periods: int = 10
    steps_per_period: int = 90
    core_material: str = DEFAULT_CORE_MATERIAL_NAME
    core_material_fallbacks: tuple[str, ...] = ("30PNF1600", "steel_1008 - Copy", "steel_1008")
    magnet_material: str = "NdFeB_1.25T"
    winding_material: str = "copper"
    shaft_material: str = "vacuum"
    air_material: str = "air"
    setup_name: str = "PPT_Transient"
    mesh_elements: dict[str, int] = field(
        default_factory=lambda: {
            "magnet": 50,
            "rotor": 500,
            "stator": 500,
            "winding": 50,
            "band": 1000,
        }
    )


DEFAULT_12S8P_SLOT_MAP = [
    ("PhaseA", "Positive"),
    ("PhaseC", "Negative"),
    ("PhaseB", "Positive"),
    ("PhaseA", "Negative"),
    ("PhaseC", "Positive"),
    ("PhaseB", "Negative"),
    ("PhaseA", "Positive"),
    ("PhaseC", "Negative"),
    ("PhaseB", "Positive"),
    ("PhaseA", "Negative"),
    ("PhaseC", "Positive"),
    ("PhaseB", "Negative"),
]


def _m2d(design: Any) -> Any:
    """Return the underlying PyAEDT Maxwell2d object from the custom wrapper."""
    return getattr(design, "solver_instance", design)


def _name(obj: Any) -> str:
    return obj if isinstance(obj, str) else obj.name


def _names(items: Any) -> list[str]:
    if items is None:
        return []
    if isinstance(items, (str, bytes)):
        return [str(items)]
    return [_name(item) for item in items if item is not None]


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def _refresh_modeler(m2d: Any) -> None:
    """Refresh PyAEDT's modeler cache after AEDT editor boolean operations."""
    for method_name in ("refresh", "refresh_all_ids", "cleanup_objects"):
        try:
            getattr(m2d.modeler, method_name)()
        except Exception:
            pass


def _editor_object_names(m2d: Any) -> list[str]:
    """Read the live object list from AEDT's editor instead of PyAEDT's cache."""
    try:
        editor = m2d.modeler.oeditor
    except Exception:
        return []

    names: list[str] = []
    for group in ("Solids", "Sheets", "Lines", "Unclassified", "UnClassified", "Non Model"):
        try:
            names.extend(str(name) for name in editor.GetObjectsInGroup(group))
        except Exception:
            pass
    return sorted(dict.fromkeys(names), key=_natural_key)


def _object_names(m2d: Any) -> list[str]:
    editor_names = _editor_object_names(m2d)
    if editor_names:
        return editor_names
    for attr in ("object_names", "objects"):
        try:
            value = getattr(m2d.modeler, attr)
            if isinstance(value, dict):
                return list(value)
            return list(value)
        except Exception:
            pass
    return []


def _delete_editor_objects(m2d: Any, names: list[str]) -> list[str]:
    """Delete modeler objects by name through the raw AEDT editor API."""
    live_names = set(_editor_object_names(m2d))
    to_delete = [name for name in names if name in live_names]
    if not to_delete:
        return []

    try:
        m2d.modeler.oeditor.Delete(["NAME:Selections", "Selections:=", ",".join(to_delete)])
    except Exception:
        m2d.modeler.delete(assignment=to_delete)
    _refresh_modeler(m2d)
    return to_delete


def _copied_tool_names(m2d: Any, tool_names: list[str]) -> list[str]:
    """Find AEDT boolean tool copies such as winding1_1 that are not real coil bodies."""
    live_names = _editor_object_names(m2d)
    copied: list[str] = []
    for tool in tool_names:
        pattern = re.compile(rf"{re.escape(tool)}_\d+$", re.IGNORECASE)
        copied.extend(name for name in live_names if pattern.fullmatch(name))
    return sorted(dict.fromkeys(copied), key=_natural_key)


def _clone_objects(m2d: Any, names: list[str]) -> list[str]:
    """Clone objects in place and return the new object names."""
    if not names:
        return []

    before = set(_object_names(m2d))
    cloned: Any = []
    try:
        cloned_result = m2d.modeler.clone(names)
        if isinstance(cloned_result, tuple):
            cloned = cloned_result[1]
        else:
            cloned = cloned_result
    except Exception:
        try:
            m2d.modeler.copy(names)
            cloned = m2d.modeler.paste()
        except Exception:
            cloned = []

    _refresh_modeler(m2d)
    cloned_names = _names(cloned)
    if not cloned_names:
        after = set(_object_names(m2d))
        cloned_names = sorted(after - before, key=_natural_key)

    original = set(names)
    return [name for name in cloned_names if name not in original]


def _rename_objects(m2d: Any, names: list[str], prefix: str) -> list[str]:
    """Rename temporary objects so AEDT prefix caches do not pollute real object groups."""
    used = set(_object_names(m2d))
    renamed: list[str] = []
    for index, old_name in enumerate(names, start=1):
        new_name = f"{prefix}{index:02d}"
        suffix = 2
        while new_name in used:
            new_name = f"{prefix}{index:02d}_{suffix}"
            suffix += 1
        try:
            m2d.modeler.oeditor.RenamePart(
                [
                    "NAME:Rename Data",
                    "Old Name:=",
                    old_name,
                    "New Name:=",
                    new_name,
                ]
            )
            renamed.append(new_name)
            used.add(new_name)
        except Exception:
            renamed.append(old_name)
    _refresh_modeler(m2d)
    return renamed


def _subtract_with_temporary_tools(
    m2d: Any,
    blanks: list[str],
    tools: list[str],
    temporary_prefix: str,
) -> dict[str, Any]:
    """Subtract cloned tools from blanks so original coils and magnets remain clean."""
    cloned_tools = _clone_objects(m2d, tools)
    if not cloned_tools:
        return {"subtract": "skipped: temporary tool clone failed", "temporary_tools": []}

    temporary_tools = _rename_objects(m2d, cloned_tools, temporary_prefix)
    result: dict[str, Any] = {"temporary_tools": temporary_tools}
    try:
        result["subtract"] = m2d.modeler.subtract(
            blank_list=blanks,
            tool_list=temporary_tools,
            keep_originals=False,
        )
        _refresh_modeler(m2d)
        leftovers = [name for name in temporary_tools if name in _object_names(m2d)]
        result["deleted_leftover_tools"] = _delete_editor_objects(m2d, leftovers)
    except Exception as exc:
        result["subtract"] = f"skipped: {exc}"
        result["deleted_leftover_tools"] = _delete_editor_objects(m2d, temporary_tools)
    return result


def _has_design_variable(design: Any, name: str) -> bool:
    """Return True when a design/model variable already exists."""
    candidates = [design, _m2d(design)]
    for obj in candidates:
        try:
            variables = getattr(obj, "variables")
            if name in variables:
                return True
        except Exception:
            pass
        try:
            variable_manager = getattr(obj, "variable_manager")
            for attr in ("variables", "design_variables", "independent_variables"):
                value = getattr(variable_manager, attr)
                if isinstance(value, dict) and name in value:
                    return True
                if isinstance(value, (list, tuple, set)) and name in value:
                    return True
        except Exception:
            pass
    return False


def _auto_group_names(m2d: Any, prefixes: tuple[str, ...], key: str | None = None) -> list[str]:
    available = _object_names(m2d)
    matches = [
        name
        for name in available
        if any(name.lower().startswith(prefix.lower()) for prefix in prefixes)
    ]
    if key == "magnets":
        matches = [name for name in matches if "space" not in name.lower()]
    if key == "windings":
        matches = [name for name in matches if re.fullmatch(r"winding\d+_\d+", name.lower()) is None]
    return sorted(matches, key=_natural_key)


def _existing(m2d: Any, names: list[str]) -> list[str]:
    available = set(_object_names(m2d))
    return [name for name in names if name in available]


def _get_group(m2d: Any, object_groups: dict[str, Any] | None, key: str, prefixes: tuple[str, ...]) -> list[str]:
    auto_names = _auto_group_names(m2d, prefixes, key=key)
    if object_groups and key in object_groups:
        explicit = _existing(m2d, _names(object_groups[key]))
        if key in {"magnets", "windings"}:
            return sorted(dict.fromkeys(explicit + auto_names), key=_natural_key)
        return explicit
    return auto_names


def _get_object(m2d: Any, name: str) -> Any | None:
    try:
        return m2d.modeler.get_object_from_name(name)
    except Exception:
        return None


def _object_center_xy(obj: Any) -> tuple[float, float] | None:
    try:
        face = obj.faces[0]
        if not isinstance(face, int):
            center = face.center
            return float(center[0]), float(center[1])
    except Exception:
        pass
    try:
        box = obj.bounding_box
        if isinstance(box, (list, tuple)) and len(box) >= 6:
            return (float(box[0]) + float(box[3])) / 2.0, (float(box[1]) + float(box[4])) / 2.0
        if isinstance(box, (list, tuple)) and len(box) >= 4:
            return (float(box[0]) + float(box[2])) / 2.0, (float(box[1]) + float(box[3])) / 2.0
    except Exception:
        pass
    return None


def _object_edges(m2d: Any, obj_name: str) -> list[Any]:
    obj = _get_object(m2d, obj_name)
    if not obj:
        return []
    try:
        return list(obj.edges)
    except Exception:
        return []


def _delete_if_present(module: Any, getter: str, deleter: str, names: list[str]) -> list[str]:
    deleted = []
    try:
        existing = set(getattr(module, getter)())
    except Exception:
        existing = set()
    for name in names:
        if name not in existing:
            continue
        try:
            getattr(module, deleter)([name])
            deleted.append(name)
        except Exception:
            pass
    return deleted


def _set_var(design: Any, name: str, value: str) -> None:
    design[name] = value


def _material_exists(m2d: Any, material: str) -> bool:
    try:
        return material.lower() in {name.lower() for name in m2d.materials.material_keys}
    except Exception:
        try:
            m2d.materials[material]
            return True
        except Exception:
            return False


def _load_core_loss_coefficients() -> dict[str, float]:
    defaults = dict(DEFAULT_CORE_LOSS_COEFFICIENTS)
    path = Path(__file__).resolve().parents[1] / "material" / "27PNF1500_core_loss_coefficients_W_m3.csv"
    if not path.exists():
        return defaults

    try:
        with path.open(newline="", encoding="utf-8-sig") as fp:
            rows = {row["coefficient"]: float(row["value"]) for row in csv.DictReader(fp)}
        defaults.update({key: rows[key] for key in defaults if key in rows})
    except Exception:
        pass
    return defaults


def get_core_loss_coefficients() -> dict[str, float]:
    """Return the Electrical Steel core-loss coefficients applied to the core material."""
    return _load_core_loss_coefficients()


def get_core_material_properties() -> dict[str, float]:
    """Return scalar core material properties applied to the custom steel material."""
    return {
        "resistivity_ohm_m": DEFAULT_CORE_RESISTIVITY_OHM_M,
        "conductivity_s_per_m": DEFAULT_CORE_CONDUCTIVITY_S_PER_M,
        "mass_density_kg_per_m3": DEFAULT_CORE_MASS_DENSITY_KG_PER_M3,
    }


def _set_electrical_steel_coreloss_with_y(mat: Any, coeff: dict[str, float], cut_depth: str = "0.3mm") -> None:
    """Set Electrical Steel core-loss coefficients, including AEDT's Y field."""
    mat.set_electrical_steel_coreloss(
        kh=coeff["Kh"],
        kc=coeff["Kc"],
        ke=coeff["Ke"],
        kdc=coeff["Kdc"],
        cut_depth=cut_depth,
    )
    # PyAEDT 0.22 exposes Kh/Kc/Ke/Kdc only, but AEDT stores the UI "Y" row
    # as core_loss_y in the material property dictionary.
    mat._props["core_loss_y"] = str(coeff["Y"])
    mat.update()


def _apply_core_material_properties(mat: Any, coeff: dict[str, float]) -> None:
    mat.permeability = "1000"
    mat.conductivity = f"{DEFAULT_CORE_CONDUCTIVITY_S_PER_M:.12g}"
    mat.mass_density = f"{DEFAULT_CORE_MASS_DENSITY_KG_PER_M3:.12g}"
    try:
        mat.set_magnetic_coercivity(value=0, x=1, y=0, z=0)
    except Exception:
        pass
    _set_electrical_steel_coreloss_with_y(mat, coeff)


def ensure_ppt_materials(design: Any, spec: IPMSMPPTSpec) -> dict[str, Any]:
    """Create the deck materials when they are absent from the AEDT project."""
    m2d = _m2d(design)
    result: dict[str, Any] = {}
    coeff = _load_core_loss_coefficients()

    if _material_exists(m2d, spec.core_material):
        result[spec.core_material] = "exists"
        try:
            mat = m2d.materials[spec.core_material]
            _apply_core_material_properties(mat, coeff)
            result[f"{spec.core_material}_coreloss"] = coeff
            result[f"{spec.core_material}_properties"] = get_core_material_properties()
        except Exception as exc:
            result[f"{spec.core_material}_coreloss"] = f"skipped: {exc}"
    else:
        try:
            mat = m2d.materials.add_material(spec.core_material)
            try:
                _apply_core_material_properties(mat, coeff)
                result[f"{spec.core_material}_coreloss"] = coeff
                result[f"{spec.core_material}_properties"] = get_core_material_properties()
            except Exception as exc:
                result[f"{spec.core_material}_coreloss"] = f"skipped: {exc}"
            mat.update()
            result[spec.core_material] = "created"
        except Exception as exc:
            result[spec.core_material] = f"skipped: {exc}"

    if _material_exists(m2d, spec.magnet_material):
        result[spec.magnet_material] = "exists"
    else:
        try:
            mat = m2d.materials.add_material(spec.magnet_material)
            mat.permeability = "1.05"
            mat.conductivity = "909090"
            mat.mass_density = "7500"
            # Br=1.25T -> Hc ~= Br / mu0 / mur.
            mat.set_magnetic_coercivity(value=947000, x=1, y=0, z=0)
            mat.update()
            result[spec.magnet_material] = "created"
        except Exception as exc:
            result[spec.magnet_material] = f"skipped: {exc}"

    return result


def apply_ppt_design_variables(design: Any, spec: IPMSMPPTSpec) -> None:
    """Apply the numeric parameters stated in the practice deck."""
    pole_expr = "pole_num" if _has_design_variable(design, "pole_num") else str(spec.pole_number)
    slot_expr = "slot_num" if _has_design_variable(design, "slot_num") else str(spec.slot_number)
    initial_wrapped_deg = spec.initial_position_deg % 360.0
    _set_var(design, "NumPoles", pole_expr)
    _set_var(design, "NumSlots", slot_expr)
    _set_var(design, "SymmetryFactor", str(spec.symmetry_factor))
    _set_var(design, "BaseRPM", f"{spec.base_rpm:g}")
    _set_var(design, "MachineRPM", f"{spec.base_rpm:g}rpm")
    _set_var(design, "Imax", f"{spec.i_peak_a:g}A")
    _set_var(design, "Beta", f"{spec.beta_deg:g}deg")
    _set_var(design, "StackLength", f"{spec.stack_length_mm:g}mm")
    _set_var(design, "R_phase", f"{spec.phase_resistance_ohm:g}ohm")
    _set_var(design, "Vdc", f"{spec.vdc_v:g}V")
    _set_var(design, "InitialPositionMD", f"{spec.initial_position_deg:g}deg")
    _set_var(design, "InitialPositionWrapped", f"{initial_wrapped_deg:g}deg")
    _set_var(design, "frq", "BaseRPM*NumPoles/120*1Hz")
    _set_var(design, "ElectricFrequency", "frq")
    total_steps = spec.steps_per_period * spec.transient_periods
    _set_var(design, "TransientPeriods", str(spec.transient_periods))
    _set_var(design, "StepsPerPeriod", str(spec.steps_per_period))
    _set_var(design, "StopTime", "TransientPeriods/frq")
    _set_var(design, "TimeStep", f"StopTime/{total_steps}")
    _set_var(design, "MotionTravelAngle", "TransientPeriods*720deg/NumPoles")
    _set_var(design, "NegativeMotionStop", "InitialPositionWrapped - 360deg")
    _set_var(design, "PositiveMotionStop", "InitialPositionWrapped + MotionTravelAngle + 360deg")


def clear_previous_ppt_setup(design: Any, spec: IPMSMPPTSpec) -> dict[str, Any]:
    """Best-effort cleanup so the notebook setup cell can be re-run."""
    m2d = _m2d(design)
    result: dict[str, Any] = {}

    boundary_names = [
        "VectorPotentialZero",
        "Matching",
        "PhaseA",
        "PhaseB",
        "PhaseC",
    ]
    for slot_index, (phase, polarity) in enumerate(DEFAULT_12S8P_SLOT_MAP, start=1):
        boundary_names.append(f"{phase}_{polarity}_{slot_index:02d}")
        for piece_index in range(1, 5):
            boundary_names.append(f"{phase}_{polarity}_{slot_index:02d}_{piece_index:02d}")

    try:
        module = m2d.odesign.GetModule("BoundarySetup")
        existing = set()
        for getter in ("GetBoundaries", "GetExcitations"):
            try:
                existing.update(getattr(module, getter)())
            except Exception:
                pass
        deleted = []
        for name in boundary_names:
            if name not in existing:
                continue
            try:
                module.DeleteBoundaries([name])
                deleted.append(name)
            except Exception:
                pass
        result["boundaries"] = deleted
    except Exception as exc:
        result["boundaries"] = f"skipped: {exc}"

    try:
        module = m2d.odesign.GetModule("MeshSetup")
        existing = set()
        for mesh_type in ("Length Based", "Skin Depth Based", "Surface Approximation Based"):
            try:
                existing.update(module.GetOperationNames(mesh_type))
            except Exception:
                pass
        deleted = []
        for name in ["Mesh_magnet", "Mesh_rotor", "Mesh_stator", "Mesh_winding", "Mesh_band"]:
            if name not in existing:
                continue
            try:
                module.DeleteOp([name])
                deleted.append(name)
            except Exception:
                pass
        result["mesh"] = deleted
    except Exception as exc:
        result["mesh"] = f"skipped: {exc}"

    try:
        module = m2d.odesign.GetModule("ModelSetup")
        result["motion"] = _delete_if_present(module, "GetMotionSetupNames", "DeleteMotionSetup", ["MotionSetup1"])
    except Exception as exc:
        result["motion"] = f"skipped: {exc}"

    try:
        if spec.setup_name in getattr(m2d, "setup_names", []):
            m2d.delete_setup(spec.setup_name)
            result["setup"] = [spec.setup_name]
        else:
            result["setup"] = []
    except Exception as exc:
        result["setup"] = f"skipped: {exc}"

    try:
        module = m2d.odesign.GetModule("ReportSetup")
        try:
            existing = set(module.GetAllReportNames())
        except Exception:
            existing = set()
        deleted = []
        for name in ["PPT_Phase_Currents", "PPT_Torque", "PPT_PhaseA_Voltage_Limit", "PPT_Losses"]:
            if name not in existing:
                continue
            try:
                module.DeleteReports([name])
                deleted.append(name)
            except Exception:
                pass
        result["reports"] = deleted
    except Exception as exc:
        result["reports"] = f"skipped: {exc}"

    return result


def set_operation_current(design: Any, spec: IPMSMPPTSpec, operation: str = "sin_current") -> None:
    """Switch between no-load and sinusoidal-current operation."""
    if operation.lower().replace("_", "") in {"noload", "backemf", "cogging"}:
        _set_var(design, "Imax", "0A")
    else:
        _set_var(design, "Imax", f"{spec.i_peak_a:g}A")


def ensure_region_and_band(
    design: Any,
    object_groups: dict[str, Any] | None,
    create_missing_region: bool = True,
    create_missing_band: bool = True,
) -> dict[str, list[str]]:
    """Reuse Region/Band if present; otherwise create simple circular sheets."""
    m2d = _m2d(design)
    region = _get_group(m2d, object_groups, "region", ("Region",))
    band = _get_group(m2d, object_groups, "band", ("Band",))

    if not region and create_missing_region:
        obj = m2d.modeler.create_circle(
            origin=[0, 0, 0],
            radius="stator_outer_radius*1.2",
            num_sides=144,
            name="Region",
            material="air",
        )
        region = [_name(obj)]

    if not band and create_missing_band:
        obj = m2d.modeler.create_circle(
            origin=[0, 0, 0],
            radius="rotor_radius + rotator_gap/2",
            num_sides=144,
            name="Band",
            material="air",
        )
        band = [_name(obj)]

    return {"region": region, "band": band}


def _assign_material(
    m2d: Any,
    objects: list[str],
    material: str,
    solve_inside: bool | None = None,
) -> list[str]:
    assigned = []
    for obj_name in objects:
        try:
            obj = m2d.modeler.get_object_from_name(obj_name)
            obj.material_name = material
            if solve_inside is not None:
                obj.solve_inside = solve_inside
            assigned.append(obj_name)
        except Exception:
            pass
    return assigned


def assign_ppt_materials(design: Any, object_groups: dict[str, Any] | None, spec: IPMSMPPTSpec) -> dict[str, list[str]]:
    """Assign deck materials to already-created geometry."""
    m2d = _m2d(design)
    stator = _get_group(m2d, object_groups, "stator", ("stator", "main_stator"))
    rotor = _get_group(m2d, object_groups, "rotor", ("rotator", "rotor"))
    shaft = _get_group(m2d, object_groups, "shaft", ("shaft",))
    magnets = _get_group(m2d, object_groups, "magnets", ("magnet",))
    windings = _get_group(m2d, object_groups, "windings", ("winding",))
    regions = _get_group(m2d, object_groups, "region", ("Region",))
    bands = _get_group(m2d, object_groups, "band", ("Band",))

    core_material_candidates = (spec.core_material,) + spec.core_material_fallbacks
    core_assigned = []
    selected_core_material = core_material_candidates[0]
    for material in core_material_candidates:
        core_assigned = _assign_material(m2d, stator + rotor, material)
        if core_assigned:
            selected_core_material = material
            break

    return {
        "core_material": [selected_core_material],
        "stator_rotor": core_assigned,
        "shaft": _assign_material(m2d, shaft, spec.shaft_material),
        "magnets": _assign_material(m2d, magnets, spec.magnet_material),
        "windings": _assign_material(m2d, windings, spec.winding_material, solve_inside=True),
        "air": _assign_material(m2d, regions + bands, spec.air_material),
    }


def resolve_geometry_overlaps(design: Any, object_groups: dict[str, Any] | None) -> dict[str, Any]:
    """Cut winding and magnet pockets out of the iron bodies before solving."""
    m2d = _m2d(design)
    stator = _get_group(m2d, object_groups, "stator", ("stator", "main_stator"))
    rotor = _get_group(m2d, object_groups, "rotor", ("rotator", "rotor"))
    windings = _get_group(m2d, object_groups, "windings", ("winding",))
    magnets = _get_group(m2d, object_groups, "magnets", ("magnet",))

    result: dict[str, Any] = {}
    if stator and windings:
        try:
            stator_result = _subtract_with_temporary_tools(
                m2d,
                stator,
                windings,
                temporary_prefix="BooleanTool_Winding_",
            )
            result["stator_minus_windings"] = stator_result.get("subtract")
            result["temporary_winding_tools"] = stator_result.get("temporary_tools", [])
            result["deleted_winding_tool_copies"] = stator_result.get("deleted_leftover_tools", [])
        except Exception as exc:
            result["stator_minus_windings"] = f"skipped: {exc}"
    else:
        result["stator_minus_windings"] = "skipped: no stator or windings"

    if rotor and magnets:
        try:
            rotor_result = _subtract_with_temporary_tools(
                m2d,
                rotor,
                magnets,
                temporary_prefix="BooleanTool_Magnet_",
            )
            result["rotor_minus_magnets"] = rotor_result.get("subtract")
            result["temporary_magnet_tools"] = rotor_result.get("temporary_tools", [])
            result["deleted_magnet_tool_copies"] = rotor_result.get("deleted_leftover_tools", [])
        except Exception as exc:
            result["rotor_minus_magnets"] = f"skipped: {exc}"
    else:
        result["rotor_minus_magnets"] = "skipped: no rotor or magnets"

    return result


def assign_magnet_coordinate_systems(
    design: Any,
    object_groups: dict[str, Any] | None,
    spec: IPMSMPPTSpec,
) -> dict[str, Any]:
    """Create radial local coordinate systems for magnet objects."""
    m2d = _m2d(design)
    magnets = _get_group(m2d, object_groups, "magnets", ("magnet",))
    explicit = _names(object_groups.get("magnets", [])) if object_groups else []
    south_candidates = set()
    has_named_polarity = any(
        re.search(r"(^|[_-])[ns]($|[_-])", name.lower()) is not None
        for name in explicit
    )
    if len(explicit) == 2 and not has_named_polarity:
        south_candidates.add(explicit[1])

    try:
        existing_cs = {cs.name for cs in m2d.modeler.coordinate_systems}
    except Exception:
        existing_cs = set()

    result: dict[str, Any] = {}
    for index, magnet in enumerate(magnets, start=1):
        obj = _get_object(m2d, magnet)
        if not obj:
            result[magnet] = "skipped: object not found"
            continue
        center = _object_center_xy(obj)
        if center is None:
            result[magnet] = "skipped: center unavailable"
            continue

        cx, cy = center
        angle = math.atan2(cy, cx)
        if abs(cx) < 1e-12 and abs(cy) < 1e-12:
            angle = 0.0

        is_south = magnet in south_candidates or re.search(r"(^|[_-])s($|[_-])", magnet.lower()) is not None
        sign = -1.0 if is_south else 1.0
        x_direction = [sign * math.cos(angle), sign * math.sin(angle), 0]
        y_direction = [-sign * math.sin(angle), sign * math.cos(angle), 0]
        cs_name = f"{magnet}_PM_CS"

        try:
            if cs_name not in existing_cs:
                m2d.modeler.create_coordinate_system(
                    origin=[cx, cy, 0],
                    reference_cs="Global",
                    name=cs_name,
                    mode="axis",
                    x_pointing=x_direction,
                    y_pointing=y_direction,
                )
                existing_cs.add(cs_name)
            obj.part_coordinate_system = cs_name
            result[magnet] = {
                "coordinate_system": cs_name,
                "polarity": "S" if is_south else "N",
                "x_direction_xy": x_direction[:2],
                "y_direction_xy": y_direction[:2],
            }
        except Exception as exc:
            result[magnet] = f"skipped: {exc}"

    return result


def assign_boundaries_and_motion(
    design: Any,
    object_groups: dict[str, Any] | None,
    spec: IPMSMPPTSpec,
    use_periodic_boundary: bool = False,
) -> dict[str, Any]:
    """Assign outer vector potential, optional periodic boundaries, and band motion."""
    m2d = _m2d(design)
    region = _get_group(m2d, object_groups, "region", ("Region",))
    band = _get_group(m2d, object_groups, "band", ("Band",))
    result: dict[str, Any] = {"vector_potential": None, "periodic": None, "motion": None}

    if region:
        try:
            region_edges = _object_edges(m2d, region[0])
            if not region_edges:
                region_edges = [
                    m2d.modeler.get_edgeid_from_position(
                        position=["stator_outer_radius*1.2", 0, 0],
                        assignment=region[0],
                    )
                ]
            result["vector_potential"] = m2d.assign_vector_potential(
                assignment=region_edges,
                vector_value=0,
                boundary="VectorPotentialZero",
            )
        except Exception as exc:
            result["vector_potential"] = f"skipped: {exc}"

    if use_periodic_boundary and region and spec.symmetry_factor > 1:
        try:
            radius = "rotor_radius + rotator_gap/2"
            master_edge = m2d.modeler.get_edgeid_from_position(
                position=[radius, 0, 0],
                assignment=region[0],
            )
            slave_edge = m2d.modeler.get_edgeid_from_position(
                position=[
                    f"{radius}*cos(360deg/SymmetryFactor)",
                    f"{radius}*sin(360deg/SymmetryFactor)",
                    0,
                ],
                assignment=region[0],
            )
            result["periodic"] = m2d.assign_master_slave(
                independent=master_edge,
                dependent=slave_edge,
                reverse_master=False,
                reverse_slave=True,
                same_as_master=False,
                boundary="Matching",
            )
        except Exception as exc:
            result["periodic"] = f"skipped: {exc}"

    if band:
        try:
            result["motion"] = m2d.assign_rotate_motion(
                assignment=band[0],
                coordinate_system="Global",
                axis="Z",
                positive_movement=True,
                start_position="InitialPositionWrapped",
                has_rotation_limits=True,
                negative_limit="NegativeMotionStop",
                positive_limit="PositiveMotionStop",
                angular_velocity="MachineRPM",
            )
        except Exception as exc:
            result["motion"] = f"skipped: {exc}"

    return result


def _slot_groups(winding_names: list[str], slot_count: int) -> list[list[str]]:
    winding_names = sorted(winding_names, key=_natural_key)
    if len(winding_names) == slot_count:
        return [[name] for name in winding_names]
    if len(winding_names) % slot_count == 0:
        per_slot = len(winding_names) // slot_count
        return [winding_names[i * per_slot : (i + 1) * per_slot] for i in range(slot_count)]
    raise ValueError(f"Expected {slot_count} or a multiple of {slot_count} winding objects, got {len(winding_names)}")


def assign_three_phase_windings(
    design: Any,
    object_groups: dict[str, Any] | None,
    spec: IPMSMPPTSpec,
    slot_map: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Assign A/B/C current windings for the 12-slot/8-pole practice motor."""
    m2d = _m2d(design)
    winding_names = _get_group(m2d, object_groups, "windings", ("winding",))
    numbered_windings = [name for name in winding_names if re.search(r"\d+$", name)]
    if len(numbered_windings) >= spec.slot_number:
        winding_names = numbered_windings
    slot_map = slot_map or DEFAULT_12S8P_SLOT_MAP
    result: dict[str, Any] = {"errors": []}

    try:
        slot_groups = _slot_groups(winding_names, len(slot_map))
    except Exception as exc:
        return {"errors": [f"slot grouping failed: {exc}"], "objects": winding_names}

    turns = spec.turns_per_coil_side
    phase_current = {
        "PhaseA": f"{turns}*Imax*sin(2*pi*frq*time + Beta)",
        "PhaseB": f"{turns}*Imax*sin(2*pi*frq*time - 120deg + Beta)",
        "PhaseC": f"{turns}*Imax*sin(2*pi*frq*time - 240deg + Beta)",
    }
    phase_coils = {phase: [] for phase in phase_current}

    for slot_index, (objects, (phase, polarity)) in enumerate(zip(slot_groups, slot_map), start=1):
        for piece_index, obj_name in enumerate(objects, start=1):
            coil_name = f"{phase}_{polarity}_{slot_index:02d}_{piece_index:02d}"
            try:
                coil = m2d.assign_coil(
                    assignment=[obj_name],
                    conductors_number=1,
                    polarity=polarity,
                    name=coil_name,
                )
                if coil:
                    phase_coils[phase].append(coil.name)
                else:
                    result["errors"].append(f"{coil_name}: assign_coil returned False")
            except Exception as exc:
                result["errors"].append(f"{coil_name}: {exc}")

    for phase, current in phase_current.items():
        if not phase_coils[phase]:
            result["errors"].append(f"{phase}: skipped winding group because no coils were created")
            continue
        try:
            winding = m2d.assign_winding(
                assignment=None,
                winding_type="Current",
                is_solid=True,
                current=current,
                parallel_branches=1,
                name=phase,
            )
            if winding:
                m2d.add_winding_coils(assignment=phase, coils=phase_coils[phase])
            else:
                result["errors"].append(f"{phase}: assign_winding returned False")
        except Exception as exc:
            result["errors"].append(f"{phase}: {exc}")

    result.update(phase_coils)
    return result


def assign_losses(design: Any, object_groups: dict[str, Any] | None) -> dict[str, Any]:
    """Enable magnet eddy-current loss and stator/rotor core loss calculations."""
    m2d = _m2d(design)
    stator = _get_group(m2d, object_groups, "stator", ("stator", "main_stator"))
    rotor = _get_group(m2d, object_groups, "rotor", ("rotator", "rotor"))
    magnets = _get_group(m2d, object_groups, "magnets", ("magnet",))
    result: dict[str, Any] = {"magnet_current_zero": [], "core_losses": None}

    for magnet in magnets:
        try:
            result["magnet_current_zero"].append(
                m2d.assign_current(assignment=magnet, amplitude=0, solid=True, name=f"{magnet}_Jz0")
            )
        except Exception as exc:
            result["magnet_current_zero"].append(f"{magnet}: skipped {exc}")

    try:
        result["core_losses"] = m2d.set_core_losses(stator + rotor, core_loss_on_field=True)
    except Exception as exc:
        result["core_losses"] = f"skipped: {exc}"

    return result


def assign_mesh(design: Any, object_groups: dict[str, Any] | None, spec: IPMSMPPTSpec) -> dict[str, Any]:
    """Apply the mesh element counts stated in the deck."""
    m2d = _m2d(design)
    groups = {
        "magnet": _get_group(m2d, object_groups, "magnets", ("magnet",)),
        "rotor": _get_group(m2d, object_groups, "rotor", ("rotator", "rotor")),
        "stator": _get_group(m2d, object_groups, "stator", ("stator", "main_stator")),
        "winding": _get_group(m2d, object_groups, "windings", ("winding",)),
        "band": _get_group(m2d, object_groups, "band", ("Band",)),
    }
    result: dict[str, Any] = {}
    for group_name, objects in groups.items():
        if not objects:
            result[group_name] = "skipped: no objects"
            continue
        try:
            result[group_name] = m2d.mesh.assign_length_mesh(
                assignment=objects,
                inside_selection=True,
                maximum_length=None,
                maximum_elements=spec.mesh_elements[group_name],
                name=f"Mesh_{group_name}",
            )
        except Exception as exc:
            result[group_name] = f"skipped: {exc}"
    return result


def create_ppt_transient_setup(design: Any, spec: IPMSMPPTSpec) -> Any:
    """Create a transient setup with a fixed number of steps per electrical period."""
    m2d = _m2d(design)
    try:
        m2d.model_depth = "StackLength"
    except Exception:
        pass
    try:
        m2d.change_symmetry_multiplier("SymmetryFactor")
    except Exception:
        pass

    setup = m2d.create_setup(name=spec.setup_name)
    setup.props["StopTime"] = "StopTime"
    setup.props["TimeStep"] = "TimeStep"
    setup.props["SaveFieldsType"] = "Every N Steps"
    setup.props["N Steps"] = "1"
    setup.props["Steps From"] = "0s"
    setup.props["Steps To"] = "StopTime"
    setup.props["OutputPerObjectCoreLoss"] = True
    setup.props["OutputPerObjectSolidLoss"] = True
    setup.props["OutputError"] = True
    setup.update()
    return setup


def create_ppt_reports(design: Any, setup_name: str = "PPT_Transient") -> dict[str, Any]:
    """Create common reports from the deck: currents, torque, voltage, and losses."""
    m2d = _m2d(design)
    setup_sweep = f"{setup_name} : Transient"
    report_defs = {
        "PPT_Phase_Currents": ["InputCurrent(PhaseA)", "InputCurrent(PhaseB)", "InputCurrent(PhaseC)"],
        "PPT_Torque": ["Moving1.Torque"],
        "PPT_PhaseA_Voltage_Limit": ["mag(InducedVoltage(PhaseA)+R_phase*InputCurrent(PhaseA))"],
        "PPT_Losses": ["CoreLoss", "SolidLoss"],
    }
    reports: dict[str, Any] = {}
    for plot_name, expressions in report_defs.items():
        try:
            reports[plot_name] = m2d.post.create_report(
                expressions=expressions,
                setup_sweep_name=setup_sweep,
                domain="Sweep",
                primary_sweep_variable="Time",
                plot_name=plot_name,
            )
        except Exception as exc:
            reports[plot_name] = f"skipped: {exc}"
    return reports


def configure_ipmsm_from_ppt(
    design: Any,
    object_groups: dict[str, Any] | None = None,
    spec: IPMSMPPTSpec | None = None,
    operation: str = "sin_current",
    use_periodic_boundary: bool = False,
    create_missing_region: bool = True,
    create_missing_band: bool = True,
    create_reports: bool = True,
    clear_existing: bool = True,
    analyze: bool = False,
    cores: int = 4,
) -> dict[str, Any]:
    """Run the full post-modeling setup flow from the practice deck."""
    spec = spec or IPMSMPPTSpec()
    apply_ppt_design_variables(design, spec)
    set_operation_current(design, spec, operation=operation)
    cleanup = clear_previous_ppt_setup(design, spec) if clear_existing else "skipped"

    created = ensure_region_and_band(
        design,
        object_groups,
        create_missing_region=create_missing_region,
        create_missing_band=create_missing_band,
    )
    merged_groups = dict(object_groups or {})
    merged_groups.setdefault("region", created["region"])
    merged_groups.setdefault("band", created["band"])

    result = {
        "variables": "applied",
        "cleanup": cleanup,
        "region_band": created,
        "geometry_overlap_resolution": resolve_geometry_overlaps(design, merged_groups),
        "material_definitions": ensure_ppt_materials(design, spec),
        "materials": assign_ppt_materials(design, merged_groups, spec),
        "magnet_coordinate_systems": assign_magnet_coordinate_systems(design, merged_groups, spec),
        "boundaries_motion": assign_boundaries_and_motion(
            design,
            merged_groups,
            spec,
            use_periodic_boundary=use_periodic_boundary,
        ),
        "windings": assign_three_phase_windings(design, merged_groups, spec),
        "losses": assign_losses(design, merged_groups),
        "mesh": assign_mesh(design, merged_groups, spec),
        "setup": create_ppt_transient_setup(design, spec),
    }

    if create_reports:
        result["reports"] = create_ppt_reports(design, spec.setup_name)

    m2d = _m2d(design)
    try:
        result["validation"] = m2d.validate_simple()
    except Exception as exc:
        result["validation"] = f"skipped: {exc}"

    if analyze:
        result["analysis"] = m2d.analyze(setup=spec.setup_name, cores=cores, use_auto_settings=False)

    return result
