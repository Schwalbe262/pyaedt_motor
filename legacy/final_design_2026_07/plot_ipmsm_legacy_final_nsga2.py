"""Plot the final exact NSGA-II volume/efficiency Pareto front."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--combined-dir", type=Path, required=True)
    args = parser.parse_args()
    directory = args.combined_dir.resolve()
    exact = pd.read_csv(directory / "global_exact_pareto.csv").sort_values(
        "active_envelope_volume_l"
    )
    plt.rcParams.update(
        {
            "font.size": 16,
            "axes.titlesize": 25,
            "axes.labelsize": 20,
            "xtick.labelsize": 16,
            "ytick.labelsize": 16,
        }
    )
    figure, axis = plt.subplots(figsize=(10, 7.5))
    axis.scatter(
        exact["active_envelope_volume_l"],
        exact["predicted_efficiency_pct"],
        s=48,
        c="#79aee0",
        alpha=0.92,
        edgecolors="#17202a",
        linewidths=0.75,
        zorder=2,
    )

    axis.set_title("Pareto Front: Volume vs Efficiency", pad=12)
    axis.set_xlabel("Volume [L]", labelpad=8)
    axis.set_ylabel("Efficiency [%]", labelpad=8)
    axis.grid(True, color="#b8bec5", alpha=0.34, linewidth=0.8)
    axis.margins(x=0.04, y=0.06)
    figure.tight_layout()
    figure.savefig(
        directory / "final_nsga2_pareto_volume_efficiency.png",
        dpi=300,
        bbox_inches="tight",
    )
    figure.savefig(
        directory / "final_nsga2_pareto_volume_efficiency.svg",
        bbox_inches="tight",
    )
    plt.close(figure)


if __name__ == "__main__":
    main()
