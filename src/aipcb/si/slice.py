"""Cutting one pair out of a routed board, and giving it somewhere to be fed from.

A slice is a real ``.kicad_pcb``: same stackup, same nets, same pours, same design
rules, with a board outline drawn tightly around one differential pair and every
piece of copper that does not reach inside removed. It is exported by ``kicad-cli``
like any other board, which is what keeps ADR 0001 true -- aipcb never writes a
Gerber itself, not even for a solver.

Three things are added that the real board does not have, and each is a deliberate
approximation worth stating out loud:

* **The coupling capacitors become copper.** A pair split by ``role: ac_coupling``
  parts is one conductor at signal frequencies; the bridge closes it. See
  :mod:`aipcb.si.pairs`.
* **Each end grows a straight, axis-aligned launch.** gerber2ems ports are
  microstrip ports and openEMS only builds them along an axis, so a pair that ends
  at a pad on a diagonal has nowhere to be fed from. The launch is the same width
  as the trace, runs outward from the last segment, and the port occupies exactly
  it, so openEMS de-embeds to the launch's inner end rather than to the board edge.
  Measured on ``examples/mcu-4layer``: 20.7 mm of conductor came back at 126 ps of
  group delay against the 126 ps that 6.1 ps/mm predicts, where two undeembedded
  1.5 mm launches would have added 18 ps. What the launch *does* do is replace the
  pad and its antipad with a uniform line, so this measures the run, not the
  discontinuity where the run ends.
* **Footprints are dropped.** No pads, no antipads, no thermal reliefs. The
  interior of the run -- which is what an impedance target is about -- is
  unaffected; the last few tenths of a millimetre at each end are not modelled.

Everything is a pure function of the routed board and the source, so a slice is a
reproducible artifact: same inputs, byte-identical output.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise

from aipcb.ids import element_uuid
from aipcb.kicad.sexpr import SNode, num, quoted, sym
from aipcb.model.layout import Stackup, copper_layer_names
from aipcb.model.simulation import ResolvedSimulation
from aipcb.netlist import Netlist
from aipcb.si.pairs import LogicalPair

__all__ = ["Port", "Slice", "SliceError", "build_slice", "port_footprint"]

Point = tuple[float, float]

#: How far inside the slice outline a clipped track may reach. Half of the widest
#: track plus a little, so no copper crosses the edge -- gerber2ems frames every
#: layer on the Edge.Cuts Gerber and copper outside it would shift the frame.
_CLIP_INSET_MM = 0.5

#: Stroke width of the slice outline, matching what ``compile/board.py`` draws. It
#: matters: gerber2ems frames every layer on the Edge.Cuts Gerber and then crops half
#: this width off each side to land back on the centreline, so a different width here
#: would offset every port from its copper.
_EDGE_WIDTH_MM = 0.1

#: Coordinates are rounded here before points are compared for identity. The router
#: emits six decimals; a nanometre of slop is not a different point.
_EPS = 1e-4


class SliceError(ValueError):
    """A pair cannot be sliced, with the reason a report can print."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True, slots=True)
class Track:
    """One straight piece of copper on the routed board."""

    start: Point
    end: Point
    width: float
    layer: str
    net: int

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)


@dataclass(frozen=True, slots=True)
class Port:
    """One simulation port: where it is, which way it feeds, and against what."""

    number: int
    """1-based, and the number that becomes ``SP<n>`` in the placement file."""
    at: Point
    """Board coordinates of the port's outer face."""
    rotation: float
    """KiCad footprint angle. gerber2ems reads it as the feed direction."""
    width_mm: float
    length_mm: float
    layer_index: int
    """Index into the copper stack, front to back, as gerber2ems counts metals."""
    plane_index: int
    impedance_ohm: float
    net: str
    layer: str

    def to_dict(self) -> dict[str, object]:
        return {
            "port": self.number,
            "net": self.net,
            "layer": self.layer,
            "at_mm": [round(self.at[0], 4), round(self.at[1], 4)],
            "rotation_deg": self.rotation,
            "width_mm": round(self.width_mm, 4),
            "length_mm": round(self.length_mm, 4),
            "impedance_ohm": self.impedance_ohm,
        }


