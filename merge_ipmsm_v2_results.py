"""Merge complete, non-overlapping IPMSM v2 result batches in case-plan order."""

from __future__ import annotations

import argparse
import csv
import io
import os
from pathlib import Path
import tempfile
from typing import Iterable

from atomic_publish import publish_no_replace


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = list(reader.fieldnames or ())
        if not fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        if any(not str(column or "").strip() for column in fieldnames):
            raise ValueError(f"CSV has a blank header field: {path}")
        if len(fieldnames) != len(set(fieldnames)):
            raise ValueError(f"CSV has duplicate header fields: {path}")
        rows = [dict(row) for row in reader]
        if any(None in row for row in rows):
            raise ValueError(f"CSV has fields beyond its header: {path}")
        return fieldnames, rows


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
    case_plan: Path | Iterable[Path],
    result_paths: Iterable[Path],
) -> tuple[list[str], list[dict[str, str]]]:
    case_plans = [case_plan] if isinstance(case_plan, Path) else list(case_plan)
    if not case_plans:
        raise ValueError("at least one case plan is required")
    plan_ids: list[str] = []
    plan_source_by_id: dict[str, Path] = {}
    for path in case_plans:
        _, plan_rows = read_csv(path)
        current_ids = unique_case_ids(plan_rows, source=str(path))
        if not current_ids:
            raise ValueError(f"case plan is empty: {path}")
        overlap = [case_id for case_id in current_ids if case_id in plan_source_by_id]
        if overlap:
            first = overlap[0]
            raise ValueError(
                "case plans overlap at case_id: "
                f"{first} ({plan_source_by_id[first]} and {path})"
            )
        plan_ids.extend(current_ids)
        plan_source_by_id.update({case_id: path for case_id in current_ids})

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
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    payload = ("\ufeff" + stream.getvalue()).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            publish_no_replace(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(f"refusing to overwrite existing merged output: {path}") from exc
        except OSError as exc:
            if path.exists():
                raise FileExistsError(
                    f"refusing to overwrite existing merged output: {path}"
                ) from exc
            raise OSError(f"cannot atomically publish merged output {path}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case-plan",
        type=Path,
        action="append",
        required=True,
        help="Case plan to append in order; repeat for non-overlapping stages.",
    )
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    headers, rows = merge_complete_results(args.case_plan, args.input)
    write_csv(args.output, headers, rows)
    print(
        f"merged_ipmsm_v2_results rows={len(rows)} plans={len(args.case_plan)} "
        f"inputs={len(args.input)} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
