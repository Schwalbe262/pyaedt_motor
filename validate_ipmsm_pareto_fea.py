"""Strictly compare Pareto FEA validation results with surrogate bounds.

This command is intentionally fail-closed.  It accepts only the canonical FEA
case-plan schema emitted by :mod:`optimize_ipmsm_nsga2`, requires exact ordered
result coverage, validates physical identities, and then evaluates hard design
constraints plus one-sided surrogate-bound coverage.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
from typing import Any, Mapping, Sequence

from ipmsm_optimization import (
    BETA_CONVENTION,
    OptimizationSpec,
    OptimizationSpecError,
    active_volume_m3,
    geometry_metrics,
    optimization_spec_from_mapping,
    phase_resistance_100c_ohm,
)
from ipmsm_surrogate_bundle import (
    FEATURE_BOUNDS_SOURCE,
    METADATA_FILENAME,
    MIN_OPTIMIZER_R2,
    PRIMARY_R2_TARGETS,
    V2_TRAINING_SCHEMA,
)
from optimize_ipmsm_nsga2 import (
    FEA_DATASET_SCHEMA_VERSION,
    FEA_MODEL_EXTENT,
    REFERENCE_FEA_QUALITY_PROFILE,
    fea_case_fieldnames,
    pareto_fieldnames,
)


SUMMARY_SCHEMA_VERSION = "ipmsm_pareto_fea_validation_v2"
ROW_SCHEMA_VERSION = "ipmsm_pareto_fea_validation_row_v1"
DEFAULT_MINIMUM_COVERAGE = 0.8
DEFAULT_IDENTITY_RELATIVE_TOLERANCE = 1e-6
NUMERIC_RELATIVE_TOLERANCE = 1e-10
NUMERIC_ABSOLUTE_TOLERANCE = 1e-12
COMPARISON_ABSOLUTE_TOLERANCE = 1e-9

OPTIMIZATION_RUN_ID_FIELD = "optimization_run_id"
PARETO_SHA256_FIELD = "pareto_sha256"
OPTIMIZATION_SPEC_SHA256_FIELD = "optimization_spec_sha256"
SURROGATE_METADATA_SHA256_FIELD = "surrogate_metadata_sha256"
SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD = "surrogate_model_artifacts_sha256"
SURROGATE_VERIFICATION_FIELD = "surrogate_verification"
PROVENANCE_FIELDS = (
    OPTIMIZATION_RUN_ID_FIELD,
    PARETO_SHA256_FIELD,
    OPTIMIZATION_SPEC_SHA256_FIELD,
    SURROGATE_METADATA_SHA256_FIELD,
    SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD,
    SURROGATE_VERIFICATION_FIELD,
)
STRICT_SURROGATE_VERIFICATION = "STRICT_V2_FINGERPRINT_VERIFIED"
OPTIMIZATION_RUN_ID_PREFIX = "ipmsm-optimization-run:sha256:"

MODEL_FINGERPRINT_COLUMNS = (
    "input_dataset_schema_version",
    "input_setup_fingerprint",
    "input_quality_profile",
    "input_material_fingerprint",
    "input_aedt_version",
    "input_beta_calibration_id",
    "input_beta_convention",
    "input_model_extent",
)

RESULT_REQUIRED_COLUMNS = (
    "case_id",
    "status",
    "geometry_group_id",
    "design_hash",
    "doe_split",
    "repeat_of_case_id",
    "optimization_run_id",
    "beta_calibration_id",
    "candidate_id",
    "operating_point_id",
    "control_source",
    "execution_host",
    "missing_required_outputs",
    "input_dataset_schema_version",
    "input_model_extent",
    "input_symmetry_factor",
    "input_use_periodic_boundary",
    "input_beta_convention",
    "input_electrical_zero_deg",
    "input_beta_calibration_id",
    "input_quality_profile",
    "input_setup_fingerprint",
    "input_material_fingerprint",
    "input_aedt_version",
    "input_geometry_mode",
    "input_operation",
    "input_base_rpm",
    "input_i_peak_a",
    "input_beta_dq_deg",
    "input_phase_resistance_ohm",
    "input_vdc_v",
    "input_series_turns_per_phase",
    "input_turns_per_coil_side",
    "output_torque_last_avg_nm",
    "output_coreloss_last_avg_w",
    "output_solidloss_last_avg_w",
    "output_copperloss_last_avg_w",
    "output_phase_current_source",
    "output_phase_voltage_source",
    "output_phase_current_last_rms_a",
    "output_phasea_voltage_last_peak_abs_v",
    "output_phaseb_voltage_last_peak_abs_v",
    "output_phasec_voltage_last_peak_abs_v",
    "output_phase_voltage_last_peak_abs_v",
    "output_total_loss_last_avg_w",
    "output_efficiency_last_pct",
)

ROW_FIELDNAMES = (
    "validation_id",
    "row_schema_version",
    "row_index",
    "case_id",
    "candidate_id",
    "operating_point_id",
    "target_kind",
    "target_value",
    "actual_torque_nm",
    "actual_power_w",
    "actual_voltage_peak_v",
    "voltage_limit_v",
    "actual_total_loss_w",
    "actual_efficiency_pct",
    "surrogate_torque_lcb_nm",
    "surrogate_voltage_peak_ucb_v",
    "surrogate_total_loss_ucb_w",
    "torque_lcb_covered",
    "voltage_ucb_covered",
    "total_loss_ucb_covered",
    "torque_lcb_relative_error",
    "voltage_ucb_relative_error",
    "total_loss_ucb_relative_error",
    "target_margin",
    "voltage_margin_v",
    "hard_constraints_passed",
    "case_binding_hash",
)


class ParetoFEAValidationError(ValueError):
    """Raised when the comparison inputs violate their strict contract."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def canonical_hash(namespace: str, payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{namespace}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ParetoFEAValidationError(f"cannot hash required input {path}: {exc}") from exc
    return digest.hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _model_artifact_hashes(metadata: Mapping[str, Any], model_root: Path) -> dict[str, str]:
    model_paths = metadata.get("model_paths")
    if not isinstance(model_paths, Mapping):
        raise ParetoFEAValidationError("surrogate metadata model_paths must be an object")
    hashes: dict[str, str] = {}
    for target in sorted(model_paths):
        recorded = model_paths[target]
        if isinstance(recorded, str):
            values = [recorded]
        elif isinstance(recorded, Sequence) and not isinstance(recorded, (str, bytes)):
            values = list(recorded)
        else:
            raise ParetoFEAValidationError(
                f"surrogate metadata model_paths.{target} must be a model path or an array of model paths"
            )
        if not values or any(not isinstance(value, str) or not value for value in values):
            raise ParetoFEAValidationError(
                f"surrogate metadata model_paths.{target} must contain nonempty model paths"
            )
        for index, value in enumerate(values):
            artifact = model_root / Path(value).name
            if not artifact.is_file():
                raise ParetoFEAValidationError(
                    f"surrogate model artifact is missing for {target}: {artifact}"
                )
            hashes[f"{target}[{index}]::{artifact.name}"] = _sha256_file(artifact)
    return hashes


