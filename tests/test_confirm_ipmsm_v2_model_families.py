from __future__ import annotations

import copy
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import build_ipmsm_untouched_test_plan as builder
import confirm_ipmsm_v2_model_families as confirmation
import diagnose_ipmsm_v2_model_families as diagnostic
import train_ipmsm_lightgbm as trainer


HEADERS = ["case_id", "geometry_group_id", "doe_split", "value"]


def row(case: str, group: str, split: str) -> dict[str, str]:
    return {"case_id": case, "geometry_group_id": group, "doe_split": split, "value": case}


def frozen_selection_payload(metadata_sha256: str) -> dict[str, object]:
    specs = [item.as_dict() for item in diagnostic.candidate_specs(20)]
    requested = (*diagnostic.COUPLED_REQUESTED, *diagnostic.INDEPENDENT_REQUESTED)
    return {
        "evidence_scope": "adaptive_exploration",
        "data_sha256": "1" * 64,
        "baseline_metadata_sha256": metadata_sha256,
        "trainer_sha256": diagnostic.file_sha256(Path(trainer.__file__).resolve()),
        "diagnostic_script_sha256": diagnostic.file_sha256(Path(diagnostic.__file__).resolve()),
        "audit_case_plan_sha256": "",
        "outer_test_group_ids_sha256": diagnostic.ordered_text_sha256(
            [f"g{index:02d}" for index in range(15)]
        ),
        "seed": 42,
        "ensemble_size": 5,
        "fingerprints": {name: "fixed" for name in trainer.V2_FINGERPRINT_COLUMNS},
        "package_versions": confirmation._package_versions(),
        "inner_fit_group_ids_sha256": "2" * 64,
        "inner_holdout_group_ids_sha256": "3" * 64,
        "candidate_specs": specs,
        "baseline_params_by_target": {name: {} for name in requested},
        "selected_family_by_target": {name: "lightgbm" for name in requested},
        "coupled_score": {"worst_NRMSE": 1.0, "mean_NRMSE": 1.0},
    }


def frozen_manifest(metadata_sha256: str) -> tuple[dict[str, object], str]:
    selection = frozen_selection_payload(metadata_sha256)
    selection_sha256 = diagnostic.canonical_sha256(selection)
    manifest: dict[str, object] = {
        "schema_version": confirmation.FROZEN_SELECTION_SCHEMA_VERSION,
        "diagnostic_only": True,
        "official_gate_eligible": False,
        "production_eligible": False,
        "outer_test_evaluated": False,
        "evidence_scope": "adaptive_exploration",
        "test_reused_for_adaptive_exploration": True,
        "selection_sha256": selection_sha256,
        "selection": selection,
    }
    return manifest, selection_sha256


class FrozenSelectionTests(unittest.TestCase):
    def test_one_shot_trust_anchors_are_committed_constants(self) -> None:
        self.assertEqual(
            confirmation.FROZEN_SELECTION_MANIFEST_SHA256,
            "520ecf1af568c1239b1c94815a89c68914ea4511e4ba50ec321229fb6c32c548",
        )
        self.assertEqual(
            confirmation.FROZEN_SELECTION_SHA256,
            "a9abb71fb8f0c39e4b5cc1fd65ecee1a9d25867ec7d8bd42f04c1423f5503d10",
        )
        self.assertEqual(
            confirmation.UNTOUCHED_PLAN_MANIFEST_SHA256,
            "0ffb1a966526a3a30073683a806f6be1c96c16c6468deaa5f9782e33f18cbb74",
        )

    def test_requires_exact_manifest_and_canonical_selection_hashes(self) -> None:
        metadata_sha256 = "a" * 64
        manifest, selection_sha256 = frozen_manifest(metadata_sha256)
        raw = json.dumps(manifest, sort_keys=True).encode("utf-8")
        file_sha256 = confirmation.sha256_bytes(raw)
        validated = confirmation.validate_frozen_selection(
            manifest,
            manifest_sha256=file_sha256,
            expected_manifest_sha256=file_sha256,
            expected_selection_sha256=selection_sha256,
            baseline_metadata_sha256=metadata_sha256,
        )
        self.assertEqual(validated["selection_sha256"], selection_sha256)
        self.assertEqual(set(validated["selected_family_by_target"].values()), {"lightgbm"})

        tampered = copy.deepcopy(manifest)
        tampered["selection"]["seed"] = 7  # type: ignore[index]
        with self.assertRaisesRegex(confirmation.ConfirmationError, "canonical"):
            confirmation.validate_frozen_selection(
                tampered,
                manifest_sha256=file_sha256,
                expected_manifest_sha256=file_sha256,
                expected_selection_sha256=selection_sha256,
                baseline_metadata_sha256=metadata_sha256,
            )

    def test_rejects_reselection_or_changed_adaptive_implementation(self) -> None:
        metadata_sha256 = "a" * 64
        manifest, selection_sha256 = frozen_manifest(metadata_sha256)
        manifest["outer_test_evaluated"] = True
        raw_sha = "b" * 64
        with self.assertRaisesRegex(confirmation.ConfirmationError, "root field"):
            confirmation.validate_frozen_selection(
                manifest,
                manifest_sha256=raw_sha,
                expected_manifest_sha256=raw_sha,
                expected_selection_sha256=selection_sha256,
                baseline_metadata_sha256=metadata_sha256,
            )
        manifest, selection_sha256 = frozen_manifest(metadata_sha256)
        manifest["selection"]["diagnostic_script_sha256"] = "0" * 64  # type: ignore[index]
        manifest["selection_sha256"] = diagnostic.canonical_sha256(manifest["selection"])  # type: ignore[arg-type]
        with self.assertRaisesRegex(confirmation.ConfirmationError, "implementation differs"):
            confirmation.validate_frozen_selection(
                manifest,
                manifest_sha256=raw_sha,
                expected_manifest_sha256=raw_sha,
                expected_selection_sha256=str(manifest["selection_sha256"]),
                baseline_metadata_sha256=metadata_sha256,
            )


