from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import pickle
import tempfile
import unittest

import ipmsm_surrogate_bundle as bundle
import train_ipmsm_lightgbm as trainer


class ConstantEstimator:
    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def predict(self, rows):
        self.calls += 1
        return [self.value for _ in rows]


class ColumnEstimator:
    def __init__(self, index: int) -> None:
        self.index = index
        self.calls = 0

    def predict(self, rows):
        self.calls += 1
        return [row[self.index] for row in rows]


INPUT_COLUMNS = (
    "input_stator_outer_radius",
    "input_stator_back_yoke_thick_ratio",
    "input_stator_back_yoke_thick",
    "input_stack_length_mm",
    "input_base_rpm",
    "input_i_peak_a",
    "input_beta_dq_deg",
    "input_phase_resistance_ohm",
)


def metadata() -> dict:
    model_targets = (
        bundle.TORQUE_TARGET,
        bundle.CORE_LOSS_TARGET,
        bundle.SOLID_LOSS_TARGET,
        bundle.VOLTAGE_TARGET,
    )
    return {
        "training_schema": "ipmsm_v2",
        "fingerprints": {
            "input_dataset_schema_version": "ipmsm_v2",
            "input_setup_fingerprint": "setup:fixture",
            "input_quality_profile": "reference_ultra",
            "input_material_fingerprint": "material:fixture",
            "input_aedt_version": "2025.2",
            "input_beta_calibration_id": "fixture-calibration",
            "input_beta_convention": "dq_current_advance_v2",
            "input_model_extent": "full_360",
        },
        "ensemble_size": 5,
        "conformal_coverage": 0.95,
        "conformal_calibration_isolated": True,
        "r2_threshold": 0.95,
        "primary_test_r2_gate_complete": True,
        "primary_test_r2_gate_passed": True,
        "primary_test_r2": {target: 0.96 for target in bundle.PRIMARY_R2_TARGETS},
        "voltage_r2_threshold": 0.95,
        "voltage_test_r2": 0.96,
        "voltage_test_r2_gate_complete": True,
        "voltage_test_r2_gate_passed": True,
        "input_columns": list(INPUT_COLUMNS),
        "modeled_output_columns": [
            bundle.TORQUE_TARGET,
            bundle.CORE_LOSS_TARGET,
            bundle.SOLID_LOSS_TARGET,
        ],
        "auxiliary_output_columns": [bundle.VOLTAGE_TARGET],
        "output_name_map": {target: target for target in model_targets},
        "model_paths": {target: f"old/location/{target}.pkl" for target in model_targets},
        "conformal_absolute_residuals": {
            target: {
                "coverage": 0.95,
                "calibration_rows": 20,
                "rank": 20,
                "quantile_abs": quantile,
            }
            for target, quantile in zip(model_targets, (2.0, 3.0, 1.0, 5.0))
        },
        "feature_bounds_source": "train",
        "feature_bounds": {
            "input_stator_outer_radius": {"min": 120.0, "max": 200.0},
            "input_stator_back_yoke_thick_ratio": {"min": 0.1, "max": 0.15},
            "input_stator_back_yoke_thick": {"min": 12.0, "max": 30.0},
            "input_stack_length_mm": {"min": 40.0, "max": 70.0},
            "input_base_rpm": {"min": 500.0, "max": 4000.0},
            "input_i_peak_a": {"min": 0.0, "max": 200.0},
            "input_beta_dq_deg": {"min": 0.0, "max": 80.0},
            "input_phase_resistance_ohm": {"min": 0.001, "max": 1.0},
        },
    }


def write_bundle(
    root: Path,
    *,
    metadata_value: dict | None = None,
    torque_estimator=None,
) -> Path:
    raw = copy.deepcopy(metadata_value or metadata())
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.json").write_text(json.dumps(raw), encoding="utf-8")
    torque_members = (
        [copy.deepcopy(torque_estimator) for _ in range(5)]
        if torque_estimator is not None
        else [
            ConstantEstimator(100.0),
            ConstantEstimator(100.0),
            ConstantEstimator(101.0),
            ConstantEstimator(102.0),
            ConstantEstimator(102.0),
        ]
    )
    estimators = {
        bundle.TORQUE_TARGET: torque_members,
        bundle.CORE_LOSS_TARGET: [ConstantEstimator(20.0) for _ in range(5)],
        bundle.SOLID_LOSS_TARGET: [ConstantEstimator(10.0) for _ in range(5)],
        bundle.VOLTAGE_TARGET: [ConstantEstimator(150.0) for _ in range(5)],
    }
    for target, estimator in estimators.items():
        path_value = raw.get("model_paths", {}).get(target)
        if path_value is None:
            continue
        recorded = path_value[0] if isinstance(path_value, list) else path_value
        with (root / Path(recorded).name).open("wb") as stream:
            pickle.dump(estimator, stream)
    return root


