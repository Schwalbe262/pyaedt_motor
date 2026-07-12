"""Run the existing optimizer only under an exact v4 authorization receipt.

This inactive wrapper adds the authorization record to the legacy optimizer's
execution contract without changing the live v3 implementation.  The receipt
is replayed before a fresh decision claim, before a resume claim, and whenever
the legacy continuation reconstructs its execution contract.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import importlib.util
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import continue_ipmsm_v2_optimization as legacy
import supervise_ipmsm_v2_pipeline_v4 as supervisor_v4


class AuthorizedOptimizationError(RuntimeError):
    """The optimizer cannot prove the configured v4 authorization."""


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _same_path(left: str | Path, right: str | Path) -> bool:
    left_path = _absolute(left)
    right_path = _absolute(right)
    try:
        if os.path.samefile(left_path, right_path):
            return True
    except (FileNotFoundError, OSError, ValueError):
        pass
    try:
        left_path = left_path.resolve(strict=False)
        right_path = right_path.resolve(strict=False)
    except OSError:
        pass
    return os.path.normcase(str(left_path)) == os.path.normcase(str(right_path))


def _frozen_local_import_bindings() -> tuple[
    tuple[str, str, str | None, tuple[str, ...]], ...
]:
    """Describe the bounded production-local import edges used downstream."""

    return (
        (
            "continue_ipmsm_v2_optimization",
            "continue_ipmsm_v2_stage2",
            "stage2_continuation",
            (),
        ),
        (
            "continue_ipmsm_v2_optimization",
            "calibrate_ipmsm_beta",
            "beta_calibration",
            (),
        ),
        (
            "continue_ipmsm_v2_optimization",
            "ipmsm_optimization",
            None,
            ("OptimizationSpec", "optimization_spec_from_mapping"),
        ),
        (
            "continue_ipmsm_v2_optimization",
            "ipmsm_surrogate_bundle",
            None,
            ("IPMSMV2SurrogateBundle", "METADATA_FILENAME", "load_surrogate_bundle"),
        ),
        (
            "continue_ipmsm_v2_optimization",
            "optimize_ipmsm_nsga2",
            "optimizer",
            (),
        ),
        (
            "continue_ipmsm_v2_optimization",
            "run_ipmsm_v2_campaign",
            "campaign_runner",
            (),
        ),
        (
            "continue_ipmsm_v2_optimization",
            "submit_ipmsm_v2_campaign",
            "campaign_submitter",
            (),
        ),
        (
            "continue_ipmsm_v2_optimization",
            "validate_ipmsm_pareto_fea",
            "pareto_validator",
            (),
        ),
        ("continue_ipmsm_v2_stage2", "atomic_publish", None, ("publish_no_replace",)),
        ("continue_ipmsm_v2_stage2", "merge_ipmsm_v2_results", "merger", ()),
        ("continue_ipmsm_v2_stage2", "run_ipmsm_v2_campaign", "campaign_runner", ()),
        ("continue_ipmsm_v2_stage2", "train_ipmsm_lightgbm", "trainer", ()),
        (
            "continue_ipmsm_v2_stage2",
            "validate_ipmsm_v2_dataset",
            "dataset_validator",
            (),
        ),
        (
            "calibrate_ipmsm_beta",
            "generate_ipmsm_quality_cases",
            None,
            ("MESH_ELEMENT_KEYS", "QUALITY_PROFILES"),
        ),
        ("calibrate_ipmsm_beta", "run_ipmsm_batch", None, ("extract_fixed_geometry",)),
        ("calibrate_ipmsm_beta", "generate_ipmsm_v2_cases", None, ()),
        ("calibrate_ipmsm_beta", "ipmsm_optimization", None, ()),
        ("calibrate_ipmsm_beta", "module.ipmsm_ppt_setup", None, ()),
        (
            "optimize_ipmsm_nsga2",
            "atomic_publish",
            None,
            (
                "PublishReceipt",
                "cleanup_publish_receipt",
                "publish_no_replace",
                "recover_owned_output",
                "rollback_owned_output",
            ),
        ),
        (
            "optimize_ipmsm_nsga2",
            "ipmsm_optimization",
            None,
            (
                "BETA_CONVENTION",
                "OptimizationCandidate",
                "OptimizationSpec",
                "OptimizationSpecError",
                "SeedParameterProvider",
                "SurrogatePredictor",
                "evaluate_design_candidate",
                "geometry_metrics",
                "load_optimization_spec",
                "nondominated_candidates",
                "select_validation_candidates",
            ),
        ),
        (
            "optimize_ipmsm_nsga2",
            "ipmsm_surrogate_bundle",
            None,
            ("IPMSMV2SurrogateBundle", "SurrogateBundleError", "load_surrogate_bundle"),
        ),
        ("run_ipmsm_v2_campaign", "calibrate_ipmsm_beta", "beta_calibration", ()),
        ("run_ipmsm_v2_campaign", "collect_ipmsm_v2_campaign", "collector", ()),
        ("run_ipmsm_v2_campaign", "submit_ipmsm_v2_campaign", "submit_campaign", ()),
        (
            "submit_ipmsm_v2_campaign",
            "submit_ipmsm_scheduler_task",
            None,
            (
                "ANSYS_ELECTRONICS_MODULE",
                "DEFAULT_BOOTSTRAP_MAX_BYTES",
                "DEFAULT_SCHEDULER_URL",
                "append_env_setup",
                "build_remote_cases_bootstrap",
                "build_task_payload",
                "get_scheduler_tasks",
                "load_and_validate_cases",
                "post_scheduler_task",
                "project_active_task_count",
                "safe_dedupe_part",
                "select_case_rows",
                "task_belongs_to_project",
                "write_manifest",
            ),
        ),
        (
            "validate_ipmsm_pareto_fea",
            "atomic_publish",
            None,
            ("PublishReceipt", "publish_no_replace", "rollback_owned_output"),
        ),
        (
            "validate_ipmsm_pareto_fea",
            "ipmsm_optimization",
            None,
            (
                "BETA_CONVENTION",
                "OptimizationSpec",
                "OptimizationSpecError",
                "active_volume_m3",
                "geometry_metrics",
                "optimization_spec_from_mapping",
                "phase_resistance_100c_ohm",
            ),
        ),
        (
            "validate_ipmsm_pareto_fea",
            "ipmsm_surrogate_bundle",
            None,
            (
                "FEATURE_BOUNDS_SOURCE",
                "METADATA_FILENAME",
                "MIN_OPTIMIZER_R2",
                "PRIMARY_R2_TARGETS",
                "V2_TRAINING_SCHEMA",
            ),
        ),
        (
            "validate_ipmsm_pareto_fea",
            "optimize_ipmsm_nsga2",
            None,
            (
                "BETA_VALIDATION_ROLE_CENTER",
                "BETA_VALIDATION_ROLES",
                "FEA_DATASET_SCHEMA_VERSION",
                "FEA_MODEL_EXTENT",
                "LOCAL_BETA_NEIGHBOR_STEP_DEG",
                "REFERENCE_FEA_QUALITY_PROFILE",
                "beta_validation_case_id",
                "fea_case_fieldnames",
                "local_beta_validation_points",
                "pareto_fieldnames",
            ),
        ),
        ("merge_ipmsm_v2_results", "atomic_publish", None, ("publish_no_replace",)),
        (
            "generate_ipmsm_v2_cases",
            "atomic_publish",
            None,
            (
                "FileIdentity",
                "PROOF_SCHEMA_VERSION",
                "PublishReceipt",
                "cleanup_publish_receipt",
                "publish_no_replace",
                "receipt_owns_destination",
                "recover_owned_output",
                "rollback_owned_output",
            ),
        ),
        (
            "generate_ipmsm_v2_cases",
            "continue_ipmsm_v2_stage2",
            "stage2_continuation",
            (),
        ),
        (
            "generate_ipmsm_v2_cases",
            "generate_ipmsm_quality_cases",
            None,
            ("MESH_ELEMENT_KEYS", "QUALITY_PROFILES"),
        ),
        (
            "generate_ipmsm_v2_cases",
            "ipmsm_optimization",
            None,
            (
                "OptimizationSpec",
                "current_density_a_per_mm2",
                "geometry_metrics",
                "load_optimization_spec",
                "optimization_spec_from_mapping",
                "phase_resistance_100c_ohm",
            ),
        ),
        ("generate_ipmsm_v2_cases", "train_ipmsm_lightgbm", "trainer", ()),
        ("collect_ipmsm_v2_campaign", "inspect_ipmsm_scheduler_job", None, ("fetch_task_remote_file",)),
        (
            "collect_ipmsm_v2_campaign",
            "merge_ipmsm_v2_results",
            None,
            ("merge_complete_results", "write_csv"),
        ),
        ("collect_ipmsm_v2_campaign", "submit_ipmsm_v2_campaign", "submit_campaign", ()),
        (
            "submit_ipmsm_scheduler_task",
            "submit_ipmsm_scheduler_job",
            None,
            (
                "DEFAULT_BOOTSTRAP_MAX_BYTES",
                "DEFAULT_MAX_CASES",
                "DEFAULT_SCHEDULER_URL",
                "append_env_setup",
                "build_remote_cases_bootstrap",
                "build_stdout_output",
                "build_subprocess_arguments",
                "compact_non_json_response",
                "get_scheduler_health",
                "load_and_validate_cases",
                "read_env_setup_file",
                "select_case_rows",
                "write_manifest",
            ),
        ),
        ("submit_ipmsm_scheduler_job", "subprocess_run", "subprocess_run", ()),
        ("subprocess_run", "run_ipmsm_batch", None, ()),
        ("train_ipmsm_lightgbm", "verify_regression_metrics", None, ()),
        ("run_ipmsm_batch", "module.ipmsm_geometry", None, ()),
        ("run_ipmsm_batch", "module.ipmsm_ppt_setup", None, ()),
        ("run_ipmsm_batch", "module.variable", None, ()),
    )


def _loaded_source_path(module: Any, label: str) -> Path:
    raw = getattr(module, "__file__", None)
    if not isinstance(raw, (str, os.PathLike)):
        raise AuthorizedOptimizationError(f"cannot identify loaded {label} source")
    path = Path(raw)
    if path.suffix.lower() == ".pyc":
        path = path.with_suffix(".py")
    try:
        return supervisor_v4._canonical_no_links(path, f"loaded {label} source")
    except (OSError, supervisor_v4.PipelineContractError) as exc:
        raise AuthorizedOptimizationError(
            f"cannot resolve loaded {label} source"
        ) from exc


def _load_pinned_local_module(module_name: str, pin: Any) -> Any:
    module = sys.modules.get(module_name)
    if module is None:
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, AttributeError, ValueError) as exc:
            raise AuthorizedOptimizationError(
                f"cannot locate local optimizer dependency: {module_name}"
            ) from exc
        origin = getattr(spec, "origin", None) if spec is not None else None
        if not isinstance(origin, str):
            raise AuthorizedOptimizationError(
                f"local optimizer dependency lacks a source origin: {module_name}"
            )
        candidate = _loaded_source_path(
            type("SourceOrigin", (), {"__file__": origin})(), module_name
        )
        if not _same_path(candidate, pin.path):
            raise AuthorizedOptimizationError(
                f"local optimizer dependency import origin differs from v4 pin: {module_name}"
            )
        if supervisor_v4._file_sha256(candidate) != pin.sha256:
            raise AuthorizedOptimizationError(
                f"local optimizer dependency import origin SHA256 differs: {module_name}"
            )
        module = importlib.import_module(module_name)
    source = _loaded_source_path(module, module_name)
    if not _same_path(source, pin.path):
        raise AuthorizedOptimizationError(
            f"loaded legacy optimizer source differs from v4 source pin: {module_name}"
        )
    if supervisor_v4._file_sha256(source) != pin.sha256:
        raise AuthorizedOptimizationError(
            f"loaded legacy optimizer source SHA256 differs from v4 source pin: {module_name}"
        )
    return module


def _audit_loaded_optimizer_sources(contract: Any) -> None:
    """Bind every module used by the legacy optimizer to one v4 source pin."""

    source_manifest = supervisor_v4._audit_legacy_optimization_source_manifest()
    import_bindings = _frozen_local_import_bindings()
    manifest_modules = {module for module, _ in source_manifest}
    binding_modules = {
        module_name
        for parent_name, child_name, _, _ in import_bindings
        for module_name in (parent_name, child_name)
    }
    if binding_modules != manifest_modules:
        raise AuthorizedOptimizationError(
            "wrapper local import closure differs from the v4 source manifest"
        )
    expected_declared = (
        supervisor_v4._frozen_legacy_optimizer_declared_source_filenames()
    )
    declared = getattr(legacy, "SOURCE_CONTRACT_FILES", None)
    if (
        type(declared) is not tuple
        or declared != expected_declared
    ):
        raise AuthorizedOptimizationError(
            "loaded legacy optimizer SOURCE_CONTRACT_FILES differs from v4 authority"
        )
    pins = getattr(contract, "source_pins", None)
    if not isinstance(pins, Mapping):
        raise AuthorizedOptimizationError("v4 contract source_pins are unavailable")
    expected_pin_keys = {
        supervisor_v4._optimization_source_pin_key(module_name)
        for module_name, _ in source_manifest
        if supervisor_v4._optimization_source_pin_key(module_name).startswith(
            "optimization_source_"
        )
    }
    actual_pin_keys = {
        key
        for key in pins
        if isinstance(key, str) and key.startswith("optimization_source_")
    }
    missing_pin_keys = expected_pin_keys - actual_pin_keys
    if missing_pin_keys:
        raise AuthorizedOptimizationError(
            "v4 source pin is missing for legacy optimizer source: "
            + ", ".join(sorted(missing_pin_keys))
        )
    if actual_pin_keys != expected_pin_keys:
        raise AuthorizedOptimizationError(
            "v4 contract legacy optimizer source-pin manifest differs"
        )
    modules: dict[str, Any] = {}
    for module_name, filename in source_manifest:
        key = supervisor_v4._optimization_source_pin_key(module_name)
        pin = pins.get(key)
        if pin is None:
            raise AuthorizedOptimizationError(
                f"v4 source pin is missing for legacy optimizer source: {filename}"
            )
        modules[module_name] = _load_pinned_local_module(module_name, pin)
    if legacy is not modules["continue_ipmsm_v2_optimization"]:
        raise AuthorizedOptimizationError(
            "legacy optimizer module object differs from sys.modules authority"
        )
    missing = object()
    for parent_name, child_name, alias, imported_names in import_bindings:
        parent = modules[parent_name]
        child = modules[child_name]
        if alias is not None and getattr(parent, alias, missing) is not child:
            raise AuthorizedOptimizationError(
                "local optimizer module alias differs from imported module: "
                f"{parent_name}.{alias}"
            )
        for imported_name in imported_names:
            imported = getattr(child, imported_name, missing)
            if imported is missing or getattr(parent, imported_name, missing) is not imported:
                raise AuthorizedOptimizationError(
                    "local optimizer from-import differs from imported module: "
                    f"{parent_name}.{imported_name}"
                )


@dataclass(frozen=True)
class AuthorizationSession:
    contract_path: Path
    receipt_path: Path
    confirmation_path: Path
    contract_identity: tuple[str, str, str]

    @classmethod
    def load(
        cls,
        contract_path: str | Path,
        receipt_path: str | Path,
        confirmation_path: str | Path,
    ) -> "AuthorizationSession":
        contract = supervisor_v4.load_contract(contract_path)
        supervisor_v4.audit_contract(contract)
        contract_exact = _absolute(contract_path)
        receipt_exact = _absolute(receipt_path)
        confirmation_exact = _absolute(confirmation_path)
        if not _same_path(contract.source, contract_exact):
            raise AuthorizedOptimizationError(
                "loaded v4 contract differs from --pipeline-contract"
            )
        if not _same_path(
            contract.optimization_confirmation.receipt, receipt_exact
        ):
            raise AuthorizedOptimizationError(
                "--authorization-receipt differs from the v4 contract"
            )
        if not _same_path(
            contract.optimization_confirmation.confirmation, confirmation_exact
        ):
            raise AuthorizedOptimizationError(
                "--confirmation differs from the v4 contract"
            )
        wrapper_path = Path(__file__).resolve(strict=True)
        wrapper_pin = contract.source_pins["optimization_runner_v4"]
        if (
            not _same_path(wrapper_pin.path, wrapper_path)
            or supervisor_v4._file_sha256(wrapper_path) != wrapper_pin.sha256
        ):
            raise AuthorizedOptimizationError(
                "loaded optimization wrapper differs from its v4 source pin"
            )
        return cls(
            contract_path=contract_exact,
            receipt_path=receipt_exact,
            confirmation_path=confirmation_exact,
            contract_identity=(
                contract.source_sha256,
                contract.canonical_sha256,
                contract.contract_sha256,
            ),
        )

    def audit(self) -> tuple[Any, Any]:
        contract = supervisor_v4.load_contract(self.contract_path)
        supervisor_v4.audit_contract(contract)
        current_identity = (
            contract.source_sha256,
            contract.canonical_sha256,
            contract.contract_sha256,
        )
        if current_identity != self.contract_identity:
            raise AuthorizedOptimizationError(
                "v4 contract identity changed during optimization authorization"
            )
        if not _same_path(
            contract.optimization_confirmation.receipt, self.receipt_path
        ) or not _same_path(
            contract.optimization_confirmation.confirmation, self.confirmation_path
        ):
            raise AuthorizedOptimizationError(
                "v4 authorization paths changed during optimization"
            )
        _audit_loaded_optimizer_sources(contract)
        authorization = supervisor_v4.audit_authorization(contract)
        return contract, authorization


def _authorized_execution_contract(
    original: Any,
    session: AuthorizationSession,
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    value = original(*args, **kwargs)
    if not isinstance(value, Mapping):
        raise AuthorizedOptimizationError(
            "legacy optimizer returned an invalid execution contract"
        )
    contract, authorization = session.audit()
    result = dict(value)
    if "authorization" in result:
        raise AuthorizedOptimizationError(
            "legacy execution contract unexpectedly defines authorization"
        )
    result["authorization"] = supervisor_v4.authorization_record(
        contract, authorization.audit
    )
    return result


def _require_authorized_payload(
    payload: Mapping[str, Any], session: AuthorizationSession
) -> None:
    contract, authorization = session.audit()
    supervisor_v4.audit_optimization_decision_authorization(
        payload, authorization
    )
    supervisor_v4.audit_contract(contract)


def _run_legacy(session: AuthorizationSession, argv: Sequence[str]) -> int:
    original_execution_contract = legacy._execution_contract
    original_start_decision = legacy._start_decision
    original_resume_claim = legacy._acquire_resume_claim

    def execution_contract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _authorized_execution_contract(
            original_execution_contract, session, *args, **kwargs
        )

    def start_decision(
        args: argparse.Namespace,
        payload: Mapping[str, Any],
        owner: Mapping[str, Any],
    ) -> Path:
        _require_authorized_payload(payload, session)
        return original_start_decision(args, payload, owner)

    def acquire_resume_claim(
        args: argparse.Namespace,
        prior: Mapping[str, Any],
        owner: Mapping[str, Any],
    ) -> Path:
        _require_authorized_payload(prior, session)
        return original_resume_claim(args, prior, owner)

    legacy._execution_contract = execution_contract  # type: ignore[assignment]
    legacy._start_decision = start_decision  # type: ignore[assignment]
    legacy._acquire_resume_claim = acquire_resume_claim  # type: ignore[assignment]
    try:
        return legacy.main(list(argv))
    finally:
        legacy._execution_contract = original_execution_contract  # type: ignore[assignment]
        legacy._start_decision = original_start_decision  # type: ignore[assignment]
        legacy._acquire_resume_claim = original_resume_claim  # type: ignore[assignment]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--pipeline-contract", type=Path, required=True)
    parser.add_argument("--authorization-receipt", type=Path, required=True)
    parser.add_argument("--confirmation", type=Path, required=True)
    parser.add_argument("-h", "--help", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    args, legacy_argv = build_parser().parse_known_args(raw)
    if args.help:
        build_parser().print_help()
        legacy.build_parser().print_help()
        return 0
    if not legacy_argv or legacy_argv[0] != "--stage2-decision":
        raise AuthorizedOptimizationError(
            "legacy optimizer arguments must begin with --stage2-decision"
        )
    # Parse before any authorization work so unknown/duplicate legacy syntax
    # cannot be smuggled through the wrapper contract.
    legacy.build_parser().parse_args(legacy_argv)
    session = AuthorizationSession.load(
        args.pipeline_contract,
        args.authorization_receipt,
        args.confirmation,
    )
    session.audit()
    return _run_legacy(session, legacy_argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