class UntouchedContractTests(unittest.TestCase):
    def test_recomputes_exact_eight_by_six_and_binds_all_plan_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full_path = root / "full.csv"
            explored_path = root / "explored.csv"
            audit_path = root / "audit.csv"
            full_rows = [
                row(f"g{group:02d}-{case}", f"g{group:02d}", "test")
                for group in range(23)
                for case in range(6)
            ]
            explored_rows = [item for item in full_rows if int(item["geometry_group_id"][1:]) < 15]
            selected_rows = [item for item in full_rows if int(item["geometry_group_id"][1:]) >= 15]
            full_payload = builder.encode_csv(HEADERS, full_rows)
            explored_payload = builder.encode_csv(HEADERS, explored_rows)
            audit_payload = builder.encode_csv(HEADERS, selected_rows)
            full_path.write_bytes(full_payload)
            explored_path.write_bytes(explored_payload)
            audit_path.write_bytes(audit_payload)
            group_hash = diagnostic.ordered_text_sha256([f"g{index:02d}" for index in range(15, 23)])
            manifest = {
                "schema_version": confirmation.UNTOUCHED_MANIFEST_SCHEMA_VERSION,
                "status": "output_expected",
                "full_plan_sha256": confirmation.sha256_bytes(full_payload),
                "explored_plan_sha256": confirmation.sha256_bytes(explored_payload),
                "output_sha256": confirmation.sha256_bytes(audit_payload),
                "script_sha256": diagnostic.file_sha256(Path(builder.__file__).resolve()),
                "counts": {
                    "expected_untouched_groups": 8,
                    "expected_rows_per_group": 6,
                    "untouched_test_groups": 8,
                    "untouched_test_rows": 48,
                    "untouched_group_ids_sha256": group_hash,
                },
            }
            manifest_sha256 = confirmation.sha256_bytes(
                json.dumps(manifest, sort_keys=True).encode("utf-8")
            )
            frozen = frozen_selection_payload("a" * 64)
            validated = confirmation.validate_untouched_contract(
                full_plan=full_path,
                explored_plan=explored_path,
                audit_case_plan=audit_path,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                expected_manifest_sha256=manifest_sha256,
                frozen_selection=frozen,
            )
            self.assertEqual(validated["audit_rows"], 48)
            self.assertEqual(validated["audit_groups"], 8)
            self.assertEqual(validated["untouched_group_ids_sha256"], group_hash)

            audit_path.write_bytes(builder.encode_csv(HEADERS, selected_rows[:-1]))
            with self.assertRaisesRegex(confirmation.ConfirmationError, "audit case plan differs"):
                confirmation.validate_untouched_contract(
                    full_plan=full_path,
                    explored_plan=explored_path,
                    audit_case_plan=audit_path,
                    manifest=manifest,
                    manifest_sha256=manifest_sha256,
                    expected_manifest_sha256=manifest_sha256,
                    frozen_selection=frozen,
                )


class PhysicalAndDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        names = (*diagnostic.PRIMARY_DIRECT, "output_phase_voltage_last_peak_abs_v")
        self.prepared = SimpleNamespace(output_name_map={name: name for name in names})
        self.x = pd.DataFrame(
            {
                "input_i_peak_a": [10.0, 12.0, 14.0, 16.0],
                "input_phase_resistance_ohm": [0.1, 0.1, 0.1, 0.1],
                "input_base_rpm": [1000.0, 1200.0, 1400.0, 1600.0],
            }
        )
        values = {
            "output_coreloss_last_avg_w": [10.0, 12.0, 14.0, 16.0],
            "output_ld_last_avg_h": [0.01, 0.011, 0.012, 0.013],
            "output_lq_last_avg_h": [0.02, 0.021, 0.022, 0.023],
            "output_solidloss_last_avg_w": [5.0, 6.0, 7.0, 8.0],
            "output_torque_last_avg_nm": [20.0, 22.0, 24.0, 26.0],
            "output_torque_last_max_nm": [21.0, 23.0, 25.0, 27.0],
            "output_phase_voltage_last_peak_abs_v": [100.0, 110.0, 120.0, 130.0],
        }
        self.split = SimpleNamespace(x_test=self.x, y_test=pd.DataFrame(values))
        self.predictions = {name: np.asarray(value, dtype=float) for name, value in values.items()}

    def test_confirmation_adds_torque_peak_cross_target_invariant(self) -> None:
        predictions = dict(self.predictions)
        predictions["output_torque_last_max_nm"] = np.ones(4)
        result = confirmation.evaluate_confirmation_predictions(
            self.prepared,
            self.split,
            predictions,
        )
        self.assertFalse(result["physical_validity"]["passed"])
        self.assertEqual(
            result["physical_validity"]["prediction"]["cross_target"]["torque_max_below_avg"],
            4,
        )

    def test_confirmation_decision_is_predeclared_and_fail_closed(self) -> None:
        baseline = {
            "primary_avg_r2": 0.5,
            "primary_min_r2": 0.2,
            "voltage_r2": 0.8,
            "physical_validity": {"passed": True},
        }
        selected = {
            "primary_avg_r2": 0.7,
            "primary_min_r2": 0.4,
            "voltage_r2": 0.8,
            "physical_validity": {"passed": True},
        }
        self.assertEqual(
            confirmation.confirmation_decision(baseline, selected),
            ("positive_confirmation", True),
        )
        selected["voltage_r2"] = 0.7
        self.assertEqual(
            confirmation.confirmation_decision(baseline, selected),
            ("negative_confirmation", False),
        )
        selected["physical_validity"] = {"passed": False}
        self.assertEqual(
            confirmation.confirmation_decision(baseline, selected),
            ("invalid", False),
        )


class PreparedDataContractTests(unittest.TestCase):
    def test_requires_exact_full_plan_identity_and_split_group_hashes(self) -> None:
        identity = {
            "train-1": ("g-train", "train"),
            "cal-1": ("g-cal", "calibration"),
            "test-1": ("g-test", "test"),
        }
        valid_df = pd.DataFrame(
            [
                {"case_id": case_id, "geometry_group_id": group_id, "doe_split": split_name}
                for case_id, (group_id, split_name) in identity.items()
            ]
        )
        prepared = SimpleNamespace(
            geometry_group_column="geometry_group_id",
            valid_df=valid_df,
        )
        outer = SimpleNamespace(
            train_group_ids=("g-train",),
            val_group_ids=("g-cal",),
            test_group_ids=("g-test",),
        )
        split_hashes = {
            "train": diagnostic.ordered_text_sha256(["g-train"]),
            "calibration": diagnostic.ordered_text_sha256(["g-cal"]),
            "test": diagnostic.ordered_text_sha256(["g-test"]),
        }
        contract = confirmation.validate_prepared_data_contract(
            prepared,
            outer,
            expected_case_identity=identity,
            expected_split_group_hashes=split_hashes,
        )
        self.assertEqual(contract["case_rows"], 3)
        self.assertEqual(contract["case_identity_sha256"], confirmation._case_identity_sha256(identity))

        prepared.valid_df = pd.concat(
            [
                valid_df,
                pd.DataFrame(
                    [{"case_id": "extra", "geometry_group_id": "g-extra", "doe_split": "train"}]
                ),
            ],
            ignore_index=True,
        )
        with self.assertRaisesRegex(confirmation.ConfirmationError, "identity differs"):
            confirmation.validate_prepared_data_contract(
                prepared,
                outer,
                expected_case_identity=identity,
                expected_split_group_hashes=split_hashes,
            )

        prepared.valid_df = valid_df
        outer.test_group_ids = ("wrong",)
        with self.assertRaisesRegex(confirmation.ConfirmationError, "group hashes differ"):
            confirmation.validate_prepared_data_contract(
                prepared,
                outer,
                expected_case_identity=identity,
                expected_split_group_hashes=split_hashes,
            )


