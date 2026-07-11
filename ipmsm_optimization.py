"""Deterministic IPMSM design-control optimization primitives.

The module deliberately has no numerical-ML or ``pymoo`` dependency.  A
surrogate is supplied as a small callable accepting the feature mapping built
by :func:`build_surrogate_features` and returning the canonical prediction
fields documented by :class:`PredictionEnvelope`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


SPEC_SCHEMA_VERSION = 1
BETA_CONVENTION = "dq_current_advance_v2"

# These are the independent geometry variables and ranges used by
# module/variable.py.  Slot/pole count remain the v1 fixed 12/8 topology.
DEFAULT_GEOMETRY_DESIGN_SPACE: tuple[tuple[str, float, float], ...] = (
    ("stator_outer_radius", 120.0, 200.0),
    ("stator_back_yoke_thick_ratio", 0.10, 0.15),
    ("stator_inner_ratio", 0.40, 0.60),
    ("stator_shoe_thick", 1.0, 2.0),
    ("stator_teeth_length_ratio", 0.80, 0.90),
    ("stator_teeth_width_ratio", 0.40, 0.80),
    ("stator_gap", 1.0, 3.0),
    ("slot_opening_ratio", 0.03, 0.15),
    ("rotator_gap", 1.0, 3.0),
    ("shaft_ratio", 0.40, 0.60),
    ("magnet_shield_thick", 1.0, 5.0),
    ("magnet_setback_ratio", 0.10, 0.20),
    ("magnet_thick_ratio", 0.20, 0.50),
    ("magnet_space_height_ratio", 0.80, 1.00),
    ("magnet_height_ratio", 0.80, 1.00),
)
GEOMETRY_VARIABLE_NAMES = tuple(item[0] for item in DEFAULT_GEOMETRY_DESIGN_SPACE)


class OptimizationSpecError(ValueError):
    """Raised when an optimization specification is incomplete or invalid."""


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _finite_number(value: Any, path: str) -> float:
    if not _is_number(value):
        raise OptimizationSpecError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise OptimizationSpecError(f"{path} must be a finite number")
    return result


def _positive_number(value: Any, path: str) -> float:
    result = _finite_number(value, path)
    if result <= 0.0:
        raise OptimizationSpecError(f"{path} must be > 0")
    return result


def _nonnegative_number(value: Any, path: str) -> float:
    result = _finite_number(value, path)
    if result < 0.0:
        raise OptimizationSpecError(f"{path} must be >= 0")
    return result


def _positive_int(value: Any, path: str) -> int:
    number = _positive_number(value, path)
    if not number.is_integer():
        raise OptimizationSpecError(f"{path} must be a positive integer")
    return int(number)


def _nonnegative_int(value: Any, path: str) -> int:
    number = _nonnegative_number(value, path)
    if not number.is_integer():
        raise OptimizationSpecError(f"{path} must be a nonnegative integer")
    return int(number)


def _required(mapping: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise OptimizationSpecError(f"missing required field: {path}.{key}")
    return mapping[key]


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OptimizationSpecError(f"{path} must be an object")
    return value


def _bounds(value: Any, path: str) -> tuple[float, float]:
    if isinstance(value, Mapping):
        lower = _finite_number(_required(value, "lower", path), f"{path}.lower")
        upper = _finite_number(_required(value, "upper", path), f"{path}.upper")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
        lower = _finite_number(value[0], f"{path}[0]")
        upper = _finite_number(value[1], f"{path}[1]")
    else:
        raise OptimizationSpecError(f"{path} must be [lower, upper] or an object with lower/upper")
    if lower >= upper:
        raise OptimizationSpecError(f"{path} lower bound must be < upper bound")
    return lower, upper


@dataclass(frozen=True)
class DesignVariableBound:
    name: str
    lower: float
    upper: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("design variable name must not be empty")
        if not all(math.isfinite(value) for value in (self.lower, self.upper)):
            raise ValueError(f"{self.name} bounds must be finite")
        if self.lower >= self.upper:
            raise ValueError(f"{self.name} lower bound must be < upper bound")

    def clip(self, value: float) -> float:
        return min(self.upper, max(self.lower, float(value)))


@dataclass(frozen=True)
class OperatingPoint:
    name: str
    speed_rpm: float
    duty_weight: float
    target_torque_nm: float | None = None
    target_power_w: float | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.name):
            raise ValueError("operating-point name must contain only letters, numbers, '_' or '-'")
        if not math.isfinite(self.speed_rpm) or self.speed_rpm <= 0.0:
            raise ValueError(f"operating point {self.name!r} speed_rpm must be > 0")
        if not math.isfinite(self.duty_weight) or self.duty_weight < 0.0:
            raise ValueError(f"operating point {self.name!r} duty_weight must be >= 0")
        targets = (self.target_torque_nm is not None, self.target_power_w is not None)
        if sum(targets) != 1:
            raise ValueError(
                f"operating point {self.name!r} must define exactly one of target_torque_nm or target_power_w"
            )
        target = self.target_torque_nm if self.target_torque_nm is not None else self.target_power_w
        if target is None or not math.isfinite(target) or target <= 0.0:
            raise ValueError(f"operating point {self.name!r} target must be > 0")

    @property
    def mechanical_angular_speed_rad_s(self) -> float:
        return self.speed_rpm * 2.0 * math.pi / 60.0

    @property
    def required_torque_nm(self) -> float:
        if self.target_torque_nm is not None:
            return self.target_torque_nm
        assert self.target_power_w is not None
        return self.target_power_w / self.mechanical_angular_speed_rad_s

    @property
    def required_power_w(self) -> float:
        if self.target_power_w is not None:
            return self.target_power_w
        assert self.target_torque_nm is not None
        return self.target_torque_nm * self.mechanical_angular_speed_rad_s

    @property
    def target_kind(self) -> str:
        return "torque" if self.target_torque_nm is not None else "power"


@dataclass(frozen=True)
class InverterSpec:
    vdc_v: float
    phase_peak_current_limit_a: float
    voltage_utilization: float = 0.95

    def __post_init__(self) -> None:
        if not math.isfinite(self.vdc_v) or self.vdc_v <= 0.0:
            raise ValueError("inverter.vdc_v must be > 0")
        if not math.isfinite(self.phase_peak_current_limit_a) or self.phase_peak_current_limit_a <= 0.0:
            raise ValueError("inverter.phase_peak_current_limit_a must be > 0")
        if not math.isfinite(self.voltage_utilization) or not 0.0 < self.voltage_utilization <= 1.0:
            raise ValueError("inverter.voltage_utilization must be > 0 and <= 1")

    @property
    def phase_peak_voltage_limit_v(self) -> float:
        return self.voltage_utilization * self.vdc_v / math.sqrt(3.0)


@dataclass(frozen=True)
class WindingSpec:
    series_turns_per_phase: int
    turns_per_coil_side: int
    coils_per_phase: int
    parallel_branches: int
    strand_area_mm2: float
    strands_per_turn: int
    fill_factor: float
    end_turn_factor: float
    overhang_mm: float
    copper_resistivity_20c_ohm_m: float = 1.724e-8
    copper_temp_coefficient_per_c: float = 0.00393
    winding_temperature_c: float = 100.0

    def __post_init__(self) -> None:
        for name in (
            "series_turns_per_phase",
            "turns_per_coil_side",
            "coils_per_phase",
            "parallel_branches",
            "strands_per_turn",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"winding.{name} must be a positive integer")
        if self.series_turns_per_phase * self.parallel_branches != self.turns_per_coil_side * self.coils_per_phase:
            raise ValueError(
                "winding turns are inconsistent: series_turns_per_phase * parallel_branches "
                "must equal turns_per_coil_side * coils_per_phase"
            )
        if not math.isfinite(self.strand_area_mm2) or self.strand_area_mm2 <= 0.0:
            raise ValueError("winding.strand_area_mm2 must be > 0")
        if not math.isfinite(self.fill_factor) or not 0.0 < self.fill_factor <= 1.0:
            raise ValueError("winding.fill_factor must be > 0 and <= 1")
        if not math.isfinite(self.end_turn_factor) or self.end_turn_factor <= 0.0:
            raise ValueError("winding.end_turn_factor must be > 0")
        if not math.isfinite(self.overhang_mm) or self.overhang_mm < 0.0:
            raise ValueError("winding.overhang_mm must be >= 0")
        if not math.isfinite(self.copper_resistivity_20c_ohm_m) or self.copper_resistivity_20c_ohm_m <= 0.0:
            raise ValueError("winding.copper_resistivity_20c_ohm_m must be > 0")
        if not math.isfinite(self.copper_temp_coefficient_per_c) or self.copper_temp_coefficient_per_c < 0.0:
            raise ValueError("winding.copper_temp_coefficient_per_c must be >= 0")
        if not math.isfinite(self.winding_temperature_c):
            raise ValueError("winding.winding_temperature_c must be finite")

    @property
    def conductor_area_per_branch_mm2(self) -> float:
        return self.strand_area_mm2 * self.strands_per_turn

    @property
    def total_parallel_conductor_area_mm2(self) -> float:
        return self.conductor_area_per_branch_mm2 * self.parallel_branches

    @property
    def resistivity_at_temperature_ohm_m(self) -> float:
        return self.copper_resistivity_20c_ohm_m * (
            1.0 + self.copper_temp_coefficient_per_c * (self.winding_temperature_c - 20.0)
        )


@dataclass(frozen=True)
class ConstraintSpec:
    current_density_limit_a_per_mm2: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.current_density_limit_a_per_mm2) or self.current_density_limit_a_per_mm2 <= 0.0:
            raise ValueError("constraints.current_density_limit_a_per_mm2 must be > 0")


@dataclass(frozen=True)
class BetaCalibrationSpec:
    electrical_zero_deg: float
    calibration_id: str
    convention: str = BETA_CONVENTION

    def __post_init__(self) -> None:
        if not math.isfinite(self.electrical_zero_deg):
            raise ValueError("beta_calibration.electrical_zero_deg must be finite")
        if not self.calibration_id.strip():
            raise ValueError("beta_calibration.calibration_id must not be blank")
        if self.convention != BETA_CONVENTION:
            raise ValueError(
                f"beta_calibration.convention must be {BETA_CONVENTION!r}"
            )


@dataclass(frozen=True)
class ControlSearchSpec:
    beta_bounds_deg: tuple[float, float] = (0.0, 80.0)
    current_grid_points: int = 33
    coarse_beta_step_deg: float = 10.0
    beta_refinement_steps_deg: tuple[float, ...] = (2.0, 0.25)
    current_refinement_denominators: tuple[int, ...] = (64, 256)

    def __post_init__(self) -> None:
        lower, upper = self.beta_bounds_deg
        if not all(math.isfinite(value) for value in (lower, upper)) or lower < 0.0 or lower >= upper or upper > 90.0:
            raise ValueError("control.beta_bounds_deg must satisfy 0 <= lower < upper <= 90")
        if isinstance(self.current_grid_points, bool) or self.current_grid_points < 2:
            raise ValueError("control.current_grid_points must be >= 2")
        if not math.isfinite(self.coarse_beta_step_deg) or self.coarse_beta_step_deg <= 0.0:
            raise ValueError("control.coarse_beta_step_deg must be > 0")
        if any(not math.isfinite(value) or value <= 0.0 for value in self.beta_refinement_steps_deg):
            raise ValueError("control.beta_refinement_steps_deg values must be > 0")
        if any(isinstance(value, bool) or value <= 0 for value in self.current_refinement_denominators):
            raise ValueError("control.current_refinement_denominators values must be positive integers")


@dataclass(frozen=True)
class NSGA2Spec:
    population_size: int = 160
    max_generations: int = 300
    seeds: tuple[int, ...] = (42, 43, 44)
    crossover_probability: float = 0.9
    crossover_eta: float = 15.0
    mutation_eta: float = 20.0
    max_fea_candidates: int = 12

    def __post_init__(self) -> None:
        if self.population_size < 2 or self.max_generations < 1:
            raise ValueError("nsga2 population_size must be >= 2 and max_generations must be >= 1")
        if not self.seeds or any(isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 for seed in self.seeds):
            raise ValueError("nsga2.seeds must contain nonnegative integers")
        if not 0.0 < self.crossover_probability <= 1.0:
            raise ValueError("nsga2.crossover_probability must be > 0 and <= 1")
        if self.crossover_eta <= 0.0 or self.mutation_eta <= 0.0:
            raise ValueError("nsga2 crossover_eta and mutation_eta must be > 0")
        if self.max_fea_candidates < 1:
            raise ValueError("nsga2.max_fea_candidates must be >= 1")


@dataclass(frozen=True)
class OptimizationSpec:
    schema_version: int
    operating_points: tuple[OperatingPoint, ...]
    geometry_design_space: tuple[DesignVariableBound, ...]
    stack_length_bounds: DesignVariableBound
    inverter: InverterSpec
    winding: WindingSpec
    constraints: ConstraintSpec
    beta_calibration: BetaCalibrationSpec
    control: ControlSearchSpec = field(default_factory=ControlSearchSpec)
    nsga2: NSGA2Spec = field(default_factory=NSGA2Spec)
    slot_number: int = 12
    pole_number: int = 8

    def __post_init__(self) -> None:
        if self.schema_version != SPEC_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version}; expected {SPEC_SCHEMA_VERSION}")
        if len(self.operating_points) < 2:
            raise ValueError("at least two operating_points are required")
        if len({point.name for point in self.operating_points}) != len(self.operating_points):
            raise ValueError("operating-point names must be unique")
        if not any(point.target_torque_nm is not None for point in self.operating_points):
            raise ValueError("at least one torque-target operating point is required")
        if not any(point.target_power_w is not None for point in self.operating_points):
            raise ValueError("at least one power-target operating point is required")
        if not math.isclose(sum(point.duty_weight for point in self.operating_points), 1.0, abs_tol=1e-9):
            raise ValueError("operating-point duty_weight values must sum to 1")
        if tuple(bound.name for bound in self.geometry_design_space) != GEOMETRY_VARIABLE_NAMES:
            raise ValueError("geometry_design_space must contain the canonical 15 variables in canonical order")
        if self.stack_length_bounds.name != "stack_length_mm":
            raise ValueError("stack_length_bounds name must be stack_length_mm")
        if self.slot_number != 12 or self.pole_number != 8:
            raise ValueError("schema v1 supports only the fixed 12-slot/8-pole topology")

    @property
    def design_space(self) -> tuple[DesignVariableBound, ...]:
        return (*self.geometry_design_space, self.stack_length_bounds)

    @property
    def design_variable_names(self) -> tuple[str, ...]:
        return tuple(bound.name for bound in self.design_space)

    @property
    def stack_length_bounds_mm(self) -> tuple[float, float]:
        return self.stack_length_bounds.lower, self.stack_length_bounds.upper

    @property
    def current_limit_a(self) -> float:
        return self.inverter.phase_peak_current_limit_a

    @property
    def beta_bounds_deg(self) -> tuple[float, float]:
        return self.control.beta_bounds_deg

    @property
    def phase_peak_voltage_limit_v(self) -> float:
        return self.inverter.phase_peak_voltage_limit_v

    @property
    def current_density_limited_peak_current_a(self) -> float:
        return (
            self.constraints.current_density_limit_a_per_mm2
            * self.winding.total_parallel_conductor_area_mm2
            * math.sqrt(2.0)
        )

    @property
    def effective_peak_current_limit_a(self) -> float:
        return min(self.current_limit_a, self.current_density_limited_peak_current_a)


def _parse_operating_points(raw: Any) -> tuple[OperatingPoint, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise OptimizationSpecError("operating_points must be an array")
    result: list[OperatingPoint] = []
    for index, item in enumerate(raw):
        path = f"operating_points[{index}]"
        entry = _mapping(item, path)
        name = str(_required(entry, "name", path))
        speed = _positive_number(_required(entry, "speed_rpm", path), f"{path}.speed_rpm")
        weight = _nonnegative_number(_required(entry, "duty_weight", path), f"{path}.duty_weight")
        torque = entry.get("target_torque_nm")
        power = entry.get("target_power_w")
        if torque is None and power is None:
            raise OptimizationSpecError(
                f"missing required target: {path} must define target_torque_nm or target_power_w"
            )
        if torque is not None and power is not None:
            raise OptimizationSpecError(
                f"{path} must define exactly one of target_torque_nm or target_power_w"
            )
        try:
            result.append(
                OperatingPoint(
                    name=name,
                    speed_rpm=speed,
                    duty_weight=weight,
                    target_torque_nm=_positive_number(torque, f"{path}.target_torque_nm") if torque is not None else None,
                    target_power_w=_positive_number(power, f"{path}.target_power_w") if power is not None else None,
                )
            )
        except ValueError as exc:
            raise OptimizationSpecError(str(exc)) from exc
    return tuple(result)


def _parse_design_space(raw: Any) -> tuple[DesignVariableBound, ...]:
    default_bounds = {name: (lower, upper) for name, lower, upper in DEFAULT_GEOMETRY_DESIGN_SPACE}
    if raw is not None:
        supplied = _mapping(raw, "design_space")
        unknown = sorted(set(supplied) - set(GEOMETRY_VARIABLE_NAMES))
        if unknown:
            raise OptimizationSpecError(f"design_space contains unsupported variables: {unknown}")
        for name, value in supplied.items():
            default_bounds[name] = _bounds(value, f"design_space.{name}")
    return tuple(DesignVariableBound(name, *default_bounds[name]) for name in GEOMETRY_VARIABLE_NAMES)


def _optimization_spec_from_mapping(raw: Mapping[str, Any]) -> OptimizationSpec:
    """Validate and construct an :class:`OptimizationSpec` from decoded JSON."""

    root = _mapping(raw, "spec")
    version_value = _required(root, "schema_version", "spec")
    if isinstance(version_value, bool) or not isinstance(version_value, int):
        raise OptimizationSpecError("schema_version must be an integer")
    if version_value != SPEC_SCHEMA_VERSION:
        raise OptimizationSpecError(
            f"unsupported schema_version {version_value}; expected {SPEC_SCHEMA_VERSION}"
        )

    operating_points = _parse_operating_points(_required(root, "operating_points", "spec"))
    geometry_design_space = _parse_design_space(root.get("design_space"))
    stack_lower, stack_upper = _bounds(
        _required(root, "stack_length_bounds_mm", "spec"), "stack_length_bounds_mm"
    )

    inverter_raw = _mapping(_required(root, "inverter", "spec"), "inverter")
    inverter = InverterSpec(
        vdc_v=_positive_number(_required(inverter_raw, "vdc_v", "inverter"), "inverter.vdc_v"),
        phase_peak_current_limit_a=_positive_number(
            _required(inverter_raw, "phase_peak_current_limit_a", "inverter"),
            "inverter.phase_peak_current_limit_a",
        ),
        voltage_utilization=_positive_number(
            inverter_raw.get("voltage_utilization", 0.95), "inverter.voltage_utilization"
        ),
    )

    winding_raw = _mapping(_required(root, "winding", "spec"), "winding")
    winding_required_ints = (
        "series_turns_per_phase",
        "turns_per_coil_side",
        "coils_per_phase",
        "parallel_branches",
        "strands_per_turn",
    )
    parsed_ints = {
        name: _positive_int(_required(winding_raw, name, "winding"), f"winding.{name}")
        for name in winding_required_ints
    }
    winding = WindingSpec(
        **parsed_ints,
        strand_area_mm2=_positive_number(
            _required(winding_raw, "strand_area_mm2", "winding"), "winding.strand_area_mm2"
        ),
        fill_factor=_positive_number(
            _required(winding_raw, "fill_factor", "winding"), "winding.fill_factor"
        ),
        end_turn_factor=_positive_number(
            _required(winding_raw, "end_turn_factor", "winding"), "winding.end_turn_factor"
        ),
        overhang_mm=_nonnegative_number(
            _required(winding_raw, "overhang_mm", "winding"), "winding.overhang_mm"
        ),
        copper_resistivity_20c_ohm_m=_positive_number(
            winding_raw.get("copper_resistivity_20c_ohm_m", 1.724e-8),
            "winding.copper_resistivity_20c_ohm_m",
        ),
        copper_temp_coefficient_per_c=_nonnegative_number(
            winding_raw.get("copper_temp_coefficient_per_c", 0.00393),
            "winding.copper_temp_coefficient_per_c",
        ),
        winding_temperature_c=_finite_number(
            winding_raw.get("winding_temperature_c", 100.0), "winding.winding_temperature_c"
        ),
    )

    constraints_raw = _mapping(_required(root, "constraints", "spec"), "constraints")
    constraints = ConstraintSpec(
        current_density_limit_a_per_mm2=_positive_number(
            _required(constraints_raw, "current_density_limit_a_per_mm2", "constraints"),
            "constraints.current_density_limit_a_per_mm2",
        )
    )

    beta_calibration_raw = _mapping(
        _required(root, "beta_calibration", "spec"),
        "beta_calibration",
    )
    beta_calibration = BetaCalibrationSpec(
        electrical_zero_deg=_finite_number(
            _required(beta_calibration_raw, "electrical_zero_deg", "beta_calibration"),
            "beta_calibration.electrical_zero_deg",
        ),
        calibration_id=str(
            _required(beta_calibration_raw, "calibration_id", "beta_calibration")
        ).strip(),
        convention=str(beta_calibration_raw.get("convention", BETA_CONVENTION)).strip(),
    )

    control_raw = _mapping(root.get("control", {}), "control")
    beta_bounds = _bounds(control_raw.get("beta_bounds_deg", [0.0, 80.0]), "control.beta_bounds_deg")
    beta_refinement_raw = control_raw.get("beta_refinement_steps_deg", [2.0, 0.25])
    current_refinement_raw = control_raw.get("current_refinement_denominators", [64, 256])
    if not isinstance(beta_refinement_raw, Sequence) or isinstance(beta_refinement_raw, (str, bytes)):
        raise OptimizationSpecError("control.beta_refinement_steps_deg must be an array")
    if not isinstance(current_refinement_raw, Sequence) or isinstance(current_refinement_raw, (str, bytes)):
        raise OptimizationSpecError("control.current_refinement_denominators must be an array")
    control = ControlSearchSpec(
        beta_bounds_deg=beta_bounds,
        current_grid_points=_positive_int(
            control_raw.get("current_grid_points", 33), "control.current_grid_points"
        ),
        coarse_beta_step_deg=_positive_number(
            control_raw.get("coarse_beta_step_deg", 10.0), "control.coarse_beta_step_deg"
        ),
        beta_refinement_steps_deg=tuple(
            _positive_number(value, f"control.beta_refinement_steps_deg[{index}]")
            for index, value in enumerate(beta_refinement_raw)
        ),
        current_refinement_denominators=tuple(
            _positive_int(value, f"control.current_refinement_denominators[{index}]")
            for index, value in enumerate(current_refinement_raw)
        ),
    )

    nsga_raw = _mapping(root.get("nsga2", {}), "nsga2")
    seeds_raw = nsga_raw.get("seeds", [42, 43, 44])
    if not isinstance(seeds_raw, Sequence) or isinstance(seeds_raw, (str, bytes)):
        raise OptimizationSpecError("nsga2.seeds must be an array")
    seeds = tuple(
        _nonnegative_int(seed, f"nsga2.seeds[{index}]")
        for index, seed in enumerate(seeds_raw)
    )
    nsga2 = NSGA2Spec(
        population_size=_positive_int(nsga_raw.get("population_size", 160), "nsga2.population_size"),
        max_generations=_positive_int(nsga_raw.get("max_generations", 300), "nsga2.max_generations"),
        seeds=seeds,
        crossover_probability=_positive_number(
            nsga_raw.get("crossover_probability", 0.9), "nsga2.crossover_probability"
        ),
        crossover_eta=_positive_number(nsga_raw.get("crossover_eta", 15.0), "nsga2.crossover_eta"),
        mutation_eta=_positive_number(nsga_raw.get("mutation_eta", 20.0), "nsga2.mutation_eta"),
        max_fea_candidates=_positive_int(
            nsga_raw.get("max_fea_candidates", 12), "nsga2.max_fea_candidates"
        ),
    )

    topology_raw = _mapping(root.get("topology", {}), "topology")
    slot_number = _positive_int(topology_raw.get("slot_number", 12), "topology.slot_number")
    pole_number = _positive_int(topology_raw.get("pole_number", 8), "topology.pole_number")

    try:
        return OptimizationSpec(
            schema_version=version_value,
            operating_points=operating_points,
            geometry_design_space=geometry_design_space,
            stack_length_bounds=DesignVariableBound("stack_length_mm", stack_lower, stack_upper),
            inverter=inverter,
            winding=winding,
            constraints=constraints,
            beta_calibration=beta_calibration,
            control=control,
            nsga2=nsga2,
            slot_number=slot_number,
            pole_number=pole_number,
        )
    except ValueError as exc:
        raise OptimizationSpecError(str(exc)) from exc


def optimization_spec_from_mapping(raw: Mapping[str, Any]) -> OptimizationSpec:
    """Validate and construct an :class:`OptimizationSpec` from decoded JSON."""

    try:
        return _optimization_spec_from_mapping(raw)
    except OptimizationSpecError:
        raise
    except ValueError as exc:
        raise OptimizationSpecError(str(exc)) from exc


def load_optimization_spec(path: str | Path) -> OptimizationSpec:
    """Load and strictly validate a versioned optimization JSON file."""

    spec_path = Path(path)
    try:
        decoded = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise OptimizationSpecError(f"cannot read optimization spec {spec_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OptimizationSpecError(
            f"invalid JSON in optimization spec {spec_path}: line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise OptimizationSpecError("optimization spec root must be an object")
    return optimization_spec_from_mapping(decoded)


def active_volume_m3(stator_outer_radius_mm: float, stack_length_mm: float) -> float:
    """Return the cylindrical active envelope volume in cubic metres."""

    radius = float(stator_outer_radius_mm)
    length = float(stack_length_mm)
    if not all(math.isfinite(value) and value > 0.0 for value in (radius, length)):
        raise ValueError("stator_outer_radius_mm and stack_length_mm must be finite and > 0")
    return math.pi * (radius * 1e-3) ** 2 * (length * 1e-3)


@dataclass(frozen=True)
class GeometryMetrics:
    coil_centroid_chord_mm: float
    mean_turn_length_mm: float
    slot_area_mm2: float
    slot_fill_ratio: float


def geometry_metrics(
    design: Mapping[str, float],
    stack_length_mm: float,
    winding: WindingSpec,
    *,
    slot_number: int = 12,
) -> GeometryMetrics:
    """Compute deterministic 2-D winding geometry approximations.

    The slot calculation is intentionally conservative and reproducible: the
    annular slot-pitch sector between tooth tip and yoke is reduced by the
    rectangular tooth body.  FEA remains the validation authority.
    """

    missing = [name for name in GEOMETRY_VARIABLE_NAMES if name not in design]
    if missing:
        raise ValueError(f"design is missing geometry variables: {missing}")
    values = {name: float(design[name]) for name in GEOMETRY_VARIABLE_NAMES}
    if any(not math.isfinite(value) for value in values.values()):
        raise ValueError("design geometry values must be finite")
    if stack_length_mm <= 0.0 or not math.isfinite(stack_length_mm):
        raise ValueError("stack_length_mm must be finite and > 0")

    outer = values["stator_outer_radius"]
    yoke = outer * values["stator_back_yoke_thick_ratio"]
    stator_inner = outer * values["stator_inner_ratio"]
    radial_space = outer - yoke - stator_inner
    tooth_length = radial_space * values["stator_teeth_length_ratio"]
    tooth_tip_radius = outer - yoke - tooth_length
    slot_outer_radius = outer - yoke
    pitch_angle = 2.0 * math.pi / slot_number
    tooth_width = (
        tooth_tip_radius
        * math.tan(pitch_angle / 2.0)
        * values["stator_teeth_width_ratio"]
        * 2.0
    )
    airgap_clearance = tooth_tip_radius - (stator_inner + values["stator_gap"])

    rotor_radius = stator_inner - values["rotator_gap"]
    shaft_radius = rotor_radius * values["shaft_ratio"]
    rotor_thickness = rotor_radius - shaft_radius
    magnet_setback = rotor_thickness * values["magnet_setback_ratio"]
    magnet_thickness = rotor_thickness * values["magnet_thick_ratio"]
    magnet_clearance = rotor_thickness - magnet_setback - magnet_thickness
    magnet_height = (
        (rotor_radius - magnet_setback - magnet_thickness) * math.cos(math.pi / 8.0)
        - values["magnet_shield_thick"]
    ) * values["magnet_height_ratio"]

    derived_positive = {
        "stator radial space": radial_space,
        "tooth length": tooth_length,
        "tooth tip radius": tooth_tip_radius,
        "tooth width": tooth_width,
        "airgap clearance": airgap_clearance,
        "rotor radius": rotor_radius,
        "shaft radius": shaft_radius,
        "magnet radial clearance": magnet_clearance,
        "magnet height": magnet_height,
    }
    invalid = [name for name, value in derived_positive.items() if value <= 0.0]
    if invalid:
        raise ValueError(f"design has nonpositive derived geometry: {invalid}")

    centroid_radius = 0.5 * (tooth_tip_radius + slot_outer_radius)
    chord = 2.0 * centroid_radius * math.sin(math.pi / slot_number)
    mean_turn_length = 2.0 * stack_length_mm + 2.0 * (
        winding.end_turn_factor * chord + winding.overhang_mm
    )
    pitch_sector_area = 0.5 * (slot_outer_radius**2 - tooth_tip_radius**2) * pitch_angle
    slot_area = pitch_sector_area - tooth_width * tooth_length
    if slot_area <= 0.0:
        raise ValueError("design has nonpositive approximate slot area")
    copper_per_slot = (
        2.0
        * 3.0
        * winding.series_turns_per_phase
        * winding.total_parallel_conductor_area_mm2
        / slot_number
    )
    fill = copper_per_slot / slot_area
    return GeometryMetrics(chord, mean_turn_length, slot_area, fill)


def phase_resistance_ohm(
    mean_turn_length_mm: float,
    winding: WindingSpec,
    *,
    temperature_c: float | None = None,
) -> float:
    """Return phase resistance from copper resistivity and winding dimensions."""

    if not math.isfinite(mean_turn_length_mm) or mean_turn_length_mm <= 0.0:
        raise ValueError("mean_turn_length_mm must be finite and > 0")
    temperature = winding.winding_temperature_c if temperature_c is None else float(temperature_c)
    if not math.isfinite(temperature):
        raise ValueError("temperature_c must be finite")
    rho = winding.copper_resistivity_20c_ohm_m * (
        1.0 + winding.copper_temp_coefficient_per_c * (temperature - 20.0)
    )
    if rho <= 0.0:
        raise ValueError("temperature-adjusted copper resistivity must be > 0")
    conductor_area_m2 = winding.conductor_area_per_branch_mm2 * 1e-6
    return (
        rho
        * winding.series_turns_per_phase
        * (mean_turn_length_mm * 1e-3)
        / (conductor_area_m2 * winding.parallel_branches)
    )


def phase_resistance_100c_ohm(
    design: Mapping[str, float],
    stack_length_mm: float,
    winding: WindingSpec,
    *,
    slot_number: int = 12,
) -> float:
    """Return the phase resistance at 100 degC for one design."""

    metrics = geometry_metrics(design, stack_length_mm, winding, slot_number=slot_number)
    return phase_resistance_ohm(metrics.mean_turn_length_mm, winding, temperature_c=100.0)


def current_density_a_per_mm2(current_peak_a: float, winding: WindingSpec) -> float:
    """Return RMS conductor current density assuming equal branch sharing."""

    if not math.isfinite(current_peak_a) or current_peak_a < 0.0:
        raise ValueError("current_peak_a must be finite and >= 0")
    return current_peak_a / math.sqrt(2.0) / winding.total_parallel_conductor_area_mm2


def copper_loss_w(current_peak_a: float, phase_resistance: float) -> float:
    if not math.isfinite(current_peak_a) or current_peak_a < 0.0:
        raise ValueError("current_peak_a must be finite and >= 0")
    if not math.isfinite(phase_resistance) or phase_resistance < 0.0:
        raise ValueError("phase_resistance must be finite and >= 0")
    return 1.5 * phase_resistance * current_peak_a**2


def total_loss_w(core_loss: float, solid_loss: float, copper_loss: float) -> float:
    values = (float(core_loss), float(solid_loss), float(copper_loss))
    if any(not math.isfinite(value) or value < 0.0 for value in values):
        raise ValueError("loss components must be finite and >= 0")
    return sum(values)


def mechanical_power_w(torque_nm: float, speed_rpm: float) -> float:
    values = (float(torque_nm), float(speed_rpm))
    if any(not math.isfinite(value) or value < 0.0 for value in values):
        raise ValueError("torque_nm and speed_rpm must be finite and >= 0")
    return torque_nm * speed_rpm * 2.0 * math.pi / 60.0


def efficiency_fraction(mechanical_power: float, total_loss: float) -> float:
    if not math.isfinite(mechanical_power) or mechanical_power <= 0.0:
        raise ValueError("mechanical_power must be finite and > 0")
    if not math.isfinite(total_loss) or total_loss < 0.0:
        raise ValueError("total_loss must be finite and >= 0")
    return mechanical_power / (mechanical_power + total_loss)


@dataclass(frozen=True)
class DQCurrentSeed:
    current_peak_a: float
    id_a: float
    iq_a: float
    beta_deg: float


def dq_currents(current_peak_a: float, beta_deg: float) -> tuple[float, float]:
    if not math.isfinite(current_peak_a) or current_peak_a < 0.0:
        raise ValueError("current_peak_a must be finite and >= 0")
    if not math.isfinite(beta_deg):
        raise ValueError("beta_deg must be finite")
    angle = math.radians(beta_deg)
    return -current_peak_a * math.sin(angle), current_peak_a * math.cos(angle)


def mtpa_seed(current_peak_a: float, psi_pm_wb: float, ld_h: float, lq_h: float) -> DQCurrentSeed:
    """Return the analytical IPMSM MTPA seed using the canonical beta sign."""

    values = (float(current_peak_a), float(psi_pm_wb), float(ld_h), float(lq_h))
    if any(not math.isfinite(value) for value in values):
        raise ValueError("MTPA inputs must be finite")
    current, psi_pm, ld, lq = values
    if current < 0.0 or psi_pm <= 0.0 or ld <= 0.0 or lq <= 0.0:
        raise ValueError("MTPA requires current >= 0 and psi_pm/ld/lq > 0")
    delta = lq - ld
    if current == 0.0 or abs(delta) <= 1e-15:
        id_a, iq_a = 0.0, current
    else:
        id_a = (psi_pm - math.sqrt(psi_pm**2 + 8.0 * delta**2 * current**2)) / (4.0 * delta)
        id_a = min(current, max(-current, id_a))
        iq_a = math.sqrt(max(0.0, current**2 - id_a**2))
    beta = math.degrees(math.atan2(-id_a, iq_a)) if current > 0.0 else 0.0
    return DQCurrentSeed(current, id_a, iq_a, beta)


@dataclass(frozen=True)
class PredictionEnvelope:
    torque_nm: float
    torque_lcb_nm: float
    core_loss_w: float
    core_loss_ucb_w: float
    solid_loss_w: float
    solid_loss_ucb_w: float
    voltage_peak_v: float
    voltage_peak_ucb_v: float
    in_domain: bool = True
    geometry_margin: float = math.inf
    uncertainty_score: float = 0.0

    @classmethod
    def from_mapping(cls, prediction: Mapping[str, Any]) -> "PredictionEnvelope":
        def find(names: Sequence[str], *, default: float | None = None) -> float:
            for name in names:
                if name in prediction and prediction[name] is not None:
                    try:
                        value = float(prediction[name])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"prediction field {name} must be numeric") from exc
                    if not math.isfinite(value):
                        raise ValueError(f"prediction field {name} must be finite")
                    return value
            if default is not None:
                return default
            raise ValueError(f"prediction is missing one of required fields: {list(names)}")

        torque = find(("torque_nm", "output_torque_last_avg_nm"))
        core = find(("core_loss_w", "output_coreloss_last_avg_w"))
        solid = find(("solid_loss_w", "output_solidloss_last_avg_w"))
        voltage = find(("voltage_peak_v", "phase_voltage_peak_v", "output_voltage_peak_v"))
        torque_lcb = find(("torque_lcb_nm",), default=torque)
        core_ucb = find(("core_loss_ucb_w",), default=core)
        solid_ucb = find(("solid_loss_ucb_w",), default=solid)
        voltage_ucb = find(("voltage_peak_ucb_v", "voltage_ucb_v"), default=voltage)
        if min(core, core_ucb, solid, solid_ucb, voltage, voltage_ucb) < 0.0:
            raise ValueError("predicted losses and voltage must be >= 0")
        if torque_lcb > torque:
            raise ValueError("torque_lcb_nm must be <= torque_nm")
        if core_ucb < core or solid_ucb < solid or voltage_ucb < voltage:
            raise ValueError("UCB prediction fields must be >= their point predictions")
        in_domain_raw = prediction.get("in_domain", True)
        in_domain = bool(in_domain_raw)
        geometry_margin = find(("geometry_margin",), default=math.inf)
        uncertainty = find(("uncertainty_score",), default=0.0)
        if uncertainty < 0.0:
            raise ValueError("uncertainty_score must be >= 0")
        return cls(
            torque,
            torque_lcb,
            core,
            core_ucb,
            solid,
            solid_ucb,
            voltage,
            voltage_ucb,
            in_domain,
            geometry_margin,
            uncertainty,
        )


class SurrogatePredictor(Protocol):
    def __call__(self, features: Mapping[str, Any]) -> Mapping[str, Any]: ...


class BatchSurrogatePredictor(Protocol):
    def predict_many(
        self,
        features_list: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]: ...


def build_surrogate_features(
    design: Mapping[str, float],
    operating_point: OperatingPoint,
    current_peak_a: float,
    beta_deg: float,
    phase_resistance: float,
    *,
    slot_number: int = 12,
    pole_number: int = 8,
) -> dict[str, Any]:
    """Build the stable flat input contract passed to a surrogate predictor."""

    id_a, iq_a = dq_currents(current_peak_a, beta_deg)
    features: dict[str, Any] = dict(design)
    features.update({f"input_{key}": value for key, value in design.items()})
    features.update(
        {
            "operating_point_name": operating_point.name,
            "speed_rpm": operating_point.speed_rpm,
            "base_rpm": operating_point.speed_rpm,
            "input_base_rpm": operating_point.speed_rpm,
            "current_peak_a": current_peak_a,
            "i_peak_a": current_peak_a,
            "input_i_peak_a": current_peak_a,
            "beta_deg": beta_deg,
            "input_beta_deg": beta_deg,
            "input_beta_dq_deg": beta_deg,
            "id_a": id_a,
            "iq_a": iq_a,
            "phase_resistance_ohm": phase_resistance,
            "input_phase_resistance_ohm": phase_resistance,
            "slot_num": slot_number,
            "pole_num": pole_number,
            "input_slot_num": slot_number,
            "input_pole_num": pole_number,
            "beta_convention": BETA_CONVENTION,
        }
    )
    return features


@dataclass(frozen=True)
class ControlPointResult:
    operating_point: OperatingPoint
    current_peak_a: float
    beta_deg: float
    id_a: float
    iq_a: float
    phase_resistance_ohm: float
    prediction: PredictionEnvelope
    copper_loss_w: float
    total_loss_ucb_w: float
    constraint_margins: Mapping[str, float]
    total_violation: float
    feasible: bool

    @property
    def required_power_w(self) -> float:
        return self.operating_point.required_power_w

    @property
    def efficiency(self) -> float:
        return efficiency_fraction(self.required_power_w, self.total_loss_ucb_w)


def _normalized_violation(margins: Mapping[str, float], scales: Mapping[str, float]) -> float:
    return sum(max(0.0, -margin) / max(scales.get(name, 1.0), 1e-12) for name, margin in margins.items())


def _invoke_predictor(predictor: SurrogatePredictor | Any, features: Mapping[str, Any]) -> Mapping[str, Any]:
    if hasattr(predictor, "predict_one"):
        prediction = predictor.predict_one(features)
    else:
        prediction = predictor(features)
    if not isinstance(prediction, Mapping):
        raise TypeError("surrogate predictor must return a mapping")
    return prediction


def _invoke_predictor_many(
    predictor: SurrogatePredictor | BatchSurrogatePredictor | Any,
    features_list: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if not features_list:
        return []
    batch_method = getattr(predictor, "predict_many", None)
    if not callable(batch_method):
        return [_invoke_predictor(predictor, features) for features in features_list]
    raw_predictions = batch_method(features_list)
    if isinstance(raw_predictions, (str, bytes, Mapping)):
        raise TypeError("surrogate predict_many must return an array of mappings")
    try:
        predictions = list(raw_predictions)
    except TypeError as exc:
        raise TypeError("surrogate predict_many must return an array of mappings") from exc
    if len(predictions) != len(features_list):
        raise ValueError(
            f"surrogate predict_many returned {len(predictions)} rows; expected {len(features_list)}"
        )
    if any(not isinstance(prediction, Mapping) for prediction in predictions):
        raise TypeError("every surrogate predict_many row must be a mapping")
    return predictions


def _grid(lower: float, upper: float, step: float) -> list[float]:
    values = [lower]
    index = 1
    while lower + index * step < upper - 1e-12:
        values.append(lower + index * step)
        index += 1
    values.append(upper)
    return values


def search_operating_point_control(
    design: Mapping[str, float],
    operating_point: OperatingPoint,
    spec: OptimizationSpec,
    predictor: SurrogatePredictor,
    phase_resistance: float,
    *,
    slot_fill_ratio: float,
    mtpa_parameters: Mapping[str, float] | None = None,
) -> ControlPointResult:
    """Select minimum conservative loss current/beta for one operating point.

    Feasible candidates are ranked by loss.  When none is feasible, the least
    normalized-violation candidate is returned for deterministic diagnostics
    and for a smooth-ish outer-optimization constraint signal.
    """

    beta_lower, beta_upper = spec.beta_bounds_deg
    current_limit = spec.effective_peak_current_limit_a
    currents = [current_limit * index / (spec.control.current_grid_points - 1) for index in range(spec.control.current_grid_points)]
    betas = _grid(beta_lower, beta_upper, spec.control.coarse_beta_step_deg)
    if mtpa_parameters is not None:
        seed = mtpa_seed(
            current_limit,
            float(mtpa_parameters["psi_pm_wb"]),
            float(mtpa_parameters["ld_h"]),
            float(mtpa_parameters["lq_h"]),
        )
        betas.append(min(beta_upper, max(beta_lower, seed.beta_deg)))

    cache: dict[tuple[float, float], ControlPointResult] = {}

    def normalized_pair(current: float, beta: float) -> tuple[float, float, tuple[float, float]]:
        current = min(current_limit, max(0.0, current))
        beta = min(beta_upper, max(beta_lower, beta))
        key = (round(current, 12), round(beta, 12))
        return current, beta, key

    def materialize(
        current: float,
        beta: float,
        key: tuple[float, float],
        prediction: Mapping[str, Any],
    ) -> None:
        envelope = PredictionEnvelope.from_mapping(prediction)
        p_copper = copper_loss_w(current, phase_resistance)
        loss_ucb = total_loss_w(envelope.core_loss_ucb_w, envelope.solid_loss_ucb_w, p_copper)
        predicted_power_lcb = envelope.torque_lcb_nm * operating_point.mechanical_angular_speed_rad_s
        if operating_point.target_kind == "torque":
            target_margin_name = "torque_target_nm"
            target_margin = envelope.torque_lcb_nm - operating_point.required_torque_nm
            target_scale = operating_point.required_torque_nm
        else:
            target_margin_name = "power_target_w"
            target_margin = predicted_power_lcb - operating_point.required_power_w
            target_scale = operating_point.required_power_w
        density = current_density_a_per_mm2(current, spec.winding)
        margins = {
            target_margin_name: target_margin,
            "voltage_v": spec.phase_peak_voltage_limit_v - envelope.voltage_peak_ucb_v,
            "inverter_current_a": spec.current_limit_a - current,
            "current_density_a_per_mm2": spec.constraints.current_density_limit_a_per_mm2 - density,
            "slot_fill_ratio": spec.winding.fill_factor - slot_fill_ratio,
            "in_domain": 1.0 if envelope.in_domain else -1.0,
            "geometry_margin": envelope.geometry_margin,
        }
        scales = {
            target_margin_name: target_scale,
            "voltage_v": spec.phase_peak_voltage_limit_v,
            "inverter_current_a": spec.current_limit_a,
            "current_density_a_per_mm2": spec.constraints.current_density_limit_a_per_mm2,
            "slot_fill_ratio": spec.winding.fill_factor,
            "in_domain": 1.0,
            "geometry_margin": 1.0,
        }
        violation = _normalized_violation(margins, scales)
        id_a, iq_a = dq_currents(current, beta)
        result = ControlPointResult(
            operating_point=operating_point,
            current_peak_a=current,
            beta_deg=beta,
            id_a=id_a,
            iq_a=iq_a,
            phase_resistance_ohm=phase_resistance,
            prediction=envelope,
            copper_loss_w=p_copper,
            total_loss_ucb_w=loss_ucb,
            constraint_margins=margins,
            total_violation=violation,
            feasible=violation <= 1e-12,
        )
        cache[key] = result

    def evaluate_many(pairs: Iterable[tuple[float, float]]) -> None:
        pending: list[tuple[float, float, tuple[float, float]]] = []
        pending_keys: set[tuple[float, float]] = set()
        for current, beta in pairs:
            normalized = normalized_pair(current, beta)
            key = normalized[2]
            if key in cache or key in pending_keys:
                continue
            pending.append(normalized)
            pending_keys.add(key)
        if not pending:
            return
        feature_rows = [
            build_surrogate_features(
                design,
                operating_point,
                current,
                beta,
                phase_resistance,
                slot_number=spec.slot_number,
                pole_number=spec.pole_number,
            )
            for current, beta, _ in pending
        ]
        predictions = _invoke_predictor_many(predictor, feature_rows)
        for (current, beta, key), prediction in zip(pending, predictions):
            materialize(current, beta, key, prediction)

    def best_result() -> ControlPointResult:
        return min(
            cache.values(),
            key=lambda item: (
                0 if item.feasible else 1,
                item.total_loss_ucb_w if item.feasible else item.total_violation,
                item.total_violation,
                item.total_loss_ucb_w,
                item.current_peak_a,
                item.beta_deg,
            ),
        )

    evaluate_many(
        (current, beta)
        for current in currents
        for beta in sorted(set(betas))
    )

    refinement_count = max(
        len(spec.control.beta_refinement_steps_deg),
        len(spec.control.current_refinement_denominators),
    )
    for index in range(refinement_count):
        best = best_result()
        beta_step = (
            spec.control.beta_refinement_steps_deg[index]
            if index < len(spec.control.beta_refinement_steps_deg)
            else 0.0
        )
        current_step = (
            current_limit / spec.control.current_refinement_denominators[index]
            if index < len(spec.control.current_refinement_denominators)
            else 0.0
        )
        local_currents = {best.current_peak_a}
        local_betas = {best.beta_deg}
        if current_step:
            local_currents.update((best.current_peak_a - current_step, best.current_peak_a + current_step))
        if beta_step:
            local_betas.update((best.beta_deg - beta_step, best.beta_deg + beta_step))
        evaluate_many(
            (current, beta)
            for current in sorted(local_currents)
            for beta in sorted(local_betas)
        )
    return best_result()


def duty_weighted_cycle_efficiency(results: Iterable[ControlPointResult]) -> float:
    rows = tuple(results)
    if not rows:
        raise ValueError("at least one control result is required")
    weight_sum = sum(row.operating_point.duty_weight for row in rows)
    if not math.isclose(weight_sum, 1.0, abs_tol=1e-9):
        raise ValueError("control-result duty weights must sum to 1")
    numerator = sum(row.operating_point.duty_weight * row.required_power_w for row in rows)
    denominator = sum(
        row.operating_point.duty_weight * (row.required_power_w + row.total_loss_ucb_w)
        for row in rows
    )
    if numerator <= 0.0 or denominator <= 0.0:
        raise ValueError("weighted required power and input power must be > 0")
    return numerator / denominator


@dataclass(frozen=True)
class OptimizationCandidate:
    candidate_id: str
    design: Mapping[str, float]
    active_volume_m3: float
    cycle_efficiency: float
    phase_resistance_ohm: float
    slot_fill_ratio: float
    control_results: tuple[ControlPointResult, ...]
    constraint_violations: tuple[float, ...]
    feasible: bool
    seed: int | None = None

    @property
    def objectives(self) -> tuple[float, float]:
        return self.active_volume_m3, 1.0 - self.cycle_efficiency

    @property
    def max_uncertainty_score(self) -> float:
        return max((row.prediction.uncertainty_score for row in self.control_results), default=math.inf)

    @property
    def total_constraint_violation(self) -> float:
        return sum(max(0.0, value) for value in self.constraint_violations)


SeedParameterProvider = Callable[[Mapping[str, float], OperatingPoint], Mapping[str, float] | None]


def evaluate_design_candidate(
    design: Mapping[str, float],
    spec: OptimizationSpec,
    predictor: SurrogatePredictor,
    *,
    candidate_id: str = "candidate",
    seed: int | None = None,
    seed_parameter_provider: SeedParameterProvider | None = None,
) -> OptimizationCandidate:
    """Evaluate geometry, inner controls, objectives and conservative constraints."""

    missing = [bound.name for bound in spec.design_space if bound.name not in design]
    if missing:
        raise ValueError(f"design is missing variables: {missing}")
    normalized = {bound.name: float(design[bound.name]) for bound in spec.design_space}
    outside = [
        bound.name
        for bound in spec.design_space
        if normalized[bound.name] < bound.lower or normalized[bound.name] > bound.upper
    ]
    if outside:
        raise ValueError(f"design variables are outside configured bounds: {outside}")
    stack = normalized["stack_length_mm"]
    metrics = geometry_metrics(normalized, stack, spec.winding, slot_number=spec.slot_number)
    resistance = phase_resistance_ohm(metrics.mean_turn_length_mm, spec.winding, temperature_c=100.0)
    controls = tuple(
        search_operating_point_control(
            normalized,
            point,
            spec,
            predictor,
            resistance,
            slot_fill_ratio=metrics.slot_fill_ratio,
            mtpa_parameters=(seed_parameter_provider(normalized, point) if seed_parameter_provider else None),
        )
        for point in spec.operating_points
    )
    cycle_efficiency = duty_weighted_cycle_efficiency(controls)
    violations = tuple(row.total_violation for row in controls)
    return OptimizationCandidate(
        candidate_id=candidate_id,
        design=normalized,
        active_volume_m3=active_volume_m3(normalized["stator_outer_radius"], stack),
        cycle_efficiency=cycle_efficiency,
        phase_resistance_ohm=resistance,
        slot_fill_ratio=metrics.slot_fill_ratio,
        control_results=controls,
        constraint_violations=violations,
        feasible=all(row.feasible for row in controls),
        seed=seed,
    )


def dominates(left: OptimizationCandidate, right: OptimizationCandidate, *, tolerance: float = 1e-12) -> bool:
    """Return whether ``left`` Pareto-dominates ``right`` with feasibility first."""

    if left.feasible != right.feasible:
        return left.feasible
    if not left.feasible:
        return left.total_constraint_violation < right.total_constraint_violation - tolerance
    left_obj = left.objectives
    right_obj = right.objectives
    no_worse = all(a <= b + tolerance for a, b in zip(left_obj, right_obj))
    strictly_better = any(a < b - tolerance for a, b in zip(left_obj, right_obj))
    return no_worse and strictly_better


def nondominated_candidates(candidates: Iterable[OptimizationCandidate]) -> list[OptimizationCandidate]:
    rows = list(candidates)
    front = [row for index, row in enumerate(rows) if not any(dominates(other, row) for j, other in enumerate(rows) if j != index)]
    return sorted(
        front,
        key=lambda row: (
            not row.feasible,
            row.total_constraint_violation,
            row.active_volume_m3,
            -row.cycle_efficiency,
            row.candidate_id,
        ),
    )


def select_validation_candidates(
    candidates: Iterable[OptimizationCandidate], max_candidates: int = 12
) -> list[OptimizationCandidate]:
    """Select deterministic, volume-spread feasible FEA candidates from a Pareto front."""

    if max_candidates < 1:
        raise ValueError("max_candidates must be >= 1")
    rows = [row for row in nondominated_candidates(candidates) if row.feasible]
    if not rows:
        return []
    if len(rows) <= max_candidates:
        return rows
    rows.sort(key=lambda row: (row.active_volume_m3, -row.cycle_efficiency, row.candidate_id))
    selected_indices = {
        round(index * (len(rows) - 1) / (max_candidates - 1))
        for index in range(max_candidates)
    } if max_candidates > 1 else {min(range(len(rows)), key=lambda idx: 1.0 - rows[idx].cycle_efficiency)}
    return [rows[index] for index in sorted(selected_indices)]