@dataclass(slots=True)
class Slice:
    """A pair, cut out and ready to export."""

    pair: LogicalPair
    board: SNode
    rect: tuple[float, float, float, float]
    """``(min_x, min_y, max_x, max_y)`` in KiCad board coordinates."""
    origin: Point
    """The slice's bottom-left corner: what every exported coordinate is measured from."""
    ports: tuple[Port, ...]
    metals: tuple[str, ...]
    conductor_length_mm: float
    launch_mm: float
    bridged: tuple[str, ...]
    notes: list[str] = field(default_factory=list)

    @property
    def size_mm(self) -> tuple[float, float]:
        return (
            round(self.rect[2] - self.rect[0], 4),
            round(self.rect[3] - self.rect[1], 4),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **self.pair.to_dict(),
            "slice_mm": list(self.size_mm),
            "origin_mm": [round(self.origin[0], 4), round(self.origin[1], 4)],
            "conductor_length_mm": round(self.conductor_length_mm, 4),
            "launch_mm": round(self.launch_mm, 4),
            "bridged_by": list(self.bridged),
            "ports": [p.to_dict() for p in self.ports],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# reading the routed board
# ---------------------------------------------------------------------------


def _net_numbers(board: SNode) -> dict[str, int]:
    out: dict[str, int] = {}
    for node in board.children("net"):
        number = node.value(0)
        name = node.value(1)
        if number is not None and name is not None:
            out[name] = int(number)
    return out


def _point(node: SNode | None) -> Point:
    if node is None:
        return (0.0, 0.0)
    return (float(node.value(0) or 0.0), float(node.value(1) or 0.0))


def _tracks(board: SNode) -> list[Track]:
    out: list[Track] = []
    for seg in board.children("segment"):
        width = seg.child("width")
        layer = seg.child("layer")
        net = seg.child("net")
        out.append(
            Track(
                start=_point(seg.child("start")),
                end=_point(seg.child("end")),
                width=float(width.value() or 0.0) if width else 0.0,
                layer=str(layer.value() or "") if layer else "",
                net=int(net.value() or 0) if net else 0,
            )
        )
    return out


def _vias(board: SNode) -> list[tuple[Point, float]]:
    """Every via as a centre and an outer radius, for collision tests."""
    out: list[tuple[Point, float]] = []
    for via in board.children("via"):
        size = via.child("size")
        radius = float(size.value() or 0.4) / 2 if size else 0.2
        out.append((_point(via.child("at")), radius))
    return out


def _key(point: Point) -> tuple[float, float]:
    return (round(point[0] / _EPS) * _EPS, round(point[1] / _EPS) * _EPS)


def _endpoints(tracks: Iterable[Track]) -> list[Point]:
    """Points where exactly one track ends: the two ends of an open conductor.

    Degrees are counted in two dimensions on purpose. A via joins the segment above
    it to the segment below it at the same ``(x, y)``, so a pair that changes layer
    stays one conductor with two ends rather than four.
    """
    degree: dict[tuple[float, float], int] = {}
    where: dict[tuple[float, float], Point] = {}
    for track in tracks:
        for point in (track.start, track.end):
            key = _key(point)
            degree[key] = degree.get(key, 0) + 1
            where[key] = point
    return [where[k] for k, d in sorted(degree.items()) if d == 1]


def _nearest_bridge(groups: list[list[Track]]) -> list[tuple[Point, Point, float, str]]:
    """Shortest hop joining each pair of sub-conductors, i.e. where the cap sits.

    Deliberately geometric rather than read off the capacitor's pads: the router
    started each net *at* a pad centre, so the two nearest dangling ends are the two
    pads, and taking them avoids a second implementation of KiCad's footprint
    transform -- which is exactly the kind of code that is wrong in the rotated case
    and never noticed.
    """
    out: list[tuple[Point, Point, float, str]] = []
    joined = groups[0]
    for other in groups[1:]:
        best: tuple[float, Point, Point, float, str] | None = None
        for a in _endpoints(joined):
            for b in _endpoints(other):
                distance = math.dist(a, b)
                layer = next(
                    (t.layer for t in other if _key(t.start) == _key(b) or _key(t.end) == _key(b)),
                    "F.Cu",
                )
                width = next(
                    (t.width for t in other if _key(t.start) == _key(b) or _key(t.end) == _key(b)),
                    0.2,
                )
                if best is None or distance < best[0]:
                    best = (distance, a, b, width, layer)
        if best is not None:
            out.append((best[1], best[2], best[3], best[4]))
            joined = [*joined, *other, Track(best[1], best[2], best[3], best[4], 0)]
    return out


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def _axis(vector: Point) -> Point:
    """The nearest unit axis direction, ties going to x. Never the zero vector."""
    if abs(vector[0]) >= abs(vector[1]):
        return (1.0 if vector[0] >= 0 else -1.0, 0.0)
    return (0.0, 1.0 if vector[1] >= 0 else -1.0)


def _outward(tracks: list[Track], point: Point) -> Point:
    """Which way the conductor is heading as it leaves ``point``."""
    key = _key(point)
    for track in tracks:
        if _key(track.start) == key:
            return (track.start[0] - track.end[0], track.start[1] - track.end[1])
        if _key(track.end) == key:
            return (track.end[0] - track.start[0], track.end[1] - track.start[1])
    return (1.0, 0.0)


#: How much board is cleared beside a launch. A typical class clearance on these
#: designs, so what is removed is what would have been a design-rule violation had
#: the launch been a real track.
_LAUNCH_CLEARANCE_MM = 0.15


def _launch_axis(pair_tracks: list[Track], p_point: Point, n_point: Point) -> Point:
    """Which way both halves of one end are launched, as a unit axis vector.

    The rule is *perpendicular to the pair's own separation*, and it is not a
    refinement -- it is the difference between a working slice and a short circuit.
    Take the average of the two traces' outgoing directions instead, and on a pair
    that leaves its pads broadside (`examples/pcie-sata`'s PCIe lanes leave the
    controller side by side, 0.5 mm apart in x, both heading down the board) the
    average can come out along x. Both launches then run along the same line,
    overlap, and weld P to N. Measured on that board before the rule existed:
    |Sdd21| of 7.26, a return loss of +40 dB, and an impedance of 437 ohm against an
    85 ohm target.

    So the axis is the one the two endpoints do *not* separate along, and only the
    sign comes from where the conductor is heading.
    """
    separation = (n_point[0] - p_point[0], n_point[1] - p_point[1])
    outward = _add(_outward(pair_tracks, p_point), _outward(pair_tracks, n_point))
    if abs(separation[0]) >= abs(separation[1]):
        component = outward[1]
        if abs(component) < 1e-9:
            component = p_point[1] - _centroid(pair_tracks)[1]
        return (0.0, 1.0 if component >= 0 else -1.0)
    component = outward[0]
    if abs(component) < 1e-9:
        component = p_point[0] - _centroid(pair_tracks)[0]
    return (1.0 if component >= 0 else -1.0, 0.0)


def _centroid(tracks: list[Track]) -> Point:
    xs = [c for t in tracks for c in (t.start[0], t.end[0])]
    ys = [c for t in tracks for c in (t.start[1], t.end[1])]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def launch_corridor(stubs: list[Track]) -> object:
    """The region the launches occupy, plus a clearance, as one shapely geometry.

    A launch runs *outward* from where the pair ends -- which is where a pad was,
    and beyond the pad is the component. Drop the footprints, as a slice does, and
    that region is not empty: it is full of the neighbouring pins' fanout, and on
    `examples/pcie-sata` every one of the twenty-two pair ends has a ground track
    crossing within a millimetre of it. A launch laid straight through that is
    welded to ground, and openEMS will report the short with a clean exit code.

    So the corridor is cleared. That is a deliberate modification of the same kind
    as dropping the footprints and bridging the coupling capacitors: the port stands
    in for the pad and for everything past it -- the driver, the connector, the
    cable -- and the neighbouring pins' fanout is not the transmission line under
    test. The slice reports how much copper it removed, so the modification is
    visible rather than assumed.
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    return unary_union(
        [
            LineString([t.start, t.end]).buffer(
                t.width / 2 + _LAUNCH_CLEARANCE_MM, quad_segs=8
            )
            for t in stubs
            if t.length > _EPS
        ]
    )


def _rotation_for(direction: Point) -> float:
    """The KiCad footprint angle that makes gerber2ems feed *along* ``direction``.

    gerber2ems maps a port's rotation to a feed direction through
    ``dir_map = {0: "y", 90: "x", 180: "y", 270: "x"}`` and then builds the port body
    from the port position towards ``(-length*sin, +length*cos)`` -- in the placement
    file's frame, where Y points *up* the board. KiCad's board frame points Y down,
    so the y component flips on the way in. Writing the four cases out beats deriving
    them: this mapping is exactly the kind of thing that is off by 180 degrees and
    still produces a plausible-looking impedance.
    """
    axis = _axis(direction)
    if axis == (0.0, -1.0):  # board -y == placement-file +y
        return 0.0
    if axis == (-1.0, 0.0):
        return 90.0
    if axis == (0.0, 1.0):
        return 180.0
    return 270.0


def _clip(track: Track, rect: tuple[float, float, float, float]) -> Track | None:
    """Liang-Barsky: the part of a track inside ``rect``, or ``None``."""
    x0, y0 = track.start
    x1, y1 = track.end
    dx, dy = x1 - x0, y1 - y0
    low, high = 0.0, 1.0
    for p, q in (
        (-dx, x0 - rect[0]),
        (dx, rect[2] - x0),
        (-dy, y0 - rect[1]),
        (dy, rect[3] - y0),
    ):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            low = max(low, t)
        else:
            high = min(high, t)
        if low > high:
            return None
    if high - low < 1e-9:
        return None
    start = (x0 + low * dx, y0 + low * dy)
    end = (x0 + high * dx, y0 + high * dy)
    if math.dist(start, end) < _EPS:
        return None
    return Track(start, end, track.width, track.layer, track.net)


# ---------------------------------------------------------------------------
# the slice itself
# ---------------------------------------------------------------------------


def build_slice(
    board: SNode,
    netlist: Netlist,
    pair: LogicalPair,
    settings: ResolvedSimulation,
    *,
    port_impedance_ohm: float,
) -> Slice:
    """Cut ``pair`` out of ``board``. Raises :class:`SliceError` when it cannot."""
    numbers = _net_numbers(board)
    stackup = netlist.layout.stackup if netlist.layout else Stackup()
    metals = copper_layer_names(stackup.copper_layers)

    tracks = _tracks(board)
    sides: dict[str, list[Track]] = {}
    bridges: list[Track] = []
    notes: list[str] = []

    for side, nets in (("p", pair.positive), ("n", pair.negative)):
        groups = []
        for net in nets:
            code = numbers.get(net)
            own = [t for t in tracks if code is not None and t.net == code]
            if own:
                groups.append(own)
        if not groups:
            raise SliceError(
                "si-pair-unrouted",
                f"{pair.name} has no copper on the board, so there is nothing to "
                "simulate",
                hint="only routed pairs can be simulated; route the board first, or "
                "finish the pair by hand",
            )
        if len(groups) < len(nets):
            missing = len(nets) - len(groups)
            notes.append(f"{missing} net(s) on the {side} side carry no copper")
        for a, b, width, layer in _nearest_bridge(groups):
            bridges.append(Track(a, b, width, layer, 0))
        sides[side] = [t for group in groups for t in group]

    ends: dict[str, list[Point]] = {}
    for side in ("p", "n"):
        conductor = [*sides[side], *[b for b in bridges if _touches(b, sides[side])]]
        found = _endpoints(conductor)
        if len(found) != 2:
            if len(found) < 2:
                raise SliceError(
                    "si-pair-not-open",
                    f"{pair.name}'s {side} conductor has {len(found)} free ends, not "
                    "two, so a port cannot be placed at each end",
                    hint="a shorted or branched conductor is not a transmission line; "
                    "check for a stub or a T",
                )
            found = _farthest_two(found)
            notes.append(
                f"the {side} conductor has more than two free ends; the two farthest "
                "apart were taken"
            )
        ends[side] = found

    # Pair the ends up: each P end goes with its nearest N end. Sorted by the P end's
    # coordinates so port numbering is a function of geometry, never of dict order.
    p_first, p_second = sorted(ends["p"])
    n_first = min(ends["n"], key=lambda q: math.dist(p_first, q))
    n_second = next(q for q in ends["n"] if q != n_first)

    all_pair = [*sides["p"], *sides["n"], *bridges]
    launch = settings.launch_mm
    ports: list[Port] = []
    stubs: list[Track] = []
    directions = {
        0: _launch_axis(all_pair, p_first, n_first),
        1: _launch_axis(all_pair, p_second, n_second),
    }
    for index, (side, point) in enumerate(
        (("p", p_first), ("p", p_second), ("n", n_first), ("n", n_second))
    ):
        end = index % 2 if index < 2 else (index - 2) % 2
        direction = directions[end]
        owner = _owner(all_pair, point)
        stop = (
            point[0] + direction[0] * launch,
            point[1] + direction[1] * launch,
        )
        stubs.append(Track(point, stop, owner.width, owner.layer, owner.net))
        layer_index = metals.index(owner.layer) if owner.layer in metals else 0
        reference = stackup.reference_below(owner.layer)
        plane_index = (
            metals.index(reference)
            if reference in metals
            else (len(metals) - 1 if layer_index == 0 else 0)
        )
        net_name = next(
            (n for n, c in numbers.items() if c == owner.net),
            pair.positive[0] if side == "p" else pair.negative[0],
        )
        ports.append(
            Port(
                number=index + 1,
                at=stop,
                rotation=_rotation_for((-direction[0], -direction[1])),
                width_mm=owner.width,
                length_mm=launch,
                layer_index=layer_index,
                plane_index=plane_index,
                impedance_ohm=port_impedance_ohm,
                net=net_name,
                layer=owner.layer,
            )
        )

    if len({p.plane_index for p in ports}) > 1 or len({p.layer_index for p in ports}) > 1:
        notes.append(
            "the pair changes layer, so its two ends are referenced to different "
            "planes; the impedance reported is the whole link, not either half"
        )

    rect = _bounds([*all_pair, *stubs], settings.margin_mm)
    origin = (rect[0], rect[3])
    conductor_length = sum(t.length for t in all_pair)

    node, cleared, dropped = _assemble(
        board, netlist, pair, rect, origin, ports, tracks, stubs, bridges, all_pair
    )
    if cleared > _EPS or dropped:
        notes.append(
            f"{cleared:.2f} mm of other nets' track and {dropped} via(s) were removed "
            "from the corridor the four launches occupy, because a launch runs out "
            "past the pad into the neighbouring pins' fanout"
        )
    return Slice(
        pair=pair,
        board=node,
        rect=rect,
        origin=origin,
        ports=tuple(ports),
        metals=metals,
        conductor_length_mm=conductor_length,
        launch_mm=launch,
        bridged=pair.bridged_by,
        notes=notes,
    )


def _add(a: Point, b: Point) -> Point:
    return (a[0] + b[0], a[1] + b[1])


def _touches(track: Track, others: list[Track]) -> bool:
    keys = {_key(t.start) for t in others} | {_key(t.end) for t in others}
    return _key(track.start) in keys or _key(track.end) in keys


def _owner(tracks: list[Track], point: Point) -> Track:
    key = _key(point)
    for track in tracks:
        if _key(track.start) == key or _key(track.end) == key:
            return track
    return tracks[0]


def _farthest_two(points: list[Point]) -> list[Point]:
    best = (points[0], points[1])
    far = -1.0
    for i, a in enumerate(points):
        for b in points[i + 1 :]:
            d = math.dist(a, b)
            if d > far:
                far, best = d, (a, b)
    return sorted(best)


def _bounds(tracks: list[Track], margin: float) -> tuple[float, float, float, float]:
    xs = [c for t in tracks for c in (t.start[0], t.end[0])]
    ys = [c for t in tracks for c in (t.start[1], t.end[1])]
    half = max((t.width for t in tracks), default=0.2) / 2
    return (
        round(min(xs) - half - margin, 4),
        round(min(ys) - half - margin, 4),
        round(max(xs) + half + margin, 4),
        round(max(ys) + half + margin, 4),
    )


# ---------------------------------------------------------------------------
# writing the slice board
# ---------------------------------------------------------------------------


#: Side of the keeper pad each port footprint carries, in millimetres. Small enough
#: to sit entirely inside the narrowest launch this tool will ever place, so it is
#: copper that changes no geometry.
_KEEPER_PAD_MM = 0.05


def port_footprint(port: Port, pair: str, net: int) -> SNode:
    """The footprint that puts a port in the placement file -- and keeps its net alive.

    gerber2ems finds ports by reading ``fab/*pos.csv`` for rows whose package is
    ``Simulation_Port`` and whose reference is ``SP<n>``, so the footprint's job is
    to be a placement row.

    It also carries one 50 um pad, which looks like a wart and is not. KiCad prunes
    every net that has no pad when it loads a board, and it prunes them *by
    renumbering*: the tracks keep their old codes and come back wearing another
    net's name. A slice has no footprints, so on the first working run every trace on
    it was labelled with a neighbour's net -- the geometry was right, the Gerber net
    attributes were wrong, the mesh generator refined nothing, and openEMS returned a
    confident short circuit at a zero exit code. The pad sits inside the launch, on
    the launch's own layer, so it adds no copper and it keeps the net.
    """
    uid = element_uuid("si-port", pair, str(port.number))
    node = SNode("footprint").add(quoted("aipcb:Simulation_Port"))
    node.add(SNode("layer").add(quoted("F.Cu")))
    node.add(SNode("uuid").add(quoted(uid)))
    node.add(SNode("at").add(num(port.at[0]), num(port.at[1]), num(port.rotation)))
    for index, (key, value, layer) in enumerate(
        (("Reference", f"SP{port.number}", "F.SilkS"), ("Value", "Simulation_Port", "F.Fab"))
    ):
        prop = SNode("property").add(quoted(key), quoted(value))
        prop.add(SNode("at").add(num(0), num(0), num(0)))
        prop.add(SNode("layer").add(quoted(layer)))
        prop.add(
            SNode("uuid").add(
                quoted(element_uuid("si-port", pair, str(port.number), str(index)))
            )
        )
        node.add(prop)
    node.add(SNode("attr").add(sym("smd"), sym("exclude_from_bom")))
    pad = SNode("pad").add(quoted("1"), sym("smd"), sym("rect"))
    pad.add(SNode("at").add(num(0), num(0)))
    pad.add(SNode("size").add(num(_KEEPER_PAD_MM), num(_KEEPER_PAD_MM)))
    pad.add(SNode("layers").add(quoted(port.layer)))
    pad.add(SNode("net").add(num(net), quoted(port.net)))
    pad.add(SNode("uuid").add(quoted(element_uuid("si-port", pair, str(port.number), "pad"))))
    node.add(pad)
    return node


def _edge(rect: tuple[float, float, float, float], pair: str) -> list[SNode]:
    corners = [
        (rect[0], rect[1]),
        (rect[2], rect[1]),
        (rect[2], rect[3]),
        (rect[0], rect[3]),
    ]
    out: list[SNode] = []
    for index, start in enumerate(corners):
        end = corners[(index + 1) % 4]
        node = SNode("gr_line")
        node.add(SNode("start").add(num(start[0]), num(start[1])))
        node.add(SNode("end").add(num(end[0]), num(end[1])))
        node.add(
            SNode("stroke").add(
                SNode("width").add(num(_EDGE_WIDTH_MM)), SNode("type").add(sym("default"))
            )
        )
        node.add(SNode("layer").add(quoted("Edge.Cuts")))
        node.add(SNode("uuid").add(quoted(element_uuid("si-edge", pair, str(index)))))
        out.append(node)
    return out


def _segment(track: Track, pair: str, index: int, net: int) -> SNode:
    node = SNode("segment")
    node.add(SNode("start").add(num(track.start[0]), num(track.start[1])))
    node.add(SNode("end").add(num(track.end[0]), num(track.end[1])))
    node.add(SNode("width").add(num(track.width)))
    node.add(SNode("layer").add(quoted(track.layer)))
    node.add(SNode("net").add(num(net)))
    node.add(SNode("uuid").add(quoted(element_uuid("si-track", pair, str(index)))))
    return node


def _assemble(
    board: SNode,
    netlist: Netlist,
    pair: LogicalPair,
    rect: tuple[float, float, float, float],
    origin: Point,
    ports: list[Port],
    tracks: list[Track],
    stubs: list[Track],
    bridges: list[Track],
    own: list[Track],
) -> tuple[SNode, float, int]:
    root = SNode("kicad_pcb")
    for name in ("version", "generator", "generator_version", "general", "paper"):
        node = board.child(name)
        if node is not None:
            root.add(node)
    title = SNode("title_block").add(SNode("title").add(quoted(f"{netlist.name} {pair.name}")))
    root.add(title)
    layers = board.child("layers")
    if layers is not None:
        root.add(layers)

    setup = board.child("setup")
    if setup is not None:
        fresh = SNode("setup")
        for child in setup.children():
            if child.name == "aux_axis_origin":
                fresh.add(SNode("aux_axis_origin").add(num(origin[0]), num(origin[1])))
            else:
                fresh.add(child)
        if fresh.child("aux_axis_origin") is None:
            fresh.add(SNode("aux_axis_origin").add(num(origin[0]), num(origin[1])))
        root.add(fresh)

    inner = (
        rect[0] + _CLIP_INSET_MM,
        rect[1] + _CLIP_INSET_MM,
        rect[2] - _CLIP_INSET_MM,
        rect[3] - _CLIP_INSET_MM,
    )
    from shapely.geometry import LineString
    from shapely.geometry import Point as ShapelyPoint

    corridor = launch_corridor(stubs)
    mine = {id(t) for t in own}
    kept: list[Track] = []
    cleared = 0.0
    for track in (*tracks, *bridges, *stubs):
        clipped = _clip(track, inner)
        if clipped is None:
            continue
        if id(track) in mine or track in stubs or track in bridges:
            kept.append(clipped)
            continue
        line = LineString([clipped.start, clipped.end])
        if not line.intersects(corridor):
            kept.append(clipped)
            continue
        cleared += line.intersection(corridor).length
        outside = line.difference(corridor)
        for piece in getattr(outside, "geoms", [outside]):
            if piece.is_empty or piece.length < _EPS:
                continue
            points = list(piece.coords)
            for a, b in pairwise(points):
                kept.append(
                    Track((a[0], a[1]), (b[0], b[1]), clipped.width,
                          clipped.layer, clipped.net)
                )

    vias = []
    dropped = 0
    for via in board.children("via"):
        at = _point(via.child("at"))
        if not (inner[0] <= at[0] <= inner[2] and inner[1] <= at[1] <= inner[3]):
            continue
        size = via.child("size")
        radius = float(size.value() or 0.4) / 2 if size else 0.2
        if ShapelyPoint(at).buffer(radius, quad_segs=8).intersects(corridor):
            dropped += 1
            continue
        vias.append(via)
    zones = list(board.children("zone"))

    # Renumber the nets. A slice carries a fraction of the board's nets, and KiCad
    # prunes the ones nothing references when it loads the file -- which shifts every
    # code after them and silently relabels every track. The geometry survives that;
    # the *names* do not, and the names are what tells the mesh generator which two
    # conductors to resolve finely. Found the hard way: a slice whose traces were
    # labelled with a neighbour's net meshed at 114 um, merged the pair into the
    # ground pour it sits in, and reported a short circuit with a clean exit code.
    names = _net_names(board)
    used = {t.net for t in kept} | {
        int(n.value() or 0) for via in vias if (n := via.child("net")) is not None
    }
    used |= {int(n.value() or 0) for z in zones if (n := z.child("net")) is not None}
    order = sorted(names[c] for c in used if names.get(c))
    recode = {0: 0, **{c: order.index(names[c]) + 1 for c in used if names.get(c)}}

    root.add(SNode("net").add(num(0), quoted("")))
    for index, name in enumerate(order, start=1):
        root.add(SNode("net").add(num(index), quoted(name)))
    for line in _edge(rect, pair.name):
        root.add(line)
    for index, track in enumerate(kept):
        root.add(_segment(track, pair.name, index, recode.get(track.net, 0)))
    for via in vias:
        root.add(_recoded(via, recode))
    for zone in zones:
        root.add(_recoded(zone, recode))
    for port in ports:
        code = next((c for c, n in names.items() if n == port.net), 0)
        root.add(port_footprint(port, pair.name, recode.get(code, 0)))
    return root, cleared, dropped


def _net_names(board: SNode) -> dict[int, str]:
    out: dict[int, str] = {}
    for node in board.children("net"):
        number, name = node.value(0), node.value(1)
        if number is not None and name is not None:
            out[int(number)] = name
    return out


def _recoded(node: SNode, recode: dict[int, int]) -> SNode:
    """A copy of ``node`` whose top-level ``(net ...)`` uses the slice's codes."""
    net = node.child("net")
    if net is None:
        return node
    fresh = SNode(node.name)
    for atom in node.atoms():
        fresh.add(atom)
    for child in node.children():
        if child.name == "net":
            fresh.add(SNode("net").add(num(recode.get(int(child.value() or 0), 0))))
        else:
            fresh.add(child)
    return fresh
