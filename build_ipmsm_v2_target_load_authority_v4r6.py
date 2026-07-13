"""Build the immutable v4r6 target-load human-authority contract.

Only genuinely human values are accepted from the input configuration.  Every
optimization artifact path is derived from an audited v4r5 contract and its
completed, authorized optimization decision.  Build mode is read-only;
``--execute`` publishes the contract without replacing any existing path.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
from typing import Any, Mapping, Sequence

import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import ipmsm_optimization
import ipmsm_target_load_coordinator as coordinator
import supervise_ipmsm_v2_pipeline as v3
import supervise_ipmsm_v2_pipeline_v4 as v4


CONFIG_SCHEMA_VERSION = "ipmsm-v2-target-load-authority-build-config-v1"


class TargetLoadAuthorityBuildError(ValueError):
    """The v4r6 authority contract cannot be derived exactly."""


@dataclass(frozen=True)
class CompletedUpstreamAudit:
    base_binding: Mapping[str, str]
    spec: ipmsm_optimization.OptimizationSpec
    candidate_ids: tuple[str, ...]
    pareto_fea_results_dir: Path
    upstream_binding_sha256: str
    filtered_plan_sha256: str
    upstream_artifacts_manifest: tuple[Mapping[str, Any], ...]
    per_case_results_manifest: tuple[Mapping[str, Any], ...]
    protected_input_directories: tuple[Path, ...]
    snapshots: tuple[authority.FileSnapshot, ...]


@dataclass(frozen=True)
class BuiltAuthorityContract:
    document: Mapping[str, Any]
    output: Path
    publication_snapshots: tuple[authority.FileSnapshot, ...]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TargetLoadAuthorityBuildError(f"{label} must be an object")
    return value


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise TargetLoadAuthorityBuildError(
            f"{label} fields mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )


def _path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TargetLoadAuthorityBuildError(f"{label} must be an exact nonblank path string")
    path = Path(value)
    if not path.is_absolute():
        raise TargetLoadAuthorityBuildError(f"{label} must be absolute")
    return authority._require_c_local(path.absolute(), label)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=True) == right.resolve(strict=True)
    except OSError:
        return left.absolute() == right.absolute()


def _artifact_path(
    value: Any,
    label: str,
    *,
    extra_keys: frozenset[str] = frozenset(),
) -> Path:
    item = _mapping(value, label)
    _expect_keys(item, {"path", "sha256", *extra_keys}, label)
    path = _path(item["path"], f"{label}.path")
    snapshot = authority.read_single_link_snapshot(path, label)
    if snapshot.sha256 != authority._sha256(item["sha256"], f"{label}.sha256"):
        raise TargetLoadAuthorityBuildError(f"{label} SHA256 differs from the live file")
    return snapshot.path


def _read_config(path: Path) -> tuple[authority.FileSnapshot, dict[str, Any]]:
    try:
        snapshot, config = authority._strict_json_snapshot(path, "authority build config")
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadAuthorityBuildError(str(exc)) from exc
    _expect_keys(
        config,
        {
            "schema_version",
            "base_v4r5_contract",
            "pyaedt_core_snapshot",
            "outputs",
            "human_target_load",
        },
        "authority build config",
    )
    if config["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise TargetLoadAuthorityBuildError("unsupported authority build config schema_version")
    return snapshot, config


def _strict_snapshot(path: Path, label: str) -> authority.FileSnapshot:
    try:
        return authority.read_single_link_snapshot(path, label)
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadAuthorityBuildError(str(exc)) from exc


def _base_binding(contract: v4.V4Contract) -> dict[str, str]:
    snapshot = _strict_snapshot(contract.source, "base v4r5 contract")
    expected = {
        "path": str(snapshot.path),
        "raw_sha256": snapshot.sha256,
        "canonical_sha256": contract.canonical_sha256,
        "contract_sha256": contract.contract_sha256,
    }
    if snapshot.sha256 != contract.source_sha256:
        raise TargetLoadAuthorityBuildError("base v4r5 raw SHA256 changed")
    return expected


def _derived_upstream_paths(decision: Mapping[str, Any]) -> SimpleNamespace:
    execution = _mapping(decision.get("execution_contract"), "decision.execution_contract")
    inputs = _mapping(execution.get("inputs"), "execution_contract.inputs")
    beta = _mapping(inputs.get("beta"), "execution_contract.inputs.beta")
    model_bundle = _mapping(inputs.get("model_bundle"), "execution_contract.inputs.model_bundle")
    optimization = _mapping(
        decision.get("optimization_artifacts"), "decision.optimization_artifacts"
    )
    validation = _mapping(decision.get("validation"), "decision.validation")
    return SimpleNamespace(
        optimization_decision=None,
        optimization_spec=_artifact_path(
            inputs.get("optimization_spec"), "execution_contract.inputs.optimization_spec"
        ),
        pareto_csv=_artifact_path(optimization.get("pareto"), "optimization_artifacts.pareto"),
        seed_fea_plan=_artifact_path(
            optimization.get("fea_cases"), "optimization_artifacts.fea_cases"
        ),
        model_metadata=_artifact_path(
            model_bundle.get("metadata"), "execution_contract.inputs.model_bundle.metadata"
        ),
        model_artifact_dir=_path(
            model_bundle.get("model_dir"), "execution_contract.inputs.model_bundle.model_dir"
        ),
        beta_calibration_manifest=_artifact_path(
            beta.get("calibration_manifest"),
            "execution_contract.inputs.beta.calibration_manifest",
        ),
        pareto_validation_summary=_artifact_path(
            validation.get("summary"), "decision.validation.summary"
        ),
        pareto_validation_rows=_artifact_path(
            validation.get("rows"), "decision.validation.rows"
        ),
        pareto_final_front=_artifact_path(
            validation.get("final_front"),
            "decision.validation.final_front",
            extra_keys=frozenset({"candidate_count", "candidate_ids"}),
        ),
    )


def _strict_csv_rows(payload: bytes, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        reader = csv.DictReader(
            io.StringIO(payload.decode("utf-8-sig"), newline=""),
            strict=True,
        )
        fieldnames = list(reader.fieldnames or ())
        rows = [dict(row) for row in reader]
    except (UnicodeError, csv.Error) as exc:
        raise TargetLoadAuthorityBuildError(f"{label} is invalid: {exc}") from exc
    if (
        not fieldnames
        or any(not str(name or "").strip() for name in fieldnames)
        or len(fieldnames) != len(set(fieldnames))
        or any(None in row or any(value is None for value in row.values()) for row in rows)
    ):
        raise TargetLoadAuthorityBuildError(f"{label} has an invalid CSV shape")
    return fieldnames, rows


def _unique_snapshots(
    values: Sequence[authority.FileSnapshot],
) -> tuple[authority.FileSnapshot, ...]:
    result: list[authority.FileSnapshot] = []
    seen: set[str] = set()
    for snapshot in values:
        key = str(snapshot.path).casefold()
        if key in seen:
            raise TargetLoadAuthorityBuildError(
                f"immutable input snapshot is duplicated: {snapshot.path}"
            )
        seen.add(key)
        result.append(snapshot)
    return tuple(result)


def _artifact_manifest(
    values: Sequence[tuple[str, authority.FileSnapshot]],
) -> tuple[Mapping[str, Any], ...]:
    labels: set[str] = set()
    paths: set[str] = set()
    records: list[Mapping[str, Any]] = []
    for label, snapshot in values:
        path_key = str(snapshot.path).casefold()
        if not label or label != label.strip() or label in labels or path_key in paths:
            raise TargetLoadAuthorityBuildError(
                "upstream artifact labels and paths must be exact and unique"
            )
        labels.add(label)
        paths.add(path_key)
        records.append(
            {
                "label": label,
                "path": str(snapshot.path),
                "size": len(snapshot.payload),
                "sha256": snapshot.sha256,
            }
        )
    if not records:
        raise TargetLoadAuthorityBuildError("upstream artifact manifest must not be empty")
    return tuple(records)


def _append_unique_artifact(
    values: list[tuple[str, authority.FileSnapshot]],
    label: str,
    snapshot: authority.FileSnapshot,
) -> None:
    for existing_label, existing in values:
        if str(existing.path).casefold() != str(snapshot.path).casefold():
            continue
        if existing.payload != snapshot.payload or existing.sha256 != snapshot.sha256:
            raise TargetLoadAuthorityBuildError(
                f"duplicate upstream artifact changed between roles: {existing_label}, {label}"
            )
        return
    if any(existing_label == label for existing_label, _ in values):
        raise TargetLoadAuthorityBuildError(f"duplicate upstream artifact label: {label}")
    values.append((label, snapshot))


def _per_case_results(
    filtered_plan: bytes,
    merged_results: bytes,
    results_dir: Path,
    candidate_ids: tuple[str, ...],
    operating_point_count: int,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[authority.FileSnapshot, ...]]:
    plan_fields, rows = _strict_csv_rows(filtered_plan, "filtered final-front plan")
    if not {"candidate_id", "case_id"} <= set(plan_fields):
        raise TargetLoadAuthorityBuildError("filtered plan lacks candidate_id/case_id")
    result_fields, merged_rows = _strict_csv_rows(merged_results, "merged Pareto FEA results")
    missing_result_fields = [
        field
        for field in coordinator.pareto_validator.RESULT_REQUIRED_COLUMNS
        if field not in result_fields
    ]
    if missing_result_fields:
        raise TargetLoadAuthorityBuildError(
            f"merged Pareto FEA results lack required metrics: {missing_result_fields}"
        )
    merged_by_case: dict[str, dict[str, str]] = {}
    for row in merged_rows:
        case_id = str(row.get("case_id") or "")
        if not case_id or case_id in merged_by_case:
            raise TargetLoadAuthorityBuildError(
                "merged Pareto FEA result case IDs must be nonblank and unique"
            )
        if str(row.get("status") or "").strip().lower() != "ok":
            raise TargetLoadAuthorityBuildError("merged Pareto FEA result status must be ok")
        merged_by_case[case_id] = row
    minimum_rows = len(candidate_ids) * operating_point_count * 2
    maximum_rows = len(candidate_ids) * operating_point_count * 3
    if not minimum_rows <= len(rows) <= maximum_rows:
        raise TargetLoadAuthorityBuildError(
            f"filtered plan row count must be in [{minimum_rows}, {maximum_rows}], got {len(rows)}"
        )
    seen: set[str] = set()
    seen_relative_paths: set[str] = set()
    manifest: list[Mapping[str, Any]] = []
    snapshots: list[authority.FileSnapshot] = []
    for row in rows:
        candidate_id = str(row.get("candidate_id") or "")
        case_id = str(row.get("case_id") or "")
        if candidate_id not in candidate_ids or not case_id or case_id in seen:
            raise TargetLoadAuthorityBuildError("filtered plan candidate/case identity is invalid")
        seen.add(case_id)
        relative = Path(f"{coordinator.submit_campaign.sanitize_case_id(case_id)}.csv")
        relative_key = relative.as_posix().casefold()
        if relative_key in seen_relative_paths:
            raise TargetLoadAuthorityBuildError(
                "sanitized Pareto FEA result filenames collide"
            )
        seen_relative_paths.add(relative_key)
        snapshot = _strict_snapshot(results_dir / relative, f"original Pareto FEA result {case_id}")
        original_fields, original_rows = _strict_csv_rows(
            snapshot.payload, f"original Pareto FEA result {case_id}"
        )
        if original_fields != result_fields or len(original_rows) != 1:
            raise TargetLoadAuthorityBuildError(
                "original Pareto FEA result must be an exact one-row merged-result schema"
            )
        if original_rows[0] != merged_by_case.get(case_id):
            raise TargetLoadAuthorityBuildError(
                "original Pareto FEA result differs from its strictly validated merged row"
            )
        manifest.append(
            {
                "candidate_id": candidate_id,
                "case_id": case_id,
                "relative_path": relative.as_posix(),
                "size": len(snapshot.payload),
                "sha256": snapshot.sha256,
            }
        )
        snapshots.append(snapshot)
    return tuple(manifest), tuple(snapshots)


def audit_completed_upstream(base_contract_path: Path) -> CompletedUpstreamAudit:
    """Read-only replay of the authorized v4r5 final optimization authority."""

    try:
        contract = v4.load_contract(base_contract_path)
        if not _same_path(contract.source, base_contract_path):
            raise TargetLoadAuthorityBuildError(
                "loaded v4r5 contract source differs from the configured path"
            )
        v4.audit_contract(contract)
        authorization = v4.audit_authorization(contract)
        decision_path = contract.base_contract.optimization.decision
        decision = v3.audit_decision(
            decision_path,
            schema_version=v3.OPTIMIZATION_DECISION_SCHEMA_VERSION,
            allowed_statuses={"complete"},
            workdir=contract.base_contract.workdir,
        )
        v4.audit_optimization_decision_authorization(decision, authorization)
    except Exception as exc:
        raise TargetLoadAuthorityBuildError(f"v4r5 completion audit failed: {exc}") from exc
    if decision.get("mode") != "execute" or decision.get("status") != "complete":
        raise TargetLoadAuthorityBuildError("optimization decision must be executed and complete")

    paths = _derived_upstream_paths(decision)
    paths.optimization_decision = decision_path
    spec_snapshot = _strict_snapshot(paths.optimization_spec, "optimization spec")
    pareto_snapshot = _strict_snapshot(paths.pareto_csv, "Pareto CSV")
    plan_snapshot = _strict_snapshot(paths.seed_fea_plan, "seed FEA plan")
    metadata_snapshot = _strict_snapshot(paths.model_metadata, "model metadata")
    beta_snapshot = _strict_snapshot(paths.beta_calibration_manifest, "beta manifest")
    validation_summary_snapshot = _strict_snapshot(
        paths.pareto_validation_summary, "Pareto FEA validation summary"
    )
    validation_rows_snapshot = _strict_snapshot(
        paths.pareto_validation_rows, "Pareto FEA validation rows"
    )
    final_front_snapshot = _strict_snapshot(
        paths.pareto_final_front, "FEA-filtered final front"
    )
    try:
        spec_mapping = json.loads(spec_snapshot.payload.decode("utf-8-sig"))
        spec = ipmsm_optimization.optimization_spec_from_mapping(spec_mapping)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise TargetLoadAuthorityBuildError(f"optimization spec is invalid: {exc}") from exc

    try:
        model_dir_identity = authority._directory_identity(
            paths.model_artifact_dir, "model artifact directory"
        )
        model_artifacts = coordinator._model_artifacts_from_directory(
            metadata_snapshot.payload,
            paths.model_artifact_dir,
        )
        filtered_plan, upstream_binding = coordinator._audit_upstream_final_front(
            paths,
            spec_json=spec_snapshot.payload,
            pareto_csv=pareto_snapshot.payload,
            seed_plan_csv=plan_snapshot.payload,
            metadata_json=metadata_snapshot.payload,
            beta_json=beta_snapshot.payload,
            model_artifacts=model_artifacts,
        )
        authority.assert_directory_unchanged(
            paths.model_artifact_dir, model_dir_identity, "model artifact directory"
        )
    except Exception as exc:
        raise TargetLoadAuthorityBuildError(f"strict final-front replay failed: {exc}") from exc
    if upstream_binding.get("schema_version") != authority.UPSTREAM_BINDING_SCHEMA_VERSION:
        raise TargetLoadAuthorityBuildError("strict upstream binding schema_version changed")
    raw_candidate_ids = upstream_binding.get("selected_candidate_ids") or ()
    if not isinstance(raw_candidate_ids, list) or any(
        not isinstance(item, str) or not item or item != item.strip()
        for item in raw_candidate_ids
    ):
        raise TargetLoadAuthorityBuildError("FEA-filtered final-front candidate IDs are invalid")
    candidate_ids = tuple(raw_candidate_ids)
    if not candidate_ids or len(candidate_ids) > 12 or len(set(candidate_ids)) != len(candidate_ids):
        raise TargetLoadAuthorityBuildError("FEA-filtered final-front candidate IDs are invalid")
    if spec.nsga2.max_fea_candidates != 12:
        raise TargetLoadAuthorityBuildError(
            "optimization spec max_fea_candidates must exactly equal authority maximum 12"
        )

    execution = _mapping(decision["execution_contract"], "decision.execution_contract")
    pareto_fea = _mapping(execution.get("pareto_fea"), "execution_contract.pareto_fea")
    output_dir = _path(pareto_fea.get("output_dir"), "execution_contract.pareto_fea.output_dir")
    decision_fea = _mapping(decision.get("pareto_fea"), "decision.pareto_fea")
    merged_results_path = _path(decision_fea.get("results"), "decision.pareto_fea.results")
    merged_results_snapshot = _strict_snapshot(
        merged_results_path, "strictly merged Pareto FEA results"
    )
    if merged_results_snapshot.sha256 != authority._sha256(
        decision_fea.get("results_sha256"), "decision.pareto_fea.results_sha256"
    ):
        raise TargetLoadAuthorityBuildError("merged Pareto FEA results SHA256 changed")
    results_dir = output_dir / "results"
    results_identity = authority._directory_identity(results_dir, "pareto_fea results directory")
    manifest, result_snapshots = _per_case_results(
        filtered_plan,
        merged_results_snapshot.payload,
        results_dir,
        candidate_ids,
        len(spec.operating_points),
    )
    authority.assert_directory_unchanged(
        results_dir, results_identity, "pareto_fea results directory"
    )
    decision_snapshot = _strict_snapshot(decision_path, "optimization decision")
    base_snapshot = _strict_snapshot(contract.source, "base v4r5 contract")
    artifact_entries: list[tuple[str, authority.FileSnapshot]] = [
        ("optimization_decision", decision_snapshot),
        ("optimization_spec", spec_snapshot),
        ("pareto_csv", pareto_snapshot),
        ("seed_fea_plan", plan_snapshot),
        ("merged_pareto_fea_results", merged_results_snapshot),
        ("model_metadata", metadata_snapshot),
        ("beta_calibration_manifest", beta_snapshot),
        ("pareto_validation_summary", validation_summary_snapshot),
        ("pareto_validation_rows", validation_rows_snapshot),
        ("pareto_final_front", final_front_snapshot),
    ]
    v3_base_snapshot = _strict_snapshot(
        contract.base_contract_binding.path, "v4r5 bound base contract"
    )
    if v3_base_snapshot.sha256 != contract.base_contract_binding.sha256:
        raise TargetLoadAuthorityBuildError("v4r5 bound base contract SHA256 changed")
    _append_unique_artifact(artifact_entries, "v4r5_bound_base_contract", v3_base_snapshot)
    for name, pin in sorted(contract.source_pins.items()):
        snapshot = _strict_snapshot(pin.path, f"v4r5 source pin {name}")
        if snapshot.sha256 != pin.sha256:
            raise TargetLoadAuthorityBuildError(f"v4r5 source pin SHA256 changed: {name}")
        _append_unique_artifact(artifact_entries, f"v4r5_source_pin:{name}", snapshot)
    authorization_mapping = _mapping(authorization.mapping, "v4r5 authorization audit")
    for label, path, hash_key in (
        (
            "v4r5_optimization_declaration",
            contract.optimization_confirmation.declaration,
            "declaration_raw_sha256",
        ),
        (
            "v4r5_optimization_confirmation",
            contract.optimization_confirmation.confirmation,
            "confirmation_raw_sha256",
        ),
        (
            "v4r5_optimization_receipt",
            contract.optimization_confirmation.receipt,
            "receipt_raw_sha256",
        ),
    ):
        snapshot = _strict_snapshot(path, label)
        if snapshot.sha256 != authority._sha256(
            authorization_mapping.get(hash_key), f"v4r5 authorization {hash_key}"
        ):
            raise TargetLoadAuthorityBuildError(f"{label} SHA256 changed")
        _append_unique_artifact(artifact_entries, label, snapshot)
    for basename, payload in sorted(model_artifacts.items()):
        snapshot = _strict_snapshot(
            paths.model_artifact_dir / basename,
            f"surrogate model artifact {basename}",
        )
        if snapshot.payload != payload:
            raise TargetLoadAuthorityBuildError(
                f"surrogate model artifact changed after strict replay: {basename}"
            )
        _append_unique_artifact(artifact_entries, f"model_artifact:{basename}", snapshot)
    source_root = Path(coordinator.__file__).resolve(strict=True).parent
    source_names = list(coordinator.OPTIMIZATION_SOURCE_FILES)
    for runtime_path in (
        Path(coordinator.__file__).resolve(strict=True),
        Path(coordinator.workflow.__file__).resolve(strict=True),
    ):
        if runtime_path.parent != source_root:
            raise TargetLoadAuthorityBuildError("upstream audit runtime source moved outside source root")
        if runtime_path.name not in source_names:
            source_names.append(runtime_path.name)
    for name in source_names:
        _append_unique_artifact(
            artifact_entries,
            f"audit_source:{name}",
            _strict_snapshot(source_root / name, f"audit source {name}"),
        )
    try:
        replayed_plan, replayed_binding = coordinator._audit_upstream_final_front(
            paths,
            spec_json=spec_snapshot.payload,
            pareto_csv=pareto_snapshot.payload,
            seed_plan_csv=plan_snapshot.payload,
            metadata_json=metadata_snapshot.payload,
            beta_json=beta_snapshot.payload,
            model_artifacts=model_artifacts,
        )
    except Exception as exc:
        raise TargetLoadAuthorityBuildError(
            f"strict final-front publication recheck failed: {exc}"
        ) from exc
    if replayed_plan != filtered_plan or replayed_binding != upstream_binding:
        raise TargetLoadAuthorityBuildError(
            "strict upstream binding changed before publication snapshots closed"
        )
    upstream_artifacts = _artifact_manifest(artifact_entries)
    upstream_artifact_snapshots = tuple(snapshot for _, snapshot in artifact_entries)
    return CompletedUpstreamAudit(
        base_binding=_base_binding(contract),
        spec=spec,
        candidate_ids=candidate_ids,
        pareto_fea_results_dir=results_dir.resolve(strict=True),
        upstream_binding_sha256=authority.canonical_sha256(dict(upstream_binding)),
        filtered_plan_sha256=hashlib.sha256(filtered_plan).hexdigest(),
        upstream_artifacts_manifest=upstream_artifacts,
        per_case_results_manifest=manifest,
        protected_input_directories=(
            output_dir.resolve(strict=True),
            paths.model_artifact_dir.resolve(strict=True),
        ),
        snapshots=_unique_snapshots(
            (base_snapshot, *upstream_artifact_snapshots, *result_snapshots)
        ),
    )


def _outputs(config: Mapping[str, Any]) -> dict[str, Path]:
    raw = _mapping(config.get("outputs"), "outputs")
    _expect_keys(
        raw,
        {"contract", "declaration", "confirmation", "authorization_receipt"},
        "outputs",
    )
    contract = _path(raw["contract"], "outputs.contract")
    authority._audit_parent_chain(contract, "outputs.contract")
    outputs = {
        "contract": contract,
        "declaration": authority._c_local_output_path(
            raw["declaration"], contract.parent, "outputs.declaration"
        ),
        "confirmation": authority._c_local_output_path(
            raw["confirmation"], contract.parent, "outputs.confirmation"
        ),
        "authorization_receipt": authority._c_local_output_path(
            raw["authorization_receipt"], contract.parent, "outputs.authorization_receipt"
        ),
    }
    if len({str(path).lower() for path in outputs.values()}) != len(outputs):
        raise TargetLoadAuthorityBuildError("authority output paths must be distinct")
    for name in ("declaration", "confirmation", "authorization_receipt"):
        if os.path.lexists(outputs[name]):
            raise TargetLoadAuthorityBuildError(f"outputs.{name} must be a fresh path")
    return outputs


def _reject_output_aliases(
    outputs: Mapping[str, Path],
    upstream: CompletedUpstreamAudit,
) -> None:
    input_files = {str(snapshot.path).casefold() for snapshot in upstream.snapshots}
    for name, output in outputs.items():
        resolved = output.resolve(strict=False)
        if str(resolved).casefold() in input_files:
            raise TargetLoadAuthorityBuildError(f"outputs.{name} aliases an immutable input")
        for directory in upstream.protected_input_directories:
            try:
                resolved.relative_to(directory)
            except ValueError:
                continue
            raise TargetLoadAuthorityBuildError(
                f"outputs.{name} is inside protected input directory {directory}"
            )


def _target_load(
    config: Mapping[str, Any],
    upstream: CompletedUpstreamAudit,
    pyaedt_snapshot: authority.FileSnapshot,
    config_snapshot: authority.FileSnapshot,
    builder_source_snapshot: authority.FileSnapshot,
) -> dict[str, Any]:
    human = _mapping(config.get("human_target_load"), "human_target_load")
    _expect_keys(
        human,
        {
            "duty_cycle",
            "current_matching",
            "beta_validation",
            "scheduler",
            "result_settle_seconds",
        },
        "human_target_load",
    )
    duty = _mapping(human["duty_cycle"], "human_target_load.duty_cycle")
    _expect_keys(duty, {"basis", "weights"}, "human_target_load.duty_cycle")
    basis = duty["basis"]
    if not isinstance(basis, str) or not basis or basis != basis.strip():
        raise TargetLoadAuthorityBuildError("duty_cycle.basis must be an exact nonblank string")
    expected_weights = [
        {"name": point.name, "duty_weight": point.duty_weight}
        for point in upstream.spec.operating_points
    ]
    if duty["weights"] != expected_weights:
        raise TargetLoadAuthorityBuildError("human duty weights differ from the optimization spec")
    current = deepcopy(_mapping(human["current_matching"], "current_matching"))
    if current.get("maximum_current_peak_a") != upstream.spec.effective_peak_current_limit_a:
        raise TargetLoadAuthorityBuildError("maximum current differs from the optimization spec")
    operating_points = [
        {
            "name": point.name,
            "speed_rpm": point.speed_rpm,
            "target_kind": point.target_kind,
            "required_torque_nm": point.required_torque_nm,
            "required_power_w": point.required_power_w,
        }
        for point in upstream.spec.operating_points
    ]
    upstream_artifacts = [dict(item) for item in upstream.upstream_artifacts_manifest]
    per_case_results = [dict(item) for item in upstream.per_case_results_manifest]
    target = {
        "objective": deepcopy(authority.FINAL_OBJECTIVE),
        "candidate_scope": {
            "source": "all_fea_filtered_front_candidates",
            "allow_subset": False,
            "expected_candidate_count": len(upstream.candidate_ids),
            "max_candidates": 12,
            "worst_case_fea_bound": (
                len(upstream.candidate_ids)
                * len(operating_points)
                * 3
                * int(current.get("max_attempts", 0))
            ),
        },
        "operating_points": operating_points,
        "duty_cycle": {"basis": basis, "weights": deepcopy(expected_weights)},
        "current_matching": current,
        "beta_validation": deepcopy(_mapping(human["beta_validation"], "beta_validation")),
        "scheduler": deepcopy(_mapping(human["scheduler"], "scheduler")),
        "result_settle_seconds": human["result_settle_seconds"],
        "upstream_authority": {
            "binding_schema_version": authority.UPSTREAM_BINDING_SCHEMA_VERSION,
            "binding_hash_algorithm": authority.UPSTREAM_BINDING_HASH_ALGORITHM,
            "upstream_binding_sha256": upstream.upstream_binding_sha256,
            "selected_candidate_ids": list(upstream.candidate_ids),
            "filtered_plan_sha256": upstream.filtered_plan_sha256,
            "builder_source": {
                "path": str(builder_source_snapshot.path),
                "sha256": builder_source_snapshot.sha256,
            },
            "build_config": {
                "path": str(config_snapshot.path),
                "sha256": config_snapshot.sha256,
            },
            "upstream_artifact_count": len(upstream_artifacts),
            "upstream_artifacts_manifest_sha256": authority.canonical_sha256(
                {
                    "schema_version": authority.UPSTREAM_ARTIFACTS_MANIFEST_SCHEMA_VERSION,
                    "artifacts": upstream_artifacts,
                }
            ),
            "upstream_artifacts": upstream_artifacts,
            "protected_input_directories": [
                str(path) for path in upstream.protected_input_directories
            ],
            "continuation_replay_requirement": authority.CONTINUATION_REPLAY_REQUIREMENT,
        },
        "upstream_results": {
            "pareto_fea_results_dir": str(upstream.pareto_fea_results_dir),
            "path_policy": "derive_and_audit_from_v4r5_decision",
            "original_per_case_files_required": True,
            "per_case_result_count": len(per_case_results),
            "per_case_results_manifest_sha256": authority.canonical_sha256(
                {
                    "schema_version": authority.PER_CASE_RESULTS_MANIFEST_SCHEMA_VERSION,
                    "results": per_case_results,
                }
            ),
            "per_case_results": per_case_results,
        },
        "pyaedt_core_snapshot": {
            "path": str(pyaedt_snapshot.path),
            "sha256": pyaedt_snapshot.sha256,
            "single_link_required": True,
        },
    }
    try:
        return authority.validate_target_load_semantics(target)
    except authority.TargetLoadAuthorityError as exc:
        raise TargetLoadAuthorityBuildError(f"human target-load config is invalid: {exc}") from exc


def build_contract(config_path: Path) -> BuiltAuthorityContract:
    config_snapshot, config = _read_config(config_path)
    base_path = _path(config["base_v4r5_contract"], "base_v4r5_contract")
    upstream = audit_completed_upstream(base_path)
    outputs = _outputs(config)
    _reject_output_aliases(outputs, upstream)
    pyaedt = _mapping(config["pyaedt_core_snapshot"], "pyaedt_core_snapshot")
    _expect_keys(pyaedt, {"path", "sha256"}, "pyaedt_core_snapshot")
    pyaedt_snapshot = _strict_snapshot(
        _path(pyaedt["path"], "pyaedt_core_snapshot.path"), "pyaedt core snapshot"
    )
    if pyaedt_snapshot.sha256 != authority._sha256(
        pyaedt["sha256"], "pyaedt_core_snapshot.sha256"
    ):
        raise TargetLoadAuthorityBuildError("pyaedt core snapshot SHA256 mismatch")
    authorizer_source = _strict_snapshot(
        Path(authority.__file__).resolve(strict=True), "target-load authorizer source"
    )
    authorizer_executable = authority.read_single_link_snapshot(
        Path(sys.executable).resolve(strict=True),
        "target-load authorizer executable",
        require_single_link=False,
    )
    builder_source = _strict_snapshot(
        Path(__file__).resolve(strict=True), "target-load authority builder source"
    )
    target_load = _target_load(
        config,
        upstream,
        pyaedt_snapshot,
        config_snapshot,
        builder_source,
    )
    authorizer_argv = [
        str(authorizer_executable.path),
        str(authorizer_source.path),
        "authorize",
        "--contract",
        str(outputs["contract"]),
        "--declaration",
        str(outputs["declaration"]),
        "--confirmation",
        str(outputs["confirmation"]),
        "--authorization-receipt",
        str(outputs["authorization_receipt"]),
        "--execute",
    ]
    pipeline = {
        "base_v4r5_contract": dict(upstream.base_binding),
        "target_load": target_load,
        "target_load_confirmation": {
            "declaration_path": str(outputs["declaration"]),
            "confirmation_path": str(outputs["confirmation"]),
            "authorization_receipt_path": str(outputs["authorization_receipt"]),
            "declaration_schema_version": authority.DECLARATION_SCHEMA_VERSION,
            "confirmation_schema_version": authority.CONFIRMATION_SCHEMA_VERSION,
            "authorization_receipt_schema_version": authority.RECEIPT_SCHEMA_VERSION,
            "authorizer_argv": authorizer_argv,
            "authorizer_executable": {
                "path": str(authorizer_executable.path),
                "sha256": authorizer_executable.sha256,
            },
            "authorizer_source": {
                "path": str(authorizer_source.path),
                "sha256": authorizer_source.sha256,
            },
        },
    }
    unsigned = {"schema_version": authority.CONTRACT_SCHEMA_VERSION, "pipeline": pipeline}
    document = {
        **unsigned,
        "contract_sha256": authority.contract_logical_sha256(unsigned),
    }
    publication_snapshots = _unique_snapshots(
        (
            config_snapshot,
            *upstream.snapshots,
            pyaedt_snapshot,
            builder_source,
            authorizer_source,
            authorizer_executable,
        )
    )
    protected = {str(snapshot.path).casefold() for snapshot in publication_snapshots}
    for name, output in outputs.items():
        if str(output.resolve(strict=False)).casefold() in protected:
            raise TargetLoadAuthorityBuildError(f"outputs.{name} aliases an immutable input")
    return BuiltAuthorityContract(
        document=document,
        output=outputs["contract"],
        publication_snapshots=publication_snapshots,
    )


def _print(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(authority.canonical_json_bytes(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        built = build_contract(args.config)
        if not args.execute:
            _print(built.document)
            return 0
        rebuilt = build_contract(args.config)
        if rebuilt.document != built.document or rebuilt.output != built.output:
            raise TargetLoadAuthorityBuildError("authority inputs changed before publication")
        result = authority.publish_canonical_no_replace(
            rebuilt.document,
            rebuilt.output,
            additional_snapshots=rebuilt.publication_snapshots,
        )
        _print(
            {
                "status": result.outcome,
                "path": str(rebuilt.output),
                "contract_sha256": rebuilt.document["contract_sha256"],
                "writes_performed": result.writes_performed,
            }
        )
        return 0
    except (OSError, TargetLoadAuthorityBuildError, authority.TargetLoadAuthorityError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
