"""Batch runner for the IPMSM Maxwell 2D transient workflow.

The notebook is useful for interactive checks, but production runs should use
this script so each case starts from a fresh Python process/import state.

Examples
--------
Setup-only smoke test:
    python run_ipmsm_batch.py --count 1 --setup-only

Run one solved case:
    python run_ipmsm_batch.py --count 1 --analyze

Run a CSV sweep with two parallel AEDT workers:
    python run_ipmsm_batch.py --cases ipmsm_cases.csv --workers 2 --analyze
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import logging
import math
import multiprocessing as mp
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any


BASE_DIR = Path(__file__).resolve().parent


def add_local_library_paths() -> None:
    """Add the local pyaedt_library path used by this project."""
    if os.name == "nt":
        candidates = [Path("Y:/git/pyaedt_library/src")]
    else:
        candidates = [
            BASE_DIR.parent / "pyaedt_library" / "src",
            BASE_DIR.parent / "git" / "pyaedt_library" / "src",
            Path("/home1/r1jae262/jupyter/git/pyaedt_library/src"),
            Path("/home1/dhj02/NEC/git/pyaedt_library/src"),
            Path("/home1/dw16/NEC/git/pyaedt_library/src"),
            Path("/home1/harry261/NEC/git/pyaedt_library/src"),
            Path("/home1/hmlee31/NEC/git/pyaedt_library/src"),
            Path("/home1/jji0930/NEC/git/pyaedt_library/src"),
            Path("/home1/wjddn5916/NEC/git/pyaedt_library/src"),
        ]
    for candidate in candidates:
        if candidate.exists():
            sys.path.insert(0, str(candidate))
            return


add_local_library_paths()

REPORT_NAMES = (
    "PPT_Phase_Currents",
    "PPT_Torque",
    "PPT_Cogging_Torque",
    "PPT_Back_EMF",
    "PPT_Inductance_Matrix",
    "PPT_PhaseA_Voltage_Limit",
    "PPT_Phase_Voltages",
    "PPT_Losses",
)

TIME_DOMAIN_WINDOWS = ("first", "last", "all")
TIME_DOMAIN_STATS = ("avg", "rms", "min", "max", "peak_abs", "peak_signed")
BACK_EMF_HARMONICS = (1, 3, 5, 7, 9, 11, 13)
INDUCTANCE_MATRIX_ENTRIES = (
    ("aa", "PhaseA", "PhaseA"),
    ("ab", "PhaseA", "PhaseB"),
    ("ac", "PhaseA", "PhaseC"),
    ("ba", "PhaseB", "PhaseA"),
    ("bb", "PhaseB", "PhaseB"),
    ("bc", "PhaseB", "PhaseC"),
    ("ca", "PhaseC", "PhaseA"),
    ("cb", "PhaseC", "PhaseB"),
    ("cc", "PhaseC", "PhaseC"),
)
TIME_DOMAIN_OUTPUT_METRICS = (
    ("torque", "nm"),
    ("cogging_torque", "nm"),
    ("coreloss", "w"),
    ("solidloss", "w"),
    ("phasea_current", "a"),
    ("phaseb_current", "a"),
    ("phasec_current", "a"),
    ("phase_current", "a"),
    ("back_emf_phasea", "v"),
    ("back_emf_phaseb", "v"),
    ("back_emf_phasec", "v"),
    ("back_emf_phase", "v"),
    *(("inductance_" + key, "h") for key, _, _ in INDUCTANCE_MATRIX_ENTRIES),
    ("ld", "h"),
    ("lq", "h"),
    ("saliency_ratio", "ratio"),
    ("phasea_voltage", "v"),
    ("phaseb_voltage", "v"),
    ("phasec_voltage", "v"),
    ("phase_voltage", "v"),
)
DERIVED_OUTPUT_COLUMNS = (
    "output_phase_voltage_limit_spwm_v",
    "output_phase_voltage_limit_svpwm_v",
    *(f"output_torque_{window}_ripple_pct" for window in TIME_DOMAIN_WINDOWS),
    *(f"output_copperloss_{window}_avg_w" for window in TIME_DOMAIN_WINDOWS),
    *(f"output_total_loss_{window}_avg_w" for window in TIME_DOMAIN_WINDOWS),
    *(f"output_mech_power_{window}_w" for window in TIME_DOMAIN_WINDOWS),
    *(f"output_efficiency_{window}_pct" for window in TIME_DOMAIN_WINDOWS),
    *(f"output_voltage_margin_{window}_v" for window in TIME_DOMAIN_WINDOWS),
    *(f"output_voltage_spwm_margin_{window}_v" for window in TIME_DOMAIN_WINDOWS),
    *(f"output_voltage_svpwm_margin_{window}_v" for window in TIME_DOMAIN_WINDOWS),
)
HARMONIC_OUTPUT_COLUMNS = (
    *(f"output_back_emf_phasea_h{harmonic}_rms_v" for harmonic in BACK_EMF_HARMONICS),
    *(f"output_back_emf_phasea_h{harmonic}_pct" for harmonic in BACK_EMF_HARMONICS),
    "output_back_emf_phasea_thd_pct",
)
OUTPUT_SUMMARY_COLUMNS = (
    "output_electric_frequency_hz",
    "output_period_s",
    "output_stop_time_s",
    *(
        f"output_{metric}_{window}_{stat}_{unit}"
        for metric, unit in TIME_DOMAIN_OUTPUT_METRICS
        for window in TIME_DOMAIN_WINDOWS
        for stat in TIME_DOMAIN_STATS
    ),
    *HARMONIC_OUTPUT_COLUMNS,
    *DERIVED_OUTPUT_COLUMNS,
)

ARTIFACT_COLUMNS = tuple(f"artifact_report_{name}" for name in REPORT_NAMES)

INPUT_SPEC_COLUMNS = (
    "input_pole_number",
    "input_slot_number",
    "input_symmetry_factor",
    "input_base_rpm",
    "input_i_peak_a",
    "input_beta_deg",
    "input_series_turns_per_phase",
    "input_turns_per_coil_side",
    "input_stack_length_mm",
    "input_phase_resistance_ohm",
    "input_vdc_v",
    "input_initial_position_deg",
    "input_transient_periods",
    "input_steps_per_period",
    "input_core_material",
    "input_core_material_fallbacks",
    "input_magnet_material",
    "input_winding_material",
    "input_shaft_material",
    "input_air_material",
    "input_setup_name",
    "input_mesh_elements",
)

INPUT_GEOMETRY_COLUMNS = (
    "input_slot_num",
    "input_pole_num",
    "input_stator_outer_radius",
    "input_stator_back_yoke_thick_ratio",
    "input_stator_back_yoke_thick",
    "input_stator_inner_ratio",
    "input_stator_inner_radius",
    "input_stator_shoe_thick",
    "input_stator_teeth_length_ratio",
    "input_stator_teeth_length",
    "input_stator_teeth_width",
    "input_stator_gap",
    "input_rotator_gap",
    "input_shaft_ratio",
    "input_rotor_radius",
    "input_shaft_radius",
    "input_magnet_shield_thick",
    "input_magnet_setback_ratio",
    "input_magnet_thick_ratio",
    "input_magnet_height_ratio",
)

INPUT_CORE_COLUMNS = (
    "input_coreloss_Kh",
    "input_coreloss_Kc",
    "input_coreloss_Ke",
    "input_coreloss_Y",
    "input_coreloss_Kdc",
    "input_core_resistivity_ohm_m",
    "input_core_conductivity_s_per_m",
    "input_core_mass_density_kg_per_m3",
    "input_core_bh_curve_points",
    "input_core_bh_curve_bmax_t",
    "input_core_bh_curve_hmax_a_per_m",
)

INPUT_RUN_COLUMNS = (
    "input_use_periodic_boundary",
    "input_operation",
)

RUN_METADATA_COLUMNS = (
    "error",
    "simulation_name",
    "project_path",
    "analyze",
    "analysis_returned_false",
    "validation",
    "elapsed_s",
    "finished_at",
)

RESULT_COLUMN_ORDER = (
    "case_id",
    "status",
    "started_at",
    *INPUT_SPEC_COLUMNS,
    *INPUT_GEOMETRY_COLUMNS,
    *INPUT_CORE_COLUMNS,
    *INPUT_RUN_COLUMNS,
    *OUTPUT_SUMMARY_COLUMNS,
    *ARTIFACT_COLUMNS,
    *RUN_METADATA_COLUMNS,
)


@dataclass(frozen=True)
class RunnerOptions:
    simulation_dir: str
    result_csv: str
    analyze: bool
    non_graphical: bool
    cleanup_linux: bool
    symmetry_factor: int
    use_periodic_boundary: bool
    cores: int


class Simulation:
    """Small state holder compatible with module.variable.set_variable."""

    def __init__(self, desktop: Any, cores: int = 4) -> None:
        self.NUM_CORE = cores
        self.NUM_TASK = 1
        self.desktop = desktop
        self.PROJECT_NAME = ""
        self.project = None
        self.num = 0

    def create_simulation_name(self, simulation_dir: Path) -> None:
        simulation_dir.mkdir(parents=True, exist_ok=True)
        counter_path = BASE_DIR / "simulation_num.txt"
        with locked_text_file(counter_path, "a+") as file:
            file.seek(0)
            raw = file.read().strip()
            if raw.isdigit():
                current = int(raw)
            else:
                current = next_simulation_number(simulation_dir)

            self.num = current
            self.PROJECT_NAME = f"simulation{current}"

            file.seek(0)
            file.truncate()
            file.write(str(current + 1))
            file.flush()
            os.fsync(file.fileno())

    def create_project(self, simulation_dir: Path) -> Any:
        if self.desktop is None:
            raise RuntimeError("Desktop instance is None. Cannot create project.")

        project_path = simulation_dir / self.PROJECT_NAME
        project_path.mkdir(parents=True, exist_ok=True)
        self.project = self.desktop.create_project(path=str(project_path), name=self.PROJECT_NAME)
        return self.project

    def set_variable(self, design: Any) -> Any:
        from module.variable import set_variable

        return set_variable(self, design)


@contextlib.contextmanager
def locked_text_file(path: Path, mode: str):
    """Open and lock a text file using only the Python standard library."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8", newline="") as file:
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield file
            finally:
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                yield file
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def next_simulation_number(simulation_dir: Path) -> int:
    existing = []
    for child in simulation_dir.glob("simulation*"):
        suffix = child.name.removeprefix("simulation")
        if suffix.isdigit():
            existing.append(int(suffix))
    return max(existing, default=0) + 1


