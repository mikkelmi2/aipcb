"""Pattern-based escape routing: getting a dense package's signals out from under it.

A fine-pitch package is where rubber-band routing meets its density ceiling, and
not because the router is weak. Escaping a 0.5 mm-pitch QFN is not a search
problem -- it is a *pattern*, one that a layout engineer draws from memory: a short
stub outward from each perimeter pad to a via just clear of the part, staggered into
two rows so the vias are not at pad pitch; or, for an area array, a dog-bone into
the gap between four balls, in the quadrant the ball points toward.

So M9e solves it with a deterministic generator rather than by bolting on a second
autorouter (ADR 0008 records why the latter is rejected rather than deferred).

The architectural rule is the important part: **fanout runs before routing, and its
output is fixed obstacles plus terminals.** The generator lays the stub and the via,
registers that copper as an obstacle, and publishes an *escape terminal* into the
routing environment in place of the package pad. The rubber-band router that runs
afterwards has no idea a fanout happened -- it sees pads at the escape points and
routes between them exactly as it always has.

Everything here meets the same bars as the rest of the toolchain: deterministic,
UUID-mapped, byte-stable, and keyed by **pad instance** rather than pad number,
because a QFN's exposed pad and a receptacle's four shield tabs are all one number
and several pieces of copper.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode
from aipcb.model.mech import Fanout
from aipcb.netlist import Netlist
from aipcb.route.geometry import class_for, rules_for, track_obstacles, via_obstacles
from aipcb.route.obstacles import Obstacle, RoutingEnvironment
from aipcb.route.stack import RoutingStack
from aipcb.route.stretch import RoutedConnection, StretchResult, Via

__all__ = [
    "ESCAPE_SUFFIX",
    "FanoutResult",
    "choose_style",
    "escape_direction",
    "generate_fanout",
    "settle_escapes",
    "via_count",
]

Point = tuple[float, float]

#: How an escape terminal is named: the pad instance it belongs to, plus this. The
#: pad instance, never the pad number -- a QFN's exposed pad and its pin 1 are both
#: real copper and only one of them is called "1".
ESCAPE_SUFFIX = "@esc"

#: How many staggered rows a perimeter escape will try before giving up on a pad.
#: Two is the classic pattern; the rest are for the pad that has something in the
#: way, and past four the vias are so far out that the escape has stopped being one.
MAX_ESCAPE_ROWS = 4

#: A pad centre this close to the hull of all the pad centres counts as being on it.
#: A tenth of a millimetre -- finer than any real pitch, coarser than the rounding
#: in a footprint file.
_HULL_TOLERANCE = 0.1


@dataclass(slots=True)
class FanoutResult:
    """What the generator produced, and how the router is to see it."""

    connections: list[RoutedConnection] = field(default_factory=list)
    """The stub-and-via copper, as ordinary routed connections."""
    obstacles: list[Obstacle] = field(default_factory=list)
    """That copper again, as things every later route must go around."""
    terminals: dict[str, tuple[str, Point, frozenset[str]]] = field(default_factory=dict)
    """Escape name to the net it carries, where it is, and the layers it reaches."""
    replaced: set[str] = field(default_factory=set)
    """Package pads the router must no longer try to reach directly."""

    def apply(self, environment: RoutingEnvironment) -> None:
        """Swap the fanned pads for their escape terminals, in place.

        After this the environment describes a board on which the package's signals
        already sit outside its pad field. That is the whole interface between the
        pattern generator and the router: no new concept, just different terminals.
        """
        for obstacle in self.obstacles:
            environment.obstacles[obstacle.name] = obstacle
        for pad in self.replaced:
            environment.pad_nets.pop(pad, None)
        for name, (net, point, layers) in self.terminals.items():
            environment.pad_centres[name] = point
            environment.pad_nets[name] = net
            environment.pad_layers[name] = layers

    def summary(self) -> dict[str, object]:
        return {
            "packages": sorted({n.split(".")[0] for n in self.terminals}),
            "escapes": len(self.terminals),
            "vias": sum(len(c.vias) for c in self.connections),
        }


# ---------------------------------------------------------------------------
# the generator
# ---------------------------------------------------------------------------


def generate_fanout(
    board: SNode,
    environment: RoutingEnvironment,
    netlist: Netlist,
    stack: RoutingStack,
    report: Report,
    *,
    congestion: float = 1.0,
) -> FanoutResult:
    """Run every package's escape pattern, before anything else is routed."""
    result = FanoutResult()
    if not netlist.fanout:
        return result

    frames = _package_frames(board)
    for refdes in sorted(netlist.fanout):
        intent = netlist.fanout[refdes]
        if intent.style == "none":
            continue
        frame = frames.get(refdes)
        if frame is None:
            report.warning(
                "fanout-unknown-package",
                f"`fanout:` names {refdes}, which has no footprint on the board",
                path=netlist.mech_path("fanout", refdes),
                hint="fanout applies to a placed package; check the reference "
                "designator",
            )
            continue
        _fan_out_package(
            refdes, intent, frame, environment, netlist, stack, result, report,
            congestion,
        )
    if result.terminals:
        report.info(
            "fanout-generated",
            f"{len(result.terminals)} escape"
            f"{'s' if len(result.terminals) != 1 else ''} generated for "
            f"{', '.join(sorted({n.split('.')[0] for n in result.terminals}))}",
            hint="the escape vias are the terminals the router sees; the pattern "
            "itself is a fixed obstacle to it",
        )
    return result


