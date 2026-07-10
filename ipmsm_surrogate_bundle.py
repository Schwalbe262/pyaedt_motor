"""Production adapter for strict IPMSM v2 LightGBM model bundles.

The adapter converts the trainer's ordered feature/model metadata into the
small prediction contract consumed by :mod:`ipmsm_optimization`.  Bundle
loading is fail-closed: legacy schemas, incomplete calibration, incomplete
training bounds, or missing model artifacts are rejected before optimization.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import pickle
from typing import Any, Mapping, Sequence


V2_TRAINING_SCHEMA = "ipmsm_v2"
METADATA_FILENAME = "metadata.json"
FEATURE_BOUNDS_SOURCE = "train"
MIN_OPTIMIZER_R2 = 0.95

TORQUE_TARGET = "output_torque_last_avg_nm"
CORE_LOSS_TARGET = "output_coreloss_last_avg_w"
SOLID_LOSS_TARGET = "output_solidloss_last_avg_w"
VOLTAGE_TARGET = "output_phase_voltage_last_peak_abs_v"
REQUIRED_OPTIMIZER_TARGETS = (
    TORQUE_TARGET,
    CORE_LOSS_TARGET,
    SOLID_LOSS_TARGET,
    VOLTAGE_TARGET,
)
PRIMARY_R2_TARGETS = (
    "output_torque_last_avg_nm",
    "output_torque_last_max_nm",
    "output_solidloss_last_avg_w",
    "output_coreloss_last_avg_w",
    "output_ld_last_avg_h",
    "output_lq_last_avg_h",
    "output_total_loss_last_avg_w",
    "output_efficiency_last_pct",
)


class SurrogateBundleError(ValueError):
    """Raised when a model bundle cannot be trusted for optimization."""


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SurrogateBundleError(f"{path} must be an object")
    return value


def _required(mapping: Mapping[str, Any], key: str, path: str = "metadata") -> Any:
    if key not in mapping:
        raise SurrogateBundleError(f"missing required field: {path}.{key}")
    return mapping[key]


def _finite(value: Any, path: str) -> float:
    if isinstance(value, bool):
        raise SurrogateBundleError(f"{path} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SurrogateBundleError(f"{path} must be a finite number") from exc
    if not math.isfinite(number):
        raise SurrogateBundleError(f"{path} must be a finite number")
    return number


def _positive_int(value: Any, path: str) -> int:
    number = _finite(value, path)
    if number <= 0.0 or not number.is_integer():
        raise SurrogateBundleError(f"{path} must be a positive integer")
    return int(number)


def _string_list(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SurrogateBundleError(f"{path} must be an array of strings")
    result = tuple(value)
    if not result or any(not isinstance(item, str) or not item for item in result):
        raise SurrogateBundleError(f"{path} must be a nonempty array of nonempty strings")
    if len(set(result)) != len(result):
        raise SurrogateBundleError(f"{path} must not contain duplicates")
    return result


@dataclass(frozen=True)
class FeatureBound:
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum) or not math.isfinite(self.maximum):
            raise ValueError("feature bounds must be finite")
        if self.minimum > self.maximum:
            raise ValueError("feature bound min must be <= max")

    def normalized_margin(self, value: float) -> float:
        width = self.maximum - self.minimum
        if width > 0.0:
            return min(value - self.minimum, self.maximum - value) / width
        scale = max(abs(self.minimum), 1.0)
        return -abs(value - self.minimum) / scale

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


@dataclass(frozen=True)
class ConformalAbsoluteResidual:
    coverage: float
    calibration_rows: int
    rank: int
    quantile_abs: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.coverage) or not 0.0 < self.coverage < 1.0:
            raise ValueError("conformal coverage must be > 0 and < 1")
        if self.calibration_rows < 1:
            raise ValueError("conformal calibration_rows must be >= 1")
        if self.rank < 1 or self.rank > self.calibration_rows:
            raise ValueError("conformal rank must be between 1 and calibration_rows")
        if not math.isfinite(self.quantile_abs) or self.quantile_abs < 0.0:
            raise ValueError("conformal quantile_abs must be >= 0")


@dataclass(frozen=True)
class LoadedTargetModel:
    canonical_target: str
    model_target: str
    estimators: tuple[Any, ...]
    conformal: ConformalAbsoluteResidual

    def __post_init__(self) -> None:
        if not self.estimators:
            raise ValueError(f"target {self.model_target} has no estimators")


def _parse_feature_bounds(
    raw: Any,
    input_columns: Sequence[str],
) -> dict[str, FeatureBound]:
    mapping = _mapping(raw, "metadata.feature_bounds")
    missing = [column for column in input_columns if column not in mapping]
    if missing:
        raise SurrogateBundleError(f"metadata.feature_bounds is missing input columns: {missing}")
    result: dict[str, FeatureBound] = {}
    for column in input_columns:
        item = _mapping(mapping[column], f"metadata.feature_bounds.{column}")
        minimum = _finite(
            _required(item, "min", f"metadata.feature_bounds.{column}"),
            f"metadata.feature_bounds.{column}.min",
        )
        maximum = _finite(
            _required(item, "max", f"metadata.feature_bounds.{column}"),
            f"metadata.feature_bounds.{column}.max",
        )
        try:
            result[column] = FeatureBound(minimum, maximum)
        except ValueError as exc:
            raise SurrogateBundleError(f"metadata.feature_bounds.{column}: {exc}") from exc
    return result


def _parse_conformal(raw: Any, target: str) -> ConformalAbsoluteResidual:
    all_targets = _mapping(raw, "metadata.conformal_absolute_residuals")
    if target not in all_targets:
        raise SurrogateBundleError(
            f"metadata.conformal_absolute_residuals is missing modeled target: {target}"
        )
    item = _mapping(
        all_targets[target],
        f"metadata.conformal_absolute_residuals.{target}",
    )
    try:
        return ConformalAbsoluteResidual(
            coverage=_finite(
                _required(item, "coverage", f"metadata.conformal_absolute_residuals.{target}"),
                f"metadata.conformal_absolute_residuals.{target}.coverage",
            ),
            calibration_rows=_positive_int(
                _required(item, "calibration_rows", f"metadata.conformal_absolute_residuals.{target}"),
                f"metadata.conformal_absolute_residuals.{target}.calibration_rows",
            ),
            rank=_positive_int(
                _required(item, "rank", f"metadata.conformal_absolute_residuals.{target}"),
                f"metadata.conformal_absolute_residuals.{target}.rank",
            ),
            quantile_abs=_finite(
                _required(item, "quantile_abs", f"metadata.conformal_absolute_residuals.{target}"),
                f"metadata.conformal_absolute_residuals.{target}.quantile_abs",
            ),
        )
    except ValueError as exc:
        raise SurrogateBundleError(
            f"metadata.conformal_absolute_residuals.{target}: {exc}"
        ) from exc


def _artifact_names(raw: Any, target: str) -> tuple[str, ...]:
    if isinstance(raw, str) and raw:
        return (raw,)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = tuple(raw)
        if values and all(isinstance(item, str) and item for item in values):
            return values
    raise SurrogateBundleError(
        f"metadata.model_paths.{target} must be a model path or a nonempty array of model paths"
    )


def _load_estimators(model_dir: Path, raw_paths: Any, target: str) -> tuple[Any, ...]:
    estimators: list[Any] = []
    for recorded_path in _artifact_names(raw_paths, target):
        # Trainer metadata may contain an absolute or invocation-relative path.
        # Loading by basename from the selected bundle directory makes a copied
        # bundle self-contained and prevents metadata from escaping that root.
        artifact = model_dir / Path(recorded_path).name
        if not artifact.is_file():
            raise SurrogateBundleError(f"model artifact is missing for {target}: {artifact}")
        try:
            with artifact.open("rb") as stream:
                loaded = pickle.load(stream)
        except Exception as exc:
            raise SurrogateBundleError(f"cannot load model artifact {artifact}: {exc}") from exc
        members = tuple(loaded) if isinstance(loaded, (list, tuple)) else (loaded,)
        for index, estimator in enumerate(members):
            if not callable(getattr(estimator, "predict", None)) and not callable(
                getattr(estimator, "predict_many", None)
            ):
                raise SurrogateBundleError(
                    f"model artifact {artifact} member {index} has no batch predict method"
                )
            estimators.append(estimator)
    return tuple(estimators)


def _feature_value(features: Mapping[str, Any], column: str) -> float | None:
    candidates = (column, column.removeprefix("input_")) if column.startswith("input_") else (column,)
    for candidate in candidates:
        if candidate in features and features[candidate] is not None:
            return _finite(features[candidate], f"prediction feature {column}")
    return None


def _resolve_ordered_features(
    features: Mapping[str, Any],
    input_columns: Sequence[str],
) -> tuple[float, ...]:
    resolved: dict[str, float] = {}

    def optional(column: str, *aliases: str) -> float | None:
        if column in resolved:
            return resolved[column]
        for name in (column, *aliases):
            value = _feature_value(features, name)
            if value is not None:
                resolved[column] = value
                return value
        return None

    # Canonical v2 beta is deliberately explicit instead of depending on a
    # legacy beta column that may use another sign/zero convention.
    optional("input_beta_dq_deg", "beta_deg", "input_beta_deg")
    optional("input_base_rpm", "speed_rpm", "base_rpm")
    optional("input_i_peak_a", "current_peak_a", "i_peak_a")
    optional("input_phase_resistance_ohm", "phase_resistance_ohm")

    for column in input_columns:
        optional(column)

    def require(name: str) -> float:
        value = resolved.get(name)
        if value is None:
            value = optional(name)
        if value is None:
            raise SurrogateBundleError(f"prediction features cannot derive required input: {name}")
        return value

    # Derive the legacy geometry columns retained by the v2 trainer from the
    # 15 independent optimizer variables.  These equations match variable.py.
    outer = optional("input_stator_outer_radius")
    yoke_ratio = optional("input_stator_back_yoke_thick_ratio")
    inner_ratio = optional("input_stator_inner_ratio")
    tooth_length_ratio = optional("input_stator_teeth_length_ratio")
    tooth_width_ratio = optional("input_stator_teeth_width_ratio")
    slot_num = optional("input_slot_num", "slot_num")
    rotator_gap = optional("input_rotator_gap")
    shaft_ratio = optional("input_shaft_ratio")
    if outer is not None and yoke_ratio is not None:
        resolved.setdefault("input_stator_back_yoke_thick", outer * yoke_ratio)
    if outer is not None and inner_ratio is not None:
        resolved.setdefault("input_stator_inner_radius", outer * inner_ratio)
    yoke = resolved.get("input_stator_back_yoke_thick")
    inner = resolved.get("input_stator_inner_radius")
    if outer is not None and yoke is not None and inner is not None and tooth_length_ratio is not None:
        resolved.setdefault("input_stator_teeth_length", (outer - yoke - inner) * tooth_length_ratio)
    tooth_length = resolved.get("input_stator_teeth_length")
    if (
        outer is not None
        and yoke is not None
        and tooth_length is not None
        and tooth_width_ratio is not None
        and slot_num is not None
        and slot_num > 0.0
    ):
        resolved.setdefault(
            "input_stator_teeth_width",
            (outer - yoke - tooth_length)
            * math.tan(math.radians(360.0 / slot_num) / 2.0)
            * tooth_width_ratio
            * 2.0,
        )
    if inner is not None and rotator_gap is not None:
        resolved.setdefault("input_rotor_radius", inner - rotator_gap)
    rotor = resolved.get("input_rotor_radius")
    if rotor is not None and shaft_ratio is not None:
        resolved.setdefault("input_shaft_radius", rotor * shaft_ratio)

    return tuple(require(column) for column in input_columns)


def _prediction_batch(
    estimator: Any,
    ordered_matrix: Sequence[Sequence[float]],
    target: str,
) -> list[float]:
    try:
        if callable(getattr(estimator, "predict_many", None)):
            raw = estimator.predict_many(ordered_matrix)
        else:
            raw = estimator.predict([list(row) for row in ordered_matrix])
    except Exception as exc:
        raise SurrogateBundleError(f"model prediction failed for {target}: {exc}") from exc
    if isinstance(raw, (str, bytes, Mapping)):
        raise SurrogateBundleError(f"model prediction for {target} must be a numeric array")
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    if not isinstance(raw, Sequence):
        raise SurrogateBundleError(f"model prediction for {target} must be a numeric array")
    if len(raw) != len(ordered_matrix):
        raise SurrogateBundleError(
            f"model prediction for {target} returned {len(raw)} rows; expected {len(ordered_matrix)}"
        )
    values: list[float] = []
    for index, value in enumerate(raw):
        if hasattr(value, "tolist"):
            value = value.tolist()
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != 1:
                raise SurrogateBundleError(
                    f"model prediction for {target} row {index} must be scalar"
                )
            value = value[0]
        values.append(_finite(value, f"model prediction for {target} row {index}"))
    return values


@dataclass(frozen=True)
class IPMSMV2SurrogateBundle:
    model_dir: Path
    input_columns: tuple[str, ...]
    feature_bounds: Mapping[str, FeatureBound]
    targets: Mapping[str, LoadedTargetModel]
    metadata: Mapping[str, Any]

    def predict_one(self, features: Mapping[str, Any]) -> dict[str, Any]:
        """Return point/conformal predictions and strict feature-bound OOD."""

        return self.predict_many([features])[0]

    def predict_many(self, features_list: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Predict a feature batch with one call per underlying estimator."""

        if not isinstance(features_list, Sequence) or isinstance(features_list, (str, bytes, Mapping)):
            raise SurrogateBundleError("prediction feature batch must be an array of mappings")
        if not features_list:
            return []
        if any(not isinstance(features, Mapping) for features in features_list):
            raise SurrogateBundleError("every prediction feature row must be a mapping")
        ordered_matrix = [
            _resolve_ordered_features(features, self.input_columns)
            for features in features_list
        ]
        ood_by_row = [
            tuple(
                column
                for column, value in zip(self.input_columns, ordered)
                if not self.feature_bounds[column].contains(value)
            )
            for ordered in ordered_matrix
        ]
        margin_by_row = [
            min(
                self.feature_bounds[column].normalized_margin(value)
                for column, value in zip(self.input_columns, ordered)
            )
            for ordered in ordered_matrix
        ]
        point_predictions: dict[str, list[float]] = {}
        quantiles: dict[str, float] = {}
        for canonical, target_model in self.targets.items():
            member_predictions = [
                _prediction_batch(estimator, ordered_matrix, target_model.model_target)
                for estimator in target_model.estimators
            ]
            point_predictions[canonical] = [
                sum(member[index] for member in member_predictions) / len(member_predictions)
                for index in range(len(ordered_matrix))
            ]
            quantiles[canonical] = target_model.conformal.quantile_abs
        predictions: list[dict[str, Any]] = []
        for index in range(len(ordered_matrix)):
            torque = point_predictions[TORQUE_TARGET][index]
            raw_nonnegative = {
                CORE_LOSS_TARGET: point_predictions[CORE_LOSS_TARGET][index],
                SOLID_LOSS_TARGET: point_predictions[SOLID_LOSS_TARGET][index],
                VOLTAGE_TARGET: point_predictions[VOLTAGE_TARGET][index],
            }
            clipped_targets = tuple(
                target for target, value in raw_nonnegative.items() if value < 0.0
            )
            core = max(0.0, raw_nonnegative[CORE_LOSS_TARGET])
            solid = max(0.0, raw_nonnegative[SOLID_LOSS_TARGET])
            voltage = max(0.0, raw_nonnegative[VOLTAGE_TARGET])
            physical_points = {
                TORQUE_TARGET: torque,
                CORE_LOSS_TARGET: core,
                SOLID_LOSS_TARGET: solid,
                VOLTAGE_TARGET: voltage,
            }
            uncertainty_score = max(
                quantiles[target] / max(abs(physical_points[target]), 1e-12)
                for target in REQUIRED_OPTIMIZER_TARGETS
            )
            predictions.append(
                {
                    "torque_nm": torque,
                    "torque_lcb_nm": torque - quantiles[TORQUE_TARGET],
                    "core_loss_w": core,
                    "core_loss_ucb_w": core + quantiles[CORE_LOSS_TARGET],
                    "solid_loss_w": solid,
                    "solid_loss_ucb_w": solid + quantiles[SOLID_LOSS_TARGET],
                    "voltage_peak_v": voltage,
                    "voltage_peak_ucb_v": voltage + quantiles[VOLTAGE_TARGET],
                    "in_domain": not ood_by_row[index],
                    "geometry_margin": margin_by_row[index],
                    "uncertainty_score": uncertainty_score,
                    "ood_features": ood_by_row[index],
                    "clipped_nonphysical_targets": clipped_targets,
                }
            )
        return predictions

    def summary(self) -> dict[str, Any]:
        return {
            "model_dir": str(self.model_dir),
            "training_schema": V2_TRAINING_SCHEMA,
            "input_columns": list(self.input_columns),
            "targets": {
                canonical: {
                    "model_target": target.model_target,
                    "ensemble_members": len(target.estimators),
                    "coverage": target.conformal.coverage,
                    "quantile_abs": target.conformal.quantile_abs,
                }
                for canonical, target in self.targets.items()
            },
            "feature_bounds_source": FEATURE_BOUNDS_SOURCE,
        }


