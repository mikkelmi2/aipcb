# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Where a routing run spends its wall clock, measured at stage boundaries.

M16c, and the reason it exists is in the toporouter postmortem: runtime, not
correctness, is what made that router unusable -- ninety seconds and thirty-five
failed nets on a 269-net board -- and it had no benchmark, so nobody could see it
coming or tell a regression from a hard board. This is the instrument that makes
`aipcb bench` possible.

**It measures at stage boundaries and nowhere else.** A timer inside the funnel's
inner loop or the A* relaxation would distort the thing it is trying to describe,
and the number it produced would be a fact about the instrument. There are eight
stages on a routing run and each is entered once, so the whole apparatus costs
eight calls to :func:`time.perf_counter` -- unmeasurable against a run that takes
seconds.

**And it is off unless somebody asks.** ``stages`` is ``None`` on every code path
that is not a benchmark, and :meth:`Stages.stage` is the only thing that ever
touches the clock, so the router's normal behaviour is exactly what it was.
Timings are wall clock rather than CPU time on purpose: wall clock is what a user
waits, and it is the number the postmortem's benchmark table is written in.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

__all__ = ["Stages", "stage"]


@dataclass(slots=True)
class Stages:
    """Seconds per named stage, in the order the stages were first entered.

    Re-entering a stage adds to it rather than replacing it, so a phase that runs
    once per layer still reports as one number. Insertion order is kept because it
    is the order of the pipeline, which is how a reader wants the table.
    """

    seconds: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[name] = self.seconds.get(name, 0.0) + (
                time.perf_counter() - started
            )

    @property
    def total(self) -> float:
        return sum(self.seconds.values())

    def to_dict(self) -> dict[str, float]:
        return {name: round(value, 4) for name, value in self.seconds.items()}


@contextmanager
def stage(stages: Stages | None, name: str) -> Iterator[None]:
    """Time a stage when somebody is measuring, and do nothing when nobody is.

    The call sites read the same either way, which is what keeps the instrument
    from leaking ``if stages is not None`` into the router.
    """
    if stages is None:
        yield
        return
    with stages.stage(name):
        yield
