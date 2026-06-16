"""Analyze numeric case-plan patterns for failed IPMSM replay rows."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
import statistics
from typing import Iterable

from select_ipmsm_replay_cases import compare_numeric, parse_exclusion_rule


SUMMARY_FIELDNAMES = (
    "feature",
    "score",
    "failed_min",
    "failed_median",
    "failed_max",
    "failed_mean",
    "ok_min",
    "ok_median",
    "ok_max",
    "ok_mean",
)
RULE_FIELDNAMES = ("rule", "matched_rows", "matched_failed_rows", "matched_ok_rows", "failed_coverage", "matched_failure_rate")


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


def parse_indexes(text: str) -> set[int]:
    indexes = {int(part.strip()) for part in text.split(",") if part.strip()}
    if not indexes:
        raise ValueError("at least one failed row index is required")
    if min(indexes) < 1:
        raise ValueError("failed row indexes are 1-based and must be >= 1")
    return indexes


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return [dict(row) for row in reader]


def numeric_feature_names(rows: list[dict[str, str]]) -> list[str]:
    features = []
    for name in rows[0] if rows else ():
        values = [finite_float(row.get(name, "")) for row in rows]
        if values and all(math.isfinite(value) for value in values) and len(set(values)) > 1:
            features.append(name)
    return features


def value_summary(rows: list[dict[str, str]], feature: str) -> tuple[float, float, float, float]:
    values = [finite_float(row.get(feature, "")) for row in rows]
    return min(values), statistics.median(values), max(values), statistics.mean(values)


def build_summary_rows(rows: list[dict[str, str]], failed_indexes: set[int]) -> list[dict[str, str]]:
    failed_rows = [row for index, row in enumerate(rows, start=1) if index in failed_indexes]
    ok_rows = [row for index, row in enumerate(rows, start=1) if index not in failed_indexes]
    if not failed_rows:
        raise ValueError("failed row indexes do not select any rows")
    if not ok_rows:
        raise ValueError("failed row indexes select every row; ok comparison set is empty")

    summaries = []
    for feature in numeric_feature_names(rows):
        failed_summary = value_summary(failed_rows, feature)
        ok_summary = value_summary(ok_rows, feature)
        all_values = [finite_float(row.get(feature, "")) for row in rows]
        span = max(all_values) - min(all_values)
        score = abs(failed_summary[3] - ok_summary[3]) / span if span else 0.0
        summaries.append(
            {
                "feature": feature,
                "score": format_number(score),
                "failed_min": format_number(failed_summary[0]),
                "failed_median": format_number(failed_summary[1]),
                "failed_max": format_number(failed_summary[2]),
                "failed_mean": format_number(failed_summary[3]),
                "ok_min": format_number(ok_summary[0]),
                "ok_median": format_number(ok_summary[1]),
                "ok_max": format_number(ok_summary[2]),
                "ok_mean": format_number(ok_summary[3]),
            }
        )
    return sorted(summaries, key=lambda row: float(row["score"] or 0.0), reverse=True)


def row_matches_rule(row: dict[str, str], rule_text: str) -> bool:
    rule = parse_exclusion_rule(rule_text)
    return all(compare_numeric(finite_float(row.get(feature, "")), operator, threshold) for feature, operator, threshold in rule)


def evaluate_rules(rows: list[dict[str, str]], failed_indexes: set[int], rules: Iterable[str]) -> list[dict[str, str]]:
    failed_count = len(failed_indexes)
    rule_texts = list(rules)
    results = []
    matched_by_rule: list[set[int]] = []
    for rule_text in rule_texts:
        matched_indexes = {index for index, row in enumerate(rows, start=1) if row_matches_rule(row, rule_text)}
        matched_by_rule.append(matched_indexes)
        matched_failed = len(matched_indexes & failed_indexes)
        matched_ok = len(matched_indexes - failed_indexes)
        matched_rows = len(matched_indexes)
        results.append(
            {
                "rule": rule_text,
                "matched_rows": str(matched_rows),
                "matched_failed_rows": str(matched_failed),
                "matched_ok_rows": str(matched_ok),
                "failed_coverage": format_number(matched_failed / failed_count if failed_count else math.nan),
                "matched_failure_rate": format_number(matched_failed / matched_rows if matched_rows else math.nan),
            }
        )
    if len(matched_by_rule) > 1:
        matched_indexes = set().union(*matched_by_rule)
        matched_failed = len(matched_indexes & failed_indexes)
        matched_ok = len(matched_indexes - failed_indexes)
        matched_rows = len(matched_indexes)
        results.append(
            {
                "rule": "__any__",
                "matched_rows": str(matched_rows),
                "matched_failed_rows": str(matched_failed),
                "matched_ok_rows": str(matched_ok),
                "failed_coverage": format_number(matched_failed / failed_count if failed_count else math.nan),
                "matched_failure_rate": format_number(matched_failed / matched_rows if matched_rows else math.nan),
            }
        )
    return results


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze numeric failure patterns in an IPMSM replay case CSV.")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--failed-row-indexes", required=True, help="Comma-separated 1-based failed row indexes in --cases.")
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--rule-output", type=Path)
    parser.add_argument("--evaluate-rule", action="append", default=[])
    parser.add_argument("--top", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        failed_indexes = parse_indexes(args.failed_row_indexes)
        rows = read_rows(args.cases)
        if max(failed_indexes) > len(rows):
            raise ValueError(f"failed row index {max(failed_indexes)} exceeds case row count {len(rows)}")
        summary_rows = build_summary_rows(rows, failed_indexes)
        rule_rows = evaluate_rules(rows, failed_indexes, args.evaluate_rule)
    except ValueError as exc:
        parser.error(str(exc))

    write_csv(args.summary_output, SUMMARY_FIELDNAMES, summary_rows)
    if args.rule_output:
        write_csv(args.rule_output, RULE_FIELDNAMES, rule_rows)

    preview = "; ".join(f"{row['feature']}={row['score']}" for row in summary_rows[: max(0, args.top)])
    print(
        "failure_patterns "
        f"rows={len(rows)} failed_rows={len(failed_indexes)} numeric_features={len(summary_rows)} "
        f"summary_output={args.summary_output} rule_output={args.rule_output or ''} top={preview}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
