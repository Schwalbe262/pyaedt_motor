"""Group-held-out diagnosis of input features and primitive target scaling.

Model selection only sees an inner partition of the training geometries. The
calibration partition remains reserved. Historical data produce diagnostic
artifacts; they cannot promote an unverified v3 design model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import warnings

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, r2_score

from .surrogate import CANDIDATES, TARGETS, PrimitiveModel, derive_performance


def metrics(truth, prediction):
    if not np.isfinite(np.asarray(prediction)).all():
        return {"r2": None, "mae": None, "finite": False}
    return {"r2": float(r2_score(truth, prediction)),
            "mae": float(mean_absolute_error(truth, prediction)), "finite": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(args.output)
    # Reuse the audited input repair, preassigned geometry groups and fingerprint gates.
    import train_ipmsm_lightgbm as trainer
    deps = trainer.require_training_dependencies()
    prepared = trainer.prepare_training_data(deps, [args.data], True, False, 1.5, v2=True)
    outer = trainer.split_training_data(deps, prepared, .2, .2, args.seed)
    inner = trainer.build_v2_model_selection_split(prepared, outer, seed=args.seed)
    if inner is None:
        raise ValueError("not enough training geometries for independent model selection")
    expected = set(TARGETS)
    if not expected <= set(outer.y_train.columns):
        raise ValueError(f"missing primitive targets: {sorted(expected - set(outer.y_train.columns))}")
    args.output.mkdir(parents=True, exist_ok=False)
    source_manifest = {}
    for source in [Path(__file__), Path(__file__).with_name("surrogate.py"), Path(trainer.__file__)]:
        payload = source.read_bytes()
        snapshot = args.output / "source" / source.name
        snapshot.parent.mkdir(exist_ok=True)
        snapshot.write_bytes(payload)
        source_manifest[source.name] = hashlib.sha256(payload).hexdigest()
    (args.output / "source_sha256.json").write_text(json.dumps(source_manifest, indent=2), encoding="utf-8")
    selected, inner_metrics = {}, {}
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*degree.*should not be below.*")
        for target in TARGETS:
            records = {}
            for family in CANDIDATES:
                model = PrimitiveModel.fit(inner.x_train, target, inner.y_train[target], family,
                                           seed=args.seed, jobs=args.jobs)
                records[family] = metrics(inner.y_val[target], model.predict(inner.x_val))
            valid = [family for family in CANDIDATES if records[family]["finite"]]
            if not valid:
                raise ValueError(f"no finite candidate for {target}")
            selected[target] = min(valid, key=lambda family: (records[family]["mae"], family))
            inner_metrics[target] = records
            print(f"selected {target}: {selected[target]}", flush=True)
        selection = {"seed": args.seed, "data_sha256": hashlib.sha256(args.data.read_bytes()).hexdigest(),
                     "candidates": list(CANDIDATES), "selected": selected, "inner_metrics": inner_metrics,
                     "fit_groups": list(inner.train_group_ids), "selection_groups": list(inner.val_group_ids),
                     "test_groups": list(outer.test_group_ids), "test_used_for_selection": False}
        # Freeze the choice before reading test targets for the final evaluation.
        (args.output / "selection.json").write_text(json.dumps(selection, indent=2, allow_nan=False), encoding="utf-8")
        models, predictions = {}, {}
        for target in TARGETS:
            models[target] = PrimitiveModel.fit(outer.x_train, target, outer.y_train[target], selected[target],
                                                seed=args.seed, jobs=args.jobs)
            predictions[target] = models[target].predict(outer.x_test)
    predicted = derive_performance(outer.x_test, pd.DataFrame(predictions, index=outer.x_test.index))
    truth = prepared.valid_df.loc[outer.x_test.index]
    final = {target: metrics(truth[target], predicted[target]) for target in predicted.columns}
    v3 = prepared.fingerprints.get("input_metric_contract_version") == "motor_metrics_v3"
    all_pass = all(record["finite"] and record["r2"] is not None and record["r2"] >= .95 for record in final.values())
    report = {"scope": "v3_candidate" if v3 else "historical_diagnostic_only", "metrics": final,
              "r2_threshold": .95, "all_targets_pass": all_pass,
              "design_promotion_allowed": False,
              "promotion_note": "Requires conformal calibration, input-domain checks and independent final FEA.",
              "fingerprints": prepared.fingerprints, "rows": len(prepared.valid_df),
              "split_rows": {"train": len(outer.x_train), "calibration": len(outer.x_val), "test": len(outer.x_test)}}
    (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    joblib.dump({"models": models, "fingerprints": prepared.fingerprints, "selection": selection,
                 "scope": report["scope"]}, args.output / "diagnostic_models.joblib")
    predicted.to_csv(args.output / "test_predictions.csv", index_label="source_row")
    for target, record in final.items():
        print(f"test {target}: R2={record['r2']} MAE={record['mae']}")
    print(f"Benchmark saved: {args.output}; all R2 gates passed={all_pass}; scope={report['scope']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
