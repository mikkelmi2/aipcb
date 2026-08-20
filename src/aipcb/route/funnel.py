"""The funnel algorithm: tightening a sleeve into the shortest path through it.

Given a sleeve -- the strip of triangles a route's homotopy class passes through --
this produces the shortest path from start to end that stays inside it. Because the
sleeve's triangles are cut out of free space that has already been inflated by the
routing clearance, the shortest path through it is also a legal one.

The implementation is the funnel algorithm discovered independently by Tompa (1981),
Chazelle (1982), Lee & Preparata (1984) and Leiserson & Maley (1985): walk the
sleeve one diagonal at a time, maintaining an apex and two concave chains, and emit
a vertex whenever the chains cross. Each diagonal is handled in amortised constant
time, so the whole tightening is linear in the length of the sleeve.

The function is pure. Same portals in, same points out, on any machine.
"""

from __future__ import annotations

__all__ = ["Portal", "orient_portals", "signed_area", "tighten"]

Point = tuple[float, float]
#: A gate the path must pass through, as its two endpoints in (left, right) order.
Portal = tuple[Point, Point]


def signed_area(a: Point, b: Point, c: Point) -> float:
    """Twice the signed area of the triangle ``abc``. Positive means one turn
    direction, negative the other; the algorithm only cares that it is consistent."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])


def orient_portals(
    diagonals: list[tuple[Point, Point]], waypoints: list[Point]
) -> list[Portal]:
    """Put each diagonal's endpoints into a consistent (left, right) order.

    A triangulation edge has no inherent direction, but the funnel needs to know
    which endpoint is on which side of the corridor. The travel direction through
    each diagonal -- from the previous reference point to the next -- decides it.

    Note the sign. Board coordinates have Y pointing down, so a *negative* signed
    area means the point is on the left as the corridor is travelled. Getting this
    backwards does not crash or fail any structural check: the sleeve is still
    valid, the funnel still terminates, and the result is a legal path -- just a
    catastrophically bad one. On the USB example the inverted sign turned a 24.6 mm
    route into a 162.6 mm zigzag between the pad rows.
    """
    portals: list[Portal] = []
    for index, (a, b) in enumerate(diagonals):
        before = waypoints[index]
        after = waypoints[index + 1]
        portals.append((a, b) if signed_area(before, after, a) < 0 else (b, a))
    return portals


def tighten(start: Point, end: Point, portals: list[Portal]) -> list[Point]:
    """Return the shortest path from ``start`` to ``end`` through ``portals``."""
    path: list[Point] = [start]
    if not portals:
        return _dedupe([start, end])

    gates: list[Portal] = [(start, start), *portals, (end, end)]

    apex = start
    left = start
    right = start
    apex_index = left_index = right_index = 0

    index = 1
    while index < len(gates):
        gate_left, gate_right = gates[index]

        # Tighten the right chain.
        if signed_area(apex, right, gate_right) <= 0:
            if apex == right or signed_area(apex, left, gate_right) > 0:
                right = gate_right
                right_index = index
            else:
                # The chains crossed: the left vertex is a corner of the path.
                path.append(left)
                apex = left
                apex_index = left_index
                left = right = apex
                left_index = right_index = apex_index
                index = apex_index + 1
                continue

        # Tighten the left chain.
        if signed_area(apex, left, gate_left) >= 0:
            if apex == left or signed_area(apex, right, gate_left) < 0:
                left = gate_left
                left_index = index
            else:
                path.append(right)
                apex = right
                apex_index = right_index
                left = right = apex
                left_index = right_index = apex_index
                index = apex_index + 1
                continue

        index += 1

    path.append(end)
    return _dedupe(path)


def _dedupe(points: list[Point], tolerance: float = 1e-6) -> list[Point]:
    """Drop repeated and collinear points, which are noise in a track."""
    cleaned: list[Point] = []
    for point in points:
        if cleaned and _close(cleaned[-1], point, tolerance):
            continue
        cleaned.append(point)
    if len(cleaned) < 3:
        return cleaned

    simplified = [cleaned[0]]
    for previous, current, following in zip(cleaned, cleaned[1:], cleaned[2:], strict=False):
        if abs(signed_area(previous, current, following)) > tolerance:
            simplified.append(current)
    simplified.append(cleaned[-1])
    return simplified


def _close(a: Point, b: Point, tolerance: float) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance
