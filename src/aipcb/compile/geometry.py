"""Coordinate transforms shared by the schematic and board writers.

KiCad's symbol libraries use a Y-up coordinate system while sheets and boards use
Y-down, so placing a symbol involves a mirror as well as a rotation. Getting the
order wrong produces a schematic that looks right and connects wrong, which is the
worst kind of bug this toolchain could ship -- so the transform lives in one place,
is a pure function, and is verified against KiCad's own netlister in the tests.
"""

from __future__ import annotations

import math
from typing import NamedTuple

__all__ = ["MM", "Point", "place_direction", "place_point"]

#: Schematic and board coordinates are millimetres throughout.
MM = 1.0


class Point(NamedTuple):
    x: float
    y: float

    def rounded(self, places: int = 4) -> Point:
        return Point(round(self.x, places), round(self.y, places))


def place_point(origin: Point, rotation_deg: float, x: float, y: float) -> Point:
    """Map a point from symbol-library space into sheet space.

    The library is Y-up and the sheet is Y-down, so the Y axis is mirrored first;
    the placement rotation is then applied in sheet space. Doing it in this order --
    rather than rotating in library space and mirroring afterwards -- is what
    matches KiCad, and the difference shows up as pins swapping ends on any symbol
    placed at 90 or 270 degrees.
    """
    theta = math.radians(rotation_deg)
    cos, sin = math.cos(theta), math.sin(theta)
    fx, fy = x, -y
    return Point(origin.x + fx * cos - fy * sin, origin.y + fx * sin + fy * cos)


def place_direction(rotation_deg: float, angle_deg: float) -> Point:
    """Map a unit direction from library space into sheet space.

    Used for pin stubs: a pin's ``at`` angle points from its connection end toward
    the symbol body, so the outward direction is the opposite.
    """
    return place_point(Point(0.0, 0.0), rotation_deg,
                       math.cos(math.radians(angle_deg)),
                       math.sin(math.radians(angle_deg)))
