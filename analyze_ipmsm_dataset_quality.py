"""Summarize IPMSM simulation result CSV quality without loading full files."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import math
from pathlib import Path
from typing import Iterable


DEFAULT_REQUIRED_OUTPUTS = (
    "output_torque_all_avg_nm",
    "output_coreloss_all_avg_w",
    "output_solidloss_all_avg_w",
)
SUMMARY_FIELDNAMES = (
    "scope",
    "path",
    "rows",
    "unique_case_ids",
    "duplicate_case_ids",
    "status_ok",
    "status_failed",
    "status_other",
    "required_complete_rows",
    "missing_required_rows",
    "missing_required_by_column",
    "elapsed_min_s",
    "elapsed_avg_s",
    "elapsed_max_s",
    "top_error",
)


class RunningStats:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum = math.inf
        self.maximum = -math.inf

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    def min_text(self) -> str:
        return format_number(self.minimum) if self.count else ""

    def avg_text(self) -> str:
        return format_number(self.total / self.count) if self.count else ""

    def max_text(self) -> str:
        return format_number(self.maximum) if self.count else ""


class DatasetQualityAccumulator:
    def __init__(self, required_outputs: tuple[str, ...]) -> None:
        self.required_outputs = required_outputs
        self.rows = 0
        self.case_ids: Counter[str] = Counter()
        self.status_counts: Counter[str] = Counter()
        self.required_complete_rows = 0
        self.missing_required_rows = 0
        self.missing_required_by_column: Counter[str] = Counter()
        self.elapsed = RunningStats()
        self.errors: Counter[str] = Counter()

    def add_row(self, row: dict[str, str]) -> None:
        self.rows += 1
        case_id = row.get("case_id", "")
        if case_id:
            self.case_ids[case_id] += 1

        status = row.get("status", "").strip().lower() or "unknown"
        self.status_counts[status] += 1
        self.elapsed.add(finite_float(row.get("elapsed_s", "")))

        missing = [column for column in self.required_outputs if not math.isfinite(finite_float(row.get(column, "")))]
        if missing:
            self.missing_required_rows += 1
            self.missing_required_by_column.update(missing)
        else:
            self.required_complete_rows += 1

        error = normalize_error(row.get("error", ""))
        if error:
            self.errors[error] += 1

    def summary_row(self, scope: str, path: str) -> dict[str, str]:
        duplicates = sum(count - 1 for count in self.case_ids.values() if count > 1)
        status_ok = self.status_counts.get("ok", 0)
        status_failed = self.status_counts.get("failed", 0)
        status_other = self.rows - status_ok - status_failed
        return {
            "scope": scope,
            "path": path,
            "rows": str(self.rows),
            "unique_case_ids": str(len(self.case_ids)),
            "duplicate_case_ids": str(duplicates),
            "status_ok": str(status_ok),
            "status_failed": str(status_failed),
            "status_other": str(status_other),
            "required_complete_rows": str(self.required_complete_rows),
            "missing_required_rows": str(self.missing_required_rows),
            "missing_required_by_column": format_counter(self.missing_required_by_column),
            "elapsed_min_s": self.elapsed.min_text(),
            "elapsed_avg_s": self.elapsed.avg_text(),
            "elapsed_max_s": self.elapsed.max_text(),
            "top_error": top_counter_item(self.errors),
        }


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


def normalize_error(value: str) -> str:
    text = " ".join(str(value).split())
    if not text:
        return ""
    return text[:160]


def format_counter(counter: Counter[str]) -> str:
    return ";".join(f"{key}:{counter[key]}" for key in sorted(counter))


def top_counter_item(counter: Counter[str]) -> str:
    if not counter:
        return ""
    item, count = counter.most_common(1)[0]
    return f"{item} ({count})"


def parse_required_outputs(text: str) -> tuple[str, ...]:
    outputs = tuple(part.strip() for part in text.split(",") if part.strip())
    if not outputs:
        raise ValueError("at least one required output column is required")
    return outputs


def analyze_file(path: Path, required_outputs: tuple[str, ...]) -> DatasetQualityAccumulator:
    accumulator = DatasetQualityAccumulator(required_outputs)
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            accumulator.add_row(row)
    return accumulator


def merge_accumulators(accumulators: Iterable[DatasetQualityAccumulator], required_outputs: tuple[str, ...]) -> DatasetQualityAccumulator:
    merged = DatasetQualityAccumulator(required_outputs)
    for accumulator in accumulators:
        merged.rows += accumulator.rows
        merged.case_ids.update(accumulator.case_ids)
        merged.status_counts.update(accumulator.status_counts)
        merged.required_complete_rows += accumulator.required_complete_rows
        merged.missing_required_rows += accumulator.missing_required_rows
        merged.missing_required_by_column.update(accumulator.missing_required_by_column)
        merged.errors.update(accumulator.errors)
        merged.elapsed.count += accumulator.elapsed.count
        merged.elapsed.total += accumulator.elapsed.total
        merged.elapsed.minimum = min(merged.elapsed.minimum, accumulator.elapsed.minimum)
        merged.elapsed.maximum = max(merged.elapsed.maximum, accumulator.elapsed.maximum)
    return merged


def write_summary(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize IPMSM simulation result CSV data quality.")
    parser.add_argument("--results", nargs="+", type=Path, required=True, help="One or more run_ipmsm_batch.py result CSVs.")
    parser.add_argument("--output", type=Path, required=True, help="Compact quality summary CSV to write.")
    parser.add_argument("--required-outputs", default=",".join(DEFAULT_REQUIRED_OUTPUTS))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        required_outputs = parse_required_outputs(args.required_outputs)
    except ValueError as exc:
        parser.error(str(exc))

    rows: list[dict[str, str]] = []
    accumulators: list[DatasetQualityAccumulator] = []
    for result_path in args.results:
        accumulator = analyze_file(result_path, required_outputs)
        accumulators.append(accumulator)
        rows.append(accumulator.summary_row("file", str(result_path)))
    if len(accumulators) > 1:
        rows.append(merge_accumulators(accumulators, required_outputs).summary_row("combined", ""))
    write_summary(args.output, rows)

    combined = rows[-1]
    print(
        "dataset_quality "
        f"rows={combined['rows']} ok={combined['status_ok']} failed={combined['status_failed']} "
        f"required_complete={combined['required_complete_rows']} missing_required={combined['missing_required_rows']} "
        f"duplicates={combined['duplicate_case_ids']} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
