"""Freeze an untouched IPMSM test cohort before its results are evaluated."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import uuid

from atomic_publish import cleanup_publish_receipt, publish_no_replace


SCHEMA_VERSION = "ipmsm-v2-untouched-test-plan-v1"
MANIFEST_SCHEMA_VERSION = "ipmsm-v2-untouched-test-plan-manifest-v1"


class UntouchedPlanError(RuntimeError):
    """The untouched confirmation cohort cannot be proven."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv_document(
    path: Path,
    *,
    maximum_rows: int = 100_000,
) -> tuple[bytes, list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise UntouchedPlanError(f"case plan is missing: {path}")
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8-sig")
        with io.StringIO(text, newline="") as stream:
            reader = csv.DictReader(stream)
            headers = list(reader.fieldnames or ())
            if not headers or len(headers) != len(set(headers)):
                raise UntouchedPlanError(f"case plan has a missing or duplicate header: {path}")
            rows: list[dict[str, str]] = []
            for index, row in enumerate(reader, start=1):
                if index > maximum_rows:
                    raise UntouchedPlanError(f"case plan exceeds {maximum_rows} rows: {path}")
                if None in row:
                    raise UntouchedPlanError(f"case plan row exceeds its header: {path}:{index}")
                rows.append(dict(row))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise UntouchedPlanError(f"cannot read case plan: {path}") from exc
    if not rows:
        raise UntouchedPlanError(f"case plan is empty: {path}")
    return raw_bytes, headers, rows


def read_csv_rows(path: Path, *, maximum_rows: int = 100_000) -> tuple[list[str], list[dict[str, str]]]:
    _, headers, rows = read_csv_document(path, maximum_rows=maximum_rows)
    return headers, rows


def normalized_text(value: object, *, field: str, row_number: int) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        raise UntouchedPlanError(f"row {row_number} has a blank {field}")
    return text


def validate_plan(
    headers: Sequence[str],
    rows: Sequence[Mapping[str, str]],
    *,
    geometry_column: str,
) -> dict[str, Any]:
    required = {"case_id", "doe_split", geometry_column}
    missing = sorted(required - set(headers))
    if missing:
        raise UntouchedPlanError(f"case plan is missing columns: {missing}")
    seen_cases: set[str] = set()
    group_splits: dict[str, str] = {}
    case_identity: dict[str, tuple[str, str]] = {}
    ordered_groups: list[str] = []
    for index, row in enumerate(rows, start=1):
        case_id = normalized_text(row.get("case_id"), field="case_id", row_number=index)
        group_id = normalized_text(row.get(geometry_column), field=geometry_column, row_number=index)
        split = normalized_text(row.get("doe_split"), field="doe_split", row_number=index).lower()
        if split not in {"train", "calibration", "test"}:
            raise UntouchedPlanError(f"row {index} has invalid doe_split: {split!r}")
        if case_id in seen_cases:
            raise UntouchedPlanError(f"duplicate case_id: {case_id}")
        previous = group_splits.setdefault(group_id, split)
        if previous != split:
            raise UntouchedPlanError(f"geometry group crosses split partitions: {group_id}")
        if group_id not in ordered_groups:
            ordered_groups.append(group_id)
        seen_cases.add(case_id)
        case_identity[case_id] = (group_id, split)
    return {
        "case_identity": case_identity,
        "group_splits": group_splits,
        "ordered_groups": tuple(ordered_groups),
        "test_groups": frozenset(group for group, split in group_splits.items() if split == "test"),
    }


def select_untouched_test_rows(
    full_headers: Sequence[str],
    full_rows: Sequence[Mapping[str, str]],
    explored_headers: Sequence[str],
    explored_rows: Sequence[Mapping[str, str]],
    *,
    geometry_column: str,
    expected_untouched_groups: int,
    expected_rows_per_group: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if (
        isinstance(expected_untouched_groups, bool)
        or not isinstance(expected_untouched_groups, int)
        or expected_untouched_groups < 1
    ):
        raise ValueError("expected_untouched_groups must be an integer >= 1")
    if (
        isinstance(expected_rows_per_group, bool)
        or not isinstance(expected_rows_per_group, int)
        or expected_rows_per_group < 1
    ):
        raise ValueError("expected_rows_per_group must be an integer >= 1")
    full = validate_plan(full_headers, full_rows, geometry_column=geometry_column)
    explored = validate_plan(explored_headers, explored_rows, geometry_column=geometry_column)
    full_test = set(full["test_groups"])
    explored_test = set(explored["test_groups"])
    if not explored_test:
        raise UntouchedPlanError("explored plan has no test geometry")
    missing_groups = sorted(explored_test - full_test)
    if missing_groups:
        raise UntouchedPlanError(f"explored test geometry is absent from the full plan: {missing_groups[:3]}")
    for case_id, identity in explored["case_identity"].items():
        if identity[1] != "test":
            continue
        if full["case_identity"].get(case_id) != identity:
            raise UntouchedPlanError(f"explored test case identity differs from the full plan: {case_id}")
    untouched_groups = full_test - explored_test
    if not untouched_groups:
        raise UntouchedPlanError("no untouched test geometry remains")
    if len(untouched_groups) != expected_untouched_groups:
        raise UntouchedPlanError(
            "untouched test geometry count differs from the declared contract: "
            f"expected={expected_untouched_groups} actual={len(untouched_groups)}"
        )
    selected = [
        dict(row)
        for row in full_rows
        if str(row.get(geometry_column) or "").strip() in untouched_groups
    ]
    if not selected or any(str(row.get("doe_split") or "").strip().lower() != "test" for row in selected):
        raise UntouchedPlanError("untouched selection contains a non-test row")
    selected_groups = {
        str(row.get(geometry_column) or "").strip()
        for row in selected
    }
    if selected_groups != untouched_groups:
        raise UntouchedPlanError("untouched selection does not cover every remaining test group")
    rows_by_group = {
        group: sum(str(row.get(geometry_column) or "").strip() == group for row in selected)
        for group in sorted(untouched_groups)
    }
    wrong_group_rows = {
        group: count
        for group, count in rows_by_group.items()
        if count != expected_rows_per_group
    }
    if wrong_group_rows:
        raise UntouchedPlanError(
            "untouched test geometry row count differs from the declared contract: "
            + str(wrong_group_rows)
        )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "full_test_groups": len(full_test),
        "explored_test_groups": len(explored_test),
        "untouched_test_groups": len(untouched_groups),
        "untouched_test_rows": len(selected),
        "expected_untouched_groups": expected_untouched_groups,
        "expected_rows_per_group": expected_rows_per_group,
        "geometry_column": geometry_column,
        "untouched_group_ids_sha256": hashlib.sha256(
            "".join(f"{group}\n" for group in sorted(untouched_groups)).encode("utf-8")
        ).hexdigest(),
    }
    return selected, summary


def encode_csv(headers: Sequence[str], rows: Iterable[Mapping[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(headers), extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def publish_no_replace_bytes(path: Path, payload: bytes) -> None:
    if path.exists():
        raise UntouchedPlanError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.parent / f".{path.name}.{uuid.uuid4().hex}.staging"
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        receipt = publish_no_replace(staged, path)
        cleanup_publish_receipt(receipt)
    except BaseException:
        try:
            staged.unlink()
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze test geometry not used by an explored IPMSM plan.")
    parser.add_argument("--full-plan", type=Path, required=True)
    parser.add_argument("--explored-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument("--geometry-column", default="geometry_group_id")
    parser.add_argument("--expected-untouched-groups", type=int, required=True)
    parser.add_argument("--expected-rows-per-group", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = args.output.resolve()
    manifest_output = (
        args.manifest_output.resolve()
        if args.manifest_output
        else output.with_name(f"{output.stem}.manifest.json")
    )
    if output == manifest_output:
        parser.error("output and manifest-output must differ")
    if output.exists() or manifest_output.exists():
        parser.error("output and manifest-output must both be fresh paths")
    try:
        full_bytes, full_headers, full_rows = read_csv_document(args.full_plan)
        explored_bytes, explored_headers, explored_rows = read_csv_document(args.explored_plan)
        selected, summary = select_untouched_test_rows(
            full_headers,
            full_rows,
            explored_headers,
            explored_rows,
            geometry_column=args.geometry_column,
            expected_untouched_groups=args.expected_untouched_groups,
            expected_rows_per_group=args.expected_rows_per_group,
        )
        payload = encode_csv(full_headers, selected)
        output_sha256 = hashlib.sha256(payload).hexdigest()
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "status": "output_expected",
            "full_plan": str(args.full_plan.resolve()),
            "full_plan_sha256": hashlib.sha256(full_bytes).hexdigest(),
            "explored_plan": str(args.explored_plan.resolve()),
            "explored_plan_sha256": hashlib.sha256(explored_bytes).hexdigest(),
            "output": str(output),
            "output_sha256": output_sha256,
            "script_sha256": file_sha256(Path(__file__).resolve()),
            "counts": summary,
        }
        manifest_payload = (
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        publish_no_replace_bytes(manifest_output, manifest_payload)
        publish_no_replace_bytes(output, payload)
    except (UntouchedPlanError, OSError, ValueError) as exc:
        parser.error(str(exc))
    summary.update(
        {
            "full_plan_sha256": manifest["full_plan_sha256"],
            "explored_plan_sha256": manifest["explored_plan_sha256"],
            "output": str(output),
            "output_sha256": file_sha256(output),
            "manifest_output": str(manifest_output),
            "manifest_sha256": file_sha256(manifest_output),
            "script_sha256": file_sha256(Path(__file__).resolve()),
        }
    )
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