@dataclass(frozen=True, slots=True)
class _Frame:
    """A placed package: where it is, which way it faces, and what layer it is on."""

    refdes: str
    origin: Point
    rotation: float
    layer: str

    def axes(self) -> tuple[Point, Point]:
        """The package's own x and y directions, in board coordinates.

        KiCad turns a footprint counter-clockwise as drawn with Y pointing down, so
        this is the same transform the obstacle reader uses on a rotated part's
        pads. Deriving the escape directions from the package's own axes rather
        than from the board's is what makes a rotated QFN escape sideways instead of
        diagonally.
        """
        theta = math.radians(self.rotation)
        cos, sin = math.cos(theta), math.sin(theta)
        return ((cos, -sin), (sin, cos))


def _package_frames(board: SNode) -> dict[str, _Frame]:
    frames: dict[str, _Frame] = {}
    for footprint in board.children("footprint"):
        at = footprint.child("at")
        reference = next(
            (p.value(1) for p in footprint.children("property") if p.value(0) == "Reference"),
            None,
        )
        if at is None or reference is None:
            continue
        frames[reference] = _Frame(
            refdes=reference,
            origin=(float(at.value(0) or 0), float(at.value(1) or 0)),
            rotation=float(at.value(2) or 0),
            layer=footprint.get("layer") or "F.Cu",
        )
    return frames


