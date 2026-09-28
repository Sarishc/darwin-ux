"""Active collection windows, derived from the immutable lifecycle history. Pure.

An experiment collects evidence ONLY while it is `running`. Each move into
`running` opens a window at that event's time; the next event (pause, stop,
complete) closes it. Windows are half-open: [opened_at, closed_at).

    draft ─ running 10:00 ─ paused 10:30 ─ running 10:45 ─ completed 11:30
    windows: [10:00, 10:30), [10:45, 11:30)

A window still open at analysis time is closed at `as_of`. The history is
validated first (contiguous sequence from 0, starts as draft, every step an
allowed transition, strictly increasing times); a broken history raises
LifecycleHistoryError, and the analysis fails closed (no conclusion).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .vocabulary import TRANSITIONS


class LifecycleHistoryError(ValueError):
    pass


@dataclass(frozen=True)
class LifecycleStep:
    sequence: int
    from_status: str | None
    to_status: str
    occurred_at: datetime


@dataclass(frozen=True)
class Window:
    opened_at: datetime
    closed_at: datetime  # exclusive

    def contains(self, moment: datetime) -> bool:
        return self.opened_at <= moment < self.closed_at

    def as_list(self) -> list[str]:
        return [self.opened_at.isoformat(), self.closed_at.isoformat()]


def validate_history(steps: Sequence[LifecycleStep]) -> None:
    if not steps:
        raise LifecycleHistoryError("empty lifecycle history")
    first = steps[0]
    if (first.sequence, first.from_status, first.to_status) != (0, None, "draft"):
        raise LifecycleHistoryError("history must start with draft at sequence 0")
    for previous, step in zip(steps, steps[1:], strict=False):
        if step.sequence != previous.sequence + 1:
            raise LifecycleHistoryError("sequence is not contiguous")
        if step.from_status != previous.to_status:
            raise LifecycleHistoryError("history chain is broken")
        if (step.from_status, step.to_status) not in TRANSITIONS:
            raise LifecycleHistoryError("transition not allowed")
        if step.occurred_at <= previous.occurred_at:
            raise LifecycleHistoryError("times do not increase")


def collection_windows(steps: Sequence[LifecycleStep], as_of: datetime) -> list[Window]:
    """The running intervals up to `as_of` (later parts are cut off)."""
    validate_history(steps)
    windows: list[Window] = []
    opened: datetime | None = None
    for step in steps:
        if step.to_status == "running":
            opened = step.occurred_at
        elif opened is not None:
            windows.append(Window(opened, step.occurred_at))
            opened = None
    if opened is not None:
        windows.append(Window(opened, as_of))
    return [Window(w.opened_at, min(w.closed_at, as_of)) for w in windows if w.opened_at < as_of]


def window_containing(windows: Sequence[Window], moment: datetime) -> Window | None:
    return next((w for w in windows if w.contains(moment)), None)
