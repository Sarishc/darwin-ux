"""Frequentist summaries for binary, session-level metrics. Pure functions, stdlib only.

Per variant: n exposed sessions, x sessions with the outcome, rate p = x / n,
and a 95% Wilson score interval:

    centre = (p + z²/2n) / (1 + z²/n)
    half   = z · sqrt(p(1-p)/n + z²/4n²) / (1 + z²/n)

Difference (candidate − control), d = p₂ − p₁, with Newcombe's hybrid score
interval (Newcombe 1998, method 10), built from the two Wilson intervals
(l₁,u₁) and (l₂,u₂):

    lower = d − sqrt((p₂ − l₂)² + (u₁ − p₁)²)
    upper = d + sqrt((u₂ − p₂)² + (p₁ − l₁)²)

Why these: both stay inside [0, 1] / [−1, 1] and behave at 0% and 100%
(where the textbook Wald interval collapses to zero width), and they need no
statistics library. z = 1.959963984540054 (two-sided 95%).

Assumptions: sessions are independent units (assignment is per session, and
each session counts once); the sample size is fixed in advance (repeatedly
peeking at the primary metric and stopping when it looks good invalidates the
95% — N8 in OPEN_QUESTIONS.md); simulated traffic says nothing about users.

Relative difference d / p₁ is reported only when p₁ > 0 (else None).
Outputs are rounded to 6 decimals so reports hash identically across runs.
"""

import math
from dataclasses import dataclass

Z_95 = 1.959963984540054
DECIMALS = 6


def _r(value: float) -> float:
    return round(value, DECIMALS) + 0.0  # + 0.0 turns -0.0 into 0.0


@dataclass(frozen=True)
class Interval:
    lower: float
    upper: float

    def excludes_zero(self) -> bool:
        return self.lower > 0 or self.upper < 0


def wilson(successes: int, n: int, z: float = Z_95) -> Interval | None:
    """95% Wilson interval for x/n; None when n == 0 (no rate exists)."""
    _check_counts(successes, n)
    if n == 0:
        return None
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return Interval(max(0.0, centre - half), min(1.0, centre + half))


def newcombe_difference(
    x_control: int, n_control: int, x_candidate: int, n_candidate: int, z: float = Z_95
) -> Interval | None:
    """95% interval for p_candidate − p_control; None if either arm has no sessions."""
    w1, w2 = wilson(x_control, n_control, z), wilson(x_candidate, n_candidate, z)
    if w1 is None or w2 is None:
        return None
    p1, p2 = x_control / n_control, x_candidate / n_candidate
    d = p2 - p1
    lower = d - math.sqrt((p2 - w2.lower) ** 2 + (w1.upper - p1) ** 2)
    upper = d + math.sqrt((w2.upper - p2) ** 2 + (p1 - w1.lower) ** 2)
    return Interval(max(-1.0, lower), min(1.0, upper))


def _check_counts(successes: int, n: int) -> None:
    if type(successes) is not int or type(n) is not int:
        raise TypeError("counts must be integers")
    if n < 0 or successes < 0 or successes > n:
        raise ValueError("need 0 <= successes <= n")


@dataclass(frozen=True)
class ArmSummary:
    exposed: int
    successes: int
    rate: float | None
    interval: Interval | None

    def as_dict(self) -> dict[str, object]:
        return {
            "exposed_sessions": self.exposed,
            "successes": self.successes,
            "rate": None if self.rate is None else _r(self.rate),
            "wilson_95": None
            if self.interval is None
            else [_r(self.interval.lower), _r(self.interval.upper)],
        }


def arm(successes: int, n: int) -> ArmSummary:
    return ArmSummary(n, successes, successes / n if n else None, wilson(successes, n))


@dataclass(frozen=True)
class Comparison:
    control: ArmSummary
    candidate: ArmSummary
    absolute_difference: float | None
    relative_difference: float | None
    interval: Interval | None

    def as_dict(self) -> dict[str, object]:
        return {
            "control": self.control.as_dict(),
            "candidate": self.candidate.as_dict(),
            "absolute_difference": None
            if self.absolute_difference is None
            else _r(self.absolute_difference),
            "relative_difference": None
            if self.relative_difference is None
            else _r(self.relative_difference),
            "difference_95": None
            if self.interval is None
            else [_r(self.interval.lower), _r(self.interval.upper)],
            "interval_excludes_zero": None
            if self.interval is None
            else self.interval.excludes_zero(),
        }


def compare(x_control: int, n_control: int, x_candidate: int, n_candidate: int) -> Comparison:
    control, candidate = arm(x_control, n_control), arm(x_candidate, n_candidate)
    if control.rate is None or candidate.rate is None:
        return Comparison(control, candidate, None, None, None)
    d = candidate.rate - control.rate
    relative = d / control.rate if control.rate > 0 else None
    return Comparison(
        control,
        candidate,
        d,
        relative,
        newcombe_difference(x_control, n_control, x_candidate, n_candidate),
    )