def _fan_out_package(
    refdes: str,
    intent: Fanout,
    frame: _Frame,
    environment: RoutingEnvironment,
    netlist: Netlist,
    stack: RoutingStack,
    result: FanoutResult,
    report: Report,
    congestion: float,
) -> None:
    pads = sorted(
        key
        for key in environment.pad_centres
        if key.partition("#")[0].partition(".")[0] == refdes
    )
    # A pad the design never connects carries a synthetic `unconnected-(...)` net
    # so the board agrees with the schematic. It is still a pad and still an
    # obstacle, but there is nothing to escape *to*: an unused pin gets no fanout,
    # which is both the brief's rule and the one a layout engineer would follow.
    connected = [
        p for p in pads if (environment.pad_nets.get(p) or "") in netlist.nets
    ]
    if not connected:
        report.warning(
            "fanout-nothing-to-escape",
            f"{refdes} has no connected pads, so there is nothing to fan out",
            path=netlist.mech_path("fanout", refdes),
            hint="unused pads get no fanout, which is the correct answer for them",
        )
        return

    layers = _escape_layers(intent, frame, stack, netlist, refdes, report)
    if not layers:
        return

    centres = {p: environment.pad_centres[p] for p in connected}
    style = intent.style if intent.style != "auto" else choose_style(centres)
    centre = _mean(list(centres.values()))
    pitch = _pitch(list(centres.values()))
    hull = _hull(list(centres.values()))

    if style == "via_in_pad":
        report.warning(
            "fanout-via-in-pad",
            f"{refdes} is fanned out with vias in its pads, which needs filled and "
            "capped vias and costs real money at the fabricator",
            path=netlist.mech_path("fanout", refdes, "style"),
            hint="via-in-pad is never chosen for you; `style: auto` picks dog-bone "
            "or perimeter escapes, which any fabricator will build",
        )

    taken: list[Obstacle] = []
    for pad in connected:
        net = environment.pad_nets[pad]
        rules = rules_for(netlist, net, congestion)
        diameter = intent.via.diameter if intent.via else rules.via_diameter
        drill = intent.via.drill if intent.via else rules.via_drill
        count = via_count(class_for(netlist, net).trace_width_mm, diameter)

        sites = _sites_for(
            pad, centres[pad], style, frame, centre, pitch, environment,
            diameter, rules.clearance, count, hull,
        )
        chosen = _first_clear(
            sites, environment, taken, net, diameter, rules.clearance,
            rules.track_width, layers, frame, centres[pad],
        )
        if chosen is None:
            report.warning(
                "fanout-pad-not-escaped",
                f"{refdes} pad {pad.partition('.')[2]} ({net}) has no clear place "
                "for an escape via, so it is left to the router",
                path=netlist.mech_path("fanout", refdes),
                net=net,
                hint="the escape pattern respects the courtyard, the board outline "
                "and every cutout; a pad with nowhere to go usually means the part "
                "is too close to an edge or a hole",
            )
            continue

        connection = _connection_for(
            pad, net, centres[pad], chosen, frame, layers, diameter, drill, rules, stack
        )
        result.connections.append(connection)
        escape = f"{pad}{ESCAPE_SUFFIX}"
        result.terminals[escape] = (net, chosen[-1], frozenset(layers))
        result.replaced.add(pad)

        obstacles = _obstacles_for(connection, escape, stack, diameter)
        taken.extend(obstacles)
        result.obstacles.extend(obstacles)


def via_count(track_width: float, via_diameter: float) -> int:
    """How many vias a net's escape needs.

    One via carries roughly what a track as wide as its barrel carries, so a net
    whose class asks for a fat track asks for proportionally more vias -- which is
    why a ground pad on a 1 mm rail gets a small column of them and a signal pad
    gets one. The model is deliberately crude and deliberately stated: it is a
    current-density rule of thumb, not a thermal simulation.
    """
    if via_diameter <= 0:
        return 1
    return max(1, min(4, math.ceil(track_width / via_diameter)))


def _escape_layers(
    intent: Fanout,
    frame: _Frame,
    stack: RoutingStack,
    netlist: Netlist,
    refdes: str,
    report: Report,
) -> tuple[str, ...]:
    """Which layers the escape vias reach, front to back, defaulted and checked.

    The barrel spans from the package's own layer to the *deepest* of them, so every
    layer named is inside the span and the router may pick the signal up on any of
    them. Naming one layer is the ordinary case; naming two is how a design says
    "either of these, whichever the corridor wants".
    """
    if intent.escape_layers:
        unknown = [n for n in intent.escape_layers if n not in stack.copper]
        if unknown:
            report.error(
                "fanout-unknown-layer",
                f"{refdes} asks to escape to {', '.join(unknown)}, which this "
                f"{len(stack.copper)}-layer board does not have",
                path=netlist.mech_path("fanout", refdes, "escape_layers"),
                hint=f"layers available: {', '.join(stack.copper)}",
            )
        # Ordered front to back whatever order the source listed them in, because
        # the barrel has to reach the deepest of them.
        chosen = tuple(
            n for n in stack.signal if n in intent.escape_layers
        )
    else:
        # Everything the package is not already on. An escape that stays on the
        # package's own layer has not escaped anything.
        chosen = tuple(n for n in stack.signal if n != frame.layer)
    if not chosen:
        report.warning(
            "fanout-nowhere-to-escape",
            f"{refdes} has no signal layer to escape to",
            path=netlist.mech_path("fanout", refdes),
            hint="a fanout needs at least one signal layer other than the one the "
            "package sits on; add copper layers, or free one from a plane",
        )
    return chosen


