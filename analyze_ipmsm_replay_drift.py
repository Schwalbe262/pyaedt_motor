"""Compare replayed IPMSM result rows against their source simulation rows."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Iterable

import train_ipmsm_lightgbm as trainer


SOURCE_ID_COLUMNS = ("input_source_case_id", "source_case_id")
PROFILE_COLUMNS = ("input_quality_profile", "quality_profile")
GROUP_COLUMNS = ("input_base_rpm", "input_i_peak_a", "input_beta_deg")
SUMMARY_FIELDNAMES = (
    "metric",
    "compared_rows",
    "mean_delta",
    "mean_abs_delta",
    "p90_abs_delta",
    "max_abs_delta",
    "mean_pct_delta",
    "mean_abs_pct_delta",
    "p50_abs_pct_delta",
    "p90_abs_pct_delta",
    "p95_abs_pct_delta",
    "max_abs_pct_delta",
    "over_threshold_rows",
    "over_threshold_pct",
)
OUTLIER_FIELDNAMES = (
    "metric",
    "source_case_id",
    "replay_case_id",
    "quality_profile",
    "baseline_value",
    "replay_value",
    "delta",
    "pct_delta",
    "abs_pct_delta",
    *GROUP_COLUMNS,
)


@dataclass(frozen=True)
class DriftRecord:
    metric: str
    source_case_id: str
    replay_case_id: str
    quality_profile: str
    baseline_value: float
    replay_value: float
    delta: float
    pct_delta: float
    group_values: dict[str, str]

    @property
    def abs_pct_delta(self) -> float:
        return abs(self.pct_delta) if math.isfinite(self.pct_delta) else math.nan


def normalize_fieldname(fieldname: str | None) -> str:
    return (fieldname or "").lstrip("\ufeff")


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


def first_value(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name, "")
        if value not in ("", None):
            return str(value)
    return ""


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


def pct_delta(value: float, baseline: float) -> float:
    if not math.isfinite(value) or not math.isfinite(baseline) or baseline == 0:
        return math.nan
    return (value - baseline) / abs(baseline) * 100.0


def is_status_ok(row: dict[str, str]) -> bool:
    status = first_value(row, "status").strip().lower()
    return not status or status == "ok"


def row_source_case_id(row: dict[str, str]) -> str:
    return first_value(row, *SOURCE_ID_COLUMNS)


def row_quality_profile(row: dict[str, str]) -> str:
    return first_value(row, *PROFILE_COLUMNS)


def parse_csv_list(text: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in text.split(",") if part.strip())
    if not values:
        raise argparse.ArgumentTypeError("value must contain at least one item")
    return values


def resolve_metrics(fieldnames: Iterable[str], metrics_text: str | None) -> tuple[str, ...]:
    available = set(fieldnames)
    if metrics_text:
        requested = parse_csv_list(metrics_text)
    else:
        requested = trainer.REQUESTED_OUTPUT_COLUMNS
    return trainer.resolve_output_columns(available, requested_columns=requested)[0]


def latest_rows_by_case_id(rows: Iterable[dict[str, str]]) -> dict[str, dict[str, str]]:
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        case_id = first_value(row, "case_id")
        if case_id:
            latest[case_id] = row
    return latest


def is_replay_row(row: dict[str, str], replay_profiles: tuple[str, ...]) -> bool:
    if not row_source_case_id(row) or not is_status_ok(row):
        return False
    profile = row_quality_profile(row)
    return not replay_profiles or profile in replay_profiles


def build_drift_records(
    rows: Iterable[dict[str, str]],
    metrics: tuple[str, ...],
    *,
    replay_profiles: tuple[str, ...] = ("mesh_time_fine",),
) -> tuple[list[DriftRecord], dict[str, int]]:
    row_list = list(rows)
    source_rows = latest_rows_by_case_id(row_list)
    records: list[DriftRecord] = []
    counters = {
        "rows": len(row_list),
        "replay_rows": 0,
        "matched_replay_rows": 0,
        "unmatched_replay_rows": 0,
        "records": 0,
    }

    for row in row_list:
        if not is_replay_row(row, replay_profiles):
            continue
        counters["replay_rows"] += 1
        source_case_id = row_source_case_id(row)
        source = source_rows.get(source_case_id)
        if source is None or not is_status_ok(source):
            counters["unmatched_replay_rows"] += 1
            continue
        matched_metric = False
        for metric in metrics:
            baseline_value = finite_float(source.get(metric, ""))
            replay_value = finite_float(row.get(metric, ""))
            delta_pct = pct_delta(replay_value, baseline_value)
            if not math.isfinite(delta_pct):
                continue
            matched_metric = True
            records.append(
                DriftRecord(
                    metric=metric,
                    source_case_id=source_case_id,
                    replay_case_id=first_value(row, "case_id"),
                    quality_profile=row_quality_profile(row),
                    baseline_value=baseline_value,
                    replay_value=replay_value,
                    delta=replay_value - baseline_value,
                    pct_delta=delta_pct,
                    group_values={column: first_value(row, column) for column in GROUP_COLUMNS},
                )
            )
        if matched_metric:
            counters["matched_replay_rows"] += 1
        else:
            counters["unmatched_replay_rows"] += 1
    counters["records"] = len(records)
    return records, counters


def mean(values: list[float]) -> float:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return math.nan
    return sum(finite) / len(finite)


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


def build_summary_rows(records: Iterable[DriftRecord], pct_threshold: float) -> list[dict[str, str]]:
    records_by_metric: dict[str, list[DriftRecord]] = {}
    for record in records:
        records_by_metric.setdefault(record.metric, []).append(record)

    rows: list[dict[str, str]] = []
    for metric in sorted(records_by_metric):
        metric_records = records_by_metric[metric]
        deltas = [record.delta for record in metric_records]
        abs_deltas = [abs(record.delta) for record in metric_records]
        pct_deltas = [record.pct_delta for record in metric_records]
        abs_pct_deltas = [record.abs_pct_delta for record in metric_records]
        over_threshold = [value for value in abs_pct_deltas if math.isfinite(value) and value > pct_threshold]
        rows.append(
            {
                "metric": metric,
                "compared_rows": str(len(metric_records)),
                "mean_delta": format_number(mean(deltas)),
                "mean_abs_delta": format_number(mean(abs_deltas)),
                "p90_abs_delta": format_number(quantile(abs_deltas, 0.90)),
                "max_abs_delta": format_number(max(abs_deltas) if abs_deltas else math.nan),
                "mean_pct_delta": format_number(mean(pct_deltas)),
                "mean_abs_pct_delta": format_number(mean(abs_pct_deltas)),
                "p50_abs_pct_delta": format_number(quantile(abs_pct_deltas, 0.50)),
                "p90_abs_pct_delta": format_number(quantile(abs_pct_deltas, 0.90)),
                "p95_abs_pct_delta": format_number(quantile(abs_pct_deltas, 0.95)),
                "max_abs_pct_delta": format_number(max(abs_pct_deltas) if abs_pct_deltas else math.nan),
                "over_threshold_rows": str(len(over_threshold)),
                "over_threshold_pct": format_number(len(over_threshold) / len(metric_records) * 100.0)
                if metric_records
                else "",
            }
        )
    return rows


def build_outlier_rows(
    records: Iterable[DriftRecord],
    *,
    pct_threshold: float,
    max_rows: int,
) -> list[dict[str, str]]:
    outliers = [
        record
        for record in records
        if math.isfinite(record.abs_pct_delta) and record.abs_pct_delta > pct_threshold
    ]
    outliers.sort(key=lambda record: (-record.abs_pct_delta, record.metric, record.source_case_id))
    rows: list[dict[str, str]] = []
    for record in outliers[:max_rows]:
        row = {
            "metric": record.metric,
            "source_case_id": record.source_case_id,
            "replay_case_id": record.replay_case_id,
            "quality_profile": record.quality_profile,
            "baseline_value": format_number(record.baseline_value),
            "replay_value": format_number(record.replay_value),
            "delta": format_number(record.delta),
            "pct_delta": format_number(record.pct_delta),
            "abs_pct_delta": format_number(record.abs_pct_delta),
        }
        row.update(record.group_values)
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare replayed IPMSM rows against source result rows.")
    parser.add_argument("--results", nargs="+", type=Path, required=True)
    parser.add_argument("--metrics", help="Comma-separated output metrics. Defaults to LightGBM training targets.")
    parser.add_argument("--replay-profiles", type=parse_csv_list, default=("mesh_time_fine",))
    parser.add_argument("--pct-threshold", type=float, default=10.0)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--outliers-output", type=Path)
    parser.add_argument("--max-outlier-rows", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rows, fieldnames = read_rows(args.results)
    try:
        metrics = resolve_metrics(fieldnames, args.metrics)
    except ValueError as exc:
        parser.error(str(exc))
    records, counters = build_drift_records(rows, metrics, replay_profiles=args.replay_profiles)
    summary_rows = build_summary_rows(records, args.pct_threshold)
    write_csv(args.summary_output, summary_rows, SUMMARY_FIELDNAMES)
    outlier_rows: list[dict[str, str]] = []
    if args.outliers_output:
        outlier_rows = build_outlier_rows(
            records,
            pct_threshold=args.pct_threshold,
            max_rows=max(args.max_outlier_rows, 0),
        )
        write_csv(args.outliers_output, outlier_rows, OUTLIER_FIELDNAMES)

    print(
        "replay_drift "
        + " ".join(f"{key}={counters[key]}" for key in ("rows", "replay_rows", "matched_replay_rows", "unmatched_replay_rows", "records"))
        + f" metrics={len(metrics)} threshold_pct={args.pct_threshold:g} outliers={len(outlier_rows)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
