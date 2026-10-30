"""Known motor relations test scaling, group uncertainty and artifact boundaries."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from module import variable
from ipmsm_optimization import WindingSpec, phase_resistance_100c_ohm
from pyaedt_motor import model_bundle as bundles
from pyaedt_motor import train_physical as training
from pyaedt_motor.surrogate import PrimitiveModel, derive_performance
from pyaedt_motor.model_bundle import PRODUCTION_TARGETS as TARGETS, VOLTAGE_SPAN_TARGET
from tests.test_validate_ipmsm_v2_dataset import valid_row


ROOT = Path(__file__).resolve().parents[1]


def spec():
    raw = json.loads((ROOT / "configs/ipmsm_7p5kw.json").read_text(encoding="utf-8"))
    # These synthetic historical-context rows are independent of the live M19 study.
    raw["material_context"] = {"core_material_model": "legacy_pnf_hybrid_v1", "core_material": "27PNF1500_CustomCoreLoss"}
    return raw


def fingerprints():
    return {"input_dataset_schema_version": "ipmsm_v2", "input_metric_contract_version": "motor_metrics_v3",
            "input_producer_fingerprint": "producer_v3:sha256:" + "a" * 64,
            "input_material_fingerprint": "materials_v2:sha256:fixture", "input_setup_fingerprint": "setup_v2:sha256:fixture",
            "input_beta_calibration_id": "beta-calibration:fixture", "input_beta_convention": "dq_current_advance_v2",
            "input_model_extent": "full_360", "input_quality_profile": "fixture", "input_aedt_version": "2025.2"}


def motor_inputs(lengths, currents=None, betas=None, speeds=None):
    config = spec()
    geometry = {name: (bounds[0] + bounds[1]) / 2 for name, bounds in config["design_space"].items()}
    geometry.update(slot_num=12, pole_num=8, stator_shoe_thick=1.5)
    model = SimpleNamespace()
    variable._set_fixed_geometry(model, {}, geometry)
    base = {"input_" + key: getattr(model, key) for key in variable.GEOMETRY_INPUT_COLUMNS}
    winding = WindingSpec(**config["winding"])
    rows = []
    for index, length in enumerate(lengths):
        row = dict(base)
        row.update(input_stack_length_mm=length, input_i_peak_a=(currents[index] if currents is not None else 80),
                   input_beta_dq_deg=(betas[index] if betas is not None else 30),
                   input_base_rpm=(speeds[index] if speeds is not None else 1200), input_steps_per_period=150,
                   input_phase_resistance_ohm=phase_resistance_100c_ohm(geometry, length, winding))
        rows.append(row)
    return pd.DataFrame(rows)


def known_motor(inputs):
    length = inputs.input_stack_length_mm.to_numpy()
    iq = inputs.input_i_peak_a * np.cos(np.deg2rad(inputs.input_beta_dq_deg))
    torque = .01 * length * iq
    return derive_performance(inputs, pd.DataFrame({
        "output_torque_last_avg_nm": torque, "output_torque_last_max_nm": 1.2 * torque,
        "output_coreloss_last_avg_w": 2 * length, "output_solidloss_last_avg_w": .2 * length,
        "output_ld_last_avg_h": 2e-5 * length, "output_lq_last_avg_h": 3e-5 * length,
        "output_phase_voltage_last_peak_abs_v": .009 * length * inputs.input_base_rpm * 2 * np.pi / 60,
        VOLTAGE_SPAN_TARGET: np.sqrt(3) * .009 * length * inputs.input_base_rpm * 2 * np.pi / 60,
    }, index=inputs.index))


def synthetic_rows():
    rows = []
    for group in range(42):
        label = "train" if group < 16 else ("calibration" if group < 36 else "test")
        x = motor_inputs([40 + .7 * group] * 2, [60, 120], [20, 40], [1200, 5000])
        y = known_motor(x)
        for index in range(2):
            case = valid_row(f"case-{group}-{index}", f"geometry-{group}")
            case.update(x.iloc[index].to_dict()); case.update(y.iloc[index].to_dict()); case.update(fingerprints())
            current = float(x.iloc[index].input_i_peak_a)
            beta = np.deg2rad(float(x.iloc[index].input_beta_dq_deg))
            case.update(input_series_turns_per_phase=48, input_turns_per_coil_side=12, input_winding_material="copper")
            case.update(doe_split=label, input_analysis_purpose="training", input_compute_inductance=True,
                        input_strict_setup=True, validation=True, output_steady_state_passed=True,
                        input_loss_policy="laminated_core", input_circle_segments=0, setup_issues="",
                        output_phase_voltage_source="flux_derivative_three_phase", output_power_balance_residual_last_pct=.01,
                        output_torque_cycle_drift_pct=.01, output_coreloss_cycle_drift_pct=.01, output_solidloss_cycle_drift_pct=.01,
                        input_commanded_id_peak_a=-current * np.sin(beta), input_commanded_iq_peak_a=current * np.cos(beta),
                        output_id_current_last_avg_a=-current * np.sin(beta), output_iq_current_last_avg_a=current * np.cos(beta),
                        output_phase_current_last_rms_a=current / np.sqrt(2),
                        output_copperloss_last_avg_w=1.5 * x.iloc[index].input_phase_resistance_ohm * current**2)
            for phase in "abc":
                case[f"output_phase{phase}_current_last_rms_a"] = current / np.sqrt(2)
                case[f"output_phase{phase}_voltage_last_peak_abs_v"] = y.iloc[index].output_phase_voltage_last_peak_abs_v
                case[f"output_phase{phase}_voltage_last_rms_v"] = y.iloc[index].output_phase_voltage_last_peak_abs_v / np.sqrt(2)
            rows.append(case)
    return pd.DataFrame(rows)


class PhysicalBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.x = motor_inputs([40, 50, 60, 70], [40, 80, 120, 137.8], [10, 20, 30, 40], [1200, 5000, 1200, 5000])
        truth = known_motor(cls.x)
        cls.models = {target: PrimitiveModel.fit(cls.x, target, truth[target], "physical_extra", jobs=1) for target in TARGETS}
        cls.domain = bundles.domain_from_training(cls.x, spec(), case_frame=cls.x.assign(
            input_series_turns_per_phase=48, input_turns_per_coil_side=12, input_winding_material="copper"))
        predicted = bundles.prediction_frame(cls.models, cls.x)
        scales = bundles.transformed_scales(cls.x, truth)
        xcal = pd.concat([cls.x] * 20, ignore_index=True)
        ycal = known_motor(xcal)
        pcal = bundles.prediction_frame(cls.models, xcal)
        cls.calibration = bundles.calibrate_joint(xcal, ycal, pcal, np.repeat(np.arange(20), 4), scales)
        choice = {"fit_groups": ["fit-a", "fit-b"], "selection_groups": ["fit-c", "fit-d"],
                  "test_used_for_selection": False}
        cls.metadata = {"fingerprints": fingerprints(), "domain": cls.domain, "calibration": cls.calibration,
                        "test_metrics": {target: training.score(truth[target], predicted[target]) for target in bundles.ALL_TARGETS},
                        "test_physics_issues": bundles.physics_issues(predicted), "test_used_for_selection": False,
                        "decision": "accepted", "fit_groups": ["fit-a", "fit-b", "fit-c", "fit-d"],
                        "calibration_groups": [str(i) for i in range(20)], "test_groups": ["test-a", "test-b"],
                        "selection": choice, "selection_sha256": hashlib.sha256(bundles.canonical_json(choice)).hexdigest()}

    def bundle(self):
        return bundles.PhysicalMotorBundle(self.models, deepcopy(self.metadata))

    def test_known_motor_prediction_and_coupled_uncertainty_enclose_truth(self):
        bundle = self.bundle()
        self.assertTrue(bundle.approved)
        point, lower, upper = bundle.predict(self.x, fingerprints=fingerprints())
        truth = known_motor(self.x)
        np.testing.assert_allclose(point, truth, rtol=1e-12, atol=1e-10)
        self.assertTrue((lower.to_numpy() <= truth.to_numpy() + 1e-10).all())
        self.assertTrue((upper.to_numpy() >= truth.to_numpy() - 1e-10).all())
        np.testing.assert_allclose(lower.output_total_loss_last_avg_w,
                                   lower.output_coreloss_last_avg_w + lower.output_solidloss_last_avg_w +
                                   1.5 * self.x.input_phase_resistance_ohm * self.x.input_i_peak_a**2)

    def test_nonzero_primitive_interval_corners_bound_derived_losses_and_efficiency(self):
        from itertools import product
        bundle = self.bundle()
        bundle.metadata["calibration"].update(quantile=1.0, group_scores={str(i): 1.0 for i in range(20)},
            training_scales={target: (.001 if "torque" in target or "voltage" in target else .2) for target in TARGETS})
        point, lower, upper = bundle.predict(self.x)
        for torque_side, core_side, solid_side in product([lower, upper], repeat=3):
            corner = point[list(TARGETS)].copy()
            corner["output_torque_last_avg_nm"] = torque_side.output_torque_last_avg_nm
            corner["output_coreloss_last_avg_w"] = core_side.output_coreloss_last_avg_w
            corner["output_solidloss_last_avg_w"] = solid_side.output_solidloss_last_avg_w
            derived = derive_performance(self.x, corner)
            for target in ["output_total_loss_last_avg_w", "output_efficiency_last_pct"]:
                self.assertTrue((derived[target] >= lower[target] - 1e-9).all())
                self.assertTrue((derived[target] <= upper[target] + 1e-9).all())

    def test_voltage_span_is_required_positive_bounded_and_jointly_calibrated(self):
        truth = known_motor(self.x)
        phase_peak = truth.output_phase_voltage_last_peak_abs_v
        np.testing.assert_allclose(truth[VOLTAGE_SPAN_TARGET], np.sqrt(3) * phase_peak)
        self.assertEqual(bundles.physics_issues(truth), [])
        for value in [0, -1, np.nan, np.inf]:
            bad = truth.copy(); bad[VOLTAGE_SPAN_TARGET] = value
            with self.subTest(value=value):
                self.assertTrue(bundles.physics_issues(bad))
        bad = truth.copy(); bad[VOLTAGE_SPAN_TARGET] = 2 * phase_peak + .01
        self.assertIn("voltage_span_exceeds_two_phase_peaks", bundles.physics_issues(bad))
        edge = truth.copy(); edge[VOLTAGE_SPAN_TARGET] = 2 * phase_peak
        self.assertEqual(bundles.physics_issues(edge), [])
        self.assertEqual(bundles.physics_issues(truth.drop(columns=VOLTAGE_SPAN_TARGET)), ["missing_outputs"])

        x = motor_inputs([50] * 20)
        prediction = known_motor(x); actual = prediction.copy()
        scale = x.input_stack_length_mm * x.input_base_rpm * 2 * np.pi / 60
        actual[VOLTAGE_SPAN_TARGET] += .001 * scale
        calibration = bundles.calibrate_joint(x, actual, prediction, range(20), {target: 1 for target in TARGETS})
        self.assertAlmostEqual(calibration["quantile"], .001)
        self.assertEqual(set(calibration["training_scales"]), set(TARGETS))
        self.assertTrue(all(abs(score - .001) < 1e-12 for score in calibration["group_scores"].values()))

        bundle = self.bundle()
        bundle.metadata["calibration"].update(quantile=1.0, group_scores={str(i): 1.0 for i in range(20)},
            training_scales={target: .001 for target in TARGETS})
        point, lower, upper = bundle.predict(self.x)
        width = .001 * self.x.input_stack_length_mm * self.x.input_base_rpm * 2 * np.pi / 60
        np.testing.assert_allclose(point[VOLTAGE_SPAN_TARGET] - lower[VOLTAGE_SPAN_TARGET], width)
        np.testing.assert_allclose(upper[VOLTAGE_SPAN_TARGET] - point[VOLTAGE_SPAN_TARGET], width)
        self.assertTrue((lower[VOLTAGE_SPAN_TARGET] > 0).all())

    def test_voltage_span_cannot_be_omitted_from_training_or_promotion(self):
        raw = synthetic_rows()
        training.validate_new_data(raw)
        with self.assertRaisesRegex(ValueError, "complete new motor_metrics_v3"):
            training.validate_new_data(raw.drop(columns=VOLTAGE_SPAN_TARGET))
        for value in [0, np.nan, np.inf, 1e9]:
            bad = raw.copy(); bad.loc[0, VOLTAGE_SPAN_TARGET] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                training.validate_new_data(bad)
        bundle = self.bundle(); del bundle.metadata["test_metrics"][VOLTAGE_SPAN_TARGET]
        self.assertFalse(bundle.approved)
        bundle = self.bundle(); bundle.metadata["test_metrics"][VOLTAGE_SPAN_TARGET]["r2"] = .94
        self.assertIn("r2_failed:" + VOLTAGE_SPAN_TARGET, bundles.promotion_issues(bundle.metadata))
        bundle = self.bundle(); del bundle.metadata["calibration"]["training_scales"][VOLTAGE_SPAN_TARGET]
        self.assertFalse(bundle.approved)
        bundle = self.bundle(); bundle.models = {key: value for key, value in bundle.models.items() if key != VOLTAGE_SPAN_TARGET}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifact"; bundles.save_bundle(bundle, root)
            with self.assertRaisesRegex(ValueError, "model target schema mismatch"):
                bundles.load_bundle(root, expected_fingerprints=fingerprints())

    def test_geometry_level_quantile_is_not_reweighted_by_repeated_operating_points(self):
        x = motor_inputs([50] * 40)
        prediction = known_motor(x)
        truth = prediction.copy()
        truth["output_torque_last_avg_nm"] += np.arange(40) * .01
        scales = {target: 1.0 for target in TARGETS}
        first = bundles.calibrate_joint(x, truth, prediction, list(range(40)), scales)
        repeated = 100
        second = bundles.calibrate_joint(pd.concat([x, pd.concat([x.iloc[[-1]]] * repeated)], ignore_index=True),
            pd.concat([truth, pd.concat([truth.iloc[[-1]]] * repeated)], ignore_index=True),
            pd.concat([prediction, pd.concat([prediction.iloc[[-1]]] * repeated)], ignore_index=True),
            list(range(40)) + [39] * repeated, scales)
        self.assertEqual(first["groups"], 40)
        self.assertEqual(first["quantile"], second["quantile"])
        self.assertEqual(first["rank"], 39)

    def test_insufficient_calibration_geometries_has_no_finite_95percent_bound(self):
        x = motor_inputs([50] * 18); y = known_motor(x)
        with self.assertRaisesRegex(ValueError, "too few independent"):
            bundles.calibrate_joint(x, y, y, range(18), {target: 1 for target in TARGETS})

    def test_unsafe_domain_and_inconsistent_geometry_or_resistance_are_rejected(self):
        for column, value in [("input_i_peak_a", 0), ("input_beta_dq_deg", 89), ("input_base_rpm", 2400),
                              ("input_stator_outer_radius", 151), ("input_phase_resistance_ohm", 1),
                              ("input_rotor_radius", 999), ("input_i_peak_a", np.nan)]:
            x = self.x.iloc[[1]].copy(); x[column] = value
            with self.subTest(column=column, value=value), self.assertRaises(ValueError):
                self.bundle().predict(x)
        outside = motor_inputs([45], [80], [20], [1200])
        with self.assertRaisesRegex(ValueError, "supported training domain"):
            # Resistance/length are valid, but beta=5 is outside trained10..40.
            outside["input_beta_dq_deg"] = 5
            self.bundle().predict(outside)

    def test_rejected_artifact_cannot_be_used_without_explicit_diagnostic_override(self):
        bundle = self.bundle()
        bundle.metadata["test_metrics"][TARGETS[0]]["r2"] = .8
        bundle.metadata["decision"] = "rejected"
        with self.assertRaisesRegex(ValueError, "rejected"):
            bundle.predict(self.x)
        self.assertEqual(len(bundle.predict(self.x, allow_rejected=True, with_bounds=False)), len(self.x))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifact"; bundles.save_bundle(bundle, root)
            with self.assertRaisesRegex(ValueError, "rejected"):
                bundles.load_bundle(root, expected_fingerprints=fingerprints())
            loaded = bundles.load_bundle(root, require_approved=False)
            self.assertFalse(loaded.approved)

    def test_artifact_round_trip_integrity_source_version_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifact"; bundles.save_bundle(self.bundle(), root)
            loaded = bundles.load_bundle(root, expected_fingerprints=fingerprints())
            np.testing.assert_allclose(loaded.predict(self.x, with_bounds=False), known_motor(self.x))
            with self.assertRaises(FileExistsError):
                bundles.save_bundle(self.bundle(), root)
            with mock.patch.object(bundles, "source_contract", return_value={"wrong": "source"}):
                with self.assertRaisesRegex(ValueError, "source contract changed"):
                    bundles.load_bundle(root, expected_fingerprints=fingerprints())
            with mock.patch.object(bundles, "_IMPORTED_PREDICTION_SOURCES", {"wrong": "loaded-source"}):
                with self.assertRaisesRegex(ValueError, "source contract changed"):
                    bundles.load_bundle(root, expected_fingerprints=fingerprints())
            with mock.patch.object(bundles, "dependency_versions", return_value={"wrong": "version"}):
                with self.assertRaisesRegex(ValueError, "dependency versions"):
                    bundles.load_bundle(root, expected_fingerprints=fingerprints())
            with (root / "models.joblib").open("ab") as stream: stream.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                bundles.load_bundle(root, expected_fingerprints=fingerprints())

    def test_file_mutation_after_verification_cannot_change_deserialized_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifact"; bundles.save_bundle(self.bundle(), root)
            original_load = bundles.joblib.load
            def change_file_then_load(checked_bytes):
                (root / "models.joblib").write_bytes(b"changed after checking")
                return original_load(checked_bytes)
            with mock.patch.object(bundles.joblib, "load", side_effect=change_file_then_load):
                loaded = bundles.load_bundle(root, expected_fingerprints=fingerprints())
            np.testing.assert_allclose(loaded.predict(self.x, with_bounds=False), known_motor(self.x))

    def test_partition_or_frozen_selection_changes_reject_promotion(self):
        bundle = self.bundle()
        bundle.metadata["test_groups"][0] = bundle.metadata["fit_groups"][0]
        self.assertFalse(bundle.approved)
        bundle = self.bundle()
        bundle.metadata["selection"]["test_used_for_selection"] = True
        self.assertFalse(bundle.approved)

    def test_wrong_producer_context_or_input_schema_cannot_enter_optimizer(self):
        wrong = {**fingerprints(), "input_producer_fingerprint": "producer_v3:sha256:" + "b" * 64}
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            self.bundle().predict(self.x, fingerprints=wrong)
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            self.bundle().predict(self.x.drop(columns="input_i_peak_a"))

    def test_selection_does_not_access_test_data(self):
        class Inner:
            x_train = PhysicalBundleTests.x
            y_train = known_motor(x_train)
            x_val = x_train.copy()
            y_val = known_motor(x_val)
            train_group_ids = ("fit-a", "fit-b")
            val_group_ids = ("select-a", "select-b")

            @property
            def y_test(self):
                raise AssertionError("test targets accessed during model selection")
        selected = training.select_families(Inner(), jobs=1, candidates=("physical_extra",))
        self.assertFalse(selected["test_used_for_selection"])
        self.assertEqual(set(selected["selected"]), set(TARGETS))

    def test_actual_winding_constants_are_uniform_declared_and_not_silent_features(self):
        raw = synthetic_rows()
        wrong = raw.copy(); wrong.loc[0, "input_series_turns_per_phase"] = 96
        with self.assertRaisesRegex(ValueError, "mixed or invalid.*winding"):
            training.validate_new_data(wrong)
        wrong = raw.copy(); wrong["input_series_turns_per_phase"] = 96
        with self.assertRaisesRegex(ValueError, "differs from declared"):
            bundles.domain_from_training(self.x, spec(), case_frame=wrong)
        query = self.x.assign(input_series_turns_per_phase=96)
        with self.assertRaisesRegex(ValueError, "winding context"):
            self.bundle().predict(query)
        self.assertEqual(self.domain["winding_case_constants"]["input_series_turns_per_phase"], 48)
        declared = deepcopy(spec()); declared["winding"].update(parallel_branches=2, coils_per_phase=8)
        with self.assertRaisesRegex(ValueError, "one parallel branch"):
            bundles.domain_from_training(self.x, declared, case_frame=raw)

    def test_historical_data_and_relabelled_physical_geometry_are_rejected(self):
        raw = synthetic_rows()
        training.validate_new_data(raw)
        old = raw.drop(columns="input_metric_contract_version")
        with self.assertRaisesRegex(ValueError, "motor_metrics_v3"):
            training.validate_new_data(old)
        relabelled = pd.concat([raw, raw.iloc[[0]].copy()], ignore_index=True)
        relabelled.loc[len(relabelled) - 1, ["case_id", "geometry_group_id", "design_hash"]] = ["other", "other-group", "other-hash"]
        with self.assertRaisesRegex(ValueError, "same physical geometry"):
            training.validate_new_data(relabelled)

    def test_historical_width_repair_cannot_hide_duplicate_train_test_geometry(self):
        import train_ipmsm_lightgbm as legacy
        raw = synthetic_rows()
        extra = raw[raw.geometry_group_id.eq("geometry-0")].copy()
        extra["case_id"] = ["review-leak-0", "review-leak-1"]
        extra["geometry_group_id"], extra["design_hash"], extra["doe_split"] = "review-test-group", "review-test-hash", "test"
        extra["input_stator_teeth_width_ratio"] += .01
        raw = pd.concat([raw, extra], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "inconsistent raw v3 derived geometry"):
            training.validate_new_data(raw)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); data = root / "input.csv"; raw.to_csv(data, index=False)
            prepared = legacy.prepare_training_data(legacy.require_training_dependencies(), [data], False, False, 1.5, v2=True)
            self.assertGreater(prepared.repaired_derived_inputs["input_stator_teeth_width_ratio"], 0)
            with self.assertRaises(ValueError):
                training.fit_prepared(prepared, spec(), root / "forbidden-run", jobs=1, candidates=("physical_extra",))
            self.assertFalse((root / "forbidden-run").exists())

    def test_source_drift_during_fit_refuses_artifact_publication(self):
        import train_ipmsm_lightgbm as legacy
        real_contract = bundles.source_contract
        real_selection = training.select_families
        state = {"changed": False}
        def current_contract():
            result = real_contract()
            if state["changed"]:
                result["surrogate.py"] = "0" * 64
            return result
        def choose_then_change(*args, **kwargs):
            result = real_selection(*args, **kwargs)
            state["changed"] = True
            return result
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); data = root / "input.csv"; synthetic_rows().to_csv(data, index=False)
            prepared = legacy.prepare_training_data(legacy.require_training_dependencies(), [data], False, False, 1.5, v2=True)
            with mock.patch.object(bundles, "source_contract", side_effect=current_contract), \
                 mock.patch.object(training, "select_families", side_effect=choose_then_change):
                with self.assertRaisesRegex(RuntimeError, "changed during fitting"):
                    training.fit_prepared(prepared, spec(), root / "run", jobs=1, candidates=("physical_extra",))
            self.assertTrue((root / "run/selection.json").is_file())
            self.assertFalse((root / "run/artifact/COMPLETE").exists())

    def test_end_to_end_training_uses_separate_groups_and_freezes_selection(self):
        import train_ipmsm_lightgbm as legacy
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); data = root / "input.csv"
            synthetic_rows().to_csv(data, index=False)
            prepared = legacy.prepare_training_data(legacy.require_training_dependencies(), [data], False, False, 1.5, v2=True)
            result = training.fit_prepared(prepared, spec(), root / "run", jobs=1, candidates=("physical_extra",))
            choice = json.loads((root / "run/selection.json").read_text())
            self.assertFalse(set(choice["outer_train_groups"]) & set(choice["calibration_groups"]))
            self.assertFalse(set(choice["outer_train_groups"]) & set(choice["test_groups"]))
            self.assertFalse(set(choice["calibration_groups"]) & set(choice["test_groups"]))
            self.assertEqual(result.metadata["calibration"]["groups"], 20)
            self.assertEqual(result.metadata["test_evaluation_scope"], "all_preassigned_test_rows_without_input_or_output_filtering")
            self.assertTrue(result.approved, result.metadata["promotion_issues"])
            loaded = bundles.load_bundle(root / "run/artifact", expected_fingerprints=result.metadata["fingerprints"])
            self.assertTrue(loaded.approved)
            self.assertEqual(set(loaded.models), set(TARGETS))
            self.assertEqual(set(result.metadata["test_metrics"]), set(bundles.ALL_TARGETS))
            self.assertEqual(len(loaded.models), 8)
            self.assertEqual(len(result.metadata["test_metrics"]), 10)


if __name__ == "__main__":
    unittest.main()