def choose_style(centres: dict[str, Point]) -> str:
    """Dog-bone for an area array, short perimeter stubs for everything else.

    The test is whether the package has pads that cannot be reached from outside
    without crossing another pad -- which is what an area array *is*. A QFN has
    exactly one interior pad, its thermal one, and one is not an array; a BGA has
    dozens. So "more than one interior pad" is the whole rule, and it is a fact
    about the geometry rather than a name somebody typed.
    """
    points = list(centres.values())
    if len(points) < 4:
        return "perimeter"
    hull = _hull(points)
    interior = [p for p in points if not _near_hull(p, hull)]
    return "dogbone" if len(interior) > 1 else "perimeter"


def escape_direction(
    local: Point, style: str
) -> Point:
    """Which way a pad escapes, in the package's own frame.

    A perimeter pad leaves along the edge it sits on -- the axis it is furthest out
    on. An area-array ball leaves along the diagonal of its quadrant, which is the
    gap between it and its three neighbours, and is the whole reason a dog-bone is
    shaped like one.
    """
    x, y = local
    if style == "dogbone":
        sx = 1.0 if x >= 0 else -1.0
        sy = 1.0 if y >= 0 else -1.0
        return (sx * math.sqrt(0.5), sy * math.sqrt(0.5))
    if abs(x) >= abs(y):
        return (1.0 if x >= 0 else -1.0, 0.0)
    return (0.0, 1.0 if y >= 0 else -1.0)


def _sites_for(
    pad: str,
    centre: Point,
    style: str,
    frame: _Frame,
    package_centre: Point,
    pitch: float,
    environment: RoutingEnvironment,
    diameter: float,
    clearance: float,
    count: int,
    hull: tuple[Point, ...],
) -> list[list[Point]]:
    """Candidate via columns for one pad, nearest first.

    Each candidate is the whole column: ``count`` via positions in a line along the
    escape direction, which the stub runs through. Staggering alternate pads into a
    second row is what keeps the vias off the pad pitch -- at 0.5 mm pitch a row of
    0.45 mm vias would be one continuous piece of copper.
    """
    if style == "via_in_pad" or (style != "dogbone" and not _near_hull(centre, hull)):
        # An interior pad on a perimeter package is a thermal pad: there is no way
        # out from under it, and the standard answer is a via straight through it.
        # That is not the expensive "via in pad" -- what costs money is a filled and
        # capped via under a solder ball, and an exposed pad's thermal vias are
        # neither. Anything the source asks for explicitly lands here too.
        return [[centre]]

    axes = frame.axes()
    delta = (centre[0] - package_centre[0], centre[1] - package_centre[1])
    local = (
        delta[0] * axes[0][0] + delta[1] * axes[0][1],
        delta[0] * axes[1][0] + delta[1] * axes[1][1],
    )
    unit_local = escape_direction(local, style)
    direction = (
        unit_local[0] * axes[0][0] + unit_local[1] * axes[1][0],
        unit_local[0] * axes[0][1] + unit_local[1] * axes[1][1],
    )

    reach = _reach(environment.obstacles.get(pad), centre, direction)
    step = diameter + clearance
    # Which row this pad's escape goes in is decided by *where the pad is*, not by
    # where it comes in a list: neighbours along an edge alternate, so the vias land
    # in two rows rather than in one at the package's own pitch, where a row of
    # 0.45 mm vias on a 0.5 mm pitch would be one continuous piece of copper.
    along = local[0] * unit_local[1] - local[1] * unit_local[0]
    stagger = (round(along / pitch) % 2) * max(step, pitch * 0.75)
    base = reach + clearance + diameter / 2

    sites: list[list[Point]] = []
    for row in range(MAX_ESCAPE_ROWS):
        start = base + stagger + row * step
        sites.append(
            [
                (
                    centre[0] + direction[0] * (start + i * step),
                    centre[1] + direction[1] * (start + i * step),
                )
                for i in range(count)
            ]
        )
    return sites


