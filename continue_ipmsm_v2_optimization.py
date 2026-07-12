"""Fail-closed continuation from the v2 data gate to validated Pareto FEA.

The command is read-only by default.  ``--execute`` is the only mode that may
create a claim, optimization checkpoints/results, scheduler tasks, validation
outputs, or the continuation decision.  ``--resume`` recovers only an exact
identity-matched hard-killed execution whose recorded owner is no longer
running on this host.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import socket
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence
import uuid

import calibrate_ipmsm_beta as beta_calibration
import continue_ipmsm_v2_stage2 as stage2_continuation
import optimize_ipmsm_nsga2 as optimizer
import run_ipmsm_v2_campaign as campaign_runner
import submit_ipmsm_v2_campaign as campaign_submitter
import validate_ipmsm_pareto_fea as pareto_validator
from ipmsm_optimization import OptimizationSpec, optimization_spec_from_mapping
from ipmsm_surrogate_bundle import (
    IPMSMV2SurrogateBundle,
    METADATA_FILENAME,
    load_surrogate_bundle,
)


SCHEMA_VERSION = "ipmsm_v2_optimization_continuation_v1"
DEFAULT_MAX_FEA_CANDIDATES = 12
DEFAULT_PROJECT_ACTIVE_CAP = 50
DEFAULT_TASK_PREFIX = "ipmsm-v2-pareto-fea"
DEFAULT_REMOTE_CASES_DIR = "remote/ipmsm_v2_pareto_fea"
DEFAULT_RESULT_DIR = "simul_log/ipmsm_v2_pareto_fea"
DEFAULT_SIMULATION_DIR = "simulation/ipmsm_v2_pareto_fea"
DEFAULT_LOG_DIR = "simul_log_scheduler/ipmsm_v2_pareto_fea_logs"
ACTIVE_STATUSES = frozenset({"optimization_started", "pareto_fea_started"})
TERMINAL_STATUS = "complete"
SOURCE_CONTRACT_FILES = (
    "continue_ipmsm_v2_optimization.py",
    "continue_ipmsm_v2_stage2.py",
    "calibrate_ipmsm_beta.py",
    "ipmsm_optimization.py",
    "ipmsm_surrogate_bundle.py",
    "optimize_ipmsm_nsga2.py",
    "run_ipmsm_v2_campaign.py",
    "submit_ipmsm_v2_campaign.py",
    "validate_ipmsm_pareto_fea.py",
)


class OptimizationContinuationError(RuntimeError):
    """Raised when an optimization continuation cannot be trusted or resumed."""


@dataclass(frozen=True)
class OutputPaths:
    root: Path
    optimization_dir: Path
    pareto: Path
    fea_cases: Path
    fea_output_dir: Path
    fea_results: Path
    validation_summary: Path
    validation_rows: Path
    final_front: Path
    checkpoint_dir: Path


@dataclass(frozen=True)
class AuditedInputs:
    stage2_decision: dict[str, Any]
    stage2_decision_sha256: str
    spec: OptimizationSpec
    spec_mapping: dict[str, Any]
    beta_summary: dict[str, Any]
    model_dir: Path
    model_metadata: Path
    model_bundle: IPMSMV2SurrogateBundle
    model_gate: stage2_continuation.GateResult
    model_source: str
    model_bundle_contract: dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage2-decision", type=Path, required=True)
    parser.add_argument("--optimization-spec", type=Path, required=True)
    parser.add_argument("--beta-summary", type=Path, required=True)
    parser.add_argument("--beta-case-plan", type=Path, required=True)
    parser.add_argument("--beta-results", type=Path, required=True)
    parser.add_argument("--beta-calibration-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--decision-output", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--scheduler-url",
        default=campaign_submitter.DEFAULT_SCHEDULER_URL,
    )
    parser.add_argument(
        "--project-active-cap",
        type=int,
        default=DEFAULT_PROJECT_ACTIVE_CAP,
    )
    parser.add_argument(
        "--max-fea-candidates",
        type=int,
        default=DEFAULT_MAX_FEA_CANDIDATES,
    )
    parser.add_argument("--task-prefix", default=DEFAULT_TASK_PREFIX)
    parser.add_argument("--remote-cases-dir", default=DEFAULT_REMOTE_CASES_DIR)
    parser.add_argument("--result-dir", default=DEFAULT_RESULT_DIR)
    parser.add_argument("--simulation-dir", default=DEFAULT_SIMULATION_DIR)
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=campaign_runner.DEFAULT_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--overall-timeout-seconds",
        type=float,
        default=campaign_runner.DEFAULT_OVERALL_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--terminal-retry-limit",
        type=int,
        default=campaign_runner.DEFAULT_TERMINAL_RETRY_LIMIT,
    )
    parser.add_argument(
        "--minimum-coverage",
        type=float,
        default=pareto_validator.DEFAULT_MINIMUM_COVERAGE,
    )
    parser.add_argument(
        "--identity-relative-tolerance",
        type=float,
        default=pareto_validator.DEFAULT_IDENTITY_RELATIVE_TOLERANCE,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Claim and execute NSGA-II, Pareto FEA, and strict comparison validation.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Audit or resume an exact prior hard-killed continuation decision.",
    )
    return parser


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise OptimizationContinuationError(f"{label} is missing: {path}")
    try:
        decoded = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise OptimizationContinuationError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise OptimizationContinuationError(f"{label} must contain one JSON object")
    return decoded


def _read_csv(path: Path, label: str) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise OptimizationContinuationError(f"{label} is missing: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = list(reader.fieldnames or ())
            if not fields or len(fields) != len(set(fields)):
                raise OptimizationContinuationError(
                    f"{label} has a missing or duplicate CSV header"
                )
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise OptimizationContinuationError(f"cannot read {label} {path}: {exc}") from exc
    if any(None in row for row in rows):
        raise OptimizationContinuationError(f"{label} has fields beyond its CSV header")
    return fields, rows


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OptimizationContinuationError(f"{label} must be an object")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise OptimizationContinuationError(f"{label} must be an integer")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise OptimizationContinuationError(f"{label} must be an integer") from exc
    return parsed


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise OptimizationContinuationError(f"{label} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise OptimizationContinuationError(f"{label} must be finite") from exc
    if not math.isfinite(parsed):
        raise OptimizationContinuationError(f"{label} must be finite")
    return parsed


def _sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise OptimizationContinuationError(f"cannot hash artifact {path}: {exc}") from exc


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OptimizationContinuationError(f"contract is not canonical JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _valid_sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise OptimizationContinuationError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _resolved(path: Path) -> Path:
    return path.resolve(strict=False)


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(_resolved(first))) == os.path.normcase(str(_resolved(second)))


def _within(path: Path, directory: Path) -> bool:
    try:
        _resolved(path).relative_to(_resolved(directory))
        return True
    except ValueError:
        return False


def _artifact_record(
    raw: Any,
    label: str,
    *,
    expected_path: Path | None = None,
) -> tuple[Path, str]:
    record = _mapping(raw, label)
    if set(record) != {"path", "sha256"}:
        raise OptimizationContinuationError(f"{label} must contain exact path/sha256 fields")
    text = str(record.get("path") or "").strip()
    if not text:
        raise OptimizationContinuationError(f"{label}.path must not be blank")
    path = Path(text)
    if not path.is_file():
        raise OptimizationContinuationError(f"{label} artifact is missing: {path}")
    expected_hash = _valid_sha256(record.get("sha256"), f"{label}.sha256")
    actual_hash = _sha256(path)
    if actual_hash != expected_hash:
        raise OptimizationContinuationError(
            f"{label} hash mismatch: expected={expected_hash} actual={actual_hash}"
        )
    if expected_path is not None and not _same_path(path, expected_path):
        raise OptimizationContinuationError(
            f"{label} path mismatch: decision={path} supplied={expected_path}"
        )
    return path, actual_hash


def _top_artifact(
    raw: Mapping[str, Any],
    path_key: str,
    hash_key: str,
    contract_path: Path,
    contract_hash: str,
    label: str,
) -> None:
    path = Path(str(raw.get(path_key) or ""))
    digest = _valid_sha256(raw.get(hash_key), f"{label}.{hash_key}")
    if not _same_path(path, contract_path) or digest != contract_hash:
        raise OptimizationContinuationError(f"{label} disagrees with its execution contract")


def _validate_gate_summary(
    recorded: Mapping[str, Any],
    gate: stage2_continuation.GateResult,
    label: str,
) -> None:
    expected = gate.summary()
    for key, value in expected.items():
        if recorded.get(key) != value:
            raise OptimizationContinuationError(f"{label}.{key} changed from recomputed evidence")


def _model_bundle_contract(bundle: IPMSMV2SurrogateBundle) -> dict[str, Any]:
    root = bundle.model_dir.resolve(strict=True)
    metadata_path = root / METADATA_FILENAME
    raw_paths = _mapping(bundle.metadata.get("model_paths"), "metadata.model_paths")
    artifacts: dict[str, dict[str, str]] = {}
    seen_paths: set[str] = set()
    for target in sorted(raw_paths):
        raw = raw_paths[target]
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            values = list(raw)
        else:
            values = []
        if not values or any(not isinstance(value, str) or not value for value in values):
            raise OptimizationContinuationError(
                f"metadata.model_paths.{target} must contain nonempty model paths"
            )
        for index, recorded in enumerate(values):
            artifact = root / Path(recorded).name
            if not artifact.is_file() or artifact.stat().st_size <= 0:
                raise OptimizationContinuationError(f"model artifact is missing or empty: {artifact}")
            recorded_artifact = Path(recorded)
            try:
                same_recorded_artifact = recorded_artifact.is_file() and os.path.samefile(
                    recorded_artifact,
                    artifact,
                )
            except OSError:
                same_recorded_artifact = False
            if not same_recorded_artifact:
                raise OptimizationContinuationError(
                    "selected model directory does not contain the exact artifact passed by the R2 gate: "
                    f"{recorded!r}"
                )
            normalized = os.path.normcase(str(artifact.resolve(strict=True)))
            if normalized in seen_paths:
                raise OptimizationContinuationError("model metadata aliases one artifact more than once")
            seen_paths.add(normalized)
            artifacts[f"{target}[{index}]::{artifact.name}"] = {
                "path": str(artifact.resolve(strict=True)),
                "sha256": _sha256(artifact),
            }
    return {
        "model_dir": str(root),
        "metadata": {
            "path": str(metadata_path.resolve(strict=True)),
            "sha256": _sha256(metadata_path),
        },
        "artifacts": artifacts,
        "fingerprints": dict(bundle.fingerprints),
    }


def _audit_beta(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Path]]:
    summary = _read_json(args.beta_summary, "beta summary")
    _, plan_rows = _read_csv(args.beta_case_plan, "beta case plan")
    _, result_rows = _read_csv(args.beta_results, "beta results")
    manifest = _read_json(args.beta_calibration_manifest, "beta calibration manifest")
    try:
        validated = beta_calibration.validate_beta_sweep_summary(
            summary,
            case_plan_rows=plan_rows,
            result_rows=result_rows,
            calibration_manifest=manifest,
            require_stage_pass=True,
        )
    except (TypeError, ValueError) as exc:
        raise OptimizationContinuationError(f"strict beta prerequisite failed: {exc}") from exc
    return validated, {
        "summary": args.beta_summary,
        "case_plan": args.beta_case_plan,
        "results": args.beta_results,
        "calibration_manifest": args.beta_calibration_manifest,
    }


def _stage1_gate_from_contract(
    decision: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> tuple[stage2_continuation.GateResult, dict[str, tuple[Path, str]]]:
    stage1_contract = _mapping(contract.get("stage1"), "execution_contract.stage1")
    artifacts = {
        name: _artifact_record(stage1_contract.get(name), f"execution_contract.stage1.{name}")
        for name in ("case_plan", "metadata", "r2", "result", "validation")
    }
    training = _mapping(contract.get("training"), "execution_contract.training")
    threshold = _finite(training.get("r2_threshold"), "training.r2_threshold")
    if threshold < 0.95 or threshold > 1.0:
        raise OptimizationContinuationError("Stage2 decision R2 threshold must be in [0.95, 1]")
    gate = stage2_continuation.evaluate_gate(
        artifacts["validation"][0],
        artifacts["metadata"][0],
        artifacts["r2"][0],
        expected_rows=_integer(stage1_contract.get("expected_rows"), "stage1.expected_rows"),
        expected_groups=_integer(stage1_contract.get("expected_groups"), "stage1.expected_groups"),
        expected_repeats=_integer(stage1_contract.get("expected_repeats"), "stage1.expected_repeats"),
        threshold=threshold,
        expected_ensemble_size=_integer(training.get("ensemble_size"), "training.ensemble_size"),
        expected_conformal_coverage=_finite(
            training.get("conformal_coverage"), "training.conformal_coverage"
        ),
    )
    if stage1_contract.get("fingerprints") != gate.fingerprints:
        raise OptimizationContinuationError(
            "execution_contract.stage1 fingerprints changed from recomputed metadata"
        )
    recorded = _mapping(decision.get("stage1"), "decision.stage1")
    _validate_gate_summary(recorded, gate, "decision.stage1")
    for name, top_path, top_hash in (
        ("case_plan", "case_plan", "case_plan_sha256"),
        ("metadata", "metadata", "metadata_sha256"),
        ("r2", "r2", "r2_sha256"),
        ("result", "result", "result_sha256"),
        ("validation", "validation_path", "validation_sha256"),
    ):
        _top_artifact(recorded, top_path, top_hash, *artifacts[name], "decision.stage1")
    return gate, artifacts


def _audit_combined_model(
    decision: Mapping[str, Any],
    contract: Mapping[str, Any],
    stage1_gate: stage2_continuation.GateResult,
) -> tuple[Path, stage2_continuation.GateResult]:
    combined = _mapping(decision.get("combined"), "decision.combined")
    raw_artifacts = _mapping(combined.get("artifacts"), "decision.combined.artifacts")
    if set(raw_artifacts) != {"merged", "validation", "metadata", "r2"}:
        raise OptimizationContinuationError("combined artifacts are incomplete or provisional")
    artifacts = {
        name: _artifact_record(raw_artifacts[name], f"decision.combined.artifacts.{name}")
        for name in ("merged", "validation", "metadata", "r2")
    }
    combined_contract = _mapping(contract.get("combined"), "execution_contract.combined")
    training = _mapping(contract.get("training"), "execution_contract.training")
    gate = stage2_continuation.evaluate_gate(
        artifacts["validation"][0],
        artifacts["metadata"][0],
        artifacts["r2"][0],
        expected_rows=_integer(combined_contract.get("expected_rows"), "combined.expected_rows"),
        expected_groups=_integer(combined_contract.get("expected_groups"), "combined.expected_groups"),
        expected_repeats=_integer(combined_contract.get("expected_repeats"), "combined.expected_repeats"),
        threshold=_finite(training.get("r2_threshold"), "training.r2_threshold"),
        expected_ensemble_size=_integer(training.get("ensemble_size"), "training.ensemble_size"),
        expected_conformal_coverage=_finite(
            training.get("conformal_coverage"), "training.conformal_coverage"
        ),
    )
    if not gate.passed or gate.decision != "skip_stage2":
        raise OptimizationContinuationError("combined surrogate R2 gate did not pass")
    _validate_gate_summary(combined, gate, "decision.combined")
    output_dir = Path(str(combined.get("output_dir") or ""))
    expected_dir = Path(str(combined_contract.get("output_dir") or ""))
    if not _same_path(output_dir, expected_dir):
        raise OptimizationContinuationError("combined output directory changed")
    metadata = artifacts["metadata"][0]
    if metadata.name != METADATA_FILENAME or not _same_path(metadata.parent, output_dir / "models"):
        raise OptimizationContinuationError("combined metadata is not in the exact passed models directory")
    if stage1_gate.decision != "run_stage2":
        raise OptimizationContinuationError("combined model exists although Stage1 gate did not request Stage2")
    return metadata, gate


def audit_inputs(args: argparse.Namespace) -> AuditedInputs:
    decision = _read_json(args.stage2_decision, "Stage2 decision")
    if decision.get("schema_version") != stage2_continuation.SCHEMA_VERSION:
        raise OptimizationContinuationError("Stage2 decision schema_version is unsupported")
    if decision.get("mode") != "execute" or decision.get("status") != "complete":
        raise OptimizationContinuationError(
            "Stage2 decision must be an executed, complete, non-provisional artifact"
        )
    decision_kind = str(decision.get("decision") or "")
    if decision_kind not in {"skip_stage2", "run_stage2"}:
        raise OptimizationContinuationError("Stage2 decision has an invalid decision value")
    recorded_path = Path(str(decision.get("decision_output") or ""))
    if not _same_path(recorded_path, args.stage2_decision):
        raise OptimizationContinuationError("Stage2 decision was moved from its recorded decision_output")
    contract = _mapping(decision.get("execution_contract"), "execution_contract")
    if decision.get("contract_sha256") != _canonical_sha256(contract):
        raise OptimizationContinuationError("Stage2 decision execution contract hash is invalid")

    beta_summary, beta_paths = _audit_beta(args)
    beta_contract = _mapping(contract.get("beta"), "execution_contract.beta")
    if set(beta_contract) != set(beta_paths):
        raise OptimizationContinuationError("Stage2 beta artifact contract is incomplete")
    for name, supplied in beta_paths.items():
        _artifact_record(
            beta_contract[name],
            f"execution_contract.beta.{name}",
            expected_path=supplied,
        )

    stage1_gate, _stage1_artifacts = _stage1_gate_from_contract(decision, contract)
    if stage1_gate.decision != decision_kind:
        raise OptimizationContinuationError("Stage2 decision no longer matches the recomputed Stage1 gate")
    stage2_contract = _mapping(contract.get("stage2"), "execution_contract.stage2")
    stage2_case_plan, stage2_case_plan_hash = _artifact_record(
        stage2_contract.get("case_plan"), "execution_contract.stage2.case_plan"
    )
    recorded_stage2 = _mapping(decision.get("stage2"), "decision.stage2")
    _top_artifact(
        recorded_stage2,
        "case_plan",
        "case_plan_sha256",
        stage2_case_plan,
        stage2_case_plan_hash,
        "decision.stage2",
    )
    if recorded_stage2.get("beta") != beta_contract:
        raise OptimizationContinuationError("decision.stage2 beta contract changed")

    if decision_kind == "skip_stage2":
        if "combined" in decision:
            raise OptimizationContinuationError("skip_stage2 decision must not select a combined model")
        model_metadata = Path(str(decision["stage1"].get("metadata") or ""))
        model_gate = stage1_gate
        model_source = "stage1"
    else:
        stage2_result = Path(str(recorded_stage2.get("result") or ""))
        stage2_result_hash = _valid_sha256(
            recorded_stage2.get("result_sha256"), "decision.stage2.result_sha256"
        )
        if not stage2_result.is_file() or _sha256(stage2_result) != stage2_result_hash:
            raise OptimizationContinuationError("completed Stage2 result hash is invalid")
        model_metadata, model_gate = _audit_combined_model(decision, contract, stage1_gate)
        model_source = "combined"

    if model_metadata.name != METADATA_FILENAME or not model_metadata.is_file():
        raise OptimizationContinuationError("selected passed model metadata is missing")
    model_dir = model_metadata.parent

    spec_mapping = _read_json(args.optimization_spec, "optimization spec")
    try:
        spec = optimization_spec_from_mapping(spec_mapping)
    except (TypeError, ValueError) as exc:
        raise OptimizationContinuationError(f"strict optimization spec failed: {exc}") from exc
    if len(spec.operating_points) != 2:
        raise OptimizationContinuationError(
            "production optimization requires exactly two operating points (torque and rated power)"
        )
    if spec.nsga2.max_fea_candidates > DEFAULT_MAX_FEA_CANDIDATES:
        raise OptimizationContinuationError(
            f"optimization spec max_fea_candidates exceeds production cap {DEFAULT_MAX_FEA_CANDIDATES}"
        )
    if args.max_fea_candidates > spec.nsga2.max_fea_candidates:
        raise OptimizationContinuationError(
            "--max-fea-candidates exceeds the strict optimization spec maximum"
        )
    expected_beta = {
        "calibration_id": str(beta_summary["beta_calibration_id"]),
        "convention": str(beta_summary["convention"]),
        "electrical_zero_deg": float(beta_summary["electrical_zero_deg"]),
        "bounds": tuple(float(value) for value in beta_summary["stage_beta_bounds_deg"]),
    }
    if (
        spec.beta_calibration.calibration_id != expected_beta["calibration_id"]
        or spec.beta_calibration.convention != expected_beta["convention"]
        or not math.isclose(
            spec.beta_calibration.electrical_zero_deg,
            expected_beta["electrical_zero_deg"],
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or tuple(spec.beta_bounds_deg) != expected_beta["bounds"]
    ):
        raise OptimizationContinuationError("optimization spec does not exactly match strict beta evidence")

    try:
        bundle = load_surrogate_bundle(model_dir)
        optimizer.validate_production_surrogate(
            bundle,
            spec,
            quality_profile=pareto_validator.REFERENCE_FEA_QUALITY_PROFILE,
        )
    except (TypeError, ValueError, OSError) as exc:
        raise OptimizationContinuationError(f"production surrogate validation failed: {exc}") from exc
    if dict(bundle.fingerprints) != model_gate.fingerprints:
        raise OptimizationContinuationError("loaded surrogate fingerprints differ from passed R2 gate")

    return AuditedInputs(
        stage2_decision=decision,
        stage2_decision_sha256=_sha256(args.stage2_decision),
        spec=spec,
        spec_mapping=spec_mapping,
        beta_summary=beta_summary,
        model_dir=model_dir,
        model_metadata=model_metadata,
        model_bundle=bundle,
        model_gate=model_gate,
        model_source=model_source,
        model_bundle_contract=_model_bundle_contract(bundle),
    )


def output_paths(args: argparse.Namespace) -> OutputPaths:
    root = args.output_dir
    optimization_dir = root / "nsga2"
    fea_output_dir = root / "pareto_fea"
    return OutputPaths(
        root=root,
        optimization_dir=optimization_dir,
        pareto=optimization_dir / optimizer.DEFAULT_PARETO_NAME,
        fea_cases=optimization_dir / optimizer.DEFAULT_FEA_CASES_NAME,
        fea_output_dir=fea_output_dir,
        fea_results=fea_output_dir / "merged_results.csv",
        validation_summary=root / "pareto_fea_validation.json",
        validation_rows=root / "pareto_fea_validation_rows.csv",
        final_front=root / pareto_validator.DEFAULT_FINAL_FRONT_NAME,
        checkpoint_dir=args.checkpoint_dir,
    )


def _optimizer_argv(args: argparse.Namespace, audited: AuditedInputs, paths: OutputPaths) -> list[str]:
    return [
        "--spec",
        str(args.optimization_spec),
        "--model-dir",
        str(audited.model_dir),
        "--output-dir",
        str(paths.optimization_dir),
        "--pareto-output",
        str(paths.pareto),
        "--fea-cases-output",
        str(paths.fea_cases),
        "--fea-quality-profile",
        pareto_validator.REFERENCE_FEA_QUALITY_PROFILE,
        "--max-fea-candidates",
        str(args.max_fea_candidates),
        "--checkpoint-dir",
        str(paths.checkpoint_dir),
    ]


def _campaign_scope(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
) -> str:
    """Return a deterministic namespace preventing cross-run remote path reuse."""

    identity = {
        "project": args.project,
        "output_dir": str(_resolved(paths.root)),
        "stage2_decision_sha256": audited.stage2_decision_sha256,
        "optimization_spec_sha256": _sha256(args.optimization_spec),
        "model_bundle": audited.model_bundle_contract,
    }
    return _canonical_sha256(identity)[:16]


def _scoped_remote_path(base: str, scope: str) -> str:
    normalized = str(base or "").strip().replace("\\", "/").rstrip("/")
    if not normalized:
        raise OptimizationContinuationError("remote campaign base directory must not be blank")
    scoped = f"{normalized}/{scope}"
    if scoped.startswith("/") or ".." in PurePosixPath(scoped).parts:
        raise OptimizationContinuationError(
            f"remote campaign directory must be a safe relative path: {base!r}"
        )
    return scoped


def _campaign_argv(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
) -> list[str]:
    maximum_cases = (
        args.max_fea_candidates
        * len(audited.spec.operating_points)
        * len(optimizer.BETA_VALIDATION_ROLES)
    )
    scope = _campaign_scope(args, audited, paths)
    return [
        "--cases",
        str(paths.fea_cases),
        "--project",
        args.project,
        "--scheduler-url",
        args.scheduler_url,
        "--project-active-cap",
        str(args.project_active_cap),
        "--start",
        "1",
        "--limit",
        str(maximum_cases),
        "--max-plan-cases",
        str(maximum_cases),
        "--task-prefix",
        f"{args.task_prefix}-{scope}",
        "--remote-cases-dir",
        _scoped_remote_path(args.remote_cases_dir, scope),
        "--result-dir",
        _scoped_remote_path(args.result_dir, scope),
        "--simulation-dir",
        _scoped_remote_path(args.simulation_dir, scope),
        "--log-dir",
        _scoped_remote_path(args.log_dir, scope),
        "--output-dir",
        str(paths.fea_output_dir),
        "--merged-output",
        "merged_results.csv",
        "--overall-timeout-seconds",
        str(args.overall_timeout_seconds),
        "--poll-interval-seconds",
        str(args.poll_interval_seconds),
        "--terminal-retry-limit",
        str(args.terminal_retry_limit),
        "--beta-summary",
        str(args.beta_summary),
        "--beta-case-plan",
        str(args.beta_case_plan),
        "--beta-results",
        str(args.beta_results),
        "--beta-calibration-manifest",
        str(args.beta_calibration_manifest),
        "--submit",
    ]


def _validator_argv(args: argparse.Namespace, audited: AuditedInputs, paths: OutputPaths) -> list[str]:
    return [
        "--spec",
        str(args.optimization_spec),
        "--model-dir",
        str(audited.model_dir),
        "--pareto",
        str(paths.pareto),
        "--case-plan",
        str(paths.fea_cases),
        "--results",
        str(paths.fea_results),
        "--summary-output",
        str(paths.validation_summary),
        "--rows-output",
        str(paths.validation_rows),
        "--final-front-output",
        str(paths.final_front),
        "--minimum-coverage",
        str(args.minimum_coverage),
        "--identity-relative-tolerance",
        str(args.identity_relative_tolerance),
    ]


def _display_command(module: Any, argv: Sequence[str]) -> list[str]:
    return [sys.executable, str(Path(module.__file__).resolve(strict=True)), *argv]


def _command_record(name: str, module: Any, argv: Sequence[str]) -> dict[str, Any]:
    command = _display_command(module, argv)
    return {
        "name": name,
        "argv": command,
        "command_line": subprocess.list2cmdline(command),
    }


def validate_args(args: argparse.Namespace, paths: OutputPaths) -> None:
    if not str(args.project or "").strip():
        raise OptimizationContinuationError("--project must not be blank")
    if not 1 <= args.project_active_cap <= DEFAULT_PROJECT_ACTIVE_CAP:
        raise OptimizationContinuationError("--project-active-cap must be between 1 and 50")
    if not 1 <= args.max_fea_candidates <= DEFAULT_MAX_FEA_CANDIDATES:
        raise OptimizationContinuationError("--max-fea-candidates must be between 1 and 12")
    if not str(args.task_prefix or "").strip():
        raise OptimizationContinuationError("--task-prefix must not be blank")
    if not math.isfinite(args.poll_interval_seconds) or args.poll_interval_seconds <= 0.0:
        raise OptimizationContinuationError("--poll-interval-seconds must be finite and > 0")
    if not math.isfinite(args.overall_timeout_seconds) or args.overall_timeout_seconds <= 0.0:
        raise OptimizationContinuationError("--overall-timeout-seconds must be finite and > 0")
    if args.terminal_retry_limit < 0:
        raise OptimizationContinuationError("--terminal-retry-limit must be >= 0")
    if not math.isfinite(args.minimum_coverage) or not 0.0 < args.minimum_coverage <= 1.0:
        raise OptimizationContinuationError("--minimum-coverage must be in (0, 1]")
    if (
        not math.isfinite(args.identity_relative_tolerance)
        or args.identity_relative_tolerance < 0.0
    ):
        raise OptimizationContinuationError("--identity-relative-tolerance must be >= 0")

    file_outputs = (
        paths.pareto,
        paths.fea_cases,
        paths.fea_results,
        paths.validation_summary,
        paths.validation_rows,
        paths.final_front,
        args.decision_output,
    )
    normalized = [os.path.normcase(str(_resolved(path))) for path in file_outputs]
    if len(set(normalized)) != len(normalized):
        raise OptimizationContinuationError("continuation output paths must be distinct")
    if _within(args.decision_output, paths.fea_output_dir):
        raise OptimizationContinuationError("decision output must be outside Pareto FEA output")
    if _within(paths.checkpoint_dir, paths.fea_output_dir) or _within(
        paths.fea_output_dir, paths.checkpoint_dir
    ):
        raise OptimizationContinuationError("checkpoint and Pareto FEA directories must not overlap")
    for path in (
        args.decision_output,
        paths.pareto,
        paths.fea_cases,
        paths.validation_summary,
        paths.validation_rows,
        paths.final_front,
    ):
        if _within(path, paths.checkpoint_dir):
            raise OptimizationContinuationError("final outputs must be outside the checkpoint directory")

    claim = _claim_path(args.decision_output)
    recovery = _recovery_claim_path(args.decision_output)
    if args.resume:
        if not args.decision_output.is_file():
            raise OptimizationContinuationError(
                f"--resume requires an existing decision output: {args.decision_output}"
            )
        if recovery.exists():
            raise OptimizationContinuationError(f"stale-claim recovery is already active: {recovery}")
    else:
        existing = [path for path in (args.decision_output, claim, recovery) if path.exists()]
        if existing:
            raise OptimizationContinuationError(f"fresh decision/claim paths required: {existing}")


def _assert_new_outputs_fresh(paths: OutputPaths) -> None:
    existing = [
        path
        for path in (
            paths.pareto,
            paths.fea_cases,
            paths.fea_output_dir,
            paths.validation_summary,
            paths.validation_rows,
            paths.final_front,
        )
        if path.exists()
    ]
    if existing:
        raise OptimizationContinuationError(f"fresh optimization/FEA outputs required: {existing}")
    if paths.checkpoint_dir.exists():
        if not paths.checkpoint_dir.is_dir():
            raise OptimizationContinuationError("checkpoint path exists but is not a directory")
        if any(paths.checkpoint_dir.iterdir()):
            raise OptimizationContinuationError(
                "checkpoint directory must be absent or empty for a new execution"
            )
    for target in (paths.pareto, paths.fea_cases):
        if optimizer._pair_stage_tokens(target):
            raise OptimizationContinuationError(f"stale optimization pair stages exist for {target}")


def _source_contract() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    return {name: _sha256(root / name) for name in SOURCE_CONTRACT_FILES}


def _execution_contract(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
) -> dict[str, Any]:
    optimizer_argv = _optimizer_argv(args, audited, paths)
    campaign_argv = _campaign_argv(args, audited, paths)
    validator_argv = _validator_argv(args, audited, paths)
    return {
        "inputs": {
            "stage2_decision": {
                "path": str(_resolved(args.stage2_decision)),
                "sha256": audited.stage2_decision_sha256,
            },
            "optimization_spec": {
                "path": str(_resolved(args.optimization_spec)),
                "sha256": _sha256(args.optimization_spec),
            },
            "beta": {
                name: {"path": str(_resolved(path)), "sha256": _sha256(path)}
                for name, path in (
                    ("summary", args.beta_summary),
                    ("case_plan", args.beta_case_plan),
                    ("results", args.beta_results),
                    ("calibration_manifest", args.beta_calibration_manifest),
                )
            },
            "model_source": audited.model_source,
            "model_bundle": audited.model_bundle_contract,
        },
        "optimization": {
            "argv": optimizer_argv,
            "checkpoint_dir": str(_resolved(paths.checkpoint_dir)),
            "max_fea_candidates": args.max_fea_candidates,
            "operating_points": [point.name for point in audited.spec.operating_points],
            "maximum_fea_cases": (
                args.max_fea_candidates
                * len(audited.spec.operating_points)
                * len(optimizer.BETA_VALIDATION_ROLES)
            ),
            "pareto_output": str(_resolved(paths.pareto)),
            "fea_cases_output": str(_resolved(paths.fea_cases)),
        },
        "pareto_fea": {
            "argv": campaign_argv,
            "project": args.project,
            "project_active_cap": args.project_active_cap,
            "task_prefix": campaign_argv[campaign_argv.index("--task-prefix") + 1],
            "output_dir": str(_resolved(paths.fea_output_dir)),
            "results": str(_resolved(paths.fea_results)),
        },
        "validation": {
            "argv": validator_argv,
            "minimum_coverage": args.minimum_coverage,
            "identity_relative_tolerance": args.identity_relative_tolerance,
            "summary_output": str(_resolved(paths.validation_summary)),
            "rows_output": str(_resolved(paths.validation_rows)),
            "final_front_output": str(_resolved(paths.final_front)),
        },
        "source_sha256": _source_contract(),
    }


def _base_payload(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
) -> dict[str, Any]:
    contract = _execution_contract(args, audited, paths)
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_output": str(_resolved(args.decision_output)),
        "contract_sha256": _canonical_sha256(contract),
        "execution_contract": contract,
        "selected_model": {
            "source": audited.model_source,
            "model_dir": str(_resolved(audited.model_dir)),
            "metadata_sha256": _sha256(audited.model_metadata),
            "fingerprints": audited.model_gate.fingerprints,
            "minimum_primary_test_r2": min(audited.model_gate.primary_test_r2.values()),
            "voltage_test_r2": audited.model_gate.voltage_test_r2,
        },
        "strict_beta": {
            "sweep_id": audited.beta_summary["sweep_id"],
            "calibration_id": audited.beta_summary["beta_calibration_id"],
            "best_beta_dq_deg": audited.beta_summary["best_beta_dq_deg"],
        },
    }


def _claim_path(decision: Path) -> Path:
    return decision.with_name(decision.name + ".claim")


def _recovery_claim_path(decision: Path) -> Path:
    claim = _claim_path(decision)
    return claim.with_name(claim.name + ".recovery")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_create_json(path: Path, value: Mapping[str, Any], label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise OptimizationContinuationError(f"{label} already exists: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _new_owner(mode: str) -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "mode": mode,
        "nonce": uuid.uuid4().hex,
    }


def _acquire_claim(
    args: argparse.Namespace,
    *,
    owner: Mapping[str, Any],
    decision_sha256: str,
    contract_sha256: str,
    original_owner: Mapping[str, Any],
) -> Path:
    claim = _claim_path(args.decision_output)
    _atomic_create_json(
        claim,
        {
            "schema_version": SCHEMA_VERSION,
            "decision_output": str(_resolved(args.decision_output)),
            "decision_sha256": decision_sha256,
            "contract_sha256": contract_sha256,
            "original_owner": dict(original_owner),
            "owner": dict(owner),
        },
        "optimization continuation claim",
    )
    return claim


def _claim_is_owned(claim: Path, owner: Mapping[str, Any]) -> bool:
    try:
        value = _read_json(claim, "optimization continuation claim")
    except OptimizationContinuationError:
        return False
    return value.get("schema_version") == SCHEMA_VERSION and value.get("owner") == dict(owner)


def _require_claim_owned(claim: Path, owner: Mapping[str, Any]) -> None:
    if not _claim_is_owned(claim, owner):
        raise OptimizationContinuationError("optimization continuation claim ownership was lost")


def _release_claim(claim: Path, owner: Mapping[str, Any]) -> None:
    if _claim_is_owned(claim, owner):
        claim.unlink(missing_ok=True)


def _start_decision(
    args: argparse.Namespace,
    payload: Mapping[str, Any],
    owner: Mapping[str, Any],
) -> Path:
    decision_sha = hashlib.sha256(_json_bytes(payload)).hexdigest()
    claim = _acquire_claim(
        args,
        owner=owner,
        decision_sha256=decision_sha,
        contract_sha256=str(payload["contract_sha256"]),
        original_owner=owner,
    )
    created = False
    try:
        _atomic_create_json(args.decision_output, payload, "optimization decision")
        created = True
        if _sha256(args.decision_output) != decision_sha:
            raise OptimizationContinuationError("created decision hash differs from its claim")
    except BaseException:
        if not created:
            _release_claim(claim, owner)
        raise
    return claim


def pid_is_running(pid: int) -> bool:
    return stage2_continuation.pid_is_running(pid)


def _require_owner_inactive(owner: Mapping[str, Any], label: str) -> None:
    hostname = str(owner.get("hostname") or "")
    if hostname != socket.gethostname():
        raise OptimizationContinuationError(
            f"{label} hostname mismatch: recorded={hostname!r} current={socket.gethostname()!r}"
        )
    pid = _integer(owner.get("pid"), f"{label}.pid")
    if pid <= 0:
        raise OptimizationContinuationError(f"{label}.pid must be positive")
    if pid_is_running(pid):
        raise OptimizationContinuationError(f"{label} is still active: pid={pid}")


def _validate_prior_decision(
    args: argparse.Namespace,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    prior = _read_json(args.decision_output, "resume optimization decision")
    failures: list[str] = []
    if prior.get("schema_version") != SCHEMA_VERSION:
        failures.append("schema_version changed")
    if prior.get("mode") != "execute":
        failures.append("mode is not execute")
    if prior.get("status") not in ACTIVE_STATUSES | {TERMINAL_STATUS}:
        failures.append(f"status={prior.get('status')!r} is not resumable")
    if not _same_path(Path(str(prior.get("decision_output") or "")), args.decision_output):
        failures.append("decision_output path changed")
    expected_contract = expected.get("execution_contract")
    if prior.get("execution_contract") != expected_contract:
        failures.append("execution_contract changed")
    if prior.get("contract_sha256") != expected.get("contract_sha256"):
        failures.append("contract_sha256 changed")
    if not isinstance(prior.get("execution_contract"), Mapping) or prior.get(
        "contract_sha256"
    ) != _canonical_sha256(prior["execution_contract"]):
        failures.append("execution contract hash is invalid")
    for key in ("selected_model", "strict_beta"):
        if prior.get(key) != expected.get(key):
            failures.append(f"{key} evidence changed")
    owner = prior.get("owner")
    if not isinstance(owner, Mapping):
        failures.append("original owner is missing")
    elif set(owner) != {"hostname", "pid", "mode", "nonce"} or owner.get("mode") != "execute":
        failures.append("original owner schema/mode is invalid")
    created_at = str(prior.get("created_at") or "")
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        created = None
    if created is None or created.tzinfo is None:
        failures.append("created_at must be a timezone-aware ISO timestamp")
    if failures:
        raise OptimizationContinuationError(
            "resume decision is not an exact immutable match: " + "; ".join(failures)
        )
    return prior


def _validate_stale_claim(
    args: argparse.Namespace,
    prior: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    claim = _claim_path(args.decision_output)
    value = _read_json(claim, "stale optimization claim")
    expected_keys = {
        "schema_version",
        "decision_output",
        "decision_sha256",
        "contract_sha256",
        "original_owner",
        "owner",
    }
    failures: list[str] = []
    if set(value) != expected_keys:
        failures.append("claim fields changed")
    if value.get("schema_version") != SCHEMA_VERSION:
        failures.append("schema_version changed")
    if value.get("decision_output") != str(_resolved(args.decision_output)):
        failures.append("decision_output changed")
    decision_sha = _sha256(args.decision_output)
    if value.get("decision_sha256") != decision_sha:
        # The final decision is the transaction commit marker.  A hard kill
        # after that atomic replace but before claim unlink leaves the claim
        # naming the pre-commit decision.  This one mismatch is recoverable
        # only for a fully revalidated complete payload (checked by main
        # before claim recovery); active decisions still require an exact
        # claim hash.
        if prior.get("status") != TERMINAL_STATUS:
            failures.append("decision hash does not match")
        else:
            _valid_sha256(value.get("decision_sha256"), "stale claim decision_sha256")
    if value.get("contract_sha256") != prior.get("contract_sha256"):
        failures.append("contract hash does not match")
    if value.get("original_owner") != prior.get("owner"):
        failures.append("original owner does not match")
    owner = value.get("owner")
    if not isinstance(owner, Mapping):
        failures.append("claim owner is invalid")
    if failures:
        raise OptimizationContinuationError(
            "stale optimization claim is not recoverable: " + "; ".join(failures)
        )
    _require_owner_inactive(owner, "stale optimization claim owner")
    return value, _sha256(claim)


def _acquire_resume_claim(
    args: argparse.Namespace,
    prior: Mapping[str, Any],
    owner: Mapping[str, Any],
) -> Path:
    original_owner = _mapping(prior.get("owner"), "resume decision owner")
    _require_owner_inactive(original_owner, "original optimization owner")
    decision_sha = _sha256(args.decision_output)
    claim = _claim_path(args.decision_output)
    recovery = _recovery_claim_path(args.decision_output)
    if recovery.exists():
        raise OptimizationContinuationError(f"stale-claim recovery is already active: {recovery}")
    if claim.exists():
        _atomic_create_json(
            recovery,
            {
                "schema_version": SCHEMA_VERSION,
                "decision_output": str(_resolved(args.decision_output)),
                "decision_sha256": decision_sha,
                "owner": dict(owner),
            },
            "stale-claim recovery lock",
        )
        try:
            _, stale_hash = _validate_stale_claim(args, prior)
            if _sha256(args.decision_output) != decision_sha or _read_json(
                args.decision_output, "resume optimization decision"
            ) != prior:
                raise OptimizationContinuationError("decision changed during stale-claim recovery")
            if _sha256(claim) != stale_hash:
                raise OptimizationContinuationError("stale claim changed during recovery")
            claim.unlink()
            claim = _acquire_claim(
                args,
                owner=owner,
                decision_sha256=decision_sha,
                contract_sha256=str(prior["contract_sha256"]),
                original_owner=original_owner,
            )
        finally:
            _release_claim(recovery, owner)
    else:
        claim = _acquire_claim(
            args,
            owner=owner,
            decision_sha256=decision_sha,
            contract_sha256=str(prior["contract_sha256"]),
            original_owner=original_owner,
        )
    try:
        if _sha256(args.decision_output) != decision_sha or _read_json(
            args.decision_output, "resume optimization decision"
        ) != prior:
            raise OptimizationContinuationError("decision changed while acquiring resume claim")
    except BaseException:
        _release_claim(claim, owner)
        raise
    return claim


def _invoke_main(
    label: str,
    function: Callable[[Sequence[str]], int],
    argv: Sequence[str],
    *,
    allowed_codes: frozenset[int] = frozenset({0}),
) -> dict[str, Any]:
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            code = function(list(argv))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    if code not in allowed_codes:
        raise OptimizationContinuationError(f"{label} returned nonzero status {code}")
    text = captured.getvalue().strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OptimizationContinuationError(f"{label} did not emit one JSON object") from exc
    if not isinstance(value, dict):
        raise OptimizationContinuationError(f"{label} did not emit one JSON object")
    return value


def _checkpoint_resume_mode(paths: OutputPaths) -> bool:
    root = paths.checkpoint_dir
    manifest = root / optimizer.CHECKPOINT_MANIFEST_NAME
    if not root.exists():
        return False
    if not root.is_dir():
        raise OptimizationContinuationError("checkpoint path is not a directory")
    entries = list(root.iterdir())
    if not entries:
        return False
    if not manifest.is_file():
        raise OptimizationContinuationError("nonempty checkpoint directory lacks manifest.json")
    return True


def _campaign_args(argv: Sequence[str]) -> argparse.Namespace:
    parsed = campaign_runner.build_parser().parse_args(list(argv))
    # Only validate the scheduler task contract here.  The full runner
    # validation deliberately rejects an already-published collector output,
    # while dedupe replay validation must also work after a hard kill that
    # happened immediately after collection.
    campaign_submitter.validate_args(parsed)
    return parsed


def _task_dedupe_contract(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
) -> dict[str, Any]:
    campaign_args = _campaign_args(_campaign_argv(args, audited, paths))
    rows = campaign_submitter.load_and_validate_cases(
        campaign_args.cases,
        campaign_args.max_plan_cases,
        False,
    )
    selected = campaign_submitter.select_case_rows(
        rows,
        campaign_args.case_start_index,
        campaign_args.case_limit,
    )
    campaign_runner.validate_foundation_rows(selected, audited.beta_summary)
    tasks = campaign_submitter.build_campaign_tasks(
        campaign_args,
        selected,
        first_row_number=campaign_args.case_start_index,
    )
    keys = [task.dedupe_key for task in tasks]
    if len(keys) != len(set(keys)):
        raise OptimizationContinuationError("Pareto FEA task dedupe keys are not unique")
    return {
        "schema": "scheduler_dedupe_key_v1",
        "task_count": len(tasks),
        "dedupe_keys": keys,
        "sha256": _canonical_sha256({"dedupe_keys": keys}),
    }


def _validate_optimization_outputs(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
) -> dict[str, Any]:
    if not paths.pareto.is_file() or not paths.fea_cases.is_file():
        raise OptimizationContinuationError("optimizer did not publish the complete Pareto/FEA pair")
    pareto_fields, pareto_rows = _read_csv(paths.pareto, "Pareto output")
    if pareto_fields != optimizer.pareto_fieldnames(audited.spec):
        raise OptimizationContinuationError("Pareto output header is not canonical")
    feasible_pareto_ids = {
        str(row.get("candidate_id") or "").strip()
        for row in pareto_rows
        if str(row.get("feasible") or "").strip().lower() in {"true", "1"}
    }
    feasible_pareto_ids.discard("")
    if not feasible_pareto_ids:
        raise OptimizationContinuationError("optimizer returned no feasible Pareto candidate")
    fea_fields, fea_rows = pareto_validator.read_csv(paths.fea_cases, "FEA case plan")[:2]
    try:
        provenance_context = optimizer.build_surrogate_provenance_context(
            args.optimization_spec,
            audited.model_bundle,
        )
        expected_provenance = optimizer.build_optimization_run_provenance(
            paths.pareto.read_bytes(),
            provenance_context,
        )
    except (TypeError, ValueError, OSError) as exc:
        raise OptimizationContinuationError(
            f"cannot recompute strict optimizer provenance: {exc}"
        ) from exc
    if expected_provenance.get(optimizer.SURROGATE_VERIFICATION_FIELD) != (
        optimizer.STRICT_BUNDLE_VERIFICATION
    ):
        raise OptimizationContinuationError("optimizer provenance is not strict production verification")
    for index, row in enumerate(fea_rows, start=1):
        actual = {
            field: str(row.get(field) or "").strip()
            for field in optimizer.FEA_PROVENANCE_FIELDS
        }
        if actual != expected_provenance:
            raise OptimizationContinuationError(
                f"FEA case-plan row {index} provenance does not bind the exact Pareto/spec/model bundle"
            )
    try:
        candidate_ids = pareto_validator.validate_case_plan(
            audited.spec,
            fea_fields,
            fea_rows,
            expected_provenance,
        )
        pareto_validator.validate_pareto_front(
            audited.spec,
            pareto_fields,
            pareto_rows,
            fea_rows,
            candidate_ids,
        )
    except (TypeError, ValueError) as exc:
        raise OptimizationContinuationError(f"strict FEA case-plan validation failed: {exc}") from exc
    if not candidate_ids or len(candidate_ids) > args.max_fea_candidates:
        raise OptimizationContinuationError("FEA candidate count is empty or exceeds its maximum")
    if not set(candidate_ids) <= feasible_pareto_ids:
        raise OptimizationContinuationError("FEA plan contains a non-feasible/non-Pareto candidate")
    minimum_rows = len(candidate_ids) * len(audited.spec.operating_points) * 2
    maximum_rows = (
        len(candidate_ids)
        * len(audited.spec.operating_points)
        * len(optimizer.BETA_VALIDATION_ROLES)
    )
    if not minimum_rows <= len(fea_rows) <= maximum_rows:
        raise OptimizationContinuationError("FEA plan beta-neighbor coverage is incomplete")
    dedupe = _task_dedupe_contract(args, audited, paths)
    return {
        "pareto": {"path": str(_resolved(paths.pareto)), "sha256": _sha256(paths.pareto)},
        "fea_cases": {
            "path": str(_resolved(paths.fea_cases)),
            "sha256": _sha256(paths.fea_cases),
        },
        "pareto_rows": len(pareto_rows),
        "feasible_pareto_candidates": len(feasible_pareto_ids),
        "fea_candidate_ids": candidate_ids,
        "fea_case_rows": len(fea_rows),
        "provenance": expected_provenance,
        "task_dedupe": dedupe,
    }


def _validate_optimizer_stdout(
    result: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    if not result:
        raise OptimizationContinuationError("optimizer did not emit its required JSON provenance")
    provenance = _mapping(evidence.get("provenance"), "optimization provenance")
    expected = {
        optimizer.OPTIMIZATION_RUN_ID_FIELD: provenance[optimizer.OPTIMIZATION_RUN_ID_FIELD],
        optimizer.PARETO_SHA256_FIELD: provenance[optimizer.PARETO_SHA256_FIELD],
        optimizer.SURROGATE_VERIFICATION_FIELD: provenance[optimizer.SURROGATE_VERIFICATION_FIELD],
    }
    mismatches = [
        name for name, value in expected.items() if result.get(name) != value
    ]
    if result.get("status") != "ok":
        mismatches.append("status")
    if mismatches:
        raise OptimizationContinuationError(
            "optimizer stdout provenance/status mismatch: " + ", ".join(sorted(set(mismatches)))
        )


def _validate_recorded_optimization(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
    recorded: Any,
) -> dict[str, Any]:
    expected = _validate_optimization_outputs(args, audited, paths)
    if recorded != expected:
        raise OptimizationContinuationError("recorded optimization artifacts/dedupe changed")
    return expected


def _campaign_output_state(paths: OutputPaths) -> str:
    if not paths.fea_output_dir.exists():
        return "absent"
    if not paths.fea_output_dir.is_dir():
        raise OptimizationContinuationError("Pareto FEA output exists but is not a directory")
    selected = paths.fea_output_dir / campaign_runner.collector.SELECTED_PLAN_NAME
    if not selected.is_file() or not paths.fea_results.is_file():
        raise OptimizationContinuationError("Pareto FEA output directory is incomplete")
    expected_fields, expected_rows = _read_csv(paths.fea_cases, "optimizer FEA case plan")
    selected_fields, selected_rows = _read_csv(selected, "collected selected case plan")
    if selected_fields != expected_fields or selected_rows != expected_rows:
        raise OptimizationContinuationError("collected FEA selected plan differs from optimizer output")
    return "complete"


def _validate_campaign_stdout(
    result: Mapping[str, Any],
    evidence: Mapping[str, Any],
    args: argparse.Namespace,
    paths: OutputPaths,
) -> None:
    expected_rows = _integer(evidence.get("fea_case_rows"), "optimization FEA case rows")
    failures: list[str] = []
    if not result:
        failures.append("missing JSON output")
    if result.get("mode") != "submit":
        failures.append("mode")
    if result.get("project") != args.project:
        failures.append("project")
    for field in ("selected_cases", "successful_cases"):
        try:
            actual = _integer(result.get(field), f"campaign stdout {field}")
        except OptimizationContinuationError:
            failures.append(field)
        else:
            if actual != expected_rows:
                failures.append(field)
    if not _same_path(Path(str(result.get("output_dir") or "")), paths.fea_output_dir):
        failures.append("output_dir")
    if not _same_path(Path(str(result.get("merged_output") or "")), paths.fea_results):
        failures.append("merged_output")
    if failures:
        raise OptimizationContinuationError(
            "Pareto FEA campaign stdout mismatch: " + ", ".join(sorted(set(failures)))
        )


def _validation_expected(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        return pareto_validator.validate_pareto_fea(
            args.optimization_spec,
            audited.model_metadata,
            paths.pareto,
            paths.fea_cases,
            paths.fea_results,
            minimum_coverage=args.minimum_coverage,
            identity_relative_tolerance=args.identity_relative_tolerance,
        )
    except (TypeError, ValueError, OSError) as exc:
        raise OptimizationContinuationError(f"strict Pareto FEA validation failed: {exc}") from exc


def _verify_validation_outputs(
    spec: OptimizationSpec,
    paths: OutputPaths,
    summary: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    if (
        not paths.validation_summary.is_file()
        or not paths.validation_rows.is_file()
        or not paths.final_front.is_file()
    ):
        raise OptimizationContinuationError("Pareto FEA validation outputs are incomplete")
    if paths.validation_summary.read_bytes() != pareto_validator._json_text(summary).encode("utf-8"):
        raise OptimizationContinuationError("Pareto FEA validation summary changed")
    if paths.validation_rows.read_bytes() != pareto_validator._row_csv_text(rows).encode("utf-8"):
        raise OptimizationContinuationError("Pareto FEA validation rows changed")
    expected_front = pareto_validator._final_front_csv_text(
        spec,
        summary["fea_filtered_final_front"],
    ).encode("utf-8")
    if paths.final_front.read_bytes() != expected_front:
        raise OptimizationContinuationError("FEA-filtered final front changed")


def _finish_validation(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
    *,
    allow_writes: bool = True,
) -> dict[str, Any]:
    summary, rows = _validation_expected(args, audited, paths)
    summary_exists = paths.validation_summary.exists()
    rows_exists = paths.validation_rows.exists()
    front_exists = paths.final_front.exists()
    if summary_exists and (not rows_exists or not front_exists):
        raise OptimizationContinuationError(
            "validation summary exists without both commit predecessors"
        )
    expected_rows = pareto_validator._row_csv_text(rows).encode("utf-8")
    expected_front = pareto_validator._final_front_csv_text(
        audited.spec,
        summary["fea_filtered_final_front"],
    ).encode("utf-8")
    if rows_exists and paths.validation_rows.read_bytes() != expected_rows:
        raise OptimizationContinuationError("hard-kill validation row orphan is not exact")
    if front_exists and paths.final_front.read_bytes() != expected_front:
        raise OptimizationContinuationError("hard-kill final-front orphan is not exact")
    if not summary_exists and (rows_exists or front_exists):
        if not allow_writes:
            raise OptimizationContinuationError(
                "validation summary is missing; read-only audit cannot repair its predecessors"
            )
        pareto_validator.write_atomic_outputs(
            paths.validation_summary,
            summary,
            None if rows_exists else paths.validation_rows,
            () if rows_exists else rows,
            None if front_exists else paths.final_front,
            None if front_exists else expected_front.decode("utf-8"),
        )
    elif not summary_exists:
        if not allow_writes:
            raise OptimizationContinuationError(
                "validation outputs are missing; read-only audit cannot create them"
            )
        result = _invoke_main(
            "Pareto FEA comparator",
            pareto_validator.main,
            _validator_argv(args, audited, paths),
            allowed_codes=frozenset({0, 1}),
        )
        if result.get("status") not in {None, summary["status"]}:
            raise OptimizationContinuationError("Pareto validator output status is inconsistent")
    _verify_validation_outputs(audited.spec, paths, summary, rows)
    if summary.get("pass") is not True or summary.get("status") != "passed":
        raise OptimizationContinuationError(
            "Pareto FEA comparator gate failed: " + ", ".join(summary.get("gate_failures") or ())
        )
    if _integer(summary.get("feasible_candidate_count"), "feasible_candidate_count") < 1:
        raise OptimizationContinuationError("Pareto FEA has no feasible validated candidate")
    return {
        "summary": {
            "path": str(_resolved(paths.validation_summary)),
            "sha256": _sha256(paths.validation_summary),
        },
        "rows": {
            "path": str(_resolved(paths.validation_rows)),
            "sha256": _sha256(paths.validation_rows),
        },
        "final_front": {
            "path": str(_resolved(paths.final_front)),
            "sha256": _sha256(paths.final_front),
            "candidate_count": summary["fea_filtered_final_front_count"],
            "candidate_ids": summary["fea_filtered_final_front_candidate_ids"],
        },
        "validation_id": summary["validation_id"],
        "feasible_candidate_count": summary["feasible_candidate_count"],
        "gate_failures": summary["gate_failures"],
        "pass": True,
    }


def _validate_complete_payload(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
    payload: Mapping[str, Any],
) -> None:
    optimization_evidence = _validate_recorded_optimization(
        args, audited, paths, payload.get("optimization_artifacts")
    )
    if _campaign_output_state(paths) != "complete":
        raise OptimizationContinuationError("complete decision lacks complete Pareto FEA output")
    fea = _mapping(payload.get("pareto_fea"), "decision.pareto_fea")
    if fea.get("results_sha256") != _sha256(paths.fea_results):
        raise OptimizationContinuationError("complete decision Pareto FEA result hash changed")
    validation = _finish_validation(args, audited, paths, allow_writes=False)
    if payload.get("validation") != validation:
        raise OptimizationContinuationError("complete decision validation artifacts changed")
    if optimization_evidence["fea_case_rows"] != fea.get("case_rows"):
        raise OptimizationContinuationError("complete decision FEA row count changed")


def _run_pipeline(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
    payload: dict[str, Any],
    claim: Path,
    owner: Mapping[str, Any],
) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    if status == TERMINAL_STATUS:
        _validate_complete_payload(args, audited, paths, payload)
        return payload

    if status == "optimization_started":
        pair_exists = (paths.pareto.exists(), paths.fea_cases.exists())
        if pair_exists == (True, False):
            raise OptimizationContinuationError("Pareto commit marker exists without FEA cases")
        optimizer_result: dict[str, Any] = {}
        optimizer_invoked = pair_exists != (True, True)
        if optimizer_invoked:
            argv = _optimizer_argv(args, audited, paths)
            if _checkpoint_resume_mode(paths):
                argv.append("--resume")
            optimizer_result = _invoke_main("production NSGA-II", optimizer.main, argv)
        evidence = _validate_optimization_outputs(args, audited, paths)
        if optimizer_invoked:
            _validate_optimizer_stdout(optimizer_result, evidence)
        payload["optimization_artifacts"] = evidence
        payload["status"] = "pareto_fea_started"
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Keep the on-disk decision at optimization_started until the final
        # commit.  The claim therefore continues to name the exact decision
        # inode/hash throughout both long-running child workflows.  A hard
        # kill simply recomputes and binds these deterministic artifacts.
    elif status == "pareto_fea_started":
        evidence = _validate_recorded_optimization(
            args, audited, paths, payload.get("optimization_artifacts")
        )
    else:
        raise OptimizationContinuationError(f"decision status is not executable: {status!r}")

    state = _campaign_output_state(paths)
    if state == "absent":
        campaign_result = _invoke_main(
            "reference_ultra Pareto FEA campaign",
            campaign_runner.main,
            _campaign_argv(args, audited, paths),
        )
        if _campaign_output_state(paths) != "complete":
            raise OptimizationContinuationError("campaign returned without complete exact FEA results")
        _validate_campaign_stdout(campaign_result, evidence, args, paths)
    validation = _finish_validation(args, audited, paths)
    payload["pareto_fea"] = {
        "output_dir": str(_resolved(paths.fea_output_dir)),
        "results": str(_resolved(paths.fea_results)),
        "results_sha256": _sha256(paths.fea_results),
        "case_rows": evidence["fea_case_rows"],
        "task_dedupe_sha256": evidence["task_dedupe"]["sha256"],
    }
    payload["validation"] = validation
    payload["status"] = TERMINAL_STATUS
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _require_claim_owned(claim, owner)
    _atomic_write_json(args.decision_output, payload)
    return payload


def _planned_commands(
    args: argparse.Namespace,
    audited: AuditedInputs,
    paths: OutputPaths,
    *,
    resume_optimizer: bool = False,
) -> list[dict[str, Any]]:
    optimizer_argv = _optimizer_argv(args, audited, paths)
    if resume_optimizer:
        optimizer_argv.append("--resume")
    return [
        _command_record("production_nsga2", optimizer, optimizer_argv),
        _command_record(
            "reference_ultra_pareto_fea",
            campaign_runner,
            _campaign_argv(args, audited, paths),
        ),
        _command_record("strict_pareto_fea_comparator", pareto_validator, _validator_argv(args, audited, paths)),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = output_paths(args)
    validate_args(args, paths)
    audited = audit_inputs(args)
    expected_payload = _base_payload(args, audited, paths)

    if not args.resume:
        _assert_new_outputs_fresh(paths)
        if not args.execute:
            output = dict(expected_payload)
            output.update(
                {
                    "mode": "dry-run",
                    "status": "planned",
                    "planned_commands": _planned_commands(args, audited, paths),
                    "writes_performed": 0,
                    "maximum_fea_candidates": args.max_fea_candidates,
                    "maximum_fea_cases": (
                        args.max_fea_candidates
                        * len(audited.spec.operating_points)
                        * len(optimizer.BETA_VALIDATION_ROLES)
                    ),
                }
            )
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            return 0
        owner = _new_owner("execute")
        payload = dict(expected_payload)
        payload.update(
            {
                "mode": "execute",
                "status": "optimization_started",
                "owner": owner,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        claim = _start_decision(args, payload, owner)
    else:
        prior = _validate_prior_decision(args, expected_payload)
        original_owner = _mapping(prior.get("owner"), "resume decision owner")
        _require_owner_inactive(original_owner, "original optimization owner")
        if prior["status"] == TERMINAL_STATUS:
            # Prove the commit before allowing the sole stale-claim hash
            # exception used for a kill between final replace and unlink.
            _validate_complete_payload(args, audited, paths, prior)
        stale_claim = _claim_path(args.decision_output).exists()
        if stale_claim:
            _validate_stale_claim(args, prior)
        if not args.execute:
            commands: list[dict[str, Any]] = []
            if prior["status"] == "optimization_started":
                commands = _planned_commands(
                    args,
                    audited,
                    paths,
                    resume_optimizer=_checkpoint_resume_mode(paths),
                )
            elif prior["status"] == "pareto_fea_started":
                commands = _planned_commands(args, audited, paths)[1:]
            output = dict(prior)
            output["mode"] = "resume-dry-run"
            output["resume_action"] = {
                "claim": "recover_stale" if stale_claim else "acquire",
                "status": prior["status"],
                "planned_commands": commands,
            }
            output["writes_performed"] = 0
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            return 0
        owner = _new_owner("resume")
        claim = _acquire_resume_claim(args, prior, owner)
        payload = dict(prior)
        payload["resume_owner"] = owner
        payload["resumed_at"] = datetime.now(timezone.utc).isoformat()

    try:
        if payload.get("execution_contract") != _execution_contract(args, audited, paths):
            raise OptimizationContinuationError("immutable continuation contract changed after claim")
        result = _run_pipeline(args, audited, paths, payload, claim, owner)
    except Exception as exc:
        payload["error"] = str(exc)
        payload["status"] = "failed"
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        if _claim_is_owned(claim, owner):
            _atomic_write_json(args.decision_output, payload)
        raise
    finally:
        _release_claim(claim, owner)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
