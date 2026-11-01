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
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable


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
DEFAULT_CORE_BH_CURVE_CSV = "30PNF1600_BH.csv"
LEGACY_CORE_MATERIAL_MODEL = "legacy_pnf_hybrid_v1"
M19_REFERENCE_CORE_MATERIAL_MODEL = "m19_29g_reference_y2_v1"
M19_REFERENCE_CORE_MATERIAL_NAME = "M19_29G_reference_y2"
M19_REFERENCE_PACK_SHA256 = "ad85048a90e1da6cfaa33d8f4fa9df9ffd5a58960b5aeedface5e1a34cd59a04"

MODEL_EXTENT_FULL_360 = "full_360"
MODEL_EXTENT_SECTOR_90 = "sector_90"
BETA_CONVENTION_DQ_CURRENT_ADVANCE_V2 = "dq_current_advance_v2"
BETA_CONVENTION_LEGACY_PHASE_OFFSET_V1 = "legacy_phase_offset_v1"
SUPPORTED_BETA_CONVENTIONS = (
    BETA_CONVENTION_DQ_CURRENT_ADVANCE_V2,
    BETA_CONVENTION_LEGACY_PHASE_OFFSET_V1,
)

PPT_REPORT_DEFS = {
    "PPT_Phase_Currents": ["InputCurrent(PhaseA)", "InputCurrent(PhaseB)", "InputCurrent(PhaseC)"],
    "PPT_Torque": ["Moving1.Torque"],
    "PPT_Cogging_Torque": ["Moving1.Torque"],
    "PPT_Back_EMF": ["InducedVoltage(PhaseA)", "InducedVoltage(PhaseB)", "InducedVoltage(PhaseC)"],
    "PPT_Inductance_Matrix": [
        "L(PhaseA,PhaseA)",
        "L(PhaseA,PhaseB)",
        "L(PhaseA,PhaseC)",
        "L(PhaseB,PhaseA)",
        "L(PhaseB,PhaseB)",
        "L(PhaseB,PhaseC)",
        "L(PhaseC,PhaseA)",
        "L(PhaseC,PhaseB)",
        "L(PhaseC,PhaseC)",
    ],
    "PPT_PhaseA_Voltage_Limit": ["mag(InducedVoltage(PhaseA)+R_phase*InputCurrent(PhaseA))"],
    "PPT_Phase_Voltages": [
        "InducedVoltage(PhaseA)+R_phase*InputCurrent(PhaseA)",
        "InducedVoltage(PhaseB)+R_phase*InputCurrent(PhaseB)",
        "InducedVoltage(PhaseC)+R_phase*InputCurrent(PhaseC)",
    ],
    "PPT_Losses": ["CoreLoss", "SolidLoss"],
    # Optional diagnostic only; it is not a physical acceptance criterion.
    "PPT_EnergyError": ["EnergyError"],
    "PPT_Flux_Linkages": ["FluxLinkage(PhaseA)", "FluxLinkage(PhaseB)", "FluxLinkage(PhaseC)"],
}


@dataclass
class IPMSMPPTSpec:
    pole_number: int = 8
    slot_number: int = 12
    model_extent: str = MODEL_EXTENT_FULL_360
    symmetry_factor: int = 1
    base_rpm: float = 1200.0
    i_peak_a: float = 137.8
    beta_deg: float = 30.0
    beta_convention: str = BETA_CONVENTION_DQ_CURRENT_ADVANCE_V2
    electrical_zero_deg: float = 0.0
    series_turns_per_phase: int = 48
    turns_per_coil_side: int = 12
    stack_length_mm: float = 49.45
    phase_resistance_ohm: float = 0.01
    vdc_v: float = 200.0
    initial_position_deg: float = -22.5
    transient_periods: int = 10
    steps_per_period: int = 90
    core_material: str = DEFAULT_CORE_MATERIAL_NAME
    core_material_model: str = LEGACY_CORE_MATERIAL_MODEL
    core_material_fallbacks: tuple[str, ...] = ("30PNF1600", "steel_1008 - Copy", "steel_1008")
    magnet_material: str = "NdFeB_1.25T"
    winding_material: str = "copper"
    shaft_material: str = "vacuum"
    air_material: str = "air"
    setup_name: str = "PPT_Transient"
    analysis_purpose: str = "training"
    compute_inductance: bool = True
    solver_output_error: bool = True
    solver_nonlinear_residual: float = 0.005
    save_fields_start_step: int | None = None
    save_fields_stop_step: int | None = None
    save_fields_every_n_steps: int | None = None
    strict_setup: bool = True
    require_steady_state: bool = False
    circle_segments: int = 0
    band_circle_segments: int = 144
    loss_policy: str = "laminated_core"
    conductor_area_per_branch_mm2: float = 15.3206469
    slot_fill_limit: float = 0.45
    mesh_maximum_lengths_mm: dict[str, float] = field(default_factory=dict)
    mesh_refinement_modes: dict[str, str] = field(default_factory=dict)
    mesh_elements: dict[str, int] = field(
        default_factory=lambda: {
            "magnet": 50,
            "rotor": 500,
            "stator": 500,
            "winding": 50,
            "band": 1000,
        }
    )


def field_save_schedule(spec: IPMSMPPTSpec) -> dict[str, Any] | None:
    """Validate an optional final-cycle solver-grid schedule, not Native retention."""
    values = (spec.save_fields_start_step, spec.save_fields_stop_step, spec.save_fields_every_n_steps)
    if all(value is None for value in values):
        return None
    if any(type(value) is not int for value in values):
        raise ValueError("save_fields start/stop/every_n_steps must all be integers, not bool or missing")
    start, stop, count = values
    periods, steps = spec.transient_periods, spec.steps_per_period
    if type(periods) is not int or periods <= 0 or type(steps) is not int or steps <= 0:
        raise ValueError("save_fields requires positive integer periods and steps_per_period")
    if start < 0 or stop <= start or stop > periods * steps:
        raise ValueError("save_fields step bounds are outside the solver grid")
    if start != (periods - 1) * steps or stop != periods * steps:
        raise ValueError("save_fields bounds must cover exactly the final electrical period")
    if count <= 0 or count > steps or steps % count:
        raise ValueError("save_fields every_n_steps must positively and evenly divide steps_per_period")
    if type(spec.base_rpm) not in (int, float) or type(spec.pole_number) is not int or spec.pole_number <= 0:
        raise ValueError("save_fields requires positive finite rpm and positive integer pole_number")
    try:
        # Match apply_ppt_design_variables' actual commanded BaseRPM, not an unrounded input.
        rpm = float(f"{spec.base_rpm:g}")
        start_s = (periods - 1) * 120 / (rpm * spec.pole_number)
        stop_s = periods * 120 / (rpm * spec.pole_number)
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        raise ValueError("save_fields time bounds must be finite and positive") from exc
    if (not math.isfinite(rpm) or rpm <= 0 or not math.isfinite(start_s) or not math.isfinite(stop_s)
            or start_s < 0 or stop_s <= start_s):
        raise ValueError("save_fields time bounds must be finite and positive")
    return {"range_type": "Every N Steps", "start_step": start, "stop_step": stop,
            "every_n_steps": count, "start_time_s": start_s, "stop_time_s": stop_s,
            "expected_endpoint_snapshots": 1 + steps // count, "units": "s",
            "Native_endpoint_inclusion_verified": False}


def canonical_dq_current_components(i_peak_a: float, beta_dq_deg: float) -> tuple[float, float]:
    """Return ``(Id, Iq)`` for the v2 current-advance convention.

    Positive beta advances the current into negative d-axis current. Therefore
    beta=0 has Id=0 and Iq=+Ipeak, which is the canonical convention used by
    new datasets and optimization code.
    """
    peak = float(i_peak_a)
    beta = float(beta_dq_deg)
    if not math.isfinite(peak) or peak < 0.0:
        raise ValueError("i_peak_a must be finite and >= 0")
    if not math.isfinite(beta):
        raise ValueError("beta_dq_deg must be finite")
    beta_rad = math.radians(beta)
    return -peak * math.sin(beta_rad), peak * math.cos(beta_rad)


def inverse_park_phase_currents(
    id_a: float,
    iq_a: float,
    electrical_angle_deg: float,
) -> dict[str, float]:
    """Convert dq currents to balanced PhaseA/B/C instantaneous currents."""
    values = (float(id_a), float(iq_a), float(electrical_angle_deg))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Id, Iq, and electrical angle must be finite")
    id_value, iq_value, angle_deg = values
    phase_axes_deg = {"PhaseA": 0.0, "PhaseB": -120.0, "PhaseC": 120.0}
    return {
        phase: id_value * math.cos(math.radians(angle_deg + axis_deg))
        - iq_value * math.sin(math.radians(angle_deg + axis_deg))
        for phase, axis_deg in phase_axes_deg.items()
    }