def _first_clear(
    sites: list[list[Point]],
    environment: RoutingEnvironment,
    taken: list[Obstacle],
    net: str,
    diameter: float,
    clearance: float,
    track_width: float,
    layers: tuple[str, ...],
    frame: _Frame,
    pad_centre: Point,
) -> list[Point] | None:
    """The first candidate that clears everything -- the vias *and* the stub.

    Checking the via positions alone is not enough, and the case that proves it is
    the pad in the middle of a package: a via somewhere outside is perfectly clear,
    and the stub reaching it would run straight across four of the part's own pins.
    So the whole escape is checked as the copper it is.
    """
    from shapely.geometry import Polygon as ShapelyPolygon

    board = ShapelyPolygon(environment.outline, environment.cutouts)
    keep = environment.edge_clearance + diameter / 2
    via_room = diameter / 2 + clearance
    track_room = track_width / 2 + clearance
    touched = (frame.layer, *layers)

    blockers = [
        ShapelyPolygon(obstacle.polygon)
        for obstacle in (*environment.obstacles.values(), *taken)
        if obstacle.kind != "body"
        and obstacle.net != net
        and len(obstacle.polygon) >= 3
        and any(obstacle.blocks(_NO_NET, layer) for layer in touched)
    ]
    # Copper of this same net already laid by an earlier escape. It may touch, but
    # two barrels still have to be two holes a fabricator can drill.
    holes = [
        ShapelyPolygon(obstacle.polygon)
        for obstacle in taken
        if obstacle.net == net
        and obstacle.kind in ("via", "pad")
        and len(obstacle.polygon) >= 3
    ]

    for column in sites:
        if _column_clear(
            column, pad_centre, board, keep, via_room, track_room, blockers, holes
        ):
            return column
    return None


#: A net name no design can have, for asking an obstacle "would you block anybody".
_NO_NET = frozenset({"\x00"})


def _column_clear(
    column: list[Point],
    pad_centre: Point,
    board: object,
    keep: float,
    via_room: float,
    track_room: float,
    blockers: list[object],
    holes: list[object],
) -> bool:
    from shapely.geometry import LineString
    from shapely.geometry import Point as ShapelyPoint

    for point in column:
        at = ShapelyPoint(point)
        if not board.covers(at) or board.boundary.distance(at) < keep:  # type: ignore[attr-defined]
            return False
        if any(shape.distance(at) < via_room for shape in blockers):  # type: ignore[attr-defined]
            return False
        if any(shape.distance(at) < via_room for shape in holes):  # type: ignore[attr-defined]
            return False

    stub = [pad_centre, *column]
    if math.dist(stub[0], stub[-1]) < 1e-9:
        return True
    line = LineString(stub)
    if not board.covers(line):  # type: ignore[attr-defined]
        return False
    return not any(shape.distance(line) < track_room for shape in blockers)  # type: ignore[attr-defined]


def _distance_to(point: Point, polygon: tuple[Point, ...]) -> float:
    from shapely.geometry import Point as ShapelyPoint
    from shapely.geometry import Polygon as ShapelyPolygon

    if len(polygon) < 3:
        return math.inf
    shape = ShapelyPolygon(polygon)
    at = ShapelyPoint(point)
    return 0.0 if shape.covers(at) else float(shape.distance(at))


