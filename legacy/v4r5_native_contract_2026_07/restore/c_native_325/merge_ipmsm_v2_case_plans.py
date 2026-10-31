"""Merge non-overlapping IPMSM v2 case plans in source and row order."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from atomic_publish import PublishReceipt, publish_no_replace, rollback_owned_output


SCHEMA_VERSION = "ipmsm-v2-case-plan-merge-v1"


@dataclass(frozen=True)
class CasePlan:
    path: Path
    sha256: str
    headers: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    design_hashes: frozenset[str]


@dataclass(frozen=True)
class MergedCasePlan:
    sources: tuple[CasePlan, ...]
    headers: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    design_hashes: frozenset[str]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _resolved(path: Path) -> str:
    return str(path.resolve(strict=False))


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def read_case_plan(path: Path) -> CasePlan:
    if not path.is_file():
        raise ValueError(f"case plan is missing: {path}")
    payload = path.read_bytes()
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"case plan is not UTF-8: {path}") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = list(reader.fieldnames or ())
    if not headers:
        raise ValueError(f"case plan has no header: {path}")
    if any(not str(header or "").strip() for header in headers):
        raise ValueError(f"case plan has a blank header field: {path}")
    if len(headers) != len(set(headers)):
        raise ValueError(f"case plan has duplicate header fields: {path}")
    missing = sorted({"case_id", "design_hash"} - set(headers))
    if missing:
        raise ValueError(f"case plan is missing required columns {missing}: {path}")

    rows: list[dict[str, str]] = []
    case_ids: set[str] = set()
    design_hashes: set[str] = set()
    for row_number, raw in enumerate(reader, start=1):
        if None in raw or any(value is None for value in raw.values()):
            raise ValueError(f"case plan row {row_number} does not match its header: {path}")
        row = dict(raw)
        case_id = str(row.get("case_id") or "").strip()
        design_hash = str(row.get("design_hash") or "").strip()
        if not case_id:
            raise ValueError(f"case plan row {row_number} has a blank case_id: {path}")
        if not design_hash:
            raise ValueError(f"case plan row {row_number} has a blank design_hash: {path}")
        if case_id in case_ids:
            raise ValueError(f"case plan contains duplicate case_id={case_id!r}: {path}")
        case_ids.add(case_id)
        design_hashes.add(design_hash)
        rows.append(row)
    if not rows:
        raise ValueError(f"case plan is empty: {path}")
    return CasePlan(
        path=path,
        sha256=_sha256(payload),
        headers=tuple(headers),
        rows=tuple(rows),
        design_hashes=frozenset(design_hashes),
    )


def merge_case_plans(paths: Iterable[Path]) -> MergedCasePlan:
    plan_paths = list(paths)
    if not plan_paths:
        raise ValueError("at least one --case-plan is required")
    sources: list[CasePlan] = []
    expected_headers: tuple[str, ...] | None = None
    rows: list[dict[str, str]] = []
    case_sources: dict[str, Path] = {}
    design_sources: dict[str, Path] = {}
    all_design_hashes: set[str] = set()
    for path in plan_paths:
        plan = read_case_plan(path)
        if expected_headers is None:
            expected_headers = plan.headers
        elif plan.headers != expected_headers:
            raise ValueError(f"case plan headers differ from the first plan: {path}")
        for row in plan.rows:
            case_id = str(row["case_id"]).strip()
            previous = case_sources.get(case_id)
            if previous is not None:
                raise ValueError(
                    f"case plans overlap at case_id={case_id!r}: {previous} and {path}"
                )
            case_sources[case_id] = path
        overlap = sorted(plan.design_hashes & all_design_hashes)
        if overlap:
            design_hash = overlap[0]
            raise ValueError(
                "case plans overlap at design_hash="
                f"{design_hash!r}: {design_sources[design_hash]} and {path}"
            )
        for design_hash in plan.design_hashes:
            design_sources[design_hash] = path
        all_design_hashes.update(plan.design_hashes)
        rows.extend(plan.rows)
        sources.append(plan)
    assert expected_headers is not None
    return MergedCasePlan(
        sources=tuple(sources),
        headers=expected_headers,
        rows=tuple(rows),
        design_hashes=frozenset(all_design_hashes),
    )


def render_case_plan(plan: MergedCasePlan) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(plan.headers), extrasaction="raise")
    writer.writeheader()
    writer.writerows(plan.rows)
    return stream.getvalue().encode("utf-8-sig")


def build_manifest(
    plan: MergedCasePlan,
    payload: bytes,
    *,
    output: Path,
    manifest_output: Path,
    execute: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "execute" if execute else "dry-run",
        "source_case_plans": [
            {
                "path": _resolved(source.path),
                "sha256": source.sha256,
                "rows": len(source.rows),
                "design_hashes": len(source.design_hashes),
            }
            for source in plan.sources
        ],
        "output": {
            "path": _resolved(output),
            "sha256": _sha256(payload),
            "rows": len(plan.rows),
            "design_hashes": len(plan.design_hashes),
        },
        "manifest_output": _resolved(manifest_output),
        "header": {
            "columns": list(plan.headers),
            "sha256": _sha256(
                json.dumps(
                    list(plan.headers),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        },
        "counts": {
            "case_plans": len(plan.sources),
            "rows": len(plan.rows),
            "case_ids": len(plan.rows),
            "design_hashes": len(plan.design_hashes),
        },
    }


def require_fresh_pair(output: Path, manifest_output: Path) -> None:
    if output.resolve(strict=False) == manifest_output.resolve(strict=False):
        raise ValueError("--output and --manifest-output must be distinct")
    for path in (output, manifest_output):
        if _path_exists(path):
            raise FileExistsError(f"refusing to overwrite existing output: {path}")


def _stage_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def publish_pair(
    output: Path,
    payload: bytes,
    manifest_output: Path,
    manifest: dict[str, Any],
) -> None:
    require_fresh_pair(output, manifest_output)
    manifest_payload = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    output_stage: Path | None = None
    manifest_stage: Path | None = None
    manifest_receipt: PublishReceipt | None = None
    try:
        output_stage = _stage_bytes(output, payload)
        manifest_stage = _stage_bytes(manifest_output, manifest_payload)
        manifest_receipt = publish_no_replace(manifest_stage, manifest_output)
        publish_no_replace(output_stage, output)
    except BaseException as exc:
        if manifest_receipt is not None and not rollback_owned_output(manifest_receipt):
            raise RuntimeError("case-plan pair publication failed and manifest rollback was unsafe") from exc
        raise
    finally:
        if output_stage is not None:
            output_stage.unlink(missing_ok=True)
        if manifest_stage is not None:
            manifest_stage.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-plan", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Publish the fresh output pair.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    require_fresh_pair(args.output, args.manifest_output)
    plan = merge_case_plans(args.case_plan)
    payload = render_case_plan(plan)
    manifest = build_manifest(
        plan,
        payload,
        output=args.output,
        manifest_output=args.manifest_output,
        execute=args.execute,
    )
    if args.execute:
        publish_pair(args.output, payload, args.manifest_output, manifest)
    print(json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
