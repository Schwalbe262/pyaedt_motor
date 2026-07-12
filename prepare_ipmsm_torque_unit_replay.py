"""Build the sealed four-case replay used to audit AEDT torque units.

The source plans are immutable evidence.  This helper copies two suspect rows
and one same-design control for each suspect into a new, deterministic plan.
Publication is opt-in, pair-atomic, and never overwrites existing evidence.
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
from typing import Any, Iterable

from atomic_publish import (
    cleanup_publish_receipt,
    publish_no_replace,
    rollback_owned_output,
)


DEFAULT_ROOT = Path("simul_log_smoke/beta_zero_recovery_26092_26093")
DEFAULT_STAGE1_PLAN = DEFAULT_ROOT / "ipmsm_v2_foundation_stage1_700_cases_r4.csv"
DEFAULT_STAGE2_PLAN = DEFAULT_ROOT / "ipmsm_v2_foundation_stage2_300_cases.csv"
DEFAULT_OUTPUT = Path("simul_log_smoke/v4r4/torque_unit_replay_plan.csv")
DEFAULT_MANIFEST = Path("simul_log_smoke/v4r4/torque_unit_replay_plan.manifest.json")
REPLAY_SUFFIX = "torqueunit_replay_v1"
SCHEMA_VERSION = "ipmsm-torque-unit-replay-plan-v1"
EXPECTED_PLAN_COLUMNS = {
    "case_id",
    "geometry_group_id",
    "design_hash",
    "operating_point_id",
    "doe_split",
    "repeat_of_case_id",
    "base_rpm",
    "i_peak_a",
    "beta_dq_deg",
    "quality_profile",
}
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
DEFAULT_EXECUTION_SOURCES = (
    Path("run_ipmsm_batch.py"),
    Path("validate_ipmsm_v2_dataset.py"),
    Path("submit_ipmsm_v2_campaign.py"),
    Path("run_ipmsm_v2_campaign.py"),
    Path("atomic_publish.py"),
    Path(__file__),
)


@dataclass(frozen=True)
class ReplaySelection:
    stage: str
    case_id: str
    role: str
    expected_beta_deg: float


@dataclass(frozen=True)
class PlanSnapshot:
    path: Path
    payload: bytes
    sha256: str
    fieldnames: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    source_lines: dict[str, int]


SELECTIONS = (
    ReplaySelection("stage1", "v2s1_0010_rated_torque_01", "suspect", 0.0),
    ReplaySelection("stage1", "v2s1_0010_rated_torque_03", "same_design_control", 80.0),
    ReplaySelection("stage2", "v2s2_0002_rated_torque_01", "same_design_control", 0.0),
    ReplaySelection("stage2", "v2s2_0002_rated_torque_03", "suspect", 80.0),
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _read_plan(path: Path, label: str) -> PlanSnapshot:
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fieldnames = list(reader.fieldnames or ())
        rows = [dict(row) for row in reader]
        if path.read_bytes() != payload:
            raise ValueError(f"{label} changed while it was read")
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    if not fieldnames or any(not str(name or "").strip() for name in fieldnames):
        raise ValueError(f"{label} has an invalid header")
    if len(fieldnames) != len(set(fieldnames)):
        raise ValueError(f"{label} has duplicate columns")
    missing = sorted(EXPECTED_PLAN_COLUMNS - set(fieldnames))
    if missing:
        raise ValueError(f"{label} is missing columns: {', '.join(missing)}")
    if tuple(fieldnames) != CANONICAL_PLAN_COLUMNS:
        raise ValueError(f"{label} does not match the canonical 45-column plan schema")
    indexed: set[str] = set()
    source_lines: dict[str, int] = {}
    for line_number, row in enumerate(rows, start=2):
        case_id = str(row.get("case_id") or "").strip()
        if not case_id or case_id in indexed:
            raise ValueError(f"{label} has a blank or duplicate case_id: {case_id!r}")
        indexed.add(case_id)
        source_lines[case_id] = line_number
    return PlanSnapshot(
        path=path,
        payload=payload,
        sha256=_sha256_bytes(payload),
        fieldnames=tuple(fieldnames),
        rows=tuple(rows),
        source_lines=source_lines,
    )


def _index_rows(rows: Iterable[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {str(row["case_id"]).strip(): dict(row) for row in rows}


def _float(row: dict[str, str], column: str, case_id: str) -> float:
    try:
        return float(str(row.get(column) or "").strip())
    except ValueError as exc:
        raise ValueError(f"{case_id} has invalid {column}") from exc


def _validate_pair(
    suspect: dict[str, str],
    control: dict[str, str],
    *,
    stage: str,
) -> None:
    for column in ("design_hash", "geometry_group_id", "operating_point_id", "base_rpm", "i_peak_a"):
        if str(suspect.get(column) or "").strip() != str(control.get(column) or "").strip():
            raise ValueError(f"{stage} suspect/control mismatch: {column}")
    if str(suspect.get("operating_point_id") or "").strip() != "rated_torque":
        raise ValueError(f"{stage} replay rows must use rated_torque")
    if str(suspect.get("quality_profile") or "").strip() != "reference_ultra":
        raise ValueError(f"{stage} suspect must use reference_ultra")
    if str(control.get("quality_profile") or "").strip() != "reference_ultra":
        raise ValueError(f"{stage} control must use reference_ultra")


def _csv_bytes(fieldnames: list[str], rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


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


def _replay_geometry_group_id(stage: str, source_group: str) -> str:
    prefix = f"v2s{1 if stage == 'stage1' else 2}_geometry_"
    if not source_group.startswith(prefix):
        raise ValueError(f"{stage} source geometry_group_id is not canonical: {source_group!r}")
    return source_group.replace(
        prefix,
        f"v2s{1 if stage == 'stage1' else 2}_{REPLAY_SUFFIX}_geometry_",
        1,
    )


def _artifact_state(path: Path, payload: bytes) -> str:
    if not path.exists():
        return "absent"
    if path.read_bytes() != payload:
        raise ValueError(f"refusing to replace changed artifact: {path}")
    return "existing_verified"


def _publish_pair(
    plan_path: Path,
    plan_payload: bytes,
    manifest_path: Path,
    manifest_payload: bytes,
) -> str:
    for path in (plan_path, manifest_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    plan_state = _artifact_state(plan_path, plan_payload)
    manifest_state = _artifact_state(manifest_path, manifest_payload)
    if plan_state == manifest_state == "existing_verified":
        return "existing_verified"
    staged_plan = _stage_payload(plan_path, plan_payload) if plan_state == "absent" else None
    staged_manifest = (
        _stage_payload(manifest_path, manifest_payload) if manifest_state == "absent" else None
    )
    receipts = []
    try:
        for staged, destination in (
            (staged_plan, plan_path),
            (staged_manifest, manifest_path),
        ):
            if staged is None:
                continue
            receipt = publish_no_replace(staged, destination, proof_path=_proof_path(destination))
            receipts.append(receipt)
        if plan_path.read_bytes() != plan_payload or manifest_path.read_bytes() != manifest_payload:
            raise RuntimeError("published torque replay pair failed byte verification")
    except BaseException as exc:
        rollback_safe = True
        for receipt in reversed(receipts):
            if not rollback_owned_output(receipt):
                rollback_safe = False
        if not rollback_safe:
            raise RuntimeError("torque replay pair publication failed and rollback was unsafe") from exc
        raise
    finally:
        for receipt in receipts:
            cleanup_publish_receipt(receipt)
        for staged in (staged_plan, staged_manifest):
            if staged is not None:
                staged.unlink(missing_ok=True)
    return "published"


def build_replay(
    stage1_plan: Path,
    stage2_plan: Path,
    *,
    execution_sources: Iterable[Path] = DEFAULT_EXECUTION_SOURCES,
) -> tuple[bytes, dict[str, Any]]:
    stage1_snapshot = _read_plan(stage1_plan, "Stage1 plan")
    stage2_snapshot = _read_plan(stage2_plan, "Stage2 plan")
    if stage1_snapshot.fieldnames != stage2_snapshot.fieldnames:
        raise ValueError("Stage1 and Stage2 plan headers differ")
    sources = {
        "stage1": (stage1_snapshot, _index_rows(stage1_snapshot.rows)),
        "stage2": (stage2_snapshot, _index_rows(stage2_snapshot.rows)),
    }
    chosen: dict[str, tuple[ReplaySelection, dict[str, str]]] = {}
    for selection in SELECTIONS:
        _, rows = sources[selection.stage]
        row = rows.get(selection.case_id)
        if row is None:
            raise ValueError(f"missing selected case: {selection.case_id}")
        actual_beta = _float(row, "beta_dq_deg", selection.case_id)
        if abs(actual_beta - selection.expected_beta_deg) > 1e-12:
            raise ValueError(
                f"{selection.case_id} beta changed: expected {selection.expected_beta_deg}, got {actual_beta}"
            )
        chosen[selection.case_id] = (selection, row)
    for stage in ("stage1", "stage2"):
        stage_rows = [item for item in chosen.values() if item[0].stage == stage]
        suspect = next(row for selection, row in stage_rows if selection.role == "suspect")
        control = next(row for selection, row in stage_rows if selection.role == "same_design_control")
        _validate_pair(suspect, control, stage=stage)

    output_fields = list(stage1_snapshot.fieldnames)
    replay_rows: list[dict[str, str]] = []
    records: list[dict[str, Any]] = []
    for selection in SELECTIONS:
        source_snapshot, _ = sources[selection.stage]
        source_row = chosen[selection.case_id][1]
        replay_row = dict(source_row)
        replay_row["case_id"] = f"{selection.case_id}_{REPLAY_SUFFIX}"
        replay_row["geometry_group_id"] = _replay_geometry_group_id(
            selection.stage,
            str(source_row["geometry_group_id"]),
        )
        replay_row["repeat_of_case_id"] = ""
        replay_rows.append(replay_row)
        pair_id = f"{selection.stage}:{source_row['design_hash']}:rated_torque"
        records.append(
            {
                "stage": selection.stage,
                "role": selection.role,
                "pair_id": pair_id,
                "replay_reason": "aedt_torque_unit_forensic_audit_v1",
                "source_case_id": selection.case_id,
                "replay_case_id": replay_row["case_id"],
                "source_plan": source_snapshot.path.as_posix(),
                "source_plan_sha256": source_snapshot.sha256,
                "source_line": source_snapshot.source_lines[selection.case_id],
                "source_row_canonical_sha256": _canonical_sha256(source_row),
                "replay_row_canonical_sha256": _canonical_sha256(replay_row),
                "design_hash": str(source_row["design_hash"]),
                "source_geometry_group_id": str(source_row["geometry_group_id"]),
                "replay_geometry_group_id": str(replay_row["geometry_group_id"]),
                "beta_dq_deg": float(source_row["beta_dq_deg"]),
            }
        )
    plan_bytes = _csv_bytes(output_fields, replay_rows)
    source_records = []
    for path in execution_sources:
        if not path.is_file():
            raise ValueError(f"execution source is missing: {path}")
        source_records.append(
            {"path": path.as_posix(), "sha256": _sha256_file(path), "size": path.stat().st_size}
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "plan_path": DEFAULT_OUTPUT.as_posix(),
        "plan_sha256": _sha256_bytes(plan_bytes),
        "plan_rows": len(replay_rows),
        "plan_columns": output_fields,
        "execution_policy": {
            "keep_projects": True,
            "task_prefix": "ipmsm-v2-torqueunit-replay-v1",
            "project": "PYAEDT_MOTOR_IPMSM_V2",
            "required_capability": "conda:pyaedt2026v1",
            "env_profile": "pyaedt2026v1",
            "scheduling_profile": "fea_bursty",
            "env_setup": "module load ansys-electronics/v252",
            "remote_cases_dir": "remote/ipmsm_v2_torqueunit_replay_v1_cases",
            "result_dir": "simul_log_scheduler/ipmsm_v2_torqueunit_replay_v1_results",
            "simulation_dir": "simulation/ipmsm_v2_torqueunit_replay_v1",
            "log_dir": "simul_log_scheduler/ipmsm_v2_torqueunit_replay_v1_logs",
            "max_workers_per_node": 1,
        },
        "execution_sources": source_records,
        "cases": records,
    }
    return plan_bytes, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage1-plan", type=Path, default=DEFAULT_STAGE1_PLAN)
    parser.add_argument("--stage2-plan", type=Path, default=DEFAULT_STAGE2_PLAN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish the sealed plan/manifest pair. The default is a read-only dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output.resolve(strict=False) == args.manifest.resolve(strict=False):
        raise ValueError("plan and manifest paths must differ")
    plan_bytes, manifest = build_replay(args.stage1_plan, args.stage2_plan)
    manifest["plan_path"] = args.output.as_posix()
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    if args.publish:
        status = _publish_pair(args.output, plan_bytes, args.manifest, manifest_bytes)
        mode = "publish"
    else:
        plan_state = _artifact_state(args.output, plan_bytes)
        manifest_state = _artifact_state(args.manifest, manifest_bytes)
        status = (
            "existing_verified"
            if plan_state == manifest_state == "existing_verified"
            else "would_publish"
        )
        mode = "dry-run"
    print(
        f"torque_unit_replay mode={mode} rows={manifest['plan_rows']} "
        f"plan_sha256={manifest['plan_sha256']} status={status}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