def _connection_for(
    pad: str,
    net: str,
    centre: Point,
    column: list[Point],
    frame: _Frame,
    layers: tuple[str, ...],
    diameter: float,
    drill: float,
    rules: object,
    stack: RoutingStack,
) -> RoutedConnection:
    """One pad's escape, as ordinary copper: a stub on the package's layer, then vias."""
    escape = f"{pad}{ESCAPE_SUFFIX}"
    # The deepest layer named, so the barrel passes every one of them.
    target = layers[-1]
    connection = RoutedConnection(net=net, start=pad, end=escape)
    if math.dist(centre, column[-1]) > 1e-9:
        connection.legs.append(
            StretchResult(
                net=net,
                layer=frame.layer,
                points=[centre, *column],
                width=getattr(rules, "track_width", 0.25),
                start=pad,
                end=escape,
            )
        )
    for index, point in enumerate(column):
        connection.vias.append(
            Via(
                net=net,
                point=point,
                from_layer=frame.layer,
                to_layer=target,
                diameter=diameter,
                drill=drill,
                kind=stack.via_type(frame.layer, target) or "through",
                name=f"fanout:{pad}#{index}",
            )
        )
        connection.barrel_length += stack.barrel_length(frame.layer, target)
    return connection


def _obstacles_for(
    connection: RoutedConnection, escape: str, stack: RoutingStack, diameter: float
) -> list[Obstacle]:
    """The fanout's copper, as things every later route must go around.

    The outermost via is registered as a *pad* under the escape's name, because that
    is what it now is: the thing the router lands on. The rest is track and via
    copper, which blocks other nets and never its own.
    """
    obstacles: list[Obstacle] = []
    for leg in connection.legs:
        obstacles.extend(
            track_obstacles(leg, f"fanout:{leg.net}/{leg.start}", leg.width / 2)
        )
    for index, via in enumerate(connection.vias):
        last = index == len(connection.vias) - 1
        if last:
            obstacles.append(
                Obstacle(
                    name=escape,
                    polygon=_ring(via.point, via.radius),
                    net=via.net,
                    # A barrel is a hole through the board, so it is in the way on
                    # every layer -- which is what an empty layer set means here.
                    layers=frozenset(),
                    kind="pad",
                )
            )
            continue
        obstacles.extend(
            via_obstacles(via, stack, f"fanout:{via.net}/{via.name}#{index}")
        )
    return obstacles


def _ring(centre: Point, radius: float, segments: int = 8) -> tuple[Point, ...]:
    scale = radius / math.cos(math.pi / segments)
    return tuple(
        (
            centre[0] + scale * math.cos(math.tau * i / segments),
            centre[1] + scale * math.sin(math.tau * i / segments),
        )
        for i in range(segments)
    )


# ---------------------------------------------------------------------------
# small geometry
# ---------------------------------------------------------------------------


def _mean(points: list[Point]) -> Point:
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


def _pitch(points: list[Point]) -> float:
    """The package's pad pitch: the shortest distance between two pad centres."""
    best = math.inf
    for index, first in enumerate(points):
        for second in points[index + 1 :]:
            best = min(best, math.dist(first, second))
    return best if math.isfinite(best) and best > 0 else 0.5


def _reach(obstacle: Obstacle | None, centre: Point, direction: Point) -> float:
    """How far the pad's own copper extends from its centre, along one direction."""
    if obstacle is None or len(obstacle.polygon) < 3:
        return 0.0
    return max(
        (x - centre[0]) * direction[0] + (y - centre[1]) * direction[1]
        for x, y in obstacle.polygon
    )


def _hull(points: list[Point]) -> tuple[Point, ...]:
    from aipcb.route.obstacles import convex_hull

    return convex_hull(tuple(points))


def _near_hull(point: Point, hull: tuple[Point, ...]) -> bool:
    from shapely.geometry import Point as ShapelyPoint
    from shapely.geometry import Polygon as ShapelyPolygon

    if len(hull) < 3:
        return True
    return float(ShapelyPolygon(hull).exterior.distance(ShapelyPoint(point))) <= _HULL_TOLERANCE


# ---------------------------------------------------------------------------
# tidying up after the router
# ---------------------------------------------------------------------------


