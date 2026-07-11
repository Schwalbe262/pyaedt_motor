"""Train deterministic LightGBM surrogate models for IPMSM simulation results."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import pickle
import random
import sys
from typing import Any, Callable, Iterable


PROJECT_DIR = Path(__file__).resolve().parent
SEED = 42
TEST_SIZE = 0.20
VAL_SIZE = 0.20
N_TUNING_TRIALS = 100
OUTLIER_IQR_WEIGHT = 1.5
R2_THRESHOLD = 0.95
V2_CONFORMAL_COVERAGE = 0.95
V2_MODEL_SELECTION_FRACTION = 0.20
V2_DEFAULT_ENSEMBLE_SIZE = 5
V2_TEST_EVALUATION_SCOPE_ALL = "all_preassigned_test"
V2_TEST_EVALUATION_SCOPE_AUDIT_CASE_PLAN = "audit_case_plan_test"

DEFAULT_DATA_PATHS = (
    PROJECT_DIR / "ipmsm_simulation_results1.csv",
    PROJECT_DIR / "ipmsm_simulation_results2.csv",
)
DEFAULT_MODEL_DIR = PROJECT_DIR / "lightgbm_ipmsm_models"
REQUIRED_TRAINING_DEPENDENCY_MODULES = ("numpy", "pandas", "sklearn.model_selection", "lightgbm")

RAW_INPUT_COLUMNS = (
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
INPUT_COLUMNS = (
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
    "input_rotator_gap",
    "input_shaft_ratio",
    "input_rotor_radius",
    "input_shaft_radius",
    "input_magnet_shield_thick",
    "input_magnet_setback_ratio",
    "input_magnet_thick_ratio",
    "input_magnet_height_ratio",
)
OPTIONAL_INPUT_COLUMNS = ("input_slot_opening_ratio", "input_magnet_space_height_ratio", "input_steps_per_period")
DERIVED_INPUT_REPAIR_COLUMNS = ("input_stator_teeth_width_ratio", "input_rotor_radius", "input_shaft_radius")

V2_DATASET_SCHEMA_VERSION = "ipmsm_v2"
V2_BETA_CONVENTION = "dq_current_advance_v2"
V2_CANONICAL_BETA_COLUMN = "input_beta_dq_deg"
V2_LEGACY_BETA_ALIAS = "input_beta_deg"
V2_REQUIRED_CONDITIONAL_INPUT_COLUMNS = (
    "input_slot_opening_ratio",
    "input_magnet_space_height_ratio",
    "input_stack_length_mm",
    "input_base_rpm",
    "input_i_peak_a",
    V2_CANONICAL_BETA_COLUMN,
    "input_phase_resistance_ohm",
)
V2_GEOMETRY_ID_COLUMNS = ("geometry_group_id", "design_hash")
V2_FINGERPRINT_COLUMNS = (
    "input_dataset_schema_version",
    "input_setup_fingerprint",
    "input_quality_profile",
    "input_material_fingerprint",
    "input_aedt_version",
    "input_beta_calibration_id",
    "input_beta_convention",
    "input_model_extent",
)

REQUESTED_OUTPUT_COLUMNS = (
    "output_torque_last_avg_nm",
    "output_torque_last_max_nm",
    "output_total_loss_last_avg_w",
    "output_solidloss_last_avg_w",
    "output_coreloss_last_avg_w",
    "output_ld_last_avg_h",
    "output_lq_last_avg_h",
    "output_efficiency_last_pc",
)
V2_PRIMITIVE_OUTPUT_COLUMNS = (
    "output_torque_last_avg_nm",
    "output_torque_last_max_nm",
    "output_solidloss_last_avg_w",
    "output_coreloss_last_avg_w",
    "output_ld_last_avg_h",
    "output_lq_last_avg_h",
)
V2_DERIVED_OUTPUT_COLUMNS = (
    "output_total_loss_last_avg_w",
    "output_efficiency_last_pct",
)
V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS = (
    *V2_PRIMITIVE_OUTPUT_COLUMNS,
    *V2_DERIVED_OUTPUT_COLUMNS,
)
V2_AUXILIARY_OUTPUT_COLUMNS = ("output_phase_voltage_last_peak_abs_v",)
OUTPUT_ALIASES = {"output_efficiency_last_pc": "output_efficiency_last_pct"}
EFFICIENCY_OUTPUT_COLUMNS = (
    "output_efficiency_first_pct",
    "output_efficiency_last_pct",
    "output_efficiency_last_pc",
    "output_efficiency_all_pct",
)

BASE_PARAMS = {
    "objective": "regression",
    "n_estimators": 800,
    "learning_rate": 0.03,
    "num_leaves": 31,
    "max_depth": -1,
    "min_child_samples": 10,
    "subsample": 0.85,
    "colsample_bytree": 0.85,
    "reg_alpha": 0.0,
    "reg_lambda": 0.1,
    "verbosity": -1,
}

PARAM_SEARCH_SPACE = {
    "n_estimators": (400, 700, 1000, 1400),
    "learning_rate": (0.01, 0.02, 0.03, 0.05, 0.08),
    "num_leaves": (15, 31, 63, 127),
    "max_depth": (-1, 4, 6, 8, 10),
    "min_child_samples": (5, 10, 20, 40),
    "subsample": (0.7, 0.85, 1.0),
    "colsample_bytree": (0.7, 0.85, 1.0),
    "reg_alpha": (0.0, 1e-3, 1e-2, 0.1),
    "reg_lambda": (0.0, 1e-3, 1e-2, 0.1, 1.0),
}

METRIC_COLUMNS = ("target", "split", "MAE", "RMSE", "R2", "MAPE_pct", "best_iteration")


@dataclass(frozen=True)
class TrainingDependencies:
    np: Any
    pd: Any
    train_test_split: Callable[..., Any]
    lgb: Any


@dataclass(frozen=True)
class PreparedData:
    raw_df: Any
    valid_df: Any
    input_columns: tuple[str, ...]
    output_columns: tuple[str, ...]
    output_name_map: dict[str, str]
    removed_output_outliers: int
    repaired_derived_inputs: dict[str, int]
    quality_report: "TrainingQualityReport"
    schema_version: str = "legacy"
    geometry_group_column: str | None = None
    fingerprints: dict[str, str] | None = None
    auxiliary_output_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrainingQualityReport:
    raw_rows: int
    rows_after_dedup: int
    dropped_duplicate_case_id_rows: int
    status_rejected_rows: int
    nonfinite_input_rows: int
    nonfinite_output_rows: int
    physical_sanity_rejected_rows: int
    valid_rows_before_outliers: int
    removed_output_outliers: int
    valid_rows: int

    @property
    def invalid_training_rows(self) -> int:
        return self.rows_after_dedup - self.valid_rows_before_outliers

    def failure_reasons(
        self,
        *,
        max_invalid_training_rows: int | None = None,
        max_removed_output_outlier_rows: int | None = None,
    ) -> list[str]:
        failures: list[str] = []
        if max_invalid_training_rows is not None and self.invalid_training_rows > max_invalid_training_rows:
            failures.append(f"invalid_training_rows {self.invalid_training_rows} > {max_invalid_training_rows}")
        if (
            max_removed_output_outlier_rows is not None
            and self.removed_output_outliers > max_removed_output_outlier_rows
        ):
            failures.append(
                f"removed_output_outlier_rows {self.removed_output_outliers} > {max_removed_output_outlier_rows}"
            )
        return failures

    def as_metadata(self) -> dict[str, int]:
        return {
            "raw_rows": self.raw_rows,
            "rows_after_dedup": self.rows_after_dedup,
            "dropped_duplicate_case_id_rows": self.dropped_duplicate_case_id_rows,
            "status_rejected_rows": self.status_rejected_rows,
            "nonfinite_input_rows": self.nonfinite_input_rows,
            "nonfinite_output_rows": self.nonfinite_output_rows,
            "physical_sanity_rejected_rows": self.physical_sanity_rejected_rows,
            "invalid_training_rows": self.invalid_training_rows,
            "valid_rows_before_outliers": self.valid_rows_before_outliers,
            "removed_output_outliers": self.removed_output_outliers,
            "valid_rows": self.valid_rows,
        }


@dataclass(frozen=True)
class SplitData:
    x_train: Any
    x_val: Any
    x_test: Any
    y_train: Any
    y_val: Any
    y_test: Any
    group_column: str | None = None
    train_group_ids: tuple[str, ...] = ()
    val_group_ids: tuple[str, ...] = ()
    test_group_ids: tuple[str, ...] = ()


class MissingTrainingDependencyError(ImportError):
    """Raised when optional training dependencies are unavailable."""


def inspect_training_dependencies(
    import_module: Callable[[str], Any] = importlib.import_module,
) -> list[dict[str, str]]:
    report: list[dict[str, str]] = []
    for name in REQUIRED_TRAINING_DEPENDENCY_MODULES:
        try:
            module = import_module(name)
        except ImportError as exc:
            report.append(
                {"module": name, "status": "missing", "version": "", "error": str(exc)}
            )
        else:
            report.append(
                {
                    "module": name,
                    "status": "ok",
                    "version": str(getattr(module, "__version__", "")),
                    "error": "",
                }
            )
    return report


def missing_training_dependency_modules(report: Iterable[dict[str, str]]) -> list[str]:
    return [row.get("module", "") for row in report if row.get("status") != "ok"]


def write_training_dependency_report(path: Path, report: list[dict[str, str]]) -> None:
    missing = missing_training_dependency_modules(report)
    payload = {"ready": not missing, "missing_modules": missing, "dependencies": report}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_training_dependency_report(report: list[dict[str, str]]) -> None:
    missing = missing_training_dependency_modules(report)
    print(
        f"training_dependencies ready={str(not missing).lower()} "
        f"ok={len(report) - len(missing)} missing={len(missing)}"
    )
    for row in report:
        detail = f"dependency module={row['module']} status={row['status']}"
        if row.get("version"):
            detail += f" version={row['version']}"
        if row.get("error"):
            detail += f" error={row['error']}"
        print(detail)


def require_training_dependencies(
    import_module: Callable[[str], Any] = importlib.import_module,
) -> TrainingDependencies:
    missing: list[str] = []

    def load(name: str) -> Any:
        try:
            return import_module(name)
        except ImportError:
            missing.append(name)
            return None

    np = load("numpy")
    pd = load("pandas")
    model_selection = load("sklearn.model_selection")
    lgb = load("lightgbm")
    if missing:
        raise MissingTrainingDependencyError(
            "missing optional training dependency module(s): "
            + ", ".join(missing)
            + ". Use the Anaconda/PyAEDT training environment before running this CLI."
        )
    return TrainingDependencies(np=np, pd=pd, train_test_split=model_selection.train_test_split, lgb=lgb)


def set_seed(seed: int, np_module: Any | None = None) -> None:
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    if np_module is not None:
        np_module.random.seed(seed)


def stable_target_seed(target_col: str, base_seed: int = SEED) -> int:
    digest = hashlib.sha256(target_col.encode("utf-8")).hexdigest()
    return base_seed + int(digest[:12], 16) % 100000


def resolve_output_columns(
    available_columns: Iterable[str],
    requested_columns: Iterable[str] = REQUESTED_OUTPUT_COLUMNS,
    aliases: dict[str, str] = OUTPUT_ALIASES,
) -> tuple[tuple[str, ...], dict[str, str]]:
    available = set(available_columns)
    output_columns: list[str] = []
    output_name_map: dict[str, str] = {}
    for requested_col in requested_columns:
        actual_col = requested_col if requested_col in available else aliases.get(requested_col, requested_col)
        if actual_col not in available:
            raise ValueError(f"missing output column: requested={requested_col}, actual={actual_col}")
        output_columns.append(actual_col)
        output_name_map[requested_col] = actual_col
    return tuple(output_columns), output_name_map


def missing_columns(available_columns: Iterable[str], required_columns: Iterable[str]) -> list[str]:
    available = set(available_columns)
    return [column for column in required_columns if column not in available]


def normalized_nonempty_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def parse_expected_fingerprints(entries: Iterable[str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    allowed = set(V2_FINGERPRINT_COLUMNS)
    for entry in entries:
        name, separator, value = str(entry).partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not name or not value:
            raise ValueError("--expected-fingerprint must use COLUMN=VALUE")
        if name not in allowed:
            raise ValueError(f"unsupported fingerprint column: {name}")
        if name in expected and expected[name] != value:
            raise ValueError(f"conflicting expected fingerprint values for {name}")
        expected[name] = value
    return expected


def assert_fingerprint_compatible(
    expected: dict[str, str],
    actual: dict[str, str],
    *,
    required_columns: Iterable[str] = V2_FINGERPRINT_COLUMNS,
) -> None:
    missing = [column for column in required_columns if column not in actual]
    if missing:
        raise ValueError(f"missing v2 fingerprint values: {missing}")
    mismatches = [
        f"{column}: expected={value!r}, actual={actual.get(column)!r}"
        for column, value in expected.items()
        if actual.get(column) != value
    ]
    if mismatches:
        raise ValueError("incompatible v2 fingerprints: " + "; ".join(mismatches))


def validate_v2_fingerprints(data: Any, expected: dict[str, str] | None = None) -> dict[str, str]:
    missing = missing_columns(data.columns, V2_FINGERPRINT_COLUMNS)
    if missing:
        raise ValueError(f"missing v2 fingerprint columns: {missing}")

    fingerprints: dict[str, str] = {}
    for column in V2_FINGERPRINT_COLUMNS:
        values = [normalized_nonempty_text(value) for value in data[column]]
        if any(value is None for value in values):
            raise ValueError(f"blank v2 fingerprint value: {column}")
        distinct = sorted(set(value for value in values if value is not None))
        if len(distinct) != 1:
            raise ValueError(f"mixed v2 fingerprint values for {column}: {distinct}")
        fingerprints[column] = distinct[0]

    required_values = {
        "input_dataset_schema_version": V2_DATASET_SCHEMA_VERSION,
        "input_beta_convention": V2_BETA_CONVENTION,
    }
    required_values.update(expected or {})
    assert_fingerprint_compatible(required_values, fingerprints)
    return fingerprints


def ensure_v2_canonical_beta_column(data: Any, fingerprints: dict[str, str]) -> str:
    if V2_CANONICAL_BETA_COLUMN in data.columns:
        return V2_CANONICAL_BETA_COLUMN
    if V2_LEGACY_BETA_ALIAS not in data.columns:
        raise ValueError(f"missing v2 canonical beta column: {V2_CANONICAL_BETA_COLUMN}")
    if fingerprints.get("input_beta_convention") != V2_BETA_CONVENTION:
        raise ValueError(
            f"{V2_LEGACY_BETA_ALIAS} is only accepted when input_beta_convention={V2_BETA_CONVENTION!r}"
        )
    data[V2_CANONICAL_BETA_COLUMN] = data[V2_LEGACY_BETA_ALIAS]
    return V2_CANONICAL_BETA_COLUMN


def resolve_v2_geometry_group_column(data: Any) -> str:
    available = [column for column in V2_GEOMETRY_ID_COLUMNS if column in data.columns]
    if not available:
        raise ValueError(f"v2 requires one geometry identity column: {list(V2_GEOMETRY_ID_COLUMNS)}")

    normalized: dict[str, list[str]] = {}
    for column in available:
        values = [normalized_nonempty_text(value) for value in data[column]]
        if any(value is None for value in values):
            raise ValueError(f"blank v2 geometry identity value: {column}")
        normalized[column] = [value for value in values if value is not None]

    if len(available) == 2:
        group_to_hash: dict[str, set[str]] = {}
        hash_to_group: dict[str, set[str]] = {}
        for group_id, design_hash in zip(normalized["geometry_group_id"], normalized["design_hash"]):
            group_to_hash.setdefault(group_id, set()).add(design_hash)
            hash_to_group.setdefault(design_hash, set()).add(group_id)
        if any(len(values) != 1 for values in group_to_hash.values()) or any(
            len(values) != 1 for values in hash_to_group.values()
        ):
            raise ValueError("geometry_group_id and design_hash must have a one-to-one mapping")
    return "geometry_group_id" if "geometry_group_id" in available else "design_hash"


def deterministic_group_partitions(
    group_values: Iterable[object],
    *,
    test_size: float,
    val_size: float,
    seed: int,
) -> dict[str, str]:
    groups = sorted(
        {normalized_nonempty_text(value) for value in group_values},
        key=lambda value: (hashlib.sha256(f"{seed}\0{value}".encode("utf-8")).hexdigest(), str(value)),
    )
    if None in groups:
        raise ValueError("geometry group values must be nonblank")
    group_ids = [str(value) for value in groups]
    if len(group_ids) < 3:
        raise ValueError("v2 grouped split requires at least 3 geometry groups")

    test_count = max(1, min(len(group_ids) - 2, int(round(len(group_ids) * test_size))))
    remaining_after_test = len(group_ids) - test_count
    val_count = max(1, min(remaining_after_test - 1, int(round(len(group_ids) * val_size))))
    assignments: dict[str, str] = {}
    for group_id in group_ids[:test_count]:
        assignments[group_id] = "test"
    for group_id in group_ids[test_count : test_count + val_count]:
        assignments[group_id] = "calibration"
    for group_id in group_ids[test_count + val_count :]:
        assignments[group_id] = "train"
    return assignments


def validated_preassigned_group_partitions(
    group_values: Iterable[object],
    split_values: Iterable[object],
) -> dict[str, str]:
    assignments: dict[str, str] = {}
    allowed = {"train", "calibration", "test"}
    groups = [normalized_nonempty_text(value) for value in group_values]
    splits = [str(value or "").strip().lower() for value in split_values]
    if len(groups) != len(splits):
        raise ValueError("v2 geometry groups and doe_split values have different lengths")
    for group, split in zip(groups, splits):
        if not group:
            raise ValueError("v2 geometry group must not be blank")
        if split not in allowed:
            raise ValueError(f"v2 doe_split must be one of {sorted(allowed)}; got {split!r}")
        previous = assignments.setdefault(group, split)
        if previous != split:
            raise ValueError(f"v2 geometry group {group!r} crosses doe_split partitions")
    if set(assignments.values()) != allowed:
        raise ValueError("v2 doe_split must contain nonempty train/calibration/test geometry groups")
    return assignments


def load_v2_audit_case_plan(
    path: Path,
    *,
    geometry_column: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Load an exact case plan whose preassigned test rows form the final audit cohort."""

    path = Path(path)
    if not path.is_file():
        raise ValueError(f"v2 audit case plan is missing: {path}")
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read v2 audit case plan {path}: {exc}") from exc
    try:
        with io.StringIO(text, newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = list(reader.fieldnames or ())
            if not fieldnames or len(fieldnames) != len(set(fieldnames)):
                raise ValueError("v2 audit case plan has a missing or duplicate CSV header")
            required = {"case_id", "doe_split", geometry_column}
            missing = sorted(required - set(fieldnames))
            if missing:
                raise ValueError(f"v2 audit case plan is missing columns: {missing}")
            rows = [dict(row) for row in reader]
    except csv.Error as exc:
        raise ValueError(f"cannot parse v2 audit case plan {path}: {exc}") from exc
    if not rows:
        raise ValueError("v2 audit case plan is empty")
    if any(None in row for row in rows):
        raise ValueError("v2 audit case plan has fields beyond its CSV header")

    case_ids: list[str] = []
    seen_case_ids: set[str] = set()
    group_splits: dict[str, str] = {}
    test_case_ids: list[str] = []
    test_groups: set[str] = set()
    allowed_splits = {"train", "calibration", "test"}
    for index, row in enumerate(rows, start=1):
        case_id = normalized_nonempty_text(row.get("case_id"))
        group_id = normalized_nonempty_text(row.get(geometry_column))
        split_name = str(row.get("doe_split") or "").strip().lower()
        if not case_id:
            raise ValueError(f"v2 audit case plan row {index} has a blank case_id")
        if case_id in seen_case_ids:
            raise ValueError(f"v2 audit case plan contains duplicate case_id: {case_id}")
        if not group_id:
            raise ValueError(
                f"v2 audit case plan row {index} has a blank {geometry_column}"
            )
        if split_name not in allowed_splits:
            raise ValueError(
                f"v2 audit case plan row {index} has invalid doe_split: {split_name!r}"
            )
        previous = group_splits.setdefault(group_id, split_name)
        if previous != split_name:
            raise ValueError(
                f"v2 audit geometry group {group_id!r} crosses doe_split partitions"
            )
        seen_case_ids.add(case_id)
        case_ids.append(case_id)
        if split_name == "test":
            test_case_ids.append(case_id)
            test_groups.add(group_id)
    if not test_case_ids or not test_groups:
        raise ValueError("v2 audit case plan has no preassigned test geometry")

    encoded_test_ids = json.dumps(
        test_case_ids,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    contract: dict[str, Any] = {
        "scope": V2_TEST_EVALUATION_SCOPE_AUDIT_CASE_PLAN,
        "case_plan": str(path.resolve(strict=False)),
        "case_plan_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "case_plan_rows": len(case_ids),
        "geometry_column": geometry_column,
        "rows": len(test_case_ids),
        "groups": len(test_groups),
        "test_case_ids_sha256": hashlib.sha256(encoded_test_ids).hexdigest(),
    }
    return rows, contract


def validate_v2_audit_records(
    plan_rows: Iterable[dict[str, object]],
    data_records: Iterable[dict[str, object]],
    *,
    geometry_column: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Bind every audit-plan row to valid data and return only its untouched test cohort."""

    normalized_plan: list[tuple[str, str, str]] = []
    plan_ids: set[str] = set()
    for row in plan_rows:
        case_id = normalized_nonempty_text(row.get("case_id"))
        group_id = normalized_nonempty_text(row.get(geometry_column))
        split_name = str(row.get("doe_split") or "").strip().lower()
        if not case_id or not group_id or split_name not in {"train", "calibration", "test"}:
            raise ValueError("v2 audit case plan contains an invalid normalized row")
        if case_id in plan_ids:
            raise ValueError(f"v2 audit case plan contains duplicate case_id: {case_id}")
        plan_ids.add(case_id)
        normalized_plan.append((case_id, group_id, split_name))

    data_by_case: dict[str, tuple[str, str]] = {}
    all_data: list[tuple[str, str, str]] = []
    for row in data_records:
        case_id = normalized_nonempty_text(row.get("case_id"))
        group_id = normalized_nonempty_text(row.get(geometry_column))
        split_name = str(row.get("doe_split") or "").strip().lower()
        if not case_id or not group_id or split_name not in {"train", "calibration", "test"}:
            raise ValueError("v2 training data contains an invalid audit identity row")
        if case_id in data_by_case:
            raise ValueError(f"v2 training data contains duplicate case_id: {case_id}")
        data_by_case[case_id] = (group_id, split_name)
        all_data.append((case_id, group_id, split_name))

    missing = [case_id for case_id, _, _ in normalized_plan if case_id not in data_by_case]
    if missing:
        raise ValueError(
            f"v2 audit case plan rows are missing from valid training data: {missing[:3]}"
        )
    mismatches = [
        case_id
        for case_id, group_id, split_name in normalized_plan
        if data_by_case[case_id] != (group_id, split_name)
    ]
    if mismatches:
        raise ValueError(
            f"v2 audit case plan identity/split differs from training data: {mismatches[:3]}"
        )

    test_case_ids = tuple(
        case_id for case_id, _, split_name in normalized_plan if split_name == "test"
    )
    test_groups = tuple(
        sorted(
            {
                group_id
                for _, group_id, split_name in normalized_plan
                if split_name == "test"
            }
        )
    )
    if not test_case_ids or not test_groups:
        raise ValueError("v2 audit case plan has no preassigned test geometry")
    audit_group_set = set(test_groups)
    leaked = [
        case_id
        for case_id, group_id, _ in all_data
        if group_id in audit_group_set and case_id not in plan_ids
    ]
    if leaked:
        raise ValueError(
            "v2 audit geometry appears outside the audit case plan: " + str(leaked[:3])
        )
    return test_case_ids, test_groups


def deterministic_model_selection_partitions(
    group_values: Iterable[object],
    *,
    seed: int,
    holdout_fraction: float = V2_MODEL_SELECTION_FRACTION,
) -> dict[str, str]:
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("model-selection holdout fraction must be greater than 0 and less than 1")
    groups = sorted(
        {normalized_nonempty_text(value) for value in group_values},
        key=lambda value: (
            hashlib.sha256(f"{seed}\0model_selection\0{value}".encode("utf-8")).hexdigest(),
            str(value),
        ),
    )
    if None in groups:
        raise ValueError("model-selection geometry group values must be nonblank")
    group_ids = [str(value) for value in groups]
    if len(group_ids) < 2:
        raise ValueError("v2 model selection requires at least 2 outer-train geometry groups")
    holdout_count = max(1, min(len(group_ids) - 1, int(round(len(group_ids) * holdout_fraction))))
    holdout_groups = set(group_ids[:holdout_count])
    return {
        group_id: "model_selection" if group_id in holdout_groups else "fit"
        for group_id in group_ids
    }


def finite_float(value: object) -> float:
    try:
        number = float(value)
    except Exception:
        return math.nan
    return number if math.isfinite(number) else math.nan


def derive_v2_outputs(
    *,
    torque_avg_nm: object,
    core_loss_w: object,
    solid_loss_w: object,
    i_peak_a: object,
    phase_resistance_ohm: object,
    rpm: object,
) -> dict[str, float]:
    torque = finite_float(torque_avg_nm)
    core_loss = finite_float(core_loss_w)
    solid_loss = finite_float(solid_loss_w)
    i_peak = finite_float(i_peak_a)
    resistance = finite_float(phase_resistance_ohm)
    speed_rpm = finite_float(rpm)
    values = (torque, core_loss, solid_loss, i_peak, resistance, speed_rpm)
    if not all(math.isfinite(value) for value in values):
        return {column: math.nan for column in V2_DERIVED_OUTPUT_COLUMNS}

    copper_loss = 1.5 * resistance * i_peak**2
    total_loss = core_loss + solid_loss + copper_loss
    mechanical_power = torque * 2.0 * math.pi * speed_rpm / 60.0
    efficiency = math.nan
    if mechanical_power > 0.0 and total_loss >= 0.0 and mechanical_power + total_loss > 0.0:
        efficiency = mechanical_power / (mechanical_power + total_loss) * 100.0
    return {
        "output_total_loss_last_avg_w": total_loss,
        "output_efficiency_last_pct": efficiency,
    }


def physical_sanity_violations(row: Any) -> list[str]:
    violations = []
    for column in EFFICIENCY_OUTPUT_COLUMNS:
        if column not in row:
            continue
        value = finite_float(row.get(column))
        if math.isfinite(value) and not 0.0 <= value <= 100.0:
            violations.append(column)
    return violations


def repaired_derived_input_values(row: Any) -> dict[str, float]:
    """Return derived input values that must match the AEDT geometry expressions."""
    repaired: dict[str, float] = {}
    stator_teeth_width = finite_float(row.get("input_stator_teeth_width"))
    stator_outer_radius = finite_float(row.get("input_stator_outer_radius"))
    stator_back_yoke_thick = finite_float(row.get("input_stator_back_yoke_thick"))
    stator_teeth_length = finite_float(row.get("input_stator_teeth_length"))
    slot_num = finite_float(row.get("input_slot_num"))
    if all(
        math.isfinite(value)
        for value in (stator_teeth_width, stator_outer_radius, stator_back_yoke_thick, stator_teeth_length, slot_num)
    ):
        denominator = (
            (stator_outer_radius - stator_back_yoke_thick - stator_teeth_length)
            * math.tan(math.radians(360.0 / slot_num) / 2.0)
            * 2.0
        )
        if denominator > 0.0:
            repaired["input_stator_teeth_width_ratio"] = stator_teeth_width / denominator

    stator_inner_radius = finite_float(row.get("input_stator_inner_radius"))
    rotator_gap = finite_float(row.get("input_rotator_gap"))
    shaft_ratio = finite_float(row.get("input_shaft_ratio"))
    if all(math.isfinite(value) for value in (stator_inner_radius, rotator_gap, shaft_ratio)):
        rotor_radius = stator_inner_radius - rotator_gap
        repaired["input_rotor_radius"] = rotor_radius
        repaired["input_shaft_radius"] = rotor_radius * shaft_ratio
    return repaired


def repair_derived_input_columns(df: Any) -> dict[str, int]:
    """Repair historical derived geometry columns before model training."""
    repaired = {column: 0 for column in DERIVED_INPUT_REPAIR_COLUMNS}
    for column in DERIVED_INPUT_REPAIR_COLUMNS:
        if column not in df.columns:
            df[column] = math.nan
    for index, row in df.iterrows():
        for column, value in repaired_derived_input_values(row).items():
            if column not in df.columns:
                continue
            current = finite_float(row.get(column))
            if not math.isfinite(current) or not math.isclose(current, value, rel_tol=1e-9, abs_tol=1e-9):
                df.at[index, column] = value
                repaired[column] += 1
    return repaired


def finite_column_coverage(data: Any, column: str) -> float:
    if data is None:
        return 1.0
    row_count = len(data)
    if row_count <= 0:
        return 0.0
    try:
        values = data[column]
    except Exception:
        values = [row.get(column) for row in data]
    finite_count = sum(1 for value in values if math.isfinite(finite_float(value)))
    return finite_count / row_count


def select_training_input_columns(
    available_columns: Iterable[str],
    data: Any = None,
    *,
    min_optional_coverage: float = 1.0,
) -> tuple[str, ...]:
    """Return model input columns, adding optional inputs only when densely populated."""
    available = set(available_columns)
    optional = tuple(
        column
        for column in OPTIONAL_INPUT_COLUMNS
        if column in available and finite_column_coverage(data, column) >= min_optional_coverage
    )
    return (*INPUT_COLUMNS, *optional)


def select_v2_training_input_columns(available_columns: Iterable[str]) -> tuple[str, ...]:
    required = tuple(dict.fromkeys((*INPUT_COLUMNS, *V2_REQUIRED_CONDITIONAL_INPUT_COLUMNS)))
    missing = missing_columns(available_columns, required)
    if missing:
        raise ValueError(f"missing v2 conditional input columns: {missing}")
    return required


def regression_metrics(y_true: Iterable[object], y_pred: Iterable[object]) -> dict[str, float]:
    true_values = [finite_float(value) for value in y_true]
    pred_values = [finite_float(value) for value in y_pred]
    pairs = [(true, pred) for true, pred in zip(true_values, pred_values) if math.isfinite(true) and math.isfinite(pred)]
    if not pairs:
        return {"MAE": math.nan, "RMSE": math.nan, "R2": math.nan, "MAPE_pct": math.nan}

    abs_errors = [abs(true - pred) for true, pred in pairs]
    squared_errors = [(true - pred) ** 2 for true, pred in pairs]
    mae = sum(abs_errors) / len(pairs)
    rmse = math.sqrt(sum(squared_errors) / len(pairs))

    mean_true = sum(true for true, _ in pairs) / len(pairs)
    ss_res = sum((true - pred) ** 2 for true, pred in pairs)
    ss_tot = sum((true - mean_true) ** 2 for true, _ in pairs)
    if ss_tot <= 0.0:
        r2 = 1.0 if ss_res <= 0.0 else 0.0
    else:
        r2 = 1.0 - ss_res / ss_tot

    percentage_errors = [abs((true - pred) / true) for true, pred in pairs if abs(true) >= 1e-12]
    mape = sum(percentage_errors) / len(percentage_errors) * 100.0 if percentage_errors else math.nan
    return {"MAE": mae, "RMSE": rmse, "R2": r2, "MAPE_pct": mape}


def split_conformal_absolute_residual(
    y_true: Iterable[object],
    y_pred: Iterable[object],
    *,
    coverage: float = V2_CONFORMAL_COVERAGE,
) -> dict[str, float | int]:
    if not 0.0 < coverage < 1.0:
        raise ValueError("conformal coverage must be greater than 0 and less than 1")
    residuals = sorted(
        abs(true - pred)
        for true, pred in zip(
            (finite_float(value) for value in y_true),
            (finite_float(value) for value in y_pred),
        )
        if math.isfinite(true) and math.isfinite(pred)
    )
    if not residuals:
        raise ValueError("split conformal calibration requires at least one finite residual")
    rank = min(len(residuals), int(math.ceil((len(residuals) + 1) * coverage)))
    return {
        "coverage": float(coverage),
        "calibration_rows": len(residuals),
        "rank": rank,
        "quantile_abs": float(residuals[rank - 1]),
    }


def feature_min_max_bounds(data: Any, columns: Iterable[str]) -> dict[str, dict[str, float]]:
    bounds: dict[str, dict[str, float]] = {}
    for column in columns:
        values = [finite_float(value) for value in data[column]]
        finite_values = [value for value in values if math.isfinite(value)]
        if not finite_values:
            raise ValueError(f"feature bounds require finite values: {column}")
        bounds[column] = {"min": float(min(finite_values)), "max": float(max(finite_values))}
    return bounds


def build_model(lgb: Any, params: dict[str, Any], seed: int, n_jobs: int) -> Any:
    merged = dict(BASE_PARAMS)
    merged.update(params)
    merged["random_state"] = seed
    merged["n_jobs"] = n_jobs
    return lgb.LGBMRegressor(**merged)


def predict_model(model: Any, x: Any) -> Any:
    if isinstance(model, (list, tuple)):
        if not model:
            raise ValueError("model ensemble must not be empty")
        member_predictions = [list(predict_model(member, x)) for member in model]
        if len({len(values) for values in member_predictions}) != 1:
            raise ValueError("model ensemble members returned different row counts")
        return [
            sum(values) / len(values)
            for values in zip(*member_predictions)
        ]
    best_iter = getattr(model, "best_iteration_", None)
    if best_iter is not None and best_iter > 0:
        return model.predict(x, num_iteration=best_iter)
    return model.predict(x)


def sample_params(rng: random.Random, search_space: dict[str, tuple[Any, ...]] = PARAM_SEARCH_SPACE) -> dict[str, Any]:
    return {key: rng.choice(values) for key, values in search_space.items()}


def prepare_training_data(
    deps: TrainingDependencies,
    data_paths: Iterable[Path],
    drop_duplicate_case_id: bool,
    remove_output_outliers: bool,
    outlier_iqr_weight: float,
    *,
    v2: bool = False,
    expected_fingerprints: dict[str, str] | None = None,
) -> PreparedData:
    paths = [Path(path) for path in data_paths]
    missing_files = [path for path in paths if not path.exists()]
    if missing_files:
        raise FileNotFoundError(f"missing data file(s): {missing_files}")

    frames = []
    for path in paths:
        part = deps.pd.read_csv(path, encoding="utf-8-sig")
        part["source_file"] = path.name
        frames.append(part)
        ok_count = part["status"].astype(str).str.lower().eq("ok").sum() if "status" in part.columns else "n/a"
        print(f"loaded path={path} rows={len(part)} columns={len(part.columns)} ok={ok_count}")

    raw_df = deps.pd.concat(frames, ignore_index=True, sort=False)
    fingerprints: dict[str, str] = {}
    geometry_group_column: str | None = None
    if v2:
        fingerprints = validate_v2_fingerprints(raw_df, expected_fingerprints)
        ensure_v2_canonical_beta_column(raw_df, fingerprints)
        geometry_group_column = resolve_v2_geometry_group_column(raw_df)
    before_dedup = len(raw_df)
    dropped_duplicate_case_id_rows = 0
    if drop_duplicate_case_id and "case_id" in raw_df.columns:
        raw_df = raw_df.drop_duplicates(subset="case_id", keep="last").reset_index(drop=True)
        dropped_duplicate_case_id_rows = before_dedup - len(raw_df)
        print(f"dropped_duplicate_case_id_rows={dropped_duplicate_case_id_rows}")
    print(f"united_raw_rows={len(raw_df)} columns={len(raw_df.columns)}")

    requested_output_columns = V2_PRIMITIVE_OUTPUT_COLUMNS if v2 else REQUESTED_OUTPUT_COLUMNS
    output_columns, output_name_map = resolve_output_columns(
        raw_df.columns,
        requested_columns=requested_output_columns,
    )
    auxiliary_output_columns: tuple[str, ...] = ()
    if v2:
        auxiliary_output_columns, auxiliary_name_map = resolve_output_columns(
            raw_df.columns,
            requested_columns=V2_AUXILIARY_OUTPUT_COLUMNS,
        )
        output_name_map.update(auxiliary_name_map)
    all_modeled_output_columns = (*output_columns, *auxiliary_output_columns)
    missing_inputs = missing_columns(raw_df.columns, RAW_INPUT_COLUMNS)
    if missing_inputs:
        raise ValueError(f"missing input columns: {missing_inputs}")

    df = raw_df.copy()
    numeric_columns = tuple(
        dict.fromkeys(
            (
                *RAW_INPUT_COLUMNS,
                *DERIVED_INPUT_REPAIR_COLUMNS,
                *OPTIONAL_INPUT_COLUMNS,
                *V2_REQUIRED_CONDITIONAL_INPUT_COLUMNS,
                *all_modeled_output_columns,
            )
        )
    )
    for column in numeric_columns:
        if column in df.columns:
            df[column] = deps.pd.to_numeric(df[column], errors="coerce")
    repaired_derived_inputs = repair_derived_input_columns(df)
    input_columns = select_v2_training_input_columns(df.columns) if v2 else select_training_input_columns(df.columns, df)
    print(
        "repaired_derived_input_rows "
        + " ".join(f"{column}={count}" for column, count in repaired_derived_inputs.items())
    )
    print("input_columns=" + ",".join(input_columns))

    status_ok = df["status"].astype(str).str.lower().eq("ok") if "status" in df.columns else deps.pd.Series(True, index=df.index)
    finite_inputs = deps.np.isfinite(df[list(input_columns)]).all(axis=1)
    finite_outputs = deps.np.isfinite(df[list(all_modeled_output_columns)]).all(axis=1)
    physical_sanity_ok = deps.pd.Series(True, index=df.index)
    for column in EFFICIENCY_OUTPUT_COLUMNS:
        if column not in df.columns:
            continue
        values = deps.pd.to_numeric(df[column], errors="coerce")
        physical_sanity_ok &= values.isna() | values.between(0.0, 100.0)
    valid_mask = status_ok & finite_inputs & finite_outputs & physical_sanity_ok
    status_rejected_rows = int((~status_ok).sum())
    nonfinite_input_rows = int((~finite_inputs).sum())
    nonfinite_output_rows = int((~finite_outputs).sum())
    physical_sanity_rejected_rows = int((~physical_sanity_ok).sum())
    valid_rows_before_outliers = int(valid_mask.sum())
    valid_df = df.loc[valid_mask].copy()

    removed_output_outliers = 0
    if v2 and remove_output_outliers:
        print("v2_output_outlier_filter=disabled")
    if remove_output_outliers and not v2:
        keep = deps.pd.Series(True, index=valid_df.index)
        for column in output_columns:
            q1 = valid_df[column].quantile(0.25)
            q3 = valid_df[column].quantile(0.75)
            iqr = q3 - q1
            low = q1 - outlier_iqr_weight * iqr
            high = q3 + outlier_iqr_weight * iqr
            keep &= valid_df[column].between(low, high)
        before_outlier_filter = len(valid_df)
        valid_df = valid_df.loc[keep].copy()
        removed_output_outliers = before_outlier_filter - len(valid_df)
        print(f"removed_output_outlier_rows={removed_output_outliers}")

    for requested_col, actual_col in output_name_map.items():
        print(f"output_column requested={requested_col} actual={actual_col}")
    quality_report = TrainingQualityReport(
        raw_rows=before_dedup,
        rows_after_dedup=len(raw_df),
        dropped_duplicate_case_id_rows=dropped_duplicate_case_id_rows,
        status_rejected_rows=status_rejected_rows,
        nonfinite_input_rows=nonfinite_input_rows,
        nonfinite_output_rows=nonfinite_output_rows,
        physical_sanity_rejected_rows=physical_sanity_rejected_rows,
        valid_rows_before_outliers=valid_rows_before_outliers,
        removed_output_outliers=removed_output_outliers,
        valid_rows=len(valid_df),
    )
    print(
        "training_filter_rows "
        + " ".join(f"{key}={value}" for key, value in quality_report.as_metadata().items())
    )
    print(f"valid_rows={len(valid_df)} raw_rows={len(raw_df)}")
    return PreparedData(
        raw_df=raw_df,
        valid_df=valid_df,
        input_columns=input_columns,
        output_columns=output_columns,
        output_name_map=output_name_map,
        removed_output_outliers=removed_output_outliers,
        repaired_derived_inputs=repaired_derived_inputs,
        quality_report=quality_report,
        schema_version=V2_DATASET_SCHEMA_VERSION if v2 else "legacy",
        geometry_group_column=geometry_group_column,
        fingerprints=fingerprints,
        auxiliary_output_columns=auxiliary_output_columns,
    )


def split_training_data(
    deps: TrainingDependencies,
    prepared: PreparedData,
    test_size: float,
    val_size: float,
    seed: int,
) -> SplitData:
    x = prepared.valid_df[list(prepared.input_columns)].copy()
    y_columns = (*prepared.output_columns, *prepared.auxiliary_output_columns)
    y = prepared.valid_df[list(y_columns)].copy()
    if prepared.geometry_group_column:
        group_values = prepared.valid_df[prepared.geometry_group_column].map(normalized_nonempty_text)
        if "doe_split" not in prepared.valid_df.columns:
            raise ValueError("v2 grouped training requires the preassigned doe_split column")
        assignments = validated_preassigned_group_partitions(
            group_values,
            prepared.valid_df["doe_split"],
        )
        split_labels = group_values.map(assignments)
        train_mask = split_labels.eq("train")
        val_mask = split_labels.eq("calibration")
        test_mask = split_labels.eq("test")
        if not train_mask.any() or not val_mask.any() or not test_mask.any():
            raise ValueError("v2 grouped split produced an empty train/calibration/test partition")
        train_groups = tuple(sorted(group_values.loc[train_mask].unique().tolist()))
        val_groups = tuple(sorted(group_values.loc[val_mask].unique().tolist()))
        test_groups = tuple(sorted(group_values.loc[test_mask].unique().tolist()))
        if set(train_groups) & set(val_groups) or set(train_groups) & set(test_groups) or set(val_groups) & set(test_groups):
            raise RuntimeError("v2 geometry group leakage detected")
        split = SplitData(
            x_train=x.loc[train_mask].copy(),
            x_val=x.loc[val_mask].copy(),
            x_test=x.loc[test_mask].copy(),
            y_train=y.loc[train_mask].copy(),
            y_val=y.loc[val_mask].copy(),
            y_test=y.loc[test_mask].copy(),
            group_column=prepared.geometry_group_column,
            train_group_ids=train_groups,
            val_group_ids=val_groups,
            test_group_ids=test_groups,
        )
        print(
            f"split_rows train={len(split.x_train)} calibration={len(split.x_val)} test={len(split.x_test)} "
            f"split_groups train={len(train_groups)} calibration={len(val_groups)} test={len(test_groups)}"
        )
        return split

    x_train_val, x_test, y_train_val, y_test = deps.train_test_split(x, y, test_size=test_size, random_state=seed)
    relative_val_size = val_size / (1.0 - test_size)
    x_train, x_val, y_train, y_val = deps.train_test_split(
        x_train_val,
        y_train_val,
        test_size=relative_val_size,
        random_state=seed,
    )
    print(f"split_rows train={len(x_train)} val={len(x_val)} test={len(x_test)}")
    return SplitData(x_train=x_train, x_val=x_val, x_test=x_test, y_train=y_train, y_val=y_val, y_test=y_test)


def select_v2_test_evaluation_split(
    prepared: PreparedData,
    outer_split: SplitData,
    audit_case_plan: Path | None,
) -> tuple[SplitData, dict[str, Any]]:
    """Keep fitting partitions unchanged while optionally restricting decisive test metrics."""

    if not prepared.geometry_group_column:
        raise ValueError("v2 test evaluation requires a geometry group column")
    if audit_case_plan is None:
        return outer_split, {
            "scope": V2_TEST_EVALUATION_SCOPE_ALL,
            "case_plan": "",
            "case_plan_sha256": "",
            "case_plan_rows": 0,
            "geometry_column": prepared.geometry_group_column,
            "rows": len(outer_split.x_test),
            "groups": len(outer_split.test_group_ids),
            "test_case_ids_sha256": "",
        }
    if "case_id" not in prepared.valid_df.columns or "doe_split" not in prepared.valid_df.columns:
        raise ValueError("v2 audit evaluation requires case_id and doe_split in valid data")

    plan_rows, contract = load_v2_audit_case_plan(
        audit_case_plan,
        geometry_column=prepared.geometry_group_column,
    )
    identity_columns = ["case_id", "doe_split", prepared.geometry_group_column]
    data_records = prepared.valid_df[identity_columns].to_dict(orient="records")
    test_case_ids, test_groups = validate_v2_audit_records(
        plan_rows,
        data_records,
        geometry_column=prepared.geometry_group_column,
    )
    normalized_data_ids = prepared.valid_df["case_id"].map(normalized_nonempty_text)
    index_by_case = {
        case_id: index
        for index, case_id in zip(prepared.valid_df.index, normalized_data_ids)
        if case_id is not None
    }
    audit_indices = [index_by_case[case_id] for case_id in test_case_ids]
    outer_test_indices = set(outer_split.x_test.index)
    outside_test = [index for index in audit_indices if index not in outer_test_indices]
    if outside_test:
        raise ValueError("v2 audit rows are not wholly inside the preassigned outer test split")

    evaluation_split = SplitData(
        x_train=outer_split.x_train,
        x_val=outer_split.x_val,
        x_test=outer_split.x_test.loc[audit_indices].copy(),
        y_train=outer_split.y_train,
        y_val=outer_split.y_val,
        y_test=outer_split.y_test.loc[audit_indices].copy(),
        group_column=outer_split.group_column,
        train_group_ids=outer_split.train_group_ids,
        val_group_ids=outer_split.val_group_ids,
        test_group_ids=test_groups,
    )
    if len(evaluation_split.x_test) != contract["rows"]:
        raise RuntimeError("v2 audit test row count is inconsistent with its case plan")
    print(
        "test_evaluation_scope=audit_case_plan_test "
        f"rows={len(evaluation_split.x_test)} groups={len(test_groups)} "
        f"case_plan={audit_case_plan}"
    )
    return evaluation_split, contract


def build_v2_model_selection_split(
    prepared: PreparedData,
    outer_split: SplitData,
    *,
    seed: int,
) -> SplitData | None:
    if not prepared.geometry_group_column:
        raise ValueError("v2 model selection requires a geometry group column")
    group_values = prepared.valid_df.loc[outer_split.x_train.index, prepared.geometry_group_column].map(
        normalized_nonempty_text
    )
    if len(set(group_values.tolist())) < 2:
        return None
    assignments = deterministic_model_selection_partitions(group_values, seed=seed)
    roles = group_values.map(assignments)
    fit_mask = roles.eq("fit")
    selection_mask = roles.eq("model_selection")
    fit_groups = tuple(sorted(group_values.loc[fit_mask].unique().tolist()))
    selection_groups = tuple(sorted(group_values.loc[selection_mask].unique().tolist()))
    if not fit_mask.any() or not selection_mask.any() or set(fit_groups) & set(selection_groups):
        raise RuntimeError("v2 inner model-selection group split is invalid")
    split = SplitData(
        x_train=outer_split.x_train.loc[fit_mask].copy(),
        x_val=outer_split.x_train.loc[selection_mask].copy(),
        x_test=outer_split.x_test,
        y_train=outer_split.y_train.loc[fit_mask].copy(),
        y_val=outer_split.y_train.loc[selection_mask].copy(),
        y_test=outer_split.y_test,
        group_column=prepared.geometry_group_column,
        train_group_ids=fit_groups,
        val_group_ids=selection_groups,
        test_group_ids=outer_split.test_group_ids,
    )
    print(
        f"model_selection_rows fit={len(split.x_train)} holdout={len(split.x_val)} "
        f"model_selection_groups fit={len(fit_groups)} holdout={len(selection_groups)}"
    )
    return split


def fit_lgbm(
    deps: TrainingDependencies,
    split: SplitData,
    target_col: str,
    params: dict[str, Any],
    seed: int,
    n_jobs: int,
    early_stopping_rounds: int,
) -> Any:
    model = build_model(deps.lgb, params, seed=seed, n_jobs=n_jobs)
    model.fit(
        split.x_train,
        split.y_train[target_col],
        eval_set=[(split.x_val, split.y_val[target_col])],
        eval_metric="l2",
        callbacks=[deps.lgb.early_stopping(early_stopping_rounds, verbose=False), deps.lgb.log_evaluation(0)],
    )
    return model


def fit_lgbm_full_training(
    deps: TrainingDependencies,
    split: SplitData,
    target_col: str,
    params: dict[str, Any],
    seed: int,
    n_jobs: int,
) -> Any:
    model = build_model(deps.lgb, params, seed=seed, n_jobs=n_jobs)
    model.fit(split.x_train, split.y_train[target_col])
    return model


def tune_params_for_target(
    deps: TrainingDependencies,
    split: SplitData,
    target_col: str,
    n_trials: int,
    seed: int,
    n_jobs: int,
    early_stopping_rounds: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rng = random.Random(stable_target_seed(target_col, seed))
    best_params = dict(BASE_PARAMS)
    best_rmse = math.inf
    records: list[dict[str, Any]] = []
    for trial in range(1, n_trials + 1):
        params = sample_params(rng)
        model = fit_lgbm(deps, split, target_col, params, seed, n_jobs, early_stopping_rounds)
        val_pred = predict_model(model, split.x_val)
        metric = regression_metrics(split.y_val[target_col], val_pred)
        record = dict(params)
        record.update({"target": target_col, "trial": trial, "val_RMSE": metric["RMSE"], "val_R2": metric["R2"]})
        records.append(record)
        if metric["RMSE"] < best_rmse:
            best_rmse = metric["RMSE"]
            best_params = params
    records.sort(key=lambda row: finite_float(row.get("val_RMSE")))
    return best_params, records


def train_one_target(
    deps: TrainingDependencies,
    split: SplitData,
    target_col: str,
    enable_tuning: bool,
    n_tuning_trials: int,
    seed: int,
    n_jobs: int,
    early_stopping_rounds: int,
    validation_split_name: str = "val",
) -> tuple[Any, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if enable_tuning:
        best_params, tuning_records = tune_params_for_target(
            deps,
            split,
            target_col,
            n_tuning_trials,
            seed,
            n_jobs,
            early_stopping_rounds,
        )
    else:
        best_params = dict(BASE_PARAMS)
        tuning_records = []

    model = fit_lgbm(deps, split, target_col, best_params, seed, n_jobs, early_stopping_rounds)
    metric_rows: list[dict[str, Any]] = []
    for split_name, split_x, split_y in (
        ("train", split.x_train, split.y_train[target_col]),
        (validation_split_name, split.x_val, split.y_val[target_col]),
        ("test", split.x_test, split.y_test[target_col]),
    ):
        pred = predict_model(model, split_x)
        metrics = regression_metrics(split_y, pred)
        metrics.update({"target": target_col, "split": split_name, "best_iteration": getattr(model, "best_iteration_", None)})
        metric_rows.append(metrics)
    return model, best_params, metric_rows, tuning_records


def train_one_target_v2(
    deps: TrainingDependencies,
    outer_split: SplitData,
    model_selection_split: SplitData | None,
    target_col: str,
    enable_tuning: bool,
    n_tuning_trials: int,
    seed: int,
    n_jobs: int,
    early_stopping_rounds: int,
    ensemble_size: int = 1,
) -> tuple[Any, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if ensemble_size < 1:
        raise ValueError("v2 ensemble_size must be >= 1")
    tuning_records: list[dict[str, Any]] = []
    best_params = dict(BASE_PARAMS)
    best_iteration: int | None = None
    if model_selection_split is not None:
        if enable_tuning:
            best_params, tuning_records = tune_params_for_target(
                deps,
                model_selection_split,
                target_col,
                n_tuning_trials,
                seed,
                n_jobs,
                early_stopping_rounds,
            )
        selection_model = fit_lgbm(
            deps,
            model_selection_split,
            target_col,
            best_params,
            seed,
            n_jobs,
            early_stopping_rounds,
        )
        selected = getattr(selection_model, "best_iteration_", None)
        if selected is not None and int(selected) > 0:
            best_iteration = int(selected)
    elif enable_tuning:
        print(f"v2_tuning_skipped target={target_col} reason=insufficient_outer_train_geometry_groups")

    final_params = dict(best_params)
    if best_iteration is not None:
        final_params["n_estimators"] = best_iteration
    ensemble = [
        fit_lgbm_full_training(deps, outer_split, target_col, final_params, seed, n_jobs)
    ]
    for member_index in range(1, ensemble_size):
        member_seed = stable_target_seed(f"{target_col}:ensemble:{member_index}", seed)
        ensemble.append(
            fit_lgbm_full_training(
                deps,
                outer_split,
                target_col,
                final_params,
                member_seed,
                n_jobs,
            )
        )
    model: Any = ensemble[0] if ensemble_size == 1 else tuple(ensemble)
    metric_rows: list[dict[str, Any]] = []
    for split_name, split_x, split_y in (
        ("train", outer_split.x_train, outer_split.y_train[target_col]),
        ("calibration", outer_split.x_val, outer_split.y_val[target_col]),
        ("test", outer_split.x_test, outer_split.y_test[target_col]),
    ):
        pred = predict_model(model, split_x)
        metrics = regression_metrics(split_y, pred)
        metrics.update({"target": target_col, "split": split_name, "best_iteration": best_iteration})
        metric_rows.append(metrics)
    return model, final_params, metric_rows, tuning_records


def v2_derived_metric_rows(
    split: SplitData,
    models: dict[str, Any],
    output_name_map: dict[str, str],
) -> list[dict[str, Any]]:
    requested_for_derivation = (
        "output_torque_last_avg_nm",
        "output_coreloss_last_avg_w",
        "output_solidloss_last_avg_w",
    )
    actual = {column: output_name_map[column] for column in requested_for_derivation}
    missing_models = [actual[column] for column in requested_for_derivation if actual[column] not in models]
    if missing_models:
        raise ValueError(f"missing primitive models for v2 derived metrics: {missing_models}")

    rows: list[dict[str, Any]] = []
    for split_name, split_x, split_y in (
        ("train", split.x_train, split.y_train),
        ("calibration", split.x_val, split.y_val),
        ("test", split.x_test, split.y_test),
    ):
        predicted = {
            requested: list(predict_model(models[actual[requested]], split_x))
            for requested in requested_for_derivation
        }
        true_derived: dict[str, list[float]] = {column: [] for column in V2_DERIVED_OUTPUT_COLUMNS}
        predicted_derived: dict[str, list[float]] = {column: [] for column in V2_DERIVED_OUTPUT_COLUMNS}
        for position in range(len(split_x)):
            common = {
                "i_peak_a": split_x.iloc[position]["input_i_peak_a"],
                "phase_resistance_ohm": split_x.iloc[position]["input_phase_resistance_ohm"],
                "rpm": split_x.iloc[position]["input_base_rpm"],
            }
            truth = derive_v2_outputs(
                torque_avg_nm=split_y.iloc[position][actual["output_torque_last_avg_nm"]],
                core_loss_w=split_y.iloc[position][actual["output_coreloss_last_avg_w"]],
                solid_loss_w=split_y.iloc[position][actual["output_solidloss_last_avg_w"]],
                **common,
            )
            prediction = derive_v2_outputs(
                torque_avg_nm=predicted["output_torque_last_avg_nm"][position],
                core_loss_w=predicted["output_coreloss_last_avg_w"][position],
                solid_loss_w=predicted["output_solidloss_last_avg_w"][position],
                **common,
            )
            for column in V2_DERIVED_OUTPUT_COLUMNS:
                true_derived[column].append(truth[column])
                predicted_derived[column].append(prediction[column])

        for column in V2_DERIVED_OUTPUT_COLUMNS:
            metrics = regression_metrics(true_derived[column], predicted_derived[column])
            metrics.update({"target": column, "split": split_name, "best_iteration": None})
            rows.append(metrics)
    return rows


def v2_conformal_absolute_residuals(
    split: SplitData,
    models: dict[str, Any],
    modeled_output_columns: Iterable[str],
    *,
    coverage: float,
) -> dict[str, dict[str, float | int]]:
    quantiles: dict[str, dict[str, float | int]] = {}
    for target_col in modeled_output_columns:
        if target_col not in models:
            raise ValueError(f"missing model for v2 conformal calibration: {target_col}")
        predictions = predict_model(models[target_col], split.x_val)
        quantiles[target_col] = split_conformal_absolute_residual(
            split.y_val[target_col],
            predictions,
            coverage=coverage,
        )
    return quantiles


def safe_model_name(target_col: str) -> str:
    return target_col.replace("/", "_").replace(" ", "_")


def write_metrics_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=METRIC_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_tuning_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["target", "trial", *PARAM_SEARCH_SPACE.keys(), "val_RMSE", "val_R2"]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def summarize_test_threshold(metric_rows: list[dict[str, Any]], threshold: float) -> tuple[str, int]:
    test_rows = [row for row in metric_rows if str(row.get("split", "")).lower() == "test"]
    r2_values = [finite_float(row.get("R2")) for row in test_rows]
    finite_r2 = [value for value in r2_values if math.isfinite(value)]
    failures = sum(1 for value in r2_values if not math.isfinite(value) or value < threshold)
    min_r2 = min(finite_r2) if finite_r2 else math.nan
    avg_r2 = sum(finite_r2) / len(finite_r2) if finite_r2 else math.nan
    return (
        f"test_r2 targets={len(test_rows)} failures={failures} threshold={threshold:.12g} "
        f"min_R2={min_r2:.12g} avg_R2={avg_r2:.12g}",
        failures,
    )


def primary_test_r2_by_target(metric_rows: Iterable[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in metric_rows:
        if str(row.get("split", "")).lower() != "test":
            continue
        target = str(row.get("target") or "")
        if target:
            result[target] = finite_float(row.get("R2"))
    return result


def single_target_test_r2_gate(
    metric_rows: Iterable[dict[str, Any]],
    target: str,
    threshold: float,
) -> tuple[float | None, bool, bool]:
    test_rows = [
        row
        for row in metric_rows
        if str(row.get("split", "")).lower() == "test"
        and str(row.get("target") or "") == target
    ]
    complete = len(test_rows) == 1
    raw_r2 = finite_float(test_rows[0].get("R2")) if complete else math.nan
    test_r2 = raw_r2 if math.isfinite(raw_r2) else None
    passed = bool(complete and test_r2 is not None and test_r2 >= threshold)
    return test_r2, complete, passed


def threshold_gate_failed(
    *,
    v2: bool,
    primary_gate_passed: bool,
    voltage_gate_passed: bool,
    metric_failures: int,
) -> bool:
    if v2:
        return metric_failures > 0 or not primary_gate_passed or not voltage_gate_passed
    return metric_failures > 0


def write_verification_csv(path: Path, metric_rows: list[dict[str, Any]], threshold: float) -> int:
    import verify_regression_metrics

    test_rows = [dict(row) for row in metric_rows if str(row.get("split", "")).lower() == "test"]
    summary_rows, _, failures = verify_regression_metrics.summarize_split(test_rows, threshold)
    verify_regression_metrics.write_summary(path, summary_rows)
    return failures


def validate_training_options(args: argparse.Namespace) -> None:
    if not 0.0 < args.test_size < 1.0:
        raise ValueError("--test-size must be greater than 0 and less than 1")
    if not 0.0 < args.val_size < 1.0:
        raise ValueError("--val-size must be greater than 0 and less than 1")
    if args.test_size + args.val_size >= 1.0:
        raise ValueError("--test-size + --val-size must be less than 1")
    if args.min_valid_rows < 3:
        raise ValueError("--min-valid-rows must be at least 3")
    if args.n_tuning_trials < 0:
        raise ValueError("--n-tuning-trials must be zero or greater")
    if args.early_stopping_rounds < 1:
        raise ValueError("--early-stopping-rounds must be at least 1")
    if args.outlier_iqr_weight < 0.0:
        raise ValueError("--outlier-iqr-weight must be zero or greater")
    if not math.isfinite(args.r2_threshold):
        raise ValueError("--r2-threshold must be finite")
    if not 0.0 < args.conformal_coverage < 1.0:
        raise ValueError("--conformal-coverage must be greater than 0 and less than 1")
    if args.max_invalid_training_rows is not None and args.max_invalid_training_rows < 0:
        raise ValueError("--max-invalid-training-rows must be zero or greater")
    if args.max_removed_output_outlier_rows is not None and args.max_removed_output_outlier_rows < 0:
        raise ValueError("--max-removed-output-outlier-rows must be zero or greater")
    if args.ensemble_size < 1:
        raise ValueError("--ensemble-size must be at least 1")
    if args.expected_fingerprint and not args.v2:
        raise ValueError("--expected-fingerprint requires --v2")
    if args.v2_audit_case_plan is not None and not args.v2:
        raise ValueError("--v2-audit-case-plan requires --v2")
    parse_expected_fingerprints(args.expected_fingerprint)


def run_training(args: argparse.Namespace, deps: TrainingDependencies) -> int:
    set_seed(args.seed, deps.np)
    args.model_dir.mkdir(parents=True, exist_ok=True)
    expected_fingerprints = parse_expected_fingerprints(args.expected_fingerprint)
    prepared = prepare_training_data(
        deps,
        args.data,
        drop_duplicate_case_id=args.drop_duplicate_case_id,
        remove_output_outliers=args.remove_output_outliers,
        outlier_iqr_weight=args.outlier_iqr_weight,
        v2=args.v2,
        expected_fingerprints=expected_fingerprints,
    )
    if len(prepared.valid_df) < args.min_valid_rows:
        raise RuntimeError(f"valid data is too small for robust training: {len(prepared.valid_df)} rows")
    quality_failures = prepared.quality_report.failure_reasons(
        max_invalid_training_rows=args.max_invalid_training_rows,
        max_removed_output_outlier_rows=args.max_removed_output_outlier_rows,
    )
    if quality_failures:
        raise RuntimeError("training data quality gate failed: " + "; ".join(quality_failures))

    split = split_training_data(deps, prepared, args.test_size, args.val_size, args.seed)
    evaluation_split, test_evaluation = (
        select_v2_test_evaluation_split(prepared, split, args.v2_audit_case_plan)
        if args.v2
        else (split, {})
    )
    model_selection_split = build_v2_model_selection_split(prepared, split, seed=args.seed) if args.v2 else None
    models: dict[str, Any] = {}
    model_paths: dict[str, str] = {}
    best_params_by_target: dict[str, dict[str, Any]] = {}
    metric_rows: list[dict[str, Any]] = []
    auxiliary_metric_rows: list[dict[str, Any]] = []
    tuning_records: list[dict[str, Any]] = []

    for target_col in prepared.output_columns:
        print(f"training target={target_col} tuning={args.enable_tuning}")
        if args.v2:
            model, best_params, target_metric_rows, target_tuning_records = train_one_target_v2(
                deps,
                evaluation_split,
                model_selection_split,
                target_col,
                args.enable_tuning,
                args.n_tuning_trials,
                args.seed,
                args.n_jobs,
                args.early_stopping_rounds,
                args.ensemble_size,
            )
        else:
            model, best_params, target_metric_rows, target_tuning_records = train_one_target(
                deps,
                split,
                target_col,
                args.enable_tuning,
                args.n_tuning_trials,
                args.seed,
                args.n_jobs,
                args.early_stopping_rounds,
            )
        models[target_col] = model
        best_params_by_target[target_col] = best_params
        metric_rows.extend(target_metric_rows)
        tuning_records.extend(target_tuning_records)

    if args.v2:
        for target_col in prepared.auxiliary_output_columns:
            print(f"training auxiliary_target={target_col} tuning={args.enable_tuning}")
            model, best_params, target_metric_rows, target_tuning_records = train_one_target_v2(
                deps,
                evaluation_split,
                model_selection_split,
                target_col,
                args.enable_tuning,
                args.n_tuning_trials,
                args.seed,
                args.n_jobs,
                args.early_stopping_rounds,
                args.ensemble_size,
            )
            models[target_col] = model
            best_params_by_target[target_col] = best_params
            auxiliary_metric_rows.extend(target_metric_rows)
            tuning_records.extend(target_tuning_records)
        metric_rows.extend(v2_derived_metric_rows(evaluation_split, models, prepared.output_name_map))

    modeled_output_columns = (*prepared.output_columns, *prepared.auxiliary_output_columns)
    conformal_absolute_residuals = (
        v2_conformal_absolute_residuals(
            split,
            models,
            modeled_output_columns,
            coverage=args.conformal_coverage,
        )
        if args.v2
        else {}
    )
    feature_bounds = feature_min_max_bounds(split.x_train, prepared.input_columns) if args.v2 else {}

    for target_col, model in models.items():
        model_path = args.model_dir / f"{safe_model_name(target_col)}_lgbm.pkl"
        with model_path.open("wb") as file:
            pickle.dump(model, file)
        model_paths[target_col] = str(model_path)

    metrics_path = args.model_dir / "metrics.csv"
    auxiliary_metrics_path = args.model_dir / "auxiliary_metrics.csv"
    tuning_path = args.model_dir / "tuning_trials.csv"
    metadata_path = args.model_dir / "metadata.json"
    write_metrics_csv(metrics_path, metric_rows)
    if args.v2:
        write_metrics_csv(auxiliary_metrics_path, auxiliary_metric_rows)
    write_tuning_csv(tuning_path, tuning_records)
    threshold_summary, failures = summarize_test_threshold(metric_rows, args.r2_threshold)
    primary_test_r2 = primary_test_r2_by_target(metric_rows) if args.v2 else {}
    primary_gate_complete = set(primary_test_r2) == set(V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS)
    primary_gate_passed = bool(
        args.v2
        and primary_gate_complete
        and all(
            math.isfinite(primary_test_r2[target])
            and primary_test_r2[target] >= args.r2_threshold
            for target in V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS
        )
    )
    voltage_target = (
        prepared.output_name_map[V2_AUXILIARY_OUTPUT_COLUMNS[0]]
        if args.v2
        else ""
    )
    voltage_test_r2, voltage_gate_complete, voltage_gate_passed = (
        single_target_test_r2_gate(
            auxiliary_metric_rows,
            voltage_target,
            args.r2_threshold,
        )
        if args.v2
        else (None, False, False)
    )

    metadata = {
        "training_schema": prepared.schema_version,
        "data_paths": [str(path) for path in args.data],
        "drop_duplicate_case_id": bool(args.drop_duplicate_case_id),
        "source_files": sorted(prepared.raw_df["source_file"].dropna().unique().tolist()) if "source_file" in prepared.raw_df.columns else [],
        "input_columns": list(prepared.input_columns),
        "requested_output_columns": list(
            (*V2_PRIMITIVE_OUTPUT_COLUMNS, *V2_DERIVED_OUTPUT_COLUMNS, *V2_AUXILIARY_OUTPUT_COLUMNS)
            if args.v2
            else REQUESTED_OUTPUT_COLUMNS
        ),
        "modeled_output_columns": list(modeled_output_columns),
        "primary_modeled_output_columns": list(prepared.output_columns),
        "auxiliary_output_columns": list(prepared.auxiliary_output_columns),
        "derived_output_columns": list(V2_DERIVED_OUTPUT_COLUMNS) if args.v2 else [],
        "actual_output_columns": list(modeled_output_columns),
        "output_name_map": prepared.output_name_map,
        "raw_rows": int(len(prepared.raw_df)),
        "valid_rows": int(len(prepared.valid_df)),
        "removed_output_outliers": int(prepared.removed_output_outliers),
        "output_outlier_filter_enabled": bool(args.remove_output_outliers and not args.v2),
        "repaired_derived_inputs": prepared.repaired_derived_inputs,
        "training_quality": prepared.quality_report.as_metadata(),
        "geometry_group_column": prepared.geometry_group_column or "",
        "split_strategy": "preassigned_geometry_group" if args.v2 else "random_rows",
        "split_group_counts": {
            "train": len(split.train_group_ids),
            "calibration": len(split.val_group_ids),
            "test": len(split.test_group_ids),
        },
        "test_evaluation": test_evaluation,
        "model_selection_group_counts": {
            "fit": len(model_selection_split.train_group_ids) if model_selection_split else len(split.train_group_ids),
            "holdout": len(model_selection_split.val_group_ids) if model_selection_split else 0,
        },
        "conformal_calibration_isolated": bool(args.v2),
        "ensemble_size": int(args.ensemble_size) if args.v2 else 1,
        "r2_threshold": float(args.r2_threshold),
        "primary_test_r2": primary_test_r2,
        "primary_test_r2_gate_complete": primary_gate_complete if args.v2 else False,
        "primary_test_r2_gate_passed": primary_gate_passed,
        "primary_test_r2_failures": int(failures),
        "voltage_r2_threshold": float(args.r2_threshold),
        "voltage_test_r2": voltage_test_r2,
        "voltage_test_r2_gate_complete": voltage_gate_complete,
        "voltage_test_r2_gate_passed": voltage_gate_passed,
        "fingerprints": prepared.fingerprints or {},
        "fingerprint_columns": list(V2_FINGERPRINT_COLUMNS) if args.v2 else [],
        "conformal_absolute_residuals": conformal_absolute_residuals,
        "conformal_coverage": float(args.conformal_coverage) if args.v2 else None,
        "feature_bounds": feature_bounds,
        "feature_bounds_source": "train" if args.v2 else "",
        "enable_tuning": bool(args.enable_tuning),
        "n_tuning_trials": int(args.n_tuning_trials),
        "seed": int(args.seed),
        "stable_target_seed": True,
        "best_params_by_target": best_params_by_target,
        "model_paths": model_paths,
        "auxiliary_model_paths": {
            column: model_paths[column]
            for column in prepared.auxiliary_output_columns
        },
        "metrics_path": str(metrics_path),
        "auxiliary_metrics_path": str(auxiliary_metrics_path) if args.v2 else "",
        "tuning_trials_path": str(tuning_path) if tuning_records else "",
    }
    with metadata_path.open("w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)

    if args.verification_output:
        verification_failures = write_verification_csv(args.verification_output, metric_rows, args.r2_threshold)
        print(f"wrote_regression_verification path={args.verification_output} failures={verification_failures}")

    print(f"saved_model_dir={args.model_dir}")
    print(f"saved_metrics={metrics_path}")
    print(f"saved_metadata={metadata_path}")
    print(threshold_summary)
    gate_failed = threshold_gate_failed(
        v2=args.v2,
        primary_gate_passed=primary_gate_passed,
        voltage_gate_passed=voltage_gate_passed,
        metric_failures=failures,
    )
    return 1 if args.fail_on_threshold and gate_failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train deterministic LightGBM IPMSM surrogate models.")
    parser.add_argument("--data", nargs="+", type=Path, default=list(DEFAULT_DATA_PATHS), help="Simulation result CSV files.")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR, help="Directory for model artifacts.")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--test-size", type=float, default=TEST_SIZE)
    parser.add_argument("--val-size", type=float, default=VAL_SIZE)
    parser.add_argument("--min-valid-rows", type=int, default=20)
    parser.add_argument("--n-jobs", type=int, default=-1)
    parser.add_argument("--early-stopping-rounds", type=int, default=80)
    parser.add_argument("--n-tuning-trials", type=int, default=N_TUNING_TRIALS)
    parser.add_argument("--outlier-iqr-weight", type=float, default=OUTLIER_IQR_WEIGHT)
    parser.add_argument("--r2-threshold", type=float, default=R2_THRESHOLD)
    parser.add_argument("--conformal-coverage", type=float, default=V2_CONFORMAL_COVERAGE)
    parser.add_argument("--ensemble-size", type=int, default=V2_DEFAULT_ENSEMBLE_SIZE)
    parser.add_argument("--verification-output", type=Path, help="Optional compact R2 verification CSV to write.")
    parser.add_argument("--fail-on-threshold", action="store_true", help="Return exit code 1 if any test R2 misses the threshold.")
    parser.add_argument("--check-dependencies", action="store_true", help="Only report optional training dependency availability.")
    parser.add_argument("--dependency-report", type=Path, help="Optional JSON path for --check-dependencies output.")
    parser.add_argument(
        "--v2",
        action="store_true",
        help="Use the strict conditional v2 schema, geometry-group split, primitive targets, and derived metrics.",
    )
    parser.add_argument(
        "--expected-fingerprint",
        action="append",
        default=[],
        metavar="COLUMN=VALUE",
        help="Require a v2 dataset fingerprint value; repeat for multiple fingerprint columns.",
    )
    parser.add_argument(
        "--v2-audit-case-plan",
        type=Path,
        help=(
            "Restrict decisive v2 test metrics to every preassigned test row in this "
            "untouched audit case plan."
        ),
    )
    parser.add_argument("--max-invalid-training-rows", type=int, help="Fail if status/nonfinite filtering removes more rows than this.")
    parser.add_argument(
        "--max-removed-output-outlier-rows",
        type=int,
        help="Fail if output outlier filtering removes more rows than this.",
    )
    parser.add_argument("--keep-duplicate-case-id", dest="drop_duplicate_case_id", action="store_false")
    parser.set_defaults(drop_duplicate_case_id=True)

    tuning_group = parser.add_mutually_exclusive_group()
    tuning_group.add_argument("--enable-tuning", dest="enable_tuning", action="store_true", default=True)
    tuning_group.add_argument("--disable-tuning", dest="enable_tuning", action="store_false")

    outlier_group = parser.add_mutually_exclusive_group()
    outlier_group.add_argument("--remove-output-outliers", dest="remove_output_outliers", action="store_true", default=True)
    outlier_group.add_argument("--keep-output-outliers", dest="remove_output_outliers", action="store_false")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_training_options(args)
        if args.check_dependencies:
            report = inspect_training_dependencies()
            if args.dependency_report:
                write_training_dependency_report(args.dependency_report, report)
                print(f"wrote_training_dependency_report path={args.dependency_report}")
            print_training_dependency_report(report)
            return 0 if not missing_training_dependency_modules(report) else 2
        deps = require_training_dependencies()
        return run_training(args, deps)
    except MissingTrainingDependencyError as exc:
        print(f"dependency_error {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"training_error {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
