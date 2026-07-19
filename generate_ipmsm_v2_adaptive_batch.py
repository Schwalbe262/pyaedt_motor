"""Generate one deterministic post-1300 adaptive IPMSM v2 enrichment batch.

Each batch contains 40 training geometry groups and 10 conformal-calibration
geometry groups (six operating rows per group).  The sealed Stage3 test cohort
is intentionally absent and must be supplied separately to training through
``continue_ipmsm_v2_stage2.py --training-audit-case-plan``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from atomic_publish import (
    PROOF_SCHEMA_VERSION,
    FileIdentity,
    PublishReceipt,
    cleanup_publish_receipt,
    publish_no_replace,
    receipt_owns_destination,
    rollback_owned_output,
)
import generate_ipmsm_v2_cases as foundation
from ipmsm_optimization import OptimizationSpec, optimization_spec_from_mapping


SCHEMA_VERSION = "ipmsm_v2_adaptive_enrichment_batch_v1"
R2_HISTORY_SCHEMA_VERSION = "ipmsm_v2_adaptive_r2_history_v1"
TRAIN_GEOMETRIES = 40
CALIBRATION_GEOMETRIES = 10
ROWS_PER_GEOMETRY = 6
EXPECTED_ROWS = (TRAIN_GEOMETRIES + CALIBRATION_GEOMETRIES) * ROWS_PER_GEOMETRY
SEED_STRIDE = 100
DEFAULT_ADAPTATION_SEED_BASE = foundation.STAGE3_ADAPTATION_SEED
DEFAULT_CALIBRATION_SEED_BASE = foundation.STAGE3_CALIBRATION_SEED
DEFAULT_CANDIDATE_POOL_GEOMETRIES = foundation.STAGE3_CANDIDATE_POOL_GEOMETRIES
MIN_PRIMARY_R2_IMPROVEMENT = 0.01
PLATEAU_CONSECUTIVE_BATCHES = 2


def adaptive_batch_seed(base_seed: int, batch_index: int) -> int:
    if type(base_seed) is not int:
        raise ValueError("adaptive seed base must be an integer")
    if type(batch_index) is not int or batch_index < 1:
        raise ValueError("adaptive batch index must be a positive integer")
    return base_seed + SEED_STRIDE * batch_index


def evaluate_adaptive_plateau(
    baseline_min_primary_r2: float,
    completed_batch_min_primary_r2: Sequence[float],
    *,
    minimum_improvement: float = MIN_PRIMARY_R2_IMPROVEMENT,
    consecutive_batches: int = PLATEAU_CONSECUTIVE_BATCHES,
) -> dict[str, Any]:
    """Return the fail-closed FEA continuation decision for completed batches."""

    values = [float(baseline_min_primary_r2), *(float(value) for value in completed_batch_min_primary_r2)]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("minimum primary R2 history must be finite")
    if not math.isfinite(minimum_improvement) or minimum_improvement <= 0.0:
        raise ValueError("minimum improvement must be finite and positive")
    if type(consecutive_batches) is not int or consecutive_batches < 1:
        raise ValueError("consecutive batch count must be a positive integer")
    improvements = [current - prior for prior, current in zip(values, values[1:])]
    trailing_below_threshold = 0
    for improvement in reversed(improvements):
        if improvement >= minimum_improvement:
            break
        trailing_below_threshold += 1
    stop_fea = trailing_below_threshold >= consecutive_batches
    return {
        "action": "model_physics_diagnosis" if stop_fea else "continue_adaptive_fea",
        "completed_batches": len(completed_batch_min_primary_r2),
        "consecutive_batches_required": consecutive_batches,
        "improvements": improvements,
        "minimum_improvement": minimum_improvement,
        "stop_fea": stop_fea,
        "trailing_below_threshold": trailing_below_threshold,
    }


def _failed_decision_r2_record(path: Path, label: str) -> tuple[dict[str, str], float]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    decision = foundation._strict_json_bytes(payload, label)
    required = {
        "schema_version": foundation.STAGE2_DECISION_SCHEMA_VERSION,
        "decision": "run_stage2",
        "mode": "execute",
        "status": "combined_r2_failed",
    }
    mismatches = [key for key, expected in required.items() if decision.get(key) != expected]
    combined = decision.get("combined")
    primary = combined.get("primary_test_r2") if isinstance(combined, Mapping) else None
    targets = set(foundation.trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS)
    if not isinstance(primary, Mapping) or set(primary) != targets:
        mismatches.append("combined.primary_test_r2")
        primary = {}
    values: list[float] = []
    for target in foundation.trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS:
        try:
            value = float(primary[target])
        except (KeyError, TypeError, ValueError):
            mismatches.append(f"combined.primary_test_r2.{target}")
            continue
        if not math.isfinite(value):
            mismatches.append(f"combined.primary_test_r2.{target}")
        else:
            values.append(value)
    if mismatches:
        raise ValueError(f"{label} is not exact failed-gate R2 evidence: {', '.join(mismatches)}")
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }, min(values)


def _initial_r2_history_bytes(failed_decision: Path) -> bytes:
    decision, minimum = _failed_decision_r2_record(
        failed_decision.resolve(strict=False),
        "initial adaptive failed decision",
    )
    return (
        json.dumps(
            {
                "schema_version": R2_HISTORY_SCHEMA_VERSION,
                "records": [
                    {
                        "batch_index": 0,
                        "decision": decision,
                        "min_primary_r2": minimum,
                    }
                ],
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _r2_history_publish_proof_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.publish-proof.json")


def _r2_history_proof_receipt(
    output: Path,
    proof: Path,
    *,
    require_destination: bool,
    require_source: bool,
) -> PublishReceipt:
    try:
        if proof.is_symlink() or not proof.is_file():
            raise ValueError("adaptive R2 history proof is not a regular file")
        raw = foundation._strict_json_bytes(
            proof.read_bytes(),
            "adaptive R2 history publication proof",
        )
        if raw.get("schema_version") != PROOF_SCHEMA_VERSION or set(raw) != {
            "schema_version",
            "source",
            "destination",
            "identity",
        }:
            raise ValueError("adaptive R2 history proof fields changed")
        if not isinstance(raw["source"], str) or not isinstance(
            raw["destination"], str
        ):
            raise ValueError("adaptive R2 history proof paths are invalid")
        raw_source = Path(raw["source"])
        raw_destination = Path(raw["destination"])
        if not raw_source.is_absolute() or not raw_destination.is_absolute():
            raise ValueError("adaptive R2 history proof paths are not absolute")
        source = raw_source.absolute()
        destination = raw_destination.absolute()
        if destination != output.absolute():
            raise ValueError("adaptive R2 history proof destination changed")
        if (
            source.parent != output.parent.absolute()
            or not source.name.startswith(f".{output.name}.")
            or not source.name.endswith(".tmp")
            or source.name == f".{output.name}.tmp"
        ):
            raise ValueError("adaptive R2 history proof staging path changed")
        identity_raw = raw["identity"]
        if not isinstance(identity_raw, Mapping):
            raise ValueError("adaptive R2 history proof identity is invalid")
        identity = FileIdentity.from_mapping(identity_raw)
        receipt = PublishReceipt(
            source=source,
            destination=output.absolute(),
            identity=identity,
            strategy="adaptive_r2_history_proof_cleanup",
            proof_path=proof.absolute(),
        )
        if require_destination and not receipt_owns_destination(receipt):
            raise ValueError(
                "adaptive R2 history proof does not own its destination"
            )
        source_exists = os.path.lexists(source)
        if require_source and not source_exists:
            raise ValueError("adaptive R2 history proof staging file is missing")
        if source_exists:
            if source.is_symlink() or not source.is_file():
                raise ValueError(
                    "adaptive R2 history proof staging file is not regular"
                )
            if FileIdentity.from_path(source) != identity:
                raise ValueError(
                    "adaptive R2 history proof does not own its staging file"
                )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"invalid adaptive R2 history publication proof: {proof}: {exc}"
        ) from exc
    return receipt


def _unlink_verified_r2_history_staging(
    receipt: PublishReceipt,
    *,
    required: bool,
) -> None:
    source = receipt.source
    if not os.path.lexists(source):
        if required:
            raise RuntimeError(
                "verified adaptive R2 history staging file is missing"
            )
        return
    try:
        if FileIdentity.from_path(source) != receipt.identity:
            raise RuntimeError(
                "verified adaptive R2 history staging identity changed"
            )
        source.unlink()
    except FileNotFoundError:
        if required:
            raise RuntimeError(
                "verified adaptive R2 history staging file disappeared"
            )
    except OSError as exc:
        raise RuntimeError(
            "cannot clean verified adaptive R2 history staging file"
        ) from exc
    if os.path.lexists(source):
        raise RuntimeError(
            "cannot clean verified adaptive R2 history staging file"
        )


def _cleanup_verified_r2_history_publication(
    receipt: PublishReceipt,
    expected_payload: bytes,
) -> None:
    """Strictly retire staging/proof evidence without touching the output."""

    proof = receipt.proof_path
    if proof is None:
        raise RuntimeError("verified adaptive R2 history publication lacks a proof")
    _unlink_verified_r2_history_staging(receipt, required=False)

    try:
        output_is_exact = (
            receipt_owns_destination(receipt)
            and receipt.destination.is_file()
            and receipt.destination.read_bytes() == expected_payload
        )
    except OSError as exc:
        raise RuntimeError(
            "cannot audit adaptive R2 history before proof cleanup"
        ) from exc
    if not output_is_exact:
        raise RuntimeError(
            "adaptive R2 history changed before proof cleanup"
        )

    try:
        proof.unlink()
    except OSError as exc:
        raise RuntimeError(
            "cannot clean verified adaptive R2 history publication proof"
        ) from exc
    if os.path.lexists(proof):
        raise RuntimeError(
            "cannot clean verified adaptive R2 history publication proof"
        )


def _recover_absent_r2_history_publication(
    receipt: PublishReceipt,
    expected_payload: bytes,
) -> None:
    """Strictly retire an application-owned orphan before a fresh publish."""

    proof = receipt.proof_path
    if proof is None:
        raise RuntimeError("orphaned adaptive R2 history publication lacks a proof")
    if os.path.lexists(receipt.destination):
        raise RuntimeError(
            "adaptive R2 history output appeared during orphan recovery"
        )
    try:
        staged_payload = receipt.source.read_bytes()
    except OSError as exc:
        raise RuntimeError(
            "cannot audit orphaned adaptive R2 history staging file"
        ) from exc
    if staged_payload != expected_payload:
        raise RuntimeError(
            "orphaned adaptive R2 history staging bytes are not canonical"
        )

    _unlink_verified_r2_history_staging(receipt, required=True)
    if os.path.lexists(receipt.destination):
        raise RuntimeError(
            "adaptive R2 history output appeared during orphan recovery"
        )
    try:
        proof.unlink()
    except OSError as exc:
        raise RuntimeError(
            "cannot clean orphaned adaptive R2 history publication proof"
        ) from exc
    if os.path.lexists(proof) or os.path.lexists(receipt.destination):
        raise RuntimeError(
            "adaptive R2 history orphan recovery did not reach a fresh state"
        )


def initialize_adaptive_r2_history(
    path: Path,
    *,
    failed_decision: Path,
) -> dict[str, Any]:
    """Publish or audit the canonical baseline history for adaptive batch one.

    The history is an independent resumable checkpoint.  Once published, it
    intentionally remains valid if later adaptive-plan validation fails; an
    exact rerun audits and reuses the same bytes.
    """

    output = path.resolve(strict=False)
    decision = failed_decision.resolve(strict=False)
    if output == decision:
        raise ValueError("adaptive R2 history must differ from the failed decision")
    payload = _initial_r2_history_bytes(decision)
    proof = _r2_history_publish_proof_path(output)
    if os.path.lexists(proof):
        if os.path.lexists(output):
            if output.is_symlink() or not output.is_file():
                raise RuntimeError(
                    "proof-bound adaptive R2 history output is not a regular file"
                )
            try:
                proof_bound_payload = output.read_bytes()
            except OSError as exc:
                raise RuntimeError(
                    "cannot audit proof-bound adaptive R2 history output"
                ) from exc
            if proof_bound_payload != payload:
                raise RuntimeError(
                    "proof-bound adaptive R2 history does not match canonical bytes"
                )
            receipt = _r2_history_proof_receipt(
                output,
                proof,
                require_destination=True,
                require_source=False,
            )
            _cleanup_verified_r2_history_publication(receipt, payload)
            if (
                output.is_symlink()
                or not output.is_file()
                or output.read_bytes() != payload
            ):
                raise RuntimeError(
                    "adaptive R2 history changed during proof cleanup"
                )
            return load_adaptive_r2_history(
                output,
                failed_decision=decision,
                batch_index=1,
            )
        receipt = _r2_history_proof_receipt(
            output,
            proof,
            require_destination=False,
            require_source=True,
        )
        _recover_absent_r2_history_publication(receipt, payload)
    if os.path.lexists(output):
        if output.is_symlink() or not output.is_file():
            raise ValueError(f"adaptive R2 history path is not a regular file: {output}")
        try:
            existing = output.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot audit existing adaptive R2 history: {exc}") from exc
        if existing != payload:
            raise ValueError(
                "existing adaptive R2 history does not exactly match the failed decision"
            )
        return load_adaptive_r2_history(
            output,
            failed_decision=decision,
            batch_index=1,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    staged = Path(staged_name)
    receipt: PublishReceipt | None = None
    rollback_unsafe = False
    audit_complete = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        receipt = publish_no_replace(staged, output, proof_path=proof)
        if output.read_bytes() != payload:
            raise RuntimeError("published adaptive R2 history bytes changed")
        loaded = load_adaptive_r2_history(
            output,
            failed_decision=decision,
            batch_index=1,
        )
        audit_complete = True
    except BaseException as exc:
        if receipt is not None and not rollback_owned_output(receipt):
            rollback_unsafe = True
            raise RuntimeError(
                "adaptive R2 history publication failed and rollback was unsafe"
            ) from exc
        raise
    finally:
        if receipt is not None and not rollback_unsafe and not audit_complete:
            cleanup_publish_receipt(receipt)
        if not audit_complete and not os.path.lexists(proof):
            staged.unlink(missing_ok=True)
    if receipt is None:
        raise RuntimeError("adaptive R2 history publication returned no receipt")
    _cleanup_verified_r2_history_publication(receipt, payload)
    return loaded


def load_adaptive_r2_history(
    path: Path,
    *,
    failed_decision: Path,
    batch_index: int,
) -> dict[str, Any]:
    """Validate the ordered, hash-bound R2 history required before a new batch."""

    adaptive_batch_seed(0, batch_index)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read adaptive R2 history {path}: {exc}") from exc
    decoded = foundation._strict_json_bytes(payload, "adaptive R2 history")
    if set(decoded) != {"records", "schema_version"}:
        raise ValueError("adaptive R2 history fields are not exact")
    if decoded.get("schema_version") != R2_HISTORY_SCHEMA_VERSION:
        raise ValueError("adaptive R2 history schema_version is unsupported")
    raw_records = decoded.get("records")
    if not isinstance(raw_records, list) or len(raw_records) != batch_index:
        raise ValueError(
            "adaptive R2 history must contain the baseline plus exactly one record "
            "for each completed earlier adaptive batch"
        )
    normalized: list[dict[str, Any]] = []
    seen_decisions: set[tuple[str, str]] = set()
    for expected_index, raw_record in enumerate(raw_records):
        if not isinstance(raw_record, Mapping) or set(raw_record) != {
            "batch_index",
            "decision",
            "min_primary_r2",
        }:
            raise ValueError(f"adaptive R2 history record {expected_index} fields are not exact")
        if raw_record.get("batch_index") != expected_index:
            raise ValueError("adaptive R2 history batch indexes must be contiguous from zero")
        raw_decision = raw_record.get("decision")
        if not isinstance(raw_decision, Mapping) or set(raw_decision) != {"path", "sha256"}:
            raise ValueError(f"adaptive R2 history record {expected_index} decision is invalid")
        decision_path = Path(str(raw_decision.get("path") or "")).resolve(strict=False)
        foundation._verified_artifact(
            raw_decision,
            f"adaptive R2 history decision {expected_index}",
        )
        actual_decision, actual_minimum = _failed_decision_r2_record(
            decision_path,
            f"adaptive R2 history decision {expected_index}",
        )
        if dict(raw_decision) != actual_decision:
            raise ValueError(
                f"adaptive R2 history decision {expected_index} path or SHA-256 changed"
            )
        try:
            recorded_minimum = float(raw_record.get("min_primary_r2"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"adaptive R2 history record {expected_index} min_primary_r2 is invalid"
            ) from exc
        if not math.isfinite(recorded_minimum) or not math.isclose(
            recorded_minimum,
            actual_minimum,
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            raise ValueError(
                f"adaptive R2 history record {expected_index} min_primary_r2 changed"
            )
        identity = (actual_decision["path"], actual_decision["sha256"])
        if identity in seen_decisions:
            raise ValueError("adaptive R2 history repeats a failed decision")
        seen_decisions.add(identity)
        normalized.append(
            {
                "batch_index": expected_index,
                "decision": actual_decision,
                "min_primary_r2": actual_minimum,
            }
        )
    current_decision, _ = _failed_decision_r2_record(
        failed_decision.resolve(strict=False),
        "current failed decision",
    )
    if normalized[-1]["decision"] != current_decision:
        raise ValueError("the final adaptive R2 history record is not --failed-decision")
    plateau = evaluate_adaptive_plateau(
        normalized[0]["min_primary_r2"],
        [record["min_primary_r2"] for record in normalized[1:]],
    )
    return {
        "artifact": {
            "path": str(path.resolve(strict=False)),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        "plateau": plateau,
        "records": normalized,
    }


def _confirmed_exclusion_contract(paths: Iterable[Path]) -> tuple[set[str], list[dict[str, Any]]]:
    excluded: set[str] = set()
    artifacts: list[dict[str, Any]] = []
    resolved_seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        resolved = path.resolve(strict=False)
        if resolved in resolved_seen:
            raise ValueError(f"duplicate confirmed exclusion CSV: {path}")
        resolved_seen.add(resolved)
        try:
            payload = path.read_bytes()
            hashes = foundation.read_excluded_design_hashes([path])
        except OSError as exc:
            raise ValueError(f"cannot read confirmed exclusion CSV {path}: {exc}") from exc
        if not hashes:
            raise ValueError(f"confirmed exclusion CSV has no design hashes: {path}")
        excluded.update(hashes)
        artifacts.append(
            {
                "design_hashes": len(hashes),
                "path": str(resolved),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return excluded, artifacts


def _fixed_audit_contract(
    path: Path,
    adaptive_evidence: Mapping[str, Any],
) -> dict[str, str]:
    proof = adaptive_evidence.get("proof")
    recorded = proof.get("stage2_audit_case_plan") if isinstance(proof, Mapping) else None
    if not isinstance(recorded, Mapping):
        raise ValueError("adaptive evidence lacks its fixed audit case-plan proof")
    _, _, supplied = foundation._verified_artifact(
        {
            "path": str(path.resolve(strict=False)),
            "sha256": foundation._file_sha256(path),
        },
        "supplied fixed audit case plan",
    )
    if supplied != dict(recorded):
        raise ValueError(
            "--fixed-audit-case-plan differs from the failed gate's sealed audit contract"
        )
    return supplied


def _rename_calibration_rows(
    rows: Iterable[dict[str, Any]],
    *,
    case_prefix: str,
) -> list[dict[str, Any]]:
    grouped = foundation._rows_by_design_hash(rows)
    renamed: list[dict[str, Any]] = []
    for geometry_index, (design_hash, group_rows) in enumerate(grouped.items(), start=1):
        group_id = (
            f"{foundation.safe_name(case_prefix)}_calibration_geometry_"
            f"{geometry_index:04d}_{design_hash[:12]}"
        )
        local_by_point: dict[str, int] = {}
        for source in group_rows:
            point = str(source["operating_point_id"])
            local_by_point[point] = local_by_point.get(point, 0) + 1
            row = dict(source)
            row["case_id"] = (
                f"{foundation.safe_name(case_prefix)}_calibration_{geometry_index:04d}_"
                f"{foundation.safe_name(point)}_{local_by_point[point]:02d}"
            )
            row["geometry_group_id"] = group_id
            row["doe_split"] = "calibration"
            row["repeat_of_case_id"] = ""
            renamed.append(row)
    return renamed


def validate_adaptive_batch_rows(
    rows: list[dict[str, Any]],
    *,
    excluded_design_hashes: Iterable[str],
) -> dict[str, Any]:
    grouped = foundation._rows_by_design_hash(rows)
    excluded = {str(value).strip() for value in excluded_design_hashes if str(value).strip()}
    split_groups = {"train": 0, "calibration": 0, "test": 0}
    split_rows = {"train": 0, "calibration": 0, "test": 0}
    split_hashes = {"train": set(), "calibration": set(), "test": set()}
    failures: list[str] = []
    case_ids = [str(row.get("case_id") or "").strip() for row in rows]
    for design_hash, group_rows in grouped.items():
        splits = {str(row.get("doe_split") or "").strip().lower() for row in group_rows}
        if len(splits) != 1 or next(iter(splits), "") not in split_groups:
            failures.append(f"group_split:{design_hash[:12]}")
            continue
        split = next(iter(splits))
        split_groups[split] += 1
        split_rows[split] += len(group_rows)
        split_hashes[split].add(design_hash)
        if len(group_rows) != ROWS_PER_GEOMETRY:
            failures.append(f"group_rows:{design_hash[:12]}={len(group_rows)}")
    if len(rows) != EXPECTED_ROWS:
        failures.append(f"rows={len(rows)}")
    if len(grouped) != TRAIN_GEOMETRIES + CALIBRATION_GEOMETRIES:
        failures.append(f"groups={len(grouped)}")
    if split_groups != {"train": TRAIN_GEOMETRIES, "calibration": CALIBRATION_GEOMETRIES, "test": 0}:
        failures.append(f"split_groups={split_groups}")
    if split_rows != {"train": 240, "calibration": 60, "test": 0}:
        failures.append(f"split_rows={split_rows}")
    if "" in case_ids or len(case_ids) != len(set(case_ids)):
        failures.append("case_ids_not_unique")
    if excluded & set(grouped):
        failures.append("prior_or_confirmed_design_overlap")
    if split_hashes["train"] & split_hashes["calibration"]:
        failures.append("cross_split_design_overlap")
    if any(str(row.get("repeat_of_case_id") or "").strip() for row in rows):
        failures.append("repeat_rows")
    strict_values = {
        "dataset_schema_version": foundation.DATASET_SCHEMA_VERSION,
        "quality_profile": "reference_ultra",
        "model_extent": foundation.MODEL_EXTENT,
        "beta_convention": foundation.BETA_CONVENTION,
    }
    for row in rows:
        if any(str(row.get(column) or "").strip() != expected for column, expected in strict_values.items()):
            failures.append("strict_identity")
            break
        if not foundation._false_like(row.get("use_periodic_boundary")) or not math.isclose(
            float(row.get("symmetry_factor", math.nan)),
            1.0,
            abs_tol=1e-12,
        ):
            failures.append("strict_extent")
            break
    if failures:
        raise ValueError("invalid adaptive enrichment batch: " + "; ".join(failures))
    return {
        "cross_split_design_overlap": 0,
        "geometry_groups": len(grouped),
        "prior_or_confirmed_design_overlap": 0,
        "repeats": 0,
        "rows": len(rows),
        "split_groups": split_groups,
        "split_rows": split_rows,
    }


def generate_adaptive_batch_rows(
    spec: OptimizationSpec,
    *,
    excluded_design_hashes: Iterable[str],
    adaptive_evidence: Mapping[str, Any],
    batch_index: int,
    case_prefix: str = "v2-adaptive",
    candidate_pool_geometries: int = DEFAULT_CANDIDATE_POOL_GEOMETRIES,
    adaptation_seed_base: int = DEFAULT_ADAPTATION_SEED_BASE,
    calibration_seed_base: int = DEFAULT_CALIBRATION_SEED_BASE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(spec.operating_points) * foundation.STAGE3_SAMPLES_PER_OPERATING_POINT != ROWS_PER_GEOMETRY:
        raise ValueError("adaptive enrichment requires exactly two operating points and three samples per point")
    excluded = {str(value).strip() for value in excluded_design_hashes if str(value).strip()}
    adaptation_seed = adaptive_batch_seed(adaptation_seed_base, batch_index)
    calibration_seed = adaptive_batch_seed(calibration_seed_base, batch_index)
    if adaptation_seed == calibration_seed:
        raise ValueError("adaptive training and calibration seeds must be distinct")
    prefix_base = foundation.safe_name(case_prefix).strip("_")
    if not prefix_base:
        raise ValueError("adaptive case prefix must contain at least one safe character")
    batch_prefix = f"{prefix_base}_batch_{batch_index:04d}"
    train_rows, adaptation = foundation.select_stage3_adaptive_train_rows(
        spec,
        excluded_design_hashes=excluded,
        adaptive_evidence=adaptive_evidence,
        adaptation_seed=adaptation_seed,
        candidate_pool_geometries=candidate_pool_geometries,
        case_prefix=batch_prefix,
        train_geometries=TRAIN_GEOMETRIES,
    )
    train_hashes = set(foundation._rows_by_design_hash(train_rows))
    raw_calibration = foundation.generate_foundation_rows(
        spec,
        geometry_count=CALIBRATION_GEOMETRIES,
        samples_per_operating_point=foundation.STAGE3_SAMPLES_PER_OPERATING_POINT,
        repeat_count=0,
        seed=calibration_seed,
        quality_profile="reference_ultra",
        case_prefix=f"{batch_prefix}_calibration_pool",
        excluded_design_hashes=excluded | train_hashes,
    )
    calibration_rows = _rename_calibration_rows(
        raw_calibration,
        case_prefix=batch_prefix,
    )
    calibration_hashes = list(foundation._rows_by_design_hash(calibration_rows))
    rows = [*train_rows, *calibration_rows]
    validate_adaptive_batch_rows(rows, excluded_design_hashes=excluded)
    return rows, {
        "adaptation": adaptation,
        "batch_index": batch_index,
        "case_prefix": batch_prefix,
        "calibration": {
            "design_hashes": calibration_hashes,
            "geometry_count": CALIBRATION_GEOMETRIES,
            "seed": calibration_seed,
            "split_groups": {"calibration": CALIBRATION_GEOMETRIES},
        },
        "candidate_pool_geometries": candidate_pool_geometries,
        "fixed_audit_policy": "reuse_sealed_stage3_test_without_new_test_rows",
        "seed_policy": {
            "adaptation_seed": adaptation_seed,
            "adaptation_seed_base": adaptation_seed_base,
            "calibration_seed": calibration_seed,
            "calibration_seed_base": calibration_seed_base,
            "formula": "role_seed_base + 100 * batch_index",
            "stride": SEED_STRIDE,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--failed-decision", type=Path, required=True)
    parser.add_argument("--fixed-audit-case-plan", type=Path, required=True)
    parser.add_argument("--r2-history", type=Path, required=True)
    parser.add_argument(
        "--initialize-r2-history",
        action="store_true",
        help=(
            "With --write and --batch-index 1 only, atomically publish or audit "
            "the canonical baseline R2 history from --failed-decision."
        ),
    )
    parser.add_argument(
        "--exclude-case-plan",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--confirmed-exclusion-csv", type=Path, action="append", default=[])
    parser.add_argument("--batch-index", type=int, required=True)
    parser.add_argument("--case-prefix", default="v2-adaptive")
    parser.add_argument(
        "--candidate-pool-geometries",
        type=int,
        default=DEFAULT_CANDIDATE_POOL_GEOMETRIES,
    )
    parser.add_argument("--adaptation-seed-base", type=int, default=DEFAULT_ADAPTATION_SEED_BASE)
    parser.add_argument("--calibration-seed-base", type=int, default=DEFAULT_CALIBRATION_SEED_BASE)
    parser.add_argument("--write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved_plan = args.output.resolve(strict=False)
    resolved_manifest = args.manifest_output.resolve(strict=False)
    resolved_history = args.r2_history.resolve(strict=False)
    plan_proof = foundation.stage3_publish_proof_path(args.output).resolve(strict=False)
    manifest_proof = foundation.stage3_publish_proof_path(
        args.manifest_output
    ).resolve(strict=False)
    history_proof = _r2_history_publish_proof_path(resolved_history)
    reserved_paths = (
        ("adaptive plan", resolved_plan),
        ("adaptive plan proof", plan_proof),
        ("adaptive manifest", resolved_manifest),
        ("adaptive manifest proof", manifest_proof),
        ("adaptive R2 history", resolved_history),
        ("adaptive R2 history proof", history_proof),
    )
    for index, (left_label, left) in enumerate(reserved_paths):
        for right_label, right in reserved_paths[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise SystemExit(
                    "reserved adaptive artifact paths must be distinct and non-nested: "
                    f"{left_label} conflicts with {right_label}"
                )
    if not args.initialize_r2_history and os.path.lexists(history_proof):
        raise SystemExit(
            "adaptive R2 history publication proof requires "
            "--initialize-r2-history recovery"
        )
    if args.initialize_r2_history and not args.write:
        raise SystemExit("--initialize-r2-history requires --write")
    if args.initialize_r2_history and args.batch_index != 1:
        raise SystemExit("--initialize-r2-history is allowed only for --batch-index 1")
    if args.write:
        foundation.recover_stage3_pair(args.output, args.manifest_output)
    elif args.output.exists() or args.manifest_output.exists():
        raise SystemExit("dry-run adaptive output paths must be fresh")
    try:
        spec_bytes = args.spec.read_bytes()
        spec = optimization_spec_from_mapping(
            foundation._strict_json_bytes(spec_bytes, "optimization spec")
        )
        prior_excluded, source_case_plans = foundation.stage3_exclusion_contract(
            args.exclude_case_plan
        )
        confirmed_excluded, confirmed_artifacts = _confirmed_exclusion_contract(
            args.confirmed_exclusion_csv
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    source_calibration_id = str(source_case_plans[0]["beta_calibration_id"])
    source_electrical_zero = float(source_case_plans[0]["electrical_zero_deg"])
    if source_calibration_id != spec.beta_calibration.calibration_id or not math.isclose(
        source_electrical_zero,
        spec.beta_calibration.electrical_zero_deg,
        abs_tol=1e-12,
    ):
        raise SystemExit("adaptive spec beta calibration does not match the source plans")
    if args.initialize_r2_history:
        try:
            initialize_adaptive_r2_history(
                args.r2_history,
                failed_decision=args.failed_decision,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
    try:
        r2_history = load_adaptive_r2_history(
            args.r2_history,
            failed_decision=args.failed_decision,
            batch_index=args.batch_index,
        )
        if r2_history["plateau"]["stop_fea"] is True:
            raise ValueError(
                "adaptive R2 plateau reached: two consecutive completed batches improved "
                "minimum primary R2 by less than 0.01; stop FEA and diagnose model/physics"
            )
        evidence = foundation.load_stage3_adaptive_evidence(
            args.failed_decision,
            source_case_plans,
        )
        fixed_audit = _fixed_audit_contract(args.fixed_audit_case_plan, evidence)
        decision_proof = evidence["proof"]["decision"]
        current_history_decision = r2_history["records"][-1]["decision"]
        if {
            "path": str(decision_proof.get("path") or ""),
            "sha256": str(decision_proof.get("sha256") or ""),
        } != current_history_decision:
            raise ValueError(
                "adaptive R2 history current decision differs from failed-gate evidence"
            )
        excluded = prior_excluded | confirmed_excluded
        rows, selection = generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=excluded,
            adaptive_evidence=evidence,
            batch_index=args.batch_index,
            case_prefix=args.case_prefix,
            candidate_pool_geometries=args.candidate_pool_geometries,
            adaptation_seed_base=args.adaptation_seed_base,
            calibration_seed_base=args.calibration_seed_base,
        )
        summary = validate_adaptive_batch_rows(rows, excluded_design_hashes=excluded)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    plan_bytes = foundation._stage3_csv_bytes(rows, spec)
    plan_contract = {
        "path": str(args.output.resolve(strict=False)),
        "sha256": hashlib.sha256(plan_bytes).hexdigest(),
    }
    spec_hash = hashlib.sha256(spec_bytes).hexdigest()
    execution_contract = {
        "batch_index": args.batch_index,
        "case_plan": plan_contract,
        "failed_decision": r2_history["records"][-1]["decision"],
        "fixed_audit_case_plan": fixed_audit,
        "plateau_policy": r2_history["plateau"],
        "r2_history": r2_history["artifact"],
        "seed_policy": selection["seed_policy"],
    }
    manifest: dict[str, Any] = {
        "case_plan": plan_contract["path"],
        "case_plan_sha256": plan_contract["sha256"],
        "confirmed_exclusions": confirmed_artifacts,
        "excluded_design_hashes": len(excluded),
        "excluded_design_hashes_sha256": foundation._canonical_sha256(sorted(excluded)),
        "failed_gate_evidence": evidence["proof"],
        "fixed_audit_case_plan": fixed_audit,
        "execution_contract": execution_contract,
        "execution_contract_sha256": foundation._canonical_sha256(execution_contract),
        "mode": "write" if args.write else "dry-run",
        "schema_version": SCHEMA_VERSION,
        "selection": selection,
        "r2_history": r2_history,
        "source_case_plans": source_case_plans,
        "spec": {"path": str(args.spec.resolve(strict=False)), "sha256": spec_hash},
        "summary": summary,
    }
    if hashlib.sha256(args.spec.read_bytes()).hexdigest() != spec_hash:
        raise RuntimeError("optimization spec changed during adaptive generation")
    immutable_artifacts = [
        *source_case_plans,
        *confirmed_artifacts,
        fixed_audit,
        r2_history["artifact"],
        *(record["decision"] for record in r2_history["records"]),
    ]
    for artifact in immutable_artifacts:
        if foundation._file_sha256(Path(artifact["path"])) != artifact["sha256"]:
            raise RuntimeError("adaptive immutable evidence changed during generation")
    if args.write:
        foundation.publish_stage3_pair(
            args.output,
            args.manifest_output,
            plan_bytes,
            manifest,
            schema_version=SCHEMA_VERSION,
        )
    print(json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
