from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import tempfile
import unittest

import ipmsm_optimization as opt


def valid_spec_mapping() -> dict:
    return {
        "schema_version": 1,
        "operating_points": [
            {
                "name": "low_speed",
                "speed_rpm": 1000.0,
                "target_torque_nm": 50.0,
                "duty_weight": 0.4,
            },
            {
                "name": "rated",
                "speed_rpm": 3000.0,
                "target_power_w": 10000.0,
                "duty_weight": 0.6,
            },
        ],
        "stack_length_bounds_mm": [40.0, 70.0],
        "inverter": {
            "vdc_v": 400.0,
            "phase_peak_current_limit_a": 200.0,
            "voltage_utilization": 0.95,
        },
        "winding": {
            "series_turns_per_phase": 48,
            "turns_per_coil_side": 12,
            "coils_per_phase": 4,
            "parallel_branches": 1,
            "strand_area_mm2": 5.0,
            "strands_per_turn": 1,
            "fill_factor": 0.6,
            "end_turn_factor": 1.0,
            "overhang_mm": 5.0,
        },
        "constraints": {"current_density_limit_a_per_mm2": 30.0},
        "beta_calibration": {
            "electrical_zero_deg": 12.5,
            "calibration_id": "fixture-calibration",
            "convention": "dq_current_advance_v2",
        },
        "control": {
            "beta_bounds_deg": [0.0, 80.0],
            "current_grid_points": 9,
            "coarse_beta_step_deg": 20.0,
            "beta_refinement_steps_deg": [2.0],
            "current_refinement_denominators": [32],
        },
        "nsga2": {"population_size": 8, "max_generations": 2, "seeds": [42]},
    }


def make_spec() -> opt.OptimizationSpec:
    return opt.optimization_spec_from_mapping(valid_spec_mapping())


def midpoint_design(spec: opt.OptimizationSpec) -> dict[str, float]:
    return {bound.name: 0.5 * (bound.lower + bound.upper) for bound in spec.design_space}


def analytic_predictor(features):
    current = float(features["current_peak_a"])
    beta = math.radians(float(features["beta_deg"]))
    torque = 0.55 * current * math.cos(beta)
    return {
        "torque_nm": torque,
        "torque_lcb_nm": torque - 1.0,
        "core_loss_w": 10.0 + 0.05 * current,
        "core_loss_ucb_w": 12.0 + 0.05 * current,
        "solid_loss_w": 5.0,
        "solid_loss_ucb_w": 6.0,
        "voltage_peak_v": 0.5 * current,
        "voltage_peak_ucb_v": 0.55 * current,
        "uncertainty_score": 0.1,
    }


class BatchAnalyticPredictor:
    def __init__(self) -> None:
        self.batch_calls = 0
        self.scalar_calls = 0
        self.batch_sizes: list[int] = []

    def __call__(self, features):
        self.scalar_calls += 1
        return analytic_predictor(features)

    def predict_many(self, features_list):
        self.batch_calls += 1
        self.batch_sizes.append(len(features_list))
        return [analytic_predictor(features) for features in features_list]


class OptimizationSpecTests(unittest.TestCase):
    def test_load_spec_exposes_stable_design_and_control_properties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "optimization.json"
            path.write_text(json.dumps(valid_spec_mapping()), encoding="utf-8")
            spec = opt.load_optimization_spec(path)

        self.assertEqual(len(spec.geometry_design_space), 15)
        self.assertEqual(len(spec.design_space), 16)
        self.assertEqual(spec.design_space[-1].name, "stack_length_mm")
        self.assertEqual(spec.stack_length_bounds_mm, (40.0, 70.0))
        self.assertEqual(spec.current_limit_a, 200.0)
        self.assertEqual(spec.beta_bounds_deg, (0.0, 80.0))
        self.assertEqual(spec.beta_calibration.electrical_zero_deg, 12.5)
        self.assertEqual([point.name for point in spec.operating_points], ["low_speed", "rated"])

    def test_required_fields_fail_instead_of_using_motor_defaults(self) -> None:
        cases = [
            ("stack_length_bounds_mm", lambda raw: raw.pop("stack_length_bounds_mm")),
            ("inverter.vdc_v", lambda raw: raw["inverter"].pop("vdc_v")),
            (
                "inverter.phase_peak_current_limit_a",
                lambda raw: raw["inverter"].pop("phase_peak_current_limit_a"),
            ),
            ("winding.strand_area_mm2", lambda raw: raw["winding"].pop("strand_area_mm2")),
            ("beta_calibration", lambda raw: raw.pop("beta_calibration")),
            (
                "constraints.current_density_limit_a_per_mm2",
                lambda raw: raw["constraints"].pop("current_density_limit_a_per_mm2"),
            ),
            (
                "required target",
                lambda raw: raw["operating_points"][0].pop("target_torque_nm"),
            ),
        ]
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                raw = copy.deepcopy(valid_spec_mapping())
                mutate(raw)
                with self.assertRaisesRegex(opt.OptimizationSpecError, expected):
                    opt.optimization_spec_from_mapping(raw)

    def test_rejects_unsupported_version_and_invalid_duty_sum(self) -> None:
        raw = valid_spec_mapping()
        raw["schema_version"] = 2
        with self.assertRaisesRegex(opt.OptimizationSpecError, "unsupported schema_version"):
            opt.optimization_spec_from_mapping(raw)

        raw = valid_spec_mapping()
        raw["operating_points"][0]["duty_weight"] = 0.5
        with self.assertRaisesRegex(opt.OptimizationSpecError, "sum to 1"):
            opt.optimization_spec_from_mapping(raw)

    def test_custom_design_bound_overrides_one_project_default(self) -> None:
        raw = valid_spec_mapping()
        raw["design_space"] = {"stator_outer_radius": {"lower": 140.0, "upper": 160.0}}
        spec = opt.optimization_spec_from_mapping(raw)
        bounds = {bound.name: bound for bound in spec.geometry_design_space}
        self.assertEqual((bounds["stator_outer_radius"].lower, bounds["stator_outer_radius"].upper), (140.0, 160.0))
        self.assertEqual((bounds["slot_opening_ratio"].lower, bounds["slot_opening_ratio"].upper), (0.03, 0.15))


class OptimizationPhysicsTests(unittest.TestCase):
    def test_active_volume_uses_si_units(self) -> None:
        self.assertAlmostEqual(opt.active_volume_m3(100.0, 50.0), math.pi * 0.1**2 * 0.05)

    def test_resistance_at_100c_and_loss_identities(self) -> None:
        spec = make_spec()
        design = midpoint_design(spec)
        metrics = opt.geometry_metrics(design, design["stack_length_mm"], spec.winding)
        resistance = opt.phase_resistance_100c_ohm(design, design["stack_length_mm"], spec.winding)
        expected_rho = 1.724e-8 * (1.0 + 0.00393 * 80.0)
        expected = expected_rho * 48 * metrics.mean_turn_length_mm * 1e-3 / (5.0e-6)
        self.assertAlmostEqual(resistance, expected)
        copper = opt.copper_loss_w(100.0, resistance)
        self.assertAlmostEqual(copper, 1.5 * resistance * 100.0**2)
        total = opt.total_loss_w(10.0, 5.0, copper)
        power = opt.mechanical_power_w(50.0, 1000.0)
        self.assertAlmostEqual(opt.efficiency_fraction(power, total), power / (power + total))

    def test_mtpa_seed_uses_negative_id_positive_beta_convention(self) -> None:
        seed = opt.mtpa_seed(100.0, 0.1, 0.0003, 0.0008)
        self.assertLess(seed.id_a, 0.0)
        self.assertGreater(seed.iq_a, 0.0)
        self.assertGreater(seed.beta_deg, 0.0)
        self.assertAlmostEqual(math.hypot(seed.id_a, seed.iq_a), 100.0)
        self.assertEqual(opt.dq_currents(100.0, 0.0), (0.0, 100.0))

    def test_winding_turn_consistency_is_validated(self) -> None:
        raw = valid_spec_mapping()
        raw["winding"]["coils_per_phase"] = 3
        with self.assertRaisesRegex(opt.OptimizationSpecError, "turns are inconsistent"):
            opt.optimization_spec_from_mapping(raw)


class InnerControlTests(unittest.TestCase):
    def test_control_search_is_deterministic_and_uses_conservative_fields(self) -> None:
        spec = make_spec()
        design = midpoint_design(spec)
        candidate1 = opt.evaluate_design_candidate(design, spec, analytic_predictor, candidate_id="one")
        candidate2 = opt.evaluate_design_candidate(design, spec, analytic_predictor, candidate_id="two")

        self.assertTrue(candidate1.feasible)
        self.assertEqual(
            [(row.current_peak_a, row.beta_deg) for row in candidate1.control_results],
            [(row.current_peak_a, row.beta_deg) for row in candidate2.control_results],
        )
        self.assertTrue(all(row.prediction.torque_lcb_nm >= row.operating_point.required_torque_nm for row in candidate1.control_results))
        self.assertLess(candidate1.cycle_efficiency, 1.0)

    def test_lcb_can_make_all_controls_infeasible(self) -> None:
        spec = make_spec()
        design = midpoint_design(spec)

        def pessimistic(features):
            prediction = dict(analytic_predictor(features))
            prediction["torque_lcb_nm"] = prediction["torque_nm"] - 200.0
            return prediction

        candidate = opt.evaluate_design_candidate(design, spec, pessimistic)
        self.assertFalse(candidate.feasible)
        self.assertGreater(candidate.total_constraint_violation, 0.0)
        self.assertEqual(opt.select_validation_candidates([candidate]), [])

    def test_batch_control_search_matches_scalar_and_batches_each_grid_stage(self) -> None:
        spec = make_spec()
        design = midpoint_design(spec)
        metrics = opt.geometry_metrics(design, design["stack_length_mm"], spec.winding)
        resistance = opt.phase_resistance_100c_ohm(
            design,
            design["stack_length_mm"],
            spec.winding,
        )
        batch_predictor = BatchAnalyticPredictor()
        batch_result = opt.search_operating_point_control(
            design,
            spec.operating_points[0],
            spec,
            batch_predictor,
            resistance,
            slot_fill_ratio=metrics.slot_fill_ratio,
        )
        scalar_result = opt.search_operating_point_control(
            design,
            spec.operating_points[0],
            spec,
            analytic_predictor,
            resistance,
            slot_fill_ratio=metrics.slot_fill_ratio,
        )

        self.assertEqual(batch_result, scalar_result)
        self.assertEqual(batch_predictor.scalar_calls, 0)
        self.assertEqual(batch_predictor.batch_calls, 2)
        self.assertEqual(batch_predictor.batch_sizes[0], 45)
        self.assertLess(batch_predictor.batch_sizes[1], batch_predictor.batch_sizes[0])

    def test_feature_contract_contains_raw_and_input_aliases(self) -> None:
        spec = make_spec()
        design = midpoint_design(spec)
        features = opt.build_surrogate_features(design, spec.operating_points[0], 100.0, 30.0, 0.1)
        self.assertEqual(features["stator_outer_radius"], features["input_stator_outer_radius"])
        self.assertEqual(features["input_i_peak_a"], 100.0)
        self.assertLess(features["id_a"], 0.0)
        self.assertEqual(features["beta_convention"], opt.BETA_CONVENTION)


if __name__ == "__main__":
    unittest.main()
