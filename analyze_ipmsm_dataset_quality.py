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
EFFICIENCY_COLUMNS = ("output_efficiency_last_pct", "output_efficiency_last_pc", "output_efficiency_all_pct")
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
    "physical_sanity_violation_rows",
    "physical_sanity_violations_by_column",
    "elapsed_min_s",
    "elapsed_avg_s",
    "elapsed_max_s",
    "top_error",
)


def normalize_fieldname(fieldname: str | None) -> str:
    return (fieldname or "").lstrip("\ufeff")


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
        self.physical_sanity_violation_rows = 0
        self.physical_sanity_violations_by_column: Counter[str] = Counter()
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

        physical_violations = physical_sanity_violations(row)
        if physical_violations:
            self.physical_sanity_violation_rows += 1
            self.physical_sanity_violations_by_column.update(physical_violations)

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
            "physical_sanity_violation_rows": str(self.physical_sanity_violation_rows),
            "physical_sanity_violations_by_column": format_counter(self.physical_sanity_violations_by_column),
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


def physical_sanity_violations(row: dict[str, str]) -> list[str]:
    violations = []
    for column in EFFICIENCY_COLUMNS:
        if column not in row:
            continue
        value = finite_float(row.get(column, ""))
        if math.isfinite(value) and not 0.0 <= value <= 100.0:
            violations.append(column)
    return violations


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


def nonnegative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return value


def int_field(row: dict[str, str], key: str) -> int:
    try:
        return int(row.get(key, "0"))
    except ValueError:
        return 0


def quality_gate_failures(
    summary_row: dict[str, str],
    *,
    min_required_complete_rows: int = 0,
    max_missing_required_rows: int | None = None,
    max_duplicate_case_ids: int | None = None,
    max_failed_rows: int | None = None,
    max_physical_sanity_violation_rows: int | None = None,
) -> list[str]:
    failures: list[str] = []
    required_complete_rows = int_field(summary_row, "required_complete_rows")
    missing_required_rows = int_field(summary_row, "missing_required_rows")
    duplicate_case_ids = int_field(summary_row, "duplicate_case_ids")
    failed_rows = int_field(summary_row, "status_failed")
    physical_sanity_violation_rows = int_field(summary_row, "physical_sanity_violation_rows")

    if required_complete_rows < min_required_complete_rows:
        failures.append(f"required_complete_rows {required_complete_rows} < {min_required_complete_rows}")
    if max_missing_required_rows is not None and missing_required_rows > max_missing_required_rows:
        failures.append(f"missing_required_rows {missing_required_rows} > {max_missing_required_rows}")
    if max_duplicate_case_ids is not None and duplicate_case_ids > max_duplicate_case_ids:
        failures.append(f"duplicate_case_ids {duplicate_case_ids} > {max_duplicate_case_ids}")
    if max_failed_rows is not None and failed_rows > max_failed_rows:
        failures.append(f"status_failed {failed_rows} > {max_failed_rows}")
    if (
        max_physical_sanity_violation_rows is not None
        and physical_sanity_violation_rows > max_physical_sanity_violation_rows
    ):
        failures.append(
            f"physical_sanity_violation_rows {physical_sanity_violation_rows} > {max_physical_sanity_violation_rows}"
        )
    return failures


def analyze_file(path: Path, required_outputs: tuple[str, ...]) -> DatasetQualityAccumulator:
    accumulator = DatasetQualityAccumulator(required_outputs)
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        reader.fieldnames = [normalize_fieldname(fieldname) for fieldname in reader.fieldnames or ()]
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
        merged.physical_sanity_violation_rows += accumulator.physical_sanity_violation_rows
        merged.physical_sanity_violations_by_column.update(accumulator.physical_sanity_violations_by_column)
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
    parser.add_argument("--min-required-complete-rows", type=nonnegative_int, default=0)
    parser.add_argument("--max-missing-required-rows", type=nonnegative_int)
    parser.add_argument("--max-duplicate-case-ids", type=nonnegative_int)
    parser.add_argument("--max-failed-rows", type=nonnegative_int)
    parser.add_argument("--max-physical-sanity-violation-rows", type=nonnegative_int)
    parser.add_argument("--fail-on-quality", action="store_true", help="Return exit code 1 when any quality gate fails.")
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
        f"physical_sanity_violations={combined['physical_sanity_violation_rows']} "
        f"duplicates={combined['duplicate_case_ids']} output={args.output}"
    )
    failures = quality_gate_failures(
        combined,
        min_required_complete_rows=args.min_required_complete_rows,
        max_missing_required_rows=args.max_missing_required_rows,
        max_duplicate_case_ids=args.max_duplicate_case_ids,
        max_failed_rows=args.max_failed_rows,
        max_physical_sanity_violation_rows=args.max_physical_sanity_violation_rows,
    )
    if failures:
        print("quality_gate failed " + "; ".join(failures))
    elif args.fail_on_quality:
        print("quality_gate passed")
    return 1 if args.fail_on_quality and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
