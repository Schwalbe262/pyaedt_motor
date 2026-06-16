"""Train deterministic LightGBM surrogate models for IPMSM simulation results."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import importlib
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


def finite_float(value: object) -> float:
    try:
        number = float(value)
    except Exception:
        return math.nan
    return number if math.isfinite(number) else math.nan


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


def build_model(lgb: Any, params: dict[str, Any], seed: int, n_jobs: int) -> Any:
    merged = dict(BASE_PARAMS)
    merged.update(params)
    merged["random_state"] = seed
    merged["n_jobs"] = n_jobs
    return lgb.LGBMRegressor(**merged)


def predict_model(model: Any, x: Any) -> Any:
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
    before_dedup = len(raw_df)
    dropped_duplicate_case_id_rows = 0
    if drop_duplicate_case_id and "case_id" in raw_df.columns:
        raw_df = raw_df.drop_duplicates(subset="case_id", keep="last").reset_index(drop=True)
        dropped_duplicate_case_id_rows = before_dedup - len(raw_df)
        print(f"dropped_duplicate_case_id_rows={dropped_duplicate_case_id_rows}")
    print(f"united_raw_rows={len(raw_df)} columns={len(raw_df.columns)}")

    output_columns, output_name_map = resolve_output_columns(raw_df.columns)
    missing_inputs = missing_columns(raw_df.columns, RAW_INPUT_COLUMNS)
    if missing_inputs:
        raise ValueError(f"missing input columns: {missing_inputs}")

    df = raw_df.copy()
    numeric_columns = tuple(dict.fromkeys((*RAW_INPUT_COLUMNS, *DERIVED_INPUT_REPAIR_COLUMNS, *OPTIONAL_INPUT_COLUMNS, *output_columns)))
    for column in numeric_columns:
        if column in df.columns:
            df[column] = deps.pd.to_numeric(df[column], errors="coerce")
    repaired_derived_inputs = repair_derived_input_columns(df)
    input_columns = select_training_input_columns(df.columns, df)
    print(
        "repaired_derived_input_rows "
        + " ".join(f"{column}={count}" for column, count in repaired_derived_inputs.items())
    )
    print("input_columns=" + ",".join(input_columns))

    status_ok = df["status"].astype(str).str.lower().eq("ok") if "status" in df.columns else deps.pd.Series(True, index=df.index)
    finite_inputs = deps.np.isfinite(df[list(input_columns)]).all(axis=1)
    finite_outputs = deps.np.isfinite(df[list(output_columns)]).all(axis=1)
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
    if remove_output_outliers:
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
    )


def split_training_data(deps: TrainingDependencies, prepared: PreparedData, test_size: float, val_size: float, seed: int) -> SplitData:
    x = prepared.valid_df[list(prepared.input_columns)].copy()
    y = prepared.valid_df[list(prepared.output_columns)].copy()
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
        ("val", split.x_val, split.y_val[target_col]),
        ("test", split.x_test, split.y_test[target_col]),
    ):
        pred = predict_model(model, split_x)
        metrics = regression_metrics(split_y, pred)
        metrics.update({"target": target_col, "split": split_name, "best_iteration": getattr(model, "best_iteration_", None)})
        metric_rows.append(metrics)
    return model, best_params, metric_rows, tuning_records


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
    if args.max_invalid_training_rows is not None and args.max_invalid_training_rows < 0:
        raise ValueError("--max-invalid-training-rows must be zero or greater")
    if args.max_removed_output_outlier_rows is not None and args.max_removed_output_outlier_rows < 0:
        raise ValueError("--max-removed-output-outlier-rows must be zero or greater")


def run_training(args: argparse.Namespace, deps: TrainingDependencies) -> int:
    set_seed(args.seed, deps.np)
    args.model_dir.mkdir(parents=True, exist_ok=True)
    prepared = prepare_training_data(
        deps,
        args.data,
        drop_duplicate_case_id=args.drop_duplicate_case_id,
        remove_output_outliers=args.remove_output_outliers,
        outlier_iqr_weight=args.outlier_iqr_weight,
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
    models: dict[str, Any] = {}
    model_paths: dict[str, str] = {}
    best_params_by_target: dict[str, dict[str, Any]] = {}
    metric_rows: list[dict[str, Any]] = []
    tuning_records: list[dict[str, Any]] = []

    for target_col in prepared.output_columns:
        print(f"training target={target_col} tuning={args.enable_tuning}")
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

    for target_col, model in models.items():
        model_path = args.model_dir / f"{safe_model_name(target_col)}_lgbm.pkl"
        with model_path.open("wb") as file:
            pickle.dump(model, file)
        model_paths[target_col] = str(model_path)

    metrics_path = args.model_dir / "metrics.csv"
    tuning_path = args.model_dir / "tuning_trials.csv"
    metadata_path = args.model_dir / "metadata.json"
    write_metrics_csv(metrics_path, metric_rows)
    write_tuning_csv(tuning_path, tuning_records)

    metadata = {
        "data_paths": [str(path) for path in args.data],
        "drop_duplicate_case_id": bool(args.drop_duplicate_case_id),
        "source_files": sorted(prepared.raw_df["source_file"].dropna().unique().tolist()) if "source_file" in prepared.raw_df.columns else [],
        "input_columns": list(prepared.input_columns),
        "requested_output_columns": list(REQUESTED_OUTPUT_COLUMNS),
        "actual_output_columns": list(prepared.output_columns),
        "output_name_map": prepared.output_name_map,
        "raw_rows": int(len(prepared.raw_df)),
        "valid_rows": int(len(prepared.valid_df)),
        "removed_output_outliers": int(prepared.removed_output_outliers),
        "repaired_derived_inputs": prepared.repaired_derived_inputs,
        "training_quality": prepared.quality_report.as_metadata(),
        "enable_tuning": bool(args.enable_tuning),
        "n_tuning_trials": int(args.n_tuning_trials),
        "seed": int(args.seed),
        "stable_target_seed": True,
        "best_params_by_target": best_params_by_target,
        "model_paths": model_paths,
        "metrics_path": str(metrics_path),
        "tuning_trials_path": str(tuning_path) if tuning_records else "",
    }
    with metadata_path.open("w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)

    if args.verification_output:
        verification_failures = write_verification_csv(args.verification_output, metric_rows, args.r2_threshold)
        print(f"wrote_regression_verification path={args.verification_output} failures={verification_failures}")

    threshold_summary, failures = summarize_test_threshold(metric_rows, args.r2_threshold)
    print(f"saved_model_dir={args.model_dir}")
    print(f"saved_metrics={metrics_path}")
    print(f"saved_metadata={metadata_path}")
    print(threshold_summary)
    return 1 if args.fail_on_threshold and failures else 0


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
    parser.add_argument("--verification-output", type=Path, help="Optional compact R2 verification CSV to write.")
    parser.add_argument("--fail-on-threshold", action="store_true", help="Return exit code 1 if any test R2 misses the threshold.")
    parser.add_argument("--check-dependencies", action="store_true", help="Only report optional training dependency availability.")
    parser.add_argument("--dependency-report", type=Path, help="Optional JSON path for --check-dependencies output.")
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
