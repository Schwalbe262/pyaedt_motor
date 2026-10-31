"""Verify regression model metrics against project R2 targets."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path


REQUIRED_COLUMNS = ("target", "split", "R2")


def read_metric_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"metrics CSV missing required columns: {missing}")
        return [dict(row) for row in reader]


def finite_float(value: object) -> float:
    try:
        number = float(str(value).strip())
    except Exception:
        return math.nan
    return number if math.isfinite(number) else math.nan


def select_split(rows: list[dict[str, str]], split: str) -> list[dict[str, str]]:
    selected = [row for row in rows if row.get("split", "").strip().lower() == split.lower()]
    if not selected:
        raise ValueError(f"no metric rows found for split={split!r}")
    return selected


def summarize_split(rows: list[dict[str, str]], r2_threshold: float) -> tuple[list[dict[str, str]], str, int]:
    summary_rows: list[dict[str, str]] = []
    r2_values: list[float] = []
    failures = 0

    for row in sorted(rows, key=lambda item: item.get("target", "")):
        r2 = finite_float(row.get("R2", ""))
        passed = math.isfinite(r2) and r2 >= r2_threshold
        if math.isfinite(r2):
            r2_values.append(r2)
        if not passed:
            failures += 1
        summary_rows.append(
            {
                "target": row.get("target", ""),
                "split": row.get("split", ""),
                "R2": f"{r2:.12g}" if math.isfinite(r2) else "",
                "R2_threshold": f"{r2_threshold:.12g}",
                "R2_gap": f"{r2 - r2_threshold:.12g}" if math.isfinite(r2) else "",
                "status": "pass" if passed else "fail",
                "MAE": row.get("MAE", ""),
                "RMSE": row.get("RMSE", ""),
                "MAPE_pct": row.get("MAPE_pct", ""),
                "best_iteration": row.get("best_iteration", ""),
            }
        )

    min_r2 = min(r2_values) if r2_values else math.nan
    avg_r2 = sum(r2_values) / len(r2_values) if r2_values else math.nan
    summary = (
        f"targets={len(summary_rows)} failures={failures} threshold={r2_threshold:.12g} "
        f"min_R2={min_r2:.12g} avg_R2={avg_r2:.12g}"
    )
    return summary_rows, summary, failures


def write_summary(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        "target",
        "split",
        "R2",
        "R2_threshold",
        "R2_gap",
        "status",
        "MAE",
        "RMSE",
        "MAPE_pct",
        "best_iteration",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify regression metrics against an R2 target.")
    parser.add_argument("--metrics", type=Path, required=True, help="metrics.csv produced by regression training.")
    parser.add_argument("--split", default="test", help="Split to verify, usually test.")
    parser.add_argument("--r2-threshold", type=float, default=0.95)
    parser.add_argument("--output", type=Path, required=True, help="Filtered verification CSV to write.")
    parser.add_argument("--fail-on-threshold", action="store_true", help="Return exit code 1 when any target misses the threshold.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rows = select_split(read_metric_rows(args.metrics), args.split)
        summary_rows, summary, failures = summarize_split(rows, args.r2_threshold)
    except ValueError as exc:
        parser.error(str(exc))
    write_summary(args.output, summary_rows)
    print(f"Wrote {len(summary_rows)} regression metric verification row(s) to {args.output}")
    print(summary)
    return 1 if args.fail_on_threshold and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
