"""Generate the fixed two-case post-affinity replay pilot plan.

The source is the already-audited paired-24 plan.  The pilot deliberately
replays one source geometry under each of its two quality profiles and changes
only ``case_id`` so that scheduler and result identities cannot alias the
pre-fix campaign.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from typing import Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
SOURCE_PLAN = ROOT / "simul_log_smoke/profile_thirdpass_speed_v2s1_paired24_cases_v1.csv"
SOURCE_PLAN_SHA256 = "56d0c097e0a755baaaf96934b2c533d79eaab0230d10f5fd28c99a38ca82ec81"
SOURCE_ROW_INDICES = (1, 13)
EXPECTED_PROFILES = (
    "time_138_p12_baseline",
    "time_135_p12_iron525",
)
SOURCE_CASE_ID_PREFIX = "v2s1_thirdpass_speed_v1_"
NEW_CASE_ID_PREFIX = "v2s1_affinityfix_replay_v1_"
DEFAULT_OUTPUT = ROOT / "simul_log_smoke/profile_affinityfix_replay2_cases_v1.csv"
SAFE_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise RuntimeError(f"CSV has no header: {path}")
        fieldnames = list(reader.fieldnames)
        if len(fieldnames) != len(set(fieldnames)):
            raise RuntimeError(f"CSV has duplicate header columns: {path}")
        rows = [dict(row) for row in reader]
    return fieldnames, rows


def _assert_only_case_id_changed(
    source: Mapping[str, str],
    replay: Mapping[str, str],
) -> None:
    source_other = {key: value for key, value in source.items() if key != "case_id"}
    replay_other = {key: value for key, value in replay.items() if key != "case_id"}
    if source_other != replay_other:
        changed = sorted(
            key
            for key in set(source_other) | set(replay_other)
            if source_other.get(key) != replay_other.get(key)
        )
        raise RuntimeError(
            "affinity replay mutated non-case_id fields: " + ", ".join(changed)
        )


def build_replay_rows(
    fieldnames: Sequence[str],
    source_rows: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    required = {"case_id", "quality_profile", "source_case_id"}
    missing = sorted(required - set(fieldnames))
    if missing:
        raise RuntimeError("source plan is missing required columns: " + ", ".join(missing))
    if len(source_rows) < max(SOURCE_ROW_INDICES):
        raise RuntimeError(
            f"source plan has {len(source_rows)} rows; row {max(SOURCE_ROW_INDICES)} is required"
        )

    selected = [source_rows[index - 1] for index in SOURCE_ROW_INDICES]
    actual_profiles = tuple(str(row.get("quality_profile") or "") for row in selected)
    if actual_profiles != EXPECTED_PROFILES:
        raise RuntimeError(
            "fixed source rows no longer contain the expected profiles: "
            f"expected={EXPECTED_PROFILES!r} actual={actual_profiles!r}"
        )
    source_ids = {str(row.get("source_case_id") or "") for row in selected}
    if len(source_ids) != 1 or "" in source_ids:
        raise RuntimeError(
            "fixed source rows do not share one nonblank source_case_id: "
            f"{sorted(source_ids)!r}"
        )

    replay_rows: list[dict[str, str]] = []
    for index, source in zip(SOURCE_ROW_INDICES, selected, strict=True):
        old_case_id = str(source.get("case_id") or "")
        if old_case_id != old_case_id.strip() or not old_case_id.startswith(
            SOURCE_CASE_ID_PREFIX
        ):
            raise RuntimeError(
                f"source row {index} has an unexpected case_id prefix: {old_case_id!r}"
            )
        new_case_id = NEW_CASE_ID_PREFIX + old_case_id[len(SOURCE_CASE_ID_PREFIX) :]
        if not SAFE_CASE_ID.fullmatch(new_case_id):
            raise RuntimeError(
                f"source row {index} produced an unsafe replay case_id: {new_case_id!r}"
            )
        replay = dict(source)
        replay["case_id"] = new_case_id
        _assert_only_case_id_changed(source, replay)
        replay_rows.append(replay)

    old_ids = {str(row.get("case_id") or "") for row in source_rows}
    new_ids = {row["case_id"] for row in replay_rows}
    if len(new_ids) != len(replay_rows):
        raise RuntimeError("generated affinity replay case_id values are not unique")
    overlap = old_ids & new_ids
    if overlap:
        raise RuntimeError(
            "generated affinity replay case_id overlaps the source plan: "
            f"{sorted(overlap)!r}"
        )
    return replay_rows


def render_csv(fieldnames: Sequence[str], rows: Iterable[Mapping[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=list(fieldnames),
        extrasaction="raise",
        lineterminator="\r\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def build_pilot_plan(source_plan: Path) -> tuple[list[str], list[dict[str, str]], bytes]:
    if not source_plan.is_file():
        raise RuntimeError(f"fixed source plan is unavailable: {source_plan}")
    actual_hash = sha256_file(source_plan)
    if actual_hash != SOURCE_PLAN_SHA256:
        raise RuntimeError(
            "fixed source plan SHA-256 mismatch; no replay plan was written: "
            f"expected={SOURCE_PLAN_SHA256} actual={actual_hash}"
        )
    fieldnames, source_rows = read_csv(source_plan)
    replay_rows = build_replay_rows(fieldnames, source_rows)
    return fieldnames, replay_rows, render_csv(fieldnames, replay_rows)


def write_no_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    created = False
    try:
        binary_flag = getattr(os, "O_BINARY", 0)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | binary_flag,
            0o644,
        )
        created = True
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError(f"failed to write replay plan: {path}")
            view = view[written:]
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise RuntimeError(f"refusing to overwrite existing replay plan: {path}") from exc
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def verify_exact_output(path: Path, expected: bytes) -> None:
    if not path.is_file():
        raise RuntimeError(f"replay plan is unavailable for verification: {path}")
    actual = path.read_bytes()
    if actual != expected:
        raise RuntimeError(
            "replay plan does not exactly match the fixed generated bytes: "
            f"expected_sha256={hashlib.sha256(expected).hexdigest()} "
            f"actual_sha256={hashlib.sha256(actual).hexdigest()}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-plan", type=Path, default=SOURCE_PLAN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--verify-output", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_plan = args.source_plan.resolve()
    output = args.output.resolve()
    if source_plan == output:
        raise RuntimeError("source and output plan paths must be different")
    _, rows, payload = build_pilot_plan(source_plan)

    if args.execute:
        write_no_replace(output, payload)
        verify_exact_output(output, payload)
        mode = "executed"
    elif args.verify_output:
        verify_exact_output(output, payload)
        mode = "verified"
    else:
        mode = "dry-run"

    print(
        json.dumps(
            {
                "case_ids": [row["case_id"] for row in rows],
                "mode": mode,
                "non_case_fields_equivalent": True,
                "old_new_case_id_overlap": 0,
                "output": str(output),
                "output_sha256": hashlib.sha256(payload).hexdigest(),
                "profiles": [row["quality_profile"] for row in rows],
                "replay_rows": len(rows),
                "source_case_id": rows[0]["source_case_id"],
                "source_plan": str(source_plan),
                "source_plan_sha256": SOURCE_PLAN_SHA256,
                "source_row_indices": list(SOURCE_ROW_INDICES),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
