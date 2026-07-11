"""Expand fixed source geometries across second-pass quality profiles."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Iterable

from generate_ipmsm_quality_cases import MESH_ELEMENT_KEYS, QualityProfile, parse_profiles, safe_name_part


REQUIRED_COLUMNS = (
    "case_id",
    "source_case_id",
    "quality_profile",
    "transient_periods",
    "steps_per_period",
    *(f"mesh_{key}_elements" for key in MESH_ELEMENT_KEYS),
)


def source_id(row: dict[str, str]) -> str:
    return str(row.get("source_case_id") or row.get("input_source_case_id") or row.get("case_id") or "").strip()


def ordered_unique_sources(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        identifier = source_id(row)
        if not identifier:
            raise ValueError("source row is missing source_case_id/input_source_case_id/case_id")
        if identifier in seen:
            continue
        seen.add(identifier)
        selected.append(dict(row))
    return selected


def parse_source_case_ids(text: str) -> list[str]:
    identifiers = [part.strip() for part in text.split(",") if part.strip()]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("--source-case-ids must not contain duplicate IDs")
    return identifiers


def select_sources_by_ids(
    sources: Iterable[dict[str, str]],
    requested_ids: Iterable[str],
) -> list[dict[str, str]]:
    source_by_id = {source_id(row): dict(row) for row in sources}
    requested = list(requested_ids)
    missing = [identifier for identifier in requested if identifier not in source_by_id]
    if missing:
        raise ValueError(f"source case IDs not found: {','.join(missing)}")
    return [source_by_id[identifier] for identifier in requested]


def load_source_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return ordered_unique_sources(dict(row) for row in reader)


def merged_fieldnames(source_fieldnames: Iterable[str] | None) -> list[str]:
    fieldnames = list(source_fieldnames or [])
    for column in REQUIRED_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)
    return fieldnames


def expand_rows(
    sources: Iterable[dict[str, str]],
    profiles: Iterable[QualityProfile],
    case_prefix: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source_index, source in enumerate(sources, start=1):
        identifier = source_id(source)
        source_name = safe_name_part(identifier)
        for profile in profiles:
            row = dict(source)
            row["case_id"] = f"{safe_name_part(case_prefix)}_{source_index:04d}_{source_name}_{profile.name}"
            row["source_case_id"] = identifier
            row["quality_profile"] = profile.name
            row["transient_periods"] = str(profile.transient_periods)
            row["steps_per_period"] = str(profile.steps_per_period)
            for key in MESH_ELEMENT_KEYS:
                row[f"mesh_{key}_elements"] = str(profile.mesh_elements[key])
            rows.append(row)
    return rows


def source_fieldnames(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return list(reader.fieldnames or [])


def write_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate second-pass profile cases from fixed source geometry rows.")
    parser.add_argument("--source-cases", type=Path, required=True, help="Existing expanded case CSV containing source rows.")
    parser.add_argument("--output", type=Path, required=True, help="CSV path for generated second-pass rows.")
    parser.add_argument("--profiles", required=True, help="Comma-separated second-pass profile names.")
    parser.add_argument(
        "--source-case-ids",
        default="",
        help="Optional comma-separated source IDs to select in exactly the supplied order.",
    )
    parser.add_argument("--case-prefix", default="secondpass")
    parser.add_argument("--max-cases", type=int, default=200)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        profiles = parse_profiles(args.profiles)
        sources = load_source_rows(args.source_cases)
        requested_ids = parse_source_case_ids(args.source_case_ids)
        if requested_ids:
            sources = select_sources_by_ids(sources, requested_ids)
        rows = expand_rows(sources, profiles, args.case_prefix)
        if len(rows) > args.max_cases:
            parser.error(f"generated {len(rows)} rows, exceeding --max-cases={args.max_cases}")
        write_rows(args.output, rows, merged_fieldnames(source_fieldnames(args.source_cases)))
    except ValueError as exc:
        parser.error(str(exc))
    print(
        "second_pass_cases "
        f"rows={len(rows)} source_cases={len(sources)} profiles={','.join(profile.name for profile in profiles)} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
