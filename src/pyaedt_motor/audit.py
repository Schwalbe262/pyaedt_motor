"""Small evidence summaries instead of loading simulation dumps into chat."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd


def dataset_summary(source: Path) -> dict:
    d = pd.read_csv(source, low_memory=False)
    result = {"source": str(source.resolve()), "rows": len(d), "columns": len(d.columns)}
    for name in ["status", "geometry_group_id", "doe_split", "input_model_extent", "input_symmetry_factor",
                 "input_beta_convention", "input_setup_fingerprint", "input_quality_profile",
                 "input_material_fingerprint", "input_metric_contract_version"]:
        if name in d:
            result[name] = {"unique": int(d[name].nunique()),
                            "counts": {str(k): int(v) for k, v in d[name].value_counts(dropna=False).head(8).items()}}
    for name in ["elapsed_s", "input_base_rpm", "input_i_peak_a", "input_beta_dq_deg",
                 "input_transient_periods", "input_steps_per_period", "input_stator_outer_radius"]:
        if name in d:
            vals = pd.to_numeric(d[name], errors="coerce")
            result[name] = {k: float(v) if math.isfinite(v) else None for k, v in
                            zip(["min", "median", "p90", "max"], vals.quantile([0, .5, .9, 1]))}
    if "input_stator_outer_radius" in d:
        result["diameter_over_300mm_rows"] = int((d.input_stator_outer_radius > 150).sum())
    if {"geometry_group_id", "doe_split"} <= set(d):
        result["groups_crossing_splits"] = int((d.groupby("geometry_group_id").doe_split.nunique() > 1).sum())
    if "repeat_of_case_id" in d:
        result["repeat_rows"] = int(d.repeat_of_case_id.fillna("").astype(str).str.strip().ne("").sum())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = dataset_summary(args.dataset)
    if args.model_dir:
        metrics = pd.read_csv(args.model_dir / "metrics.csv")
        report["test_metrics"] = metrics.loc[metrics.split == "test", ["target", "R2", "MAE", "RMSE"]].to_dict("records")
        report["primary_r2_failures"] = int((metrics.loc[metrics.split == "test", "R2"] < .95).sum())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Audit: rows={report['rows']}, evidence={args.output}")
    return 0
