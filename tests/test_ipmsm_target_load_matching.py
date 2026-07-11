from __future__ import annotations

import unittest

import numpy as np

from ipmsm_target_load_matching import (
    LoadObservation,
    TargetLoadPolicy,
    plan_target_load_match,
)


def policy(**overrides: object) -> TargetLoadPolicy:
    values: dict[str, object] = {
        "target_value": 100.0,
        "relative_tolerance": 0.01,
        "initial_current_peak_a": 50.0,
        "minimum_current_peak_a": 10.0,
        "maximum_current_peak_a": 100.0,
        "max_attempts": 5,
        "monotonic_relative_tolerance": 0.005,
        "minimum_step_relative": 0.01,
        "maximum_scale_per_attempt": 1.5,
    }
    values.update(overrides)
    return TargetLoadPolicy(**values)  # type: ignore[arg-type]


class TargetLoadPolicyTests(unittest.TestCase):
    def test_policy_requires_explicit_bounded_tolerance(self) -> None:
        with self.assertRaisesRegex(ValueError, "relative_tolerance"):
            policy(relative_tolerance=0.0)
        with self.assertRaisesRegex(ValueError, "relative_tolerance"):
            policy(relative_tolerance=0.051)
        with self.assertRaisesRegex(ValueError, "inside the current bounds"):
            policy(initial_current_peak_a=101.0)
        with self.assertRaisesRegex(ValueError, "positive bootstrap"):
            policy(initial_current_peak_a=0.0, minimum_current_peak_a=0.0)
        with self.assertRaisesRegex(ValueError, "must not be booleans"):
            policy(target_value=True)
        with self.assertRaisesRegex(ValueError, "not be booleans"):
            policy(target_value=np.bool_(True))
        with self.assertRaisesRegex(ValueError, "conflicts with maximum_scale"):
            policy(minimum_step_relative=0.18, maximum_scale_per_attempt=1.1)

    def test_observation_rejects_nonphysical_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "current_peak_a"):
            LoadObservation(-1.0, 1.0)
        with self.assertRaisesRegex(ValueError, "output_value"):
            LoadObservation(1.0, -1.0)
        with self.assertRaisesRegex(ValueError, "current_peak_a"):
            LoadObservation(True, 1.0)
        with self.assertRaisesRegex(ValueError, "output_value"):
            LoadObservation(1.0, False)
        with self.assertRaisesRegex(ValueError, "current_peak_a"):
            LoadObservation(np.bool_(True), 1.0)
        with self.assertRaisesRegex(ValueError, "output_value"):
            LoadObservation(1.0, np.bool_(False))