def settle_escapes(
    connections: list[RoutedConnection], escapes: set[str]
) -> tuple[int, int]:
    """Take back the escapes the router turned out not to need.

    The generator has to propose an escape for every pad *before* anything is
    routed, because it cannot know which of them the router will take up. Usually it
    takes them all -- that is what the pattern is for. Occasionally a pad sits at the
    edge of the package nearest its destination and the router reaches the escape
    point on the package's own layer without ever going to the back. The via then
    joins copper to nothing: KiCad calls it a dangling via and a fabricator drills it
    for no reason.

    So the generator tidies up after itself. Two passes, both of which only ever
    remove copper that is doing nothing:

    *A leg of zero length is not copper.* When the router changes layer exactly at
    the escape point, the leg on the far side has the same start and end, and the via
    that served it serves nothing.

    *An escape nobody used is not an escape.* When no route on an escape layer
    touches the barrel, the via comes out and the stub stays -- the pad is still
    connected, by the copper the router actually laid.

    Returns how many legs and how many vias were taken back.
    """
    dropped_legs = _drop_degenerate_legs(connections, escapes)
    dropped_vias = _drop_unused_escapes(connections, escapes)
    return dropped_legs, dropped_vias


#: A leg shorter than this is a rounding artefact rather than a piece of track.
_DEGENERATE = 1e-6


def _drop_degenerate_legs(
    connections: list[RoutedConnection], escapes: set[str]
) -> int:
    """Remove a route's last leg when it is a hair long, and the via that served it.

    Only at an escape, and only the *last* leg, because that is the one case where
    dropping a via cannot disconnect anything: the escape terminal is itself a via,
    present on the package's layer and on the escape layer both, so a route that
    stops one barrel short of it is still on it.

    "A hair" is one via diameter. Below that the leg is shorter than the hole it
    ends at, and what the router has drawn is two barrels a fraction of a millimetre
    apart doing the job of one -- which KiCad reports as overlapping holes.
    """
    dropped = 0
    for connection in connections:
        while (
            connection.end in escapes
            and len(connection.legs) > 1
            and connection.vias
            and _is_a_hair(connection.legs[-1], connection.vias[-1])
        ):
            connection.legs.pop()
            connection.vias.pop()
            dropped += 1
    return dropped


def _is_a_hair(leg: StretchResult, via: Via) -> bool:
    return len(leg.points) < 2 or leg.length <= max(via.diameter, _DEGENERATE)


def _drop_unused_escapes(
    connections: list[RoutedConnection], escapes: set[str]
) -> int:
    """Remove an escape via that no route on the far layer ever reached."""
    laid: dict[tuple[str, str], list[tuple[float, tuple[Point, Point]]]] = {}
    for connection in connections:
        for leg in connection.legs:
            for segment in leg.segments:
                laid.setdefault((leg.net, leg.layer), []).append((leg.width / 2, segment))

    dropped = 0
    for connection in connections:
        # Only the generator's own stubs. A route that *ends* at an escape has its
        # own vias joining two real legs, and taking one of those out would break
        # the route rather than tidy it.
        if connection.end not in escapes or connection.end != (
            f"{connection.start}{ESCAPE_SUFFIX}"
        ):
            continue
        keep = []
        for via in connection.vias:
            if _barrel_reaches_copper(via, laid):
                keep.append(via)
            else:
                dropped += 1
        connection.vias = keep
    return dropped


def _barrel_reaches_copper(
    via: Via,
    laid: dict[tuple[str, str], list[tuple[float, tuple[Point, Point]]]],
) -> bool:
    """Whether some route other than the stub itself lands on the far side of a via.

    Measured against the barrel's annulus rather than against an exact endpoint: a
    route that lands on a via and is then trimmed back onto its own net's existing
    copper no longer ends at the point it was aimed at, and is still connected to it.
    """
    for half_width, segment in laid.get((via.net, via.to_layer), ()):
        if _segment_distance(segment, via.point) <= via.radius + half_width:
            return True
    return False


def _segment_distance(segment: tuple[Point, Point], point: Point) -> float:
    (ax, ay), (bx, by) = segment
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-18:
        return math.dist((ax, ay), point)
    t = ((point[0] - ax) * dx + (point[1] - ay) * dy) / length_sq
    t = min(1.0, max(0.0, t))
    return math.dist((ax + dx * t, ay + dy * t), point)
