"""Compare deterministic model families without opening an official IPMSM gate.

Candidate selection uses only the preassigned outer-train geometry groups and
their deterministic inner holdout.  The frozen selection is hashed before any
outer-test prediction.  Only one strict JSON diagnostic is published; no model
bundle or threshold-pass artifact is created.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.metadata
import itertools
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import uuid

from atomic_publish import cleanup_publish_receipt, publish_no_replace
import train_ipmsm_lightgbm as trainer


SCHEMA_VERSION = "ipmsm-v2-model-family-diagnostic-v2"
SELECTION_SCHEMA_VERSION = "ipmsm-v2-model-family-selection-lock-v1"
PRIMARY_DIRECT = (
    "output_coreloss_last_avg_w",
    "output_ld_last_avg_h",
    "output_lq_last_avg_h",
    "output_solidloss_last_avg_w",
    "output_torque_last_avg_nm",
    "output_torque_last_max_nm",
)
COUPLED_REQUESTED = (
    "output_torque_last_avg_nm",
    "output_coreloss_last_avg_w",
    "output_solidloss_last_avg_w",
)
INDEPENDENT_REQUESTED = (
    "output_torque_last_max_nm",
    "output_ld_last_avg_h",
    "output_lq_last_avg_h",
    "output_phase_voltage_last_peak_abs_v",
)
DERIVED_REQUESTED = (
    "output_total_loss_last_avg_w",
    "output_efficiency_last_pct",
)
COUPLED_ALLOWED_KINDS = frozenset({"lightgbm", "extra_trees", "random_forest"})
COMPACT_DROP_COLUMNS = frozenset(
    {
        "input_slot_num",
        "input_pole_num",
        "input_stator_back_yoke_thick",
        "input_stator_inner_radius",
        "input_stator_teeth_length",
        "input_stator_teeth_width",
        "input_rotor_radius",
        "input_shaft_radius",
    }
)


class DiagnosticError(RuntimeError):
    """The diagnostic cannot preserve its selection/evaluation contract."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DiagnosticError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise DiagnosticError(f"nonfinite JSON constant: {value}")


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiagnosticError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise DiagnosticError(f"JSON root must be an object: {path}")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ordered_text_sha256(values: Iterable[object]) -> str:
    payload = "".join(f"{str(value).strip()}\n" for value in values).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    kind: str
    feature_mode: str
    params: tuple[tuple[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "feature_mode": self.feature_mode,
            "params": dict(self.params),
        }


def candidate_specs(tree_estimators: int) -> tuple[CandidateSpec, ...]:
    if isinstance(tree_estimators, bool) or not isinstance(tree_estimators, int) or tree_estimators < 20:
        raise ValueError("tree_estimators must be an integer >= 20")
    return (
        CandidateSpec("lightgbm", "lightgbm", "full", ()),
        CandidateSpec("ridge1_full", "ridge", "full", (("alpha", 1.0),)),
        CandidateSpec("ridge10_compact", "ridge", "compact", (("alpha", 10.0),)),
        CandidateSpec("ridge100_compact", "ridge", "compact", (("alpha", 100.0),)),
        CandidateSpec(
            "extra1_compact",
            "extra_trees",
            "compact",
            (("n_estimators", tree_estimators), ("min_samples_leaf", 1)),
        ),
        CandidateSpec(
            "extra2_compact",
            "extra_trees",
            "compact",
            (("n_estimators", tree_estimators), ("min_samples_leaf", 2)),
        ),
        CandidateSpec(
            "extra1_full",
            "extra_trees",
            "full",
            (("n_estimators", tree_estimators), ("min_samples_leaf", 1)),
        ),
        CandidateSpec(
            "rf1_compact",
            "random_forest",
            "compact",
            (("n_estimators", tree_estimators), ("min_samples_leaf", 1)),
        ),
        CandidateSpec(
            "rf2_compact",
            "random_forest",
            "compact",
            (("n_estimators", tree_estimators), ("min_samples_leaf", 2)),
        ),
        CandidateSpec(
            "hist_compact",
            "hist_gradient_boosting",
            "compact",
            (("max_iter", 300), ("learning_rate", 0.05), ("max_leaf_nodes", 15), ("l2_regularization", 1.0)),
        ),
    )


def feature_columns(input_columns: Sequence[str], mode: str) -> tuple[str, ...]:
    columns = tuple(input_columns)
    if mode == "full":
        return columns
    if mode != "compact":
        raise ValueError(f"unsupported feature mode: {mode}")
    compact = tuple(column for column in columns if column not in COMPACT_DROP_COLUMNS)
    required = {
        "input_stator_outer_radius",
        "input_stack_length_mm",
        "input_base_rpm",
        "input_i_peak_a",
        "input_beta_dq_deg",
        "input_phase_resistance_ohm",
    }
    if not required <= set(compact):
        raise DiagnosticError("compact feature set lost a required geometry or operating input")
    return compact


def _candidate_seed(target: str, candidate: str, base_seed: int) -> int:
    return trainer.stable_target_seed(f"{target}:diagnostic:{candidate}", base_seed)


def _build_estimator(spec: CandidateSpec, *, target: str, seed: int, n_jobs: int) -> Any:
    params = dict(spec.params)
    model_seed = _candidate_seed(target, spec.name, seed)
    if spec.kind == "ridge":
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(StandardScaler(), Ridge(alpha=float(params["alpha"])))
    if spec.kind == "extra_trees":
        from sklearn.ensemble import ExtraTreesRegressor

        return ExtraTreesRegressor(
            n_estimators=int(params["n_estimators"]),
            min_samples_leaf=int(params["min_samples_leaf"]),
            max_features=1.0,
            n_jobs=n_jobs,
            random_state=model_seed,
        )
    if spec.kind == "random_forest":
        from sklearn.ensemble import RandomForestRegressor

        return RandomForestRegressor(
            n_estimators=int(params["n_estimators"]),
            min_samples_leaf=int(params["min_samples_leaf"]),
            max_features=1.0,
            n_jobs=n_jobs,
            random_state=model_seed,
        )
    if spec.kind == "hist_gradient_boosting":
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(
            max_iter=int(params["max_iter"]),
            learning_rate=float(params["learning_rate"]),
            max_leaf_nodes=int(params["max_leaf_nodes"]),
            l2_regularization=float(params["l2_regularization"]),
            early_stopping=False,
            random_state=model_seed,
        )
    raise ValueError(f"unsupported estimator kind: {spec.kind}")


def strict_metric(y_true: Sequence[object], y_pred: Sequence[object]) -> dict[str, Any]:
    import numpy as np

    truth = np.asarray(y_true, dtype=float)
    predicted = np.asarray(y_pred, dtype=float)
    if truth.ndim != 1 or predicted.ndim != 1 or len(truth) != len(predicted) or not len(truth):
        raise DiagnosticError("metric arrays must be nonempty one-dimensional pairs")
    if not np.isfinite(truth).all():
        raise DiagnosticError("ground-truth metric array contains a nonfinite value")
    invalid = int((~np.isfinite(predicted)).sum())
    if invalid:
        return {
            "status": "invalid_prediction",
            "rows": len(truth),
            "invalid_prediction_rows": invalid,
            "MAE": None,
            "RMSE": None,
            "MAPE_pct": None,
            "R2": None,
            "NRMSE": None,
        }
    error = predicted - truth
    mae = float(np.mean(np.abs(error)))
    rmse = float(np.sqrt(np.mean(error**2)))
    centered = truth - float(np.mean(truth))
    denominator = float(np.sum(centered**2))
    r2 = None if denominator <= 0.0 else float(1.0 - float(np.sum(error**2)) / denominator)
    scale = max(float(np.std(truth)), 1.0e-12 * max(1.0, float(np.mean(np.abs(truth)))))
    nonzero = np.abs(truth) > 1.0e-12
    mape = float(np.mean(np.abs(error[nonzero] / truth[nonzero])) * 100.0) if nonzero.any() else None
    return {
        "status": "ok" if r2 is not None else "constant_truth",
        "rows": len(truth),
        "invalid_prediction_rows": 0,
        "MAE": mae,
        "RMSE": rmse,
        "MAPE_pct": mape,
        "R2": r2,
        "NRMSE": rmse / scale,
    }


def _positive_prediction_violations(values: Sequence[object]) -> int:
    import numpy as np

    array = np.asarray(values, dtype=float)
    return int((~np.isfinite(array) | (array <= 0.0)).sum())


def _nonnegative_prediction_violations(values: Sequence[object]) -> int:
    import numpy as np

    array = np.asarray(values, dtype=float)
    return int((~np.isfinite(array) | (array < 0.0)).sum())


def _derived_arrays(
    split_x: Any,
    torque: Sequence[object],
    core: Sequence[object],
    solid: Sequence[object],
) -> tuple[list[float], list[float], dict[str, int]]:
    total: list[float] = []
    efficiency: list[float] = []
    violations = {"torque_nonpositive": 0, "core_negative": 0, "solid_negative": 0, "derived_invalid": 0}
    for position in range(len(split_x)):
        torque_value = float(torque[position])
        core_value = float(core[position])
        solid_value = float(solid[position])
        violations["torque_nonpositive"] += int(not math.isfinite(torque_value) or torque_value <= 0.0)
        violations["core_negative"] += int(not math.isfinite(core_value) or core_value < 0.0)
        violations["solid_negative"] += int(not math.isfinite(solid_value) or solid_value < 0.0)
        values = trainer.derive_v2_outputs(
            torque_avg_nm=torque_value,
            core_loss_w=core_value,
            solid_loss_w=solid_value,
            i_peak_a=split_x.iloc[position]["input_i_peak_a"],
            phase_resistance_ohm=split_x.iloc[position]["input_phase_resistance_ohm"],
            rpm=split_x.iloc[position]["input_base_rpm"],
        )
        total_value = float(values["output_total_loss_last_avg_w"])
        efficiency_value = float(values["output_efficiency_last_pct"])
        invalid = (
            not math.isfinite(total_value)
            or total_value < 0.0
            or not math.isfinite(efficiency_value)
            or not 0.0 <= efficiency_value <= 100.0
        )
        violations["derived_invalid"] += int(invalid)
        total.append(total_value)
        efficiency.append(efficiency_value)
    return total, efficiency, violations


def select_coupled_triplet(
    *,
    split_x: Any,
    truth: Mapping[str, Sequence[object]],
    predictions: Mapping[str, Mapping[str, Sequence[object]]],
    candidate_names: Sequence[str],
) -> dict[str, Any]:
    true_total, true_efficiency, true_violations = _derived_arrays(
        split_x,
        truth[COUPLED_REQUESTED[0]],
        truth[COUPLED_REQUESTED[1]],
        truth[COUPLED_REQUESTED[2]],
    )
    if any(true_violations.values()):
        raise DiagnosticError("inner coupled ground truth violates physical derivation")
    best: tuple[tuple[float, float, tuple[str, str, str]], dict[str, Any]] | None = None
    evaluated = 0
    rejected = 0
    for names in itertools.product(candidate_names, repeat=3):
        evaluated += 1
        torque = predictions[COUPLED_REQUESTED[0]][names[0]]
        core = predictions[COUPLED_REQUESTED[1]][names[1]]
        solid = predictions[COUPLED_REQUESTED[2]][names[2]]
        pred_total, pred_efficiency, violations = _derived_arrays(split_x, torque, core, solid)
        if any(violations.values()):
            rejected += 1
            continue
        pairs = (
            (truth[COUPLED_REQUESTED[0]], torque),
            (truth[COUPLED_REQUESTED[1]], core),
            (truth[COUPLED_REQUESTED[2]], solid),
            (true_total, pred_total),
            (true_efficiency, pred_efficiency),
        )
        metrics = [strict_metric(actual, predicted) for actual, predicted in pairs]
        nrmse = [item["NRMSE"] for item in metrics]
        if any(value is None or not math.isfinite(float(value)) for value in nrmse):
            rejected += 1
            continue
        score = (max(float(value) for value in nrmse), sum(float(value) for value in nrmse) / len(nrmse), names)
        item = {
            "families": dict(zip(COUPLED_REQUESTED, names)),
            "worst_NRMSE": score[0],
            "mean_NRMSE": score[1],
            "physical_violations": violations,
        }
        if best is None or score < best[0]:
            best = (score, item)
    if best is None:
        raise DiagnosticError("no physically valid coupled family triplet exists")
    return {**best[1], "evaluated_triplets": evaluated, "rejected_triplets": rejected}


def select_independent_family(
    target: str,
    truth: Sequence[object],
    predictions: Mapping[str, Sequence[object]],
    candidate_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    allowed = set(candidate_names) if candidate_names is not None else set(predictions)
    if not allowed <= set(predictions):
        raise DiagnosticError(f"independent candidate coverage is incomplete for {target}")
    for name, values in predictions.items():
        if name not in allowed:
            continue
        metric = strict_metric(truth, values)
        physical_violations = _positive_prediction_violations(values)
        if metric["NRMSE"] is None or physical_violations:
            continue
        ranked.append((float(metric["NRMSE"]), name, metric))
    if not ranked:
        raise DiagnosticError(f"no physically valid independent family exists for {target}")
    nrmse, name, metric = min(ranked, key=lambda item: (item[0], item[1]))
    return {"family": name, "NRMSE": nrmse, "R2": metric["R2"], "physical_violations": 0}


def _fit_candidate_prediction(
    *,
    spec: CandidateSpec,
    target: str,
    prepared: Any,
    fit_indices: Any,
    predict_indices: Any,
    baseline_params: Mapping[str, Any],
    seed: int,
    n_jobs: int,
) -> Any:
    columns = feature_columns(prepared.input_columns, spec.feature_mode)
    if spec.kind == "lightgbm":
        model = trainer.build_model(trainer.require_training_dependencies().lgb, dict(baseline_params), seed=seed, n_jobs=n_jobs)
    else:
        model = _build_estimator(spec, target=target, seed=seed, n_jobs=n_jobs)
    model.fit(prepared.valid_df.loc[fit_indices, list(columns)], prepared.valid_df.loc[fit_indices, target])
    return model.predict(prepared.valid_df.loc[predict_indices, list(columns)])


def _ensemble_lgbm_prediction(
    *,
    deps: Any,
    split: Any,
    predict_x: Any,
    target: str,
    params: Mapping[str, Any],
    seed: int,
    n_jobs: int,
    ensemble_size: int,
) -> Any:
    models = [trainer.fit_lgbm_full_training(deps, split, target, dict(params), seed, n_jobs)]
    for member_index in range(1, ensemble_size):
        member_seed = trainer.stable_target_seed(f"{target}:ensemble:{member_index}", seed)
        models.append(trainer.fit_lgbm_full_training(deps, split, target, dict(params), member_seed, n_jobs))
    model: Any = models[0] if len(models) == 1 else tuple(models)
    return trainer.predict_model(model, predict_x)


def evaluate_predictions(
    prepared: Any,
    split: Any,
    predictions: Mapping[str, Sequence[object]],
) -> dict[str, Any]:
    actual = prepared.output_name_map
    rows: list[dict[str, Any]] = []
    truth_direct_violations: dict[str, int] = {}
    prediction_direct_violations: dict[str, int] = {}
    for requested in PRIMARY_DIRECT:
        target = actual[requested]
        violation_counter = (
            _nonnegative_prediction_violations
            if requested in {"output_coreloss_last_avg_w", "output_solidloss_last_avg_w"}
            else _positive_prediction_violations
        )
        truth_direct_violations[requested] = violation_counter(split.y_test[target])
        prediction_direct_violations[requested] = violation_counter(predictions[target])
        rows.append({"target": requested, **strict_metric(split.y_test[target], predictions[target])})
    torque_target, core_target, solid_target = (actual[name] for name in COUPLED_REQUESTED)
    true_total, true_efficiency, true_violations = _derived_arrays(
        split.x_test,
        split.y_test[torque_target].tolist(),
        split.y_test[core_target].tolist(),
        split.y_test[solid_target].tolist(),
    )
    if any(true_violations.values()):
        raise DiagnosticError("outer-test ground truth violates physical derivation")
    pred_total, pred_efficiency, pred_violations = _derived_arrays(
        split.x_test,
        predictions[torque_target],
        predictions[core_target],
        predictions[solid_target],
    )
    rows.append({"target": DERIVED_REQUESTED[0], **strict_metric(true_total, pred_total)})
    rows.append({"target": DERIVED_REQUESTED[1], **strict_metric(true_efficiency, pred_efficiency)})
    voltage_target = actual["output_phase_voltage_last_peak_abs_v"]
    truth_direct_violations["output_phase_voltage_last_peak_abs_v"] = _positive_prediction_violations(
        split.y_test[voltage_target]
    )
    prediction_direct_violations["output_phase_voltage_last_peak_abs_v"] = _positive_prediction_violations(
        predictions[voltage_target]
    )
    if any(truth_direct_violations.values()):
        raise DiagnosticError("outer-test ground truth has a nonphysical direct target")
    rows.append(
        {
            "target": "output_phase_voltage_last_peak_abs_v",
            "role": "auxiliary_voltage",
            **strict_metric(split.y_test[voltage_target], predictions[voltage_target]),
        }
    )
    primary_rows = [row for row in rows if row.get("role") != "auxiliary_voltage"]
    primary_r2 = [row["R2"] for row in primary_rows]
    complete = len(primary_rows) == 8 and all(value is not None for value in primary_r2)
    return {
        "rows": rows,
        "primary_metric_count": len(primary_rows),
        "primary_complete": complete,
        "primary_min_r2": min(float(value) for value in primary_r2) if complete else None,
        "primary_avg_r2": sum(float(value) for value in primary_r2) / len(primary_r2) if complete else None,
        "voltage_r2": rows[-1]["R2"],
        "physical_validity": {
            "truth": {"direct": truth_direct_violations, "derived": true_violations},
            "prediction": {"direct": prediction_direct_violations, "derived": pred_violations},
            "passed": not any(pred_violations.values()) and not any(prediction_direct_violations.values()),
        },
    }


def _metadata_fingerprints(metadata: Mapping[str, Any]) -> dict[str, str]:
    raw = metadata.get("fingerprints")
    if not isinstance(raw, Mapping):
        raise DiagnosticError("baseline metadata has no fingerprint mapping")
    entries = [f"{key}={value}" for key, value in raw.items()]
    try:
        parsed = trainer.parse_expected_fingerprints(entries)
    except ValueError as exc:
        raise DiagnosticError("baseline metadata fingerprints are invalid") from exc
    if set(parsed) != set(trainer.V2_FINGERPRINT_COLUMNS):
        raise DiagnosticError("baseline metadata fingerprint coverage is incomplete")
    return parsed


def _baseline_params(metadata: Mapping[str, Any], targets: Sequence[str]) -> dict[str, dict[str, Any]]:
    raw = metadata.get("best_params_by_target")
    if not isinstance(raw, Mapping):
        raise DiagnosticError("baseline metadata has no target parameter mapping")
    result: dict[str, dict[str, Any]] = {}
    for target in targets:
        value = raw.get(target)
        if not isinstance(value, Mapping):
            raise DiagnosticError(f"baseline metadata has no parameters for {target}")
        result[target] = dict(value)
    return result


def audit_baseline_r2_reproduction(
    metadata: Mapping[str, Any],
    baseline_evaluation: Mapping[str, Any],
    *,
    maximum_drift: float,
) -> float:
    if not math.isfinite(maximum_drift) or maximum_drift < 0.0:
        raise ValueError("maximum baseline R2 drift must be finite and >= 0")
    expected_primary = metadata.get("primary_test_r2")
    if not isinstance(expected_primary, Mapping) or len(expected_primary) != 8:
        raise DiagnosticError("baseline metadata primary R2 coverage is not exactly eight targets")
    rows = baseline_evaluation.get("rows")
    if not isinstance(rows, list):
        raise DiagnosticError("baseline evaluation metric rows are unavailable")
    observed = {
        str(row.get("target")): row.get("R2")
        for row in rows
        if isinstance(row, Mapping) and row.get("role") != "auxiliary_voltage"
    }
    if set(observed) != set(expected_primary):
        raise DiagnosticError("baseline metadata and reproduced primary target identities differ")
    drift: list[float] = []
    for target, expected in expected_primary.items():
        actual = observed.get(str(target))
        if (
            isinstance(expected, bool)
            or not isinstance(expected, (int, float))
            or not math.isfinite(float(expected))
            or isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isfinite(float(actual))
        ):
            raise DiagnosticError(f"baseline R2 is nonfinite for {target}")
        drift.append(abs(float(actual) - float(expected)))
    expected_voltage = metadata.get("voltage_test_r2")
    observed_voltage = baseline_evaluation.get("voltage_r2")
    if (
        isinstance(expected_voltage, bool)
        or not isinstance(expected_voltage, (int, float))
        or not math.isfinite(float(expected_voltage))
        or isinstance(observed_voltage, bool)
        or not isinstance(observed_voltage, (int, float))
        or not math.isfinite(float(observed_voltage))
    ):
        raise DiagnosticError("baseline voltage R2 is nonfinite")
    drift.append(abs(float(observed_voltage) - float(expected_voltage)))
    maximum = max(drift)
    if maximum > maximum_drift:
        raise DiagnosticError(
            f"baseline R2 reproduction drift {maximum:.12g} exceeds {maximum_drift:.12g}"
        )
    return maximum


def _split_summary(split: Any) -> dict[str, Any]:
    groups = {
        "train": tuple(split.train_group_ids),
        "calibration": tuple(split.val_group_ids),
        "test": tuple(split.test_group_ids),
    }
    disjoint = not (
        set(groups["train"]) & set(groups["calibration"])
        or set(groups["train"]) & set(groups["test"])
        or set(groups["calibration"]) & set(groups["test"])
    )
    if not disjoint:
        raise DiagnosticError("outer split geometry groups overlap")
    return {
        "rows": {"train": len(split.x_train), "calibration": len(split.x_val), "test": len(split.x_test)},
        "groups": {name: len(values) for name, values in groups.items()},
        "group_ids_sha256": {name: ordered_text_sha256(values) for name, values in groups.items()},
        "group_disjoint": True,
    }


def _publish_report(path: Path, report: Mapping[str, Any]) -> None:
    if path.exists():
        raise DiagnosticError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    staged = path.parent / f".{path.name}.{uuid.uuid4().hex}.staging"
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        receipt = publish_no_replace(staged, path)
        cleanup_publish_receipt(receipt)
    except BaseException:
        try:
            staged.unlink()
        except OSError:
            pass
        raise


def run_diagnostic(args: argparse.Namespace) -> dict[str, Any]:
    data_path = args.data.resolve()
    metadata_path = args.baseline_metadata.resolve()
    output_path = args.output.resolve()
    selection_manifest_path = (
        args.selection_manifest.resolve()
        if args.selection_manifest
        else output_path.with_name(f"{output_path.stem}.selection.json")
    )
    if selection_manifest_path == output_path:
        raise DiagnosticError("selection manifest and final report paths must differ")
    if output_path.exists():
        raise DiagnosticError(f"output already exists: {output_path}")
    if selection_manifest_path.exists():
        raise DiagnosticError(f"selection manifest already exists: {selection_manifest_path}")
    metadata = read_json_object(metadata_path)
    fingerprints = _metadata_fingerprints(metadata)
    seed = metadata.get("seed")
    ensemble_size = metadata.get("ensemble_size")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise DiagnosticError("baseline metadata seed is invalid")
    if isinstance(ensemble_size, bool) or not isinstance(ensemble_size, int) or ensemble_size < 1:
        raise DiagnosticError("baseline metadata ensemble_size is invalid")

    deps = trainer.require_training_dependencies()
    prepared = trainer.prepare_training_data(
        deps,
        [data_path],
        drop_duplicate_case_id=True,
        remove_output_outliers=False,
        outlier_iqr_weight=1.5,
        v2=True,
        expected_fingerprints=fingerprints,
    )
    outer = trainer.split_training_data(deps, prepared, test_size=0.2, val_size=0.2, seed=seed)
    evaluation, evaluation_contract = trainer.select_v2_test_evaluation_split(
        prepared,
        outer,
        args.audit_case_plan.resolve() if args.audit_case_plan else None,
    )
    inner = trainer.build_v2_model_selection_split(prepared, outer, seed=seed)
    if inner is None:
        raise DiagnosticError("inner model-selection split is unavailable")
    if set(inner.train_group_ids) | set(inner.val_group_ids) != set(outer.train_group_ids):
        raise DiagnosticError("inner split does not partition the outer-train groups")
    if set(inner.train_group_ids) & set(inner.val_group_ids):
        raise DiagnosticError("inner fit and holdout groups overlap")

    targets = tuple((*prepared.output_columns, *prepared.auxiliary_output_columns))
    expected_targets = {
        prepared.output_name_map[name]
        for name in (*COUPLED_REQUESTED, *INDEPENDENT_REQUESTED)
    }
    if set(targets) != expected_targets:
        raise DiagnosticError("v2 modeled target coverage changed")
    baseline_params = _baseline_params(metadata, targets)
    specs = candidate_specs(args.tree_estimators)
    spec_by_name = {spec.name: spec for spec in specs}
    inner_predictions: dict[str, dict[str, Any]] = {name: {} for name in (*COUPLED_REQUESTED, *INDEPENDENT_REQUESTED)}
    inner_records: list[dict[str, Any]] = []
    for requested in (*COUPLED_REQUESTED, *INDEPENDENT_REQUESTED):
        target = prepared.output_name_map[requested]
        truth = prepared.valid_df.loc[inner.x_val.index, target]
        for spec in specs:
            predicted = _fit_candidate_prediction(
                spec=spec,
                target=target,
                prepared=prepared,
                fit_indices=inner.x_train.index,
                predict_indices=inner.x_val.index,
                baseline_params=baseline_params[target],
                seed=seed,
                n_jobs=args.n_jobs,
            )
            inner_predictions[requested][spec.name] = predicted
            metric = strict_metric(truth, predicted)
            inner_records.append(
                {
                    "target": requested,
                    "family": spec.name,
                    **metric,
                    "positive_prediction_violations": _positive_prediction_violations(predicted),
                }
            )

    coupled_truth = {
        requested: prepared.valid_df.loc[inner.x_val.index, prepared.output_name_map[requested]].tolist()
        for requested in COUPLED_REQUESTED
    }
    coupled_candidate_names = tuple(
        spec.name for spec in specs if spec.kind in COUPLED_ALLOWED_KINDS
    )
    if "lightgbm" not in coupled_candidate_names:
        raise DiagnosticError("coupled family set lost the baseline LightGBM candidate")
    coupled = select_coupled_triplet(
        split_x=inner.x_val,
        truth=coupled_truth,
        predictions={requested: inner_predictions[requested] for requested in COUPLED_REQUESTED},
        candidate_names=coupled_candidate_names,
    )
    independent: dict[str, dict[str, Any]] = {}
    for requested in INDEPENDENT_REQUESTED:
        allowed = (
            coupled_candidate_names
            if requested == "output_torque_last_max_nm"
            else tuple(spec_by_name)
        )
        independent[requested] = select_independent_family(
            requested,
            prepared.valid_df.loc[inner.x_val.index, prepared.output_name_map[requested]].tolist(),
            inner_predictions[requested],
            allowed,
        )
    selected_by_requested = dict(coupled["families"])
    selected_by_requested.update({target: value["family"] for target, value in independent.items()})
    data_sha256 = file_sha256(data_path)
    metadata_sha256 = file_sha256(metadata_path)
    trainer_sha256 = file_sha256(Path(trainer.__file__).resolve())
    diagnostic_sha256 = file_sha256(Path(__file__).resolve())
    evaluation_plan_sha256 = file_sha256(args.audit_case_plan.resolve()) if args.audit_case_plan else ""
    package_versions = {
        name: importlib.metadata.version(name)
        for name in ("lightgbm", "numpy", "pandas", "scikit-learn")
    }
    selection_payload = {
        "evidence_scope": args.evidence_scope,
        "data_sha256": data_sha256,
        "baseline_metadata_sha256": metadata_sha256,
        "trainer_sha256": trainer_sha256,
        "diagnostic_script_sha256": diagnostic_sha256,
        "audit_case_plan_sha256": evaluation_plan_sha256,
        "outer_test_group_ids_sha256": ordered_text_sha256(evaluation.test_group_ids),
        "seed": seed,
        "ensemble_size": ensemble_size,
        "fingerprints": dict(prepared.fingerprints),
        "package_versions": package_versions,
        "inner_fit_group_ids_sha256": ordered_text_sha256(inner.train_group_ids),
        "inner_holdout_group_ids_sha256": ordered_text_sha256(inner.val_group_ids),
        "candidate_specs": [spec.as_dict() for spec in specs],
        "baseline_params_by_target": baseline_params,
        "selected_family_by_target": selected_by_requested,
        "coupled_score": {
            "worst_NRMSE": coupled["worst_NRMSE"],
            "mean_NRMSE": coupled["mean_NRMSE"],
        },
    }
    selection_sha256 = canonical_sha256(selection_payload)
    selection_manifest = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "diagnostic_only": True,
        "official_gate_eligible": False,
        "production_eligible": False,
        "outer_test_evaluated": False,
        "evidence_scope": args.evidence_scope,
        "test_reused_for_adaptive_exploration": args.evidence_scope == "adaptive_exploration",
        "selection_sha256": selection_sha256,
        "selection": selection_payload,
    }
    _publish_report(selection_manifest_path, selection_manifest)
    selection_manifest_sha256 = file_sha256(selection_manifest_path)

    prediction_cache: dict[tuple[str, str], Any] = {}

    def final_prediction(requested: str, family: str) -> Any:
        target = prepared.output_name_map[requested]
        cache_key = (target, family)
        if cache_key in prediction_cache:
            return prediction_cache[cache_key]
        spec = spec_by_name[family]
        if spec.kind == "lightgbm":
            predicted = _ensemble_lgbm_prediction(
                deps=deps,
                split=outer,
                predict_x=evaluation.x_test,
                target=target,
                params=baseline_params[target],
                seed=seed,
                n_jobs=args.n_jobs,
                ensemble_size=ensemble_size,
            )
        else:
            columns = feature_columns(prepared.input_columns, spec.feature_mode)
            model = _build_estimator(spec, target=target, seed=seed, n_jobs=args.n_jobs)
            model.fit(outer.x_train[list(columns)], outer.y_train[target])
            predicted = model.predict(evaluation.x_test[list(columns)])
        prediction_cache[cache_key] = predicted
        return predicted

    baseline_predictions = {
        prepared.output_name_map[requested]: final_prediction(requested, "lightgbm")
        for requested in (*COUPLED_REQUESTED, *INDEPENDENT_REQUESTED)
    }
    selected_predictions = {
        prepared.output_name_map[requested]: final_prediction(requested, selected_by_requested[requested])
        for requested in (*COUPLED_REQUESTED, *INDEPENDENT_REQUESTED)
    }
    baseline_evaluation = evaluate_predictions(prepared, evaluation, baseline_predictions)
    selected_evaluation = evaluate_predictions(prepared, evaluation, selected_predictions)

    baseline_drift = audit_baseline_r2_reproduction(
        metadata,
        baseline_evaluation,
        maximum_drift=args.max_baseline_r2_drift,
    )
    family_gain = bool(
        selected_evaluation["physical_validity"]["passed"]
        and selected_evaluation["primary_avg_r2"] is not None
        and baseline_evaluation["primary_avg_r2"] is not None
        and selected_evaluation["primary_min_r2"] is not None
        and baseline_evaluation["primary_min_r2"] is not None
        and selected_evaluation["voltage_r2"] is not None
        and baseline_evaluation["voltage_r2"] is not None
        and selected_evaluation["primary_avg_r2"] > baseline_evaluation["primary_avg_r2"]
        and selected_evaluation["primary_min_r2"] > baseline_evaluation["primary_min_r2"]
        and selected_evaluation["voltage_r2"] >= baseline_evaluation["voltage_r2"]
    )
    if not family_gain:
        recommended_action = "retain_current_family_pending_more_evidence"
    elif args.evidence_scope == "adaptive_exploration":
        recommended_action = "freeze_candidate_and_require_untouched_confirmation"
    else:
        recommended_action = "consider_family_selection_in_future_contract"
    summary = {
        "selected_primary_min_r2": selected_evaluation["primary_min_r2"],
        "selected_primary_avg_r2": selected_evaluation["primary_avg_r2"],
        "selected_voltage_r2": selected_evaluation["voltage_r2"],
        "baseline_primary_min_r2": baseline_evaluation["primary_min_r2"],
        "baseline_primary_avg_r2": baseline_evaluation["primary_avg_r2"],
        "baseline_voltage_r2": baseline_evaluation["voltage_r2"],
        "baseline_metadata_max_abs_r2_drift": baseline_drift,
        "recommended_action": recommended_action,
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "diagnostic_only": True,
        "official_gate_eligible": False,
        "production_eligible": False,
        "selection_locked_before_test": True,
        "evidence_scope": args.evidence_scope,
        "test_reused_for_adaptive_exploration": args.evidence_scope == "adaptive_exploration",
        "provenance": {
            "data_path": str(data_path),
            "data_sha256": data_sha256,
            "baseline_metadata_path": str(metadata_path),
            "baseline_metadata_sha256": metadata_sha256,
            "trainer_sha256": trainer_sha256,
            "diagnostic_script_sha256": diagnostic_sha256,
            "fingerprints": dict(prepared.fingerprints),
        },
        "split": {
            "outer": _split_summary(evaluation),
            "inner": {
                "fit_rows": len(inner.x_train),
                "holdout_rows": len(inner.x_val),
                "fit_groups": len(inner.train_group_ids),
                "holdout_groups": len(inner.val_group_ids),
                "fit_group_ids_sha256": selection_payload["inner_fit_group_ids_sha256"],
                "holdout_group_ids_sha256": selection_payload["inner_holdout_group_ids_sha256"],
                "partitions_outer_train": True,
            },
            "test_evaluation": evaluation_contract,
        },
        "candidate_specs": [spec.as_dict() for spec in specs],
        "inner_candidates": inner_records,
        "selection": {
            "sha256": selection_sha256,
            "manifest_path": str(selection_manifest_path),
            "manifest_sha256": selection_manifest_sha256,
            "selected_family_by_target": selected_by_requested,
            "coupled": coupled,
            "independent": independent,
        },
        "outer_test": {
            "evaluated_after_selection_sha256": selection_sha256,
            "baseline_control": baseline_evaluation,
            "selected_families": selected_evaluation,
        },
        "summary": summary,
    }
    _publish_report(output_path, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an isolated inner-selected IPMSM v2 model-family diagnostic.",
    )
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--baseline-metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selection-manifest", type=Path)
    parser.add_argument("--audit-case-plan", type=Path)
    parser.add_argument(
        "--evidence-scope",
        choices=("adaptive_exploration", "untouched_confirmation"),
        required=True,
    )
    parser.add_argument("--tree-estimators", type=int, default=240)
    parser.add_argument("--n-jobs", type=int, default=-1)
    parser.add_argument("--max-baseline-r2-drift", type=float, default=1.0e-9)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run_diagnostic(args)
    except (DiagnosticError, FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    summary = report["summary"]
    print(
        "model_family_diagnostic "
        f"selection={report['selection']['sha256']} "
        f"baseline_avg={summary['baseline_primary_avg_r2']} "
        f"selected_avg={summary['selected_primary_avg_r2']} "
        f"action={summary['recommended_action']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
