"""Summarize LightGBM output IQR outliers for IPMSM training CSVs."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Iterable

import train_ipmsm_lightgbm as trainer


SUMMARY_FIELDNAMES = (
    "metric",
    "rows",
    "q1",
    "q3",
    "iqr",
    "low",
    "high",
    "outlier_rows",
    "outlier_pct",
)
COMBINED_FIELDNAMES = (
    "rows",
    "rows_with_any_output_outlier",
    "rows_without_output_outliers",
    "max_metric_outlier_count",
)


def normalize_fieldname(fieldname: str | None) -> str:
    return (fieldname or "").lstrip("\ufeff")


def finite_float(value: object) -> float:
    try:
        number = float(str(value).strip())
    except Exception:
        return math.nan
    return number if math.isfinite(number) else math.nan


def format_number(value: float) -> str:
    if not math.isfinite(value):
        return ""
    return f"{value:.12g}"


def read_rows(paths: Iterable[Path]) -> tuple[list[dict[str, str]], list[str]]:
    rows: list[dict[str, str]] = []
    fieldnames: list[str] = []
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            reader.fieldnames = [normalize_fieldname(fieldname) for fieldname in reader.fieldnames or ()]
            for fieldname in reader.fieldnames or ():
                if fieldname not in fieldnames:
                    fieldnames.append(fieldname)
            rows.extend(dict(row) for row in reader)
    return rows, fieldnames


def parse_csv_list(text: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in text.split(",") if part.strip())
    if not values:
        raise argparse.ArgumentTypeError("value must contain at least one item")
    return values


def resolve_metrics(fieldnames: Iterable[str], metrics_text: str | None) -> tuple[str, ...]:
    requested = parse_csv_list(metrics_text) if metrics_text else trainer.REQUESTED_OUTPUT_COLUMNS
    return trainer.resolve_output_columns(fieldnames, requested_columns=requested)[0]


def quantile(values: list[float], fraction: float) -> float:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return math.nan
    if len(finite) == 1:
        return finite[0]
    position = (len(finite) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return finite[lower]
    weight = position - lower
    return finite[lower] * (1.0 - weight) + finite[upper] * weight


def build_outlier_summary(
    rows: list[dict[str, str]],
    metrics: tuple[str, ...],
    *,
    outlier_iqr_weight: float = trainer.OUTLIER_IQR_WEIGHT,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    row_outlier_counts = [0 for _ in rows]
    summary_rows: list[dict[str, str]] = []
    for metric in metrics:
        values = [finite_float(row.get(metric, "")) for row in rows]
        q1 = quantile(values, 0.25)
        q3 = quantile(values, 0.75)
        iqr = q3 - q1
        low = q1 - outlier_iqr_weight * iqr
        high = q3 + outlier_iqr_weight * iqr
        outlier_indices = [
            index
            for index, value in enumerate(values)
            if math.isfinite(value) and not low <= value <= high
        ]
        for index in outlier_indices:
            row_outlier_counts[index] += 1
        summary_rows.append(
            {
                "metric": metric,
                "rows": str(len([value for value in values if math.isfinite(value)])),
                "q1": format_number(q1),
                "q3": format_number(q3),
                "iqr": format_number(iqr),
                "low": format_number(low),
                "high": format_number(high),
                "outlier_rows": str(len(outlier_indices)),
                "outlier_pct": format_number(len(outlier_indices) / len(rows) * 100.0) if rows else "",
            }
        )
    rows_with_any = sum(1 for count in row_outlier_counts if count > 0)
    combined = {
        "rows": str(len(rows)),
        "rows_with_any_output_outlier": str(rows_with_any),
        "rows_without_output_outliers": str(len(rows) - rows_with_any),
        "max_metric_outlier_count": str(max(row_outlier_counts) if row_outlier_counts else 0),
    }
    return summary_rows, combined


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize IPMSM output IQR outliers.")
    parser.add_argument("--results", nargs="+", type=Path, required=True)
    parser.add_argument("--metrics", help="Comma-separated output metrics. Defaults to LightGBM training targets.")
    parser.add_argument("--outlier-iqr-weight", type=float, default=trainer.OUTLIER_IQR_WEIGHT)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--combined-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.outlier_iqr_weight < 0:
        parser.error("--outlier-iqr-weight must be zero or greater")
    rows, fieldnames = read_rows(args.results)
    try:
        metrics = resolve_metrics(fieldnames, args.metrics)
    except ValueError as exc:
        parser.error(str(exc))
    summary_rows, combined = build_outlier_summary(rows, metrics, outlier_iqr_weight=args.outlier_iqr_weight)
    write_csv(args.summary_output, summary_rows, SUMMARY_FIELDNAMES)
    if args.combined_output:
        write_csv(args.combined_output, [combined], COMBINED_FIELDNAMES)
    print(
        "output_outliers "
        + " ".join(f"{key}={combined[key]}" for key in COMBINED_FIELDNAMES)
        + f" metrics={len(metrics)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