class TargetLoadPlannerTests(unittest.TestCase):
    def test_empty_history_proposes_declared_initial_current(self) -> None:
        result = plan_target_load_match(policy(), [])
        self.assertEqual(result.status, "propose")
        self.assertEqual(result.proposed_current_peak_a, 50.0)
        self.assertEqual(result.attempts_used, 0)

    def test_matching_observation_is_terminal(self) -> None:
        observed = LoadObservation(50.0, 100.5)
        result = plan_target_load_match(policy(), [observed])
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.matched_observation, observed)
        self.assertAlmostEqual(result.relative_error or 0.0, 0.005)

    def test_exact_tolerance_boundaries_are_matched(self) -> None:
        declared = policy(target_value=14.32394487827058, relative_tolerance=0.01)
        for output in (declared.lower_output, declared.upper_output):
            with self.subTest(output=output):
                result = plan_target_load_match(declared, [LoadObservation(50.0, output)])
                self.assertEqual(result.status, "matched")
                self.assertIsNotNone(result.matched_observation)
                self.assertLessEqual(result.relative_error or 0.0, declared.relative_tolerance)

    def test_below_target_uses_bounded_ratio_increase(self) -> None:
        result = plan_target_load_match(policy(), [LoadObservation(50.0, 50.0)])
        self.assertEqual(result.status, "propose")
        self.assertEqual(result.reason, "bounded_ratio_increase")
        self.assertEqual(result.proposed_current_peak_a, 75.0)

    def test_zero_current_uses_explicit_bootstrap_not_ratio_scaling(self) -> None:
        declared = policy(
            initial_current_peak_a=25.0,
            minimum_current_peak_a=0.0,
            maximum_current_peak_a=1000.0,
        )
        result = plan_target_load_match(declared, [LoadObservation(0.0, 0.0)])
        self.assertEqual(result.status, "propose")
        self.assertEqual(result.reason, "zero_current_bootstrap")
        self.assertEqual(result.proposed_current_peak_a, 25.0)

    def test_numerical_zero_bootstrap_is_the_only_scale_cap_exception(self) -> None:
        declared = policy(
            initial_current_peak_a=25.0,
            minimum_current_peak_a=0.0,
            maximum_current_peak_a=1000.0,
        )
        numerical_zero = plan_target_load_match(declared, [LoadObservation(1.0e-7, 0.0)])
        self.assertEqual(numerical_zero.reason, "zero_current_bootstrap")
        positive = plan_target_load_match(declared, [LoadObservation(1.0e-5, 0.0)])
        self.assertEqual(positive.reason, "bounded_ratio_increase")
        self.assertLessEqual(
            positive.proposed_current_peak_a or float("inf"),
            1.0e-5 * declared.maximum_scale_per_attempt,
        )

    def test_above_target_uses_bounded_ratio_decrease(self) -> None:
        result = plan_target_load_match(policy(), [LoadObservation(80.0, 160.0)])
        self.assertEqual(result.status, "propose")
        self.assertEqual(result.reason, "bounded_ratio_decrease")
        self.assertAlmostEqual(result.proposed_current_peak_a or 0.0, 80.0 / 1.5)

    def test_scale_cap_is_not_overridden_by_wide_policy_span(self) -> None:
        wide = policy(
            initial_current_peak_a=1.0,
            minimum_current_peak_a=0.0,
            maximum_current_peak_a=1000.0,
        )
        increase = plan_target_load_match(wide, [LoadObservation(1.0, 1.0)])
        decrease = plan_target_load_match(wide, [LoadObservation(1.0, 200.0)])
        self.assertAlmostEqual(increase.proposed_current_peak_a or 0.0, 1.5)
        self.assertAlmostEqual(decrease.proposed_current_peak_a or 0.0, 1.0 / 1.5)

    def test_bracket_uses_safeguarded_secant(self) -> None:
        result = plan_target_load_match(
            policy(minimum_step_relative=0.001),
            [LoadObservation(40.0, 80.0), LoadObservation(70.0, 120.0)],
        )
        self.assertEqual(result.status, "propose")
        self.assertTrue(result.bracketed)
        self.assertAlmostEqual(result.proposed_current_peak_a or 0.0, 55.0)

    def test_bracket_proposal_obeys_scale_cap_from_latest_attempt(self) -> None:
        declared = policy(minimum_current_peak_a=1.0, maximum_current_peak_a=100.0)
        result = plan_target_load_match(
            declared,
            [LoadObservation(10.0, 130.0), LoadObservation(5.0, 50.0)],
        )
        self.assertEqual(result.status, "propose")
        self.assertTrue(result.bracketed)
        self.assertLessEqual(
            result.proposed_current_peak_a or float("inf"),
            5.0 * declared.maximum_scale_per_attempt,
        )
        self.assertGreater(result.proposed_current_peak_a or 0.0, 5.0)

    def test_sampled_secant_uses_largest_unsampled_bracket_interval(self) -> None:
        result = plan_target_load_match(
            policy(monotonic_relative_tolerance=0.005),
            [
                LoadObservation(40.0, 98.0),
                LoadObservation(50.0, 97.5),
                LoadObservation(60.0, 102.0),
            ],
        )
        self.assertEqual(result.status, "propose")
        self.assertTrue(result.bracketed)
        self.assertEqual(result.proposed_current_peak_a, 45.0)

    def test_nonmonotonic_response_fails_closed(self) -> None:
        result = plan_target_load_match(
            policy(monotonic_relative_tolerance=0.0),
            [LoadObservation(40.0, 90.0), LoadObservation(60.0, 80.0)],
        )
        self.assertEqual(result.status, "nonmonotonic")
        self.assertIsNone(result.proposed_current_peak_a)

    def test_nonmonotonic_history_is_not_hidden_by_one_matching_row(self) -> None:
        result = plan_target_load_match(
            policy(monotonic_relative_tolerance=0.0),
            [
                LoadObservation(40.0, 110.0),
                LoadObservation(50.0, 100.0),
                LoadObservation(60.0, 90.0),
            ],
        )
        self.assertEqual(result.status, "nonmonotonic")
        self.assertIsNone(result.matched_observation)

    def test_cumulative_small_drops_fail_running_maximum_check(self) -> None:
        result = plan_target_load_match(
            policy(monotonic_relative_tolerance=0.005),
            [
                LoadObservation(40.0, 80.0),
                LoadObservation(50.0, 79.6),
                LoadObservation(60.0, 79.2),
            ],
        )
        self.assertEqual(result.status, "nonmonotonic")

    def test_current_limits_fail_closed(self) -> None:
        high = plan_target_load_match(policy(), [LoadObservation(100.0, 80.0)])
        low = plan_target_load_match(policy(), [LoadObservation(10.0, 120.0)])
        self.assertEqual(high.status, "infeasible")
        self.assertEqual(low.status, "infeasible")

    def test_attempt_budget_is_checked_before_new_proposal(self) -> None:
        result = plan_target_load_match(
            policy(max_attempts=2),
            [LoadObservation(40.0, 60.0), LoadObservation(50.0, 70.0)],
        )
        self.assertEqual(result.status, "exhausted")
        self.assertIsNone(result.proposed_current_peak_a)

    def test_proven_current_bound_infeasibility_precedes_attempt_exhaustion(self) -> None:
        result = plan_target_load_match(
            policy(max_attempts=2),
            [LoadObservation(50.0, 60.0), LoadObservation(100.0, 80.0)],
        )
        self.assertEqual(result.status, "infeasible")
        self.assertEqual(result.reason, "maximum_current_reached_below_target")

    def test_duplicate_or_out_of_bounds_observations_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            plan_target_load_match(
                policy(),
                [LoadObservation(50.0, 70.0), LoadObservation(50.0, 80.0)],
            )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            plan_target_load_match(policy(), [LoadObservation(101.0, 120.0)])
        with self.assertRaisesRegex(ValueError, "exceeds max_attempts"):
            plan_target_load_match(
                policy(max_attempts=2),
                [
                    LoadObservation(40.0, 60.0),
                    LoadObservation(50.0, 70.0),
                    LoadObservation(60.0, 80.0),
                ],
            )


if __name__ == "__main__":
    unittest.main()