class ConfirmationLifecycleTests(unittest.TestCase):
    REQUESTED = (*diagnostic.COUPLED_REQUESTED, *diagnostic.INDEPENDENT_REQUESTED)

    def args(self, root: Path, *, resume: bool) -> SimpleNamespace:
        return SimpleNamespace(
            data=root / "data.csv",
            baseline_metadata=root / "baseline-metadata.json",
            frozen_selection_manifest=root / "selection.json",
            audit_case_plan=root / "audit.csv",
            untouched_plan_manifest=root / "untouched.json",
            full_case_plan=root / "full.csv",
            explored_case_plan=root / "explored.csv",
            lock_output=root / "confirmation.lock.json",
            output=root / "confirmation.json",
            n_jobs=1,
            resume=resume,
        )

    def context(self, args: SimpleNamespace) -> dict[str, object]:
        paths = confirmation._confirmation_paths(args)
        selected = {name: "lightgbm" for name in self.REQUESTED}
        baseline_params = {name: {} for name in self.REQUESTED}
        fingerprints = {name: "fixed" for name in trainer.V2_FINGERPRINT_COLUMNS}
        data_bytes = b"frozen-data"
        frozen_bytes = b"frozen-selection"
        untouched_manifest_bytes = b"untouched-manifest"
        lock_payload = {
            "data_sha256": confirmation.sha256_bytes(data_bytes),
            "frozen_selection_manifest_sha256": confirmation.sha256_bytes(frozen_bytes),
            "untouched_plan_manifest_sha256": confirmation.sha256_bytes(
                untouched_manifest_bytes
            ),
            "confirmation_script_sha256": "4" * 64,
        }
        lock_sha256 = diagnostic.canonical_sha256(lock_payload)
        lock = {
            "schema_version": confirmation.LOCK_SCHEMA_VERSION,
            "status": "locked_before_untouched_prediction",
            "diagnostic_only": True,
            "official_gate_eligible": False,
            "production_eligible": False,
            "lock_sha256": lock_sha256,
            "lock": lock_payload,
        }
        return {
            "data_bytes": data_bytes,
            "metadata_bytes": b"frozen-metadata",
            "metadata": {},
            "frozen_bytes": frozen_bytes,
            "frozen": {
                "selection_sha256": "5" * 64,
                "selected_family_by_target": selected,
                "baseline_params_by_target": baseline_params,
                "fingerprints": fingerprints,
                "seed": 42,
                "ensemble_size": 5,
                "specs": (SimpleNamespace(name="lightgbm", kind="lightgbm"),),
            },
            "untouched_manifest_bytes": untouched_manifest_bytes,
            "untouched": {
                "audit_case_plan_bytes": b"audit-plan",
                "audit_case_plan_sha256": "6" * 64,
                "audit_rows": 48,
                "audit_groups": 8,
                "full_case_rows": 700,
                "full_case_identity": {"case-1": ("geometry-1", "train")},
                "full_case_identity_sha256": "7" * 64,
                "full_split_group_ids_sha256": {
                    "train": "8" * 64,
                    "calibration": "9" * 64,
                    "test": "a" * 64,
                },
                "untouched_group_ids": tuple(f"group-{index}" for index in range(8)),
            },
            "confirmation_script_sha256": "4" * 64,
            "expected_evaluation_contract": {
                "rows": 48,
                "groups": 8,
                "case_plan": str(paths["audit_case_plan"]),
                "case_plan_sha256": "6" * 64,
            },
            "lock_payload": lock_payload,
            "lock_sha256": lock_sha256,
            "lock": lock,
        }

    def evaluation(self, r2: float, *, physically_valid: bool = True) -> dict[str, object]:
        targets = (
            *diagnostic.PRIMARY_DIRECT,
            *diagnostic.DERIVED_REQUESTED,
            "output_phase_voltage_last_peak_abs_v",
        )
        rows: list[dict[str, object]] = []
        for target in targets:
            item: dict[str, object] = {
                "target": target,
                "status": "ok",
                "rows": 48,
                "invalid_prediction_rows": 0,
                "MAE": 0.0,
                "RMSE": 0.0,
                "MAPE_pct": 0.0,
                "R2": r2,
                "NRMSE": 0.0,
            }
            if target == "output_phase_voltage_last_peak_abs_v":
                item["role"] = "auxiliary_voltage"
            rows.append(item)
        direct_fields = {
            *diagnostic.PRIMARY_DIRECT,
            "output_phase_voltage_last_peak_abs_v",
        }
        direct = {name: 0 for name in direct_fields}
        derived = {
            "torque_nonpositive": 0,
            "core_negative": 0,
            "solid_negative": 0,
            "derived_invalid": 0,
        }
        cross = {"torque_max_below_avg": 0}
        truth = {
            "direct": dict(direct),
            "derived": dict(derived),
            "cross_target": dict(cross),
        }
        prediction = copy.deepcopy(truth)
        if not physically_valid:
            prediction["direct"]["output_torque_last_avg_nm"] = 1
        primary_average = sum([r2] * 8) / 8
        return {
            "rows": rows,
            "primary_metric_count": 8,
            "primary_complete": True,
            "primary_min_r2": r2,
            "primary_avg_r2": primary_average,
            "voltage_r2": r2,
            "physical_validity": {
                "truth": truth,
                "prediction": prediction,
                "passed": physically_valid,
            },
        }

    def synchronize_evaluation_aggregates(self, evaluation: dict[str, object]) -> None:
        rows = evaluation["rows"]
        assert isinstance(rows, list)
        primary_r2 = [row["R2"] for row in rows[:8]]
        complete = all(value is not None for value in primary_r2)
        evaluation["primary_complete"] = complete
        evaluation["primary_min_r2"] = min(primary_r2) if complete else None
        evaluation["primary_avg_r2"] = (
            sum(primary_r2) / len(primary_r2) if complete else None
        )
        evaluation["voltage_r2"] = rows[-1]["R2"]

    def synchronize_report_decision(self, report: dict[str, object]) -> None:
        baseline = report["baseline_control"]
        selected = report["selected_families"]
        assert isinstance(baseline, dict)
        assert isinstance(selected, dict)
        self.synchronize_evaluation_aggregates(baseline)
        self.synchronize_evaluation_aggregates(selected)
        status, family_gain = confirmation.confirmation_decision(baseline, selected)
        report["status"] = status
        report["summary"] = {
            "decision_rule": confirmation.DECISION_RULE,
            "family_gain": family_gain,
            "baseline_primary_min_r2": baseline["primary_min_r2"],
            "baseline_primary_avg_r2": baseline["primary_avg_r2"],
            "baseline_voltage_r2": baseline["voltage_r2"],
            "selected_primary_min_r2": selected["primary_min_r2"],
            "selected_primary_avg_r2": selected["primary_avg_r2"],
            "selected_voltage_r2": selected["voltage_r2"],
        }

    def report(
        self,
        args: SimpleNamespace,
        context: dict[str, object],
        baseline: dict[str, object],
        selected: dict[str, object],
    ) -> dict[str, object]:
        paths = confirmation._confirmation_paths(args)
        lock_bytes = confirmation._canonical_document_bytes(context["lock"])
        status, family_gain = confirmation.confirmation_decision(baseline, selected)
        frozen = context["frozen"]
        untouched = context["untouched"]
        lock_payload = context["lock_payload"]
        assert isinstance(frozen, dict)
        assert isinstance(untouched, dict)
        assert isinstance(lock_payload, dict)
        return {
            "schema_version": confirmation.SCHEMA_VERSION,
            "status": status,
            "diagnostic_only": True,
            "official_gate_eligible": False,
            "production_eligible": False,
            "selection_frozen_before_confirmation": True,
            "historical_metadata_r2_compared": False,
            "baseline_control_scope": "simultaneous_same_untouched_cohort",
            "confirmation_lock": {
                "path": str(paths["lock_output"]),
                "lock_sha256": context["lock_sha256"],
                "file_sha256": confirmation.sha256_bytes(lock_bytes),
            },
            "provenance": {
                "data_path": str(paths["data"]),
                "data_sha256": lock_payload["data_sha256"],
                "frozen_selection_manifest_path": str(paths["frozen_selection_manifest"]),
                "frozen_selection_manifest_sha256": lock_payload[
                    "frozen_selection_manifest_sha256"
                ],
                "frozen_selection_sha256": frozen["selection_sha256"],
                "untouched_plan_manifest_path": str(paths["untouched_plan_manifest"]),
                "untouched_plan_manifest_sha256": lock_payload[
                    "untouched_plan_manifest_sha256"
                ],
                "audit_case_plan_path": str(paths["audit_case_plan"]),
                "audit_case_plan_sha256": untouched["audit_case_plan_sha256"],
                "confirmation_script_sha256": context["confirmation_script_sha256"],
            },
            "test_evaluation": context["expected_evaluation_contract"],
            "prepared_data_contract": {
                "case_rows": untouched["full_case_rows"],
                "case_identity_sha256": untouched["full_case_identity_sha256"],
                "split_group_ids_sha256": untouched["full_split_group_ids_sha256"],
            },
            "selected_family_by_target": frozen["selected_family_by_target"],
            "baseline_control": baseline,
            "selected_families": selected,
            "summary": {
                "decision_rule": confirmation.DECISION_RULE,
                "family_gain": family_gain,
                "baseline_primary_min_r2": baseline["primary_min_r2"],
                "baseline_primary_avg_r2": baseline["primary_avg_r2"],
                "baseline_voltage_r2": baseline["voltage_r2"],
                "selected_primary_min_r2": selected["primary_min_r2"],
                "selected_primary_avg_r2": selected["primary_avg_r2"],
                "selected_voltage_r2": selected["voltage_r2"],
            },
        }

    @contextmanager
    def execution_patches(
        self,
        context: dict[str, object],
        baseline: dict[str, object],
        selected: dict[str, object],
        *,
        context_sequence: list[dict[str, object]] | None = None,
        before_fit: object | None = None,
        after_evaluations: object | None = None,
    ):
        frozen = context["frozen"]
        untouched = context["untouched"]
        assert isinstance(frozen, dict)
        assert isinstance(untouched, dict)
        requested = self.REQUESTED
        prepared = SimpleNamespace(
            output_columns=tuple(diagnostic.PRIMARY_DIRECT),
            auxiliary_output_columns=("output_phase_voltage_last_peak_abs_v",),
            output_name_map={name: name for name in requested},
        )
        outer = SimpleNamespace(x_train=None, y_train=None)
        evaluation = SimpleNamespace(
            x_test=[0] * 48,
            test_group_ids=untouched["untouched_group_ids"],
        )
        evaluation_values = iter((baseline, selected))

        def evaluate(*_: object, **__: object) -> dict[str, object]:
            value = next(evaluation_values)
            if value is selected and callable(after_evaluations):
                after_evaluations()
            return value

        def dependencies() -> object:
            if callable(before_fit):
                before_fit()
            return object()

        with ExitStack() as stack:
            if context_sequence is None:
                stack.enter_context(
                    mock.patch.object(
                        confirmation,
                        "_build_confirmation_context",
                        return_value=context,
                    )
                )
            else:
                stack.enter_context(
                    mock.patch.object(
                        confirmation,
                        "_build_confirmation_context",
                        side_effect=context_sequence,
                    )
                )
            require = stack.enter_context(
                mock.patch.object(trainer, "require_training_dependencies", side_effect=dependencies)
            )
            stack.enter_context(
                mock.patch.object(trainer, "prepare_training_data", return_value=prepared)
            )
            stack.enter_context(mock.patch.object(trainer, "split_training_data", return_value=outer))
            stack.enter_context(
                mock.patch.object(
                    confirmation,
                    "validate_prepared_data_contract",
                    return_value={
                        "case_rows": untouched["full_case_rows"],
                        "case_identity_sha256": untouched["full_case_identity_sha256"],
                        "split_group_ids_sha256": untouched["full_split_group_ids_sha256"],
                    },
                )
            )
            stack.enter_context(
                mock.patch.object(
                    trainer,
                    "select_v2_test_evaluation_split",
                    return_value=(evaluation, context["expected_evaluation_contract"]),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    diagnostic,
                    "_baseline_params",
                    return_value=frozen["baseline_params_by_target"],
                )
            )
            stack.enter_context(
                mock.patch.object(
                    diagnostic,
                    "_metadata_fingerprints",
                    return_value=frozen["fingerprints"],
                )
            )
            ensemble = stack.enter_context(
                mock.patch.object(
                    diagnostic,
                    "_ensemble_lgbm_prediction",
                    return_value=np.zeros(48),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    confirmation,
                    "evaluate_confirmation_predictions",
                    side_effect=evaluate,
                )
            )
            yield {"require": require, "ensemble": ensemble}

    def publish_lock(self, args: SimpleNamespace, context: dict[str, object]) -> None:
        confirmation.diagnostic._publish_report(Path(args.lock_output), context["lock"])

    def publish_report(
        self,
        args: SimpleNamespace,
        report: dict[str, object],
    ) -> None:
        confirmation.diagnostic._publish_report(Path(args.output), report)

    def test_strict_metric_semantics_accept_producer_edge_states(self) -> None:
        negative_r2 = self.evaluation(-1.0e100)
        negative_rows = negative_r2["rows"]
        assert isinstance(negative_rows, list)
        negative_rows[0]["MAPE_pct"] = None

        constant_truth = self.evaluation(0.2)
        constant_rows = constant_truth["rows"]
        assert isinstance(constant_rows, list)
        constant_rows[0]["status"] = "constant_truth"
        constant_rows[0]["R2"] = None
        constant_rows[0]["MAPE_pct"] = None
        self.synchronize_evaluation_aggregates(constant_truth)

        invalid_prediction = self.evaluation(0.2)
        invalid_rows = invalid_prediction["rows"]
        assert isinstance(invalid_rows, list)
        invalid_row = invalid_rows[0]
        invalid_row["status"] = "invalid_prediction"
        invalid_row["invalid_prediction_rows"] = 1
        for metric in ("MAE", "RMSE", "MAPE_pct", "R2", "NRMSE"):
            invalid_row[metric] = None
        physical = invalid_prediction["physical_validity"]
        assert isinstance(physical, dict)
        prediction = physical["prediction"]
        assert isinstance(prediction, dict)
        direct = prediction["direct"]
        assert isinstance(direct, dict)
        direct[invalid_row["target"]] = 1
        physical["passed"] = False
        self.synchronize_evaluation_aggregates(invalid_prediction)

        for name, evaluation in (
            ("negative_unbounded_r2", negative_r2),
            ("constant_truth", constant_truth),
            ("invalid_prediction", invalid_prediction),
        ):
            with self.subTest(name=name):
                self.assertIs(
                    confirmation._audit_evaluation(
                        evaluation,
                        expected_rows=48,
                        label=name,
                    ),
                    evaluation,
                )

    def test_fresh_and_valid_lock_only_resume_preserve_frozen_selection(self) -> None:
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                args = self.args(root, resume=resume)
                context = self.context(args)
                baseline = self.evaluation(0.5)
                selected = self.evaluation(0.7)
                if resume:
                    self.publish_lock(args, context)

                def lock_precedes_fit() -> None:
                    self.assertTrue(Path(args.lock_output).is_file())
                    self.assertFalse(Path(args.output).exists())

                with self.execution_patches(
                    context,
                    baseline,
                    selected,
                    before_fit=lock_precedes_fit,
                ) as calls:
                    report = confirmation.run_confirmation(args)

                self.assertEqual(report["status"], "positive_confirmation")
                self.assertEqual(
                    report["selected_family_by_target"],
                    context["frozen"]["selected_family_by_target"],
                )
                self.assertTrue(Path(args.lock_output).is_file())
                self.assertTrue(Path(args.output).is_file())
                self.assertEqual(calls["require"].call_count, 1)
                self.assertEqual(calls["ensemble"].call_count, len(self.REQUESTED))

    def test_resume_complete_negative_or_invalid_is_terminal_without_fit(self) -> None:
        cases = (
            (self.evaluation(0.5), self.evaluation(0.4), "negative_confirmation"),
            (self.evaluation(0.5), self.evaluation(0.7, physically_valid=False), "invalid"),
        )
        for baseline, selected, expected_status in cases:
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory() as tmp:
                args = self.args(Path(tmp), resume=True)
                context = self.context(args)
                self.publish_lock(args, context)
                expected = self.report(args, context, baseline, selected)
                self.publish_report(args, expected)
                with (
                    mock.patch.object(
                        confirmation,
                        "_build_confirmation_context",
                        return_value=context,
                    ),
                    mock.patch.object(trainer, "require_training_dependencies") as fit,
                ):
                    actual = confirmation.run_confirmation(args)

                self.assertEqual(actual, expected)
                self.assertEqual(actual["status"], expected_status)
                self.assertTrue(actual["diagnostic_only"])
                self.assertFalse(actual["official_gate_eligible"])
                self.assertFalse(actual["production_eligible"])
                fit.assert_not_called()

    def test_resume_rejects_canonical_self_consistent_metric_semantic_tampering(self) -> None:
        null_metrics = {
            "MAE": None,
            "RMSE": None,
            "MAPE_pct": None,
            "R2": None,
            "NRMSE": None,
        }
        cases = (
            ("ok_r2_above_one", "ok", 0, {"R2": 1.0000001}, False),
            ("negative_mae", "ok", 0, {"MAE": -0.1}, False),
            ("negative_rmse", "ok", 0, {"RMSE": -0.1}, False),
            ("negative_mape", "ok", 0, {"MAPE_pct": -0.1}, False),
            ("negative_nrmse", "ok", 0, {"NRMSE": -0.1}, False),
            ("ok_nonzero_invalid_count", "ok", 1, {}, False),
            ("ok_null_r2", "ok", 0, {"R2": None}, False),
            ("constant_truth_with_r2", "constant_truth", 0, {}, False),
            (
                "constant_truth_nonzero_invalid_count",
                "constant_truth",
                1,
                {"R2": None},
                False,
            ),
            (
                "constant_truth_null_mae",
                "constant_truth",
                0,
                {"MAE": None, "R2": None},
                False,
            ),
            ("invalid_prediction_zero_count", "invalid_prediction", 0, null_metrics, True),
            ("invalid_prediction_count_above_rows", "invalid_prediction", 49, null_metrics, True),
            (
                "invalid_prediction_nonnull_metric",
                "invalid_prediction",
                1,
                {**null_metrics, "MAE": 0.0},
                True,
            ),
        )
        for name, status, invalid_count, updates, physical_invalid in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                args = self.args(Path(tmp), resume=True)
                context = self.context(args)
                self.publish_lock(args, context)
                expected = self.report(
                    args,
                    context,
                    self.evaluation(0.5),
                    self.evaluation(0.7),
                )
                selected = expected["selected_families"]
                assert isinstance(selected, dict)
                rows = selected["rows"]
                assert isinstance(rows, list)
                metric_row = rows[0]
                metric_row["status"] = status
                metric_row["invalid_prediction_rows"] = invalid_count
                metric_row.update(updates)
                if physical_invalid:
                    physical = selected["physical_validity"]
                    assert isinstance(physical, dict)
                    prediction = physical["prediction"]
                    assert isinstance(prediction, dict)
                    direct = prediction["direct"]
                    assert isinstance(direct, dict)
                    direct[metric_row["target"]] = 1
                    physical["passed"] = False
                self.synchronize_report_decision(expected)
                self.publish_report(args, expected)
                before = Path(args.output).read_bytes()
                self.assertEqual(before, confirmation._canonical_document_bytes(expected))

                with (
                    mock.patch.object(
                        confirmation,
                        "_build_confirmation_context",
                        return_value=context,
                    ),
                    mock.patch.object(trainer, "require_training_dependencies") as fit,
                    self.assertRaisesRegex(confirmation.ConfirmationError, "strict_metric semantics"),
                ):
                    confirmation.run_confirmation(args)

                fit.assert_not_called()
                self.assertEqual(Path(args.output).read_bytes(), before)

    def test_report_only_tampered_lock_and_pre_resume_source_drift_fail_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(Path(tmp), resume=True)
            Path(args.output).write_bytes(b"do-not-overwrite")
            before = Path(args.output).read_bytes()
            with self.assertRaisesRegex(confirmation.ConfirmationError, "without its lock"):
                confirmation.run_confirmation(args)
            self.assertEqual(Path(args.output).read_bytes(), before)

        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(Path(tmp), resume=True)
            context = self.context(args)
            tampered = copy.deepcopy(context["lock"])
            tampered["status"] = "tampered"
            confirmation.diagnostic._publish_report(Path(args.lock_output), tampered)
            with (
                mock.patch.object(
                    confirmation,
                    "_build_confirmation_context",
                    return_value=context,
                ),
                mock.patch.object(trainer, "require_training_dependencies") as fit,
                self.assertRaisesRegex(confirmation.ConfirmationError, "lock differs"),
            ):
                confirmation.run_confirmation(args)
            self.assertFalse(Path(args.output).exists())
            fit.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(Path(tmp), resume=True)
            original = self.context(args)
            self.publish_lock(args, original)
            drifted = copy.deepcopy(original)
            drifted["confirmation_script_sha256"] = "f" * 64
            drifted["lock_payload"]["confirmation_script_sha256"] = "f" * 64
            drifted["lock_sha256"] = diagnostic.canonical_sha256(drifted["lock_payload"])
            drifted["lock"]["lock_sha256"] = drifted["lock_sha256"]
            drifted["lock"]["lock"] = drifted["lock_payload"]
            with (
                mock.patch.object(
                    confirmation,
                    "_build_confirmation_context",
                    return_value=drifted,
                ),
                mock.patch.object(trainer, "require_training_dependencies") as fit,
                self.assertRaisesRegex(confirmation.ConfirmationError, "lock differs"),
            ):
                confirmation.run_confirmation(args)
            self.assertFalse(Path(args.output).exists())
            fit.assert_not_called()

    def test_final_input_or_lock_drift_fails_before_report_publication(self) -> None:
        for drift_kind in ("source", "lock"):
            with self.subTest(drift=drift_kind), tempfile.TemporaryDirectory() as tmp:
                args = self.args(Path(tmp), resume=False)
                original = self.context(args)
                baseline = self.evaluation(0.5)
                selected = self.evaluation(0.7)
                if drift_kind == "source":
                    drifted = copy.deepcopy(original)
                    drifted["lock_payload"]["data_sha256"] = "e" * 64
                    drifted["lock_sha256"] = diagnostic.canonical_sha256(
                        drifted["lock_payload"]
                    )
                    drifted["lock"]["lock_sha256"] = drifted["lock_sha256"]
                    drifted["lock"]["lock"] = drifted["lock_payload"]
                    contexts = [original, drifted]
                    after_evaluations = None
                else:
                    contexts = None

                    def after_evaluations() -> None:
                        lock = copy.deepcopy(original["lock"])
                        lock["status"] = "tampered-mid-run"
                        Path(args.lock_output).write_bytes(
                            confirmation._canonical_document_bytes(lock)
                        )

                with self.execution_patches(
                    original,
                    baseline,
                    selected,
                    context_sequence=contexts,
                    after_evaluations=after_evaluations,
                ):
                    with self.assertRaisesRegex(
                        confirmation.ConfirmationError,
                        "inputs changed|lock differs|lock file identity changed",
                    ):
                        confirmation.run_confirmation(args)

                self.assertTrue(Path(args.lock_output).is_file())
                self.assertFalse(Path(args.output).exists())

if __name__ == "__main__":
    unittest.main()