def _expected_optimization_provenance(
    *,
    spec_sha256: str,
    metadata_sha256: str,
    pareto_sha256: str,
    artifacts_sha256: str,
) -> dict[str, str]:
    identity = {
        PARETO_SHA256_FIELD: pareto_sha256,
        OPTIMIZATION_SPEC_SHA256_FIELD: spec_sha256,
        SURROGATE_METADATA_SHA256_FIELD: metadata_sha256,
        SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: artifacts_sha256,
        SURROGATE_VERIFICATION_FIELD: STRICT_SURROGATE_VERIFICATION,
    }
    return {
        OPTIMIZATION_RUN_ID_FIELD: OPTIMIZATION_RUN_ID_PREFIX
        + _canonical_json_sha256(identity),
        **identity,
    }


def _metadata_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ParetoFEAValidationError(f"surrogate metadata {label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ParetoFEAValidationError(
            f"surrogate metadata {label} must be a finite number"
        ) from exc
    if not math.isfinite(number):
        raise ParetoFEAValidationError(f"surrogate metadata {label} must be a finite number")
    return number


def read_spec(path: Path) -> tuple[OptimizationSpec, dict[str, Any], str]:
    if not path.is_file():
        raise ParetoFEAValidationError(f"optimization spec must be an existing file: {path}")
    try:
        decoded = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ParetoFEAValidationError(f"cannot read strict optimization spec {path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ParetoFEAValidationError("optimization spec root must be an object")
    try:
        spec = optimization_spec_from_mapping(decoded)
    except OptimizationSpecError as exc:
        raise ParetoFEAValidationError(str(exc)) from exc
    return spec, decoded, canonical_hash("ipmsm-optimization-spec", decoded)


def read_model_metadata(
    path: Path,
    spec: OptimizationSpec,
) -> tuple[dict[str, Any], dict[str, str], str]:
    if not path.is_file():
        raise ParetoFEAValidationError(f"surrogate model metadata must be an existing file: {path}")
    try:
        decoded = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ParetoFEAValidationError(f"cannot read strict surrogate metadata {path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ParetoFEAValidationError("surrogate metadata root must be an object")
    if decoded.get("training_schema") != V2_TRAINING_SCHEMA:
        raise ParetoFEAValidationError(
            f"surrogate metadata training_schema must be {V2_TRAINING_SCHEMA!r}"
        )
    if decoded.get("feature_bounds_source") != FEATURE_BOUNDS_SOURCE:
        raise ParetoFEAValidationError(
            f"surrogate metadata feature_bounds_source must be {FEATURE_BOUNDS_SOURCE!r}"
        )
    r2_threshold = _metadata_number(decoded.get("r2_threshold"), "r2_threshold")
    voltage_threshold = _metadata_number(
        decoded.get("voltage_r2_threshold"),
        "voltage_r2_threshold",
    )
    voltage_r2 = _metadata_number(decoded.get("voltage_test_r2"), "voltage_test_r2")
    if r2_threshold < MIN_OPTIMIZER_R2:
        raise ParetoFEAValidationError(
            f"surrogate metadata r2_threshold must be >= {MIN_OPTIMIZER_R2}"
        )
    if decoded.get("primary_test_r2_gate_complete") is not True or decoded.get(
        "primary_test_r2_gate_passed"
    ) is not True:
        raise ParetoFEAValidationError("surrogate metadata primary R2 gate must be complete and passed")
    primary = decoded.get("primary_test_r2")
    if not isinstance(primary, Mapping):
        raise ParetoFEAValidationError("surrogate metadata primary_test_r2 must be an object")
    for target in PRIMARY_R2_TARGETS:
        try:
            raw_value = primary[target]
        except KeyError as exc:
            raise ParetoFEAValidationError(
                f"surrogate metadata primary_test_r2 is missing finite target {target!r}"
            ) from exc
        value = _metadata_number(raw_value, f"primary_test_r2.{target}")
        if value < MIN_OPTIMIZER_R2:
            raise ParetoFEAValidationError(
                f"surrogate metadata primary_test_r2.{target} must be >= {MIN_OPTIMIZER_R2}"
            )
    if decoded.get("voltage_test_r2_gate_complete") is not True or decoded.get(
        "voltage_test_r2_gate_passed"
    ) is not True:
        raise ParetoFEAValidationError("surrogate metadata voltage R2 gate must be complete and passed")
    if voltage_r2 < max(voltage_threshold, MIN_OPTIMIZER_R2):
        raise ParetoFEAValidationError("surrogate metadata voltage_test_r2 does not pass its strict gate")

    raw_fingerprints = decoded.get("fingerprints")
    if not isinstance(raw_fingerprints, Mapping):
        raise ParetoFEAValidationError("surrogate metadata fingerprints must be an object")
    fingerprints: dict[str, str] = {}
    for column in MODEL_FINGERPRINT_COLUMNS:
        value = str(raw_fingerprints.get(column) or "").strip()
        if not value:
            raise ParetoFEAValidationError(
                f"surrogate metadata fingerprints is missing nonblank {column}"
            )
        fingerprints[column] = value
    expected = {
        "input_dataset_schema_version": FEA_DATASET_SCHEMA_VERSION,
        "input_quality_profile": REFERENCE_FEA_QUALITY_PROFILE,
        "input_beta_calibration_id": spec.beta_calibration.calibration_id,
        "input_beta_convention": BETA_CONVENTION,
        "input_model_extent": FEA_MODEL_EXTENT,
    }
    mismatches = [
        f"{column}: expected={value!r} actual={fingerprints[column]!r}"
        for column, value in expected.items()
        if fingerprints[column] != value
    ]
    if mismatches:
        raise ParetoFEAValidationError(
            "surrogate metadata fingerprints do not match optimization spec: " + "; ".join(mismatches)
        )
    _fingerprint(fingerprints["input_setup_fingerprint"], "setup_v2", "model setup fingerprint")
    _fingerprint(
        fingerprints["input_material_fingerprint"],
        "materials_v2",
        "model material fingerprint",
    )
    if fingerprints["input_aedt_version"].lower() in {"auto", "unknown"}:
        raise ParetoFEAValidationError("surrogate metadata has unknown input_aedt_version")
    return decoded, fingerprints, canonical_hash("ipmsm-surrogate-metadata", decoded)


def read_csv(path: Path, label: str) -> tuple[list[str], list[dict[str, str]], str]:
    if not path.is_file():
        raise ParetoFEAValidationError(f"{label} must be an existing file: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = list(reader.fieldnames or ())
            if not fieldnames or any(not str(name or "").strip() for name in fieldnames):
                raise ParetoFEAValidationError(f"{label} must have a nonblank CSV header")
            if len(fieldnames) != len(set(fieldnames)):
                raise ParetoFEAValidationError(f"{label} has duplicate CSV header names")
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ParetoFEAValidationError(f"cannot read {label} {path}: {exc}") from exc
    if not rows:
        raise ParetoFEAValidationError(f"{label} must contain at least one row")
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ParetoFEAValidationError(f"{label} has fields beyond its header or missing cells")
    digest = canonical_hash(
        "ipmsm-pareto-fea-csv",
        {"label": label, "fieldnames": fieldnames, "rows": rows},
    )
    return fieldnames, rows, digest


def _text(row: Mapping[str, Any], column: str, label: str) -> str:
    value = str(row.get(column) or "").strip()
    if not value:
        raise ParetoFEAValidationError(f"{label} has blank {column}")
    return value


def _finite(row: Mapping[str, Any], column: str, label: str) -> float:
    try:
        value = float(row.get(column))
    except (TypeError, ValueError) as exc:
        raise ParetoFEAValidationError(f"{label} has invalid {column}") from exc
    if not math.isfinite(value):
        raise ParetoFEAValidationError(f"{label} has non-finite {column}")
    return value


def _nonnegative(row: Mapping[str, Any], column: str, label: str) -> float:
    value = _finite(row, column, label)
    if value < 0.0:
        raise ParetoFEAValidationError(f"{label} has negative {column}")
    return value


def _false_like(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _strict_bool(value: Any, label: str) -> bool:
    text = str(value or "").strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise ParetoFEAValidationError(f"{label} must be an explicit boolean")


def _equivalent(expected: Any, actual: Any) -> bool:
    expected_text = "" if expected is None else str(expected).strip()
    actual_text = "" if actual is None else str(actual).strip()
    if expected_text.lower() in {"true", "false"} or actual_text.lower() in {"true", "false"}:
        return expected_text.lower() == actual_text.lower()
    try:
        expected_number = float(expected_text)
        actual_number = float(actual_text)
    except (TypeError, ValueError):
        return expected_text == actual_text
    return math.isfinite(expected_number) and math.isfinite(actual_number) and math.isclose(
        expected_number,
        actual_number,
        rel_tol=NUMERIC_RELATIVE_TOLERANCE,
        abs_tol=NUMERIC_ABSOLUTE_TOLERANCE,
    )


def _close_identity(actual: float, expected: float, relative_tolerance: float) -> bool:
    return math.isclose(
        actual,
        expected,
        rel_tol=relative_tolerance,
        abs_tol=COMPARISON_ABSOLUTE_TOLERANCE,
    )


def _comparison_tolerance(reference: float) -> float:
    return max(COMPARISON_ABSOLUTE_TOLERANCE, abs(reference) * NUMERIC_RELATIVE_TOLERANCE)


def _fingerprint(value: Any, namespace: str, label: str) -> str:
    text = str(value or "").strip()
    prefix = f"{namespace}:sha256:"
    digest = text[len(prefix) :] if text.startswith(prefix) else ""
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ParetoFEAValidationError(f"{label} must be a canonical {namespace} SHA-256 fingerprint")
    return text


def _plain_sha256(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ParetoFEAValidationError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _candidate_design_hash(design: Mapping[str, float]) -> str:
    encoded = json.dumps(
        {key: float(value) for key, value in sorted(design.items())},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_column_name(value: str) -> str:
    return "".join(character if character.isalnum() or character == "_" else "_" for character in value)


def validate_case_plan(
    spec: OptimizationSpec,
    fieldnames: Sequence[str],
    rows: Sequence[dict[str, str]],
    expected_provenance: Mapping[str, str],
) -> list[str]:
    expected_header = fea_case_fieldnames(spec)
    if list(fieldnames) != expected_header:
        raise ParetoFEAValidationError("FEA case-plan header does not exactly match the generated canonical schema")

    calibration_id = _fingerprint(
        spec.beta_calibration.calibration_id,
        "beta-calibration",
        "spec.beta_calibration.calibration_id",
    )
    point_by_name = {point.name: point for point in spec.operating_points}
    point_order = [point.name for point in spec.operating_points]
    seen_cases: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    candidate_order: list[str] = []
    grouped: dict[str, list[dict[str, str]]] = {}
    homogeneous_provenance: dict[str, str] | None = None

    for index, row in enumerate(rows, start=1):
        label = f"FEA case-plan row {index}"
        row_provenance = {
            field: _text(row, field, label)
            for field in PROVENANCE_FIELDS
        }
        for field in (
            PARETO_SHA256_FIELD,
            OPTIMIZATION_SPEC_SHA256_FIELD,
            SURROGATE_METADATA_SHA256_FIELD,
            SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD,
        ):
            _plain_sha256(row_provenance[field], f"{label} {field}")
        run_id = row_provenance[OPTIMIZATION_RUN_ID_FIELD]
        run_digest = run_id[len(OPTIMIZATION_RUN_ID_PREFIX) :] if run_id.startswith(
            OPTIMIZATION_RUN_ID_PREFIX
        ) else ""
        _plain_sha256(run_digest, f"{label} {OPTIMIZATION_RUN_ID_FIELD}")
        if row_provenance[SURROGATE_VERIFICATION_FIELD] != STRICT_SURROGATE_VERIFICATION:
            raise ParetoFEAValidationError(
                f"{label} {SURROGATE_VERIFICATION_FIELD} must be strict verified provenance"
            )
        if homogeneous_provenance is None:
            homogeneous_provenance = row_provenance
        elif row_provenance != homogeneous_provenance:
            raise ParetoFEAValidationError("FEA case-plan provenance must be homogeneous on every row")
        for field in (
            PARETO_SHA256_FIELD,
            OPTIMIZATION_SPEC_SHA256_FIELD,
            SURROGATE_METADATA_SHA256_FIELD,
            SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD,
            SURROGATE_VERIFICATION_FIELD,
            OPTIMIZATION_RUN_ID_FIELD,
        ):
            if row_provenance[field] != expected_provenance[field]:
                raise ParetoFEAValidationError(
                    f"{label} {field} does not match independently recomputed strict provenance"
                )
        case_id = _text(row, "case_id", label)
        candidate_id = _text(row, "candidate_id", label)
        point_id = _text(row, "operating_point_id", label)
        if case_id in seen_cases:
            raise ParetoFEAValidationError(f"duplicate FEA case-plan case_id: {case_id!r}")
        if (candidate_id, point_id) in seen_pairs:
            raise ParetoFEAValidationError(
                f"duplicate FEA case-plan candidate/operating-point pair: {candidate_id!r}/{point_id!r}"
            )
        seen_cases.add(case_id)
        seen_pairs.add((candidate_id, point_id))
        if point_id not in point_by_name:
            raise ParetoFEAValidationError(f"{label} references unknown operating_point_id={point_id!r}")
        if case_id != f"{candidate_id}__{point_id}":
            raise ParetoFEAValidationError(f"{label} case_id is not canonical for its candidate/operating point")
        if _text(row, "geometry_group_id", label) != f"optimization_{candidate_id}":
            raise ParetoFEAValidationError(f"{label} geometry_group_id is not canonical")
        required_text = {
            "doe_split": "test",
            "dataset_schema_version": FEA_DATASET_SCHEMA_VERSION,
            "beta_convention": BETA_CONVENTION,
            "beta_calibration_id": calibration_id,
            "model_extent": FEA_MODEL_EXTENT,
            "quality_profile": REFERENCE_FEA_QUALITY_PROFILE,
            "geometry_mode": "fixed",
            "operation": "sin_current",
            "control_source": "surrogate_inner_search",
        }
        for column, expected in required_text.items():
            if _text(row, column, label) != expected:
                raise ParetoFEAValidationError(f"{label} {column} does not match canonical value {expected!r}")
        if str(row.get("repeat_of_case_id") or "").strip():
            raise ParetoFEAValidationError(f"{label} repeat_of_case_id must be blank")
        if not _false_like(row.get("use_periodic_boundary")):
            raise ParetoFEAValidationError(f"{label} must disable periodic boundary")

        expected_numbers = {
            "slot_num": float(spec.slot_number),
            "pole_num": float(spec.pole_number),
            "base_rpm": point_by_name[point_id].speed_rpm,
            "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
            "symmetry_factor": 1.0,
            "vdc_v": spec.inverter.vdc_v,
            "series_turns_per_phase": float(spec.winding.series_turns_per_phase),
            "turns_per_coil_side": float(spec.winding.turns_per_coil_side),
        }
        for column, expected in expected_numbers.items():
            if not _equivalent(expected, row.get(column)):
                raise ParetoFEAValidationError(f"{label} {column} does not match optimization spec")

        current = _nonnegative(row, "i_peak_a", label)
        beta = _finite(row, "beta_dq_deg", label)
        resistance = _nonnegative(row, "phase_resistance_ohm", label)
        if current > spec.effective_peak_current_limit_a + _comparison_tolerance(spec.effective_peak_current_limit_a):
            raise ParetoFEAValidationError(f"{label} exceeds effective peak-current limit")
        if not spec.beta_bounds_deg[0] <= beta <= spec.beta_bounds_deg[1]:
            raise ParetoFEAValidationError(f"{label} beta_dq_deg is outside optimization bounds")
        if resistance <= 0.0:
            raise ParetoFEAValidationError(f"{label} phase_resistance_ohm must be > 0")

        torque_lcb = _finite(row, "surrogate_torque_lcb_nm", label)
        voltage_ucb = _nonnegative(row, "surrogate_voltage_peak_ucb_v", label)
        _nonnegative(row, "surrogate_total_loss_ucb_w", label)
        point = point_by_name[point_id]
        required_torque = point.required_torque_nm
        if torque_lcb + _comparison_tolerance(required_torque) < required_torque:
            raise ParetoFEAValidationError(f"{label} surrogate torque LCB does not satisfy its target")
        voltage_limit = spec.phase_peak_voltage_limit_v
        if voltage_ucb > voltage_limit + _comparison_tolerance(voltage_limit):
            raise ParetoFEAValidationError(f"{label} surrogate voltage UCB exceeds the hard voltage limit")

        if candidate_id not in grouped:
            candidate_order.append(candidate_id)
            grouped[candidate_id] = []
        elif candidate_order[-1] != candidate_id:
            raise ParetoFEAValidationError(f"candidate {candidate_id!r} is not contiguous in FEA case-plan order")
        grouped[candidate_id].append(row)

    for candidate_id in candidate_order:
        candidate_rows = grouped[candidate_id]
        actual_order = [str(row["operating_point_id"]).strip() for row in candidate_rows]
        if actual_order != point_order:
            raise ParetoFEAValidationError(
                f"candidate {candidate_id!r} operating-point order/coverage mismatch: "
                f"expected={point_order!r} actual={actual_order!r}"
            )
        design = {
            bound.name: _finite(candidate_rows[0], bound.name, f"candidate {candidate_id!r}")
            for bound in spec.design_space
        }
        for bound in spec.design_space:
            value = design[bound.name]
            if value < bound.lower or value > bound.upper:
                raise ParetoFEAValidationError(
                    f"candidate {candidate_id!r} design variable {bound.name} is outside spec bounds"
                )
        try:
            metrics = geometry_metrics(
                design,
                design["stack_length_mm"],
                spec.winding,
                slot_number=spec.slot_number,
            )
            expected_resistance = phase_resistance_100c_ohm(
                design,
                design["stack_length_mm"],
                spec.winding,
                slot_number=spec.slot_number,
            )
        except ValueError as exc:
            raise ParetoFEAValidationError(
                f"candidate {candidate_id!r} has invalid derived winding geometry: {exc}"
            ) from exc
        if metrics.slot_fill_ratio > spec.winding.fill_factor + _comparison_tolerance(
            spec.winding.fill_factor
        ):
            raise ParetoFEAValidationError(
                f"candidate {candidate_id!r} exceeds winding slot-fill limit"
            )
        expected_design_hash = _candidate_design_hash(design)
        for row in candidate_rows:
            label = f"candidate {candidate_id!r}/{row['operating_point_id']!r}"
            actual_hash = _plain_sha256(row.get("design_hash"), f"{label} design_hash")
            if actual_hash != expected_design_hash:
                raise ParetoFEAValidationError(f"{label} design_hash does not match canonical design values")
            for name, expected in design.items():
                if not _equivalent(expected, row.get(name)):
                    raise ParetoFEAValidationError(f"{label} changes candidate design variable {name}")
            if not _equivalent(expected_resistance, row.get("phase_resistance_ohm")):
                raise ParetoFEAValidationError(
                    f"{label} phase_resistance_ohm does not match canonical 100C winding calculation"
                )
    return candidate_order


def _require_exact_numeric(
    expected_row: Mapping[str, Any],
    expected_column: str,
    actual_row: Mapping[str, Any],
    actual_column: str,
    label: str,
) -> None:
    expected = _finite(expected_row, expected_column, label)
    actual = _finite(actual_row, actual_column, label)
    if expected != actual:
        raise ParetoFEAValidationError(
            f"{label} {actual_column} does not exactly match Pareto column {expected_column}"
        )


def validate_pareto_front(
    spec: OptimizationSpec,
    fieldnames: Sequence[str],
    rows: Sequence[dict[str, str]],
    plan_rows: Sequence[dict[str, str]],
    candidate_order: Sequence[str],
) -> dict[str, dict[str, str]]:
    if list(fieldnames) != pareto_fieldnames(spec):
        raise ParetoFEAValidationError(
            "Pareto CSV header does not exactly match the generated canonical schema"
        )
    candidates: dict[str, dict[str, str]] = {}
    for index, row in enumerate(rows, start=1):
        label = f"Pareto row {index}"
        candidate_id = _text(row, "candidate_id", label)
        if candidate_id in candidates:
            raise ParetoFEAValidationError(f"duplicate Pareto candidate_id: {candidate_id!r}")
        if not _strict_bool(row.get("feasible"), f"{label} feasible"):
            raise ParetoFEAValidationError(f"{label} is not on a feasible Pareto front")
        violation = _nonnegative(row, "total_constraint_violation", label)
        if violation > _comparison_tolerance(0.0):
            raise ParetoFEAValidationError(f"{label} has nonzero total_constraint_violation")
        for bound in spec.design_space:
            value = _finite(row, bound.name, label)
            if value < bound.lower or value > bound.upper:
                raise ParetoFEAValidationError(
                    f"{label} design variable {bound.name} is outside spec bounds"
                )
        for point in spec.operating_points:
            prefix = _safe_column_name(point.name)
            if _text(row, f"{prefix}_target_kind", label) != point.target_kind:
                raise ParetoFEAValidationError(
                    f"{label} {prefix}_target_kind does not match optimization spec"
                )
            expected_numbers = {
                f"{prefix}_speed_rpm": point.speed_rpm,
                f"{prefix}_required_torque_nm": point.required_torque_nm,
                f"{prefix}_required_power_w": point.required_power_w,
            }
            for column, expected in expected_numbers.items():
                if not _equivalent(expected, row.get(column)):
                    raise ParetoFEAValidationError(f"{label} {column} does not match optimization spec")
            if not _strict_bool(row.get(f"{prefix}_feasible"), f"{label} {prefix}_feasible"):
                raise ParetoFEAValidationError(
                    f"{label} operating point {point.name!r} is not surrogate-feasible"
                )
            point_violation = _nonnegative(row, f"{prefix}_constraint_violation", label)
            if point_violation > _comparison_tolerance(0.0):
                raise ParetoFEAValidationError(
                    f"{label} operating point {point.name!r} has nonzero constraint violation"
                )
            _nonnegative(row, f"{prefix}_current_peak_a", label)
            _finite(row, f"{prefix}_beta_deg", label)
            _finite(row, f"{prefix}_torque_lcb_nm", label)
            _nonnegative(row, f"{prefix}_voltage_peak_ucb_v", label)
            _nonnegative(row, f"{prefix}_total_loss_ucb_w", label)
        candidates[candidate_id] = row

    plan_by_candidate: dict[str, list[dict[str, str]]] = {}
    for row in plan_rows:
        plan_by_candidate.setdefault(str(row["candidate_id"]).strip(), []).append(row)
    for candidate_id in candidate_order:
        if candidate_id not in candidates:
            raise ParetoFEAValidationError(
                f"FEA-selected candidate {candidate_id!r} is missing from the Pareto front"
            )
        pareto_row = candidates[candidate_id]
        selected_rows = plan_by_candidate[candidate_id]
        first_plan = selected_rows[0]
        label = f"FEA-selected candidate {candidate_id!r}"
        for name in spec.design_variable_names:
            _require_exact_numeric(pareto_row, name, first_plan, name, label)
        _require_exact_numeric(
            pareto_row,
            "phase_resistance_100c_ohm",
            first_plan,
            "phase_resistance_ohm",
            label,
        )
        plan_by_point = {
            str(row["operating_point_id"]).strip(): row
            for row in selected_rows
        }
        for point in spec.operating_points:
            prefix = _safe_column_name(point.name)
            plan = plan_by_point[point.name]
            point_label = f"{label}/{point.name!r}"
            for pareto_column, plan_column in (
                (f"{prefix}_current_peak_a", "i_peak_a"),
                (f"{prefix}_beta_deg", "beta_dq_deg"),
                (f"{prefix}_torque_lcb_nm", "surrogate_torque_lcb_nm"),
                (f"{prefix}_voltage_peak_ucb_v", "surrogate_voltage_peak_ucb_v"),
                (f"{prefix}_total_loss_ucb_w", "surrogate_total_loss_ucb_w"),
            ):
                _require_exact_numeric(
                    pareto_row,
                    pareto_column,
                    plan,
                    plan_column,
                    point_label,
                )
    return candidates


def validate_result_contract(
    spec: OptimizationSpec,
    plan_rows: Sequence[dict[str, str]],
    result_fieldnames: Sequence[str],
    result_rows: Sequence[dict[str, str]],
    model_fingerprints: Mapping[str, str],
) -> dict[str, str]:
    missing = [column for column in RESULT_REQUIRED_COLUMNS if column not in result_fieldnames]
    missing.extend(
        f"input_{bound.name}"
        for bound in spec.design_space
        if f"input_{bound.name}" not in result_fieldnames
    )
    if missing:
        raise ParetoFEAValidationError(f"collected FEA results are missing required columns: {missing}")
    if len(plan_rows) != len(result_rows):
        raise ParetoFEAValidationError(
            f"ordered FEA result coverage mismatch: plan_rows={len(plan_rows)} result_rows={len(result_rows)}"
        )
    plan_ids = [str(row["case_id"]).strip() for row in plan_rows]
    result_ids = [str(row.get("case_id") or "").strip() for row in result_rows]
    if len(set(result_ids)) != len(result_ids) or any(not case_id for case_id in result_ids):
        raise ParetoFEAValidationError("collected FEA result case_id values must be unique and nonblank")
    if result_ids != plan_ids:
        raise ParetoFEAValidationError("collected FEA results do not exactly match case-plan order and coverage")

    compare_input_columns = (
        *[bound.name for bound in spec.design_space],
        "slot_num",
        "pole_num",
        "base_rpm",
        "i_peak_a",
        "beta_dq_deg",
        "beta_convention",
        "electrical_zero_deg",
        "beta_calibration_id",
        "model_extent",
        "symmetry_factor",
        "use_periodic_boundary",
        "phase_resistance_ohm",
        "vdc_v",
        "series_turns_per_phase",
        "turns_per_coil_side",
        "quality_profile",
        "geometry_mode",
        "operation",
        "dataset_schema_version",
    )
    metadata_columns = (
        "geometry_group_id",
        "design_hash",
        "doe_split",
        "repeat_of_case_id",
        "optimization_run_id",
        "beta_calibration_id",
        "candidate_id",
        "operating_point_id",
        "control_source",
    )
    fingerprint_values: dict[str, set[str]] = {
        "input_setup_fingerprint": set(),
        "input_material_fingerprint": set(),
        "input_aedt_version": set(),
    }
    for index, (plan, result) in enumerate(zip(plan_rows, result_rows), start=1):
        case_id = str(plan["case_id"]).strip()
        label = f"collected FEA result {index} case_id={case_id!r}"
        if str(result.get("status") or "").strip().lower() != "ok":
            raise ParetoFEAValidationError(f"{label} status must be 'ok'")
        if str(result.get("missing_required_outputs") or "").strip():
            raise ParetoFEAValidationError(f"{label} has missing_required_outputs")
        if str(result.get("execution_host") or "").strip().lower() in {"", "unknown"}:
            raise ParetoFEAValidationError(f"{label} has unknown execution_host")
        for column in metadata_columns:
            if not _equivalent(plan.get(column), result.get(column)):
                raise ParetoFEAValidationError(f"{label} does not match case plan column {column}")
        for column in compare_input_columns:
            result_column = f"input_{column}"
            if not _equivalent(plan.get(column), result.get(result_column)):
                raise ParetoFEAValidationError(f"{label} does not match case plan column {result_column}")
        if str(result.get("input_dataset_schema_version") or "").strip() != FEA_DATASET_SCHEMA_VERSION:
            raise ParetoFEAValidationError(f"{label} has invalid dataset schema")
        if str(result.get("input_quality_profile") or "").strip() != REFERENCE_FEA_QUALITY_PROFILE:
            raise ParetoFEAValidationError(f"{label} is not reference_ultra")
        if str(result.get("input_model_extent") or "").strip() != FEA_MODEL_EXTENT:
            raise ParetoFEAValidationError(f"{label} is not full_360")
        if str(result.get("input_beta_convention") or "").strip() != BETA_CONVENTION:
            raise ParetoFEAValidationError(f"{label} has invalid beta convention")
        if not _false_like(result.get("input_use_periodic_boundary")):
            raise ParetoFEAValidationError(f"{label} enables periodic boundary")
        if not _equivalent(1.0, result.get("input_symmetry_factor")):
            raise ParetoFEAValidationError(f"{label} has invalid symmetry factor")
        if str(result.get("output_phase_current_source") or "") != "measured_three_phase":
            raise ParetoFEAValidationError(f"{label} does not use measured three-phase current")
        if str(result.get("output_phase_voltage_source") or "") != "measured_three_phase":
            raise ParetoFEAValidationError(f"{label} does not use measured three-phase voltage")
        for column in fingerprint_values:
            value = _text(result, column, label)
            fingerprint_values[column].add(value)
        for column in MODEL_FINGERPRINT_COLUMNS:
            actual = _text(result, column, label)
            expected = model_fingerprints[column]
            if actual != expected:
                raise ParetoFEAValidationError(
                    f"{label} {column} does not match surrogate training fingerprint: "
                    f"expected={expected!r} actual={actual!r}"
                )

    for column, values in fingerprint_values.items():
        if len(values) != 1:
            raise ParetoFEAValidationError(f"collected FEA results mix {column}: {sorted(values)!r}")
    setup = _fingerprint(next(iter(fingerprint_values["input_setup_fingerprint"])), "setup_v2", "setup fingerprint")
    material = _fingerprint(
        next(iter(fingerprint_values["input_material_fingerprint"])),
        "materials_v2",
        "material fingerprint",
    )
    aedt_version = next(iter(fingerprint_values["input_aedt_version"]))
    if aedt_version.strip().lower() in {"", "auto", "unknown"}:
        raise ParetoFEAValidationError("collected FEA results have unknown AEDT version")
    return {
        "setup_fingerprint": setup,
        "material_fingerprint": material,
        "aedt_version": aedt_version,
    }


def _relative_error(actual: float, bound: float) -> float:
    return abs(actual - bound) / max(abs(actual), COMPARISON_ABSOLUTE_TOLERANCE)


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def coverage_summary(
    rows: Sequence[dict[str, Any]],
    covered_key: str,
    error_key: str,
    minimum_coverage: float,
) -> dict[str, Any]:
    total = len(rows)
    covered = sum(1 for row in rows if bool(row[covered_key]))
    required = math.ceil(minimum_coverage * total - 1e-12)
    errors = [float(row[error_key]) for row in rows]
    return {
        "covered": covered,
        "total": total,
        "required_covered": required,
        "rate": covered / total,
        "passed": covered >= required,
        "relative_error": {
            "mean": statistics.fmean(errors),
            "median": statistics.median(errors),
            "p90": _percentile(errors, 0.9),
            "max": max(errors),
        },
    }


def assess_rows(
    spec: OptimizationSpec,
    plan_rows: Sequence[dict[str, str]],
    result_rows: Sequence[dict[str, str]],
    *,
    identity_relative_tolerance: float,
    pareto_hash: str,
    case_plan_hash: str,
    results_hash: str,
) -> list[dict[str, Any]]:
    point_by_name = {point.name: point for point in spec.operating_points}
    assessments: list[dict[str, Any]] = []
    for index, (plan, result) in enumerate(zip(plan_rows, result_rows), start=1):
        case_id = str(plan["case_id"]).strip()
        label = f"FEA result case_id={case_id!r}"
        point_id = str(plan["operating_point_id"]).strip()
        point = point_by_name[point_id]
        rpm = _finite(result, "input_base_rpm", label)
        current_peak = _nonnegative(result, "input_i_peak_a", label)
        resistance = _nonnegative(result, "input_phase_resistance_ohm", label)
        phase_rms = _nonnegative(result, "output_phase_current_last_rms_a", label)
        expected_phase_rms = current_peak / math.sqrt(2.0)
        if not _close_identity(phase_rms, expected_phase_rms, identity_relative_tolerance):
            raise ParetoFEAValidationError(f"{label} violates phase-current RMS identity")

        torque = _finite(result, "output_torque_last_avg_nm", label)
        core_loss = _nonnegative(result, "output_coreloss_last_avg_w", label)
        solid_loss = _nonnegative(result, "output_solidloss_last_avg_w", label)
        reported_copper = _nonnegative(result, "output_copperloss_last_avg_w", label)
        expected_copper = 3.0 * resistance * phase_rms * phase_rms
        if not _close_identity(reported_copper, expected_copper, identity_relative_tolerance):
            raise ParetoFEAValidationError(f"{label} violates copper-loss identity")
        actual_total_loss = core_loss + solid_loss + expected_copper
        reported_total_loss = _nonnegative(result, "output_total_loss_last_avg_w", label)
        if not _close_identity(reported_total_loss, actual_total_loss, identity_relative_tolerance):
            raise ParetoFEAValidationError(f"{label} violates total-loss identity")

        actual_power = torque * 2.0 * math.pi * rpm / 60.0
        if actual_power <= 0.0:
            raise ParetoFEAValidationError(f"{label} must produce positive mechanical power")
        actual_efficiency = actual_power / (actual_power + actual_total_loss) * 100.0
        reported_efficiency = _finite(result, "output_efficiency_last_pct", label)
        if not 0.0 <= reported_efficiency <= 100.0:
            raise ParetoFEAValidationError(f"{label} efficiency is outside [0, 100]")
        if not _close_identity(reported_efficiency, actual_efficiency, identity_relative_tolerance):
            raise ParetoFEAValidationError(f"{label} violates efficiency identity")

        phase_voltages = [
            _nonnegative(result, f"output_phase{phase}_voltage_last_peak_abs_v", label)
            for phase in ("a", "b", "c")
        ]
        actual_voltage = _nonnegative(result, "output_phase_voltage_last_peak_abs_v", label)
        if not _close_identity(actual_voltage, max(phase_voltages), identity_relative_tolerance):
            raise ParetoFEAValidationError(f"{label} violates phase-voltage envelope identity")

        torque_lcb = _finite(plan, "surrogate_torque_lcb_nm", label)
        voltage_ucb = _nonnegative(plan, "surrogate_voltage_peak_ucb_v", label)
        loss_ucb = _nonnegative(plan, "surrogate_total_loss_ucb_w", label)
        if point.target_kind == "torque":
            target_value = point.required_torque_nm
            target_margin = torque - target_value
        else:
            target_value = point.required_power_w
            target_margin = actual_power - target_value
        voltage_limit = spec.phase_peak_voltage_limit_v
        voltage_margin = voltage_limit - actual_voltage
        target_passed = target_margin >= -_comparison_tolerance(target_value)
        voltage_passed = voltage_margin >= -_comparison_tolerance(voltage_limit)
        assessment: dict[str, Any] = {
            "row_schema_version": ROW_SCHEMA_VERSION,
            "row_index": index,
            "case_id": case_id,
            "candidate_id": str(plan["candidate_id"]).strip(),
            "operating_point_id": point_id,
            "target_kind": point.target_kind,
            "target_value": target_value,
            "actual_torque_nm": torque,
            "actual_power_w": actual_power,
            "actual_voltage_peak_v": actual_voltage,
            "voltage_limit_v": voltage_limit,
            "actual_total_loss_w": actual_total_loss,
            "actual_efficiency_pct": actual_efficiency,
            "surrogate_torque_lcb_nm": torque_lcb,
            "surrogate_voltage_peak_ucb_v": voltage_ucb,
            "surrogate_total_loss_ucb_w": loss_ucb,
            "torque_lcb_covered": torque + _comparison_tolerance(torque_lcb) >= torque_lcb,
            "voltage_ucb_covered": actual_voltage <= voltage_ucb + _comparison_tolerance(voltage_ucb),
            "total_loss_ucb_covered": actual_total_loss <= loss_ucb + _comparison_tolerance(loss_ucb),
            "torque_lcb_relative_error": _relative_error(torque, torque_lcb),
            "voltage_ucb_relative_error": _relative_error(actual_voltage, voltage_ucb),
            "total_loss_ucb_relative_error": _relative_error(actual_total_loss, loss_ucb),
            "target_margin": target_margin,
            "voltage_margin_v": voltage_margin,
            "hard_constraints_passed": target_passed and voltage_passed,
        }
        assessment["case_binding_hash"] = canonical_hash(
            "ipmsm-pareto-fea-row",
            {
                "pareto_hash": pareto_hash,
                "case_plan_hash": case_plan_hash,
                "results_hash": results_hash,
                "row_index": index,
                "plan_row": plan,
                "result_row": result,
                "assessment": assessment,
            },
        )
        assessments.append(assessment)
    return assessments


def validate_pareto_fea(
    spec_path: Path,
    model_metadata_path: Path,
    pareto_path: Path,
    case_plan_path: Path,
    results_path: Path,
    *,
    minimum_coverage: float = DEFAULT_MINIMUM_COVERAGE,
    identity_relative_tolerance: float = DEFAULT_IDENTITY_RELATIVE_TOLERANCE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not math.isfinite(minimum_coverage) or not 0.0 < minimum_coverage <= 1.0:
        raise ParetoFEAValidationError("minimum_coverage must be finite and in (0, 1]")
    if not math.isfinite(identity_relative_tolerance) or identity_relative_tolerance < 0.0:
        raise ParetoFEAValidationError("identity_relative_tolerance must be finite and >= 0")

    spec, _spec_mapping, spec_hash = read_spec(spec_path)
    model_metadata, model_fingerprints, model_metadata_hash = read_model_metadata(
        model_metadata_path,
        spec,
    )
    spec_sha256 = _sha256_file(spec_path)
    model_metadata_sha256 = _sha256_file(model_metadata_path)
    artifact_hashes = _model_artifact_hashes(model_metadata, model_metadata_path.parent)
    model_artifacts_sha256 = _canonical_json_sha256(artifact_hashes)
    pareto_fields, pareto_rows, pareto_hash = read_csv(pareto_path, "Pareto front")
    pareto_sha256 = _sha256_file(pareto_path)
    expected_provenance = _expected_optimization_provenance(
        spec_sha256=spec_sha256,
        metadata_sha256=model_metadata_sha256,
        pareto_sha256=pareto_sha256,
        artifacts_sha256=model_artifacts_sha256,
    )
    plan_fields, plan_rows, plan_hash = read_csv(case_plan_path, "FEA case plan")
    result_fields, result_rows, results_hash = read_csv(results_path, "collected FEA results")
    candidate_order = validate_case_plan(spec, plan_fields, plan_rows, expected_provenance)
    pareto_candidates = validate_pareto_front(
        spec,
        pareto_fields,
        pareto_rows,
        plan_rows,
        candidate_order,
    )
    fingerprints = validate_result_contract(
        spec,
        plan_rows,
        result_fields,
        result_rows,
        model_fingerprints,
    )
    assessments = assess_rows(
        spec,
        plan_rows,
        result_rows,
        identity_relative_tolerance=identity_relative_tolerance,
        pareto_hash=pareto_hash,
        case_plan_hash=plan_hash,
        results_hash=results_hash,
    )

    coverage = {
        "torque_lcb": coverage_summary(
            assessments, "torque_lcb_covered", "torque_lcb_relative_error", minimum_coverage
        ),
        "voltage_ucb": coverage_summary(
            assessments, "voltage_ucb_covered", "voltage_ucb_relative_error", minimum_coverage
        ),
        "total_loss_ucb": coverage_summary(
            assessments, "total_loss_ucb_covered", "total_loss_ucb_relative_error", minimum_coverage
        ),
    }
    candidate_summaries: list[dict[str, Any]] = []
    feasible_candidate_ids: list[str] = []
    expected_points = [point.name for point in spec.operating_points]
    for candidate_id in candidate_order:
        rows = [row for row in assessments if row["candidate_id"] == candidate_id]
        passed_points = [row["operating_point_id"] for row in rows if row["hard_constraints_passed"]]
        passed = len(rows) == len(expected_points) and all(row["hard_constraints_passed"] for row in rows)
        plan_row = next(row for row in plan_rows if str(row["candidate_id"]).strip() == candidate_id)
        fea_active_volume = active_volume_m3(
            _finite(plan_row, "stator_outer_radius", f"candidate {candidate_id!r}"),
            _finite(plan_row, "stack_length_mm", f"candidate {candidate_id!r}"),
        )
        assessment_by_point = {row["operating_point_id"]: row for row in rows}
        target_cycle_numerator = sum(
            point.duty_weight * point.required_power_w for point in spec.operating_points
        )
        target_cycle_denominator = sum(
            point.duty_weight
            * (point.required_power_w + assessment_by_point[point.name]["actual_total_loss_w"])
            for point in spec.operating_points
        )
        actual_cycle_numerator = sum(
            point.duty_weight * assessment_by_point[point.name]["actual_power_w"]
            for point in spec.operating_points
        )
        actual_cycle_denominator = sum(
            point.duty_weight
            * (
                assessment_by_point[point.name]["actual_power_w"]
                + assessment_by_point[point.name]["actual_total_loss_w"]
            )
            for point in spec.operating_points
        )
        if min(
            target_cycle_numerator,
            target_cycle_denominator,
            actual_cycle_numerator,
            actual_cycle_denominator,
        ) <= 0.0:
            raise ParetoFEAValidationError(
                f"candidate {candidate_id!r} has nonpositive FEA cycle-efficiency identity"
            )
        target_cycle_efficiency = target_cycle_numerator / target_cycle_denominator
        actual_cycle_efficiency = actual_cycle_numerator / actual_cycle_denominator
        if passed:
            feasible_candidate_ids.append(candidate_id)
        candidate_summaries.append(
            {
                "candidate_id": candidate_id,
                "expected_operating_points": expected_points,
                "passed_operating_points": passed_points,
                "failed_operating_points": [
                    row["operating_point_id"] for row in rows if not row["hard_constraints_passed"]
                ],
                "all_operating_points_passed": passed,
                "active_volume_m3": fea_active_volume,
                "fea_actual_cycle_efficiency": actual_cycle_efficiency,
                "target_load_cycle_efficiency": target_cycle_efficiency,
                "fea_cycle_efficiency": actual_cycle_efficiency,
                "fea_cycle_efficiency_basis": "actual_mechanical_power",
                "fea_objectives": {
                    "active_volume_m3": fea_active_volume,
                    "one_minus_cycle_efficiency": 1.0 - actual_cycle_efficiency,
                },
            }
        )

    gate_failures = [f"{name}_coverage" for name, value in coverage.items() if not value["passed"]]
    if not feasible_candidate_ids:
        gate_failures.append("no_fea_feasible_candidate")
    summary: dict[str, Any] = {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "status": "passed" if not gate_failures else "failed",
        "pass": not gate_failures,
        "gate_failures": gate_failures,
        "thresholds": {
            "minimum_one_sided_coverage": minimum_coverage,
            "identity_relative_tolerance": identity_relative_tolerance,
        },
        "input_hashes": {
            "optimization_spec": spec_hash,
            "surrogate_model_metadata": model_metadata_hash,
            "pareto_front": pareto_hash,
            "fea_case_plan": plan_hash,
            "collected_fea_results": results_hash,
            OPTIMIZATION_SPEC_SHA256_FIELD: spec_sha256,
            SURROGATE_METADATA_SHA256_FIELD: model_metadata_sha256,
            PARETO_SHA256_FIELD: pareto_sha256,
            SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: model_artifacts_sha256,
        },
        "contract": {
            "ordered_case_coverage": True,
            "unique_case_ids": True,
            "all_status_ok": True,
            "case_rows": len(plan_rows),
            "candidate_count": len(candidate_order),
            "pareto_candidate_count": len(pareto_candidates),
            "operating_points_per_candidate": len(spec.operating_points),
            "quality_profile": REFERENCE_FEA_QUALITY_PROFILE,
            "model_extent": FEA_MODEL_EXTENT,
            "beta_convention": BETA_CONVENTION,
            "beta_calibration_id": spec.beta_calibration.calibration_id,
            "optimization_provenance": expected_provenance,
            **fingerprints,
        },
        "coverage": coverage,
        "candidates": candidate_summaries,
        "feasible_candidate_count": len(feasible_candidate_ids),
        "feasible_candidate_ids": feasible_candidate_ids,
        "row_binding_hashes": [row["case_binding_hash"] for row in assessments],
    }
    summary["validation_id"] = canonical_hash("ipmsm-pareto-fea-validation", summary)
    for row in assessments:
        row["validation_id"] = summary["validation_id"]
    return summary, assessments


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _row_csv_text(rows: Sequence[Mapping[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=ROW_FIELDNAMES, extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _stage_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _publish_no_replace(staged_path: Path, output_path: Path) -> None:
    """Atomically publish one staged inode without replacing an existing path."""

    try:
        os.link(staged_path, output_path)
    except FileExistsError as exc:
        raise ParetoFEAValidationError(
            f"refusing to overwrite raced validation output: {output_path}"
        ) from exc
    except OSError as exc:
        raise ParetoFEAValidationError(
            f"atomic no-replace hardlink publish failed for {output_path}: {exc}"
        ) from exc


def _rollback_published_inode(staged_path: Path, output_path: Path) -> None:
    """Remove our publication only while the destination still names our inode."""

    try:
        if os.path.samefile(staged_path, output_path):
            output_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        # An external replacement or an uninspectable path must never be deleted.
        return


def write_atomic_outputs(
    summary_path: Path,
    summary: Mapping[str, Any],
    rows_path: Path | None = None,
    rows: Sequence[Mapping[str, Any]] = (),
) -> None:
    outputs = [summary_path, *([rows_path] if rows_path is not None else [])]
    resolved = [path.resolve() for path in outputs]
    if len(resolved) != len(set(resolved)):
        raise ParetoFEAValidationError("summary and row CSV outputs must be distinct paths")
    existing = [path for path in outputs if path.exists()]
    if existing:
        raise ParetoFEAValidationError(f"refusing to overwrite existing validation output(s): {existing}")

    staged: list[Path] = []
    published_rows = False
    row_temp: Path | None = None
    try:
        summary_temp = _stage_text(summary_path, _json_text(summary))
        staged.append(summary_temp)
        if rows_path is not None:
            row_temp = _stage_text(rows_path, _row_csv_text(rows))
            staged.append(row_temp)
            _publish_no_replace(row_temp, rows_path)
            published_rows = True
        _publish_no_replace(summary_temp, summary_path)
    except Exception:
        if published_rows and row_temp is not None and rows_path is not None:
            _rollback_published_inode(row_temp, rows_path)
        raise
    finally:
        for path in staged:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    model_group = parser.add_mutually_exclusive_group(required=True)
    model_group.add_argument(
        "--model-dir",
        type=Path,
        help=f"Strict surrogate bundle directory containing {METADATA_FILENAME}",
    )
    model_group.add_argument(
        "--model-metadata",
        type=Path,
        help="Strict surrogate metadata.json path",
    )
    parser.add_argument("--pareto", type=Path, required=True)
    parser.add_argument("--case-plan", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--rows-output", type=Path)
    parser.add_argument("--minimum-coverage", type=float, default=DEFAULT_MINIMUM_COVERAGE)
    parser.add_argument(
        "--identity-relative-tolerance",
        type=float,
        default=DEFAULT_IDENTITY_RELATIVE_TOLERANCE,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        model_metadata_path = (
            args.model_dir / METADATA_FILENAME if args.model_dir is not None else args.model_metadata
        )
        assert model_metadata_path is not None
        input_paths = [
            args.spec.resolve(),
            model_metadata_path.resolve(),
            args.pareto.resolve(),
            args.case_plan.resolve(),
            args.results.resolve(),
        ]
        output_paths = [args.summary_output.resolve(), *([args.rows_output.resolve()] if args.rows_output else [])]
        if set(input_paths) & set(output_paths):
            raise ParetoFEAValidationError("validation outputs must not overwrite input files")
        summary, rows = validate_pareto_fea(
            args.spec,
            model_metadata_path,
            args.pareto,
            args.case_plan,
            args.results,
            minimum_coverage=args.minimum_coverage,
            identity_relative_tolerance=args.identity_relative_tolerance,
        )
        write_atomic_outputs(args.summary_output, summary, args.rows_output, rows)
    except (ParetoFEAValidationError, OSError) as exc:
        print(f"validation_error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": summary["status"],
                "validation_id": summary["validation_id"],
                "case_rows": summary["contract"]["case_rows"],
                "feasible_candidate_count": summary["feasible_candidate_count"],
                "summary_output": str(args.summary_output),
                "rows_output": str(args.rows_output) if args.rows_output else "",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
