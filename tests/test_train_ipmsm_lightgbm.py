from __future__ import annotations

import hashlib
import math
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

    def test_sample_params_uses_each_search_space_key(self) -> None:
        rng = __import__("random").Random(7)
        search_space = {"a": (1, 2), "b": ("x", "y")}

        params = trainer.sample_params(rng, search_space)

        self.assertEqual(set(params), {"a", "b"})
        self.assertIn(params["a"], search_space["a"])
        self.assertIn(params["b"], search_space["b"])

    def test_validate_training_options_rejects_bad_split_sum(self) -> None:
        args = trainer.build_parser().parse_args(["--test-size", "0.8", "--val-size", "0.3"])

        with self.assertRaisesRegex(ValueError, "must be less than 1"):
            trainer.validate_training_options(args)

    def test_validate_training_options_accepts_defaults(self) -> None:
        args = trainer.build_parser().parse_args([])

        trainer.validate_training_options(args)

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
