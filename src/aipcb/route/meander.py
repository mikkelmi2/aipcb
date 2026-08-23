# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Length matching: making the short half of a pair as long as the long one.

Skew is length, so closing it means adding length, and the only honest place to add
it is where there is room. A meander is a detour folded into a straight stretch of a
route: the wire leaves its line, comes back, and arrives at the same place having
travelled further.

Three rules shape what is built here, and all three are about not making the board
worse to fix a number:

**In the net's own corridor.** A meander that wanders into a neighbouring corridor
turns a skew warning into a clearance violation. Every candidate is checked against
the same free space the route was tightened in, so a meander that does not fit is
not built.

**Not in the coupled run.** A differential pair's two halves are coupled because
they are parallel; folding one of them up destroys exactly the property the pair
exists for. Meanders go in the fan-out, where the halves are already going their
separate ways.

**Barrels count.** A via is a millimetre and a half of conductor on a 1.6 mm board,
which is ten times a fast pair's skew budget. The length being matched is the whole
conductor, vias included; the caller passes the shortfall and this module adds it.
"""

from __future__ import annotations

import math
from itertools import pairwise

__all__ = ["MEANDER_PEAKS", "lengthen", "polyline_length"]

Point = tuple[float, float]

#: How many peaks a meander is folded into. Three is enough to add real length
#: without the amplitude becoming a feature of its own, and few enough that the
#: result still looks like a track a person would draw.
MEANDER_PEAKS = 3

#: The largest amplitude tried, as a multiple of the corridor a track needs. Beyond
#: this a "meander" is a detour, and a detour belongs in the topology, not here.
MAX_AMPLITUDE = 6.0

#: How close to the target length counts as matched, in millimetres. A tenth of the
#: tightest skew budget anyone writes.
TOLERANCE = 0.005


def polyline_length(points: list[Point]) -> float:
    return sum(math.dist(a, b) for a, b in pairwise(points))


def lengthen(
    points: list[Point],
    extra: float,
    fits: object,
    corridor: float,
    *,
    keep: tuple[int, int] = (0, 0),
) -> list[Point] | None:
    """Add ``extra`` millimetres to a polyline by meandering its longest segment.

    ``fits`` is a predicate taking a candidate polyline and saying whether it is
    still legal -- in practice, whether it stays inside the free space the route was
    tightened in. ``keep`` protects a number of segments at each end from being
    meandered, which is how the coupled part of a pair is left alone.

    Returns ``None`` when the length cannot be added without leaving the corridor,
    which is a real answer: the honest response to "this pair cannot be matched
    here" is to say so, not to ship a meander that violates clearance.
    """
    if extra <= TOLERANCE or len(points) < 2:
        return None

    order = sorted(
        range(keep[0], len(points) - 1 - keep[1]),
        key=lambda i: (-math.dist(points[i], points[i + 1]), i),
    )
    for index in order:
        start, end = points[index], points[index + 1]
        span = math.dist(start, end)
        if span < corridor * 2:
            continue
        amplitude = _amplitude_for(start, end, extra, corridor, fits, points, index)
        if amplitude is None:
            continue
        folded = _meander(start, end, amplitude)
        return [*points[: index + 1], *folded, *points[index + 1 :]]
    return None


def _amplitude_for(
    start: Point,
    end: Point,
    extra: float,
    corridor: float,
    fits: object,
    points: list[Point],
    index: int,
) -> float | None:
    """The smallest amplitude that adds ``extra`` and still fits, or ``None``.

    The relationship between amplitude and added length is monotone and smooth, so a
    bisection finds the amplitude that adds exactly the shortfall -- no more, since
    over-lengthening a matched pair is the same mistake as under-lengthening it.
    """
    assert callable(fits)
    ceiling = corridor * MAX_AMPLITUDE
    if _added(start, end, ceiling) < extra:
        return None

    low, high = 0.0, ceiling
    for _ in range(40):
        middle = (low + high) / 2
        if _added(start, end, middle) < extra:
            low = middle
        else:
            high = middle
    amplitude = high
    if abs(_added(start, end, amplitude) - extra) > TOLERANCE * 10:
        return None

    candidate = [
        *points[: index + 1],
        *_meander(start, end, amplitude),
        *points[index + 1 :],
    ]
    return amplitude if fits(candidate) else None


def _added(start: Point, end: Point, amplitude: float) -> float:
    """How much longer the meandered segment is than the straight one."""
    return polyline_length([start, *_meander(start, end, amplitude), end]) - math.dist(
        start, end
    )


def _meander(start: Point, end: Point, amplitude: float) -> list[Point]:
    """The interior points of a meander folded into one segment.

    A symmetric zigzag: peaks alternate sides of the line, evenly spaced, with the
    ends left on the line so the meander joins its neighbours without a kink.
    """
    span = math.dist(start, end)
    if span < 1e-9 or amplitude <= 0:
        return []
    ux, uy = (end[0] - start[0]) / span, (end[1] - start[1]) / span
    nx, ny = -uy, ux
    points: list[Point] = []
    for peak in range(MEANDER_PEAKS):
        along = span * (peak + 0.5) / MEANDER_PEAKS
        side = amplitude if peak % 2 == 0 else -amplitude
        points.append(
            (
                start[0] + ux * along + nx * side,
                start[1] + uy * along + ny * side,
            )
        )
    return points