def case_value(case: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = case.get(name)
        if value not in (None, ""):
            return value
    return default


def case_float(case: dict[str, Any], *names: str, default: float) -> float:
    return float(case_value(case, *names, default=default))


def case_int(case: dict[str, Any], *names: str, default: int) -> int:
    return int(float(case_value(case, *names, default=default)))


def case_bool(case: dict[str, Any], *names: str, default: bool = False) -> bool:
    value = case_value(case, *names, default=default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def build_spec(case: dict[str, Any], default_symmetry_factor: int = 4) -> Any:
    """Build the PPT setup spec from one CSV/default case row."""
    from module.ipmsm_ppt_setup import IPMSMPPTSpec

    defaults = IPMSMPPTSpec()
    return IPMSMPPTSpec(
        pole_number=case_int(case, "pole_number", "pole_num", default=8),
        slot_number=case_int(case, "slot_number", "slot_num", default=12),
        symmetry_factor=case_int(case, "symmetry_factor", default=default_symmetry_factor),
        base_rpm=case_float(case, "base_rpm", "rpm", default=1200.0),
        i_peak_a=case_float(case, "i_peak_a", "i_peak", "current_peak_a", default=137.8),
        beta_deg=case_float(case, "beta_deg", "beta", default=0.0),
        series_turns_per_phase=case_int(case, "series_turns_per_phase", default=48),
        turns_per_coil_side=case_int(case, "turns_per_coil_side", default=12),
        stack_length_mm=case_float(case, "stack_length_mm", default=49.45),
        phase_resistance_ohm=case_float(case, "phase_resistance_ohm", "r_phase", default=0.01),
        vdc_v=case_float(case, "vdc_v", "vdc", default=200.0),
        initial_position_deg=case_float(case, "initial_position_deg", default=-22.5),
        transient_periods=case_int(case, "transient_periods", "periods", default=10),
        steps_per_period=case_int(case, "steps_per_period", default=90),
        core_material=str(case_value(case, "core_material", default=defaults.core_material)),
        magnet_material=str(case_value(case, "magnet_material", default=defaults.magnet_material)),
    )


def load_cases(path: str | None, count: int) -> list[dict[str, Any]]:
    if path:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
            return [dict(row) for row in csv.DictReader(file)]
    return [{"case_id": f"case_{idx:04d}"} for idx in range(1, count + 1)]


def dataframe_first_row(df: Any) -> dict[str, Any]:
    if df is None:
        return {}
    if hasattr(df, "iloc") and len(df) > 0:
        return df.iloc[0].to_dict()
    return {}


def normalize_csv_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def prefixed_row(data: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {f"{prefix}{key}": normalize_csv_value(value) for key, value in data.items()}


def initialize_result_row_schema(row: dict[str, Any]) -> None:
    """Keep the shared CSV header stable even if the first case fails early."""
    for column in RESULT_COLUMN_ORDER:
        row.setdefault(column, "")


def ordered_result_fieldnames(existing_header: list[str], row: dict[str, Any]) -> list[str]:
    """Return a deterministic CSV header while preserving unknown sweep columns."""
    fieldnames: list[str] = []
    for column in RESULT_COLUMN_ORDER:
        if column in existing_header or column in row:
            fieldnames.append(column)
    for source in (existing_header, list(row)):
        for column in source:
            if column not in fieldnames:
                fieldnames.append(column)
    return fieldnames


def append_result_row(result_csv: Path, row: dict[str, Any]) -> None:
    """Append one row to the shared result CSV with a process-safe lock."""
    initialize_result_row_schema(row)
    lock_path = result_csv.with_suffix(result_csv.suffix + ".lock")
    result_csv.parent.mkdir(parents=True, exist_ok=True)
    with locked_text_file(lock_path, "a+") as _lock:
        file_exists = result_csv.exists() and result_csv.stat().st_size > 0
        existing_header = []
        existing_rows = []
        if file_exists:
            with result_csv.open("r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)
                existing_header = reader.fieldnames or []
                existing_rows = list(reader)

        fieldnames = ordered_result_fieldnames(existing_header, row)

        if file_exists and fieldnames != existing_header:
            with result_csv.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(existing_rows)
            file_exists = True

        with result_csv.open("a", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)


TIME_UNITS_TO_SECONDS = {
    "fs": 1e-15,
    "ps": 1e-12,
    "ns": 1e-9,
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "": 1.0,
}

POWER_UNITS_TO_WATTS = {
    "W": 1.0,
    "w": 1.0,
    "kW": 1e3,
    "kw": 1e3,
    "KW": 1e3,
    "MW": 1e6,
    "mw": 1e-3,
    "mW": 1e-3,
    "": 1.0,
}

CURRENT_UNITS_TO_AMPERES = {
    "A": 1.0,
    "amp": 1.0,
    "Amp": 1.0,
    "mA": 1e-3,
    "kA": 1e3,
    "": 1.0,
}

VOLTAGE_UNITS_TO_VOLTS = {
    "V": 1.0,
    "v": 1.0,
    "mV": 1e-3,
    "kV": 1e3,
    "": 1.0,
}

INDUCTANCE_UNITS_TO_HENRY = {
    "H": 1.0,
    "h": 1.0,
    "mH": 1e-3,
    "uH": 1e-6,
    "µH": 1e-6,
    "nH": 1e-9,
    "pH": 1e-12,
    "": 1.0,
}

TORQUE_UNITS_TO_NM = {
    "NewtonMeter": 1.0,
    "Newton Meter": 1.0,
    "N*m": 1.0,
    "N m": 1.0,
    "Nm": 1.0,
    "": 1.0,
}


def extract_column_unit(column: str) -> str:
    """Return the unit in an AEDT CSV column name, for example ``Time [ms]`` -> ``ms``."""
    match = re.search(r"\[([^\]]*)\]", str(column))
    return match.group(1).strip() if match else ""


def parse_number_with_unit(value: Any) -> tuple[float, str]:
    """Parse values like ``1.2ms`` while keeping plain numeric AEDT CSV cells fast."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), ""
    text = str(value).strip()
    if not text:
        return math.nan, ""
    match = re.match(r"^([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([^\d\s]*)\s*$", text)
    if match:
        return float(match.group(1)), match.group(2).strip()
    return float(text), ""


def parse_time_seconds(value: Any, default_unit: str = "") -> float:
    number, unit = parse_number_with_unit(value)
    unit = unit or default_unit
    return number * TIME_UNITS_TO_SECONDS.get(unit, 1.0)


def unit_scale_to_base(unit: str, unit_suffix: str) -> float:
    """Scale AEDT report values to the output unit implied by ``unit_suffix``."""
    if unit_suffix == "w":
        return POWER_UNITS_TO_WATTS.get(unit, 1.0)
    if unit_suffix == "nm":
        return TORQUE_UNITS_TO_NM.get(unit, 1.0)
    if unit_suffix == "a":
        return CURRENT_UNITS_TO_AMPERES.get(unit, 1.0)
    if unit_suffix == "v":
        return VOLTAGE_UNITS_TO_VOLTS.get(unit, 1.0)
    if unit_suffix == "h":
        return INDUCTANCE_UNITS_TO_HENRY.get(unit, 1.0)
    return 1.0


def parse_report_value(value: Any, default_unit: str, unit_suffix: str) -> float:
    number, unit = parse_number_with_unit(value)
    unit = unit or default_unit
    return number * unit_scale_to_base(unit, unit_suffix)


def find_column(columns: list[str], tokens: tuple[str, ...]) -> str | None:
    lowered_tokens = tuple(token.lower() for token in tokens)
    for column in columns:
        text = column.lower()
        if all(token in text for token in lowered_tokens):
            return column
    return None


def non_time_columns(columns: list[str]) -> list[str]:
    """Return report data columns after the sweep/time column.

    AEDT exported CSVs prepend design-variation columns before ``Time``.  Using
    every non-time column as a fallback can accidentally summarize inputs such
    as ``BaseRPM`` as if they were transient results.
    """
    for index, column in enumerate(columns):
        if "time" in column.lower():
            return list(columns[index + 1 :])
    return [column for column in columns if "time" not in column.lower()]


def read_report_csv(path: str) -> Any:
    import pandas as pd

    with open(path, "r", encoding="utf-8-sig", errors="ignore") as fp:
        header = fp.readline()
    delimiter = ";" if header.count(";") > header.count(",") else ","
    return pd.read_csv(path, sep=delimiter)


def series_stats(values: Any) -> dict[str, float]:
    finite = [float(value) for value in values if value is not None and not math.isnan(float(value))]
    if not finite:
        return {
            "avg": math.nan,
            "rms": math.nan,
            "min": math.nan,
            "max": math.nan,
            "peak_abs": math.nan,
            "peak_signed": math.nan,
        }
    peak_signed = max(finite, key=lambda value: abs(value))
    return {
        "avg": sum(finite) / len(finite),
        "rms": math.sqrt(sum(value * value for value in finite) / len(finite)),
        "min": min(finite),
        "max": max(finite),
        "peak_abs": abs(peak_signed),
        "peak_signed": peak_signed,
    }


def summarize_metric(
    df: Any,
    value_column: str,
    output_prefix: str,
    period_s: float,
    stop_s: float,
    unit_suffix: str,
) -> dict[str, float]:
    time_column = find_column(list(df.columns), ("time",)) or df.columns[0]
    time_unit = extract_column_unit(time_column)
    value_unit = extract_column_unit(value_column)
    time_s = df[time_column].map(lambda value: parse_time_seconds(value, time_unit))
    values = df[value_column].map(lambda value: parse_report_value(value, value_unit, unit_suffix))
    eps = max(period_s, stop_s, 1.0) * 1e-9

    windows = {
        "first": (time_s >= -eps) & (time_s <= period_s + eps),
        "last": (time_s >= stop_s - period_s - eps) & (time_s <= stop_s + eps),
        "all": [True] * len(df),
    }

    result: dict[str, float] = {}
    for window_name, mask in windows.items():
        selected = values[mask]
        if len(selected) == 0:
            selected = values
        stats = series_stats(selected)
        for stat in TIME_DOMAIN_STATS:
            result[f"{output_prefix}_{window_name}_{stat}_{unit_suffix}"] = stats[stat]
    return result


def report_time_value_pairs(df: Any, value_column: str, unit_suffix: str) -> list[tuple[float, float]]:
    """Return finite ``(time_s, value_base_unit)`` pairs from an exported AEDT report."""
    time_column = find_column(list(df.columns), ("time",)) or df.columns[0]
    time_unit = extract_column_unit(time_column)
    value_unit = extract_column_unit(value_column)
    pairs: list[tuple[float, float]] = []
    for raw_time, raw_value in zip(df[time_column], df[value_column]):
        time_s = parse_time_seconds(raw_time, time_unit)
        value = parse_report_value(raw_value, value_unit, unit_suffix)
        if math.isfinite(time_s) and math.isfinite(value):
            pairs.append((time_s, value))
    return pairs


def summarize_last_cycle_harmonics(
    df: Any,
    value_column: str,
    output_prefix: str,
    fundamental_hz: float,
    period_s: float,
    stop_s: float,
    unit_suffix: str,
) -> dict[str, float]:
    """Estimate last-cycle RMS harmonics using sine/cosine projection.

    The PPT analysis compares selected Back EMF harmonics.  AEDT exports the
    waveform as a time trace, so this keeps the post-process local and avoids
    depending on a separate FFT report object.
    """
    pairs = report_time_value_pairs(df, value_column, unit_suffix)
    if not pairs or not math.isfinite(fundamental_hz) or fundamental_hz <= 0:
        return {}

    eps = max(period_s, stop_s, 1.0) * 1e-9
    selected = [
        (time_s, value)
        for time_s, value in pairs
        if stop_s - period_s - eps <= time_s <= stop_s + eps
    ]
    if len(selected) < 3:
        selected = pairs

    omega = 2.0 * math.pi * fundamental_hz
    count = float(len(selected))
    result: dict[str, float] = {}
    harmonic_rms: dict[int, float] = {}
    for harmonic in BACK_EMF_HARMONICS:
        cos_sum = sum(value * math.cos(harmonic * omega * time_s) for time_s, value in selected)
        sin_sum = sum(value * math.sin(harmonic * omega * time_s) for time_s, value in selected)
        peak = 2.0 * math.sqrt(cos_sum * cos_sum + sin_sum * sin_sum) / count
        rms = peak / math.sqrt(2.0)
        harmonic_rms[harmonic] = rms
        result[f"{output_prefix}_h{harmonic}_rms_{unit_suffix}"] = rms

    fundamental_rms = harmonic_rms.get(1, math.nan)
    for harmonic, rms in harmonic_rms.items():
        result[f"{output_prefix}_h{harmonic}_pct"] = safe_divide(rms, fundamental_rms) * 100.0

    thd_rms = math.sqrt(sum(rms * rms for harmonic, rms in harmonic_rms.items() if harmonic != 1))
    result[f"{output_prefix}_thd_pct"] = safe_divide(thd_rms, fundamental_rms) * 100.0
    return result


def find_inductance_matrix_column(columns: list[str], source: str, target: str) -> str | None:
    """Find an AEDT matrix column like ``L(PhaseA,PhaseB)`` regardless of unit suffixes."""
    needle = f"l({source.lower()},{target.lower()})"
    for column in columns:
        compact = str(column).lower().replace(" ", "")
        if needle in compact:
            return column
    return None


def summarize_inductance_matrix(df: Any, spec: Any, period_s: float, stop_s: float) -> dict[str, float]:
    """Summarize abc inductance entries and approximate D/Q inductance traces."""
    result: dict[str, float] = {}
    columns = list(df.columns)
    matrix_columns: dict[str, str] = {}
    for key, source, target in INDUCTANCE_MATRIX_ENTRIES:
        column = find_inductance_matrix_column(columns, source, target)
        if not column:
            continue
        matrix_columns[key] = column
        result.update(summarize_metric(df, column, f"output_inductance_{key}", period_s, stop_s, "h"))

    if len(matrix_columns) != len(INDUCTANCE_MATRIX_ENTRIES):
        return result

    time_column = find_column(columns, ("time",)) or df.columns[0]
    time_unit = extract_column_unit(time_column)
    omega_mech = 2.0 * math.pi * float(spec.base_rpm) / 60.0
    pole_pairs = float(spec.pole_number) / 2.0
    initial_mech_rad = math.radians(float(spec.initial_position_deg))

    ld_values: list[float] = []
    lq_values: list[float] = []
    saliency_values: list[float] = []
    for index in range(len(df)):
        time_s = parse_time_seconds(df[time_column].iloc[index], time_unit)
        values: dict[str, float] = {}
        for key, column in matrix_columns.items():
            unit = extract_column_unit(column)
            values[key] = parse_report_value(df[column].iloc[index], unit, "h")
        if not math.isfinite(time_s) or not all(math.isfinite(value) for value in values.values()):
            ld_values.append(math.nan)
            lq_values.append(math.nan)
            saliency_values.append(math.nan)
            continue

        labc = [
            [values["aa"], values["ab"], values["ac"]],
            [values["ba"], values["bb"], values["bc"]],
            [values["ca"], values["cb"], values["cc"]],
        ]
        theta = pole_pairs * (initial_mech_rad + omega_mech * time_s)
        angles = (theta, theta - 2.0 * math.pi / 3.0, theta + 2.0 * math.pi / 3.0)

        park = [
            [(2.0 / 3.0) * math.cos(angle) for angle in angles],
            [-(2.0 / 3.0) * math.sin(angle) for angle in angles],
        ]
        inverse_park = [
            [math.cos(angle), -math.sin(angle)]
            for angle in angles
        ]

        ld = sum(park[0][i] * labc[i][j] * inverse_park[j][0] for i in range(3) for j in range(3))
        lq = sum(park[1][i] * labc[i][j] * inverse_park[j][1] for i in range(3) for j in range(3))
        ld_values.append(ld)
        lq_values.append(lq)
        saliency_values.append(safe_divide(lq, ld))

    derived = df[[time_column]].copy()
    derived["Ld [H]"] = ld_values
    derived["Lq [H]"] = lq_values
    derived["SaliencyRatio"] = saliency_values
    result.update(summarize_metric(derived, "Ld [H]", "output_ld", period_s, stop_s, "h"))
    result.update(summarize_metric(derived, "Lq [H]", "output_lq", period_s, stop_s, "h"))
    result.update(summarize_metric(derived, "SaliencyRatio", "output_saliency_ratio", period_s, stop_s, "ratio"))
    return result


def finite_float(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return math.nan
    return number if math.isfinite(number) else math.nan


def safe_divide(numerator: float, denominator: float) -> float:
    if not math.isfinite(numerator) or not math.isfinite(denominator) or abs(denominator) < 1e-30:
        return math.nan
    return numerator / denominator


def summarize_phase_envelope(
    output_summary: dict[str, Any],
    phase_prefixes: tuple[str, ...],
    aggregate_prefix: str,
    unit_suffix: str,
) -> None:
    """Summarize the three-phase envelope for RMS and peak-style constraints."""
    for window in TIME_DOMAIN_WINDOWS:
        for stat in TIME_DOMAIN_STATS:
            values = [
                finite_float(output_summary.get(f"{prefix}_{window}_{stat}_{unit_suffix}"))
                for prefix in phase_prefixes
            ]
            values = [value for value in values if math.isfinite(value)]
            if not values:
                continue
            if stat in {"min"}:
                aggregate = min(values)
            elif stat in {"avg", "rms"}:
                aggregate = sum(values) / len(values)
            else:
                aggregate = max(values, key=abs if stat == "peak_signed" else None)
                if stat in {"max", "peak_abs"}:
                    aggregate = max(values)
            output_summary[f"{aggregate_prefix}_{window}_{stat}_{unit_suffix}"] = aggregate


def is_current_driven_operation(operation: str) -> bool:
    normalized = operation.lower().replace("_", "").replace("-", "")
    return normalized not in {"noload", "backemf", "cogging"}


def populate_commanded_current_metrics(output_summary: dict[str, Any], spec: Any, operation: str) -> None:
    """Store physical commanded phase-current metrics for optimization CSVs."""
    peak = abs(float(spec.i_peak_a)) if is_current_driven_operation(operation) else 0.0
    rms = peak / math.sqrt(2.0)
    stats = {
        "avg": 0.0,
        "rms": rms,
        "min": -peak,
        "max": peak,
        "peak_abs": peak,
        "peak_signed": peak,
    }
    for window in TIME_DOMAIN_WINDOWS:
        for prefix in ("output_phasea_current", "output_phaseb_current", "output_phasec_current", "output_phase_current"):
            for stat, value in stats.items():
                output_summary[f"{prefix}_{window}_{stat}_a"] = value


def add_derived_motor_metrics(output_summary: dict[str, Any], spec: Any) -> None:
    """Add optimization-friendly metrics derived from transient report summaries."""
    omega_mech_rad_s = 2.0 * math.pi * float(spec.base_rpm) / 60.0
    phase_resistance = float(spec.phase_resistance_ohm)
    vdc = float(spec.vdc_v)
    phase_voltage_limit_spwm = vdc / 2.0
    phase_voltage_limit_svpwm = vdc / math.sqrt(3.0)
    output_summary["output_phase_voltage_limit_spwm_v"] = phase_voltage_limit_spwm
    output_summary["output_phase_voltage_limit_svpwm_v"] = phase_voltage_limit_svpwm

    for window in TIME_DOMAIN_WINDOWS:
        torque_avg = finite_float(output_summary.get(f"output_torque_{window}_avg_nm"))
        torque_min = finite_float(output_summary.get(f"output_torque_{window}_min_nm"))
        torque_max = finite_float(output_summary.get(f"output_torque_{window}_max_nm"))
        output_summary[f"output_torque_{window}_ripple_pct"] = (
            safe_divide(torque_max - torque_min, abs(torque_avg)) * 100.0
        )

        phase_current_rms = finite_float(output_summary.get(f"output_phase_current_{window}_rms_a"))

        copper_loss = 3.0 * phase_resistance * phase_current_rms * phase_current_rms
        output_summary[f"output_copperloss_{window}_avg_w"] = copper_loss if math.isfinite(copper_loss) else math.nan

        core_loss = finite_float(output_summary.get(f"output_coreloss_{window}_avg_w"))
        solid_loss = finite_float(output_summary.get(f"output_solidloss_{window}_avg_w"))
        total_loss = sum(value for value in (core_loss, solid_loss, copper_loss) if math.isfinite(value))
        if not any(math.isfinite(value) for value in (core_loss, solid_loss, copper_loss)):
            total_loss = math.nan
        output_summary[f"output_total_loss_{window}_avg_w"] = total_loss

        mech_power = torque_avg * omega_mech_rad_s
        output_summary[f"output_mech_power_{window}_w"] = mech_power if math.isfinite(mech_power) else math.nan
        output_summary[f"output_efficiency_{window}_pct"] = (
            safe_divide(mech_power, mech_power + total_loss) * 100.0
        )

        voltage_peak = finite_float(output_summary.get(f"output_phase_voltage_{window}_peak_abs_v"))
        output_summary[f"output_voltage_spwm_margin_{window}_v"] = (
            phase_voltage_limit_spwm - voltage_peak if math.isfinite(voltage_peak) else math.nan
        )
        output_summary[f"output_voltage_svpwm_margin_{window}_v"] = (
            phase_voltage_limit_svpwm - voltage_peak if math.isfinite(voltage_peak) else math.nan
        )
        # Backward-compatible alias: the previous column used the SVPWM limit.
        output_summary[f"output_voltage_margin_{window}_v"] = output_summary[
            f"output_voltage_svpwm_margin_{window}_v"
        ]


def summarize_transient_outputs(exported_reports: dict[str, str], spec: Any, operation: str = "sin_current") -> dict[str, Any]:
    """Summarize first-cycle, last-cycle, and all-cycle transient outputs."""
    frq_hz = float(spec.base_rpm) * float(spec.pole_number) / 120.0
    period_s = 1.0 / frq_hz
    stop_s = float(spec.transient_periods) * period_s
    result: dict[str, Any] = {
        "output_electric_frequency_hz": frq_hz,
        "output_period_s": period_s,
        "output_stop_time_s": stop_s,
    }

    torque_path = exported_reports.get("artifact_report_PPT_Torque")
    if torque_path and Path(torque_path).exists():
        df = read_report_csv(torque_path)
        column = find_column(list(df.columns), ("torque",))
        if column:
            result.update(summarize_metric(df, column, "output_torque", period_s, stop_s, "nm"))

    cogging_path = exported_reports.get("artifact_report_PPT_Cogging_Torque")
    if not is_current_driven_operation(operation) and cogging_path and Path(cogging_path).exists():
        df = read_report_csv(cogging_path)
        column = find_column(list(df.columns), ("torque",))
        if column:
            result.update(summarize_metric(df, column, "output_cogging_torque", period_s, stop_s, "nm"))

    populate_commanded_current_metrics(result, spec, operation)

    back_emf_path = exported_reports.get("artifact_report_PPT_Back_EMF")
    if back_emf_path and Path(back_emf_path).exists():
        df = read_report_csv(back_emf_path)
        data_columns = non_time_columns(list(df.columns))
        phase_columns: list[str | None] = []
        for index, phase in enumerate(("a", "b", "c")):
            column = find_column(list(df.columns), (f"phase{phase}",))
            if not column and index < len(data_columns):
                column = data_columns[index]
            phase_columns.append(column)
            if column:
                result.update(summarize_metric(df, column, f"output_back_emf_phase{phase}", period_s, stop_s, "v"))
        summarize_phase_envelope(
            result,
            ("output_back_emf_phasea", "output_back_emf_phaseb", "output_back_emf_phasec"),
            "output_back_emf_phase",
            "v",
        )
        if phase_columns and phase_columns[0]:
            result.update(
                summarize_last_cycle_harmonics(
                    df,
                    phase_columns[0],
                    "output_back_emf_phasea",
                    frq_hz,
                    period_s,
                    stop_s,
                    "v",
                )
            )

    inductance_path = exported_reports.get("artifact_report_PPT_Inductance_Matrix")
    if inductance_path and Path(inductance_path).exists():
        df = read_report_csv(inductance_path)
        result.update(summarize_inductance_matrix(df, spec, period_s, stop_s))

    voltage_path = exported_reports.get("artifact_report_PPT_Phase_Voltages")
    if voltage_path and Path(voltage_path).exists():
        df = read_report_csv(voltage_path)
        data_columns = non_time_columns(list(df.columns))
        for index, phase in enumerate(("a", "b", "c")):
            column = find_column(list(df.columns), (f"phase{phase}",))
            if not column and index < len(data_columns):
                column = data_columns[index]
            if column:
                result.update(
                    summarize_metric(df, column, f"output_phase{phase}_voltage", period_s, stop_s, "v")
                )
        summarize_phase_envelope(
            result,
            ("output_phasea_voltage", "output_phaseb_voltage", "output_phasec_voltage"),
            "output_phase_voltage",
            "v",
        )
    else:
        voltage_a_path = exported_reports.get("artifact_report_PPT_PhaseA_Voltage_Limit")
        if voltage_a_path and Path(voltage_a_path).exists():
            df = read_report_csv(voltage_a_path)
            columns = non_time_columns(list(df.columns))
            if columns:
                result.update(summarize_metric(df, columns[0], "output_phasea_voltage", period_s, stop_s, "v"))
                result.update(
                    {
                        key.replace("output_phasea_voltage", "output_phase_voltage"): value
                        for key, value in result.items()
                        if key.startswith("output_phasea_voltage")
                    }
                )

    losses_path = exported_reports.get("artifact_report_PPT_Losses")
    if losses_path and Path(losses_path).exists():
        df = read_report_csv(losses_path)
        core_column = find_column(list(df.columns), ("coreloss",))
        solid_column = find_column(list(df.columns), ("solidloss",))
        if core_column:
            result.update(summarize_metric(df, core_column, "output_coreloss", period_s, stop_s, "w"))
        if solid_column:
            result.update(summarize_metric(df, solid_column, "output_solidloss", period_s, stop_s, "w"))

    add_derived_motor_metrics(result, spec)
    return result


def is_inductance_quantity(quantity: str) -> bool:
    """Return True for AEDT report quantities that look like inductance matrix entries."""
    text = str(quantity).lower()
    return "induct" in text or re.search(r"(^|[^a-z])l\s*\(", text) is not None


def export_inductance_matrix_data(m2d: Any, out_path: Path, setup_name: str) -> None:
    """Export transient inductance matrix quantities when AEDT exposes them."""
    setup_sweep = f"{setup_name} : Transient"
    categories = ("Transient", "Matrix", "AC Magnetic")
    display_types = ("Data Table", "Rectangular Plot")

    for category in categories:
        for display_type in display_types:
            try:
                quantity_categories = m2d.post.available_quantities_categories(
                    report_category=category,
                    display_type=display_type,
                    solution=setup_sweep,
                )
            except Exception:
                quantity_categories = []

            quantities: list[str] = []
            for quantity_category in quantity_categories or [None]:
                try:
                    quantities.extend(
                        m2d.post.available_report_quantities(
                            report_category=category,
                            display_type=display_type,
                            solution=setup_sweep,
                            quantities_category=quantity_category,
                        )
                    )
                except Exception:
                    continue

            selected = []
            for quantity in quantities:
                if is_inductance_quantity(quantity) and quantity not in selected:
                    selected.append(quantity)
            if not selected:
                continue

            data = m2d.post.get_solution_data(
                expressions=selected,
                setup_sweep_name=setup_sweep,
                domain="Sweep",
                primary_sweep_variable="Time",
                report_category=category,
            )
            if not data or not hasattr(data, "export_data_to_csv"):
                continue
            if not data.export_data_to_csv(str(out_path), delimiter=";"):
                continue
            if out_path.exists() and out_path.stat().st_size > 0:
                return

    raise RuntimeError("no transient inductance matrix quantities were available for export")


def export_ppt_reports(design: Any, project_path: Path, case_id: str, setup_name: str = "PPT_Transient") -> dict[str, str]:
    """Export reports created by configure_ipmsm_from_ppt to per-case CSV files."""
    m2d = getattr(design, "solver_instance", design)
    export_dir = project_path / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    exported = {}
    for report_name in REPORT_NAMES:
        out_path = export_dir / f"{case_id}_{report_name}.csv"
        try:
            if report_name == "PPT_Inductance_Matrix":
                export_inductance_matrix_data(m2d, out_path, setup_name)
                exported[f"artifact_report_{report_name}"] = str(out_path)
                continue

            exported_path = None
            try:
                exported_path = m2d.post.export_report_to_file(str(export_dir), report_name, ".csv")
            except Exception:
                exported_path = None

            if exported_path and Path(exported_path).exists():
                exported_file = Path(exported_path)
                if exported_file.resolve() != out_path.resolve():
                    if out_path.exists():
                        out_path.unlink()
                    exported_file.replace(out_path)
            else:
                report_modules = [
                    getattr(getattr(m2d, "post", None), "oreportsetup", None),
                    getattr(m2d, "oreportsetup", None),
                ]
                try:
                    report_modules.append(m2d.odesign.GetModule("ReportSetup"))
                except Exception:
                    pass

                for report_module in report_modules:
                    if report_module is None:
                        continue
                    try:
                        report_module.ExportToFile(report_name, str(out_path), False)
                        break
                    except Exception:
                        continue

            if not out_path.exists() or out_path.stat().st_size == 0:
                raise RuntimeError("report export did not create a CSV file")
            exported[f"artifact_report_{report_name}"] = str(out_path)
        except Exception as exc:
            exported[f"artifact_report_{report_name}"] = f"skipped: {exc}"
    return exported


def missing_required_output_metrics(output_summary: dict[str, Any]) -> list[str]:
    """Return required transient metrics that were not exported/summarized."""
    required = [
        "output_torque_all_avg_nm",
        "output_coreloss_all_avg_w",
        "output_solidloss_all_avg_w",
    ]
    missing = []
    for key in required:
        value = output_summary.get(key)
        if value is None:
            missing.append(key)
            continue
        try:
            if math.isnan(float(value)):
                missing.append(key)
        except Exception:
            pass
    return missing


def safe_release_desktop(desktop: Any) -> None:
    if desktop is None:
        return
    try:
        desktop.release_desktop(close_projects=True, close_on_exit=True)
    except Exception:
        logging.exception("Desktop release failed")


def cleanup_project_folder(project_path: Path, cleanup_linux: bool, success: bool) -> None:
    if os.name == "nt" or not cleanup_linux or not success:
        return
    time.sleep(2)
    try:
        shutil.rmtree(project_path)
        logging.info("Deleted completed project folder: %s", project_path)
    except Exception:
        logging.exception("Failed to delete project folder: %s", project_path)


def run_one_case(payload: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    """Run one IPMSM case. Designed to be called by multiprocessing workers."""
    from pyaedt_module.core import pyDesktop

    from module.ipmsm_geometry import create_ipmsm_design
    from module.ipmsm_ppt_setup import (
        configure_ipmsm_from_ppt,
        get_core_loss_coefficients,
        get_core_material_properties,
    )

    try:
        from ansys.aedt.core import settings

        settings.skip_license_check = True
        settings.wait_for_license = False
    except Exception:
        pass

    case, option_dict = payload
    options = RunnerOptions(**option_dict)
    simulation_dir = Path(options.simulation_dir)
    result_csv = Path(options.result_csv)
    case_id = str(case_value(case, "case_id", "id", default="case"))

    start_dt = datetime.now()
    start = time.time()
    desktop = None
    project_path = None
    success = False

    row: dict[str, Any] = {
        "case_id": case_id,
        "status": "started",
        "started_at": start_dt.isoformat(timespec="seconds"),
    }

    try:
        spec = build_spec(case, default_symmetry_factor=options.symmetry_factor)
        use_periodic_boundary = case_bool(
            case,
            "use_periodic_boundary",
            default=options.use_periodic_boundary,
        )
        operation = str(case_value(case, "operation", default="sin_current"))

        input_data = {}
        input_data.update(asdict(spec))
        input_data.update({f"coreloss_{key}": value for key, value in get_core_loss_coefficients().items()})
        input_data.update({f"core_{key}": value for key, value in get_core_material_properties().items()})
        input_data["use_periodic_boundary"] = use_periodic_boundary
        input_data["operation"] = operation
        row.update(prefixed_row(input_data, "input_"))
        row.update(summarize_transient_outputs({}, spec, operation=operation))

        desktop = pyDesktop(
            version=None,
            non_graphical=options.non_graphical,
            close_on_exit=True,
            new_desktop=True,
        )
        sim = Simulation(desktop=desktop, cores=options.cores)
        sim.create_simulation_name(simulation_dir)
        project = sim.create_project(simulation_dir)
        project_path = Path(project.path)

        design, input_df, object_groups = create_ipmsm_design(project, sim)
        row.update(prefixed_row(dataframe_first_row(input_df), "input_"))

        setup_result = configure_ipmsm_from_ppt(
            design,
            object_groups=object_groups,
            spec=spec,
            operation=operation,
            use_periodic_boundary=use_periodic_boundary,
            create_missing_region=True,
            create_missing_band=True,
            create_reports=options.analyze,
            clear_existing=True,
            analyze=options.analyze,
            cores=options.cores,
        )
        analysis_returned_false = options.analyze and setup_result.get("analysis") is False

        try:
            project.save()
        except Exception:
            logging.exception("Project save failed for %s", sim.PROJECT_NAME)

        exported = export_ppt_reports(design, project_path, case_id, setup_name=spec.setup_name) if options.analyze else {}
        output_summary = summarize_transient_outputs(exported, spec, operation=operation) if options.analyze else {}
        row.update(prefixed_row(output_summary, ""))
        row.update(exported)
        missing_outputs = missing_required_output_metrics(output_summary) if options.analyze else []
        if missing_outputs:
            raise RuntimeError(f"Missing required transient output metrics: {missing_outputs}")
        elapsed = time.time() - start
        success = True

        row.update(
            {
                "status": "ok",
                "simulation_name": sim.PROJECT_NAME,
                "project_path": str(project_path),
                "analyze": options.analyze,
                "analysis_returned_false": analysis_returned_false,
                "validation": str(setup_result.get("validation")),
                "elapsed_s": round(elapsed, 3),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    except Exception as exc:
        elapsed = time.time() - start
        logging.exception("Case failed: %s", case_id)
        row.update(
            {
                "status": "failed",
                "error": repr(exc),
                "project_path": str(project_path) if project_path else "",
                "elapsed_s": round(elapsed, 3),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    finally:
        safe_release_desktop(desktop)
        if project_path is not None:
            cleanup_project_folder(project_path, options.cleanup_linux, success)
        append_result_row(result_csv, row)

    return row


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run IPMSM PyAEDT cases as a batch job.")
    parser.add_argument("--cases", help="Optional CSV file with case columns such as rpm, beta_deg, i_peak_a.")
    parser.add_argument("--count", type=int, default=1, help="Number of default random-geometry cases if --cases is omitted.")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel AEDT worker processes.")
    parser.add_argument("--cores", type=int, default=4, help="AEDT solver cores per worker.")
    parser.add_argument("--symmetry-factor", type=int, default=4, help="AEDT design symmetry multiplier.")
    parser.add_argument("--periodic-boundary", action="store_true", help="Assign periodic matching boundary for a sector model.")
    parser.add_argument("--simulation-dir", default=str(BASE_DIR / "simulation"), help="Directory for per-case AEDT projects.")
    parser.add_argument("--result-csv", default=str(BASE_DIR / "ipmsm_simulation_results.csv"), help="Shared CSV result file.")
    parser.add_argument("--analyze", dest="analyze", action="store_true", default=True, help="Run the transient solve.")
    parser.add_argument("--setup-only", dest="analyze", action="store_false", help="Build and validate setup without solving.")
    parser.add_argument("--non-graphical", action="store_true", default=(os.name != "nt"), help="Launch AEDT in non-graphical mode.")
    parser.add_argument("--graphical", dest="non_graphical", action="store_false", help="Launch AEDT with GUI.")
    parser.add_argument("--cleanup-linux", action="store_true", default=(os.name != "nt"), help="Delete successful project folders on Linux.")
    parser.add_argument("--keep-projects", dest="cleanup_linux", action="store_false", help="Keep project folders even on Linux.")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(processName)s %(message)s",
    )
    args = parse_args()
    cases = load_cases(args.cases, args.count)
    if not cases:
        raise RuntimeError("No cases to run.")

    options = RunnerOptions(
        simulation_dir=args.simulation_dir,
        result_csv=args.result_csv,
        analyze=args.analyze,
        non_graphical=args.non_graphical,
        cleanup_linux=args.cleanup_linux,
        symmetry_factor=args.symmetry_factor,
        use_periodic_boundary=args.periodic_boundary,
        cores=args.cores,
    )
    payloads = [(case, asdict(options)) for case in cases]

    logging.info(
        "Starting %d case(s), workers=%d, analyze=%s, symmetry_factor=%s, periodic_boundary=%s",
        len(payloads),
        args.workers,
        args.analyze,
        args.symmetry_factor,
        args.periodic_boundary,
    )
    if args.workers <= 1:
        for payload in payloads:
            result = run_one_case(payload)
            logging.info("Finished %s: %s", result.get("case_id"), result.get("status"))
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=args.workers) as pool:
            for result in pool.imap_unordered(run_one_case, payloads):
                logging.info("Finished %s: %s", result.get("case_id"), result.get("status"))
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