def canonical_phase_currents(
    i_peak_a: float,
    beta_dq_deg: float,
    electrical_angle_deg: float,
    electrical_zero_deg: float = 0.0,
) -> dict[str, float]:
    """Evaluate the canonical v2 three-phase excitation at one angle."""
    id_a, iq_a = canonical_dq_current_components(i_peak_a, beta_dq_deg)
    return inverse_park_phase_currents(id_a, iq_a, electrical_angle_deg + electrical_zero_deg)


def commanded_dq_current_components(spec: IPMSMPPTSpec) -> tuple[float, float]:
    """Return dq components represented by the selected excitation convention."""
    if spec.beta_convention == BETA_CONVENTION_DQ_CURRENT_ADVANCE_V2:
        return canonical_dq_current_components(spec.i_peak_a, spec.beta_deg)
    if spec.beta_convention == BETA_CONVENTION_LEGACY_PHASE_OFFSET_V1:
        # Legacy Ia=+I*sin(theta+beta) is the sign-reversed q-axis form under
        # the v2 Park transform. Keep it available only by its explicit name.
        legacy_id, legacy_iq = canonical_dq_current_components(spec.i_peak_a, spec.beta_deg)
        return -legacy_id, -legacy_iq
    raise ValueError(f"unsupported beta_convention: {spec.beta_convention!r}")


def phase_current_expressions(spec: IPMSMPPTSpec) -> dict[str, str]:
    """Return AEDT winding expressions for the explicitly selected convention."""
    if spec.beta_convention == BETA_CONVENTION_DQ_CURRENT_ADVANCE_V2:
        angle = "2*pi*frq*time + ElectricalZero"
        return {
            "PhaseA": f"IdCommand*cos({angle}) - IqCommand*sin({angle})",
            "PhaseB": f"IdCommand*cos({angle} - 120deg) - IqCommand*sin({angle} - 120deg)",
            "PhaseC": f"IdCommand*cos({angle} + 120deg) - IqCommand*sin({angle} + 120deg)",
        }
    if spec.beta_convention == BETA_CONVENTION_LEGACY_PHASE_OFFSET_V1:
        return {
            "PhaseA": "Imax*sin(2*pi*frq*time + Beta)",
            "PhaseB": "Imax*sin(2*pi*frq*time - 120deg + Beta)",
            "PhaseC": "Imax*sin(2*pi*frq*time - 240deg + Beta)",
        }
    raise ValueError(f"unsupported beta_convention: {spec.beta_convention!r}")


def validate_ppt_spec_contract(
    spec: IPMSMPPTSpec,
    use_periodic_boundary: bool = False,
) -> None:
    """Fail closed on model-extent, symmetry, periodic, and beta mismatches."""
    _validate_core_material_selection(spec)
    if type(spec.solver_output_error) is not bool:
        raise ValueError("solver_output_error must be a boolean")
    residual = spec.solver_nonlinear_residual
    if type(residual) not in (int, float):
        raise ValueError("solver_nonlinear_residual must be finite and positive")
    try:
        residual = float(residual)
    except OverflowError as exc:
        raise ValueError("solver_nonlinear_residual must be finite and positive") from exc
    if not math.isfinite(residual) or residual <= 0:
        raise ValueError("solver_nonlinear_residual must be finite and positive")
    field_save_schedule(spec)
    for name, value in (("conductor_area_per_branch_mm2", spec.conductor_area_per_branch_mm2),
                        ("slot_fill_limit", spec.slot_fill_limit)):
        if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if spec.slot_fill_limit > 1:
        raise ValueError("slot_fill_limit must be <=1")
    if spec.model_extent not in {MODEL_EXTENT_FULL_360, MODEL_EXTENT_SECTOR_90}:
        raise ValueError(
            f"model_extent must be {MODEL_EXTENT_FULL_360!r} or {MODEL_EXTENT_SECTOR_90!r}"
        )
    if spec.model_extent == MODEL_EXTENT_SECTOR_90:
        raise ValueError(
            "model_extent='sector_90' is unavailable until a real sector geometry builder exists"
        )
    if int(spec.symmetry_factor) != 1:
        raise ValueError("model_extent='full_360' requires symmetry_factor=1")
    if use_periodic_boundary:
        raise ValueError("periodic boundary is invalid for model_extent='full_360'")
    if spec.beta_convention not in SUPPORTED_BETA_CONVENTIONS:
        raise ValueError(
            "beta_convention must be explicitly set to one of "
            + ", ".join(repr(value) for value in SUPPORTED_BETA_CONVENTIONS)
        )
    canonical_dq_current_components(spec.i_peak_a, spec.beta_deg)
    if not math.isfinite(float(spec.electrical_zero_deg)):
        raise ValueError("electrical_zero_deg must be finite")
    if spec.analysis_purpose not in {"screening", "training", "reference"}:
        raise ValueError("analysis_purpose must be screening, training, or reference")
    if not spec.compute_inductance and spec.analysis_purpose != "screening":
        raise ValueError("only screening may omit inductance; training requires Ld/Lq")
    if spec.circle_segments != 0 and spec.circle_segments < 12:
        raise ValueError("circle_segments must be 0 (exact arcs) or at least 12")
    if spec.band_circle_segments != 0 and spec.band_circle_segments < 12:
        raise ValueError("band_circle_segments must be 0 (exact arcs) or at least 12")
    if spec.loss_policy not in {"laminated_core", "legacy_implicit"}:
        raise ValueError("unsupported loss_policy")
    if not isinstance(spec.mesh_maximum_lengths_mm, dict):
        raise ValueError("mesh_maximum_lengths_mm must be a group/length mapping")
    if not isinstance(spec.mesh_refinement_modes, dict):
        raise ValueError("mesh_refinement_modes must be a group/mode mapping")
    for group, mode in spec.mesh_refinement_modes.items():
        if group not in {"magnet", "rotor", "stator", "winding", "band"} or mode != "length_only":
            raise ValueError(f"unsupported mesh refinement group/mode: {group}={mode!r}")
        if group not in spec.mesh_maximum_lengths_mm:
            raise ValueError(f"length_only requires a positive mesh length: {group}")
    for group, length in spec.mesh_maximum_lengths_mm.items():
        if group not in spec.mesh_elements or type(length) not in (int, float) or not math.isfinite(length) or length <= 0:
            raise ValueError(f"invalid local mesh length: {group}={length!r}")


DEFAULT_12S8P_SECTOR_COIL_SIDE_MAP = [
    ("PhaseA", "Negative"),
    ("PhaseB", "Positive"),
    ("PhaseB", "Negative"),
    ("PhaseC", "Positive"),
    ("PhaseC", "Negative"),
    ("PhaseA", "Positive"),
]
DEFAULT_12S8P_COIL_SIDE_MAP = DEFAULT_12S8P_SECTOR_COIL_SIDE_MAP * 4
LEGACY_12S8P_SLOT_MAP = [
    ("PhaseA", "Positive"),
    ("PhaseC", "Negative"),
    ("PhaseB", "Positive"),
    ("PhaseA", "Negative"),
    ("PhaseC", "Positive"),
    ("PhaseB", "Negative"),
] * 2


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


def _material_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "material"


def core_material_name_for_model(model_id: str) -> str:
    """Resolve an explicit material model without relabelling another grade."""
    names = {LEGACY_CORE_MATERIAL_MODEL: DEFAULT_CORE_MATERIAL_NAME,
             M19_REFERENCE_CORE_MATERIAL_MODEL: M19_REFERENCE_CORE_MATERIAL_NAME}
    if model_id not in names:
        raise ValueError(f"unsupported core_material_model: {model_id!r}")
    return names[model_id]


def _validate_core_material_selection(spec: IPMSMPPTSpec) -> None:
    canonical = core_material_name_for_model(spec.core_material_model)
    if not isinstance(spec.core_material, str) or not spec.core_material.strip():
        raise ValueError("core_material must be a nonempty name")
    if spec.core_material_model == M19_REFERENCE_CORE_MATERIAL_MODEL:
        if spec.core_material != canonical:
            raise ValueError("reference core material name/model mismatch")
    elif spec.core_material.casefold() == M19_REFERENCE_CORE_MATERIAL_NAME.casefold():
        raise ValueError("the M19 reference name requires its explicit reference model")


def _reference_core_pack_path() -> Path:
    return Path(__file__).resolve().parents[1] / "configs" / "material_m19_29g_reference_y2.json"


