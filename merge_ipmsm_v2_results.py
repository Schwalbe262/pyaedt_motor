"""Merge complete, non-overlapping IPMSM v2 result batches in case-plan order."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Iterable


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), [dict(row) for row in reader]


def unique_case_ids(rows: Iterable[dict[str, str]], *, source: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            raise ValueError(f"{source} row {index} has a blank case_id")
        if case_id in seen:
            raise ValueError(f"{source} contains duplicate case_id: {case_id}")
        seen.add(case_id)
        result.append(case_id)
    return result


def merge_complete_results(
    case_plan: Path,
    result_paths: Iterable[Path],
) -> tuple[list[str], list[dict[str, str]]]:
    _, plan_rows = read_csv(case_plan)
    plan_ids = unique_case_ids(plan_rows, source=str(case_plan))
    if not plan_ids:
        raise ValueError("case plan is empty")

    headers: list[str] = []
    results_by_case: dict[str, dict[str, str]] = {}
    for path in result_paths:
        fieldnames, rows = read_csv(path)
        for column in fieldnames:
            if column not in headers:
                headers.append(column)
        for row in rows:
            case_id = str(row.get("case_id") or "").strip()
            if not case_id:
                raise ValueError(f"{path} contains a blank case_id")
            if case_id in results_by_case:
                raise ValueError(f"result batches overlap at case_id: {case_id}")
            results_by_case[case_id] = row

    plan_set = set(plan_ids)
    extra = sorted(set(results_by_case) - plan_set)
    missing = [case_id for case_id in plan_ids if case_id not in results_by_case]
    failed = [
        case_id
        for case_id in plan_ids
        if case_id in results_by_case
        and str(results_by_case[case_id].get("status") or "").strip().lower() != "ok"
    ]
    failures: list[str] = []
    if extra:
        failures.append(f"extra={len(extra)} first={extra[:3]}")
    if missing:
        failures.append(f"missing={len(missing)} first={missing[:3]}")
    if failed:
        failures.append(f"non_ok={len(failed)} first={failed[:3]}")
    if failures:
        raise ValueError("result coverage is incomplete: " + "; ".join(failures))
    if "case_id" not in headers or "status" not in headers:
        raise ValueError("result CSVs must contain case_id and status")
    return headers, [results_by_case[case_id] for case_id in plan_ids]


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-plan", type=Path, required=True)
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    headers, rows = merge_complete_results(args.case_plan, args.input)
    write_csv(args.output, headers, rows)
    print(f"merged_ipmsm_v2_results rows={len(rows)} inputs={len(args.input)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
