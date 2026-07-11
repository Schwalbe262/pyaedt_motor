from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import train_ipmsm_lightgbm as trainer


class FakeColumns:
    def __init__(self, values: dict[str, list[object]]) -> None:
        self.values = values
        self.columns = tuple(values)

    def __getitem__(self, column: str) -> list[object]:
        return self.values[column]

    def __setitem__(self, column: str, values: list[object]) -> None:
        self.values[column] = values
        self.columns = tuple(self.values)


class TrainIpmsmLightgbmTests(unittest.TestCase):
    def test_stable_target_seed_is_deterministic(self) -> None:
        target = "output_torque_last_avg_nm"
        expected = trainer.SEED + int(hashlib.sha256(target.encode("utf-8")).hexdigest()[:12], 16) % 100000

        self.assertEqual(trainer.stable_target_seed(target), expected)
        self.assertEqual(trainer.stable_target_seed(target), trainer.stable_target_seed(target))
        self.assertNotEqual(trainer.stable_target_seed(target), trainer.stable_target_seed("output_lq_last_avg_h"))

    def test_resolve_output_columns_uses_alias(self) -> None:
        available = {
            "output_torque_last_avg_nm",
            "output_torque_last_max_nm",
            "output_total_loss_last_avg_w",
            "output_solidloss_last_avg_w",
            "output_coreloss_last_avg_w",
            "output_ld_last_avg_h",
            "output_lq_last_avg_h",
            "output_efficiency_last_pct",
        }

        actual, name_map = trainer.resolve_output_columns(available)

        self.assertIn("output_efficiency_last_pct", actual)
        self.assertEqual(name_map["output_efficiency_last_pc"], "output_efficiency_last_pct")

    def test_resolve_output_columns_reports_missing_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing output column"):
            trainer.resolve_output_columns({"output_torque_last_avg_nm"}, requested_columns=("missing_target",))

    def test_regression_metrics_matches_expected_values(self) -> None:
        metrics = trainer.regression_metrics([1.0, 2.0, 3.0], [1.0, 2.0, 4.0])

        self.assertAlmostEqual(metrics["MAE"], 1.0 / 3.0)
        self.assertAlmostEqual(metrics["RMSE"], math.sqrt(1.0 / 3.0))
        self.assertAlmostEqual(metrics["R2"], 0.5)
        self.assertAlmostEqual(metrics["MAPE_pct"], (1.0 / 3.0) / 3.0 * 100.0)

    def test_repaired_derived_input_values_match_design_expression(self) -> None:
        repaired = trainer.repaired_derived_input_values(
            {
                "input_stator_inner_radius": "79.515",
                "input_rotator_gap": "1.54",
                "input_shaft_ratio": "0.516",
                "input_rotor_radius": "131.45",
                "input_shaft_radius": "67.8282",
            }
        )

        self.assertAlmostEqual(repaired["input_rotor_radius"], 77.975)
        self.assertAlmostEqual(repaired["input_shaft_radius"], 77.975 * 0.516)

    def test_repaired_derived_input_values_recovers_teeth_width_ratio(self) -> None:
        repaired = trainer.repaired_derived_input_values(
            {
                "input_slot_num": "12",
                "input_stator_outer_radius": "155.0",
                "input_stator_back_yoke_thick": "22.01",
                "input_stator_teeth_length": "45.293325",
                "input_stator_teeth_width": "33.931477685988845",
            }
        )

        self.assertAlmostEqual(repaired["input_stator_teeth_width_ratio"], 0.722, places=3)

    def test_repaired_derived_input_values_skip_incomplete_rows(self) -> None:
        self.assertEqual(trainer.repaired_derived_input_values({"input_stator_inner_radius": "79.515"}), {})

    def test_select_training_input_columns_includes_recovered_and_available_optional_inputs(self) -> None:
        columns = trainer.select_training_input_columns(
            {
                *trainer.RAW_INPUT_COLUMNS,
                "input_stator_teeth_width_ratio",
                "input_slot_opening_ratio",
                "input_steps_per_period",
            }
        )

        self.assertIn("input_stator_teeth_width_ratio", columns)
        self.assertIn("input_slot_opening_ratio", columns)
        self.assertIn("input_steps_per_period", columns)
        self.assertNotIn("input_magnet_space_height_ratio", columns)

    def test_select_training_input_columns_excludes_sparse_optional_inputs(self) -> None:
        columns = trainer.select_training_input_columns(
            {
                *trainer.RAW_INPUT_COLUMNS,
                "input_slot_opening_ratio",
            },
            [
                {"input_slot_opening_ratio": "0.09"},
                {"input_slot_opening_ratio": ""},
            ],
        )

        self.assertNotIn("input_slot_opening_ratio", columns)

    def test_v2_requires_all_conditional_inputs(self) -> None:
        available = {*trainer.INPUT_COLUMNS, *trainer.V2_REQUIRED_CONDITIONAL_INPUT_COLUMNS}

        columns = trainer.select_v2_training_input_columns(available)

        self.assertIn("input_stack_length_mm", columns)
        self.assertIn("input_base_rpm", columns)
        self.assertIn("input_i_peak_a", columns)
        self.assertIn("input_beta_dq_deg", columns)
        self.assertIn("input_phase_resistance_ohm", columns)
        with self.assertRaisesRegex(ValueError, "input_beta_dq_deg"):
            trainer.select_v2_training_input_columns(available - {"input_beta_dq_deg"})

    def test_v2_fingerprints_are_single_valued_and_strict(self) -> None:
        values = {
            column: ["value", "value"]
            for column in trainer.V2_FINGERPRINT_COLUMNS
        }
        values["input_dataset_schema_version"] = [trainer.V2_DATASET_SCHEMA_VERSION] * 2
        values["input_beta_convention"] = [trainer.V2_BETA_CONVENTION] * 2
        frame = FakeColumns(values)

        fingerprints = trainer.validate_v2_fingerprints(frame, {"input_quality_profile": "value"})

        self.assertEqual(fingerprints["input_dataset_schema_version"], "ipmsm_v2")
        values["input_quality_profile"][1] = "other"
        with self.assertRaisesRegex(ValueError, "mixed v2 fingerprint"):
            trainer.validate_v2_fingerprints(frame)

    def test_v2_legacy_beta_alias_is_guarded_by_convention(self) -> None:
        frame = FakeColumns({"input_beta_deg": [10.0, 20.0]})

        column = trainer.ensure_v2_canonical_beta_column(
            frame,
            {"input_beta_convention": trainer.V2_BETA_CONVENTION},
        )

        self.assertEqual(column, "input_beta_dq_deg")
        self.assertEqual(frame["input_beta_dq_deg"], [10.0, 20.0])
        with self.assertRaisesRegex(ValueError, "only accepted"):
            trainer.ensure_v2_canonical_beta_column(
                FakeColumns({"input_beta_deg": [30.0]}),
                {"input_beta_convention": "legacy_phase_offset"},
            )

    def test_v2_geometry_identity_mapping_must_be_one_to_one(self) -> None:
        frame = FakeColumns(
            {
                "geometry_group_id": ["g1", "g1", "g2"],
                "design_hash": ["h1", "h1", "h2"],
            }
        )
        self.assertEqual(trainer.resolve_v2_geometry_group_column(frame), "geometry_group_id")

        frame.values["design_hash"][1] = "other"
        with self.assertRaisesRegex(ValueError, "one-to-one"):
            trainer.resolve_v2_geometry_group_column(frame)

    def test_deterministic_group_partitions_prevent_group_leakage(self) -> None:
        groups = [f"geometry-{index}" for index in range(10) for _ in range(3)]

        first = trainer.deterministic_group_partitions(groups, test_size=0.2, val_size=0.2, seed=42)
        second = trainer.deterministic_group_partitions(reversed(groups), test_size=0.2, val_size=0.2, seed=42)

        self.assertEqual(first, second)
        self.assertEqual(set(first.values()), {"train", "calibration", "test"})
        self.assertEqual(len([value for value in first.values() if value == "test"]), 2)
        self.assertEqual(len([value for value in first.values() if value == "calibration"]), 2)

    def test_preassigned_doe_split_is_stable_and_rejects_group_leakage(self) -> None:
        assignments = trainer.validated_preassigned_group_partitions(
            ["g-train", "g-train", "g-cal", "g-test"],
            ["train", "train", "calibration", "test"],
        )
        self.assertEqual(
            assignments,
            {"g-train": "train", "g-cal": "calibration", "g-test": "test"},
        )
        with self.assertRaisesRegex(ValueError, "crosses doe_split"):
            trainer.validated_preassigned_group_partitions(
                ["g1", "g1", "g2", "g3"],
                ["train", "test", "calibration", "test"],
            )

    def test_v2_audit_case_plan_selects_only_its_untouched_test_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stage2.csv"
            rows = [
                {"case_id": "s2-train", "geometry_group_id": "s2-g-train", "doe_split": "train"},
                {"case_id": "s2-test-a", "geometry_group_id": "s2-g-test", "doe_split": "test"},
                {"case_id": "s2-test-b", "geometry_group_id": "s2-g-test", "doe_split": "test"},
            ]
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            plan_rows, contract = trainer.load_v2_audit_case_plan(
                path,
                geometry_column="geometry_group_id",
            )
            test_ids, test_groups = trainer.validate_v2_audit_records(
                plan_rows,
                [
                    {
                        "case_id": "s1-test",
                        "geometry_group_id": "s1-g-test",
                        "doe_split": "test",
                    },
                    *rows,
                ],
                geometry_column="geometry_group_id",
            )

        self.assertEqual(test_ids, ("s2-test-a", "s2-test-b"))
        self.assertEqual(test_groups, ("s2-g-test",))
        self.assertEqual(contract["scope"], trainer.V2_TEST_EVALUATION_SCOPE_AUDIT_CASE_PLAN)
        self.assertEqual(contract["rows"], 2)
        self.assertEqual(contract["groups"], 1)
        self.assertEqual(contract["case_plan_rows"], 3)

    def test_v2_audit_case_plan_rejects_geometry_present_outside_plan(self) -> None:
        plan_rows = [
            {"case_id": "s2-test", "geometry_group_id": "shared", "doe_split": "test"}
        ]
        with self.assertRaisesRegex(ValueError, "outside the audit case plan"):
            trainer.validate_v2_audit_records(
                plan_rows,
                [
                    *plan_rows,
                    {"case_id": "s1-test", "geometry_group_id": "shared", "doe_split": "test"},
                ],
                geometry_column="geometry_group_id",
            )

    def test_model_selection_partitions_are_inside_outer_train_groups(self) -> None:
        groups = [f"geometry-{index}" for index in range(10) for _ in range(2)]

        roles = trainer.deterministic_model_selection_partitions(groups, seed=42)

        self.assertEqual(set(roles), {f"geometry-{index}" for index in range(10)})
        self.assertEqual(set(roles.values()), {"fit", "model_selection"})
        self.assertEqual(len([role for role in roles.values() if role == "model_selection"]), 2)

    def test_v2_training_never_fits_on_outer_calibration(self) -> None:
        class FakeRegressor:
            def __init__(self, owner: object, params: dict[str, object]) -> None:
                self.owner = owner
                self.params = params
                self.best_iteration_ = None
                self.fit_x = None
                self.fit_kwargs: dict[str, object] = {}
                owner.instances.append(self)

            def fit(self, x: object, y: list[float], **kwargs: object) -> None:
                self.fit_x = x
                self.fit_kwargs = kwargs
                self.value = sum(y) / len(y)
                if "eval_set" in kwargs:
                    self.best_iteration_ = 3

            def predict(self, x: list[object], **kwargs: object) -> list[float]:
                return [self.value] * len(x)

        class FakeLightGbm:
            def __init__(self) -> None:
                self.instances: list[FakeRegressor] = []

            def LGBMRegressor(self, **params: object) -> FakeRegressor:
                return FakeRegressor(self, params)

            @staticmethod
            def early_stopping(*args: object, **kwargs: object) -> object:
                return object()

            @staticmethod
            def log_evaluation(*args: object, **kwargs: object) -> object:
                return object()

        target = "target"
        outer = trainer.SplitData(
            x_train=["outer-train-1", "outer-train-2"],
            x_val=["outer-calibration"],
            x_test=["outer-test"],
            y_train={target: [1.0, 3.0]},
            y_val={target: [2.0]},
            y_test={target: [4.0]},
        )
        inner = trainer.SplitData(
            x_train=["inner-fit"],
            x_val=["inner-model-selection"],
            x_test=outer.x_test,
            y_train={target: [1.0]},
            y_val={target: [3.0]},
            y_test=outer.y_test,
        )
        fake_lgb = FakeLightGbm()
        deps = trainer.TrainingDependencies(np=None, pd=None, train_test_split=lambda *args: None, lgb=fake_lgb)

        _, params, metrics, _ = trainer.train_one_target_v2(
            deps,
            outer,
            inner,
            target,
            False,
            0,
            42,
            1,
            5,
        )

        self.assertEqual(len(fake_lgb.instances), 2)
        self.assertIs(fake_lgb.instances[0].fit_x, inner.x_train)
        self.assertIs(fake_lgb.instances[0].fit_kwargs["eval_set"][0][0], inner.x_val)
        self.assertIs(fake_lgb.instances[1].fit_x, outer.x_train)
        self.assertEqual(fake_lgb.instances[1].fit_kwargs, {})
        self.assertNotIn(outer.x_val, [model.fit_x for model in fake_lgb.instances])
        self.assertEqual(params["n_estimators"], 3)
        self.assertEqual([row["split"] for row in metrics], ["train", "calibration", "test"])

    def test_v2_derived_outputs_use_copper_loss_and_mechanical_power(self) -> None:
        derived = trainer.derive_v2_outputs(
            torque_avg_nm=10.0,
            core_loss_w=20.0,
            solid_loss_w=5.0,
            i_peak_a=10.0,
            phase_resistance_ohm=0.1,
            rpm=600.0,
        )

        expected_loss = 20.0 + 5.0 + 1.5 * 0.1 * 10.0**2
        expected_power = 10.0 * 2.0 * math.pi * 600.0 / 60.0
        self.assertAlmostEqual(derived["output_total_loss_last_avg_w"], expected_loss)
        self.assertAlmostEqual(
            derived["output_efficiency_last_pct"],
            expected_power / (expected_power + expected_loss) * 100.0,
        )

    def test_v2_auxiliary_voltage_target_is_separate_from_primary_gate(self) -> None:
        self.assertEqual(
            trainer.V2_AUXILIARY_OUTPUT_COLUMNS,
            ("output_phase_voltage_last_peak_abs_v",),
        )
        self.assertEqual(
            len((*trainer.V2_PRIMITIVE_OUTPUT_COLUMNS, *trainer.V2_DERIVED_OUTPUT_COLUMNS)),
            8,
        )
        self.assertNotIn(
            trainer.V2_AUXILIARY_OUTPUT_COLUMNS[0],
            trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS,
        )
        with self.assertRaisesRegex(ValueError, "output_phase_voltage_last_peak_abs_v"):
            trainer.resolve_output_columns(set(), requested_columns=trainer.V2_AUXILIARY_OUTPUT_COLUMNS)

    def test_primary_test_r2_map_excludes_calibration_and_auxiliary_rows(self) -> None:
        rows = [
            {"target": "primary", "split": "calibration", "R2": 0.1},
            {"target": "primary", "split": "test", "R2": 0.96},
        ]

        self.assertEqual(trainer.primary_test_r2_by_target(rows), {"primary": 0.96})

    def test_single_target_voltage_r2_gate_is_complete_finite_and_thresholded(self) -> None:
        target = trainer.V2_AUXILIARY_OUTPUT_COLUMNS[0]
        rows = [
            {"target": target, "split": "calibration", "R2": 0.1},
            {"target": target, "split": "test", "R2": 0.96},
        ]

        self.assertEqual(
            trainer.single_target_test_r2_gate(rows, target, 0.95),
            (0.96, True, True),
        )
        self.assertEqual(
            trainer.single_target_test_r2_gate(rows, target, 0.97),
            (0.96, True, False),
        )
        self.assertEqual(
            trainer.single_target_test_r2_gate([], target, 0.95),
            (None, False, False),
        )
        self.assertEqual(
            trainer.single_target_test_r2_gate(
                [{"target": target, "split": "test", "R2": math.nan}],
                target,
                0.95,
            ),
            (None, True, False),
        )

    def test_fail_on_threshold_combines_primary_and_voltage_gates_for_v2(self) -> None:
        self.assertFalse(
            trainer.threshold_gate_failed(
                v2=True,
                primary_gate_passed=True,
                voltage_gate_passed=True,
                metric_failures=0,
                )
            )
        self.assertTrue(
            trainer.threshold_gate_failed(
                v2=True,
                primary_gate_passed=True,
                voltage_gate_passed=True,
                metric_failures=1,
            )
        )
        for primary_passed, voltage_passed in ((False, True), (True, False), (False, False)):
            with self.subTest(primary_passed=primary_passed, voltage_passed=voltage_passed):
                self.assertTrue(
                    trainer.threshold_gate_failed(
                        v2=True,
                        primary_gate_passed=primary_passed,
                        voltage_gate_passed=voltage_passed,
                        metric_failures=0,
                    )
                )
        self.assertTrue(
            trainer.threshold_gate_failed(
                v2=False,
                primary_gate_passed=False,
                voltage_gate_passed=False,
                metric_failures=1,
            )
        )

    def test_predict_model_averages_v2_ensemble_members(self) -> None:
        class ConstantModel:
            def __init__(self, value: float) -> None:
                self.value = value

            def predict(self, rows: list[object]) -> list[float]:
                return [self.value] * len(rows)

        predictions = trainer.predict_model(
            (ConstantModel(1.0), ConstantModel(3.0), ConstantModel(5.0)),
            ["a", "b"],
        )

        self.assertEqual(predictions, [3.0, 3.0])

    def test_split_conformal_uses_finite_sample_corrected_absolute_quantile(self) -> None:
        result = trainer.split_conformal_absolute_residual(
            [0.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 2.0, 3.0],
            coverage=0.75,
        )

        self.assertEqual(result["calibration_rows"], 4)
        self.assertEqual(result["rank"], 4)
        self.assertEqual(result["quantile_abs"], 3.0)

    def test_split_conformal_ignores_nonfinite_pairs_deterministically(self) -> None:
        result = trainer.split_conformal_absolute_residual(
            [1.0, math.nan, 5.0],
            [2.0, 100.0, 8.0],
            coverage=0.5,
        )

        self.assertEqual(result["calibration_rows"], 2)
        self.assertEqual(result["rank"], 2)
        self.assertEqual(result["quantile_abs"], 3.0)

    def test_feature_min_max_bounds_are_per_input(self) -> None:
        bounds = trainer.feature_min_max_bounds(
            FakeColumns({"a": [3.0, -1.0, 2.0], "b": [10.0, 20.0, 15.0]}),
            ("a", "b"),
        )

        self.assertEqual(bounds, {"a": {"min": -1.0, "max": 3.0}, "b": {"min": 10.0, "max": 20.0}})

    def test_v2_is_opt_in_and_expected_fingerprints_require_v2(self) -> None:
        legacy = trainer.build_parser().parse_args([])
        self.assertFalse(legacy.v2)
        self.assertTrue(legacy.remove_output_outliers)
        self.assertIsNone(legacy.v2_audit_case_plan)

        args = trainer.build_parser().parse_args(
            ["--expected-fingerprint", "input_quality_profile=reference_ultra"]
        )
        with self.assertRaisesRegex(ValueError, "requires --v2"):
            trainer.validate_training_options(args)

        args = trainer.build_parser().parse_args(["--v2-audit-case-plan", "stage2.csv"])
        with self.assertRaisesRegex(ValueError, "requires --v2"):
            trainer.validate_training_options(args)

    def test_validate_training_options_rejects_invalid_conformal_coverage(self) -> None:
        args = trainer.build_parser().parse_args(["--conformal-coverage", "1"])

        with self.assertRaisesRegex(ValueError, "--conformal-coverage"):
            trainer.validate_training_options(args)

    def test_validate_training_options_rejects_nonfinite_r2_threshold(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                args = trainer.build_parser().parse_args([f"--r2-threshold={value}"])
                with self.assertRaisesRegex(ValueError, "--r2-threshold must be finite"):
                    trainer.validate_training_options(args)

    def test_sample_params_uses_each_search_space_key(self) -> None:
        rng = __import__("random").Random(7)
        search_space = {"a": (1, 2), "b": ("x", "y")}

        params = trainer.sample_params(rng, search_space)

        self.assertEqual(set(params), {"a", "b"})
        self.assertIn(params["a"], search_space["a"])
        self.assertIn(params["b"], search_space["b"])

    def test_training_quality_report_exposes_filter_failures(self) -> None:
        report = trainer.TrainingQualityReport(
            raw_rows=10,
            rows_after_dedup=9,
            dropped_duplicate_case_id_rows=1,
            status_rejected_rows=2,
            nonfinite_input_rows=1,
            nonfinite_output_rows=1,
            physical_sanity_rejected_rows=1,
            valid_rows_before_outliers=6,
            removed_output_outliers=2,
            valid_rows=4,
        )

        self.assertEqual(report.invalid_training_rows, 3)
        self.assertEqual(
            report.failure_reasons(max_invalid_training_rows=0, max_removed_output_outlier_rows=1),
            ["invalid_training_rows 3 > 0", "removed_output_outlier_rows 2 > 1"],
        )
        self.assertEqual(report.as_metadata()["valid_rows"], 4)
        self.assertEqual(report.as_metadata()["physical_sanity_rejected_rows"], 1)

    def test_physical_sanity_violations_detect_out_of_range_efficiency(self) -> None:
        violations = trainer.physical_sanity_violations(
            {
                "output_efficiency_last_pct": "120",
                "output_efficiency_all_pct": "nan",
                "output_torque_last_avg_nm": "10",
            }
        )

        self.assertEqual(violations, ["output_efficiency_last_pct"])

    def test_validate_training_options_rejects_bad_split_sum(self) -> None:
        args = trainer.build_parser().parse_args(["--test-size", "0.8", "--val-size", "0.3"])

        with self.assertRaisesRegex(ValueError, "must be less than 1"):
            trainer.validate_training_options(args)

    def test_validate_training_options_rejects_negative_quality_limits(self) -> None:
        args = trainer.build_parser().parse_args(["--max-invalid-training-rows", "-1"])
        with self.assertRaisesRegex(ValueError, "--max-invalid-training-rows"):
            trainer.validate_training_options(args)

        args = trainer.build_parser().parse_args(["--max-removed-output-outlier-rows", "-1"])
        with self.assertRaisesRegex(ValueError, "--max-removed-output-outlier-rows"):
            trainer.validate_training_options(args)

    def test_validate_training_options_accepts_defaults(self) -> None:
        args = trainer.build_parser().parse_args([])

        trainer.validate_training_options(args)

    def test_inspect_training_dependencies_reports_versions_and_missing_modules(self) -> None:
        modules = {
            "numpy": SimpleNamespace(__version__="2.4.3"),
            "sklearn.model_selection": SimpleNamespace(),
            "lightgbm": SimpleNamespace(__version__="4.6.0"),
        }

        def fake_import(name: str) -> object:
            if name == "pandas":
                raise ImportError("No module named pandas")
            return modules[name]

        report = trainer.inspect_training_dependencies(fake_import)
        by_module = {row["module"]: row for row in report}

        self.assertEqual(by_module["numpy"]["status"], "ok")
        self.assertEqual(by_module["numpy"]["version"], "2.4.3")
        self.assertEqual(by_module["pandas"]["status"], "missing")
        self.assertEqual(trainer.missing_training_dependency_modules(report), ["pandas"])

    def test_write_training_dependency_report_marks_readiness(self) -> None:
        report = [
            {"module": "numpy", "status": "ok", "version": "2.4.3", "error": ""},
            {"module": "pandas", "status": "missing", "version": "", "error": "missing"},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deps.json"
            trainer.write_training_dependency_report(path, report)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(payload["ready"])
        self.assertEqual(payload["missing_modules"], ["pandas"])

    def test_require_training_dependencies_reports_missing_modules(self) -> None:
        def fake_import(name: str) -> object:
            if name == "numpy":
                return SimpleNamespace()
            raise ImportError(name)

        with self.assertRaises(trainer.MissingTrainingDependencyError) as caught:
            trainer.require_training_dependencies(fake_import)

        message = str(caught.exception)
        self.assertIn("pandas", message)
        self.assertIn("sklearn.model_selection", message)
        self.assertIn("lightgbm", message)

    def test_require_training_dependencies_returns_loaded_modules(self) -> None:
        modules = {
            "numpy": SimpleNamespace(),
            "pandas": SimpleNamespace(),
            "sklearn.model_selection": SimpleNamespace(train_test_split=object()),
            "lightgbm": SimpleNamespace(),
        }

        deps = trainer.require_training_dependencies(lambda name: modules[name])

        self.assertIs(deps.np, modules["numpy"])
        self.assertIs(deps.pd, modules["pandas"])
        self.assertIs(deps.train_test_split, modules["sklearn.model_selection"].train_test_split)
        self.assertIs(deps.lgb, modules["lightgbm"])


if __name__ == "__main__":
    unittest.main()
