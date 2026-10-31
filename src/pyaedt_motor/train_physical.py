"""Train a checked motor surrogate without using calibration/test for selection."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import warnings

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, r2_score

from .io import atomic_write_bytes
from .surrogate import CANDIDATES, GEOMETRY, PrimitiveModel
from .model_bundle import (
    PRODUCTION_TARGETS as TARGETS, VOLTAGE_SPAN_TARGET, ALL_TARGETS, PhysicalMotorBundle, canonical_json, calibrate_joint, check_fingerprints,
    domain_from_training, case_winding_constants, physics_issues, prediction_frame, promotion_issues, save_bundle,
    transformed_scales, validate_declared_inputs, validate_inputs, validate_raw_geometry_fields,
    capture_runtime_pin, assert_runtime_pin, source_payload_sha256,
)


def score(truth, predictions) -> dict:
    actual, predicted = np.asarray(truth, dtype=float), np.asarray(predictions, dtype=float)
    if len(actual) < 2 or not np.isfinite(actual).all() or not np.isfinite(predicted).all():
        return {"finite": False, "r2": None, "mae": None}
    return {"finite": True, "r2": float(r2_score(actual, predicted)),
            "mae": float(mean_absolute_error(actual, predicted))}


def score_by_speed(truth: pd.DataFrame, predictions: pd.DataFrame) -> dict:
    """Expose operating-point errors after selection, without filtering test rows."""
    required = {"input_base_rpm", "geometry_group_id", *ALL_TARGETS}
    if required - set(truth) or set(ALL_TARGETS) - set(predictions):
        raise ValueError("speed diagnostics require complete test inputs and all targets")
    if (not len(truth) or not truth.index.is_unique or not predictions.index.is_unique or
            not truth.index.equals(predictions.index)):
        raise ValueError("speed diagnostics require nonempty, uniquely aligned test rows")
    speeds = pd.to_numeric(truth["input_base_rpm"], errors="coerce").to_numpy(float)
    groups = truth["geometry_group_id"]
    if (not np.isfinite(speeds).all() or (speeds <= 0).any() or groups.isna().any() or
            groups.astype(str).str.strip().eq("").any()):
        raise ValueError("speed diagnostics require finite positive speeds and geometry identities")
    records = []
    for speed in np.unique(speeds):
        positions = np.flatnonzero(speeds == speed)
        actual = truth.iloc[positions]
        predicted = predictions.iloc[positions]
        metrics = {}
        for target in ALL_TARGETS:
            y = actual[target].to_numpy(float)
            p = predicted[target].to_numpy(float)
            finite = bool(np.isfinite(y).all() and np.isfinite(p).all())
            reason = ("nonfinite_values" if not finite else
                      "fewer_than_two_rows" if len(y) < 2 else
                      "constant_truth" if np.ptp(y) == 0 else None)
            metrics[target] = {
                "finite": finite, "r2": float(r2_score(y, p)) if reason is None else None,
                "r2_undefined_reason": reason,
                "mae": float(mean_absolute_error(y, p)) if finite else None,
                "max_abs_error": float(np.max(np.abs(y - p))) if finite else None,
            }
        records.append({"speed_rpm": float(speed), "rows": len(positions),
                        "geometry_groups": int(actual["geometry_group_id"].astype(str).nunique()),
                        "metrics": metrics})
    return {"evaluation_scope": "all_preassigned_test_rows_partitioned_by_observed_speed",
            "used_for_selection": False, "diagnostic_only": True,
            "rows": len(truth), "by_speed_rpm": records}


def validate_new_data(frame: pd.DataFrame) -> None:
    """Reject bad/old rows rather than silently changing the evaluation cohort."""
    required = {"case_id", "geometry_group_id", "design_hash", "input_metric_contract_version", "input_producer_fingerprint",
                "input_stack_length_mm", *["input_" + name for name in GEOMETRY], *ALL_TARGETS}
    if required - set(frame):
        raise ValueError("production training requires complete new motor_metrics_v3 rows")
    if not len(frame) or frame.case_id.isna().any() or frame.case_id.astype(str).str.strip().eq("").any():
        raise ValueError("missing training case identity")
    if not frame.case_id.is_unique:
        raise ValueError("duplicate training case identities are not permitted")
    if set(frame.input_metric_contract_version.astype(str)) != {"motor_metrics_v3"}:
        raise ValueError("production training cannot use historical or mixed metric contracts")
    if "input_operation" not in frame or not frame.input_operation.eq("sin_current").all():
        raise ValueError("loaded surrogate training requires sin_current motoring cases")
    case_winding_constants(frame)
    validate_raw_geometry_fields(frame)
    physical_owners, group_keys = {}, {}
    for _, row in frame.iterrows():
        key = tuple(format(float(row["input_" + name]), ".12g") for name in (*GEOMETRY, "stack_length_mm"))
        group = str(row["geometry_group_id"]).strip()
        if physical_owners.setdefault(key, group) != group:
            raise ValueError("same physical geometry is assigned to different group identities")
        if group_keys.setdefault(group, key) != key:
            raise ValueError("one geometry group contains different physical geometries")
    import train_ipmsm_lightgbm as trainer
    import validate_ipmsm_v2_dataset as validator
    fingerprints = trainer.validate_v2_fingerprints(frame)
    check_fingerprints(fingerprints)
    result = validator.validate_rows(frame.fillna("").to_dict("records"), fieldnames=list(frame))
    if not result.passed:
        raise ValueError(f"new training dataset physics/identity gate failed: {result.as_row()['issues']}")
    if not np.isfinite(frame[list(ALL_TARGETS)].to_numpy(float)).all():
        raise ValueError("nonfinite new motor targets")
    issues = physics_issues(frame)
    if issues:
        raise ValueError("new training target physics gate failed: " + ",".join(issues))


def select_families(inner, *, seed=42, jobs=4, candidates=CANDIDATES) -> dict:
    """Only inner fitting and selection fields are accessed by this function."""
    if not candidates or len(set(candidates)) != len(candidates) or set(candidates) - set(CANDIDATES):
        raise ValueError("invalid surrogate candidate family list")
    if set(inner.train_group_ids) & set(inner.val_group_ids):
        raise ValueError("inner fitting/selection geometries overlap")
    selected, records = {}, {}
    for target in TARGETS:
        scores = {}
        for family in candidates:
            try:
                model = PrimitiveModel.fit(inner.x_train, target, inner.y_train[target], family, seed=seed, jobs=jobs)
                scores[family] = score(inner.y_val[target], model.predict(inner.x_val))
            except (ValueError, FloatingPointError, np.linalg.LinAlgError) as exc:
                scores[family] = {"finite": False, "mae": None, "r2": None, "error": str(exc)[:500]}
        finite = [family for family in candidates if scores[family]["finite"]]
        if not finite:
            raise ValueError(f"no finite inner model for {target}")
        selected[target] = min(finite, key=lambda family: (scores[family]["mae"], family))
        records[target] = scores
    return {"selected": selected, "inner_metrics": records, "seed": seed, "candidates": list(candidates),
            "fit_groups": sorted(inner.train_group_ids), "selection_groups": sorted(inner.val_group_ids),
            "test_used_for_selection": False}


def assert_effective_cohort_unchanged(prepared) -> None:
    raw = prepared.raw_df.set_index("case_id")
    effective = prepared.valid_df.set_index("case_id")
    if set(raw.index) != set(effective.index):
        raise ValueError("preprocessing changed the physical training cohort")
    effective = effective.loc[raw.index]
    columns = list(dict.fromkeys([*prepared.input_columns, *ALL_TARGETS]))
    before = raw[columns].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    after = effective[columns].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    if not np.isfinite(before).all() or not np.allclose(before, after, rtol=1e-12, atol=1e-12):
        raise ValueError("preprocessing changed new v3 inputs or target labels")
    for column in ["geometry_group_id", "design_hash", "doe_split"]:
        if not raw[column].astype(str).equals(effective[column].astype(str)):
            raise ValueError("preprocessing changed physical group identities or partition labels")


def _outside_support_count(inputs, domain) -> int:
    count = 0
    for index in range(len(inputs)):
        try:
            validate_inputs(inputs.iloc[index:index + 1], domain)
        except ValueError:
            count += 1
    return count


def fit_prepared(prepared, spec: dict, destination: Path, *, seed=42, jobs=4,
                 candidates=CANDIDATES, data_sha256: str | None = None, frame_evidence=None) -> PhysicalMotorBundle:
    """Publish selection before evaluating the full preassigned test cohort."""
    import train_ipmsm_lightgbm as trainer
    runtime_pin = capture_runtime_pin()
    root = Path(trainer.__file__).resolve().parent
    training_paths = {"train_physical.py": Path(__file__), "train_ipmsm_lightgbm.py": Path(trainer.__file__),
                      "validate_ipmsm_v2_dataset.py": root / "validate_ipmsm_v2_dataset.py",
                      "ipmsm_waveform_quality.py": root / "module/ipmsm_waveform_quality.py"}
    training_payloads = {name: path.read_bytes() for name, path in training_paths.items()}
    training_hashes = {name: source_payload_sha256(payload) for name, payload in training_payloads.items()}
    def publication_guard():
        assert_runtime_pin(runtime_pin)
        if frame_evidence is not None:
            frame_evidence.assert_unchanged()
        if any(source_payload_sha256(path.read_bytes()) != training_hashes[name]
               for name, path in training_paths.items()):
            raise RuntimeError("training/preprocessing code changed during fitting; refusing publication")
    publication_guard()
    validate_new_data(prepared.raw_df)
    validate_new_data(prepared.valid_df)
    if any(prepared.repaired_derived_inputs.values()):
        raise ValueError("new v3 data must not require historical geometry repairs")
    assert_effective_cohort_unchanged(prepared)
    check_fingerprints(prepared.fingerprints)
    if len(prepared.valid_df) != len(prepared.raw_df):
        raise ValueError("production training cannot silently filter the preassigned data cohort")
    # The historical preparer keeps the new span column but selects only its
    # original targets. Extend the effective target contract before either split.
    prepared = replace(prepared, auxiliary_output_columns=tuple(dict.fromkeys(
        (*prepared.auxiliary_output_columns, VOLTAGE_SPAN_TARGET))))
    deps = trainer.require_training_dependencies()
    outer = trainer.split_training_data(deps, prepared, .2, .2, seed)
    inner = trainer.build_v2_model_selection_split(prepared, outer, seed=seed)
    if inner is None:
        raise ValueError("not enough independent fitting geometries")
    if (set(inner.train_group_ids) | set(inner.val_group_ids)) != set(outer.train_group_ids):
        raise ValueError("inner selection is not confined to the outer training geometries")
    domain = domain_from_training(outer.x_train, spec, case_frame=prepared.raw_df, frame_evidence=frame_evidence)
    validate_declared_inputs(prepared.valid_df[list(prepared.input_columns)], domain["declaration"])
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*degree.*should not be below.*")
        selection = select_families(inner, seed=seed, jobs=jobs, candidates=candidates)
        selection.update(outer_train_groups=sorted(outer.train_group_ids),
                         calibration_groups=sorted(outer.val_group_ids), test_groups=sorted(outer.test_group_ids))
        selection_payload = canonical_json(selection)
        atomic_write_bytes(destination / "selection.json", selection_payload)
        models = {target: PrimitiveModel.fit(outer.x_train, target, outer.y_train[target],
                                            selection["selected"][target], seed=seed, jobs=jobs) for target in TARGETS}
        scales = transformed_scales(outer.x_train, outer.y_train)
        cal_predictions = prediction_frame(models, outer.x_val)
        try:
            calibration = calibrate_joint(outer.x_val, outer.y_val, cal_predictions,
                                          prepared.valid_df.loc[outer.x_val.index, prepared.geometry_group_column], scales)
        except ValueError as exc:
            calibration = {"error": str(exc), "coverage": .95}
        # No test targets were accessed by model-family fitting/selection above.
        test_predictions = prediction_frame(models, outer.x_test)
    truth = prepared.valid_df.loc[outer.x_test.index]
    metrics = {target: score(truth[target], test_predictions[target]) for target in ALL_TARGETS}
    metadata = {"fingerprints": prepared.fingerprints, "domain": domain, "calibration": calibration,
                "selected_families": selection["selected"], "selection": selection,
                "selection_sha256": hashlib.sha256(selection_payload).hexdigest(),
                "dataset_sha256": data_sha256 or hashlib.sha256(prepared.raw_df.to_csv(index=False, lineterminator="\n").encode()).hexdigest(),
                "dataset_hash_kind": "raw_input_csv" if data_sha256 else "canonical_prepared_frame_csv",
                "test_used_for_selection": False, "test_metrics": metrics,
                "test_metrics_by_speed": score_by_speed(truth, test_predictions),
                "test_physics_issues": physics_issues(test_predictions), "r2_threshold": .95,
                "fit_groups": sorted(outer.train_group_ids), "calibration_groups": sorted(outer.val_group_ids),
                "test_groups": sorted(outer.test_group_ids),
                "rows": {"train": len(outer.x_train), "calibration": len(outer.x_val), "test": len(outer.x_test)},
                "test_rows_outside_supported_box": _outside_support_count(outer.x_test, domain),
                "test_evaluation_scope": "all_preassigned_test_rows_without_input_or_output_filtering",
                "spec_sha256": hashlib.sha256(canonical_json(spec)).hexdigest(),
                "training_source_sha256": hashlib.sha256(training_payloads["train_physical.py"]).hexdigest(),
                "training_sources": training_hashes,
                "training_source_raw_sha256": {name: hashlib.sha256(payload).hexdigest() for name, payload in training_payloads.items()},
                "final_design_requires_independent_fea": True}
    issues = promotion_issues(metadata)
    metadata["promotion_issues"] = issues
    metadata["decision"] = "rejected" if issues else "accepted"
    bundle = PhysicalMotorBundle(models, metadata)
    save_bundle(bundle, destination / "artifact", runtime_pin=runtime_pin, publication_guard=publication_guard)
    atomic_write_bytes(destination / "report.json", canonical_json({
        key: metadata[key] for key in ["decision", "promotion_issues", "test_metrics", "test_metrics_by_speed", "test_physics_issues",
                                      "rows", "test_evaluation_scope", "test_rows_outside_supported_box"]}))
    test_predictions.to_csv(destination / "test_predictions.csv", index_label="source_row")
    atomic_write_bytes(destination / "source_spec.json", canonical_json(spec))
    atomic_write_bytes(destination / "source_train_physical.py", training_payloads["train_physical.py"])
    (destination / "training_source").mkdir()
    for name, payload in training_payloads.items():
        atomic_write_bytes(destination / "training_source" / name, payload)
    return bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    from . import reference_frames as frames
    frames.evidence_arguments(parser)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(args.output)
    if args.jobs < 1 or args.seed < 0:
        raise ValueError("jobs must be positive and seed nonnegative")
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    import train_ipmsm_lightgbm as trainer
    # Freeze bytes before preprocessing: a live collector may advance its CSV.
    payload = args.data.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    deps = trainer.require_training_dependencies()
    with tempfile.TemporaryDirectory(prefix="ipmsm_physical_training_") as temporary:
        snapshot = Path(temporary) / args.data.name
        snapshot.write_bytes(payload)
        frame = pd.read_csv(snapshot, low_memory=False)
        validate_new_data(frame)
        prepared = trainer.prepare_training_data(deps, [snapshot], False, False, 1.5, v2=True)
        bundle = fit_prepared(prepared, json.loads(args.spec.read_text(encoding="utf-8-sig")), args.output,
                              seed=args.seed, jobs=args.jobs, data_sha256=digest,
                              frame_evidence=frames.evidence_from_arguments(args))
    atomic_write_bytes(args.output / "source_dataset.csv", payload)
    for target, metric in bundle.metadata["test_metrics"].items():
        print(f"{target}: R2={metric['r2']} MAE={metric['mae']}")
    print(f"Physical surrogate decision={bundle.metadata['decision']}; artifact={args.output / 'artifact'}")
    return 0 if bundle.approved else 2


if __name__ == "__main__":
    raise SystemExit(main())
