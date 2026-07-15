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
import hashlib
import json
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
import uuid


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MAX_CASES = 200
DATASET_SCHEMA_VERSION = "ipmsm_v2"
DEFAULT_MODEL_EXTENT = "full_360"
DEFAULT_BETA_CONVENTION = "dq_current_advance_v2"
DEFAULT_AEDT_BACKEND = "standalone"
AEDT_BACKENDS = (DEFAULT_AEDT_BACKEND, "pooled")
AEDT_BACKEND_ENV = "MFT_AEDT_BACKEND"
AEDT_POOL_URL_ENV = "MFT_AEDT_SCHEDULER_URL"
AEDT_LEASE_WAIT_ENV = "MFT_AEDT_LEASE_WAIT_SECONDS"
AEDT_RELEASE_WAIT_ENV = "MFT_AEDT_RELEASE_WAIT_SECONDS"
AEDT_POOL_WORKSPACE_ROOT_ENV = "MFT_AEDT_POOL_WORKSPACE_ROOT"
AEDT_POOL_WORKSPACE_PATH_ENV = "MFT_AEDT_WORKSPACE_PATH"
AEDT_POOL_ISOLATION_POLICY_ENV = "MFT_AEDT_ISOLATION_POLICY"
AEDT_POOL_SESSION_VERSION_ENV = "MFT_AEDT_SESSION_VERSION"
DEFAULT_AEDT_LEASE_WAIT_SECONDS = 1800
DEFAULT_AEDT_RELEASE_WAIT_SECONDS = 300
DEFAULT_AEDT_POOL_WORKSPACE_ROOT = "/gpfs/tmp_cpu2/mft_pool"
DEFAULT_AEDT_POOL_SESSION_VERSION = "2025.2"
AEDT_POOL_HPC_CORES = 4


class PooledLeaseUnavailableError(RuntimeError):
    """A pooled run could not obtain a project lease and must not fall back."""


class PooledLeaseReleaseError(RuntimeError):
    """A pooled lease did not return the validated project-close ACK."""


def resolve_aedt_backend(
    cli_backend: str | None,
    environ: dict[str, str] | None = None,
) -> str:
    """Resolve CLI-over-env backend selection with standalone as the default."""
    environment = os.environ if environ is None else environ
    candidate = cli_backend if cli_backend is not None else environment.get(AEDT_BACKEND_ENV, "")
    backend = str(candidate or "").strip().lower() or DEFAULT_AEDT_BACKEND
    if backend not in AEDT_BACKENDS:
        raise ValueError(f"{AEDT_BACKEND_ENV} / --aedt-backend must be standalone or pooled")
    return backend


def positive_env_seconds(environ: dict[str, str], name: str, default: int) -> int:
    raw = str(environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def nonnegative_env_int(environ: dict[str, str], name: str) -> int:
    raw = str(environ.get(name, "") or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def pooled_isolation_policy(environ: dict[str, str]) -> str:
    policy = str(environ.get(AEDT_POOL_ISOLATION_POLICY_ENV, "") or "").strip().lower()
    policy = policy or "family"
    if policy not in {"family", "shared_if_compatible"}:
        raise ValueError(
            f"{AEDT_POOL_ISOLATION_POLICY_ENV} must be family or shared_if_compatible"
        )
    return policy


def pooled_session_profile(environ: dict[str, str]) -> dict[str, Any]:
    """Return the Desktop-global contract shared by MFT and IPMSM clients."""
    version = str(
        environ.get(AEDT_POOL_SESSION_VERSION_ENV, "")
        or DEFAULT_AEDT_POOL_SESSION_VERSION
    ).strip()
    if not version:
        raise ValueError(f"{AEDT_POOL_SESSION_VERSION_ENV} must not be blank")
    return {
        "profile_version": 2,
        "aedt_version": version,
        "python_environment": "pyaedt2026v1",
        "pyaedt_version": "0.22.0",
        "filesystem": "gpfs-shared-v1",
        "desktop_dso": {
            "config_name": "pyaedt_config",
            # Keep this byte-for-byte compatible with the MFT client. Icepak
            # is unused by IPMSM itself but is host-owned for mixed sessions.
            "designs": {
                "Icepak": {
                    "cores": AEDT_POOL_HPC_CORES,
                    "tasks": 1,
                    "gpus": 0,
                    "use_auto_settings": False,
                },
                "Maxwell 2D": {
                    "cores": AEDT_POOL_HPC_CORES,
                    "tasks": 1,
                    "gpus": 0,
                    "use_auto_settings": True,
                },
                "Maxwell 3D": {
                    "cores": AEDT_POOL_HPC_CORES,
                    "tasks": 1,
                    "gpus": 0,
                    "use_auto_settings": True,
                },
            },
        },
    }


def prepare_pooled_workspace(
    simulation_dir: Path,
    *,
    task_id: int,
    case_id: str,
    environ: dict[str, str],
) -> Path:
    """Create a deterministic cross-account directory for the AEDT host."""
    configured_path = str(
        environ.get(AEDT_POOL_WORKSPACE_PATH_ENV, "") or ""
    ).strip()
    if configured_path:
        workspace = Path(configured_path).expanduser()
        if not workspace.is_absolute():
            raise ValueError(
                f"{AEDT_POOL_WORKSPACE_PATH_ENV} must be an absolute path"
            )
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True, mode=0o777)
        try:
            workspace.chmod(0o777)
        except OSError as exc:
            raise RuntimeError(
                f"pooled AEDT workspace is not cross-account writable: {workspace}"
            ) from exc
        return workspace
    configured_root = str(environ.get(AEDT_POOL_WORKSPACE_ROOT_ENV, "") or "").strip()
    if configured_root:
        root = Path(configured_root).expanduser()
    elif os.name == "nt":
        # Local tests/development have no GPFS mount.
        root = simulation_dir.expanduser().resolve()
    else:
        root = Path(DEFAULT_AEDT_POOL_WORKSPACE_ROOT)
    case_digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
    leaf = f"ipmsm-{task_id or os.getpid()}-{case_digest}"
    workspace = (root / leaf).resolve()
    root_resolved = root.resolve()
    if workspace.parent != root_resolved:
        raise ValueError("pooled AEDT workspace escaped its configured root")
    workspace.mkdir(parents=True, exist_ok=True, mode=0o777)
    try:
        workspace.chmod(0o777)
    except OSError as exc:
        raise RuntimeError(
            f"pooled AEDT workspace is not cross-account writable: {workspace}"
        ) from exc
    return workspace


def safe_path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


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
        if safe_path_exists(candidate):
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
MESH_ELEMENT_KEYS = ("magnet", "rotor", "stator", "winding", "band")
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
    ("id_current", "a"),
    ("iq_current", "a"),
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
    "output_back_emf_phasea_h1_cos_peak_v",
    "output_back_emf_phasea_h1_sin_peak_v",
    "output_back_emf_phasea_h1_phase_deg",
    "output_back_emf_phasea_thd_pct",
)
OUTPUT_SUMMARY_COLUMNS = (
    "output_electric_frequency_hz",
    "output_period_s",
    "output_stop_time_s",
    "output_phase_current_source",
    "output_phase_voltage_source",
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
    "input_dataset_schema_version",
    "input_pole_number",
    "input_slot_number",
    "input_model_extent",
    "input_symmetry_factor",
    "input_base_rpm",
    "input_i_peak_a",
    "input_beta_dq_deg",
    "input_beta_deg",
    "input_beta_convention",
    "input_electrical_zero_deg",
    "input_commanded_id_peak_a",
    "input_commanded_iq_peak_a",
    "input_series_turns_per_phase",
    "input_turns_per_coil_side",
    "input_stack_length_mm",
    "input_phase_resistance_ohm",
    "input_vdc_v",
    "input_initial_position_deg",
    "input_transient_periods",
    "input_steps_per_period",
    "input_transient_total_steps",
    "input_electric_frequency_hz",
    "input_electrical_period_s",
    "input_transient_stop_time_s",
    "input_transient_time_step_s",
    "input_core_material",
    "input_core_material_fallbacks",
    "input_magnet_material",
    "input_winding_material",
    "input_shaft_material",
    "input_air_material",
    "input_setup_name",
    "input_mesh_elements",
    *(f"input_mesh_{key}_elements" for key in MESH_ELEMENT_KEYS),
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
    "input_stator_teeth_width_ratio",
    "input_stator_teeth_length",
    "input_stator_teeth_width",
    "input_stator_gap",
    "input_slot_opening_ratio",
    "input_rotator_gap",
    "input_shaft_ratio",
    "input_rotor_radius",
    "input_shaft_radius",
    "input_magnet_shield_thick",
    "input_magnet_setback_ratio",
    "input_magnet_thick_ratio",
    "input_magnet_space_height_ratio",
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
    "input_quality_profile",
    "input_setup_fingerprint",
    "input_material_fingerprint",
    "input_aedt_version",
    "input_beta_calibration_id",
    "input_geometry_mode",
    "input_source_case_id",
    "input_source_result_path",
    "input_use_periodic_boundary",
    "input_operation",
)

