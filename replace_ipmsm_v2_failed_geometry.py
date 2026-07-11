"""Replace one failed IPMSM v2 geometry group with a fresh Sobol geometry.

The command is read-only by default.  Pass ``--execute`` only after reviewing
the compact dry-run manifest printed to stdout.  Execution publishes a fresh
case-plan CSV and its ``.manifest.json`` sidecar without replacing either
path.  Explicit clean-rerun case IDs may also be renamed without changing row
order, controls, or repeat relationships.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from atomic_publish import (
    PublishReceipt,
    cleanup_publish_receipt,
    publish_no_replace,
    rollback_owned_output,
)

from generate_ipmsm_v2_cases import (
    BETA_CONVENTION,
    DATASET_SCHEMA_VERSION,
    MODEL_EXTENT,
    _quality_profile_values,
    _valid_geometry_samples,
    fieldnames_for_rows,
    stable_design_hash,
)
from ipmsm_optimization import (
    GEOMETRY_VARIABLE_NAMES,
    OptimizationSpec,
    geometry_metrics,
    load_optimization_spec,
    phase_resistance_100c_ohm,
)


MANIFEST_SCHEMA_VERSION = "ipmsm_v2_failed_geometry_replacement_v1"
DESIGN_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
GROUP_ID_PATTERN = re.compile(r".+_[0-9a-f]{12}")
MUTABLE_FIELDS = frozenset(
    {
        "geometry_group_id",
        "design_hash",
        *GEOMETRY_VARIABLE_NAMES,
        "stack_length_mm",
        "phase_resistance_ohm",
    }
)


@dataclass(frozen=True)
class ReplacementPlan:
    fieldnames: tuple[str, ...]
    source_rows: tuple[dict[str, str], ...]
    output_rows: tuple[dict[str, str], ...]
    failed_design_hash: str
    replacement_design_hash: str
    failed_geometry_group_id: str
    replacement_geometry_group_id: str
    replaced_row_count: int
    replaced_repeat_row_count: int
    retry_case_id_map: tuple[tuple[str, str], ...]
    updated_repeat_reference_count: int
    output_payload: bytes


def _path_exists(path: Path) -> bool:
    """Return true for regular paths and broken symlinks."""

    return os.path.lexists(path)


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validated_design_hash(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not DESIGN_HASH_PATTERN.fullmatch(result):
        raise ValueError(f"{label} must be one lowercase 64-character SHA-256 hex digest")
    return result


def _read_csv_exact(path: Path, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    with stream:
        reader = csv.DictReader(stream)
        fieldnames = list(reader.fieldnames or ())
        if not fieldnames:
            raise ValueError(f"{label} has no CSV header: {path}")
        if any(not str(column or "").strip() for column in fieldnames):
            raise ValueError(f"{label} has a blank CSV header field: {path}")
        if len(fieldnames) != len(set(fieldnames)):
            raise ValueError(f"{label} has duplicate CSV header fields: {path}")
        rows: list[dict[str, str]] = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise ValueError(f"{label} row {row_number} has fields beyond its header: {path}")
            if any(value is None for value in raw.values()):
                raise ValueError(f"{label} row {row_number} has missing trailing fields: {path}")
            rows.append({str(key): str(value) for key, value in raw.items()})
    if not rows:
        raise ValueError(f"{label} has no data rows: {path}")
    return fieldnames, rows


def _finite_float(row: Mapping[str, str], field: str, row_number: int) -> float:
    raw_value = row.get(field)
    raw = str("" if raw_value is None else raw_value).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"source plan row {row_number} field {field!r} must be finite") from exc
    if not math.isfinite(value):
        raise ValueError(f"source plan row {row_number} field {field!r} must be finite")
    return value


def _integer(row: Mapping[str, str], field: str, row_number: int) -> int:
    value = _finite_float(row, field, row_number)
    if not value.is_integer():
        raise ValueError(f"source plan row {row_number} field {field!r} must be an integer")
    return int(value)


def _close(actual: float, expected: float, *, tolerance: float = 1e-12) -> bool:
    return math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance)


def _validate_source_plan(
    spec: OptimizationSpec,
    fieldnames: Sequence[str],
    rows: Sequence[Mapping[str, str]],
) -> None:
    expected_fields = fieldnames_for_rows(spec)
    if list(fieldnames) != expected_fields:
        raise ValueError(
            "source plan header must exactly match the canonical IPMSM v2 header and order; "
            f"expected={expected_fields!r} actual={list(fieldnames)!r}"
        )

    points = {point.name: point for point in spec.operating_points}
    bounds = {bound.name: bound for bound in spec.design_space}
    seen_case_ids: set[str] = set()
    case_rows: dict[str, tuple[int, Mapping[str, str]]] = {}
    hash_to_group: dict[str, str] = {}
    group_to_hash: dict[str, str] = {}
    group_signatures: dict[str, tuple[float, ...]] = {}
    group_splits: dict[str, str] = {}
    base_rows_by_group: set[str] = set()

    for row_number, row in enumerate(rows, start=2):
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            raise ValueError(f"source plan row {row_number} has a blank case_id")
        if case_id in seen_case_ids:
            raise ValueError(f"source plan contains duplicate case_id {case_id!r}")
        seen_case_ids.add(case_id)
        case_rows[case_id] = (row_number, row)

        design_hash = _validated_design_hash(row.get("design_hash"), f"source plan row {row_number} design_hash")
        group_id = str(row.get("geometry_group_id") or "").strip()
        if not GROUP_ID_PATTERN.fullmatch(group_id) or not group_id.endswith(design_hash[:12]):
            raise ValueError(
                f"source plan row {row_number} geometry_group_id must end with its design_hash prefix"
            )
        previous_group = hash_to_group.setdefault(design_hash, group_id)
        if previous_group != group_id:
            raise ValueError(f"design_hash {design_hash!r} belongs to multiple geometry groups")
        previous_hash = group_to_hash.setdefault(group_id, design_hash)
        if previous_hash != design_hash:
            raise ValueError(f"geometry_group_id {group_id!r} belongs to multiple design hashes")

        design = {name: _finite_float(row, name, row_number) for name in GEOMETRY_VARIABLE_NAMES}
        stack = _finite_float(row, "stack_length_mm", row_number)
        for name, value in {**design, "stack_length_mm": stack}.items():
            bound = bounds[name]
            if value < bound.lower or value > bound.upper:
                raise ValueError(
                    f"source plan row {row_number} field {name!r}={value} is outside "
                    f"[{bound.lower}, {bound.upper}]"
                )
        computed_hash = stable_design_hash(design, stack)
        if computed_hash != design_hash:
            raise ValueError(f"source plan row {row_number} geometry does not match design_hash")
        try:
            metrics = geometry_metrics(design, stack, spec.winding, slot_number=spec.slot_number)
        except ValueError as exc:
            raise ValueError(f"source plan row {row_number} has infeasible geometry: {exc}") from exc
        if metrics.slot_fill_ratio > spec.winding.fill_factor:
            raise ValueError(
                f"source plan row {row_number} slot_fill_ratio={metrics.slot_fill_ratio} exceeds "
                f"fill_factor={spec.winding.fill_factor}"
            )
        expected_resistance = phase_resistance_100c_ohm(
            design,
            stack,
            spec.winding,
            slot_number=spec.slot_number,
        )
        if not _close(_finite_float(row, "phase_resistance_ohm", row_number), expected_resistance):
            raise ValueError(f"source plan row {row_number} phase_resistance_ohm is inconsistent with geometry")

        signature = tuple(design[name] for name in GEOMETRY_VARIABLE_NAMES) + (stack, expected_resistance)
        previous_signature = group_signatures.setdefault(group_id, signature)
        if previous_signature != signature:
            raise ValueError(f"geometry group {group_id!r} contains inconsistent geometry rows")

        point_id = str(row.get("operating_point_id") or "").strip()
        point = points.get(point_id)
        if point is None:
            raise ValueError(f"source plan row {row_number} has unknown operating_point_id {point_id!r}")
        if not _close(_finite_float(row, "base_rpm", row_number), point.speed_rpm):
            raise ValueError(f"source plan row {row_number} base_rpm does not match the optimization spec")
        current = _finite_float(row, "i_peak_a", row_number)
        if current <= 0.0 or current > spec.effective_peak_current_limit_a + 1e-12:
            raise ValueError(f"source plan row {row_number} i_peak_a is outside the effective current range")
        beta = _finite_float(row, "beta_dq_deg", row_number)
        if beta < spec.beta_bounds_deg[0] or beta > spec.beta_bounds_deg[1]:
            raise ValueError(f"source plan row {row_number} beta_dq_deg is outside the spec bounds")

        split = str(row.get("doe_split") or "").strip()
        if split not in {"train", "calibration", "test"}:
            raise ValueError(f"source plan row {row_number} has invalid doe_split {split!r}")
        previous_split = group_splits.setdefault(group_id, split)
        if previous_split != split:
            raise ValueError(f"geometry group {group_id!r} belongs to multiple DOE splits")
        repeat_of = str(row.get("repeat_of_case_id") or "").strip()
        if not repeat_of:
            base_rows_by_group.add(group_id)
        elif split != "train":
            raise ValueError(f"source plan row {row_number} repeat rows must remain in the train split")

        exact_values = {
            "beta_calibration_id": spec.beta_calibration.calibration_id,
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "model_extent": MODEL_EXTENT,
            "beta_convention": BETA_CONVENTION,
            "operation": "sin_current",
        }
        for field, expected in exact_values.items():
            if str(row.get(field) or "").strip() != str(expected):
                raise ValueError(f"source plan row {row_number} field {field!r} does not match the spec contract")
        if _integer(row, "symmetry_factor", row_number) != 1:
            raise ValueError(f"source plan row {row_number} symmetry_factor must be 1")
        periodic_value = row.get("use_periodic_boundary")
        if str("" if periodic_value is None else periodic_value).strip().lower() != "false":
            raise ValueError(f"source plan row {row_number} use_periodic_boundary must be false")
        if _integer(row, "slot_num", row_number) != spec.slot_number:
            raise ValueError(f"source plan row {row_number} slot_num does not match the spec")
        if _integer(row, "pole_num", row_number) != spec.pole_number:
            raise ValueError(f"source plan row {row_number} pole_num does not match the spec")
        if not _close(_finite_float(row, "vdc_v", row_number), spec.inverter.vdc_v):
            raise ValueError(f"source plan row {row_number} vdc_v does not match the spec")
        if not _close(
            _finite_float(row, "electrical_zero_deg", row_number),
            spec.beta_calibration.electrical_zero_deg,
        ):
            raise ValueError(f"source plan row {row_number} electrical_zero_deg does not match the spec")

        profile_name = str(row.get("quality_profile") or "").strip()
        try:
            profile_values = _quality_profile_values(profile_name)
        except ValueError as exc:
            raise ValueError(f"source plan row {row_number} {exc}") from exc
        for field, expected in profile_values.items():
            if _integer(row, field, row_number) != int(expected):
                raise ValueError(f"source plan row {row_number} field {field!r} does not match its quality profile")

    missing_base = sorted(set(group_to_hash) - base_rows_by_group)
    if missing_base:
        raise ValueError(f"geometry groups have no non-repeat source rows: {missing_base[:3]}")

    for row_number, row in enumerate(rows, start=2):
        repeat_of = str(row.get("repeat_of_case_id") or "").strip()
        if not repeat_of:
            continue
        source_entry = case_rows.get(repeat_of)
        if source_entry is None:
            raise ValueError(f"source plan row {row_number} repeat_of_case_id {repeat_of!r} does not exist")
        source_number, source = source_entry
        if source_number >= row_number:
            raise ValueError(f"source plan row {row_number} repeat anchor must precede the repeat row")
        if str(source.get("repeat_of_case_id") or "").strip():
            raise ValueError(f"source plan row {row_number} repeat anchor must not itself be a repeat")
        for field in fieldnames:
            if field in {"case_id", "repeat_of_case_id"}:
                continue
            if row[field] != source[field]:
                raise ValueError(
                    f"source plan row {row_number} repeat field {field!r} differs from anchor {repeat_of!r}"
                )


def read_excluded_design_hashes_exact(paths: Iterable[Path]) -> tuple[set[str], list[dict[str, str]]]:
    hashes: set[str] = set()
    artifacts: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for path in paths:
        normalized = _normalized_path(path)
        if normalized in seen_paths:
            raise ValueError(f"duplicate --exclude-plan path: {path}")
        seen_paths.add(normalized)
        fieldnames, rows = _read_csv_exact(path, "exclusion plan")
        hash_columns = [field for field in ("design_hash", "input_design_hash") if field in fieldnames]
        if not hash_columns:
            raise ValueError(f"exclusion plan has no design_hash or input_design_hash column: {path}")
        for row_number, row in enumerate(rows, start=2):
            populated = {field: str(row.get(field) or "").strip() for field in hash_columns if str(row.get(field) or "").strip()}
            if not populated:
                raise ValueError(f"exclusion plan row {row_number} has a blank design hash: {path}")
            distinct = set(populated.values())
            if len(distinct) != 1:
                raise ValueError(f"exclusion plan row {row_number} has conflicting design hash columns: {path}")
            hashes.add(_validated_design_hash(next(iter(distinct)), f"exclusion plan row {row_number} design hash"))
        artifacts.append({"path": str(path), "sha256": _sha256_file(path)})
    return hashes, artifacts


def _render_csv(fieldnames: Sequence[str], rows: Iterable[Mapping[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fieldnames), extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def build_replacement_plan(
    spec: OptimizationSpec,
    fieldnames: Sequence[str],
    source_rows: Sequence[Mapping[str, str]],
    *,
    failed_design_hash: str,
    seed: int,
    excluded_design_hashes: Iterable[str] = (),
    retry_case_ids: Iterable[str] = (),
) -> ReplacementPlan:
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("seed must be an integer in [0, 4294967295]")
    failed_hash = _validated_design_hash(failed_design_hash, "failed design hash")
    _validate_source_plan(spec, fieldnames, source_rows)

    requested_retry_ids = [str(value).strip() for value in retry_case_ids]
    if any(not value for value in requested_retry_ids):
        raise ValueError("retry case IDs must not be blank")
    if len(requested_retry_ids) != len(set(requested_retry_ids)):
        raise ValueError("duplicate --retry-case-id values are not allowed")
    source_case_ids = [str(row["case_id"]).strip() for row in source_rows]
    source_case_id_set = set(source_case_ids)
    missing_retry_ids = [case_id for case_id in requested_retry_ids if case_id not in source_case_id_set]
    if missing_retry_ids:
        raise ValueError(f"retry case ID is not present in the source plan: {missing_retry_ids[0]}")
    requested_retry_set = set(requested_retry_ids)
    ordered_retry_ids = [case_id for case_id in source_case_ids if case_id in requested_retry_set]
    retry_case_id_map = {
        case_id: f"{case_id}_clean_retry_01" for case_id in ordered_retry_ids
    }
    retry_targets = list(retry_case_id_map.values())
    if len(retry_targets) != len(set(retry_targets)):
        raise ValueError("retry case ID renames produce duplicate target IDs")
    conflicting_targets = [target for target in retry_targets if target in source_case_id_set]
    if conflicting_targets:
        raise ValueError(f"retry case ID rename conflicts with an existing case_id: {conflicting_targets[0]}")

    failed_rows = [row for row in source_rows if str(row["design_hash"]).strip() == failed_hash]
    if not failed_rows:
        raise ValueError(f"failed design hash is not present in the source plan: {failed_hash}")
    old_group_ids = {str(row["geometry_group_id"]).strip() for row in failed_rows}
    if len(old_group_ids) != 1:
        raise ValueError(f"failed design hash must identify exactly one geometry group; found={len(old_group_ids)}")
    old_group_id = next(iter(old_group_ids))

    source_hashes = {_validated_design_hash(row["design_hash"], "source design hash") for row in source_rows}
    explicit_exclusions = {
        _validated_design_hash(value, "excluded design hash") for value in excluded_design_hashes
    }
    exclusions = source_hashes | explicit_exclusions
    new_design, new_stack, new_hash = _valid_geometry_samples(
        spec,
        1,
        seed,
        excluded_design_hashes=exclusions,
    )[0]
    if new_hash in exclusions:
        raise RuntimeError("Sobol replacement unexpectedly overlaps an excluded design hash")
    new_group_id = old_group_id[:-12] + new_hash[:12]
    existing_other_groups = {
        str(row["geometry_group_id"]).strip()
        for row in source_rows
        if str(row["design_hash"]).strip() != failed_hash
    }
    if new_group_id in existing_other_groups:
        raise RuntimeError(f"replacement geometry_group_id collides with an existing group: {new_group_id}")
    new_resistance = phase_resistance_100c_ohm(
        new_design,
        new_stack,
        spec.winding,
        slot_number=spec.slot_number,
    )

    output_rows: list[dict[str, str]] = []
    for source in source_rows:
        output = {field: str(source[field]) for field in fieldnames}
        if str(source["design_hash"]).strip() == failed_hash:
            output.update({name: str(new_design[name]) for name in GEOMETRY_VARIABLE_NAMES})
            output.update(
                {
                    "geometry_group_id": new_group_id,
                    "design_hash": new_hash,
                    "stack_length_mm": str(new_stack),
                    "phase_resistance_ohm": str(new_resistance),
                }
            )
        source_case_id = str(source["case_id"]).strip()
        source_repeat_id = str(source.get("repeat_of_case_id") or "").strip()
        output["case_id"] = retry_case_id_map.get(source_case_id, source_case_id)
        output["repeat_of_case_id"] = retry_case_id_map.get(source_repeat_id, source_repeat_id)
        output_rows.append(output)

    if len(output_rows) != len(source_rows):
        raise RuntimeError("replacement changed the source row count")
    expected_case_ids = [retry_case_id_map.get(case_id, case_id) for case_id in source_case_ids]
    if [row["case_id"] for row in output_rows] != expected_case_ids:
        raise RuntimeError("replacement changed case IDs outside the declared retry mapping or changed row order")
    for row_number, (source, output) in enumerate(zip(source_rows, output_rows), start=2):
        is_target = str(source["design_hash"]).strip() == failed_hash
        source_case_id = str(source["case_id"]).strip()
        source_repeat_id = str(source.get("repeat_of_case_id") or "").strip()
        for field in fieldnames:
            if is_target and field in MUTABLE_FIELDS:
                continue
            if field == "case_id" and source_case_id in retry_case_id_map:
                continue
            if field == "repeat_of_case_id" and source_repeat_id in retry_case_id_map:
                continue
            if output[field] != str(source[field]):
                raise RuntimeError(f"replacement changed preserved field {field!r} at row {row_number}")

    _validate_source_plan(spec, fieldnames, output_rows)
    output_hashes = {row["design_hash"] for row in output_rows}
    if failed_hash in output_hashes or new_hash not in output_hashes:
        raise RuntimeError("replacement design-hash coverage is inconsistent")
    if len(output_hashes) != len(source_hashes):
        raise RuntimeError("replacement changed the number of geometry designs")

    payload = _render_csv(fieldnames, output_rows)
    return ReplacementPlan(
        fieldnames=tuple(fieldnames),
        source_rows=tuple(dict(row) for row in source_rows),
        output_rows=tuple(output_rows),
        failed_design_hash=failed_hash,
        replacement_design_hash=new_hash,
        failed_geometry_group_id=old_group_id,
        replacement_geometry_group_id=new_group_id,
        replaced_row_count=len(failed_rows),
        replaced_repeat_row_count=sum(bool(str(row.get("repeat_of_case_id") or "").strip()) for row in failed_rows),
        retry_case_id_map=tuple((case_id, retry_case_id_map[case_id]) for case_id in ordered_retry_ids),
        updated_repeat_reference_count=sum(
            str(row.get("repeat_of_case_id") or "").strip() in retry_case_id_map for row in source_rows
        ),
        output_payload=payload,
    )


def manifest_path_for_output(output: Path) -> Path:
    return Path(f"{output}.manifest.json")


def build_manifest(
    plan: ReplacementPlan,
    *,
    mode: str,
    seed: int,
    spec_path: Path,
    source_plan: Path,
    exclude_artifacts: Sequence[Mapping[str, str]],
    excluded_design_hash_count: int,
    output: Path,
) -> dict[str, Any]:
    if mode not in {"dry-run", "execute"}:
        raise ValueError(f"unsupported manifest mode: {mode}")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "mode": mode,
        "status": "validated" if mode == "dry-run" else "created",
        "seed": seed,
        "spec": {"path": str(spec_path), "sha256": _sha256_file(spec_path)},
        "source_plan": {"path": str(source_plan), "sha256": _sha256_file(source_plan)},
        "exclude_plans": [dict(item) for item in exclude_artifacts],
        "excluded_design_hash_count": excluded_design_hash_count,
        "failed_design_hash": plan.failed_design_hash,
        "replacement_design_hash": plan.replacement_design_hash,
        "failed_geometry_group_id": plan.failed_geometry_group_id,
        "replacement_geometry_group_id": plan.replacement_geometry_group_id,
        "row_count": len(plan.output_rows),
        "replaced_row_count": plan.replaced_row_count,
        "replaced_repeat_row_count": plan.replaced_repeat_row_count,
        "retry_case_id_count": len(plan.retry_case_id_map),
        "retry_case_id_map": [
            {"source": source, "replacement": target}
            for source, target in plan.retry_case_id_map
        ],
        "updated_repeat_reference_count": plan.updated_repeat_reference_count,
        "row_order_preserved": True,
        "case_id_sequence_matches_declared_mapping": True,
        "non_retry_case_ids_preserved": True,
        "control_fields_preserved": True,
        "split_repeat_relationships_preserved": True,
        "output": {
            "path": str(output),
            "sha256": _sha256_bytes(plan.output_payload),
        },
        "manifest_path": str(manifest_path_for_output(output)),
    }


def _stage_bytes(path: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _atomic_publish_pair(output: Path, output_payload: bytes, manifest: Mapping[str, Any]) -> None:
    manifest_path = manifest_path_for_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if _path_exists(output):
        raise FileExistsError(f"refusing to overwrite existing replacement plan: {output}")
    if _path_exists(manifest_path):
        raise FileExistsError(f"refusing to overwrite existing replacement manifest: {manifest_path}")

    manifest_payload = (
        json.dumps(dict(manifest), indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    output_stage: Path | None = None
    manifest_stage: Path | None = None
    manifest_receipt: PublishReceipt | None = None
    try:
        output_stage = _stage_bytes(output, output_payload)
        manifest_stage = _stage_bytes(manifest_path, manifest_payload)
        try:
            manifest_receipt = publish_no_replace(manifest_stage, manifest_path)
        except FileExistsError as exc:
            raise FileExistsError(f"refusing to overwrite existing replacement manifest: {manifest_path}") from exc
        try:
            publish_no_replace(output_stage, output)
        except FileExistsError as exc:
            raise FileExistsError(f"refusing to overwrite existing replacement plan: {output}") from exc
        except OSError as exc:
            if _path_exists(output):
                raise FileExistsError(f"refusing to overwrite existing replacement plan: {output}") from exc
            raise OSError(f"cannot atomically publish replacement plan {output}: {exc}") from exc
    except Exception:
        if manifest_receipt is not None:
            rollback_owned_output(manifest_receipt)
        raise
    finally:
        if manifest_receipt is not None:
            cleanup_publish_receipt(manifest_receipt)
        if output_stage is not None:
            output_stage.unlink(missing_ok=True)
        if manifest_stage is not None:
            manifest_stage.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--source-plan", type=Path, required=True)
    parser.add_argument(
        "--exclude-plan",
        type=Path,
        action="append",
        default=[],
        help="Additional plan whose design hashes must not be selected; repeat as needed.",
    )
    parser.add_argument("--failed-design-hash", required=True)
    parser.add_argument(
        "--retry-case-id",
        action="append",
        default=[],
        help="Case ID to rename with the deterministic _clean_retry_01 suffix; repeat as needed.",
    )
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Atomically create the output and manifest. Omit for a read-only dry run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output
    manifest_path = manifest_path_for_output(output)
    input_paths = [args.spec, args.source_plan, *args.exclude_plan]
    output_paths = {_normalized_path(output), _normalized_path(manifest_path)}
    collisions = [path for path in input_paths if _normalized_path(path) in output_paths]
    if collisions:
        raise ValueError(f"output paths must be distinct from every input path: {collisions[0]}")
    if _path_exists(output):
        raise FileExistsError(f"refusing to overwrite existing replacement plan: {output}")
    if _path_exists(manifest_path):
        raise FileExistsError(f"refusing to overwrite existing replacement manifest: {manifest_path}")

    spec = load_optimization_spec(args.spec)
    fieldnames, source_rows = _read_csv_exact(args.source_plan, "source plan")
    explicit_exclusions, exclusion_artifacts = read_excluded_design_hashes_exact(args.exclude_plan)
    source_hashes = {
        _validated_design_hash(row.get("design_hash"), "source plan design_hash")
        for row in source_rows
    }
    plan = build_replacement_plan(
        spec,
        fieldnames,
        source_rows,
        failed_design_hash=args.failed_design_hash,
        seed=args.seed,
        excluded_design_hashes=explicit_exclusions,
        retry_case_ids=args.retry_case_id,
    )
    manifest = build_manifest(
        plan,
        mode="execute" if args.execute else "dry-run",
        seed=args.seed,
        spec_path=args.spec,
        source_plan=args.source_plan,
        exclude_artifacts=exclusion_artifacts,
        excluded_design_hash_count=len(source_hashes | explicit_exclusions),
        output=output,
    )
    if args.execute:
        _atomic_publish_pair(output, plan.output_payload, manifest)
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