def load_surrogate_bundle(model_dir: str | Path) -> IPMSMV2SurrogateBundle:
    """Load a strict, self-contained trainer v2 model directory."""

    root = Path(model_dir)
    metadata_path = root / METADATA_FILENAME
    try:
        decoded = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise SurrogateBundleError(f"cannot read surrogate metadata {metadata_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SurrogateBundleError(
            f"invalid surrogate metadata JSON {metadata_path}: line {exc.lineno}, column {exc.colno}"
        ) from exc
    metadata = _mapping(decoded, "metadata")
    training_schema = _required(metadata, "training_schema")
    if training_schema != V2_TRAINING_SCHEMA:
        raise SurrogateBundleError(
            f"metadata.training_schema must be {V2_TRAINING_SCHEMA!r}; got {training_schema!r}"
        )
    if _required(metadata, "feature_bounds_source") != FEATURE_BOUNDS_SOURCE:
        raise SurrogateBundleError(
            f"metadata.feature_bounds_source must be {FEATURE_BOUNDS_SOURCE!r}"
        )
    r2_threshold = _finite(_required(metadata, "r2_threshold"), "metadata.r2_threshold")
    if r2_threshold < MIN_OPTIMIZER_R2:
        raise SurrogateBundleError(
            f"metadata.r2_threshold must be >= {MIN_OPTIMIZER_R2}; got {r2_threshold}"
        )
    if _required(metadata, "primary_test_r2_gate_complete") is not True:
        raise SurrogateBundleError("metadata.primary_test_r2_gate_complete must be true")
    if _required(metadata, "primary_test_r2_gate_passed") is not True:
        raise SurrogateBundleError("metadata.primary_test_r2_gate_passed must be true")
    primary_test_r2 = _mapping(_required(metadata, "primary_test_r2"), "metadata.primary_test_r2")
    for target in PRIMARY_R2_TARGETS:
        value = _finite(
            _required(primary_test_r2, target, "metadata.primary_test_r2"),
            f"metadata.primary_test_r2.{target}",
        )
        if value < MIN_OPTIMIZER_R2:
            raise SurrogateBundleError(
                f"metadata.primary_test_r2.{target} must be >= {MIN_OPTIMIZER_R2}; got {value}"
            )

    input_columns = _string_list(_required(metadata, "input_columns"), "metadata.input_columns")
    modeled_outputs = _string_list(
        _required(metadata, "modeled_output_columns"), "metadata.modeled_output_columns"
    )
    auxiliary_outputs = _string_list(
        _required(metadata, "auxiliary_output_columns"), "metadata.auxiliary_output_columns"
    )
    if VOLTAGE_TARGET not in auxiliary_outputs:
        raise SurrogateBundleError(
            f"metadata.auxiliary_output_columns must include {VOLTAGE_TARGET}"
        )
    output_name_map = _mapping(_required(metadata, "output_name_map"), "metadata.output_name_map")
    model_paths = _mapping(_required(metadata, "model_paths"), "metadata.model_paths")
    conformal_raw = _required(metadata, "conformal_absolute_residuals")
    feature_bounds = _parse_feature_bounds(_required(metadata, "feature_bounds"), input_columns)

    available_outputs = set(modeled_outputs) | set(auxiliary_outputs)
    targets: dict[str, LoadedTargetModel] = {}
    for canonical in REQUIRED_OPTIMIZER_TARGETS:
        if canonical not in output_name_map:
            raise SurrogateBundleError(f"metadata.output_name_map is missing required target: {canonical}")
        model_target = output_name_map[canonical]
        if not isinstance(model_target, str) or not model_target:
            raise SurrogateBundleError(f"metadata.output_name_map.{canonical} must be a nonempty string")
        if model_target not in available_outputs:
            raise SurrogateBundleError(
                f"mapped target {model_target} for {canonical} is absent from modeled/auxiliary outputs"
            )
        if model_target not in model_paths:
            raise SurrogateBundleError(f"metadata.model_paths is missing modeled target: {model_target}")
        calibration = _parse_conformal(conformal_raw, model_target)
        estimators = _load_estimators(root, model_paths[model_target], model_target)
        targets[canonical] = LoadedTargetModel(canonical, model_target, estimators, calibration)

    return IPMSMV2SurrogateBundle(
        model_dir=root,
        input_columns=input_columns,
        feature_bounds=feature_bounds,
        targets=targets,
        metadata=metadata,
    )
