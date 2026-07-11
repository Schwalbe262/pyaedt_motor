"""Deterministic current proposals for target-load matched IPMSM FEA.

The module is intentionally solver- and scheduler-free.  A caller records one
FEA observation at a time for a single candidate / operating point / beta
probe, then asks :func:`plan_target_load_match` for the next bounded current.
No tolerance is assumed here: production code must provide an explicit policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import numbers
from typing import Iterable, Literal


DecisionStatus = Literal["propose", "matched", "infeasible", "nonmonotonic", "exhausted"]


def _is_real_number(value: object) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


@dataclass(frozen=True)
class LoadObservation:
    """One completed FEA load observation."""

    current_peak_a: float
    output_value: float

    def __post_init__(self) -> None:
        if (
            not _is_real_number(self.current_peak_a)
            or not math.isfinite(float(self.current_peak_a))
            or self.current_peak_a < 0.0
        ):
            raise ValueError("current_peak_a must be finite and >= 0")
        if (
            not _is_real_number(self.output_value)
            or not math.isfinite(float(self.output_value))
            or self.output_value < 0.0
        ):
            raise ValueError("output_value must be finite and >= 0")


@dataclass(frozen=True)
class TargetLoadPolicy:
    """Explicit safeguards for one target-load matching sequence."""

    target_value: float
    relative_tolerance: float
    initial_current_peak_a: float
    minimum_current_peak_a: float
    maximum_current_peak_a: float
    max_attempts: int
    monotonic_relative_tolerance: float
    minimum_step_relative: float = 0.01
    maximum_scale_per_attempt: float = 1.5

    def __post_init__(self) -> None:
        values = (
            self.target_value,
            self.relative_tolerance,
            self.initial_current_peak_a,
            self.minimum_current_peak_a,
            self.maximum_current_peak_a,
            self.monotonic_relative_tolerance,
            self.minimum_step_relative,
            self.maximum_scale_per_attempt,
        )
        if not all(_is_real_number(value) for value in values):
            raise ValueError("target-load policy values must not be booleans and must be real numbers")
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("target-load policy values must be finite")
        if self.target_value <= 0.0:
            raise ValueError("target_value must be > 0")
        if not 0.0 < self.relative_tolerance <= 0.05:
            raise ValueError("relative_tolerance must be in (0, 0.05]")
        if self.minimum_current_peak_a < 0.0:
            raise ValueError("minimum_current_peak_a must be >= 0")
        if self.maximum_current_peak_a <= self.minimum_current_peak_a:
            raise ValueError("maximum_current_peak_a must exceed minimum_current_peak_a")
        if not self.minimum_current_peak_a <= self.initial_current_peak_a <= self.maximum_current_peak_a:
            raise ValueError("initial_current_peak_a must stay inside the current bounds")
        if self.initial_current_peak_a <= max(1.0e-9, self.maximum_current_peak_a * 1.0e-9):
            raise ValueError("initial_current_peak_a must be a distinct positive bootstrap current")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 2:
            raise ValueError("max_attempts must be an integer >= 2")
        if not 0.0 <= self.monotonic_relative_tolerance <= 0.10:
            raise ValueError("monotonic_relative_tolerance must be in [0, 0.10]")
        if not 0.0 < self.minimum_step_relative <= 0.20:
            raise ValueError("minimum_step_relative must be in (0, 0.20]")
        if not 1.0 < self.maximum_scale_per_attempt <= 3.0:
            raise ValueError("maximum_scale_per_attempt must be in (1, 3]")
        maximum_bidirectional_step = 1.0 - 1.0 / self.maximum_scale_per_attempt
        if self.minimum_step_relative > maximum_bidirectional_step:
            raise ValueError(
                "minimum_step_relative conflicts with maximum_scale_per_attempt"
            )

    @property
    def lower_output(self) -> float:
        return self.target_value * (1.0 - self.relative_tolerance)

    @property
    def upper_output(self) -> float:
        return self.target_value * (1.0 + self.relative_tolerance)


@dataclass(frozen=True)
class TargetLoadDecision:
    """Fail-closed result of one planning step."""

    status: DecisionStatus
    proposed_current_peak_a: float | None
    matched_observation: LoadObservation | None
    relative_error: float | None
    attempts_used: int
    reason: str
    bracketed: bool


def _current_epsilon(policy: TargetLoadPolicy) -> float:
    return max(1.0e-9, policy.maximum_current_peak_a * 1.0e-9)


def _relative_error(policy: TargetLoadPolicy, observation: LoadObservation) -> float:
    return abs(observation.output_value - policy.target_value) / policy.target_value


def _normalized_observations(
    policy: TargetLoadPolicy,
    observations: Iterable[LoadObservation],
) -> tuple[LoadObservation, ...]:
    ordered = tuple(sorted(observations, key=lambda item: item.current_peak_a))
    epsilon = _current_epsilon(policy)
    for item in ordered:
        if item.current_peak_a < policy.minimum_current_peak_a - epsilon:
            raise ValueError("observation current is below the policy minimum")
        if item.current_peak_a > policy.maximum_current_peak_a + epsilon:
            raise ValueError("observation current exceeds the policy maximum")
    for previous, current in zip(ordered, ordered[1:]):
        if current.current_peak_a - previous.current_peak_a <= epsilon:
            raise ValueError("observation currents must be unique")
    return ordered


def _nonmonotonic(policy: TargetLoadPolicy, ordered: tuple[LoadObservation, ...]) -> bool:
    allowed_drop = policy.target_value * policy.monotonic_relative_tolerance
    if not ordered:
        return False
    running_maximum = ordered[0].output_value
    for current in ordered[1:]:
        if current.output_value + allowed_drop < running_maximum:
            return True
        running_maximum = max(running_maximum, current.output_value)
    return False


def _duplicate_current(
    policy: TargetLoadPolicy,
    ordered: tuple[LoadObservation, ...],
    proposed: float,
) -> bool:
    epsilon = _current_epsilon(policy)
    return any(abs(item.current_peak_a - proposed) <= epsilon for item in ordered)


def _distinct_scale_limited_proposal(
    policy: TargetLoadPolicy,
    ordered: tuple[LoadObservation, ...],
    reference_current: float,
    minimum_current: float,
    maximum_current: float,
    desired: float,
) -> float | None:
    epsilon = _current_epsilon(policy)
    if reference_current <= epsilon:
        return None
    lower_current = max(
        policy.minimum_current_peak_a,
        minimum_current,
        reference_current / policy.maximum_scale_per_attempt,
    )
    upper_current = min(
        policy.maximum_current_peak_a,
        maximum_current,
        reference_current * policy.maximum_scale_per_attempt,
    )
    if upper_current < lower_current or upper_current - lower_current <= epsilon:
        return None
    desired = min(upper_current, max(lower_current, desired))
    if not _duplicate_current(policy, ordered, desired):
        return desired
    sampled = sorted(
        {
            lower_current,
            upper_current,
            *(
                item.current_peak_a
                for item in ordered
                if lower_current <= item.current_peak_a <= upper_current
            ),
        }
    )
    intervals = [
        (right - left, left, right)
        for left, right in zip(sampled, sampled[1:])
        if right - left > 2.0 * epsilon
    ]
    if not intervals:
        return None
    _, left, right = max(intervals, key=lambda item: (item[0], -item[1]))
    proposed = (left + right) / 2.0
    return None if _duplicate_current(policy, ordered, proposed) else proposed


def _decision(
    status: DecisionStatus,
    *,
    attempts: int,
    reason: str,
    proposed: float | None = None,
    matched: LoadObservation | None = None,
    relative_error: float | None = None,
    bracketed: bool = False,
) -> TargetLoadDecision:
    return TargetLoadDecision(
        status=status,
        proposed_current_peak_a=proposed,
        matched_observation=matched,
        relative_error=relative_error,
        attempts_used=attempts,
        reason=reason,
        bracketed=bracketed,
    )


def plan_target_load_match(
    policy: TargetLoadPolicy,
    observations: Iterable[LoadObservation],
) -> TargetLoadDecision:
    """Return the next bounded current or a terminal fail-closed decision.

    Observations must be supplied in attempt order; monotonicity is checked in
    current order while the last observation is the per-attempt scale anchor.
    Once a lower and upper observation bracket the target, safeguarded secant
    interpolation is used. Outside a bracket, a bounded ratio step is proposed.
    The declared initial/numerical-zero bootstrap is the only scale-cap exception.
    """

    history = tuple(observations)
    ordered = _normalized_observations(policy, history)
    attempts = len(ordered)
    if attempts > policy.max_attempts:
        raise ValueError("observation count exceeds max_attempts")
    if not ordered:
        return _decision(
            "propose",
            attempts=0,
            reason="initial_current",
            proposed=policy.initial_current_peak_a,
        )

    closest = min(ordered, key=lambda item: (_relative_error(policy, item), item.current_peak_a))
    closest_error = _relative_error(policy, closest)
    if _nonmonotonic(policy, ordered):
        return _decision(
            "nonmonotonic",
            attempts=attempts,
            reason="output_decreased_beyond_monotonic_tolerance",
            relative_error=closest_error,
        )
    if policy.lower_output <= closest.output_value <= policy.upper_output:
        return _decision(
            "matched",
            attempts=attempts,
            reason="target_load_within_tolerance",
            matched=closest,
            relative_error=min(closest_error, policy.relative_tolerance),
        )
    below = [item for item in ordered if item.output_value < policy.lower_output]
    above = [item for item in ordered if item.output_value > policy.upper_output]
    epsilon = _current_epsilon(policy)

    if below and above:
        lower = max(below, key=lambda item: (item.output_value, item.current_peak_a))
        upper = min(above, key=lambda item: (item.output_value, item.current_peak_a))
        if lower.current_peak_a >= upper.current_peak_a:
            return _decision(
                "nonmonotonic",
                attempts=attempts,
                reason="target_bracket_has_reversed_current_order",
                relative_error=closest_error,
                bracketed=True,
            )
        if attempts >= policy.max_attempts:
            return _decision(
                "exhausted",
                attempts=attempts,
                reason="maximum_attempts_reached_inside_target_bracket",
                relative_error=closest_error,
                bracketed=True,
            )
        output_span = upper.output_value - lower.output_value
        if output_span <= 0.0:
            proposed = (lower.current_peak_a + upper.current_peak_a) / 2.0
        else:
            proposed = lower.current_peak_a + (
                (policy.target_value - lower.output_value)
                * (upper.current_peak_a - lower.current_peak_a)
                / output_span
            )
        bracket_span = upper.current_peak_a - lower.current_peak_a
        current_margin = min(
            bracket_span * 0.25,
            max(epsilon * 2.0, bracket_span * policy.minimum_step_relative),
        )
        if lower.current_peak_a + current_margin >= upper.current_peak_a - current_margin:
            proposed = (lower.current_peak_a + upper.current_peak_a) / 2.0
        else:
            proposed = min(
                upper.current_peak_a - current_margin,
                max(lower.current_peak_a + current_margin, proposed),
            )
        proposed = _distinct_scale_limited_proposal(
            policy,
            ordered,
            history[-1].current_peak_a,
            lower.current_peak_a,
            upper.current_peak_a,
            proposed,
        )
        if proposed is None:
            return _decision(
                "exhausted",
                attempts=attempts,
                reason="bracket_is_too_narrow_for_a_new_current",
                relative_error=closest_error,
                bracketed=True,
            )
        return _decision(
            "propose",
            attempts=attempts,
            reason="safeguarded_secant_inside_target_bracket",
            proposed=proposed,
            relative_error=closest_error,
            bracketed=True,
        )

    if below:
        anchor = max(below, key=lambda item: item.current_peak_a)
        if anchor.current_peak_a >= policy.maximum_current_peak_a - epsilon:
            return _decision(
                "infeasible",
                attempts=attempts,
                reason="maximum_current_reached_below_target",
                relative_error=closest_error,
            )
        if attempts >= policy.max_attempts:
            return _decision(
                "exhausted",
                attempts=attempts,
                reason="maximum_attempts_reached_below_target",
                relative_error=closest_error,
            )
        if anchor.current_peak_a <= epsilon:
            proposed = policy.initial_current_peak_a
            if _duplicate_current(policy, ordered, proposed):
                return _decision(
                    "infeasible",
                    attempts=attempts,
                    reason="no_distinct_positive_bootstrap_current_is_available",
                    relative_error=closest_error,
                )
            return _decision(
                "propose",
                attempts=attempts,
                reason="zero_current_bootstrap",
                proposed=proposed,
                relative_error=closest_error,
            )
        ratio = (
            policy.maximum_scale_per_attempt
            if anchor.output_value <= 0.0
            else policy.target_value / anchor.output_value
        )
        factor = min(
            policy.maximum_scale_per_attempt,
            max(1.0 + policy.minimum_step_relative, ratio),
        )
        minimum_step = max(epsilon * 2.0, anchor.current_peak_a * policy.minimum_step_relative)
        proposed = _distinct_scale_limited_proposal(
            policy,
            ordered,
            history[-1].current_peak_a,
            anchor.current_peak_a + minimum_step,
            policy.maximum_current_peak_a,
            max(anchor.current_peak_a + minimum_step, anchor.current_peak_a * factor),
        )
        if proposed is None:
            return _decision(
                "exhausted",
                attempts=attempts,
                reason="scale_cap_prevents_distinct_higher_current",
                relative_error=closest_error,
            )
        return _decision(
            "propose",
            attempts=attempts,
            reason="bounded_ratio_increase",
            proposed=proposed,
            relative_error=closest_error,
        )

    anchor = min(above, key=lambda item: item.current_peak_a)
    if anchor.current_peak_a <= policy.minimum_current_peak_a + epsilon:
        return _decision(
            "infeasible",
            attempts=attempts,
            reason="minimum_current_reached_above_target",
            relative_error=closest_error,
        )
    if attempts >= policy.max_attempts:
        return _decision(
            "exhausted",
            attempts=attempts,
            reason="maximum_attempts_reached_above_target",
            relative_error=closest_error,
        )
    ratio = policy.target_value / anchor.output_value if anchor.output_value > 0.0 else 0.0
    factor = max(
        1.0 / policy.maximum_scale_per_attempt,
        min(1.0 - policy.minimum_step_relative, ratio),
    )
    minimum_step = max(epsilon * 2.0, anchor.current_peak_a * policy.minimum_step_relative)
    proposed = _distinct_scale_limited_proposal(
        policy,
        ordered,
        history[-1].current_peak_a,
        policy.minimum_current_peak_a,
        anchor.current_peak_a - minimum_step,
        min(anchor.current_peak_a - minimum_step, anchor.current_peak_a * factor),
    )
    if proposed is None:
        return _decision(
            "exhausted",
            attempts=attempts,
            reason="scale_cap_prevents_distinct_lower_current",
            relative_error=closest_error,
        )
    return _decision(
        "propose",
        attempts=attempts,
        reason="bounded_ratio_decrease",
        proposed=proposed,
        relative_error=closest_error,
    )
