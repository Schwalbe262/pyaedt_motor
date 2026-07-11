"""One-shot untouched confirmation of the frozen IPMSM v2 family selection.

The adaptive v5 diagnostic is immutable evidence.  This wrapper validates its
selection lock plus the separately frozen 8-geometry audit cohort, publishes a
new no-replace confirmation lock, and only then fits/predicts on the untouched
cohort.  The result remains diagnostic-only and cannot open the official R2
gate or produce a production model bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

import build_ipmsm_untouched_test_plan as untouched_builder
import diagnose_ipmsm_v2_model_families as diagnostic
import train_ipmsm_lightgbm as trainer


SCHEMA_VERSION = "ipmsm-v2-model-family-untouched-confirmation-v1"
LOCK_SCHEMA_VERSION = "ipmsm-v2-model-family-confirmation-lock-v1"
FROZEN_SELECTION_SCHEMA_VERSION = "ipmsm-v2-model-family-selection-lock-v1"
UNTOUCHED_MANIFEST_SCHEMA_VERSION = "ipmsm-v2-untouched-test-plan-manifest-v1"
EXPECTED_UNTOUCHED_GROUPS = 8
EXPECTED_ROWS_PER_GROUP = 6
EXPECTED_UNTOUCHED_ROWS = EXPECTED_UNTOUCHED_GROUPS * EXPECTED_ROWS_PER_GROUP
FROZEN_SELECTION_MANIFEST_SHA256 = "520ecf1af568c1239b1c94815a89c68914ea4511e4ba50ec321229fb6c32c548"
FROZEN_SELECTION_SHA256 = "a9abb71fb8f0c39e4b5cc1fd65ecee1a9d25867ec7d8bd42f04c1423f5503d10"
UNTOUCHED_PLAN_MANIFEST_SHA256 = "0ffb1a966526a3a30073683a806f6be1c96c16c6468deaa5f9782e33f18cbb74"
DECISION_RULE = (
    "physical_valid && selected_avg_r2 > baseline_avg_r2 && "
    "selected_min_r2 > baseline_min_r2 && selected_voltage_r2 >= baseline_voltage_r2"
)


class ConfirmationError(RuntimeError):
    """The untouched confirmation contract cannot be proven."""


def _validate_sha256(value: object, *, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ConfirmationError(f"{label} must be a lowercase SHA256")
    return text


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfirmationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ConfirmationError(f"nonfinite JSON constant: {value}")


def read_json_document(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw_bytes = path.read_bytes()
        value = json.loads(
            raw_bytes.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfirmationError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise ConfirmationError(f"JSON root must be an object: {path}")
    return raw_bytes, value


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _package_versions() -> dict[str, str]:
    return {
        name: importlib.metadata.version(name)
        for name in ("lightgbm", "numpy", "pandas", "scikit-learn")
    }


def _candidate_specs_from_frozen(selection: Mapping[str, Any]) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
    raw_specs = selection.get("candidate_specs")
    if not isinstance(raw_specs, list) or not raw_specs:
        raise ConfirmationError("frozen selection has no candidate_specs")
    if not all(isinstance(item, dict) for item in raw_specs):
        raise ConfirmationError("frozen candidate_specs must be JSON objects")
    estimator_counts = {
        item.get("params", {}).get("n_estimators")
        for item in raw_specs
        if isinstance(item.get("params"), dict) and "n_estimators" in item["params"]
    }
    if (
        len(estimator_counts) != 1
        or isinstance(next(iter(estimator_counts)), bool)
        or not isinstance(next(iter(estimator_counts)), int)
    ):
        raise ConfirmationError("frozen tree estimator count is ambiguous")
    tree_estimators = int(next(iter(estimator_counts)))
    current_specs = diagnostic.candidate_specs(tree_estimators)
    current_payload = [spec.as_dict() for spec in current_specs]
    if current_payload != raw_specs:
        raise ConfirmationError("current candidate contract differs from the frozen selection")
    return current_specs, current_payload


def validate_frozen_selection(
    manifest: Mapping[str, Any],
    *,
    manifest_sha256: str,
    expected_manifest_sha256: str,
    expected_selection_sha256: str,
    baseline_metadata_sha256: str,
) -> dict[str, Any]:
    if manifest_sha256 != _validate_sha256(
        expected_manifest_sha256,
        label="expected frozen selection manifest SHA256",
    ):
        raise ConfirmationError("frozen selection manifest file SHA256 differs from expectation")
    expected_roots = {
        "schema_version": FROZEN_SELECTION_SCHEMA_VERSION,
        "diagnostic_only": True,
        "official_gate_eligible": False,
        "production_eligible": False,
        "outer_test_evaluated": False,
        "evidence_scope": "adaptive_exploration",
        "test_reused_for_adaptive_exploration": True,
    }
    for key, expected in expected_roots.items():
        if manifest.get(key) != expected:
            raise ConfirmationError(f"frozen selection root field differs: {key}")
    selection = manifest.get("selection")
    if not isinstance(selection, dict):
        raise ConfirmationError("frozen selection payload is missing")
    canonical_selection_sha256 = diagnostic.canonical_sha256(selection)
    declared_selection_sha256 = _validate_sha256(
        manifest.get("selection_sha256"),
        label="frozen selection SHA256",
    )
    expected_selection_sha256 = _validate_sha256(
        expected_selection_sha256,
        label="expected selection SHA256",
    )
    if canonical_selection_sha256 != declared_selection_sha256:
        raise ConfirmationError("frozen selection canonical SHA256 is invalid")
    if declared_selection_sha256 != expected_selection_sha256:
        raise ConfirmationError("frozen selection SHA256 differs from expectation")
    if selection.get("evidence_scope") != "adaptive_exploration":
        raise ConfirmationError("frozen selection payload is not adaptive exploration evidence")
    if selection.get("audit_case_plan_sha256") not in {"", None}:
        raise ConfirmationError("adaptive frozen selection unexpectedly used an audit case plan")
    if selection.get("baseline_metadata_sha256") != baseline_metadata_sha256:
        raise ConfirmationError("baseline metadata differs from the frozen selection")
    current_trainer_sha256 = diagnostic.file_sha256(Path(trainer.__file__).resolve())
    current_diagnostic_sha256 = diagnostic.file_sha256(Path(diagnostic.__file__).resolve())
    if selection.get("trainer_sha256") != current_trainer_sha256:
        raise ConfirmationError("training implementation differs from the frozen selection")
    if selection.get("diagnostic_script_sha256") != current_diagnostic_sha256:
        raise ConfirmationError("adaptive diagnostic implementation differs from the frozen selection")
    if selection.get("package_versions") != _package_versions():
        raise ConfirmationError("training package versions differ from the frozen selection")
    specs, specs_payload = _candidate_specs_from_frozen(selection)
    expected_requested = set((*diagnostic.COUPLED_REQUESTED, *diagnostic.INDEPENDENT_REQUESTED))
    selected = selection.get("selected_family_by_target")
    if not isinstance(selected, dict) or set(selected) != expected_requested:
        raise ConfirmationError("frozen selected-family target coverage is invalid")
    spec_names = {spec.name for spec in specs}
    if any(not isinstance(family, str) or family not in spec_names for family in selected.values()):
        raise ConfirmationError("frozen selection references an unknown candidate family")
    seed = selection.get("seed")
    ensemble_size = selection.get("ensemble_size")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ConfirmationError("frozen selection seed is invalid")
    if isinstance(ensemble_size, bool) or not isinstance(ensemble_size, int) or ensemble_size < 1:
        raise ConfirmationError("frozen selection ensemble size is invalid")
    baseline_params = selection.get("baseline_params_by_target")
    fingerprints = selection.get("fingerprints")
    if not isinstance(baseline_params, dict) or not isinstance(fingerprints, dict):
        raise ConfirmationError("frozen selection parameters or fingerprints are missing")
    _validate_sha256(selection.get("outer_test_group_ids_sha256"), label="explored test group SHA256")
    return {
        "selection_sha256": declared_selection_sha256,
        "selection": dict(selection),
        "specs": specs,
        "candidate_specs": specs_payload,
        "selected_family_by_target": dict(selected),
        "seed": seed,
        "ensemble_size": ensemble_size,
        "baseline_params_by_target": dict(baseline_params),
        "fingerprints": dict(fingerprints),
        "trainer_sha256": current_trainer_sha256,
        "diagnostic_sha256": current_diagnostic_sha256,
    }


def _split_group_hashes(validated_plan: Mapping[str, Any]) -> dict[str, str]:
    group_splits = validated_plan.get("group_splits")
    if not isinstance(group_splits, dict):
        raise ConfirmationError("validated case plan has no group split mapping")
    return {
        split: diagnostic.ordered_text_sha256(
            sorted(group for group, role in group_splits.items() if role == split)
        )
        for split in ("train", "calibration", "test")
    }


def _case_identity_sha256(identity: Mapping[str, tuple[str, str]]) -> str:
    payload = "".join(
        f"{case_id}\0{group_id}\0{split_name}\n"
        for case_id, (group_id, split_name) in sorted(identity.items())
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_untouched_contract(
    *,
    full_plan: Path,
    explored_plan: Path,
    audit_case_plan: Path,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    expected_manifest_sha256: str,
    frozen_selection: Mapping[str, Any],
) -> dict[str, Any]:
    if manifest_sha256 != _validate_sha256(
        expected_manifest_sha256,
        label="expected untouched manifest SHA256",
    ):
        raise ConfirmationError("untouched manifest file SHA256 differs from expectation")
    if manifest.get("schema_version") != UNTOUCHED_MANIFEST_SCHEMA_VERSION:
        raise ConfirmationError("untouched manifest schema is invalid")
    if manifest.get("status") != "output_expected":
        raise ConfirmationError("untouched manifest status is invalid")
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise ConfirmationError("untouched manifest counts are missing")
    expected_counts = {
        "expected_untouched_groups": EXPECTED_UNTOUCHED_GROUPS,
        "expected_rows_per_group": EXPECTED_ROWS_PER_GROUP,
        "untouched_test_groups": EXPECTED_UNTOUCHED_GROUPS,
        "untouched_test_rows": EXPECTED_UNTOUCHED_ROWS,
    }
    for key, expected in expected_counts.items():
        if counts.get(key) != expected:
            raise ConfirmationError(f"untouched manifest count differs: {key}")

    full_bytes, full_headers, full_rows = untouched_builder.read_csv_document(full_plan)
    explored_bytes, explored_headers, explored_rows = untouched_builder.read_csv_document(explored_plan)
    audit_bytes, audit_headers, audit_rows = untouched_builder.read_csv_document(audit_case_plan)
    if manifest.get("full_plan_sha256") != sha256_bytes(full_bytes):
        raise ConfirmationError("full case plan differs from the untouched manifest")
    if manifest.get("explored_plan_sha256") != sha256_bytes(explored_bytes):
        raise ConfirmationError("explored case plan differs from the untouched manifest")
    if manifest.get("output_sha256") != sha256_bytes(audit_bytes):
        raise ConfirmationError("audit case plan differs from the untouched manifest")
    if manifest.get("script_sha256") != diagnostic.file_sha256(
        Path(untouched_builder.__file__).resolve()
    ):
        raise ConfirmationError("untouched-plan builder differs from its manifest")
    selected, recomputed = untouched_builder.select_untouched_test_rows(
        full_headers,
        full_rows,
        explored_headers,
        explored_rows,
        geometry_column="geometry_group_id",
        expected_untouched_groups=EXPECTED_UNTOUCHED_GROUPS,
        expected_rows_per_group=EXPECTED_ROWS_PER_GROUP,
    )
    if sha256_bytes(untouched_builder.encode_csv(full_headers, selected)) != sha256_bytes(audit_bytes):
        raise ConfirmationError("audit plan is not the exact recomputed untouched cohort")
    if audit_headers != full_headers or audit_rows != selected:
        raise ConfirmationError("audit plan rows differ from the recomputed untouched cohort")
    if counts.get("untouched_group_ids_sha256") != recomputed["untouched_group_ids_sha256"]:
        raise ConfirmationError("untouched geometry group SHA256 differs from recomputation")
    full_validated = untouched_builder.validate_plan(
        full_headers,
        full_rows,
        geometry_column="geometry_group_id",
    )
    explored_validated = untouched_builder.validate_plan(
        explored_headers,
        explored_rows,
        geometry_column="geometry_group_id",
    )
    explored_test_group_sha256 = diagnostic.ordered_text_sha256(
        sorted(explored_validated["test_groups"])
    )
    frozen_outer_sha256 = frozen_selection.get("outer_test_group_ids_sha256")
    if explored_test_group_sha256 != frozen_outer_sha256:
        raise ConfirmationError("explored plan does not match the frozen adaptive test groups")
    untouched_groups = sorted(
        {str(row["geometry_group_id"]).strip() for row in selected}
    )
    full_case_identity = dict(full_validated["case_identity"])
    return {
        "full_plan_sha256": sha256_bytes(full_bytes),
        "explored_plan_sha256": sha256_bytes(explored_bytes),
        "audit_case_plan_sha256": sha256_bytes(audit_bytes),
        "audit_case_plan_bytes": audit_bytes,
        "untouched_group_ids": tuple(untouched_groups),
        "untouched_group_ids_sha256": recomputed["untouched_group_ids_sha256"],
        "explored_test_group_ids_sha256": explored_test_group_sha256,
        "full_split_group_ids_sha256": _split_group_hashes(full_validated),
        "full_case_identity": full_case_identity,
        "full_case_identity_sha256": _case_identity_sha256(full_case_identity),
        "full_case_rows": len(full_case_identity),
        "audit_rows": len(audit_rows),
        "audit_groups": len(untouched_groups),
    }


def validate_prepared_data_contract(
    prepared: Any,
    outer_split: Any,
    *,
    expected_case_identity: Mapping[str, tuple[str, str]],
    expected_split_group_hashes: Mapping[str, str],
) -> dict[str, Any]:
    geometry_column = prepared.geometry_group_column
    if geometry_column != "geometry_group_id":
        raise ConfirmationError("prepared data geometry identity column changed")
    required = {"case_id", "doe_split", geometry_column}
    if not required <= set(prepared.valid_df.columns):
        raise ConfirmationError("prepared data is missing full-plan identity columns")
    actual_identity: dict[str, tuple[str, str]] = {}
    for index, row in enumerate(
        prepared.valid_df[["case_id", geometry_column, "doe_split"]].to_dict(orient="records"),
        start=1,
    ):
        case_id = untouched_builder.normalized_text(row.get("case_id"), field="case_id", row_number=index)
        group_id = untouched_builder.normalized_text(
            row.get(geometry_column),
            field=geometry_column,
            row_number=index,
        )
        split_name = untouched_builder.normalized_text(
            row.get("doe_split"),
            field="doe_split",
            row_number=index,
        ).lower()
        if split_name not in {"train", "calibration", "test"}:
            raise ConfirmationError("prepared data has an invalid doe_split")
        if case_id in actual_identity:
            raise ConfirmationError(f"prepared data has duplicate case_id: {case_id}")
        actual_identity[case_id] = (group_id, split_name)
    if actual_identity != dict(expected_case_identity):
        missing = sorted(set(expected_case_identity) - set(actual_identity))
        extra = sorted(set(actual_identity) - set(expected_case_identity))
        mismatched = sorted(
            case_id
            for case_id in set(actual_identity) & set(expected_case_identity)
            if actual_identity[case_id] != expected_case_identity[case_id]
        )
        raise ConfirmationError(
            "prepared data identity differs from the full case plan: "
            f"missing={missing[:3]} extra={extra[:3]} mismatched={mismatched[:3]}"
        )
    actual_split_hashes = {
        "train": diagnostic.ordered_text_sha256(sorted(outer_split.train_group_ids)),
        "calibration": diagnostic.ordered_text_sha256(sorted(outer_split.val_group_ids)),
        "test": diagnostic.ordered_text_sha256(sorted(outer_split.test_group_ids)),
    }
    if actual_split_hashes != dict(expected_split_group_hashes):
        raise ConfirmationError("prepared train/calibration/test group hashes differ from the full case plan")
    return {
        "case_rows": len(actual_identity),
        "case_identity_sha256": _case_identity_sha256(actual_identity),
        "split_group_ids_sha256": actual_split_hashes,
    }


def _torque_peak_below_average_violations(
    average_values: Sequence[object],
    maximum_values: Sequence[object],
) -> int:
    import numpy as np

    average = np.asarray(average_values, dtype=float)
    maximum = np.asarray(maximum_values, dtype=float)
    if average.ndim != 1 or maximum.ndim != 1 or len(average) != len(maximum):
        raise ConfirmationError("torque average/maximum arrays must be paired one-dimensional values")
    finite = np.isfinite(average) & np.isfinite(maximum)
    tolerance = 1.0e-12 * np.maximum(1.0, np.maximum(np.abs(average), np.abs(maximum)))
    return int((finite & (maximum + tolerance < average)).sum())


def evaluate_confirmation_predictions(
    prepared: Any,
    split: Any,
    predictions: Mapping[str, Sequence[object]],
) -> dict[str, Any]:
    result = diagnostic.evaluate_predictions(prepared, split, predictions)
    average_target = prepared.output_name_map["output_torque_last_avg_nm"]
    maximum_target = prepared.output_name_map["output_torque_last_max_nm"]
    truth_violations = {
        "torque_max_below_avg": _torque_peak_below_average_violations(
            split.y_test[average_target],
            split.y_test[maximum_target],
        )
    }
    prediction_violations = {
        "torque_max_below_avg": _torque_peak_below_average_violations(
            predictions[average_target],
            predictions[maximum_target],
        )
    }
    if any(truth_violations.values()):
        raise ConfirmationError("untouched ground truth violates torque max >= torque average")
    result["physical_validity"]["truth"]["cross_target"] = truth_violations
    result["physical_validity"]["prediction"]["cross_target"] = prediction_violations
    result["physical_validity"]["passed"] = bool(
        result["physical_validity"]["passed"] and not any(prediction_violations.values())
    )
    return result


def confirmation_decision(
    baseline: Mapping[str, Any],
    selected: Mapping[str, Any],
) -> tuple[str, bool]:
    required = ("primary_avg_r2", "primary_min_r2", "voltage_r2")
    if (
        not bool(selected.get("physical_validity", {}).get("passed"))
        or any(baseline.get(key) is None or selected.get(key) is None for key in required)
    ):
        return "invalid", False
    gain = bool(
        float(selected["primary_avg_r2"]) > float(baseline["primary_avg_r2"])
        and float(selected["primary_min_r2"]) > float(baseline["primary_min_r2"])
        and float(selected["voltage_r2"]) >= float(baseline["voltage_r2"])
    )
    return ("positive_confirmation" if gain else "negative_confirmation"), gain


def run_confirmation(args: argparse.Namespace) -> dict[str, Any]:
    paths = {
        name: Path(getattr(args, name)).resolve()
        for name in (
            "data",
            "baseline_metadata",
            "frozen_selection_manifest",
            "audit_case_plan",
            "untouched_plan_manifest",
            "full_case_plan",
            "explored_case_plan",
            "lock_output",
            "output",
        )
    }
    if paths["lock_output"] == paths["output"]:
        raise ConfirmationError("lock output and report output must differ")
    if paths["lock_output"].exists() or paths["output"].exists():
        raise ConfirmationError("lock output and report output must both be fresh paths")

    data_bytes = paths["data"].read_bytes()
    metadata_bytes, metadata = read_json_document(paths["baseline_metadata"])
    frozen_bytes, frozen_manifest = read_json_document(paths["frozen_selection_manifest"])
    untouched_manifest_bytes, untouched_manifest = read_json_document(paths["untouched_plan_manifest"])
    frozen = validate_frozen_selection(
        frozen_manifest,
        manifest_sha256=sha256_bytes(frozen_bytes),
        expected_manifest_sha256=FROZEN_SELECTION_MANIFEST_SHA256,
        expected_selection_sha256=FROZEN_SELECTION_SHA256,
        baseline_metadata_sha256=sha256_bytes(metadata_bytes),
    )
    untouched = validate_untouched_contract(
        full_plan=paths["full_case_plan"],
        explored_plan=paths["explored_case_plan"],
        audit_case_plan=paths["audit_case_plan"],
        manifest=untouched_manifest,
        manifest_sha256=sha256_bytes(untouched_manifest_bytes),
        expected_manifest_sha256=UNTOUCHED_PLAN_MANIFEST_SHA256,
        frozen_selection=frozen["selection"],
    )
    confirmation_script_sha256 = diagnostic.file_sha256(Path(__file__).resolve())
    lock_payload = {
        "decision_rule": DECISION_RULE,
        "duplicate_case_id_policy": "reject_in_valid_confirmation_data",
        "data_sha256": sha256_bytes(data_bytes),
        "baseline_metadata_sha256": sha256_bytes(metadata_bytes),
        "frozen_selection_manifest_sha256": sha256_bytes(frozen_bytes),
        "frozen_selection_sha256": frozen["selection_sha256"],
        "untouched_plan_manifest_sha256": sha256_bytes(untouched_manifest_bytes),
        "full_case_plan_sha256": untouched["full_plan_sha256"],
        "explored_case_plan_sha256": untouched["explored_plan_sha256"],
        "audit_case_plan_sha256": untouched["audit_case_plan_sha256"],
        "untouched_group_ids_sha256": untouched["untouched_group_ids_sha256"],
        "explored_test_group_ids_sha256": untouched["explored_test_group_ids_sha256"],
        "full_split_group_ids_sha256": untouched["full_split_group_ids_sha256"],
        "full_case_rows": untouched["full_case_rows"],
        "full_case_identity_sha256": untouched["full_case_identity_sha256"],
        "selected_family_by_target": frozen["selected_family_by_target"],
        "candidate_specs": frozen["candidate_specs"],
        "baseline_params_by_target": frozen["baseline_params_by_target"],
        "seed": frozen["seed"],
        "ensemble_size": frozen["ensemble_size"],
        "fingerprints": frozen["fingerprints"],
        "package_versions": _package_versions(),
        "trainer_sha256": frozen["trainer_sha256"],
        "adaptive_diagnostic_sha256": frozen["diagnostic_sha256"],
        "confirmation_script_sha256": confirmation_script_sha256,
    }
    lock_sha256 = diagnostic.canonical_sha256(lock_payload)
    lock = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "status": "locked_before_untouched_prediction",
        "diagnostic_only": True,
        "official_gate_eligible": False,
        "production_eligible": False,
        "lock_sha256": lock_sha256,
        "lock": lock_payload,
    }
    diagnostic._publish_report(paths["lock_output"], lock)
    lock_file_sha256 = diagnostic.file_sha256(paths["lock_output"])

    with tempfile.TemporaryDirectory(prefix="ipmsm-untouched-confirm-") as temporary:
        temporary_root = Path(temporary)
        data_snapshot = temporary_root / "data.csv"
        audit_snapshot = temporary_root / "audit.csv"
        data_snapshot.write_bytes(data_bytes)
        audit_snapshot.write_bytes(untouched["audit_case_plan_bytes"])
        deps = trainer.require_training_dependencies()
        prepared = trainer.prepare_training_data(
            deps,
            [data_snapshot],
            drop_duplicate_case_id=False,
            remove_output_outliers=False,
            outlier_iqr_weight=1.5,
            v2=True,
            expected_fingerprints=frozen["fingerprints"],
        )
        outer = trainer.split_training_data(
            deps,
            prepared,
            test_size=0.2,
            val_size=0.2,
            seed=frozen["seed"],
        )
        prepared_contract = validate_prepared_data_contract(
            prepared,
            outer,
            expected_case_identity=untouched["full_case_identity"],
            expected_split_group_hashes=untouched["full_split_group_ids_sha256"],
        )
        evaluation, evaluation_contract = trainer.select_v2_test_evaluation_split(
            prepared,
            outer,
            audit_snapshot,
        )
        evaluation_contract = dict(evaluation_contract)
        evaluation_contract["case_plan"] = str(paths["audit_case_plan"])
        evaluation_contract["case_plan_sha256"] = untouched["audit_case_plan_sha256"]
        if tuple(evaluation.test_group_ids) != tuple(untouched["untouched_group_ids"]):
            raise ConfirmationError("training evaluation groups differ from the locked untouched groups")
        targets = tuple((*prepared.output_columns, *prepared.auxiliary_output_columns))
        expected_targets = {
            prepared.output_name_map[name]
            for name in (*diagnostic.COUPLED_REQUESTED, *diagnostic.INDEPENDENT_REQUESTED)
        }
        if set(targets) != expected_targets:
            raise ConfirmationError("v2 modeled target coverage changed")
        metadata_params = diagnostic._baseline_params(metadata, targets)
        if metadata_params != frozen["baseline_params_by_target"]:
            raise ConfirmationError("baseline metadata parameters differ from the frozen selection")
        if diagnostic._metadata_fingerprints(metadata) != frozen["fingerprints"]:
            raise ConfirmationError("baseline metadata fingerprints differ from the frozen selection")
        spec_by_name = {spec.name: spec for spec in frozen["specs"]}
        prediction_cache: dict[tuple[str, str], Any] = {}

        def final_prediction(requested: str, family: str) -> Any:
            target = prepared.output_name_map[requested]
            key = (target, family)
            if key in prediction_cache:
                return prediction_cache[key]
            spec = spec_by_name[family]
            if spec.kind == "lightgbm":
                predicted = diagnostic._ensemble_lgbm_prediction(
                    deps=deps,
                    split=outer,
                    predict_x=evaluation.x_test,
                    target=target,
                    params=frozen["baseline_params_by_target"][target],
                    seed=frozen["seed"],
                    n_jobs=args.n_jobs,
                    ensemble_size=frozen["ensemble_size"],
                )
            else:
                columns = diagnostic.feature_columns(prepared.input_columns, spec.feature_mode)
                model = diagnostic._build_estimator(
                    spec,
                    target=target,
                    seed=frozen["seed"],
                    n_jobs=args.n_jobs,
                )
                model.fit(outer.x_train[list(columns)], outer.y_train[target])
                predicted = model.predict(evaluation.x_test[list(columns)])
            prediction_cache[key] = predicted
            return predicted

        requested_targets = (*diagnostic.COUPLED_REQUESTED, *diagnostic.INDEPENDENT_REQUESTED)
        baseline_predictions = {
            prepared.output_name_map[requested]: final_prediction(requested, "lightgbm")
            for requested in requested_targets
        }
        selected_predictions = {
            prepared.output_name_map[requested]: final_prediction(
                requested,
                frozen["selected_family_by_target"][requested],
            )
            for requested in requested_targets
        }
        baseline_evaluation = evaluate_confirmation_predictions(
            prepared,
            evaluation,
            baseline_predictions,
        )
        selected_evaluation = evaluate_confirmation_predictions(
            prepared,
            evaluation,
            selected_predictions,
        )

    decision, family_gain = confirmation_decision(baseline_evaluation, selected_evaluation)
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": decision,
        "diagnostic_only": True,
        "official_gate_eligible": False,
        "production_eligible": False,
        "selection_frozen_before_confirmation": True,
        "historical_metadata_r2_compared": False,
        "baseline_control_scope": "simultaneous_same_untouched_cohort",
        "confirmation_lock": {
            "path": str(paths["lock_output"]),
            "lock_sha256": lock_sha256,
            "file_sha256": lock_file_sha256,
        },
        "provenance": {
            "data_path": str(paths["data"]),
            "data_sha256": sha256_bytes(data_bytes),
            "frozen_selection_manifest_path": str(paths["frozen_selection_manifest"]),
            "frozen_selection_manifest_sha256": sha256_bytes(frozen_bytes),
            "frozen_selection_sha256": frozen["selection_sha256"],
            "untouched_plan_manifest_path": str(paths["untouched_plan_manifest"]),
            "untouched_plan_manifest_sha256": sha256_bytes(untouched_manifest_bytes),
            "audit_case_plan_path": str(paths["audit_case_plan"]),
            "audit_case_plan_sha256": untouched["audit_case_plan_sha256"],
            "confirmation_script_sha256": confirmation_script_sha256,
        },
        "test_evaluation": evaluation_contract,
        "prepared_data_contract": prepared_contract,
        "selected_family_by_target": frozen["selected_family_by_target"],
        "baseline_control": baseline_evaluation,
        "selected_families": selected_evaluation,
        "summary": {
            "decision_rule": DECISION_RULE,
            "family_gain": family_gain,
            "baseline_primary_min_r2": baseline_evaluation["primary_min_r2"],
            "baseline_primary_avg_r2": baseline_evaluation["primary_avg_r2"],
            "baseline_voltage_r2": baseline_evaluation["voltage_r2"],
            "selected_primary_min_r2": selected_evaluation["primary_min_r2"],
            "selected_primary_avg_r2": selected_evaluation["primary_avg_r2"],
            "selected_voltage_r2": selected_evaluation["voltage_r2"],
        },
    }
    diagnostic._publish_report(paths["output"], report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Confirm a frozen IPMSM v2 model-family selection on its untouched test cohort.",
    )
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--baseline-metadata", type=Path, required=True)
    parser.add_argument("--frozen-selection-manifest", type=Path, required=True)
    parser.add_argument("--audit-case-plan", type=Path, required=True)
    parser.add_argument("--untouched-plan-manifest", type=Path, required=True)
    parser.add_argument("--full-case-plan", type=Path, required=True)
    parser.add_argument("--explored-case-plan", type=Path, required=True)
    parser.add_argument("--lock-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--n-jobs", type=int, default=-1)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run_confirmation(args)
    except (ConfirmationError, diagnostic.DiagnosticError, OSError, ValueError) as exc:
        parser.error(str(exc))
    summary = report["summary"]
    print(
        "model_family_untouched_confirmation "
        f"status={report['status']} "
        f"baseline_avg={summary['baseline_primary_avg_r2']} "
        f"selected_avg={summary['selected_primary_avg_r2']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
