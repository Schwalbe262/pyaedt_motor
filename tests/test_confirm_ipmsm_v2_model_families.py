from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