def load_reference_core_material_pack() -> dict[str, Any]:
    """Load the pinned original-point reference, not an inferred PNF replacement."""
    # Git may check JSON out with CRLF on Windows. Canonical UTF8/LF identity is
    # the same across accounts and C/Linux freezes; JSON content changes fail.
    raw = _reference_core_pack_path().read_bytes().decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != M19_REFERENCE_PACK_SHA256:
        raise ValueError("M19 reference material pack SHA256 mismatch")
    pack = json.loads(raw)
    if (pack["schema_version"] != "ipmsm_core_material_reference_v1"
            or pack["model_id"] != M19_REFERENCE_CORE_MATERIAL_MODEL
            or pack["solver_material_name"] != M19_REFERENCE_CORE_MATERIAL_NAME):
        raise ValueError("M19 reference material pack identity mismatch")
    points = pack["bh"]["points_H_B"]
    if len(points) != 23 or points[0] != [0.0, 0.0]:
        raise ValueError("M19 reference requires the23 original BH points")
    for index, point in enumerate(points):
        if len(point) != 2 or any(isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in point):
            raise ValueError("invalid M19 reference BH point")
        if index and (point[0] <= points[index - 1][0] or point[1] <= points[index - 1][1]):
            raise ValueError("M19 reference BH points must be strictly increasing")
    digest = hashlib.sha256(json.dumps(points, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if digest != pack["bh"]["points_sha256"]:
        raise ValueError("M19 reference BH point digest mismatch")
    return pack


def get_core_loss_coefficients(model_id: str = LEGACY_CORE_MATERIAL_MODEL) -> dict[str, float]:
    """Return the Electrical Steel core-loss coefficients applied to the core material."""
    core_material_name_for_model(model_id)
    if model_id == M19_REFERENCE_CORE_MATERIAL_MODEL:
        loss = load_reference_core_material_pack()["core_loss"]
        return {key: float(loss[key]) for key in ("Kh", "Kc", "Ke", "Y", "Kdc")}
    return _load_core_loss_coefficients()


def _load_core_bh_curve() -> list[list[float]]:
    """Load the nonlinear B-H curve as AEDT ``[[H_A_per_m, B_tesla], ...]`` pairs."""
    path = _material_dir() / DEFAULT_CORE_BH_CURVE_CSV
    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8-sig") as fp:
        reader = csv.DictReader(fp)
        headers = reader.fieldnames or []
        b_column = next((header for header in headers if header.lower().startswith("b")), None)
        h_column = next((header for header in headers if header.lower().startswith("h")), None)
        if not b_column or not h_column:
            raise ValueError(f"BH curve CSV must have B and H columns: {path}")

        points: list[list[float]] = []
        for line_number, row in enumerate(reader, start=2):
            try:
                b_value = float(row[b_column])
                h_value = float(row[h_column])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid BH curve value at {path}:{line_number}") from exc
            if not (math.isfinite(b_value) and math.isfinite(h_value) and b_value >= 0.0 and h_value >= 0.0):
                raise ValueError(f"Nonfinite or negative BH curve value at {path}:{line_number}")
            points.append([h_value, b_value])

    points.sort(key=lambda point: (point[0], point[1]))
    unique_points: list[list[float]] = []
    seen_h: dict[float, float] = {}
    for h_value, b_value in points:
        rounded_h = round(h_value, 12)
        if rounded_h in seen_h:
            if seen_h[rounded_h] != b_value:
                raise ValueError(f"Conflicting B values for H={h_value}: {path}")
            continue
        seen_h[rounded_h] = b_value
        if unique_points and b_value < unique_points[-1][1]:
            raise ValueError(f"BH curve must be monotone: {path}")
        unique_points.append([h_value, b_value])

    if unique_points and unique_points[0][0] == 0.0 and unique_points[0][1] != 0.0:
        raise ValueError(f"Initial magnetization curve must have B=0 at H=0: {path}")
    if unique_points and (unique_points[0][0] > 0.0 or unique_points[0][1] > 0.0):
        unique_points.insert(0, [0.0, 0.0])
    if len(unique_points) < 3 or unique_points[-1][0] <= 0.0 or unique_points[-1][1] <= 0.0:
        raise ValueError(f"BH curve needs at least three valid points: {path}")
    return unique_points


def get_core_bh_curve_metadata(model_id: str = LEGACY_CORE_MATERIAL_MODEL) -> dict[str, Any]:
    """Identify every applied B-H point, including interior saturation data."""
    core_material_name_for_model(model_id)
    curve = (load_reference_core_material_pack()["bh"]["points_H_B"]
             if model_id == M19_REFERENCE_CORE_MATERIAL_MODEL else _load_core_bh_curve())
    if not curve:
        return {
            "bh_curve_points": 0,
            "bh_curve_bmax_t": math.nan,
            "bh_curve_hmax_a_per_m": math.nan,
            "bh_curve_sha256": "",
        }
    return {
        "bh_curve_points": len(curve),
        "bh_curve_bmax_t": max(point[1] for point in curve),
        "bh_curve_hmax_a_per_m": max(point[0] for point in curve),
        "bh_curve_sha256": hashlib.sha256(
            json.dumps(curve, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest(),
    }


def get_core_material_properties(model_id: str = LEGACY_CORE_MATERIAL_MODEL) -> dict[str, Any]:
    """Return scalar core material properties applied to the custom steel material."""
    core_material_name_for_model(model_id)
    if model_id == M19_REFERENCE_CORE_MATERIAL_MODEL:
        pack = load_reference_core_material_pack()
        properties = dict(pack["properties"])
        properties.update({"resistivity_ohm_m": 1.0 / properties["conductivity_s_per_m"],
                           "core_material_model": model_id,
                           "solver_material_name": pack["solver_material_name"],
                           "material_pack_sha256": M19_REFERENCE_PACK_SHA256,
                           # Static provenance is portable; never insert resolved runtime paths.
                           "material_definition": pack})
        properties.update(get_core_bh_curve_metadata(model_id))
        return properties
    properties = {
        "resistivity_ohm_m": DEFAULT_CORE_RESISTIVITY_OHM_M,
        "conductivity_s_per_m": DEFAULT_CORE_CONDUCTIVITY_S_PER_M,
        "mass_density_kg_per_m3": DEFAULT_CORE_MASS_DENSITY_KG_PER_M3,
    }
    properties.update(get_core_bh_curve_metadata())
    return properties


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


def _apply_core_bh_curve(mat: Any, *, strict: bool = False) -> dict[str, Any]:
    bh_curve = _load_core_bh_curve()
    if not bh_curve:
        if strict:
            raise ValueError("Strict simulation requires the nonlinear core B-H curve")
        mat.permeability = "1000"
        return {"source": "fallback_simple_permeability", "points": 0}

    mat.permeability = bh_curve
    # Explicit units keep the AEDT material editor aligned with the GUI table:
    # H in A/m and B in tesla.
    mat.permeability.set_non_linear(x_unit="A_per_meter", y_unit="tesla")
    return {
        "source": str(_material_dir() / DEFAULT_CORE_BH_CURVE_CSV),
        "points": len(bh_curve),
        "bmax_t": max(point[1] for point in bh_curve),
        "hmax_a_per_m": max(point[0] for point in bh_curve),
    }


def _apply_core_material_properties(
    mat: Any, coeff: dict[str, float], *, strict: bool = False
) -> dict[str, Any]:
    result = {"bh_curve": _apply_core_bh_curve(mat, strict=strict)}
    mat.conductivity = f"{DEFAULT_CORE_CONDUCTIVITY_S_PER_M:.12g}"
    mat.mass_density = f"{DEFAULT_CORE_MASS_DENSITY_KG_PER_M3:.12g}"
    try:
        mat.set_magnetic_coercivity(value=0, x=1, y=0, z=0)
    except Exception:
        pass
    _set_electrical_steel_coreloss_with_y(mat, coeff)
    return result


def _reference_native_material_payload(pack: dict[str, Any]) -> list[Any]:
    """Documented native Coordinate/X/Y syntax avoids the0.22 AMAT importer bug."""
    scalar = lambda value: format(float(value), ".15g")
    loss, props = pack["core_loss"], pack["properties"]
    coordinates = ["NAME:BHCoordinates"] + [
        ["NAME:Coordinate", "X:=", h, "Y:=", b] for h, b in pack["bh"]["points_H_B"]]
    return [
        "NAME:" + pack["solver_material_name"], "CoordinateSystemType:=", "Cartesian",
        "BulkOrSurfaceType:=", 1, ["NAME:PhysicsTypes", "set:=", ["Electromagnetic"]],
        ["NAME:permeability", "property_type:=", "nonlinear", "BTypeForSingleCurve:=", "Normal",
         "HUnit:=", pack["bh"]["H_unit"], "BUnit:=", pack["bh"]["B_unit"],
         "IsTemperatureDependent:=", False, "RecoilPermeability:=", "1", coordinates],
        "conductivity:=", scalar(props["conductivity_s_per_m"]),
        "mass_density:=", scalar(props["mass_density_kg_per_m3"]),
        ["NAME:magnetic_coercivity", "property_type:=", "VectorProperty", "Magnitude:=", "0A_per_meter",
         "DirComp1:=", "1", "DirComp2:=", "0", "DirComp3:=", "0"],
        ["NAME:core_loss_type", "property_type:=", "ChoiceProperty", "Choice:=", loss["model"]],
        "core_loss_kh:=", scalar(loss["Kh"]), "core_loss_kc:=", scalar(loss["Kc"]),
        "core_loss_ke:=", scalar(loss["Ke"]), "core_loss_y:=", scalar(loss["Y"]),
        "core_loss_kdc:=", scalar(loss["Kdc"]),
        "core_loss_equiv_cut_depth:=", scalar(loss["equivalent_cut_depth_m"]) + "meter",
        ["NAME:stacking_type", "property_type:=", "ChoiceProperty", "Choice:=", pack["composition"]["type"]],
    ]


def _native_material_properties(raw: Any, expected_name: str) -> dict[str, Any]:
    from ansys.aedt.core.generic.data_handlers import _arg2dict

    if isinstance(raw, bool) or raw is None:
        raise ValueError("native material GetData returned no payload")
    parsed: dict[str, Any] = {}
    _arg2dict(list(raw), parsed)
    if set(parsed) != {expected_name} or not isinstance(parsed[expected_name], dict):
        raise ValueError("native material GetData name differs from the requested reference")
    return parsed[expected_name]


def _material_readback_number(value: Any, unit: str = "") -> float:
    if isinstance(value, bool):
        raise ValueError("boolean material scalar is invalid")
    text = str(value).strip()
    if unit:
        match = re.fullmatch(r"([-+0-9.eE]+)\s*(meter|mm|m|A_per_meter)", text)
        if not match or (unit == "meter" and match[2] not in {"meter", "mm", "m"}) or (
                unit == "A_per_meter" and match[2] != unit):
            raise ValueError(f"unexpected material scalar unit: {value!r}")
        number = float(match[1]) * (0.001 if match[2] == "mm" else 1.0)
    else:
        number = float(text)
    if not math.isfinite(number):
        raise ValueError("nonfinite material readback")
    return number


def _verify_reference_material_readback(props: dict[str, Any], pack: dict[str, Any]) -> None:
    permeability = props["permeability"]
    if (permeability.get("property_type") != "nonlinear"
            or str(permeability.get("BTypeForSingleCurve", "")).casefold() != "normal"
            or str(permeability.get("HUnit", "")).casefold() != "a_per_meter"
            or str(permeability.get("BUnit", "")).casefold() != "tesla"
            or permeability.get("IsTemperatureDependent") is not False):
        raise ValueError("reference native BH type/units/temperature definition mismatch")
    coordinates = permeability["BHCoordinates"]
    points = coordinates.get("Point", coordinates.get("Coordinate"))
    if not isinstance(points, list):
        raise ValueError("reference native BH readback is unavailable")
    if points and isinstance(points[0], dict):
        points = [[p["X"], p["Y"]] for p in points]
    expected = pack["bh"]["points_H_B"]
    if len(points) != len(expected) or any(
            len(p) != 2 or any(not math.isclose(_material_readback_number(x), y, rel_tol=1e-12, abs_tol=1e-9)
                               for x, y in zip(p, q)) for p, q in zip(points, expected)):
        raise ValueError("reference native BH differs from the23 original points")
    scalar_expectations = {"conductivity": pack["properties"]["conductivity_s_per_m"],
                           "mass_density": pack["properties"]["mass_density_kg_per_m3"],
                           **{"core_loss_" + key.lower(): pack["core_loss"][key]
                              for key in ("Kh", "Kc", "Ke", "Y", "Kdc")}}
    for key, expected_value in scalar_expectations.items():
        if not math.isclose(_material_readback_number(props[key]), expected_value, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"reference native material scalar mismatch: {key}")
    for key, choice in (("core_loss_type", "Electrical Steel"), ("stacking_type", "Solid")):
        if props[key].get("Choice") != choice:
            raise ValueError(f"reference native material choice mismatch: {key}")
    if not math.isclose(_material_readback_number(props["core_loss_equiv_cut_depth"], "meter"),
                        pack["core_loss"]["equivalent_cut_depth_m"], rel_tol=1e-12):
        raise ValueError("reference equivalent cut depth mismatch")
    if _material_readback_number(props["magnetic_coercivity"]["Magnitude"], "A_per_meter") != 0:
        raise ValueError("reference core coercivity must be zero")
    if _material_readback_number(permeability["RecoilPermeability"]) != 1:
        raise ValueError("reference recoil permeability mismatch")


def _hydrate_reference_material_cache(m2d: Any, material: str) -> dict[str, Any]:
    """Bind the already verified native definition to PyAEDT's read-only wrapper.

    PyAEDT0.22's targeted loader constructs Material(material_update=False)
    from native GetData. Refresh both in-memory name lists before any lookup:
    a material_keys hit with a stale names list otherwise calls Material.update.
    The caller verifies native GetData/Composition unchanged after this bridge.
    """
    materials = m2d.materials
    names = list(materials.mat_names_aedt)  # Preserve lazy-loaded system/user names.
    cached = materials._aedmattolibrary(material)
    if cached is None or cached is False or getattr(cached, "name", None) != material:
        raise RuntimeError("reference material cache hydration failed")
    cached._material_update = False
    if materials.material_keys.get(material.casefold()) is not cached:
        raise RuntimeError("reference wrapper is absent from the PyAEDT material cache")
    if material.casefold() not in {name.casefold() for name in names}:
        names.append(material)
    materials._mats = names
    materials._mats_lower = [name.casefold() for name in names]
    if (materials.exists_material(material) is not cached
            or materials.exists_material(material.casefold()) is not cached
            or materials[material] is not cached or materials[material.casefold()] is not cached):
        raise RuntimeError("reference exact/lowercase PyAEDT lookups disagree")
    if cached.is_conductor() is not True:
        raise RuntimeError("reference PyAEDT wrapper cannot classify the verified conductor")
    if cached._material_update is not False:
        raise RuntimeError("reference PyAEDT wrapper must remain read-only")
    return {"pyaedt_material_cache_verified": True, "pyaedt_conductor_verified": True,
            "pyaedt_material_update_policy": "read_only"}


def _ensure_reference_core_material(m2d: Any, spec: IPMSMPPTSpec) -> dict[str, Any]:
    """Write a private project material, then verify independent live native data."""
    if spec.core_material != core_material_name_for_model(spec.core_material_model):
        raise ValueError("reference core material name/model mismatch")
    pack = load_reference_core_material_pack()
    manager = m2d.oproject.GetDefinitionManager()
    names = list(manager.GetProjectMaterialNames())
    collisions = [name for name in names if name.casefold() == spec.core_material.casefold()]
    if collisions and collisions != [spec.core_material]:
        raise ValueError("ambiguous existing reference material name")
    payload = _reference_native_material_payload(pack)
    operation = "updated" if collisions else "created"
    applied = (manager.EditMaterial(spec.core_material, payload) if collisions else manager.AddMaterial(payload))
    # Native documented APIs return None on success; direct readback is authoritative.
    if applied is False:
        raise ValueError("native reference material registration returned False")
    after_names = list(manager.GetProjectMaterialNames())
    if after_names.count(spec.core_material) != 1:
        raise ValueError("native reference material is missing from the project inventory")
    raw = manager.GetManager("Material").GetData(spec.core_material)
    props = _native_material_properties(raw, spec.core_material)
    _verify_reference_material_readback(props, pack)
    obj = m2d.oproject.GetChildObject("Materials").GetChildObject(spec.core_material)
    if "Composition" not in list(obj.GetPropNames()) or str(obj.GetPropValue("Composition")) != "Solid":
        raise ValueError("live reference material Composition is not Solid")
    bridge = _hydrate_reference_material_cache(m2d, spec.core_material)
    after_raw = manager.GetManager("Material").GetData(spec.core_material)
    after_props = _native_material_properties(after_raw, spec.core_material)
    _verify_reference_material_readback(after_props, pack)
    if after_props != props or str(obj.GetPropValue("Composition")) != "Solid":
        raise RuntimeError("reference native material changed during PyAEDT cache hydration")
    return {spec.core_material: operation, "core_material_model": spec.core_material_model,
            "material_pack_sha256": M19_REFERENCE_PACK_SHA256,
            "bh_curve_sha256": pack["bh"]["points_sha256"], "bh_curve_points": 23,
            "native_readback_verified": True, "composition_live": "Solid",
            # Keep raw provenance opaque to the generic operation-failure scanner.
            "native_GetData_json": json.dumps(list(after_raw)),
            "native_definition_unchanged_by_cache_bridge": True, **bridge,
            "reference_note": pack["identity_note"]}


def ensure_ppt_materials(design: Any, spec: IPMSMPPTSpec) -> dict[str, Any]:
    """Create the deck materials when they are absent from the AEDT project."""
    _validate_core_material_selection(spec)
    m2d = _m2d(design)
    result: dict[str, Any] = {}
    coeff = get_core_loss_coefficients(spec.core_material_model)

    if spec.core_material_model == M19_REFERENCE_CORE_MATERIAL_MODEL:
        result.update(_ensure_reference_core_material(m2d, spec))
    elif _material_exists(m2d, spec.core_material):
        result[spec.core_material] = "exists"
        try:
            mat = m2d.materials[spec.core_material]
            applied = _apply_core_material_properties(mat, coeff, strict=spec.strict_setup)
            result[f"{spec.core_material}_coreloss"] = coeff
            result[f"{spec.core_material}_properties"] = get_core_material_properties()
            result[f"{spec.core_material}_bh_curve"] = applied["bh_curve"]
        except Exception as exc:
            result[f"{spec.core_material}_coreloss"] = f"skipped: {exc}"
    else:
        try:
            mat = m2d.materials.add_material(spec.core_material)
            try:
                applied = _apply_core_material_properties(mat, coeff, strict=spec.strict_setup)
                result[f"{spec.core_material}_coreloss"] = coeff
                result[f"{spec.core_material}_properties"] = get_core_material_properties()
                result[f"{spec.core_material}_bh_curve"] = applied["bh_curve"]
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
    validate_ppt_spec_contract(spec)
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
    _set_var(design, "ElectricalZero", f"{spec.electrical_zero_deg:g}deg")
    if spec.beta_convention == BETA_CONVENTION_DQ_CURRENT_ADVANCE_V2:
        _set_var(design, "IdCommand", "-Imax*sin(Beta)")
        _set_var(design, "IqCommand", "Imax*cos(Beta)")
    else:
        _set_var(design, "IdCommand", "Imax*sin(Beta)")
        _set_var(design, "IqCommand", "-Imax*cos(Beta)")
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
    for slot_index, (phase, polarity) in enumerate(DEFAULT_12S8P_COIL_SIDE_MAP, start=1):
        boundary_names.append(f"{phase}_{polarity}_{slot_index:02d}")

    # Older helper versions incorrectly assigned the same excitation to both
    # coil sides in a slot. Remove those names too so notebook reruns start
    # from a clean boundary state.
    for slot_index, (phase, polarity) in enumerate(LEGACY_12S8P_SLOT_MAP, start=1):
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
        for name in PPT_REPORT_DEFS:
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
    band_circle_segments: int = 144,
) -> dict[str, Any]:
    """Reuse Region/Band, with configurable moving-interface faceting.

    This is an artificial interface within air, not the physical rotor surface.
    Region stays unchanged so band-only comparisons isolate the interface mesh.
    """
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
            num_sides=band_circle_segments,
            name="Band",
            material="air",
        )
        band = [_name(obj)]

    result: dict[str, Any] = {"region": region, "band": band}
    if band:
        if len(band) != 1:
            raise RuntimeError("moving-band geometry must contain exactly one interface object")
        edge_count = len(m2d.modeler.oeditor.GetEdgeIDsFromObject(band[0]))
        expected_edges = band_circle_segments if band_circle_segments else 1
        if edge_count != expected_edges:
            raise RuntimeError(f"moving-band geometry differs from requested segments: {edge_count} edges, expected {expected_edges}")
        result["band_geometry"] = {"requested_segments": band_circle_segments, "edge_count": edge_count}
    return result


def live_material_name(m2d: Any, object_name: str) -> str:
    """Read AEDT directly: Object3d.material_name caches its setter value."""
    value = m2d.modeler.oeditor.GetPropertyValue("Geometry3DAttributeTab", object_name, "Material")
    return str(value).strip().strip('"')


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
            if live_material_name(m2d, obj_name).casefold() != material.casefold():
                continue
            if solve_inside is not None:
                obj.solve_inside = solve_inside
            assigned.append(obj_name)
        except Exception:
            pass
    return assigned


def _assign_reference_core_material(m2d: Any, objects: list[str], material: str) -> list[str]:
    """Assign the verified project reference without PyAEDT's stale name cache.

    Maxwell's documented ChangeProperty material block is also the final native
    operation used by Object3d.material_name. Only its cached exists_material
    lookup is bypassed. SolveInside and every other object property are kept.
    """
    if material != M19_REFERENCE_CORE_MATERIAL_NAME:
        raise ValueError("native reference assignment requires the exact reference material name")
    if not objects:
        return []
    manager = m2d.oproject.GetDefinitionManager()
    if list(manager.GetProjectMaterialNames()).count(material) != 1:
        raise ValueError("verified reference material is absent from the native project inventory")
    applied = m2d.modeler.oeditor.ChangeProperty([
        "NAME:AllTabs", ["NAME:Geometry3DAttributeTab", ["NAME:PropServers", *objects],
                         ["NAME:ChangedProps", ["NAME:Material", "Value:=", '"' + material + '"']]],
    ])
    # Native success is void; a returned False is failure even if a stale live
    # object happened to have the requested name before this operation.
    if applied is False:
        raise RuntimeError("native reference core assignment returned False")
    assigned = []
    for name in objects:
        if live_material_name(m2d, name).casefold() == material.casefold():
            # Downstream conductor classification uses Object3d.material_name.
            # Synchronize only its Python cache after independent live proof;
            # never invoke the setter or change SolveInside.
            m2d.modeler.get_object_from_name(name)._material_name = material
            assigned.append(name)
    return assigned


def assign_ppt_materials(design: Any, object_groups: dict[str, Any] | None, spec: IPMSMPPTSpec) -> dict[str, Any]:
    """Assign deck materials to already-created geometry."""
    _validate_core_material_selection(spec)
    m2d = _m2d(design)
    stator = _get_group(m2d, object_groups, "stator", ("stator", "main_stator"))
    rotor = _get_group(m2d, object_groups, "rotor", ("rotator", "rotor"))
    shaft = _get_group(m2d, object_groups, "shaft", ("shaft",))
    magnets = _get_group(m2d, object_groups, "magnets", ("magnet",))
    windings = _get_group(m2d, object_groups, "windings", ("winding",))
    regions = _get_group(m2d, object_groups, "region", ("Region",))
    bands = _get_group(m2d, object_groups, "band", ("Band",))
    pockets = _get_group(m2d, object_groups, "air_pockets", ("magnet_space",))

    core_material_candidates = ((spec.core_material,) if spec.core_material_model == M19_REFERENCE_CORE_MATERIAL_MODEL
                                else (spec.core_material,) + spec.core_material_fallbacks)
    core_assigned = []
    selected_core_material = core_material_candidates[0]
    for material in core_material_candidates:
        if spec.core_material_model == M19_REFERENCE_CORE_MATERIAL_MODEL:
            core_assigned = _assign_reference_core_material(m2d, stator + rotor, material)
        else:
            core_assigned = _assign_material(m2d, stator + rotor, material)
        if core_assigned and set(core_assigned) == set(stator + rotor):
            selected_core_material = material
            break

    result: dict[str, Any] = {
        "core_material": [selected_core_material],
        "stator_rotor": core_assigned,
        "shaft": _assign_material(m2d, shaft, spec.shaft_material),
        "magnets": _assign_material(m2d, magnets, spec.magnet_material),
        "windings": _assign_material(m2d, windings, spec.winding_material, solve_inside=True),
        "air": _assign_material(m2d, regions + bands + pockets, spec.air_material),
        "air_pockets": pockets,
    }
    expected = {"stator_rotor": stator + rotor, "shaft": shaft, "magnets": magnets,
                "windings": windings, "air": regions + bands + pockets}
    errors = []
    if not stator or not rotor:
        errors.append("both stator and rotor iron objects are required")
    if spec.strict_setup and selected_core_material.casefold() != spec.core_material.casefold():
        errors.append("fallback core material cannot satisfy the requested strict material contract")
    if spec.model_extent == "full_360":
        for label, names, count in [("magnets", magnets, spec.pole_number),
                                    ("windings", windings, 2 * spec.slot_number)]:
            if len(names) != count:
                errors.append(f"{label}: expected {count} objects, found {len(names)}")
    for group, names in expected.items():
        if set(result[group]) != set(names):
            errors.append(f"{group}: assigned {len(result[group])}/{len(names)} objects")
    readback = {}
    for name in dict.fromkeys(name for names in expected.values() for name in names):
        try:
            readback[name] = live_material_name(m2d, name)
        except Exception as exc:
            errors.append(f"{name}: live material readback failed: {exc}")
    result["expected_counts"] = {key: len(names) for key, names in expected.items()}
    result["material_readback"] = readback
    result["errors"] = errors
    return result


def audit_actual_winding_geometry(
    m2d: Any,
    winding_names: Any,
    *,
    turns_per_coil_side: int,
    conductor_area_per_branch_mm2: float,
    slot_fill_limit: float,
    expected_count: int = 24,
) -> dict[str, Any]:
    """Read actual winding windows after CAD Booleans and reject invalid fill.

    Face areas come directly from the live editor in model-length squared.
    Only millimetre models are accepted. The declared conductor area is a fixed
    physical input, independent of the operating current. No cached geometry,
    analytic estimate, unit default, or synthesized area can satisfy this gate.
    Failures raise ValueError with a JSON-safe ``winding_geometry_audit``.
    """
    audit: dict[str, Any] = {
        "schema_version": "ipmsm_actual_winding_geometry_v1",
        "source": "live_editor.GetModelUnits/GetFaceIDs/GetFaceArea",
        "model_units_live": None,
        "units": {"length": "mm", "window_area": "mm^2", "conductor_area": "mm^2"},
        "count": 0,
        "expected_count": None,
        "min_window_area_mm2": None,
        "max_slot_fill_ratio": None,
        "objects": [],
        "violations": [],
        "passed": False,
    }

    def fail(message: str) -> None:
        audit["error"] = message
        error = ValueError("actual winding geometry audit failed: " + message)
        error.winding_geometry_audit = audit
        raise error

    def positive_number(value: Any, label: str) -> float:
        if isinstance(value, bool) or value is None:
            fail(label + " must be finite and positive, not bool/missing")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            fail(label + " must be a finite positive number")
        if not math.isfinite(number) or number <= 0.0:
            fail(label + " must be finite and positive")
        return number

    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count <= 0:
        fail("expected_count must be a positive integer")
    audit["expected_count"] = expected_count
    if isinstance(turns_per_coil_side, bool) or not isinstance(turns_per_coil_side, int) or turns_per_coil_side <= 0:
        fail("turns_per_coil_side must be a positive integer")
    conductor_area = positive_number(conductor_area_per_branch_mm2, "conductor_area_per_branch_mm2")
    fill_limit = positive_number(slot_fill_limit, "slot_fill_limit")
    if fill_limit > 1.0:
        fail("slot_fill_limit must not exceed 1")
    copper_area = positive_number(turns_per_coil_side * conductor_area, "copper_area_per_coil_side_mm2")
    audit.update(turns_per_coil_side=turns_per_coil_side,
                 conductor_area_per_branch_mm2=conductor_area,
                 copper_area_per_coil_side_mm2=copper_area,
                 slot_fill_limit=fill_limit)
    if winding_names is None or isinstance(winding_names, (bool, str, bytes)):
        fail("winding_names must be a sequence of distinct object names")
    try:
        names = list(winding_names)
    except Exception as exc:
        fail("winding_names unavailable: " + type(exc).__name__ + ": " + str(exc))
    audit["count"] = len(names)
    if len(names) != expected_count:
        fail(f"unexpected winding count {len(names)}; expected {expected_count}")
    if any(not isinstance(name, str) or not name or name != name.strip() for name in names):
        fail("winding object names must be nonempty strings without surrounding whitespace")
    if len({name.casefold() for name in names}) != expected_count:
        fail("duplicate winding object names")
    try:
        editor = m2d.modeler.oeditor
        model_units = editor.GetModelUnits()
    except Exception as exc:
        fail("live editor model units unavailable: " + type(exc).__name__ + ": " + str(exc))
    if isinstance(model_units, str):
        audit["model_units_live"] = model_units
    if not isinstance(model_units, str) or model_units.strip() != "mm":
        fail("live editor model units must be mm")
    seen_faces: set[int] = set()
    for name in names:
        obj: dict[str, Any] = {"name": name, "face_ids": [], "face_areas_mm2": [],
                               "window_area_mm2": None, "slot_fill_ratio": None}
        audit["objects"].append(obj)
        try:
            raw_faces = editor.GetFaceIDs(name)
        except Exception as exc:
            fail("live face IDs unavailable for " + name + ": " + type(exc).__name__ + ": " + str(exc))
        if raw_faces is None or isinstance(raw_faces, (bool, str, bytes)):
            fail("missing/invalid face IDs for " + name)
        try:
            face_ids = list(raw_faces)
        except Exception as exc:
            fail("invalid face IDs for " + name + ": " + type(exc).__name__ + ": " + str(exc))
        if not face_ids:
            fail("missing face IDs for " + name)
        for raw_face in face_ids:
            if isinstance(raw_face, bool):
                fail("bool face ID for " + name)
            if isinstance(raw_face, int):
                face = raw_face
            elif isinstance(raw_face, str) and raw_face.isascii() and raw_face.isdecimal():
                face = int(raw_face)
            else:
                fail("non-integer face ID for " + name)
            if face <= 0:
                fail("nonpositive face ID for " + name)
            if face in seen_faces:
                fail("duplicate face ID " + str(face) + " for " + name)
            seen_faces.add(face)
            obj["face_ids"].append(face)
            try:
                raw_area = editor.GetFaceArea(face)
            except Exception as exc:
                fail("live face area unavailable for " + name + ": " + type(exc).__name__ + ": " + str(exc))
            area = positive_number(raw_area, "face area for " + name + "/" + str(face))
            obj["face_areas_mm2"].append(area)
        window_area = positive_number(sum(obj["face_areas_mm2"]), "total window area for " + name)
        fill = positive_number(copper_area / window_area, "slot fill for " + name)
        obj.update(window_area_mm2=window_area, slot_fill_ratio=fill)
        audit["min_window_area_mm2"] = min(item["window_area_mm2"] for item in audit["objects"])
        audit["max_slot_fill_ratio"] = max(item["slot_fill_ratio"] for item in audit["objects"])
        if fill > fill_limit:
            audit["violations"].append({"name": name, "slot_fill_ratio": fill, "slot_fill_limit": fill_limit})
    if audit["violations"]:
        fail(f"maximum actual slot fill {audit['max_slot_fill_ratio']:.12g} exceeds limit {fill_limit:.12g}")
    audit["passed"] = True
    return audit


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
    validate_ppt_spec_contract(spec, use_periodic_boundary=use_periodic_boundary)
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


def _sort_coil_sides_by_angle(m2d: Any, winding_names: list[str]) -> list[str]:
    """Sort coil-side sheet objects by their polar angle around the machine center."""
    sorted_by_name = sorted(winding_names, key=_natural_key)
    angle_items: list[tuple[float, str]] = []

    for name in sorted_by_name:
        try:
            obj = m2d.modeler.get_object_from_name(name)
            bounding_box = [float(value) for value in obj.bounding_box]
            x_center = 0.5 * (bounding_box[0] + bounding_box[3])
            y_center = 0.5 * (bounding_box[1] + bounding_box[4])
            angle = math.atan2(y_center, x_center)
            if angle < 0.0:
                angle += 2.0 * math.pi
            angle_items.append((angle, name))
        except Exception:
            return sorted_by_name

    return [name for _, name in sorted(angle_items)]


def assign_three_phase_windings(
    design: Any,
    object_groups: dict[str, Any] | None,
    spec: IPMSMPPTSpec,
    coil_side_map: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Assign A/B/C current windings for the 12-slot/8-pole practice motor."""
    m2d = _m2d(design)
    winding_names = _get_group(m2d, object_groups, "windings", ("winding",))
    numbered_windings = [name for name in winding_names if re.search(r"\d+$", name)]
    if len(numbered_windings) >= len(DEFAULT_12S8P_COIL_SIDE_MAP):
        winding_names = numbered_windings
    winding_names = _sort_coil_sides_by_angle(m2d, winding_names)
    coil_side_map = coil_side_map or DEFAULT_12S8P_COIL_SIDE_MAP
    result: dict[str, Any] = {"errors": []}

    if len(winding_names) != len(coil_side_map):
        return {
            "errors": [
                f"Expected {len(coil_side_map)} coil-side winding objects for the PPT 12-slot/8-pole layout, "
                f"got {len(winding_names)}"
            ],
            "objects": winding_names,
        }

    phase_current = phase_current_expressions(spec)
    phase_coils = {phase: [] for phase in phase_current}

    for side_index, (obj_name, (phase, polarity)) in enumerate(zip(winding_names, coil_side_map), start=1):
        coil_name = f"{phase}_{polarity}_{side_index:02d}"
        try:
            coil = m2d.assign_coil(
                assignment=[obj_name],
                conductors_number=spec.turns_per_coil_side,
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
                is_solid=False,
                current=current,
                parallel_branches=1,
                name=phase,
            )
            if winding:
                if m2d.add_winding_coils(assignment=phase, coils=phase_coils[phase]) is False:
                    result["errors"].append(f"{phase}: add_winding_coils returned False")
            else:
                result["errors"].append(f"{phase}: assign_winding returned False")
        except Exception as exc:
            result["errors"].append(f"{phase}: {exc}")

    result.update(phase_coils)
    result["coil_side_order"] = winding_names
    return result


def assign_losses(
    design: Any, object_groups: dict[str, Any] | None, loss_policy: str = "laminated_core"
) -> dict[str, Any]:
    """Enable magnet eddy-current loss and stator/rotor core loss calculations."""
    m2d = _m2d(design)
    stator = _get_group(m2d, object_groups, "stator", ("stator", "main_stator"))
    rotor = _get_group(m2d, object_groups, "rotor", ("rotator", "rotor"))
    magnets = _get_group(m2d, object_groups, "magnets", ("magnet",))
    windings = _get_group(m2d, object_groups, "windings", ("winding",))
    result: dict[str, Any] = {"magnet_current_zero": [], "core_losses": None}

    try:
        objects = stator + rotor + magnets + windings
        result["eddy_before"] = {
            name: "on" if m2d.oboundary.GetEddyEffect(name) else "off" for name in objects
        }
        if loss_policy == "laminated_core":
            # Electrical-steel CoreLoss already contains lamination eddy loss.
            # Do not also solve a solid bulk eddy-current path through the iron.
            result["eddy_core_disabled"] = m2d.eddy_effects_on(stator + rotor, enable_eddy_effects=False)
            result["eddy_winding_disabled"] = m2d.eddy_effects_on(windings, enable_eddy_effects=False)
            result["eddy_magnet_enabled"] = m2d.eddy_effects_on(magnets, enable_eddy_effects=True)
    except Exception as exc:
        result["eddy_settings"] = f"skipped: {exc}"

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

    try:
        result["eddy_readback"] = {
            name: "on" if m2d.oboundary.GetEddyEffect(name) else "off" for name in objects
        }
        if loss_policy == "laminated_core":
            expected = {name: "off" for name in stator + rotor + windings}
            expected.update({name: "on" for name in magnets})
            if result["eddy_readback"] != expected:
                raise RuntimeError("eddy-current setting readback does not match the loss policy")
    except Exception as exc:
        result["eddy_readback_error"] = f"skipped: {exc}"

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
            # PyAEDT 0.22 documents ``maximum_length=None`` as disabled, but
            # still serializes it as ``Nonemm`` in ``MaxLength``. AEDT then
            # rejects the setup even though ``RestrictLength`` is false. Build
            # the operation with a valid placeholder and disable that limit in
            # the persisted properties; the element-count contract is unchanged.
            local_length = spec.mesh_maximum_lengths_mm.get(group_name)
            operation = m2d.mesh.assign_length_mesh(
                assignment=objects,
                inside_selection=True,
                maximum_length=f"{local_length:g}mm" if local_length is not None else "1mm",
                maximum_elements=None if spec.mesh_refinement_modes.get(group_name) == "length_only" else spec.mesh_elements[group_name],
                name=f"Mesh_{group_name}",
            )
            if not operation:
                raise RuntimeError("length mesh operation was not created")
            if spec.mesh_refinement_modes.get(group_name) == "length_only":
                operation.props["RestrictElem"] = False
                operation.props["NumMaxElem"] = "1000"  # Valid inactive placeholder, not a growth limit.
            operation.props["RestrictLength"] = local_length is not None
            operation.props["MaxLength"] = f"{local_length:g}mm" if local_length is not None else "1mm"
            if operation.update() is False:
                raise RuntimeError("length mesh operation update failed")
            result[group_name] = operation
        except Exception as exc:
            result[group_name] = f"skipped: {exc}"
    return result


def audit_mesh_operation_settings(operations: dict[str, Any], objects: dict[str, list], spec: IPMSMPPTSpec, *, source: str) -> dict[str, Any]:
    """Compare independent native/saved properties; this does not certify mesh sizes."""
    import re
    validate_ppt_spec_contract(spec)
    audit: dict[str, Any] = {"source": source, "passed": False, "groups": {},
                             "actual_mesh_length_verified": False}
    try:
        if source not in {"native_OO", "saved_AEDT"}:
            raise ValueError("unsupported mesh evidence source")
        for group in ("magnet", "rotor", "stator", "winding", "band"):
            prop = operations["Mesh_" + group]
            assigned = prop["Objects"]
            expected = objects[group]
            if not isinstance(assigned, list) or not expected or len(set(assigned)) != len(assigned):
                raise ValueError(f"invalid mesh assignment: {group}")
            if set(assigned) != set(expected) or any(type(x) not in (str, int) for x in assigned):
                raise ValueError(f"mesh object assignment differs: {group}")
            if prop.get("Type") not in ("LengthBased", "Length Based"):
                raise ValueError(f"mesh type differs: {group}")
            length_only = spec.mesh_refinement_modes.get(group) == "length_only"
            length = spec.mesh_maximum_lengths_mm.get(group)
            wanted = {"Enabled": True, "RefineInside": True, "RestrictElem": not length_only,
                      "RestrictLength": length is not None, "ApplyToInitialMesh": False}
            for key, value in wanted.items():
                if type(prop.get(key)) is not bool or prop[key] is not value:
                    raise ValueError(f"mesh flag differs: {group}.{key}")
            # Disabled fields can be hidden by native OO. The saved-file audit checks them too.
            count = prop.get("NumMaxElem")
            if not length_only or source == "saved_AEDT":
                if type(count) is int:
                    number = count
                elif type(count) is str and count.isdecimal():
                    number = int(count)
                else:
                    raise ValueError(f"invalid native mesh count: {group}")
                if number != (1000 if length_only else spec.mesh_elements[group]):
                    raise ValueError(f"mesh count differs: {group}")
            if length is not None:
                match = re.fullmatch(r"\s*([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)\s*mm\s*", str(prop.get("MaxLength", "")))
                if not match or not math.isclose(float(match[1]), float(f"{length:g}"), rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError(f"mesh length/mm unit differs: {group}")
            elif source == "saved_AEDT" and prop.get("MaxLength") != "1mm":
                raise ValueError(f"inactive mesh length placeholder differs: {group}")
            audit["groups"][group] = {"mode": "length_only" if length_only else "additional_budget",
                                      "requested_length_mm": length, "properties": dict(prop)}
        audit["passed"] = True
        audit["inactive_placeholders_verified"] = source == "saved_AEDT"
        return audit
    except (KeyError, TypeError, ValueError) as exc:
        audit["error"] = str(exc)
        error = ValueError("native mesh settings invalid: " + str(exc))
        error.mesh_refinement_audit = audit
        raise error from exc


def audit_native_mesh_settings(design: Any, object_groups: dict[str, Any], spec: IPMSMPPTSpec) -> dict[str, Any]:
    """Read actual Mesh child properties, never cached MeshOperation.props."""
    m2d = _m2d(design)
    groups = {"magnet": _get_group(m2d, object_groups, "magnets", ("magnet",)),
              "rotor": _get_group(m2d, object_groups, "rotor", ("rotator", "rotor")),
              "stator": _get_group(m2d, object_groups, "stator", ("stator", "main_stator")),
              "winding": _get_group(m2d, object_groups, "windings", ("winding",)),
              "band": _get_group(m2d, object_groups, "band", ("Band",))}
    operations = {}
    try:
        mesh = m2d.odesign.GetChildObject("Mesh")
        for group in groups:
            node = mesh.GetChildObject("Mesh_" + group)
            names = set(node.GetPropNames())
            read = lambda name: node.GetPropValue(name) if name in names else None
            assignment = read("Assignment")
            operations["Mesh_" + group] = {"Type": read("Type"), "Enabled": read("Enabled"),
                "Objects": [part.strip() for part in assignment.split(",")] if isinstance(assignment, str) else assignment,
                "RefineInside": read("Region") == "Inside Selection", "RestrictElem": read("Restrict Max Elems"),
                "RestrictLength": read("Restrict Length"), "ApplyToInitialMesh": read("Apply To Initial Mesh"),
                "NumMaxElem": read("Max Elems"), "MaxLength": read("Max Length")}
    except Exception as exc:
        error = ValueError("native mesh OO readback failed: " + str(exc))
        error.mesh_refinement_audit = {"source": "native_OO", "passed": False, "error": str(exc)}
        raise error from exc
    return audit_mesh_operation_settings(operations, groups, spec, source="native_OO")


def audit_native_model_depth(design: Any, spec: IPMSMPPTSpec) -> dict[str, Any]:
    """Fail closed on native 2D depth; Python proxy/setter attributes are not evidence."""
    m2d = _m2d(design)
    audit: dict[str, Any] = {
        "source": "native_DesignSettings.GetPropValue/GetPropSIValue_and_StackLength_SI",
        "passed": False,
        "declared_stack_length_mm": None,
        "commanded_stack_length_expression": None,
        "expected_depth_SI_m": None,
        "model_units_live": None,
        "native_depth_property": None,
        "native_depth_expression": None,
        "native_depth_SI_m": None,
        "StackLength_expression_live": None,
        "StackLength_SI_m": None,
        "relative_tolerance": 1e-9,
        "absolute_tolerance_m": 1e-12,
    }

    def positive(value: Any, label: str) -> float:
        if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(label + " must be a positive finite number")
        return float(value)

    try:
        declared = positive(spec.stack_length_mm, "declared stack_length_mm")
        # This is the exact command emitted by apply_ppt_design_variables.
        commanded_mm = f"{declared:g}"
        expected = float(commanded_mm) * 1e-3
        audit.update(declared_stack_length_mm=declared,
                     commanded_stack_length_expression=commanded_mm + "mm", expected_depth_SI_m=expected)
        units = m2d.modeler.oeditor.GetModelUnits()
        audit["model_units_live"] = units if isinstance(units, str) else None
        if not isinstance(units, str) or units.strip() != "mm":
            raise ValueError("native editor model units must be mm")
        settings = m2d.odesign.GetChildObject("Design Settings")
        names = [str(name) for name in settings.GetPropNames()
                 if re.sub(r"[^a-z]", "", str(name).lower()) == "modeldepth"]
        if len(names) != 1:
            raise ValueError("native Model Depth property is unavailable or ambiguous")
        audit["native_depth_property"] = names[0]
        expression = settings.GetPropValue(names[0])
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("native Model Depth expression is unavailable")
        audit["native_depth_expression"] = expression
        depth_si = positive(settings.GetPropSIValue(names[0]), "native Model Depth")
        audit["native_depth_SI_m"] = depth_si
        stack_expression = m2d.odesign.GetVariableValue("StackLength")
        if not isinstance(stack_expression, str) or not stack_expression.strip():
            raise ValueError("native StackLength expression is unavailable")
        audit["StackLength_expression_live"] = stack_expression
        stack_si = positive(m2d.get_evaluated_value("StackLength"), "native StackLength")
        audit["StackLength_SI_m"] = stack_si
        for label, actual in (("native StackLength", stack_si), ("native Model Depth", depth_si)):
            if not math.isclose(actual, expected, rel_tol=audit["relative_tolerance"],
                                abs_tol=audit["absolute_tolerance_m"]):
                raise ValueError(f"{label}={actual:g}m differs from commanded StackLength={commanded_mm}mm")
    except Exception as exc:
        audit["error"] = f"{type(exc).__name__}: {exc}"
        error = ValueError("Native ModelDepth audit failed: " + str(exc))
        error.native_model_depth_audit = audit
        raise error from exc
    audit["passed"] = True
    return audit


def create_ppt_transient_setup(design: Any, spec: IPMSMPPTSpec) -> Any:
    """Create a transient setup with a fixed number of steps per electrical period."""
    validate_ppt_spec_contract(spec)
    m2d = _m2d(design)
    try:
        m2d.model_depth = "StackLength"
    except Exception:
        if spec.strict_setup:
            raise
    try:
        if m2d.change_symmetry_multiplier("SymmetryFactor") is False:
            raise RuntimeError("AEDT rejected the symmetry multiplier")
    except Exception:
        if spec.strict_setup:
            raise

    setup = m2d.create_setup(name=spec.setup_name)
    schedule = field_save_schedule(spec)
    if schedule is not None:
        # SetupProps writes call Native update unless disabled, as in SDK set_save_fields.
        setup.auto_update = False
    setup.props["StopTime"] = "StopTime"
    setup.props["TimeStep"] = "TimeStep"
    setup.props["SaveFieldsType"] = "None"
    setup.props.pop("N Steps", None)
    setup.props.pop("Steps From", None)
    setup.props.pop("Steps To", None)
    if schedule is not None:
        # Exact PyAEDT 0.22 SetupMaxwell.set_save_fields Every-N-Steps properties.
        setup.props["SaveFieldsType"] = "Every N Steps"
        setup.props.pop("SweepRanges", None)
        setup.props["N Steps"] = str(schedule["every_n_steps"])
        setup.props["Steps From"] = f"{schedule['start_time_s']}s"
        setup.props["Steps To"] = f"{schedule['stop_time_s']}s"
    setup.props["OutputPerObjectCoreLoss"] = True
    setup.props["OutputPerObjectSolidLoss"] = True
    setup.props["OutputError"] = spec.solver_output_error
    setup.props["NonlinearSolverResidual"] = str(float(spec.solver_nonlinear_residual))
    if setup.update() is False:
        raise RuntimeError("AEDT rejected the transient setup")
    return setup


def enable_ppt_transient_inductance(
    design: Any, incremental_matrix: bool = False, enabled: bool = True
) -> Any:
    """Enable the transient inductance matrix used by the PPT Ld/Lq workflow."""
    m2d = _m2d(design)
    try:
        return m2d.change_inductance_computation(
            compute_transient_inductance=enabled,
            incremental_matrix=incremental_matrix,
        )
    except Exception as exc:
        return f"skipped: {exc}"


def create_ppt_reports(
    design: Any, setup_name: str = "PPT_Transient", compute_inductance: bool = True,
    *, solver_output_error: bool = True,
) -> dict[str, Any]:
    """Create physical reports and the explicitly enabled solver diagnostic."""
    if type(solver_output_error) is not bool:
        raise ValueError("solver_output_error must be a boolean")
    m2d = _m2d(design)
    setup_sweep = f"{setup_name} : Transient"
    reports: dict[str, Any] = {}
    for plot_name, expressions in PPT_REPORT_DEFS.items():
        if plot_name == "PPT_EnergyError" and not solver_output_error:
            continue
        if plot_name == "PPT_Inductance_Matrix" and not compute_inductance:
            continue
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


def critical_setup_issues(result: dict[str, Any]) -> list[str]:
    """Find failed essential operations before starting an expensive solve."""
    issues: list[str] = []

    def inspect(value: Any, path: str) -> None:
        if isinstance(value, str) and value.lower().startswith("skipped:"):
            issues.append(f"{path}: {value}")
        elif value is False or value is None:
            if not path.endswith(".periodic"):
                issues.append(f"{path}: operation not applied")
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == "errors" and item:
                    issues.append(f"{path}.errors: {item}")
                else:
                    inspect(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                inspect(item, f"{path}[{index}]")

    for stage in (
        "geometry_overlap_resolution", "winding_geometry_audit", "material_definitions", "materials",
        "magnet_coordinate_systems", "boundaries_motion", "windings", "losses",
        "mesh", "inductance_computation", "setup", "validation",
    ):
        if stage in result:
            inspect(result[stage], stage)
    if type(result.get("validation")) is int and result["validation"] != 1:
        issues.append("validation: AEDT design validation failed")
    materials = result.get("materials")
    if isinstance(materials, dict):
        for group in ("stator_rotor", "magnets", "windings", "air"):
            if not materials.get(group):
                issues.append(f"materials.{group}: no assigned objects")
    return issues


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
    cores: int | None = 4,
    analysis_error_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run the full post-modeling setup flow from the practice deck."""
    spec = spec or IPMSMPPTSpec()
    validate_ppt_spec_contract(spec, use_periodic_boundary=use_periodic_boundary)
    apply_ppt_design_variables(design, spec)
    set_operation_current(design, spec, operation=operation)
    cleanup = clear_previous_ppt_setup(design, spec) if clear_existing else "skipped"

    created = ensure_region_and_band(
        design,
        object_groups,
        create_missing_region=create_missing_region,
        create_missing_band=create_missing_band,
        band_circle_segments=spec.band_circle_segments,
    )
    merged_groups = dict(object_groups or {})
    merged_groups.setdefault("region", created["region"])
    merged_groups.setdefault("band", created["band"])

    setup_started = time.perf_counter()
    overlap_resolution = resolve_geometry_overlaps(design, merged_groups)
    m2d = _m2d(design)
    winding_geometry_audit = audit_actual_winding_geometry(
        m2d, _get_group(m2d, merged_groups, "windings", ("winding",)),
        turns_per_coil_side=spec.turns_per_coil_side,
        conductor_area_per_branch_mm2=spec.conductor_area_per_branch_mm2,
        slot_fill_limit=spec.slot_fill_limit, expected_count=2 * spec.slot_number,
    )
    result = {
        "variables": "applied",
        "cleanup": cleanup,
        "region_band": created,
        "geometry_overlap_resolution": overlap_resolution,
        "winding_geometry_audit": winding_geometry_audit,
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
        "losses": assign_losses(design, merged_groups, spec.loss_policy),
        "mesh": assign_mesh(design, merged_groups, spec),
        "inductance_computation": (
            enable_ppt_transient_inductance(design) if spec.compute_inductance
            else enable_ppt_transient_inductance(design, enabled=False)
        ),
        "setup": create_ppt_transient_setup(design, spec),
    }

    m2d = _m2d(design)
    if spec.mesh_refinement_modes:
        result["native_mesh_audit"] = audit_native_mesh_settings(design, merged_groups, spec)
    result["native_model_depth_audit"] = audit_native_model_depth(design, spec)
    try:
        result["validation"] = m2d.validate_simple()
    except Exception as exc:
        result["validation"] = f"skipped: {exc}"

    result["setup_issues"] = critical_setup_issues(result)
    result["timings"] = {"setup_s": time.perf_counter() - setup_started}
    if (spec.strict_setup or spec.core_material_model == M19_REFERENCE_CORE_MATERIAL_MODEL) and result["setup_issues"]:
        raise RuntimeError("Essential AEDT setup failed: " + "; ".join(result["setup_issues"]))

    if create_reports:
        result["reports"] = create_ppt_reports(
            design, spec.setup_name, spec.compute_inductance, solver_output_error=spec.solver_output_error
        )

    try:
        result["native_model_depth_audit_post_validation"] = audit_native_model_depth(design, spec)
    except ValueError as exc:
        exc.native_model_depth_audit["stage"] = "after_validation_and_reports"
        exc.native_model_depth_audit["before_validation"] = result["native_model_depth_audit"]
        raise

    if spec.mesh_refinement_modes:
        result["native_mesh_audit_post_validation"] = audit_native_mesh_settings(design, merged_groups, spec)
    if analyze:
        solve_started = time.perf_counter()
        if analysis_error_callback is None:
            result["analysis"] = m2d.analyze(
                setup=spec.setup_name,
                cores=cores,
                use_auto_settings=False,
            )
        else:
            try:
                result["analysis"] = m2d.analyze(
                    setup=spec.setup_name,
                    cores=cores,
                    use_auto_settings=False,
                )
            except Exception:
                analysis_error_callback()
                raise
        result["timings"]["solve_s"] = time.perf_counter() - solve_started

    return result
