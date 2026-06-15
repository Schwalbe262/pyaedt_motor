"""Filter IPMSM result CSVs into a training-ready dataset with audit metrics."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Iterable

import train_ipmsm_lightgbm as trainer


SUMMARY_FIELDNAMES = (
    "rows_read",
    "rows_after_dedup",
    "duplicate_case_id_rows",
    "kept_rows",
    "rejected_rows",
    "status_rejected_rows",
    "nonfinite_input_rows",
    "nonfinite_output_rows",
)


def finite_float(value: object) -> float:
    try:
        number = float(str(value).strip())
    except Exception:
        return math.nan
    return number if math.isfinite(number) else math.nan


def is_finite_row(row: dict[str, str], columns: Iterable[str]) -> bool:
    return all(math.isfinite(finite_float(row.get(column, ""))) for column in columns)


def is_status_ok(row: dict[str, str], has_status_column: bool) -> bool:
    if not has_status_column:
        return True
    return str(row.get("status", "")).strip().lower() == "ok"


def read_rows(paths: Iterable[Path]) -> tuple[list[dict[str, str]], list[str]]:
    rows: list[dict[str, str]] = []
    fieldnames: list[str] = []
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            for fieldname in reader.fieldnames or ():
                if fieldname not in fieldnames:
                    fieldnames.append(fieldname)
            rows.extend(dict(row) for row in reader)
    return rows, fieldnames


def drop_duplicate_case_ids(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    if not any("case_id" in row for row in rows):
        return rows, 0
    latest_by_case_id: dict[str, int] = {}
    keep = [True] * len(rows)
    duplicates = 0
    for index, row in enumerate(rows):
        case_id = row.get("case_id", "")
        if not case_id:
            continue
        previous = latest_by_case_id.get(case_id)
        if previous is not None:
            keep[previous] = False
            duplicates += 1
        latest_by_case_id[case_id] = index
    return [row for row, should_keep in zip(rows, keep) if should_keep], duplicates


def filter_training_rows(
    rows: list[dict[str, str]],
    fieldnames: Iterable[str],
    *,
    drop_duplicate_case_id: bool = True,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    available_columns = set(fieldnames)
    output_columns, _ = trainer.resolve_output_columns(available_columns)
    missing_inputs = trainer.missing_columns(available_columns, trainer.RAW_INPUT_COLUMNS)
    if missing_inputs:
        raise ValueError(f"missing input columns: {missing_inputs}")

    rows_after_dedup = rows
    duplicate_case_id_rows = 0
    if drop_duplicate_case_id:
        rows_after_dedup, duplicate_case_id_rows = drop_duplicate_case_ids(rows)

    has_status_column = "status" in available_columns
    kept_rows: list[dict[str, str]] = []
    status_rejected_rows = 0
    nonfinite_input_rows = 0
    nonfinite_output_rows = 0
    for row in rows_after_dedup:
        status_ok = is_status_ok(row, has_status_column)
        finite_inputs = is_finite_row(row, trainer.RAW_INPUT_COLUMNS)
        finite_outputs = is_finite_row(row, output_columns)
        if not status_ok:
            status_rejected_rows += 1
        if not finite_inputs:
            nonfinite_input_rows += 1
        if not finite_outputs:
            nonfinite_output_rows += 1
        if status_ok and finite_inputs and finite_outputs:
            kept_rows.append(row)

    summary = {
        "rows_read": len(rows),
        "rows_after_dedup": len(rows_after_dedup),
        "duplicate_case_id_rows": duplicate_case_id_rows,
        "kept_rows": len(kept_rows),
        "rejected_rows": len(rows_after_dedup) - len(kept_rows),
        "status_rejected_rows": status_rejected_rows,
        "nonfinite_input_rows": nonfinite_input_rows,
        "nonfinite_output_rows": nonfinite_output_rows,
    }
    return kept_rows, summary


def write_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path: Path, summary: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        writer.writerow({key: str(summary[key]) for key in SUMMARY_FIELDNAMES})


def filter_failures(
    summary: dict[str, int],
    *,
    min_kept_rows: int,
    max_rejected_rows: int | None,
    max_duplicate_case_id_rows: int | None,
) -> list[str]:
    failures: list[str] = []
    if summary["kept_rows"] < min_kept_rows:
        failures.append(f"kept_rows {summary['kept_rows']} < {min_kept_rows}")
    if max_rejected_rows is not None and summary["rejected_rows"] > max_rejected_rows:
        failures.append(f"rejected_rows {summary['rejected_rows']} > {max_rejected_rows}")
    if max_duplicate_case_id_rows is not None and summary["duplicate_case_id_rows"] > max_duplicate_case_id_rows:
        failures.append(
            f"duplicate_case_id_rows {summary['duplicate_case_id_rows']} > {max_duplicate_case_id_rows}"
        )
    return failures


def nonnegative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Filter IPMSM result CSVs into a training-ready dataset.")
    parser.add_argument("--results", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, help="Optional one-row filter audit CSV.")
    parser.add_argument("--keep-duplicate-case-id", dest="drop_duplicate_case_id", action="store_false")
    parser.set_defaults(drop_duplicate_case_id=True)
    parser.add_argument("--min-kept-rows", type=nonnegative_int, default=1)
    parser.add_argument("--max-rejected-rows", type=nonnegative_int)
    parser.add_argument("--max-duplicate-case-id-rows", type=nonnegative_int)
    parser.add_argument("--fail-on-filter", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rows, fieldnames = read_rows(args.results)
    try:
        kept_rows, summary = filter_training_rows(
            rows,
            fieldnames,
            drop_duplicate_case_id=args.drop_duplicate_case_id,
        )
    except ValueError as exc:
        parser.error(str(exc))
    write_rows(args.output, kept_rows, fieldnames)
    if args.summary_output:
        write_summary(args.summary_output, summary)

    print(
        "training_dataset_filter "
        + " ".join(f"{key}={summary[key]}" for key in SUMMARY_FIELDNAMES)
        + f" output={args.output}"
    )
    failures = filter_failures(
        summary,
        min_kept_rows=args.min_kept_rows,
        max_rejected_rows=args.max_rejected_rows,
        max_duplicate_case_id_rows=args.max_duplicate_case_id_rows,
    )
    if failures:
        print("training_dataset_filter failed " + "; ".join(failures))
    elif args.fail_on_filter:
        print("training_dataset_filter passed")
    return 1 if args.fail_on_filter and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