def prediction_features() -> dict:
    return {
        "stator_outer_radius": 160.0,
        "stator_back_yoke_thick_ratio": 0.125,
        "stack_length_mm": 55.0,
        "speed_rpm": 2000.0,
        "current_peak_a": 100.0,
        "beta_deg": 30.0,
        "phase_resistance_ohm": 0.1,
    }


class SurrogateBundleTests(unittest.TestCase):
    def test_loads_relocatable_ensemble_and_builds_conservative_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loaded = bundle.load_surrogate_bundle(write_bundle(Path(tmp) / "model"))
            prediction = loaded.predict_one(prediction_features())

        self.assertEqual(prediction["torque_nm"], 101.0)
        self.assertEqual(prediction["torque_lcb_nm"], 99.0)
        self.assertEqual(prediction["core_loss_ucb_w"], 23.0)
        self.assertEqual(prediction["solid_loss_ucb_w"], 11.0)
        self.assertEqual(prediction["voltage_peak_ucb_v"], 155.0)
        self.assertTrue(prediction["in_domain"])
        self.assertEqual(prediction["ood_features"], ())
        self.assertEqual(loaded.summary()["targets"][bundle.TORQUE_TARGET]["ensemble_members"], 5)
        self.assertEqual(loaded.summary()["ensemble_size"], 5)
        self.assertEqual(loaded.summary()["conformal_coverage"], 0.95)
        self.assertTrue(loaded.summary()["conformal_calibration_isolated"])
        self.assertEqual(loaded.summary()["fingerprints"], metadata()["fingerprints"])
        self.assertEqual(
            bundle.REQUIRED_OPTIMIZER_FINGERPRINTS,
            trainer.V2_FINGERPRINT_COLUMNS,
        )
        self.assertEqual(len(bundle.PRIMARY_R2_TARGETS), 8)
        self.assertNotIn(bundle.VOLTAGE_TARGET, bundle.PRIMARY_R2_TARGETS)

    def test_batch_and_scalar_envelopes_are_equivalent_with_one_call_per_estimator(self) -> None:
        rows = [prediction_features() for _ in range(3)]
        rows[1]["current_peak_a"] = 150.0
        rows[2]["beta_deg"] = 85.0
        with tempfile.TemporaryDirectory() as tmp:
            model_root = write_bundle(Path(tmp) / "model")
            batch_bundle = bundle.load_surrogate_bundle(model_root)
            batch_predictions = batch_bundle.predict_many(rows)
            scalar_bundle = bundle.load_surrogate_bundle(model_root)
            scalar_predictions = [scalar_bundle.predict_one(row) for row in rows]

        self.assertEqual(batch_predictions, scalar_predictions)
        for target in batch_bundle.targets.values():
            self.assertTrue(all(estimator.calls == 1 for estimator in target.estimators))
        for target in scalar_bundle.targets.values():
            self.assertTrue(all(estimator.calls == len(rows) for estimator in target.estimators))

    def test_feature_bounds_mark_out_of_domain_without_clipping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loaded = bundle.load_surrogate_bundle(write_bundle(Path(tmp) / "model"))
            features = prediction_features()
            features["beta_deg"] = 85.0
            prediction = loaded.predict_one(features)

        self.assertFalse(prediction["in_domain"])
        self.assertIn("input_beta_dq_deg", prediction["ood_features"])
        self.assertLess(prediction["geometry_margin"], 0.0)

    def test_derives_trainer_geometry_columns_in_declared_order(self) -> None:
        raw = metadata()
        expected_yoke = 160.0 * 0.125
        raw["feature_bounds"]["input_stator_back_yoke_thick"] = {
            "min": expected_yoke,
            "max": expected_yoke,
        }
        with tempfile.TemporaryDirectory() as tmp:
            loaded = bundle.load_surrogate_bundle(
                write_bundle(
                    Path(tmp) / "model",
                    metadata_value=raw,
                    torque_estimator=ColumnEstimator(INPUT_COLUMNS.index("input_stator_back_yoke_thick")),
                )
            )
            prediction = loaded.predict_one(prediction_features())

        self.assertEqual(prediction["torque_nm"], expected_yoke)
        self.assertTrue(prediction["in_domain"])

    def test_metadata_contract_is_fail_closed(self) -> None:
        mutations = [
            (
                "training_schema",
                lambda raw: raw.__setitem__("training_schema", "legacy"),
            ),
            (
                "metadata.fingerprints",
                lambda raw: raw.pop("fingerprints"),
            ),
            (
                "input_beta_calibration_id",
                lambda raw: raw["fingerprints"].pop("input_beta_calibration_id"),
            ),
            (
                "input_quality_profile must be a nonempty string",
                lambda raw: raw["fingerprints"].__setitem__("input_quality_profile", ""),
            ),
            (
                "feature_bounds_source",
                lambda raw: raw.__setitem__("feature_bounds_source", "all_rows"),
            ),
            (
                "primary_test_r2_gate_passed",
                lambda raw: raw.__setitem__("primary_test_r2_gate_passed", False),
            ),
            (
                "voltage_test_r2_gate_complete must be true",
                lambda raw: raw.__setitem__("voltage_test_r2_gate_complete", False),
            ),
            (
                "voltage_test_r2_gate_passed must be true",
                lambda raw: raw.__setitem__("voltage_test_r2_gate_passed", False),
            ),
            (
                "feature_bounds is missing",
                lambda raw: raw["feature_bounds"].pop("input_beta_dq_deg"),
            ),
            (
                "conformal_absolute_residuals is missing",
                lambda raw: raw["conformal_absolute_residuals"].pop(bundle.CORE_LOSS_TARGET),
            ),
            (
                "auxiliary_output_columns must include",
                lambda raw: raw.__setitem__("auxiliary_output_columns", ["another_voltage"]),
            ),
            (
                "output_name_map is missing",
                lambda raw: raw["output_name_map"].pop(bundle.VOLTAGE_TARGET),
            ),
        ]
        for expected, mutate in mutations:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                raw = metadata()
                mutate(raw)
                root = write_bundle(Path(tmp) / "model", metadata_value=raw)
                with self.assertRaisesRegex(bundle.SurrogateBundleError, expected):
                    bundle.load_surrogate_bundle(root)

    def test_optimizer_fingerprint_compatibility_is_fail_closed_for_every_required_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loaded = bundle.load_surrogate_bundle(write_bundle(Path(tmp) / "model"))

        loaded.assert_fingerprint_compatible(dict(loaded.fingerprints))
        for name in bundle.REQUIRED_OPTIMIZER_FINGERPRINTS:
            with self.subTest(name=name):
                expected = dict(loaded.fingerprints)
                expected[name] = "mismatch"
                with self.assertRaisesRegex(bundle.SurrogateBundleError, name):
                    loaded.assert_fingerprint_compatible(expected)

    def test_production_ensemble_and_conformal_contract_is_fail_closed(self) -> None:
        mutations = [
            (
                "metadata.ensemble_size",
                lambda raw: raw.pop("ensemble_size"),
            ),
            (
                "ensemble_size must be >= 5",
                lambda raw: raw.__setitem__("ensemble_size", 4),
            ),
            (
                "metadata.conformal_coverage",
                lambda raw: raw.pop("conformal_coverage"),
            ),
            (
                "conformal_coverage must be >= 0.95",
                lambda raw: raw.__setitem__("conformal_coverage", 0.94),
            ),
            (
                "conformal_calibration_isolated must be true",
                lambda raw: raw.__setitem__("conformal_calibration_isolated", False),
            ),
            (
                "metadata.conformal_calibration_isolated",
                lambda raw: raw.pop("conformal_calibration_isolated"),
            ),
            (
                "coverage must equal metadata.conformal_coverage",
                lambda raw: raw["conformal_absolute_residuals"][bundle.TORQUE_TARGET].update(
                    {"coverage": 0.9, "rank": 19}
                ),
            ),
            (
                "rank must equal ceil",
                lambda raw: raw["conformal_absolute_residuals"][bundle.TORQUE_TARGET].__setitem__(
                    "rank", 19
                ),
            ),
            (
                "estimator count.*must equal metadata.ensemble_size 6",
                lambda raw: raw.__setitem__("ensemble_size", 6),
            ),
        ]
        for expected, mutate in mutations:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                raw = metadata()
                mutate(raw)
                root = write_bundle(Path(tmp) / "model", metadata_value=raw)
                with self.assertRaisesRegex(bundle.SurrogateBundleError, expected):
                    bundle.load_surrogate_bundle(root)

    def test_every_optimizer_fingerprint_is_required_and_nonblank(self) -> None:
        for name in bundle.REQUIRED_OPTIMIZER_FINGERPRINTS:
            with self.subTest(name=name, condition="missing"), tempfile.TemporaryDirectory() as tmp:
                raw = metadata()
                raw["fingerprints"].pop(name)
                root = write_bundle(Path(tmp) / "model", metadata_value=raw)
                with self.assertRaisesRegex(bundle.SurrogateBundleError, name):
                    bundle.load_surrogate_bundle(root)

            with self.subTest(name=name, condition="blank"), tempfile.TemporaryDirectory() as tmp:
                raw = metadata()
                raw["fingerprints"][name] = " "
                root = write_bundle(Path(tmp) / "model", metadata_value=raw)
                with self.assertRaisesRegex(bundle.SurrogateBundleError, name):
                    bundle.load_surrogate_bundle(root)

    def test_voltage_r2_metadata_is_required_and_thresholded(self) -> None:
        required_fields = (
            "voltage_r2_threshold",
            "voltage_test_r2",
            "voltage_test_r2_gate_complete",
            "voltage_test_r2_gate_passed",
        )
        for field in required_fields:
            with self.subTest(missing=field), tempfile.TemporaryDirectory() as tmp:
                raw = metadata()
                raw.pop(field)
                root = write_bundle(Path(tmp) / "model", metadata_value=raw)
                with self.assertRaises(bundle.SurrogateBundleError) as caught:
                    bundle.load_surrogate_bundle(root)
                self.assertIn(f"missing required field: metadata.{field}", str(caught.exception))

        low_cases = (
            ({"voltage_test_r2": 0.94}, ">= 0.95"),
            ({"voltage_r2_threshold": 0.97}, ">= 0.97"),
        )
        for changes, expected in low_cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as tmp:
                raw = metadata()
                raw.update(changes)
                root = write_bundle(Path(tmp) / "model", metadata_value=raw)
                with self.assertRaises(bundle.SurrogateBundleError) as caught:
                    bundle.load_surrogate_bundle(root)
                self.assertIn(expected, str(caught.exception))

        for field in ("voltage_r2_threshold", "voltage_test_r2"):
            with self.subTest(nonfinite=field), tempfile.TemporaryDirectory() as tmp:
                raw = metadata()
                raw[field] = math.nan
                root = write_bundle(Path(tmp) / "model", metadata_value=raw)
                with self.assertRaisesRegex(bundle.SurrogateBundleError, f"metadata.{field} must be a finite number"):
                    bundle.load_surrogate_bundle(root)

    def test_missing_artifact_and_invalid_conformal_rank_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = metadata()
            root = write_bundle(Path(tmp) / "model", metadata_value=raw)
            (root / Path(raw["model_paths"][bundle.VOLTAGE_TARGET]).name).unlink()
            with self.assertRaisesRegex(bundle.SurrogateBundleError, "artifact is missing"):
                bundle.load_surrogate_bundle(root)

        with tempfile.TemporaryDirectory() as tmp:
            raw = metadata()
            raw["conformal_absolute_residuals"][bundle.TORQUE_TARGET]["rank"] = 21
            root = write_bundle(Path(tmp) / "model", metadata_value=raw)
            with self.assertRaisesRegex(bundle.SurrogateBundleError, "rank must be between"):
                bundle.load_surrogate_bundle(root)

    def test_negative_magnitude_prediction_is_clipped_before_conformal_ucb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_bundle(Path(tmp) / "model")
            with (root / f"{bundle.CORE_LOSS_TARGET}.pkl").open("wb") as stream:
                pickle.dump([ConstantEstimator(-2.0) for _ in range(5)], stream)
            loaded = bundle.load_surrogate_bundle(root)
            prediction = loaded.predict_one(prediction_features())

        self.assertEqual(prediction["core_loss_w"], 0.0)
        self.assertEqual(prediction["core_loss_ucb_w"], 3.0)
        self.assertIn(bundle.CORE_LOSS_TARGET, prediction["clipped_nonphysical_targets"])


if __name__ == "__main__":
    unittest.main()
