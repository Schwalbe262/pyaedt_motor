from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import train_ipmsm_lightgbm as trainer


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
            }
        )

        self.assertIn("input_stator_teeth_width_ratio", columns)
        self.assertIn("input_slot_opening_ratio", columns)
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
