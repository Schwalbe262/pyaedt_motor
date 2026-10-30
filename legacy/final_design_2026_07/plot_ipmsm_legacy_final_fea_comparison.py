"""Create presentation-ready plots for final NSGA-II versus FEA validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _metric_box(axis: plt.Axes, metrics: dict[str, float]) -> None:
    axis.text(
        0.96,
        0.04,
        "\n".join(
            (
                f"MAE: {metrics['MAE']:.3f}",
                f"RMSE: {metrics['RMSE']:.3f}",
                f"Bias: {metrics['bias']:+.3f}",
            )
        ),
        transform=axis.transAxes,
        ha="right",
        va="bottom",
        fontsize=13,
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.86},
    )


def _identity_panel(
    axis: plt.Axes,
    frame: pd.DataFrame,
    actual: str,
    predicted: str,
    title: str,
    unit: str,
    metrics: dict[str, float],
) -> None:
    x = frame[actual].to_numpy(dtype=float)
    y = frame[predicted].to_numpy(dtype=float)
    low = min(float(np.min(x)), float(np.min(y)))
    high = max(float(np.max(x)), float(np.max(y)))
    padding = max((high - low) * 0.09, 0.05)
    limits = (low - padding, high + padding)
    axis.scatter(
        x,
        y,
        s=90,
        c="#3f88c5",
        alpha=0.84,
        edgecolors="#17202a",
        linewidths=0.75,
        zorder=2,
    )
    axis.plot(limits, limits, "--", color="#d62728", linewidth=1.8, zorder=1)
    axis.set_xlim(*limits)
    axis.set_ylim(*limits)
    axis.set_aspect("equal", adjustable="box")
    axis.set_title(title)
    axis.set_xlabel(f"FEA [{unit}]")
    axis.set_ylabel(f"Predicted [{unit}]")
    axis.grid(True, alpha=0.27)
    _metric_box(axis, metrics)


def actual_vs_predicted(
    frame: pd.DataFrame, summary: dict[str, object], output_dir: Path
) -> None:
    metrics = summary["primary_error_metrics"]
    figure, axes = plt.subplots(2, 2, figsize=(16, 14))
    specs = (
        (
            "fea_torque_nm",
            "predicted_torque_nm",
            "Torque",
            "N·m",
            metrics["torque_nm"],
        ),
        (
            "fea_efficiency_physical_pct",
            "predicted_efficiency_physical_pct",
            "Physical efficiency",
            "%",
            metrics["efficiency_physical_pct"],
        ),
        (
            "fea_total_loss_physical_w",
            "predicted_total_loss_physical_w",
            "Physical total loss",
            "W",
            metrics["total_loss_physical_w"],
        ),
        (
            "fea_core_loss_physical_w",
            "predicted_core_loss_w",
            "Core loss",
            "W",
            metrics["core_loss_w"],
        ),
    )
    for axis, spec in zip(axes.flat, specs):
        _identity_panel(axis, frame, *spec)
    figure.suptitle("Model/NSGA-II Prediction vs Reference-Ultra FEA", fontsize=25, y=0.995)
    figure.tight_layout(rect=(0, 0, 1, 0.98))
    figure.savefig(
        output_dir / "model_vs_fea_actual_predicted.png",
        dpi=300,
        bbox_inches="tight",
    )
    plt.close(figure)


def validation_on_front(
    comparison: pd.DataFrame,
    predictions: pd.DataFrame,
    exact: pd.DataFrame,
    candidates: pd.DataFrame,
    output_dir: Path,
) -> None:
    merged = predictions[["case_id", "active_envelope_volume_l"]].merge(
        comparison, on="case_id", validate="one_to_one"
    )
    actual_sorted = merged.sort_values("active_envelope_volume_l")
    actual_front_rows = []
    best_efficiency = -np.inf
    for _, row in actual_sorted.iterrows():
        if float(row["fea_efficiency_physical_pct"]) > best_efficiency + 1.0e-12:
            actual_front_rows.append(row)
            best_efficiency = float(row["fea_efficiency_physical_pct"])
    actual_front = pd.DataFrame(actual_front_rows)

    figure, axis = plt.subplots(figsize=(13, 9))
    exact_sorted = exact.sort_values("active_envelope_volume_l")
    axis.plot(
        exact_sorted["active_envelope_volume_l"],
        exact_sorted["predicted_efficiency_pct"],
        color="#365a7c",
        linewidth=1.6,
        alpha=0.75,
        label=f"Predicted exact Pareto ({len(exact)})",
    )
    axis.scatter(
        candidates["active_envelope_volume_l"],
        candidates["predicted_efficiency_pct"],
        s=54,
        color="#75aadb",
        alpha=0.72,
        edgecolors="#1e2a35",
        linewidths=0.5,
        label=f"Predicted candidate set ({len(candidates)})",
    )
    for _, row in merged.iterrows():
        axis.plot(
            [row["active_envelope_volume_l"], row["active_envelope_volume_l"]],
            [row["predicted_efficiency_physical_pct"], row["fea_efficiency_physical_pct"]],
            color="#777777",
            linestyle="--",
            linewidth=1.0,
            alpha=0.75,
        )
    axis.scatter(
        merged["active_envelope_volume_l"],
        merged["predicted_efficiency_physical_pct"],
        s=190,
        marker="*",
        color="#f28e2b",
        edgecolors="#572b00",
        linewidths=0.9,
        label="Selected prediction (10)",
        zorder=4,
    )
    axis.scatter(
        merged["active_envelope_volume_l"],
        merged["fea_efficiency_physical_pct"],
        s=105,
        marker="s",
        color="#d62728",
        edgecolors="#4a0b0b",
        linewidths=0.8,
        label="Reference-ultra FEA (10)",
        zorder=5,
    )
    axis.plot(
        actual_front["active_envelope_volume_l"],
        actual_front["fea_efficiency_physical_pct"],
        color="#8b0000",
        linewidth=2.0,
        label=f"FEA nondominated subset ({len(actual_front)})",
        zorder=3,
    )
    for _, row in merged.iterrows():
        axis.annotate(
            str(int(row["selection_order"])),
            (row["active_envelope_volume_l"], row["fea_efficiency_physical_pct"]),
            xytext=(6, -15),
            textcoords="offset points",
            fontsize=12,
        )
    axis.set_title("Final NSGA-II Front and Reference-Ultra FEA Validation")
    axis.set_xlabel("Active envelope volume [L]")
    axis.set_ylabel("Physical efficiency [%]")
    axis.grid(True, alpha=0.28)
    axis.legend(loc="center right", fontsize=13)
    figure.tight_layout()
    figure.savefig(
        output_dir / "fea_validation_on_nsga2_front.png",
        dpi=300,
        bbox_inches="tight",
    )
    plt.close(figure)


def symmetry_diagnostic(
    frame: pd.DataFrame, summary: dict[str, object], output_dir: Path
) -> None:
    x = frame["fea_torque_nm"].to_numpy(dtype=float)
    y = frame["predicted_torque_nm"].to_numpy(dtype=float)
    diagnostic = summary["legacy_symmetry4_torque_diagnostic"]
    figure, axis = plt.subplots(figsize=(11, 8))
    axis.scatter(
        x,
        y,
        s=105,
        c="#3f88c5",
        alpha=0.86,
        edgecolors="#17202a",
        linewidths=0.8,
        zorder=3,
    )
    x_line = np.linspace(float(np.min(x)) * 0.98, float(np.max(x)) * 1.02, 100)
    axis.plot(x_line, 4.0 * x_line, "--", color="#d62728", linewidth=2.0, label="y = 4 × FEA")
    for ordinal, (actual_value, predicted_value) in enumerate(zip(x, y), start=1):
        axis.annotate(
            str(ordinal),
            (actual_value, predicted_value),
            xytext=(6, 4),
            textcoords="offset points",
            fontsize=12,
        )
    ratio = diagnostic["predicted_to_raw_fea_ratio"]
    scaled = diagnostic["predicted_minus_scaled_fea_error_metrics"]
    axis.text(
        0.04,
        0.96,
        f"Mean Pred/FEA ratio: {ratio['mean']:.3f}\nPred vs 4×FEA MAE: {scaled['MAE']:.3f} N·m",
        transform=axis.transAxes,
        va="top",
        fontsize=15,
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.88},
    )
    axis.set_title("Legacy Symmetry-Multiplier Torque Diagnostic")
    axis.set_xlabel("Full-360, symmetry=1 FEA torque [N·m]")
    axis.set_ylabel("Legacy surrogate predicted torque [N·m]")
    axis.grid(True, alpha=0.28)
    axis.legend(loc="lower right")
    figure.tight_layout()
    figure.savefig(
        output_dir / "legacy_symmetry4_torque_diagnostic.png",
        dpi=300,
        bbox_inches="tight",
    )
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison-dir", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--combined-dir", type=Path, required=True)
    args = parser.parse_args()
    comparison_dir = args.comparison_dir.resolve()
    frame = pd.read_csv(comparison_dir / "predicted_vs_fea.csv")
    summary = json.loads((comparison_dir / "comparison_summary.json").read_text(encoding="utf-8"))
    predictions = pd.read_csv(args.predictions)
    exact = pd.read_csv(args.combined_dir / "global_exact_pareto.csv")
    candidates = pd.read_csv(args.combined_dir / "pareto_candidate_set_200.csv")
    plt.rcParams.update(
        {
            "font.size": 16,
            "axes.titlesize": 22,
            "axes.labelsize": 18,
            "xtick.labelsize": 14,
            "ytick.labelsize": 14,
            "legend.fontsize": 14,
        }
    )
    actual_vs_predicted(frame, summary, comparison_dir)
    validation_on_front(frame, predictions, exact, candidates, comparison_dir)
    symmetry_diagnostic(frame, summary, comparison_dir)


if __name__ == "__main__":
    main()
