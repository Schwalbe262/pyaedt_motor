"""Prepare sealed Stage1/Stage2 plans after the AEDT torque-unit incident.

The two official source plans remain immutable evidence.  Exactly one suspect
``case_id`` in each plan is replaced with a new execution identity; every
other byte in each CSV is retained.  The sealed four-case replay is required
as provenance, and publication is an opt-in, proof-backed, no-overwrite
three-file transaction.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from atomic_publish import (
    cleanup_publish_receipt,
    publish_no_replace,
    rollback_owned_output,
)
from submit_ipmsm_v2_campaign import campaign_dedupe_key, sanitize_case_id


DEFAULT_ROOT = Path("simul_log_smoke/beta_zero_recovery_26092_26093")
DEFAULT_STAGE1_PLAN = DEFAULT_ROOT / "ipmsm_v2_foundation_stage1_700_cases_r4.csv"
DEFAULT_STAGE2_PLAN = DEFAULT_ROOT / "ipmsm_v2_foundation_stage2_300_cases.csv"
DEFAULT_REPLAY_PLAN = Path("simul_log_smoke/v4r4/torque_unit_replay_plan_sealed.csv")
DEFAULT_REPLAY_MANIFEST = Path(
    "simul_log_smoke/v4r4/torque_unit_replay_plan_sealed.manifest.json"
)
DEFAULT_STAGE1_OUTPUT = Path(
    "simul_log_smoke/v4r4/ipmsm_v2_foundation_stage1_700_cases_torqueunit_fix_v1.csv"
)
DEFAULT_STAGE2_OUTPUT = Path(
    "simul_log_smoke/v4r4/ipmsm_v2_foundation_stage2_300_cases_torqueunit_fix_v1.csv"
)
DEFAULT_MANIFEST_OUTPUT = Path(
    "simul_log_smoke/v4r4/torque_unit_recovery_plans.manifest.json"
)

SCHEMA_VERSION = "ipmsm-torque-unit-recovery-plans-v1"
EXPECTED_REPLAY_PLAN_SHA256 = (
    "16d5730b3f9c1c4f55fc912cf5bf405f3e938df33babd2f66a1a3f45c774fc7a"
)
EXPECTED_REPLAY_MANIFEST_SHA256 = (
    "e3e71d27aadb3b422260babb213495d497be9f92e5b18edcfc6d44496f23364a"
)
QUARANTINED_STAGE2_TASK_ID = 28880

CANONICAL_PLAN_COLUMNS = (
    "case_id",
    "geometry_group_id",
    "design_hash",
    "operating_point_id",
    "doe_split",
    "repeat_of_case_id",
    "beta_calibration_id",
    "dataset_schema_version",
    "quality_profile",
    "model_extent",
    "symmetry_factor",
    "use_periodic_boundary",
    "beta_convention",
    "electrical_zero_deg",
    "operation",
    "slot_num",
    "pole_num",
    "stator_outer_radius",
    "stator_back_yoke_thick_ratio",
    "stator_inner_ratio",
    "stator_shoe_thick",
    "stator_teeth_length_ratio",
    "stator_teeth_width_ratio",
    "stator_gap",
    "slot_opening_ratio",
    "rotator_gap",
    "shaft_ratio",
    "magnet_shield_thick",
    "magnet_setback_ratio",
    "magnet_thick_ratio",
    "magnet_space_height_ratio",
    "magnet_height_ratio",
    "base_rpm",
    "i_peak_a",
    "beta_dq_deg",
    "stack_length_mm",
    "phase_resistance_ohm",
    "vdc_v",
    "transient_periods",
    "steps_per_period",
    "mesh_magnet_elements",
    "mesh_rotor_elements",
    "mesh_stator_elements",
    "mesh_winding_elements",
    "mesh_band_elements",
)

STAGE_REPLACEMENTS = {
    "stage1": {
        "source_case_id": "v2s1_0010_rated_torque_01",
        "revised_case_id": "v2s1_0010_rated_torque_01_torqueunit_fix_v1",
        "replay_case_id": "v2s1_0010_rated_torque_01_torqueunit_replay_v1",
        "expected_rows": 700,
    },
    "stage2": {
        "source_case_id": "v2s2_0002_rated_torque_03",
        "revised_case_id": "v2s2_0002_rated_torque_03_torqueunit_fix_v1",
        "replay_case_id": "v2s2_0002_rated_torque_03_torqueunit_replay_v1",
        "expected_rows": 300,
    },
}

EXPECTED_REPLAY_CASES = {
    "v2s1_0010_rated_torque_01": (
        "stage1",
        "suspect",
        "v2s1_0010_rated_torque_01_torqueunit_replay_v1",
    ),
    "v2s1_0010_rated_torque_03": (
        "stage1",
        "same_design_control",
        "v2s1_0010_rated_torque_03_torqueunit_replay_v1",
    ),
    "v2s2_0002_rated_torque_01": (
        "stage2",
        "same_design_control",
        "v2s2_0002_rated_torque_01_torqueunit_replay_v1",
    ),
    "v2s2_0002_rated_torque_03": (
        "stage2",
        "suspect",
        "v2s2_0002_rated_torque_03_torqueunit_replay_v1",
    ),
}

STAGE2_SCHEDULER_IDENTITY = {
    "project": "PYAEDT_MOTOR_IPMSM_V2",
    "task_prefix": "ipmsm-v2-foundation-s2",
    "remote_cases_dir": "remote/ipmsm_v2_foundation_s2",
    "result_dir": "simul_log/ipmsm_v2_foundation_s2",
    "simulation_dir": "simulation/ipmsm_v2_foundation_s2",
    "log_dir": "simul_log_scheduler/ipmsm_v2_foundation_s2_logs",
}


@dataclass(frozen=True)
class PlanSnapshot:
    path: Path
    payload: bytes
    sha256: str
    fieldnames: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    source_lines: dict[str, int]


@dataclass(frozen=True)
class ReplayEvidence:
    plan: PlanSnapshot
    manifest_sha256: str
    records: dict[str, dict[str, Any]]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _stable_read_bytes(path: Path, label: str) -> bytes:
    try:
        payload = path.read_bytes()
        if path.read_bytes() != payload:
            raise ValueError(f"{label} changed while it was read")
    except OSError as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    return payload


def _parse_plan_payload(
    path: Path,
    payload: bytes,
    label: str,
    *,
    expected_rows: int,
) -> PlanSnapshot:
    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fieldnames = tuple(reader.fieldnames or ())
        rows = tuple(dict(row) for row in reader)
    except (UnicodeError, csv.Error) as exc:
        raise ValueError(f"cannot parse {label}: {path}") from exc
    if fieldnames != CANONICAL_PLAN_COLUMNS:
        raise ValueError(f"{label} does not match the canonical 45-column plan schema")
    if len(rows) != expected_rows:
        raise ValueError(f"{label} row count changed: expected {expected_rows}, got {len(rows)}")
    source_lines: dict[str, int] = {}
    for line_number, row in enumerate(rows, start=2):
        if set(row) != set(CANONICAL_PLAN_COLUMNS) or any(value is None for value in row.values()):
            raise ValueError(f"{label} row {line_number} does not match the canonical schema")
        case_id = str(row.get("case_id") or "").strip()
        if not case_id or case_id in source_lines:
            raise ValueError(f"{label} has a blank or duplicate case_id: {case_id!r}")
        source_lines[case_id] = line_number
    return PlanSnapshot(
        path=path,
        payload=payload,
        sha256=_sha256_bytes(payload),
        fieldnames=fieldnames,
        rows=rows,
        source_lines=source_lines,
    )


def _read_plan(path: Path, label: str, *, expected_rows: int) -> PlanSnapshot:
    return _parse_plan_payload(
        path,
        _stable_read_bytes(path, label),
        label,
        expected_rows=expected_rows,
    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate replay manifest key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite replay manifest constant: {value}")


def _read_json(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    payload = _stable_read_bytes(path, label)
    try:
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload, value


def _row_index(snapshot: PlanSnapshot) -> dict[str, dict[str, str]]:
    return {str(row["case_id"]): dict(row) for row in snapshot.rows}


def _same_path(recorded: object, actual: Path) -> bool:
    if not isinstance(recorded, str) or not recorded.strip():
        return False
    return Path(recorded).resolve(strict=False) == actual.resolve(strict=False)


def _validate_replay_evidence(
    replay_plan: Path,
    replay_manifest: Path,
    sources: dict[str, PlanSnapshot],
    *,
    expected_plan_sha256: str,
    expected_manifest_sha256: str | None,
) -> ReplayEvidence:
    replay = _read_plan(replay_plan, "sealed replay plan", expected_rows=4)
    if replay.sha256 != expected_plan_sha256:
        raise ValueError(
            "sealed replay plan SHA-256 changed: "
            f"expected {expected_plan_sha256}, got {replay.sha256}"
        )
    manifest_payload, manifest = _read_json(replay_manifest, "sealed replay manifest")
    manifest_sha256 = _sha256_bytes(manifest_payload)
    if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256:
        raise ValueError(
            "sealed replay manifest SHA-256 changed: "
            f"expected {expected_manifest_sha256}, got {manifest_sha256}"
        )
    if manifest.get("schema_version") != "ipmsm-torque-unit-replay-plan-v1":
        raise ValueError("sealed replay manifest schema changed")
    if manifest.get("plan_sha256") != expected_plan_sha256:
        raise ValueError("sealed replay manifest does not bind the expected plan SHA-256")
    if manifest.get("plan_rows") != 4 or tuple(manifest.get("plan_columns") or ()) != CANONICAL_PLAN_COLUMNS:
        raise ValueError("sealed replay manifest plan shape changed")
    if not _same_path(manifest.get("plan_path"), replay_plan):
        raise ValueError("sealed replay manifest plan path does not identify the replay plan")

    raw_records = manifest.get("cases")
    if not isinstance(raw_records, list) or len(raw_records) != len(EXPECTED_REPLAY_CASES):
        raise ValueError("sealed replay manifest case mapping changed")
    records: dict[str, dict[str, Any]] = {}
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise ValueError("sealed replay manifest contains a non-object case record")
        source_case_id = str(raw.get("source_case_id") or "")
        if not source_case_id or source_case_id in records:
            raise ValueError("sealed replay manifest has a blank or duplicate source case")
        records[source_case_id] = raw
    if set(records) != set(EXPECTED_REPLAY_CASES):
        raise ValueError("sealed replay manifest source-case set changed")

    replay_rows = _row_index(replay)
    source_rows = {stage: _row_index(snapshot) for stage, snapshot in sources.items()}
    for source_case_id, (stage, role, replay_case_id) in EXPECTED_REPLAY_CASES.items():
        record = records[source_case_id]
        if (
            record.get("stage") != stage
            or record.get("role") != role
            or record.get("replay_case_id") != replay_case_id
        ):
            raise ValueError(f"sealed replay mapping changed for {source_case_id}")
        source = sources[stage]
        source_row = source_rows[stage].get(source_case_id)
        replay_row = replay_rows.get(replay_case_id)
        if source_row is None or replay_row is None:
            raise ValueError(f"sealed replay row is missing for {source_case_id}")
        if record.get("source_plan_sha256") != source.sha256:
            raise ValueError(f"sealed replay source-plan hash changed for {source_case_id}")
        if record.get("source_line") != source.source_lines[source_case_id]:
            raise ValueError(f"sealed replay source line changed for {source_case_id}")
        if record.get("source_row_canonical_sha256") != _canonical_sha256(source_row):
            raise ValueError(f"sealed replay source-row hash changed for {source_case_id}")
        if record.get("replay_row_canonical_sha256") != _canonical_sha256(replay_row):
            raise ValueError(f"sealed replay row hash changed for {source_case_id}")
        if str(replay_row.get("design_hash") or "") != str(source_row.get("design_hash") or ""):
            raise ValueError(f"sealed replay design identity changed for {source_case_id}")
    if set(replay_rows) != {value[2] for value in EXPECTED_REPLAY_CASES.values()}:
        raise ValueError("sealed replay plan contains unexpected case IDs")
    return ReplayEvidence(plan=replay, manifest_sha256=manifest_sha256, records=records)


def _replace_case_id_only(
    source: PlanSnapshot,
    *,
    stage: str,
) -> tuple[bytes, PlanSnapshot]:
    replacement = STAGE_REPLACEMENTS[stage]
    source_case_id = str(replacement["source_case_id"])
    revised_case_id = str(replacement["revised_case_id"])
    if source_case_id not in source.source_lines:
        raise ValueError(f"{stage} source plan is missing suspect {source_case_id}")
    if revised_case_id in source.source_lines:
        raise ValueError(f"{stage} revised case ID already exists: {revised_case_id}")
    lines = source.payload.splitlines(keepends=True)
    source_line = source.source_lines[source_case_id]
    if len(lines) != len(source.rows) + 1:
        raise ValueError(f"{stage} source plan has an ambiguous physical line layout")
    line_index = source_line - 1
    old_prefix = source_case_id.encode("utf-8") + b","
    new_prefix = revised_case_id.encode("utf-8") + b","
    if not lines[line_index].startswith(old_prefix):
        raise ValueError(f"{stage} suspect case_id is not the unquoted first field")
    lines[line_index] = new_prefix + lines[line_index][len(old_prefix) :]
    payload = b"".join(lines)
    revised = _parse_plan_payload(
        source.path,
        payload,
        f"revised {stage} plan",
        expected_rows=int(replacement["expected_rows"]),
    )
    for index, (old_row, new_row) in enumerate(zip(source.rows, revised.rows, strict=True)):
        changed = {key for key in CANONICAL_PLAN_COLUMNS if old_row[key] != new_row[key]}
        if index == line_index - 1:
            if changed != {"case_id"} or new_row["case_id"] != revised_case_id:
                raise ValueError(f"revised {stage} suspect changed fields other than case_id")
        elif changed:
            raise ValueError(f"revised {stage} changed an unrelated row")
    return payload, revised


def _dedupe_key(row: dict[str, str]) -> str:
    args = SimpleNamespace(**STAGE2_SCHEDULER_IDENTITY)
    return campaign_dedupe_key(args, row, sanitize_case_id(row["case_id"]))


def _stage2_dedupe_evidence(
    source: PlanSnapshot,
    revised: PlanSnapshot,
) -> tuple[dict[str, Any], str, str]:
    source_suspect = str(STAGE_REPLACEMENTS["stage2"]["source_case_id"])
    revised_suspect = str(STAGE_REPLACEMENTS["stage2"]["revised_case_id"])
    unchanged: list[dict[str, str]] = []
    source_replacement_key = ""
    revised_replacement_key = ""
    source_keys: set[str] = set()
    revised_keys: set[str] = set()
    for source_row, revised_row in zip(source.rows, revised.rows, strict=True):
        old_key = _dedupe_key(dict(source_row))
        new_key = _dedupe_key(dict(revised_row))
        if old_key in source_keys or new_key in revised_keys:
            raise ValueError("Stage2 scheduler dedupe keys are not unique")
        source_keys.add(old_key)
        revised_keys.add(new_key)
        if source_row["case_id"] == source_suspect:
            if revised_row["case_id"] != revised_suspect or old_key == new_key:
                raise ValueError("Stage2 replacement did not receive a fresh dedupe identity")
            source_replacement_key = old_key
            revised_replacement_key = new_key
            continue
        if source_row != revised_row or old_key != new_key:
            raise ValueError(f"Stage2 unchanged dedupe changed for {source_row['case_id']}")
        unchanged.append({"case_id": source_row["case_id"], "dedupe_key": old_key})
    if len(unchanged) != 299 or not source_replacement_key or not revised_replacement_key:
        raise ValueError("Stage2 dedupe preservation count is not exactly 299")
    unchanged_sha = _canonical_sha256(unchanged)
    evidence = {
        "schema_version": "ipmsm-v2-stage2-dedupe-preservation-v1",
        "identity": dict(STAGE2_SCHEDULER_IDENTITY),
        "identity_inputs": [
            "project",
            "task_prefix",
            "safe_case_id",
            "remote_cases_dir",
            "result_dir",
            "canonical_row_json",
        ],
        "unchanged_rows": len(unchanged),
        "unchanged_case_and_dedupe_canonical_sha256": unchanged_sha,
        "source_unchanged_canonical_sha256": unchanged_sha,
        "revised_unchanged_canonical_sha256": unchanged_sha,
        "all_unchanged_dedupe_keys_preserved": True,
        "replacement_source_dedupe_key": source_replacement_key,
        "replacement_revised_dedupe_key": revised_replacement_key,
    }
    return evidence, source_replacement_key, revised_replacement_key


def _plan_record(snapshot: PlanSnapshot, *, path: Path) -> dict[str, Any]:
    return {
        "path": path.as_posix(),
        "sha256": snapshot.sha256,
        "bytes": len(snapshot.payload),
        "rows": len(snapshot.rows),
        "columns": len(snapshot.fieldnames),
    }


def build_recovery_bundle(
    stage1_plan: Path,
    stage2_plan: Path,
    replay_plan: Path,
    replay_manifest: Path,
    stage1_output: Path,
    stage2_output: Path,
    *,
    expected_replay_plan_sha256: str = EXPECTED_REPLAY_PLAN_SHA256,
    expected_replay_manifest_sha256: str | None = EXPECTED_REPLAY_MANIFEST_SHA256,
) -> tuple[bytes, bytes, dict[str, Any]]:
    stage1 = _read_plan(stage1_plan, "Stage1 source plan", expected_rows=700)
    stage2 = _read_plan(stage2_plan, "Stage2 source plan", expected_rows=300)
    if set(stage1.source_lines) & set(stage2.source_lines):
        raise ValueError("Stage1 and Stage2 source plans have overlapping case IDs")
    sources = {"stage1": stage1, "stage2": stage2}
    replay = _validate_replay_evidence(
        replay_plan,
        replay_manifest,
        sources,
        expected_plan_sha256=expected_replay_plan_sha256,
        expected_manifest_sha256=expected_replay_manifest_sha256,
    )
    stage1_payload, revised_stage1 = _replace_case_id_only(stage1, stage="stage1")
    stage2_payload, revised_stage2 = _replace_case_id_only(stage2, stage="stage2")
    if set(revised_stage1.source_lines) & set(revised_stage2.source_lines):
        raise ValueError("revised Stage1 and Stage2 plans have overlapping case IDs")
    dedupe, source_dedupe, revised_dedupe = _stage2_dedupe_evidence(stage2, revised_stage2)

    revised_by_stage = {"stage1": revised_stage1, "stage2": revised_stage2}
    replacements: list[dict[str, Any]] = []
    for stage in ("stage1", "stage2"):
        spec = STAGE_REPLACEMENTS[stage]
        source_case_id = str(spec["source_case_id"])
        revised_case_id = str(spec["revised_case_id"])
        replay_case_id = str(spec["replay_case_id"])
        source_row = _row_index(sources[stage])[source_case_id]
        revised_row = _row_index(revised_by_stage[stage])[revised_case_id]
        replay_record = replay.records[source_case_id]
        replacements.append(
            {
                "stage": stage,
                "reason": "aedt_millinewtonmeter_parser_recovery_v1",
                "source_case_id": source_case_id,
                "revised_case_id": revised_case_id,
                "source_line": sources[stage].source_lines[source_case_id],
                "revised_line": revised_by_stage[stage].source_lines[revised_case_id],
                "source_plan_sha256": sources[stage].sha256,
                "revised_plan_sha256": revised_by_stage[stage].sha256,
                "source_row_canonical_sha256": _canonical_sha256(source_row),
                "revised_row_canonical_sha256": _canonical_sha256(revised_row),
                "only_changed_fields": ["case_id"],
                "replay_case_id": replay_case_id,
                "replay_role": replay_record["role"],
                "replay_line": replay.plan.source_lines[replay_case_id],
                "replay_row_canonical_sha256": replay_record[
                    "replay_row_canonical_sha256"
                ],
                "quarantined_scheduler_task_id": (
                    QUARANTINED_STAGE2_TASK_ID if stage == "stage2" else None
                ),
            }
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_plans": {
            "stage1": _plan_record(stage1, path=stage1_plan),
            "stage2": _plan_record(stage2, path=stage2_plan),
        },
        "revised_plans": {
            "stage1": _plan_record(revised_stage1, path=stage1_output),
            "stage2": _plan_record(revised_stage2, path=stage2_output),
        },
        "replacements": replacements,
        "sealed_replay": {
            "plan_path": replay_plan.as_posix(),
            "plan_sha256": replay.plan.sha256,
            "manifest_path": replay_manifest.as_posix(),
            "manifest_sha256": replay.manifest_sha256,
            "validated_case_links": len(replay.records),
        },
        "stage2_scheduler_dedupe": dedupe,
        "quarantine": {
            "scheduler_task_ids": [QUARANTINED_STAGE2_TASK_ID],
            "records": [
                {
                    "scheduler_task_id": QUARANTINED_STAGE2_TASK_ID,
                    "case_id": STAGE_REPLACEMENTS["stage2"]["source_case_id"],
                    "source_dedupe_key": source_dedupe,
                    "replacement_case_id": STAGE_REPLACEMENTS["stage2"][
                        "revised_case_id"
                    ],
                    "replacement_dedupe_key": revised_dedupe,
                    "reason": "reported_torque_violates_apparent_power_bound",
                }
            ],
        },
    }
    return stage1_payload, stage2_payload, manifest


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _stage_payload(path: Path, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _proof_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.publish-proof.json")


def _artifact_state(path: Path, payload: bytes) -> str:
    if not path.exists():
        return "absent"
    if _stable_read_bytes(path, "existing recovery artifact") != payload:
        raise ValueError(f"refusing to replace changed artifact: {path}")
    return "existing_verified"


def _publish_bundle(artifacts: Iterable[tuple[Path, bytes]]) -> str:
    items = list(artifacts)
    resolved = [path.resolve(strict=False) for path, _ in items]
    if len(resolved) != len(set(resolved)):
        raise ValueError("recovery bundle output paths must be distinct")
    for path, _ in items:
        path.parent.mkdir(parents=True, exist_ok=True)
    states = [_artifact_state(path, payload) for path, payload in items]
    if all(state == "existing_verified" for state in states):
        return "existing_verified"
    staged = [
        _stage_payload(path, payload) if state == "absent" else None
        for (path, payload), state in zip(items, states, strict=True)
    ]
    receipts = []
    try:
        for (destination, _), source in zip(items, staged, strict=True):
            if source is None:
                continue
            receipts.append(
                publish_no_replace(source, destination, proof_path=_proof_path(destination))
            )
        for destination, payload in items:
            if _stable_read_bytes(destination, "published recovery artifact") != payload:
                raise RuntimeError("published recovery bundle failed byte verification")
    except BaseException as exc:
        rollback_safe = True
        for receipt in reversed(receipts):
            if not rollback_owned_output(receipt):
                rollback_safe = False
        if not rollback_safe:
            raise RuntimeError(
                "recovery bundle publication failed and ownership-safe rollback was impossible"
            ) from exc
        raise
    finally:
        for receipt in receipts:
            cleanup_publish_receipt(receipt)
        for path in staged:
            if path is not None:
                path.unlink(missing_ok=True)
    return "published"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage1-plan", type=Path, default=DEFAULT_STAGE1_PLAN)
    parser.add_argument("--stage2-plan", type=Path, default=DEFAULT_STAGE2_PLAN)
    parser.add_argument("--replay-plan", type=Path, default=DEFAULT_REPLAY_PLAN)
    parser.add_argument("--replay-manifest", type=Path, default=DEFAULT_REPLAY_MANIFEST)
    parser.add_argument("--stage1-output", type=Path, default=DEFAULT_STAGE1_OUTPUT)
    parser.add_argument("--stage2-output", type=Path, default=DEFAULT_STAGE2_OUTPUT)
    parser.add_argument("--manifest-output", type=Path, default=DEFAULT_MANIFEST_OUTPUT)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish the two revised plans and manifest. Default is a read-only dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inputs = [args.stage1_plan, args.stage2_plan, args.replay_plan, args.replay_manifest]
    outputs = [args.stage1_output, args.stage2_output, args.manifest_output]
    input_paths = {path.resolve(strict=False) for path in inputs}
    output_paths = [path.resolve(strict=False) for path in outputs]
    if len(output_paths) != len(set(output_paths)) or input_paths & set(output_paths):
        raise ValueError("recovery inputs and three output paths must all be distinct")
    stage1_payload, stage2_payload, manifest = build_recovery_bundle(
        args.stage1_plan,
        args.stage2_plan,
        args.replay_plan,
        args.replay_manifest,
        args.stage1_output,
        args.stage2_output,
        expected_replay_plan_sha256=EXPECTED_REPLAY_PLAN_SHA256,
        expected_replay_manifest_sha256=EXPECTED_REPLAY_MANIFEST_SHA256,
    )
    manifest_payload = _manifest_bytes(manifest)
    artifacts = [
        (args.stage1_output, stage1_payload),
        (args.stage2_output, stage2_payload),
        (args.manifest_output, manifest_payload),
    ]
    if args.publish:
        status = _publish_bundle(artifacts)
        mode = "publish"
    else:
        states = [_artifact_state(path, payload) for path, payload in artifacts]
        status = "existing_verified" if all(x == "existing_verified" for x in states) else "would_publish"
        mode = "dry-run"
    print(
        f"torque_unit_recovery mode={mode} stage1_rows=700 stage2_rows=300 "
        f"replacements=2 replay_sha256={manifest['sealed_replay']['plan_sha256']} "
        f"stage2_dedupe_preserved={manifest['stage2_scheduler_dedupe']['unchanged_rows']} "
        f"status={status}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