RUN_METADATA_COLUMNS = (
    "execution_host",
    "slurm_job_id",
    "slurm_array_task_id",
    "error",
    "simulation_name",
    "project_path",
    "analyze",
    "analysis_returned_false",
    "missing_required_outputs",
    "validation",
    "elapsed_s",
    "finished_at",
)

CASE_METADATA_COLUMNS = (
    "geometry_group_id",
    "design_hash",
    "operating_point_id",
    "doe_split",
    "repeat_of_case_id",
    "beta_calibration_id",
    "optimization_run_id",
    "candidate_id",
    "control_source",
)

RESULT_COLUMN_ORDER = (
    "case_id",
    "status",
    "started_at",
    *CASE_METADATA_COLUMNS,
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
    model_extent: str = DEFAULT_MODEL_EXTENT
    beta_convention: str = DEFAULT_BETA_CONVENTION
    electrical_zero_deg: float = 0.0
    aedt_backend: str = DEFAULT_AEDT_BACKEND


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
                current = max(int(raw), next_simulation_number(simulation_dir))
            else:
                current = next_simulation_number(simulation_dir)

            self.num = current
            self.PROJECT_NAME = f"simulation{current}"

            file.seek(0)
            file.truncate()
            file.write(str(current + 1))
            file.flush()
            os.fsync(file.fileno())

    def create_pooled_pending_name(self, task_id: int) -> None:
        """Create a globally namespaced placeholder without a local counter.

        ``simulation_num.txt`` is scoped to one deployed repository/account and
        therefore cannot provide uniqueness inside a cross-account AEDT pool.
        The final name is rebound after the scheduler returns the lease id.
        """
        owner = int(task_id) if int(task_id) > 0 else os.getpid()
        self.PROJECT_NAME = f"ipmsm-pending-{owner}-{uuid.uuid4().hex[:12]}"

    def bind_pooled_lease_identity(self, task_id: int, lease_id: int) -> str:
        """Bind the project to one session-safe, globally unique lease name."""
        owner = int(task_id) if int(task_id) > 0 else os.getpid()
        lease_value = int(lease_id)
        if lease_value <= 0:
            raise ValueError("pooled AEDT lease id must be positive")
        self.PROJECT_NAME = (
            f"ipmsm-{owner}-{lease_value}-{uuid.uuid4().hex[:12]}"
        )
        return self.PROJECT_NAME

    def create_project(self, simulation_dir: Path) -> Any:
        if self.desktop is None:
            raise RuntimeError("Desktop instance is None. Cannot create project.")

        project_path = simulation_dir / self.PROJECT_NAME
        project_path.mkdir(parents=True, exist_ok=True)
        self.project = self.desktop.create_project(path=str(project_path), name=self.PROJECT_NAME)
        return self.project

    def close_project(self) -> None:
        if self.project is None:
            return
        close = getattr(self.project, "close", None)
        if callable(close):
            close()
            return
        odesktop = getattr(self.desktop, "odesktop", None)
        close_project = getattr(odesktop, "CloseProject", None)
        if not callable(close_project):
            raise RuntimeError(f"Cannot close pooled AEDT project {self.PROJECT_NAME!r}")
        close_project(self.PROJECT_NAME)

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


def stable_fingerprint(namespace: str, payload: dict[str, Any]) -> str:
    """Return a deterministic, human-identifiable SHA-256 fingerprint."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return f"{namespace}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def setup_fingerprint(
    spec: Any,
    quality_profile: str,
    operation: str,
    use_periodic_boundary: bool,
) -> str:
    return stable_fingerprint(
        "setup_v2",
        {
            "model_extent": spec.model_extent,
            "symmetry_factor": int(spec.symmetry_factor),
            "beta_convention": spec.beta_convention,
            "electrical_zero_deg": float(spec.electrical_zero_deg),
            "series_turns_per_phase": int(spec.series_turns_per_phase),
            "turns_per_coil_side": int(spec.turns_per_coil_side),
            "transient_periods": int(spec.transient_periods),
            "steps_per_period": int(spec.steps_per_period),
            "mesh_elements": {key: int(value) for key, value in sorted(spec.mesh_elements.items())},
            "setup_name": spec.setup_name,
            "quality_profile": quality_profile,
            "operation": operation,
            "use_periodic_boundary": bool(use_periodic_boundary),
        },
    )


def material_fingerprint(
    spec: Any,
    core_loss_coefficients: dict[str, Any],
    core_material_properties: dict[str, Any],
) -> str:
    return stable_fingerprint(
        "materials_v2",
        {
            "core_material": spec.core_material,
            "core_material_fallbacks": list(spec.core_material_fallbacks),
            "magnet_material": spec.magnet_material,
            "winding_material": spec.winding_material,
            "shaft_material": spec.shaft_material,
            "air_material": spec.air_material,
            "core_loss_coefficients": core_loss_coefficients,
            "core_material_properties": core_material_properties,
        },
    )


def planned_aedt_version(case: dict[str, Any]) -> str:
    explicit = str(case_value(case, "aedt_version", "input_aedt_version", default="")).strip()
    if explicit:
        return explicit
    environment_version = os.environ.get("AEDT_VERSION", "").strip()
    if environment_version:
        return environment_version
    roots = sorted((key for key in os.environ if key.startswith("ANSYSEM_ROOT")), reverse=True)
    if roots:
        suffix = roots[0].removeprefix("ANSYSEM_ROOT")
        if len(suffix) == 3 and suffix.isdigit():
            return f"20{suffix[:2]}.{suffix[2:]}"
        return suffix or roots[0]
    return "auto"


def detected_aedt_version(desktop: Any, fallback: str) -> str:
    """Best-effort readback without depending on one PyAEDT wrapper version."""
    for source in (desktop, getattr(desktop, "desktop", None)):
        if source is None:
            continue
        for attribute in ("aedt_version", "desktop_version", "version"):
            value = getattr(source, attribute, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            if value not in (None, ""):
                return str(value)
    return fallback


def build_mesh_elements(case: dict[str, Any], defaults: Any) -> dict[str, int]:
    """Build per-case mesh element counts from stable CSV column names."""
    mesh_elements = dict(defaults.mesh_elements)
    for key in MESH_ELEMENT_KEYS:
        aliases = (
            f"mesh_{key}_elements",
            f"{key}_mesh_elements",
            f"mesh_{key}",
        )
        raw_value = case_value(case, *aliases, default=None)
        if raw_value in (None, ""):
            continue
        value = int(float(raw_value))
        if value < 1:
            raise ValueError(f"mesh element count must be >= 1 for {aliases[0]}")
        mesh_elements[key] = value
    return mesh_elements


def validate_transient_spec(spec: Any) -> None:
    if int(spec.pole_number) < 1:
        raise ValueError("pole_number must be >= 1")
    if float(spec.base_rpm) <= 0.0:
        raise ValueError("base_rpm must be > 0")
    if int(spec.transient_periods) < 1:
        raise ValueError("transient_periods must be >= 1")
    if int(spec.steps_per_period) < 1:
        raise ValueError("steps_per_period must be >= 1")


def transient_setup_metadata(spec: Any) -> dict[str, Any]:
    validate_transient_spec(spec)
    electric_frequency_hz = float(spec.base_rpm) * float(spec.pole_number) / 120.0
    electrical_period_s = 1.0 / electric_frequency_hz
    total_steps = int(spec.transient_periods) * int(spec.steps_per_period)
    stop_time_s = float(spec.transient_periods) * electrical_period_s
    return {
        "transient_total_steps": total_steps,
        "electric_frequency_hz": electric_frequency_hz,
        "electrical_period_s": electrical_period_s,
        "transient_stop_time_s": stop_time_s,
        "transient_time_step_s": stop_time_s / total_steps,
    }


FIXED_GEOMETRY_REQUIRED_KEYS = (
    "slot_num",
    "pole_num",
    "stator_outer_radius",
    "stator_back_yoke_thick_ratio",
    "stator_inner_ratio",
    "stator_shoe_thick",
    "stator_teeth_length_ratio",
    "stator_teeth_width_ratio",
    "stator_gap",
    "rotator_gap",
    "shaft_ratio",
    "magnet_shield_thick",
    "magnet_setback_ratio",
    "magnet_thick_ratio",
    "magnet_height_ratio",
)
FIXED_GEOMETRY_OPTIONAL_DEFAULTS = {
    "slot_opening_ratio": 0.09,
    "magnet_space_height_ratio": 1.0,
}
FIXED_GEOMETRY_ALIASES = {
    key: (f"input_{key}", key)
    for key in (
        *FIXED_GEOMETRY_REQUIRED_KEYS,
        *FIXED_GEOMETRY_OPTIONAL_DEFAULTS,
        "stator_back_yoke_thick",
        "stator_inner_radius",
        "stator_teeth_length",
        "stator_teeth_width",
    )
}
FIXED_GEOMETRY_ALIASES["slot_num"] = ("input_slot_num", "slot_num", "slot_number")
FIXED_GEOMETRY_ALIASES["pole_num"] = ("input_pole_num", "pole_num", "pole_number")


def finite_case_value(case: dict[str, Any], *names: str) -> float:
    raw_value = case_value(case, *names, default=None)
    if raw_value in (None, ""):
        return math.nan
    try:
        value = float(raw_value)
    except Exception:
        return math.nan
    return value if math.isfinite(value) else math.nan


def derive_stator_teeth_width_ratio(values: dict[str, float]) -> float:
    width = values.get("stator_teeth_width", math.nan)
    outer_radius = values.get("stator_outer_radius", math.nan)
    back_yoke = values.get("stator_back_yoke_thick", math.nan)
    if not math.isfinite(back_yoke):
        back_yoke = outer_radius * values.get("stator_back_yoke_thick_ratio", math.nan)
    teeth_length = values.get("stator_teeth_length", math.nan)
    if not math.isfinite(teeth_length):
        inner_radius = values.get("stator_inner_radius", math.nan)
        if not math.isfinite(inner_radius):
            inner_radius = outer_radius * values.get("stator_inner_ratio", math.nan)
        teeth_length = (outer_radius - back_yoke - inner_radius) * values.get("stator_teeth_length_ratio", math.nan)
    slot_num = values.get("slot_num", math.nan)
    if not all(math.isfinite(value) for value in (width, outer_radius, back_yoke, teeth_length, slot_num)):
        return math.nan
    denominator = (outer_radius - back_yoke - teeth_length) * math.tan(math.radians(360.0 / slot_num) / 2.0) * 2.0
    if denominator <= 0.0:
        return math.nan
    return width / denominator


def validate_fixed_geometry(values: dict[str, float]) -> None:
    errors: list[str] = []

    def require_positive(key: str) -> None:
        if values[key] <= 0.0:
            errors.append(f"{key} must be > 0")

    def require_ratio(key: str) -> None:
        if values[key] <= 0.0 or values[key] > 1.0:
            errors.append(f"{key} must be > 0 and <= 1")

    for key in ("slot_num", "pole_num"):
        if values[key] <= 0.0 or not math.isclose(values[key], round(values[key]), abs_tol=1e-9):
            errors.append(f"{key} must be a positive integer")

    for key in ("stator_outer_radius", "stator_shoe_thick", "stator_gap", "rotator_gap", "magnet_shield_thick"):
        require_positive(key)

    for key in (
        "stator_back_yoke_thick_ratio",
        "stator_inner_ratio",
        "stator_teeth_length_ratio",
        "stator_teeth_width_ratio",
        "slot_opening_ratio",
        "shaft_ratio",
        "magnet_setback_ratio",
        "magnet_thick_ratio",
        "magnet_space_height_ratio",
        "magnet_height_ratio",
    ):
        require_ratio(key)

    if errors:
        raise ValueError(f"fixed geometry is invalid: {'; '.join(errors)}")

    slot_num = int(round(values["slot_num"]))
    pole_num = int(round(values["pole_num"]))
    stator_outer_radius = values["stator_outer_radius"]
    stator_back_yoke_thick = stator_outer_radius * values["stator_back_yoke_thick_ratio"]
    stator_inner_radius = stator_outer_radius * values["stator_inner_ratio"]
    stator_radial_space = stator_outer_radius - stator_back_yoke_thick - stator_inner_radius
    stator_teeth_length = stator_radial_space * values["stator_teeth_length_ratio"]
    stator_tooth_base_radius = stator_outer_radius - stator_back_yoke_thick - stator_teeth_length
    angle_rad = math.radians(360.0 / slot_num)
    stator_teeth_width = stator_tooth_base_radius * math.tan(angle_rad / 2.0) * values["stator_teeth_width_ratio"] * 2.0
    stator_airgap_clearance = stator_tooth_base_radius - (stator_inner_radius + values["stator_gap"])
    slot_opening = (
        2.0 * stator_tooth_base_radius * math.sin(angle_rad / 2.0)
        - stator_teeth_width * math.cos(angle_rad / 2.0)
    ) * values["slot_opening_ratio"]
    rotor_radius = stator_inner_radius - values["rotator_gap"]
    shaft_radius = rotor_radius * values["shaft_ratio"]
    rotor_thick = rotor_radius - shaft_radius
    magnet_setback = rotor_thick * values["magnet_setback_ratio"]
    magnet_thick = rotor_thick * values["magnet_thick_ratio"]
    magnet_radial_clearance = rotor_thick - magnet_setback - magnet_thick
    magnet_height = (
        (rotor_radius - magnet_setback - magnet_thick) * math.cos(math.pi / pole_num)
        - values["magnet_shield_thick"]
    )

    derived_checks = {
        "stator_radial_space": stator_radial_space,
        "stator_teeth_length": stator_teeth_length,
        "stator_tooth_base_radius": stator_tooth_base_radius,
        "stator_teeth_width": stator_teeth_width,
        "stator_airgap_clearance": stator_airgap_clearance,
        "slot_opening": slot_opening,
        "rotor_radius": rotor_radius,
        "shaft_radius": shaft_radius,
        "rotor_thick": rotor_thick,
        "magnet_setback": magnet_setback,
        "magnet_thick": magnet_thick,
        "magnet_radial_clearance": magnet_radial_clearance,
        "magnet_height": magnet_height,
        "magnet_space_height": magnet_height * values["magnet_space_height_ratio"],
        "scaled_magnet_height": magnet_height * values["magnet_height_ratio"],
    }
    derived_errors = [f"{key} must be > 0" for key, value in derived_checks.items() if value <= 0.0]
    if derived_errors:
        raise ValueError(f"fixed geometry is invalid: {'; '.join(derived_errors)}")


def extract_fixed_geometry(case: dict[str, Any]) -> dict[str, float]:
    """Extract fixed geometry values from case rows or prior result CSV rows."""
    values: dict[str, float] = {}
    for key, aliases in FIXED_GEOMETRY_ALIASES.items():
        value = finite_case_value(case, *aliases)
        if math.isfinite(value):
            values[key] = value

    if not values:
        return {}

    if "stator_teeth_width_ratio" not in values:
        derived = derive_stator_teeth_width_ratio(values)
        if math.isfinite(derived):
            values["stator_teeth_width_ratio"] = derived

    for key, default in FIXED_GEOMETRY_OPTIONAL_DEFAULTS.items():
        values.setdefault(key, default)

    missing = [key for key in FIXED_GEOMETRY_REQUIRED_KEYS if key not in values]
    if missing:
        raise ValueError(f"fixed geometry columns are incomplete; missing: {missing}")

    validate_fixed_geometry(values)

    values["slot_num"] = int(round(values["slot_num"]))
    values["pole_num"] = int(round(values["pole_num"]))
    return {
        key: values[key]
        for key in (
            "slot_num",
            "pole_num",
            "stator_outer_radius",
            "stator_back_yoke_thick_ratio",
            "stator_inner_ratio",
            "stator_shoe_thick",
            "stator_teeth_length_ratio",
            "stator_teeth_width_ratio",
            "stator_gap",
            "slot_opening_ratio",
            "rotator_gap",
            "shaft_ratio",
            "magnet_shield_thick",
            "magnet_setback_ratio",
            "magnet_thick_ratio",
            "magnet_space_height_ratio",
            "magnet_height_ratio",
        )
    }


def build_spec(
    case: dict[str, Any],
    default_symmetry_factor: int = 1,
    default_model_extent: str = DEFAULT_MODEL_EXTENT,
    default_beta_convention: str = DEFAULT_BETA_CONVENTION,
    default_electrical_zero_deg: float = 0.0,
) -> Any:
    """Build the PPT setup spec from one CSV/default case row."""
    from module.ipmsm_ppt_setup import IPMSMPPTSpec, validate_ppt_spec_contract

    defaults = IPMSMPPTSpec()
    spec = IPMSMPPTSpec(
        pole_number=case_int(case, "pole_number", "pole_num", default=8),
        slot_number=case_int(case, "slot_number", "slot_num", default=12),
        model_extent=str(
            case_value(case, "model_extent", "input_model_extent", default=default_model_extent)
        ).strip(),
        symmetry_factor=case_int(
            case,
            "symmetry_factor",
            "input_symmetry_factor",
            default=default_symmetry_factor,
        ),
        base_rpm=case_float(case, "base_rpm", "rpm", default=1200.0),
        i_peak_a=case_float(case, "i_peak_a", "i_peak", "current_peak_a", default=137.8),
        beta_deg=case_float(
            case,
            "beta_dq_deg",
            "input_beta_dq_deg",
            "beta_deg",
            "input_beta_deg",
            "beta",
            default=defaults.beta_deg,
        ),
        beta_convention=str(
            case_value(
                case,
                "beta_convention",
                "input_beta_convention",
                default=default_beta_convention,
            )
        ).strip(),
        electrical_zero_deg=case_float(
            case,
            "electrical_zero_deg",
            "input_electrical_zero_deg",
            default=default_electrical_zero_deg,
        ),
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
        mesh_elements=build_mesh_elements(case, defaults),
    )
    validate_transient_spec(spec)
    validate_ppt_spec_contract(spec)
    return spec


def load_cases(path: str | None, count: int) -> list[dict[str, Any]]:
    if path:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
            return normalize_case_ids([dict(row) for row in csv.DictReader(file)])
    return [{"case_id": f"case_{idx:04d}"} for idx in range(1, count + 1)]


def normalize_case_ids(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        row = dict(case)
        if row.get("case_id") in (None, ""):
            row["case_id"] = str(case_value(row, "id", default=f"case_{index:04d}"))
        normalized.append(row)
    return normalized


def duplicate_case_ids(cases: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for index, case in enumerate(cases, start=1):
        case_id = str(case_value(case, "case_id", "id", default=f"case_{index:04d}"))
        if case_id in seen and case_id not in duplicates:
            duplicates.append(case_id)
        seen.add(case_id)
    return duplicates


def validate_case_inputs(
    cases: list[dict[str, Any]],
    default_symmetry_factor: int = 1,
    default_model_extent: str = DEFAULT_MODEL_EXTENT,
    default_beta_convention: str = DEFAULT_BETA_CONVENTION,
    default_electrical_zero_deg: float = 0.0,
    default_use_periodic_boundary: bool = False,
) -> None:
    from module.ipmsm_ppt_setup import validate_ppt_spec_contract

    for index, case in enumerate(cases, start=1):
        case_id = str(case_value(case, "case_id", "id", default=f"case_{index:04d}"))
        try:
            spec = build_spec(
                case,
                default_symmetry_factor=default_symmetry_factor,
                default_model_extent=default_model_extent,
                default_beta_convention=default_beta_convention,
                default_electrical_zero_deg=default_electrical_zero_deg,
            )
            use_periodic_boundary = case_bool(
                case,
                "use_periodic_boundary",
                "input_use_periodic_boundary",
                default=default_use_periodic_boundary,
            )
            validate_ppt_spec_contract(spec, use_periodic_boundary=use_periodic_boundary)
            extract_fixed_geometry(case)
        except Exception as exc:
            raise RuntimeError(f"case plan row {case_id} has invalid inputs: {exc}") from exc


def validate_case_plan(
    cases: list[dict[str, Any]],
    max_cases: int,
    allow_over_budget: bool = False,
    default_symmetry_factor: int = 1,
    default_model_extent: str = DEFAULT_MODEL_EXTENT,
    default_beta_convention: str = DEFAULT_BETA_CONVENTION,
    default_electrical_zero_deg: float = 0.0,
    default_use_periodic_boundary: bool = False,
) -> None:
    if not cases:
        raise RuntimeError("No cases to run.")
    duplicates = duplicate_case_ids(cases)
    if duplicates:
        preview = ", ".join(duplicates[:5])
        raise RuntimeError(f"duplicate case_id value(s) in case plan: {preview}")
    if max_cases < 1:
        raise RuntimeError("--max-cases must be at least 1.")
    if len(cases) > max_cases and not allow_over_budget:
        raise RuntimeError(
            f"case plan has {len(cases)} rows, exceeding --max-cases={max_cases}; "
            "pass --allow-over-budget only for an intentional approved run."
        )
    validate_case_inputs(
        cases,
        default_symmetry_factor=default_symmetry_factor,
        default_model_extent=default_model_extent,
        default_beta_convention=default_beta_convention,
        default_electrical_zero_deg=default_electrical_zero_deg,
        default_use_periodic_boundary=default_use_periodic_boundary,
    )


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
    "mNewtonMeter": 1e-3,
    "mNewton Meter": 1e-3,
    "mN*m": 1e-3,
    "mN m": 1e-3,
    "mNm": 1e-3,
    "uNewtonMeter": 1e-6,
    "µNewtonMeter": 1e-6,
    "μNewtonMeter": 1e-6,
    "uN*m": 1e-6,
    "µN*m": 1e-6,
    "μN*m": 1e-6,
    "uNm": 1e-6,
    "µNm": 1e-6,
    "μNm": 1e-6,
    "nNewtonMeter": 1e-9,
    "nN*m": 1e-9,
    "nNm": 1e-9,
    "kNewtonMeter": 1e3,
    "kN*m": 1e3,
    "kNm": 1e3,
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
    units: dict[str, float] | None = None
    if unit_suffix == "w":
        units = POWER_UNITS_TO_WATTS
    elif unit_suffix == "nm":
        units = TORQUE_UNITS_TO_NM
    elif unit_suffix == "a":
        units = CURRENT_UNITS_TO_AMPERES
    elif unit_suffix == "v":
        units = VOLTAGE_UNITS_TO_VOLTS
    elif unit_suffix == "h":
        units = INDUCTANCE_UNITS_TO_HENRY
    if units is None:
        return 1.0
    try:
        return units[unit]
    except KeyError as exc:
        raise ValueError(
            f"unsupported AEDT report unit {unit!r} for output suffix {unit_suffix!r}"
        ) from exc


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


def summarize_sampled_values(
    time_values_s: list[float],
    values: list[float],
    output_prefix: str,
    period_s: float,
    stop_s: float,
    unit_suffix: str,
) -> dict[str, float]:
    """Summarize already-converted samples with the standard time windows."""
    pairs = [
        (time_s, value)
        for time_s, value in zip(time_values_s, values)
        if math.isfinite(time_s) and math.isfinite(value)
    ]
    eps = max(period_s, stop_s, 1.0) * 1e-9
    windows = {
        "first": [value for time_s, value in pairs if -eps <= time_s <= period_s + eps],
        "last": [value for time_s, value in pairs if stop_s - period_s - eps <= time_s <= stop_s + eps],
        "all": [value for _, value in pairs],
    }
    result: dict[str, float] = {}
    for window_name, selected in windows.items():
        stats = series_stats(selected or windows["all"])
        for stat in TIME_DOMAIN_STATS:
            result[f"{output_prefix}_{window_name}_{stat}_{unit_suffix}"] = stats[stat]
    return result


def canonical_electrical_frame_angle_rad(spec: Any, time_s: float) -> float:
    """Return the calibrated rotor-dq electrical angle used by v2 excitation.

    ``ElectricalZero`` is calibrated at the configured rotor start position,
    so it already contains the fixed rotor/winding-axis offset.  Adding the
    mechanical initial position again would rotate measured dq values twice.
    """
    frequency_hz = float(spec.base_rpm) * float(spec.pole_number) / 120.0
    time_value = float(time_s)
    electrical_zero_deg = float(spec.electrical_zero_deg)
    if not all(math.isfinite(value) for value in (frequency_hz, time_value, electrical_zero_deg)):
        raise ValueError("electrical-frame inputs must be finite")
    return 2.0 * math.pi * frequency_hz * time_value + math.radians(electrical_zero_deg)


def summarize_dq_current_report(
    df: Any,
    phase_columns: tuple[str, str, str],
    spec: Any,
    period_s: float,
    stop_s: float,
) -> dict[str, float]:
    """Transform signed measured phase currents into the canonical dq frame."""
    time_column = find_column(list(df.columns), ("time",)) or df.columns[0]
    time_unit = extract_column_unit(time_column)
    current_units = tuple(extract_column_unit(column) for column in phase_columns)
    time_values: list[float] = []
    id_values: list[float] = []
    iq_values: list[float] = []
    for raw_time, raw_a, raw_b, raw_c in zip(
        df[time_column],
        df[phase_columns[0]],
        df[phase_columns[1]],
        df[phase_columns[2]],
    ):
        time_s = parse_time_seconds(raw_time, time_unit)
        currents = (
            parse_report_value(raw_a, current_units[0], "a"),
            parse_report_value(raw_b, current_units[1], "a"),
            parse_report_value(raw_c, current_units[2], "a"),
        )
        theta = canonical_electrical_frame_angle_rad(spec, time_s)
        angles = (theta, theta - 2.0 * math.pi / 3.0, theta + 2.0 * math.pi / 3.0)
        id_a = (2.0 / 3.0) * sum(current * math.cos(angle) for current, angle in zip(currents, angles))
        iq_a = -(2.0 / 3.0) * sum(current * math.sin(angle) for current, angle in zip(currents, angles))
        time_values.append(time_s)
        id_values.append(id_a)
        iq_values.append(iq_a)
    result = summarize_sampled_values(time_values, id_values, "output_id_current", period_s, stop_s, "a")
    result.update(summarize_sampled_values(time_values, iq_values, "output_iq_current", period_s, stop_s, "a"))
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
        cos_peak = 2.0 * cos_sum / count
        sin_peak = 2.0 * sin_sum / count
        peak = math.hypot(cos_peak, sin_peak)
        rms = peak / math.sqrt(2.0)
        harmonic_rms[harmonic] = rms
        result[f"{output_prefix}_h{harmonic}_rms_{unit_suffix}"] = rms
        if harmonic == 1:
            result[f"{output_prefix}_h1_cos_peak_{unit_suffix}"] = cos_peak
            result[f"{output_prefix}_h1_sin_peak_{unit_suffix}"] = sin_peak
            result[f"{output_prefix}_h1_phase_deg"] = math.degrees(math.atan2(cos_peak, sin_peak))

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
        theta = canonical_electrical_frame_angle_rad(spec, time_s)
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


def motor_efficiency_pct(mech_power: float, total_loss: float) -> float:
    if not math.isfinite(mech_power) or not math.isfinite(total_loss):
        return math.nan
    if mech_power <= 0.0 or total_loss < 0.0:
        return math.nan
    return safe_divide(mech_power, mech_power + total_loss) * 100.0


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
        output_summary[f"output_efficiency_{window}_pct"] = motor_efficiency_pct(mech_power, total_loss)

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


def output_physics_issues(
    output_summary: Mapping[str, Any],
    *,
    operation: str,
    max_mech_loss_to_apparent_ratio: float = 1.05,
) -> list[str]:
    """Return fail-closed steady-state power-envelope violations.

    The sum of per-phase apparent powers is an intentionally loose upper
    bound.  It catches report-unit mistakes without assuming a power factor or
    relying on the efficiency value derived from the same torque report.
    """

    if not is_current_driven_operation(operation):
        return []
    if (
        not math.isfinite(max_mech_loss_to_apparent_ratio)
        or max_mech_loss_to_apparent_ratio < 1.0
    ):
        raise ValueError("max_mech_loss_to_apparent_ratio must be finite and >= 1")
    apparent_terms = []
    for phase in ("a", "b", "c"):
        voltage = finite_float(output_summary.get(f"output_phase{phase}_voltage_last_rms_v"))
        current = finite_float(output_summary.get(f"output_phase{phase}_current_last_rms_a"))
        if not math.isfinite(voltage) or not math.isfinite(current):
            return ["apparent_power_inputs_nonfinite"]
        apparent_terms.append(abs(voltage) * abs(current))
    apparent_power = sum(apparent_terms)
    mech_power = finite_float(output_summary.get("output_mech_power_last_w"))
    total_loss = finite_float(output_summary.get("output_total_loss_last_avg_w"))
    if not all(math.isfinite(value) for value in (apparent_power, mech_power, total_loss)):
        return ["apparent_power_inputs_nonfinite"]
    if apparent_power <= 0.0 or total_loss < 0.0:
        return ["apparent_power_inputs_invalid"]
    if (abs(mech_power) + total_loss) > max_mech_loss_to_apparent_ratio * apparent_power:
        return ["apparent_power_bound"]
    return []


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
            torque_unit = extract_column_unit(column)
            torque_scale = unit_scale_to_base(torque_unit, "nm")
            logging.info(
                "Torque report source unit=%r scale_to_nm=%g",
                torque_unit,
                torque_scale,
            )
            result.update(summarize_metric(df, column, "output_torque", period_s, stop_s, "nm"))

    cogging_path = exported_reports.get("artifact_report_PPT_Cogging_Torque")
    if not is_current_driven_operation(operation) and cogging_path and Path(cogging_path).exists():
        df = read_report_csv(cogging_path)
        column = find_column(list(df.columns), ("torque",))
        if column:
            result.update(summarize_metric(df, column, "output_cogging_torque", period_s, stop_s, "nm"))

    current_path = exported_reports.get("artifact_report_PPT_Phase_Currents")
    measured_current = False
    if current_path and Path(current_path).exists():
        df = read_report_csv(current_path)
        data_columns = non_time_columns(list(df.columns))
        phase_columns: list[str | None] = []
        for index, phase in enumerate(("a", "b", "c")):
            column = find_column(list(df.columns), (f"phase{phase}",))
            if not column and index < len(data_columns):
                column = data_columns[index]
            phase_columns.append(column)
            if column:
                result.update(
                    summarize_metric(df, column, f"output_phase{phase}_current", period_s, stop_s, "a")
                )
        if all(phase_columns):
            measured_current = True
            measured_columns = (str(phase_columns[0]), str(phase_columns[1]), str(phase_columns[2]))
            summarize_phase_envelope(
                result,
                ("output_phasea_current", "output_phaseb_current", "output_phasec_current"),
                "output_phase_current",
                "a",
            )
            result.update(summarize_dq_current_report(df, measured_columns, spec, period_s, stop_s))
    if not measured_current:
        populate_commanded_current_metrics(result, spec, operation)
        result["output_phase_current_source"] = "commanded_fallback"
    else:
        result["output_phase_current_source"] = "measured_three_phase"

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
        voltage_phase_count = 0
        for index, phase in enumerate(("a", "b", "c")):
            column = find_column(list(df.columns), (f"phase{phase}",))
            if not column and index < len(data_columns):
                column = data_columns[index]
            if column:
                voltage_phase_count += 1
                result.update(
                    summarize_metric(df, column, f"output_phase{phase}_voltage", period_s, stop_s, "v")
                )
        summarize_phase_envelope(
            result,
            ("output_phasea_voltage", "output_phaseb_voltage", "output_phasec_voltage"),
            "output_phase_voltage",
            "v",
        )
        result["output_phase_voltage_source"] = (
            "measured_three_phase" if voltage_phase_count == 3 else "incomplete_phase_report"
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
                result["output_phase_voltage_source"] = "phasea_fallback"

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


def missing_required_output_metrics(
    output_summary: dict[str, Any],
    *,
    require_v2: bool = False,
    operation: str = "sin_current",
) -> list[str]:
    """Return required transient metrics that were not exported/summarized."""
    required = [
        "output_torque_all_avg_nm",
        "output_coreloss_all_avg_w",
        "output_solidloss_all_avg_w",
    ]
    if require_v2:
        required.extend(
            [
                "output_phase_voltage_last_peak_abs_v",
                "output_phasea_voltage_last_peak_abs_v",
                "output_phaseb_voltage_last_peak_abs_v",
                "output_phasec_voltage_last_peak_abs_v",
                "output_ld_last_avg_h",
                "output_lq_last_avg_h",
            ]
        )
        if is_current_driven_operation(operation):
            required.extend(
                [
                    "output_phase_current_last_rms_a",
                    "output_id_current_last_avg_a",
                    "output_iq_current_last_avg_a",
                ]
            )
        else:
            required.append("output_back_emf_phasea_h1_rms_v")
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
    if require_v2 and output_summary.get("output_phase_voltage_source") != "measured_three_phase":
        missing.append("output_phase_voltage_source")
    if (
        require_v2
        and is_current_driven_operation(operation)
        and output_summary.get("output_phase_current_source") != "measured_three_phase"
    ):
        missing.append("output_phase_current_source")
    return missing


def safe_release_desktop(desktop: Any) -> None:
    if desktop is None:
        return
    try:
        desktop.release_desktop(close_projects=True, close_on_exit=True)
    except Exception:
        logging.exception("Desktop release failed")


def safe_close_pooled_project(simulation: Simulation | None) -> None:
    if simulation is None or simulation.project is None:
        return
    try:
        simulation.close_project()
    except Exception:
        logging.exception("Pooled project close failed for %s", simulation.PROJECT_NAME)


def safe_release_pooled_lease(
    lease: Any,
    wait_seconds: int,
    *,
    require_released: bool,
) -> str:
    if lease is None:
        return ""
    try:
        status = lease.release(wait_seconds=wait_seconds)
    except Exception as exc:
        logging.exception("Pooled AEDT lease release failed")
        return f"{type(exc).__name__}: {exc}"
    if not require_released:
        return ""
    if not isinstance(status, dict):
        return f"invalid release response: {status!r}"
    state = str(status.get("state") or getattr(lease, "state", "") or "").strip().lower()
    if state != "released":
        return f"expected released close ACK, got {state or '<blank>'}"
    return ""


def safe_report_pooled_solver_fault(lease: Any) -> None:
    if lease is None:
        return
    try:
        lease.report_fault("solver_timeout")
    except Exception:
        logging.exception("Pooled AEDT solver fault report failed")
    finally:
        # Fault settlement is now host-owned. Stop client keepalives without
        # sending a normal close/release that could race a native solve whose
        # state is unknown.
        for method_name in ("stop_heartbeat", "stop_process_keepalive"):
            method = getattr(lease, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    logging.exception(
                        "Pooled AEDT lease %s failed after solver fault",
                        method_name,
                    )


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
    case, option_dict = payload
    options = RunnerOptions(**option_dict)
    simulation_dir = Path(options.simulation_dir)
    result_csv = Path(options.result_csv)
    case_id = str(case_value(case, "case_id", "id", default="case"))

    start_dt = datetime.now()
    start = time.time()
    desktop = None
    sim = None
    lease = None
    lease_granted = False
    pooled_solver_uncertain = False
    lease_release_wait_seconds = DEFAULT_AEDT_RELEASE_WAIT_SECONDS
    project_path = None
    success = False
    aedt_backend = str(options.aedt_backend or "").strip().lower()
    pooled_backend = aedt_backend == "pooled"

    row: dict[str, Any] = {
        "case_id": case_id,
        "status": "started",
        "started_at": start_dt.isoformat(timespec="seconds"),
        "execution_host": (
            os.environ.get("SLURMD_NODENAME")
            or os.environ.get("HOSTNAME")
            or os.environ.get("COMPUTERNAME")
            or "unknown"
        ),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID", ""),
        "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID", ""),
    }
    for metadata_column in CASE_METADATA_COLUMNS:
        row[metadata_column] = case_value(
            case,
            metadata_column,
            f"input_{metadata_column}",
            default="",
        )

    try:
        if aedt_backend not in AEDT_BACKENDS:
            raise ValueError("aedt_backend must be standalone or pooled")
        spec = build_spec(
            case,
            default_symmetry_factor=options.symmetry_factor,
            default_model_extent=options.model_extent,
            default_beta_convention=options.beta_convention,
            default_electrical_zero_deg=options.electrical_zero_deg,
        )
        fixed_geometry = extract_fixed_geometry(case)
        use_periodic_boundary = case_bool(
            case,
            "use_periodic_boundary",
            "input_use_periodic_boundary",
            default=options.use_periodic_boundary,
        )
        operation = str(case_value(case, "operation", default="sin_current"))

        from module.ipmsm_ppt_setup import (
            commanded_dq_current_components,
            get_core_loss_coefficients,
            get_core_material_properties,
            validate_ppt_spec_contract,
        )

        validate_ppt_spec_contract(spec, use_periodic_boundary=use_periodic_boundary)
        core_loss_coefficients = get_core_loss_coefficients()
        core_material_properties = get_core_material_properties()
        quality_profile = str(
            case_value(case, "quality_profile", "input_quality_profile", default="default")
        ).strip() or "default"
        aedt_version = planned_aedt_version(case)
        commanded_id_a, commanded_iq_a = commanded_dq_current_components(spec)
        if not is_current_driven_operation(operation):
            commanded_id_a = 0.0
            commanded_iq_a = 0.0

        input_data = {}
        input_data.update(asdict(spec))
        input_data.update(transient_setup_metadata(spec))
        input_data.update({f"mesh_{key}_elements": spec.mesh_elements.get(key, "") for key in MESH_ELEMENT_KEYS})
        input_data.update({f"coreloss_{key}": value for key, value in core_loss_coefficients.items()})
        input_data.update({f"core_{key}": value for key, value in core_material_properties.items()})
        input_data["dataset_schema_version"] = DATASET_SCHEMA_VERSION
        input_data["beta_dq_deg"] = (
            spec.beta_deg if spec.beta_convention == DEFAULT_BETA_CONVENTION else ""
        )
        input_data["commanded_id_peak_a"] = commanded_id_a
        input_data["commanded_iq_peak_a"] = commanded_iq_a
        input_data["quality_profile"] = quality_profile
        input_data["setup_fingerprint"] = setup_fingerprint(
            spec,
            quality_profile=quality_profile,
            operation=operation,
            use_periodic_boundary=use_periodic_boundary,
        )
        input_data["material_fingerprint"] = material_fingerprint(
            spec,
            core_loss_coefficients=core_loss_coefficients,
            core_material_properties=core_material_properties,
        )
        input_data["aedt_version"] = aedt_version
        input_data["beta_calibration_id"] = case_value(
            case, "beta_calibration_id", "input_beta_calibration_id", default=""
        )
        input_data["geometry_mode"] = "fixed" if fixed_geometry else "random"
        input_data["source_case_id"] = case_value(case, "source_case_id", default="")
        input_data["source_result_path"] = case_value(case, "source_result_path", default="")
        input_data["use_periodic_boundary"] = use_periodic_boundary
        input_data["operation"] = operation
        row.update(prefixed_row(input_data, "input_"))
        row.update(summarize_transient_outputs({}, spec, operation=operation))

        from pyaedt_module.core import pyDesktop

        from module.ipmsm_geometry import create_ipmsm_design
        from module.ipmsm_ppt_setup import configure_ipmsm_from_ppt

        try:
            from ansys.aedt.core import settings

            settings.enable_error_handler = False
            settings.skip_license_check = True
            settings.wait_for_license = False
        except Exception:
            pass

        if pooled_backend:
            sim = Simulation(desktop=None, cores=options.cores)
            sim.fixed_geometry = fixed_geometry
            task_id = nonnegative_env_int(os.environ, "SLURM_SCHED_TASK_ID")
            sim.create_pooled_pending_name(task_id)
            try:
                if int(options.cores) != AEDT_POOL_HPC_CORES:
                    raise ValueError(
                        "pooled AEDT requires exactly "
                        f"{AEDT_POOL_HPC_CORES} cores to match the host DSO profile"
                    )
                scheduler_url = str(os.environ.get(AEDT_POOL_URL_ENV, "") or "").strip()
                if not scheduler_url:
                    raise ValueError(f"{AEDT_POOL_URL_ENV} is required for pooled AEDT")
                lease_wait_seconds = positive_env_seconds(
                    os.environ,
                    AEDT_LEASE_WAIT_ENV,
                    DEFAULT_AEDT_LEASE_WAIT_SECONDS,
                )
                lease_release_wait_seconds = positive_env_seconds(
                    os.environ,
                    AEDT_RELEASE_WAIT_ENV,
                    DEFAULT_AEDT_RELEASE_WAIT_SECONDS,
                )
                from module.aedt_attach_client import acquire_project_lease

                requested_workspace = prepare_pooled_workspace(
                    Path(options.simulation_dir),
                    task_id=task_id,
                    case_id=case_id,
                    environ=os.environ,
                )

                lease = acquire_project_lease(
                    scheduler_url,
                    sim.PROJECT_NAME,
                    # Stable for this simulation intent.  Transport retries must
                    # not create independent queued leases that can be granted
                    # after this worker has already abandoned them.
                    request_key=(
                        f"ipmsm:{task_id or os.getpid()}:"
                        f"{hashlib.sha256(case_id.encode('utf-8')).hexdigest()[:16]}"
                    ),
                    task_id=task_id,
                    allocation_id=0,
                    # The central pool owns dedicated host allocations.  A
                    # normal simulation task's node is provenance, not an
                    # affinity constraint for its pooled Desktop.
                    node_name="",
                    workload_family="ipmsm",
                    session_profile=pooled_session_profile(os.environ),
                    project_namespace="pyaedt_motor",
                    isolation_policy=pooled_isolation_policy(os.environ),
                    workspace_path=str(requested_workspace),
                    protocol_version=2,
                    admission_timeout_seconds=lease_wait_seconds,
                )
                lease.wait_until_leased(timeout_seconds=lease_wait_seconds)
                lease_granted = True
                leased_workspace = str(
                    getattr(lease, "workspace_path", "") or ""
                ).strip()
                if not leased_workspace:
                    raise RuntimeError("pooled AEDT lease did not preserve workspace_path")
                if Path(leased_workspace).resolve() != requested_workspace:
                    raise RuntimeError(
                        "pooled AEDT lease workspace readback mismatch: "
                        f"requested={requested_workspace}, actual={leased_workspace}"
                    )
                simulation_dir = requested_workspace
                sim.bind_pooled_lease_identity(
                    task_id,
                    int(getattr(lease, "lease_id", 0) or 0),
                )
                lease.bind_project_name(sim.PROJECT_NAME)
            except Exception as exc:
                raise PooledLeaseUnavailableError(
                    f"pooled AEDT lease unavailable: {type(exc).__name__}: {exc}"
                ) from exc
            desktop = lease.connect_desktop(
                non_graphical=options.non_graphical,
                desktop_factory=pyDesktop,
            )
            sim.desktop = desktop
            row["input_aedt_version"] = detected_aedt_version(desktop, aedt_version)
        else:
            try:
                desktop = pyDesktop(
                    version=None,
                    non_graphical=options.non_graphical,
                    close_on_exit=True,
                    new_desktop=True,
                )
                row["input_aedt_version"] = detected_aedt_version(desktop, aedt_version)
            except AttributeError as exc:
                if "EnableAutoSave" in str(exc):
                    raise RuntimeError(
                        "AEDT desktop startup failed before project creation; "
                        "pyDesktop did not expose a usable desktop instance."
                    ) from exc
                raise
            sim = Simulation(desktop=desktop, cores=options.cores)
            sim.fixed_geometry = fixed_geometry
            sim.create_simulation_name(simulation_dir)
        project = sim.create_project(simulation_dir)
        project_path = Path(project.path)
        if pooled_backend:
            activation = lease.activate(project_name=sim.PROJECT_NAME)
            if str(activation.get("state") or "") != "active":
                raise RuntimeError(
                    "pooled AEDT lease activation was not acknowledged: "
                    f"state={activation.get('state')!r}"
                )

        design, input_df, object_groups = create_ipmsm_design(project, sim)
        row.update(prefixed_row(dataframe_first_row(input_df), "input_"))

        configure_kwargs: dict[str, Any] = {
            "object_groups": object_groups,
            "spec": spec,
            "operation": operation,
            "use_periodic_boundary": use_periodic_boundary,
            "create_missing_region": True,
            "create_missing_band": True,
            "create_reports": options.analyze,
            "clear_existing": True,
            "analyze": options.analyze,
            # Passing ``cores=`` asks PyAEDT to mutate and later restore a
            # Desktop-global DSO registry entry. The pooled session host owns
            # the immutable profile, so None deliberately reuses that default.
            "cores": None if pooled_backend else options.cores,
        }
        if pooled_backend and lease_granted and options.analyze:
            def report_uncertain_pooled_solver() -> None:
                nonlocal pooled_solver_uncertain
                pooled_solver_uncertain = True
                safe_report_pooled_solver_fault(lease)

            configure_kwargs["analysis_error_callback"] = (
                report_uncertain_pooled_solver
            )
        setup_result = configure_ipmsm_from_ppt(design, **configure_kwargs)
        analysis_returned_false = options.analyze and setup_result.get("analysis") is False
        row.update(
            {
                "simulation_name": sim.PROJECT_NAME,
                "project_path": str(project_path),
                "analyze": options.analyze,
                "analysis_returned_false": analysis_returned_false,
                "validation": str(setup_result.get("validation")),
            }
        )

        try:
            project.save()
        except Exception:
            logging.exception("Project save failed for %s", sim.PROJECT_NAME)

        if analysis_returned_false:
            if pooled_backend and lease_granted:
                report_uncertain_pooled_solver()
            raise RuntimeError(
                f"AEDT analysis returned False; validation={setup_result.get('validation')}"
            )

        exported = export_ppt_reports(design, project_path, case_id, setup_name=spec.setup_name) if options.analyze else {}
        output_summary = summarize_transient_outputs(exported, spec, operation=operation) if options.analyze else {}
        row.update(prefixed_row(output_summary, ""))
        row.update(exported)
        missing_outputs = (
            missing_required_output_metrics(
                output_summary,
                require_v2=spec.beta_convention == DEFAULT_BETA_CONVENTION,
                operation=operation,
            )
            if options.analyze
            else []
        )
        row["missing_required_outputs"] = ";".join(missing_outputs)
        if missing_outputs:
            raise RuntimeError(f"Missing required transient output metrics: {missing_outputs}")
        physics_issues = output_physics_issues(output_summary, operation=operation)
        if physics_issues:
            raise RuntimeError(f"Transient output physics validation failed: {physics_issues}")
        elapsed = time.time() - start
        success = True

        row.update(
            {
                "status": "ok",
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
        if isinstance(exc, PooledLeaseUnavailableError):
            row["error_class"] = "pooled_lease_unavailable"
    finally:
        if pooled_backend:
            release_error = ""
            if pooled_solver_uncertain:
                row["pooled_release_suppressed"] = "solver_state_uncertain"
            else:
                if lease_granted:
                    safe_close_pooled_project(sim)
                release_error = safe_release_pooled_lease(
                    lease,
                    lease_release_wait_seconds,
                    require_released=lease_granted,
                )
            if release_error:
                row["pooled_release_error"] = release_error
                if row.get("status") == "ok":
                    success = False
                    row.update(
                        {
                            "status": "failed",
                            "error": repr(PooledLeaseReleaseError(release_error)),
                            "error_class": "pooled_release_failed",
                            "finished_at": datetime.now().isoformat(timespec="seconds"),
                        }
                    )
        else:
            safe_release_desktop(desktop)
        if project_path is not None:
            cleanup_project_folder(project_path, options.cleanup_linux, success)
        append_result_row(result_csv, row)

    return row


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run IPMSM PyAEDT cases as a batch job.")
    parser.add_argument("--cases", help="Optional CSV file with case columns such as rpm, beta_deg, i_peak_a.")
    parser.add_argument("--count", type=int, default=1, help="Number of default random-geometry cases if --cases is omitted.")
    parser.add_argument("--max-cases", type=int, default=DEFAULT_MAX_CASES, help="Guardrail for planned case rows.")
    parser.add_argument("--allow-over-budget", action="store_true", help="Allow planned case rows above --max-cases for an intentional approved run.")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel AEDT worker processes.")
    parser.add_argument("--cores", type=int, default=4, help="AEDT solver cores per worker.")
    parser.add_argument(
        "--model-extent",
        choices=("full_360", "sector_90"),
        default=DEFAULT_MODEL_EXTENT,
        help="Geometry extent contract. sector_90 is reserved until a real sector builder is available.",
    )
    parser.add_argument("--symmetry-factor", type=int, default=1, help="AEDT design symmetry multiplier.")
    parser.add_argument("--periodic-boundary", action="store_true", help="Request a periodic matching boundary.")
    parser.add_argument(
        "--beta-convention",
        choices=(DEFAULT_BETA_CONVENTION, "legacy_phase_offset_v1"),
        default=DEFAULT_BETA_CONVENTION,
        help="Current-angle convention; legacy behavior must be requested explicitly.",
    )
    parser.add_argument("--electrical-zero-deg", type=float, default=0.0)
    parser.add_argument("--simulation-dir", default=str(BASE_DIR / "simulation"), help="Directory for per-case AEDT projects.")
    parser.add_argument("--result-csv", default=str(BASE_DIR / "ipmsm_simulation_results.csv"), help="Shared CSV result file.")
    parser.add_argument("--analyze", dest="analyze", action="store_true", default=True, help="Run the transient solve.")
    parser.add_argument("--setup-only", dest="analyze", action="store_false", help="Build and validate setup without solving.")
    parser.add_argument("--non-graphical", action="store_true", default=(os.name != "nt"), help="Launch AEDT in non-graphical mode.")
    parser.add_argument("--graphical", dest="non_graphical", action="store_false", help="Launch AEDT with GUI.")
    parser.add_argument("--cleanup-linux", action="store_true", default=(os.name != "nt"), help="Delete successful project folders on Linux.")
    parser.add_argument("--keep-projects", dest="cleanup_linux", action="store_false", help="Keep project folders even on Linux.")
    parser.add_argument(
        "--aedt-backend",
        choices=AEDT_BACKENDS,
        default=None,
        help=f"AEDT lifecycle backend; defaults to ${AEDT_BACKEND_ENV} then standalone.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(processName)s %(message)s",
    )
    args = parse_args()
    aedt_backend = resolve_aedt_backend(args.aedt_backend)
    cases = load_cases(args.cases, args.count)
    validate_case_plan(
        cases,
        args.max_cases,
        allow_over_budget=args.allow_over_budget,
        default_symmetry_factor=args.symmetry_factor,
        default_model_extent=args.model_extent,
        default_beta_convention=args.beta_convention,
        default_electrical_zero_deg=args.electrical_zero_deg,
        default_use_periodic_boundary=args.periodic_boundary,
    )

    options = RunnerOptions(
        simulation_dir=args.simulation_dir,
        result_csv=args.result_csv,
        analyze=args.analyze,
        non_graphical=args.non_graphical,
        cleanup_linux=args.cleanup_linux,
        symmetry_factor=args.symmetry_factor,
        use_periodic_boundary=args.periodic_boundary,
        cores=args.cores,
        model_extent=args.model_extent,
        beta_convention=args.beta_convention,
        electrical_zero_deg=args.electrical_zero_deg,
        aedt_backend=aedt_backend,
    )
    payloads = [(case, asdict(options)) for case in cases]

    logging.info(
        "Starting %d case(s), workers=%d, analyze=%s, model_extent=%s, symmetry_factor=%s, "
        "periodic_boundary=%s, beta_convention=%s, aedt_backend=%s",
        len(payloads),
        args.workers,
        args.analyze,
        args.model_extent,
        args.symmetry_factor,
        args.periodic_boundary,
        args.beta_convention,
        aedt_backend,
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
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
