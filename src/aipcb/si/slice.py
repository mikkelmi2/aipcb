"""Cutting one pair out of a routed board, and giving it somewhere to be fed from.

A slice is a real ``.kicad_pcb``: same stackup, same nets, same pours, same design
rules, with a board outline drawn tightly around one differential pair and every
piece of copper that does not reach inside removed. It is exported by ``kicad-cli``
like any other board, which is what keeps ADR 0001 true -- aipcb never writes a
Gerber itself, not even for a solver.

Six things separate a slice from the board it came out of, and each is a deliberate
approximation worth stating out loud:

* **The coupling capacitors become copper.** A pair split by ``role: ac_coupling``
  parts is one conductor at signal frequencies; the bridge closes it, at the width
  and on the layer of the pads it lands on, in two halves that carry their nets.
  See :mod:`aipcb.si.pairs` and :func:`_bridge_tracks`.
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
* **The corridor the launches occupy is cleared** of other nets' copper. Past the
  pad is the component, and with the footprints gone that region is full of the
  neighbouring pins' fanout -- see :func:`launch_corridor`.
* **Footprints are dropped.** No pads, no antipads, no thermal reliefs. The
  interior of the run -- which is what an impedance target is about -- is
  unaffected; the last few tenths of a millimetre at each end are not modelled.
* **Every declared plane is tied to the reference net.** A supply plane is a
  reference because it is decoupled to ground, and the slice cuts away the
  decoupling along with the supply and the pads. See :func:`_grounded_planes`.
* **The boundary is stitched.** The cut ends the pours in mid-air where the board
  continues them; a ring of reference-net vias just inside the outline restores
  the return path the cut removed. See :func:`_fence`.

The last two arrived together in M13.6 and are one idea measured twice: **a slice's
return path has to be a single conductor.** Before them ``examples/pcie-sata``'s
``REFCLK`` slice held a tenth of its energy in a resonance for forty thousand
timesteps; after them the same slice decays past -39 dB and is still falling. Each
half alone leaves the plateau where it was, which is why neither is optional.

M13.7 stopped taking that on trust. :func:`_check_return_path` runs on every slice
as it is generated and refuses one whose copper is not all bonded, or whose declared
reference plane the model does not reach -- naming the layer, the net and the area
rather than the rule. The same milestone made both bands that remove a via say so:
the launch corridor always counted its removals and the half-millimetre clip band
did not, which is how a slice missing every stitching via in its own window came to
report ``0 via(s) were removed``.

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

__all__ = [
    "Port",
    "RemovedVia",
    "Slice",
    "SliceError",
    "build_slice",
    "port_footprint",
]

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
    """A pair cannot be sliced, with the reason a report can print.

    ``fatal`` separates the two kinds. An unrouted pair is a *warning*: there is
    nothing to simulate and nothing is wrong with the tool. A slice that violates
    an invariant -- floating copper, an unbonded reference -- is an **error**,
    because the alternative to failing is handing the solver a board that does not
    exist and publishing the number it comes back with. M13.6 paid for that
    distinction in solver hours.
    """

    def __init__(
        self, code: str, message: str, hint: str = "", *, fatal: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.fatal = fatal

    @property
    def status(self) -> str:
        """What a batch calls the pair this stopped."""
        return "failed" if self.fatal else "not-routed"


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
class RemovedVia:
    """One via the slice took out, and which of the two reasons took it.

    Both reasons are legitimate and only one of them used to say so. M13.6 found
    22 of ``examples/pcie-sata``'s 41 stitching vias missing from the eleven slices
    while every slice reported ``0 via(s) were removed``: the launch corridor
    counted its removals and the clip band did not, so the *reporting* was the
    defect rather than the clipping. Every removal is a record now, whichever band
    took it.
    """

    at: Point
    net: str
    why: str
    """``launch corridor`` or ``clip band``."""

    def to_dict(self) -> dict[str, object]:
        return {
            "at_mm": [round(self.at[0], 4), round(self.at[1], 4)],
            "net": self.net,
            "why": self.why,
        }

    def describe(self) -> str:
        return f"{self.net or 'no net'} at ({self.at[0]:.3f}, {self.at[1]:.3f})"


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
    half_length_mm: tuple[float, float] = (0.0, 0.0)
    """``(positive, negative)`` conductor in the slice, launches included.

    Carried so that the *slice's own* skew is a measurement rather than an
    assumption. M13c fits an intra-pair delay out of the mode-conversion curve and
    compares it to what M11e measured on the board -- and the two are only
    comparable if the structure that was simulated has the same length mismatch the
    board does. It does not always: the slicer grows a 1.5 mm launch at each of the
    four ends and trims each one to where there is room, so a slice can carry skew
    the routed pair does not. This is the number that says how much.
    """
    notes: list[str] = field(default_factory=list)
    removed_vias: tuple[RemovedVia, ...] = ()
    """Every via the slice took out, with the band that took it. Never empty silently."""

    @property
    def pair_gap_mm(self) -> float | None:
        """Edge-to-edge gap between the two halves, measured at the driven end.

        Read off the ports rather than off the net class, because what the mesh has
        to resolve is the geometry that was sliced. Ports 1 and 3 are the two halves
        of the driven end, so the distance between their launch centres less one
        trace width is the gap the solver has to put cells into.
        """
        at_end = [p for p in self.ports if p.number in (1, 3)]
        if len(at_end) != 2:
            return None
        pitch = math.dist(at_end[0].at, at_end[1].at)
        gap = pitch - (at_end[0].width_mm + at_end[1].width_mm) / 2
        return gap if gap > 0 else None

    @property
    def slice_skew_mm(self) -> float:
        """How far out of length the *sliced* structure is, both launches included."""
        return abs(self.half_length_mm[0] - self.half_length_mm[1])

    @property
    def spans_layers(self) -> bool:
        """Whether the two ends of this slice sit on different copper layers.

        A pair that changes layer part way along puts a via barrel, and usually a
        change of reference plane, inside the span the ports measure. That matters
        to whoever reads the impedance: the estimator in :func:`aipcb.si.results.
        analyse` is a median input impedance, which is the characteristic impedance
        of a *uniform* line and is not the characteristic impedance of anything when
        the line is a cascade of two sections and a barrel.

        Measured on `examples/pcie-sata` in M13.5: the two links whose ports span
        layers -- `PCIE_RXP/N` and `REFCLKP/N` -- are exactly the two that miss
        their +/-10 % band, at -14.4 % and -41.4 %, while `PCIE_TXP/N` and all eight
        SATA links keep both ends on one layer.
        """
        return len({port.layer for port in self.ports}) > 1

    @property
    def spans_planes(self) -> bool:
        """Whether the two ends are referenced to different planes."""
        return len({port.plane_index for port in self.ports}) > 1

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
            "half_length_mm": [round(v, 4) for v in self.half_length_mm],
            "slice_skew_mm": round(self.slice_skew_mm, 4),
            "spans_layers": self.spans_layers,
            "spans_planes": self.spans_planes,
            "launch_mm": round(self.launch_mm, 4),
            "bridged_by": list(self.bridged),
            "vias_removed": len(self.removed_vias),
            "vias_removed_detail": [v.to_dict() for v in self.removed_vias],
            "ports": [p.to_dict() for p in self.ports],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# reading the routed board
# ---------------------------------------------------------------------------


def reference_net(netlist: Netlist) -> str | None:
    """The net a slice ties every declared plane to, or ``None`` if it has no planes.

    The net covering the most copper layers wins, counting both the planes the
    stackup declares and the board-scope pours; ties break on name so the answer is
    a function of the design rather than of dictionary order. On
    ``examples/pcie-sata`` that is ``GND``, on three layers, against ``P3V3`` on one.
    """
    if netlist.layout is None or netlist.layout.stackup is None:
        return None
    stackup = netlist.layout.stackup
    covered: dict[str, set[str]] = {}
    for plane in stackup.planes:
        covered.setdefault(plane.net, set()).add(plane.layer)
    for pour in netlist.pours:
        if pour.scope != "board":
            continue
        for layer in pour.copper_layers:
            covered.setdefault(pour.net, set()).add(layer)
    if not covered:
        return None
    return min(covered, key=lambda net: (-len(covered[net]), net))


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
        out.append((_point(via.child("at")), _via_radius(via)))
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


@dataclass(frozen=True, slots=True)
class _Hop:
    """One capacitor's worth of copper: where it goes, and what it lands on."""

    a: Point
    b: Point
    width: float
    layer: str
    net_a: int
    """Board net code of the conductor the ``a`` end lands on."""
    net_b: int
    """And of the one at ``b``. The two differ -- that is what the cap separates."""


def _incident(tracks: Iterable[Track], point: Point) -> Track | None:
    """The track ending at ``point``, or ``None``. Ties broken by nothing: any will do."""
    return next(
        (t for t in tracks if _key(t.start) == _key(point) or _key(t.end) == _key(point)),
        None,
    )


def _nearest_bridge(groups: list[list[Track]]) -> list[_Hop]:
    """Shortest hop joining each pair of sub-conductors, i.e. where the cap sits.

    Deliberately geometric rather than read off the capacitor's pads: the router
    started each net *at* a pad centre, so the two nearest dangling ends are the two
    pads, and taking them avoids a second implementation of KiCad's footprint
    transform -- which is exactly the kind of code that is wrong in the rotated case
    and never noticed.
    """
    out: list[_Hop] = []
    joined = groups[0]
    for other in groups[1:]:
        best: _Hop | None = None
        shortest = math.inf
        for a in _endpoints(joined):
            for b in _endpoints(other):
                distance = math.dist(a, b)
                if distance >= shortest:
                    continue
                at_a, at_b = _incident(joined, a), _incident(other, b)
                shortest = distance
                best = _Hop(
                    a=a,
                    b=b,
                    width=at_b.width if at_b is not None else 0.2,
                    layer=at_b.layer if at_b is not None else "F.Cu",
                    net_a=at_a.net if at_a is not None else 0,
                    net_b=at_b.net if at_b is not None else 0,
                )
        if best is not None:
            out.append(best)
            joined = [*joined, *other, *_bridge_tracks(best)]
    return out


def _bridge_tracks(hop: _Hop) -> tuple[Track, Track]:
    """The copper that stands in for the capacitor, in two halves that carry nets.

    Geometrically this is one straight piece: same width and same layer as the pads
    it lands on, so the pair's line is uninterrupted through the cap. It is emitted
    as two halves for one reason, and it is not cosmetic. **gerber2ems meshes the
    nets it is told about and nothing else** -- ``grid_gen`` reads ``netinfo.json``
    and adds grid lines from the traces of those nets, so copper the Gerber labels
    ``no-net`` gets no edge cells and lands wherever the grid happens to fall.

    A single-track bridge is net 0, and M13.7 measured what that costs: on
    ``examples/pcie-sata``'s ``PCIE_TX`` it was the *only* unnetted copper in any
    slice on the corpus, and it sat exactly at the discontinuity the run is trying
    to resolve. Splitting at the midpoint gives each half the net of the pad it
    starts from, so both sides of the cap are nets under test and the mesh
    generator resolves the bridge like the line it continues. The two halves touch,
    which is the short the bridge *is*.
    """
    mid = ((hop.a[0] + hop.b[0]) / 2, (hop.a[1] + hop.b[1]) / 2)
    return (
        Track(hop.a, mid, hop.width, hop.layer, hop.net_a),
        Track(mid, hop.b, hop.width, hop.layer, hop.net_b),
    )


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
    bridged_by_side: dict[str, list[Track]] = {}
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
        for hop in _nearest_bridge(groups):
            for bridge in _bridge_tracks(hop):
                bridges.append(bridge)
                bridged_by_side.setdefault(side, []).append(bridge)
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

    rect = enclose_vias(_bounds([*all_pair, *stubs], settings.margin_mm), _vias(board))
    origin = (rect[0], rect[3])
    conductor_length = sum(t.length for t in all_pair)

    def half(side: str) -> float:
        """One conductor's total length, its two launches included.

        The launches are the same length on all four ends by construction, so they
        cancel in the difference -- they are counted anyway, because what this
        measures is the structure the solver was handed rather than the part of it
        the router drew.
        """
        return (
            sum(t.length for t in sides[side])
            + sum(t.length for t in bridged_by_side.get(side, []))
            + 2 * launch
        )

    built = _assemble(
        board, netlist, pair, rect, origin, ports, tracks, stubs, bridges, all_pair,
        settings,
    )
    if built.tied:
        notes.append(
            f"{', '.join(built.tied)} carries a plane the slice cannot connect -- its "
            "supply, its pads and its decoupling are all outside the window -- so "
            f"it is tied to {reference_net(netlist)} here. On the board it is a "
            "reference plane; modelled floating it is a resonant plate instead"
        )
    for why in ("launch corridor", "clip band"):
        notes.extend(_removal_notes(built, why))
    _check_return_path(built.board, netlist, pair, metals)
    return Slice(
        pair=pair,
        board=built.board,
        rect=rect,
        origin=origin,
        ports=tuple(ports),
        metals=metals,
        conductor_length_mm=conductor_length,
        half_length_mm=(half("p"), half("n")),
        launch_mm=launch,
        bridged=pair.bridged_by,
        notes=notes,
        removed_vias=built.removed,
    )


#: How many removed vias a note lists by position before it summarises the rest.
_MAX_LISTED_VIAS = 8


def _removal_notes(built: _Assembly, why: str) -> list[str]:
    """One note per band that took something, naming what it took.

    The corridor's note keeps M12's wording, because it was never the broken half
    and a report that changes its phrasing for no reason is a report nobody diffs.
    """
    taken = [v for v in built.removed if v.why == why]
    listed = ", ".join(v.describe() for v in taken[:_MAX_LISTED_VIAS])
    if len(taken) > _MAX_LISTED_VIAS:
        listed += f", and {len(taken) - _MAX_LISTED_VIAS} more"
    if why == "launch corridor":
        if built.cleared_mm <= _EPS and not taken:
            return []
        note = (
            f"{built.cleared_mm:.2f} mm of other nets' track and {len(taken)} via(s) "
            "were removed from the corridor the four launches occupy, because a "
            "launch runs out past the pad into the neighbouring pins' fanout"
        )
        return [f"{note}: {listed}" if taken else note]
    if not taken:
        return []
    return [
        f"{len(taken)} via(s) inside the slice window sat in the "
        f"{_CLIP_INSET_MM:.2f} mm clip band just inside the outline and were "
        f"removed: {listed}"
    ]


def _check_return_path(
    board: SNode, netlist: Netlist, pair: LogicalPair, metals: tuple[str, ...]
) -> None:
    """Refuse to hand the solver a slice whose return path is not one conductor.

    M13.6's lesson, run on every slice at generation time. Two ways to fail, and
    each names the copper rather than the rule: a sheet bonded to nothing, or a
    declared reference plane the slice does not reach. Both used to be silent, and
    a silent one costs a solver run and a number that looks like a measurement.

    See :mod:`aipcb.si.integrity` for what "bonded" is measured on, and for the one
    thing this cannot see: the slice is checked before it is filled, so a plane cut
    in two *by the fill* is :mod:`aipcb.checks.planes`'s finding on the real board,
    not this one's.
    """
    from aipcb.highspeed import target_for
    from aipcb.si.integrity import inspect_return_path

    path = inspect_return_path(board, metals)
    if path.floating:
        listed = "; ".join(f.describe() for f in path.floating)
        raise SliceError(
            "si-slice-floating-copper",
            f"{pair.name}'s slice leaves copper at no defined potential: {listed}. "
            "A return path that is not one conductor is a resonator, and the "
            "impedance it produces is not the board's",
            hint="tie it to the reference net, or stitch the slice so a via reaches "
            "it; `aipcb.si.slice._grounded_planes` and `_fence` are the two places "
            "that already do this for planes and for the cut boundary",
            fatal=True,
        )

    target = target_for(netlist, pair.net_class)
    unbonded = path.unbonded(target.reference if target else None)
    if unbonded is not None:
        raise SliceError(
            "si-slice-reference-unbonded",
            f"{pair.name} is referenced to {unbonded}, and the slice does not bond "
            "that layer to the rest of its return path, so the impedance would be "
            "measured against copper the model leaves floating",
            hint=f"the slice needs copper on {unbonded} tied to the reference net -- "
            "a plane declared on it, and a stitching via inside the window that "
            "reaches it",
            fatal=True,
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


def enclose_vias(
    rect: tuple[float, float, float, float], vias: list[tuple[Point, float]]
) -> tuple[float, float, float, float]:
    """Grow ``rect`` on all four sides so the vias inside it clear the outline.

    :func:`_assemble` keeps a via only when its centre lies inside the rectangle
    *inset* by :data:`_CLIP_INSET_MM`, so before M13.6 a via landing in that
    half-millimetre band was dropped without a word -- unlike the launch corridor,
    which drops vias too and says how many in the slice's own notes.

    Counted on ``examples/pcie-sata`` against the routed board, over all eleven
    slices: **92 GND vias lie inside a slice rectangle, 58 reached the slice, 4
    were removed by the corridor and said so, and 30 went silently.**

    The stitching vias are the ones that matter, and they fared worse than the
    average because M10's grid is anchored to multiples of its 8 mm pitch in board
    coordinates while a slice is cut to whatever the pair needs. Of 41 stitching
    vias inside the eleven rectangles, **19 reached a slice**:

    ==================  =========  =========  =============================
    slice               in rect    in slice
    ==================  =========  =========  =============================
    ``PCIE_RXN/P``      2          **0**      converges, reads -14.4 %
    ``REFCLKN/P``       2          **0**      the non-convergent one
    ``SATA1_TXN/P``     4          **0**      no ground via of any size at all
    ``SATA2_RXN/P``     3          **0**
    ``SATA0_TXN/P``     8          3
    ==================  =========  =========  =============================

    ``SATA1_TX`` is the one that shows what it cost: its 7.638 mm window fell
    between two rows of the grid, every via inside it was in the clip band, and the
    model went to the solver with its two ground pours and both planes mutually
    isolated -- four sheets of copper where the board has one conductor, under a
    100 ohm microstrip whose reference was connected to nothing.

    The growth is **uniform and fixed**, not fitted to where the vias are. A first
    attempt grew each side to clear whichever vias it found and re-scanned, which
    is a cascade: on ``PCIE_TX`` each expansion swept in another via and the window
    came out 92 % larger than the pair needed. A constant instead -- the clip inset
    plus the widest via's radius -- guarantees the property that matters (a via
    whose centre is in the pair's own window is in the slice, clear of the edge) at
    a cost that is the same 0.8 mm on every board and cannot run away.
    """
    pad = _CLIP_INSET_MM + max((radius for _, radius in vias), default=0.2)
    return (
        round(rect[0] - pad, 4),
        round(rect[1] - pad, 4),
        round(rect[2] + pad, 4),
        round(rect[3] + pad, 4),
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


#: Wavelengths of via spacing along the slice boundary. A quarter wavelength at the
#: top of the swept band is the usual rule of thumb for a via fence, and it is what
#: M13.6 measured with: at 8 GHz in FR4 it comes to about 4.4 mm.
_FENCE_WAVELENGTHS = 0.25

#: Fallback fence via geometry, in millimetres, for a design that declares no
#: ``stitching:`` of its own. Matches ``examples/pcie-sata``'s stitching via.
_FENCE_VIA_MM = (0.6, 0.3)


def fence_pitch_mm(stackup: Stackup, settings: ResolvedSimulation) -> float:
    """How far apart the boundary vias sit: a quarter wavelength at the band's top.

    Slower in the laminate than in air by ``sqrt(epsilon_r)``, so the number falls
    as either the frequency or the dielectric constant rises. Rounded down to a
    tenth of a millimetre, because it lands in the slice board and therefore in the
    digest, and a cache key should not carry sixteen digits of float.
    """
    epsilon = max(stackup.epsilon_r_default, 1.0)
    wavelength_mm = 299_792.458 / (settings.stop_hz / 1e6) / math.sqrt(epsilon)
    return math.floor(wavelength_mm * _FENCE_WAVELENGTHS * 10) / 10


def _fence(
    rect: tuple[float, float, float, float],
    pair: str,
    net: int,
    pitch: float,
    via: tuple[float, float],
    keep_clear: object,
) -> list[SNode]:
    """A ring of reference-net vias just inside the slice outline.

    **What this is for.** The slice boundary is where the board's return path was
    cut. On the board the pours run on past it and the stitching grid ties them
    together every few millimetres; in the slice they stop in mid-air, tied only
    wherever a stitching via happened to fall inside -- which on
    ``examples/pcie-sata`` is anywhere from eight down to none, decided by where a
    7.6 mm window lands on an 8 mm grid. The fence restores the continuity the cut
    removed, and it does it at a spacing that does not depend on luck.

    **It is copper the board does not have**, and so it is the sixth approximation
    in the list at the top of this module. What earns it is the measurement.
    ``REFCLKN+REFCLKP`` at 32 000 steps, one change at a time, on the same slice:

    ===========================  ==================================================
    slice                        energy
    ===========================  ==================================================
    as generated before M13.6    plateaus at -7 to -10 dB for 42 000 steps
    fence only                   plateaus at -9.5 to -10.8 dB -- **no better**
    ``In2.Cu`` tied only         plateaus at -14 to -15 dB (M13.5's i5)
    fence **and** the tie        -9.8, -18.8, -29.3, **-39.9 dB and still falling**
    ===========================  ==================================================

    Neither half works alone and together they do, which is the reading that makes
    them one idea rather than two fixes: what the slice was missing is a return
    structure that is a *single conductor*. Tying the supply plane gives the fourth
    sheet the same potential as the other three; the fence ties all four together
    densely enough that no parallel-plate mode between them has anywhere to stand.

    Positions that would land on somebody's copper are dropped rather than
    negotiated, the same way :mod:`aipcb.route.stitch` drops them on a real board.
    """
    from shapely.geometry import Point as ShapelyPoint

    diameter, drill = via
    inset = _CLIP_INSET_MM + diameter / 2
    x0, y0 = rect[0] + inset, rect[1] + inset
    x1, y1 = rect[2] - inset, rect[3] - inset
    if x1 <= x0 or y1 <= y0:
        return []

    def along(low: float, high: float) -> list[float]:
        steps = max(1, round((high - low) / pitch))
        return [low + index * (high - low) / steps for index in range(steps + 1)]

    def at(x: float, y: float) -> Point:
        return (round(x, 4), round(y, 4))

    # A set, and both coordinates rounded the same way, so the four corners are one
    # via each. Rounding only the coordinate that varies put two vias in the same
    # hole at every corner, which is a drill file with a duplicate in it.
    points = {at(x, y0) for x in along(x0, x1)} | {at(x, y1) for x in along(x0, x1)}
    points |= {at(x0, y) for y in along(y0, y1)} | {at(x1, y) for y in along(y0, y1)}

    out: list[SNode] = []
    for index, (x, y) in enumerate(sorted(points)):
        pad = ShapelyPoint((x, y)).buffer(diameter / 2 + _CLIP_INSET_MM, quad_segs=8)
        if pad.intersects(keep_clear):
            continue
        node = SNode("via")
        node.add(SNode("at").add(num(x), num(y)))
        node.add(SNode("size").add(num(diameter)))
        node.add(SNode("drill").add(num(drill)))
        node.add(SNode("layers").add(quoted("F.Cu"), quoted("B.Cu")))
        node.add(SNode("net").add(num(net)))
        node.add(SNode("uuid").add(quoted(element_uuid("si-fence", pair, str(index)))))
        out.append(node)
    return out


def _via_radius(via: SNode) -> float:
    size = via.child("size")
    return float(size.value() or 0.4) / 2 if size is not None else 0.2


def _fence_via(netlist: Netlist, reference: str) -> tuple[float, float]:
    """Diameter and drill for a fence via: the board's own stitching via if it has
    one on the reference net, so the fence is drilled like the stitching it stands
    in for rather than to a number invented here."""
    for intent in netlist.stitching:
        if intent.net == reference and intent.via is not None:
            return (intent.via.diameter, intent.via.drill)
    return _FENCE_VIA_MM


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


@dataclass(frozen=True, slots=True)
class _Assembly:
    """The slice board, and everything the assembly had to take out to make it."""

    board: SNode
    cleared_mm: float
    """Length of other nets' track cut out of the four launch corridors."""
    removed: tuple[RemovedVia, ...]
    tied: tuple[str, ...]
    """Plane layers retagged to the reference net. See :func:`_grounded_planes`."""


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
    settings: ResolvedSimulation,
) -> _Assembly:
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
    from shapely.ops import unary_union

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

    # Two bands take vias out, and both of them say so. The corridor's removals were
    # always reported; the clip band's were not, which is how `REFCLK`'s slice came
    # to report `0 via(s) were removed` while carrying none of the stitching inside
    # its own window. A via outside `rect` entirely is not a removal -- it was never
    # in this pair's window -- so only the band between `rect` and `inner` counts.
    vias = []
    removed: list[RemovedVia] = []
    named = _net_names(board)
    for via in board.children("via"):
        at = _point(via.child("at"))
        net = named.get(int((via.child("net") or SNode("x")).value() or 0), "")
        if not (inner[0] <= at[0] <= inner[2] and inner[1] <= at[1] <= inner[3]):
            if rect[0] <= at[0] <= rect[2] and rect[1] <= at[1] <= rect[3]:
                removed.append(RemovedVia(at=at, net=net, why="clip band"))
            continue
        radius = _via_radius(via)
        if ShapelyPoint(at).buffer(radius, quad_segs=8).intersects(corridor):
            removed.append(RemovedVia(at=at, net=net, why="launch corridor"))
            continue
        vias.append(via)
    zones, tied = _grounded_planes(
        list(board.children("zone")), netlist, _net_numbers(board)
    )

    # The boundary fence goes on last, so it can be kept off everything already
    # placed. It carries the reference net, which is the net the tied planes now
    # carry too -- the three together are what make the slice's return path one
    # conductor rather than four sheets. See :func:`_fence` for the measurement.
    numbers = _net_numbers(board)
    reference = reference_net(netlist)
    stackup = netlist.layout.stackup if netlist.layout else None
    fenced: list[SNode] = []
    if reference is not None and reference in numbers and stackup is not None:
        occupied = unary_union(
            [corridor]
            + [
                LineString([t.start, t.end]).buffer(t.width / 2)
                for t in kept
                if _net_names(board).get(t.net) != reference
            ]
            + [
                ShapelyPoint(centre).buffer(radius)
                for centre, radius in (
                    (_point(v.child("at")), _via_radius(v)) for v in vias
                )
            ]
        )
        fenced = _fence(
            rect,
            pair.name,
            numbers[reference],
            fence_pitch_mm(stackup, settings),
            _fence_via(netlist, reference),
            occupied,
        )

    # Renumber the nets. A slice carries a fraction of the board's nets, and KiCad
    # prunes the ones nothing references when it loads the file -- which shifts every
    # code after them and silently relabels every track. The geometry survives that;
    # the *names* do not, and the names are what tells the mesh generator which two
    # conductors to resolve finely. Found the hard way: a slice whose traces were
    # labelled with a neighbour's net meshed at 114 um, merged the pair into the
    # ground pour it sits in, and reported a short circuit with a clean exit code.
    names = _net_names(board)
    used = {t.net for t in kept} | {
        int(n.value() or 0)
        for via in (*vias, *fenced)
        if (n := via.child("net")) is not None
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
    for via in (*vias, *fenced):
        root.add(_recoded(via, recode))
    for zone in zones:
        root.add(_recoded(zone, recode))
    for port in ports:
        code = next((c for c, n in names.items() if n == port.net), 0)
        root.add(port_footprint(port, pair.name, recode.get(code, 0)))
    return _Assembly(
        board=root,
        cleared_mm=cleared,
        removed=tuple(sorted(removed, key=lambda v: (v.why, v.at))),
        tied=tied,
    )


def _grounded_planes(
    zones: list[SNode], netlist: Netlist, numbers: dict[str, int]
) -> tuple[list[SNode], tuple[str, ...]]:
    """Tie every zone on a declared plane layer to the slice's reference net.

    The defect this closes, measured on all eleven ``examples/pcie-sata`` slices in
    M13.6: ``In2.Cu`` carries ``P3V3`` and, inside a slice, is **connected to
    nothing at all** -- 79 to 367 mm2 of copper with no via, no pad and no port
    touching it. The design declares that layer a plane and the ``pcie_rx`` class
    names it as its ``reference:``, so on the board it is the return path for every
    pair that lands on ``B.Cu``. What makes it one is its supply, its pads and the
    decoupling across it, and the slice cuts away all three: a 5 x 25 mm plate
    between two grounded planes, with the pair's own via barrel through its
    antipad, is a parallel-plate resonator rather than a reference.

    So the slice ties it. This is an approximation and it is the fifth in the list
    at the top of this module: it models a well-decoupled supply plane as the AC
    ground the board treats it as, and it cannot therefore tell anyone that the
    decoupling is inadequate. What it replaces is not a more conservative model but
    a *different board* -- one whose reference plane floats, which no assembled
    board's does.

    Only layers the stackup declares as planes are touched, and only zones lying
    entirely on them, so a signal zone or a partial pour is left exactly as it is.
    """
    reference = reference_net(netlist)
    if reference is None or netlist.layout is None or netlist.layout.stackup is None:
        return zones, ()
    planes = {p.layer for p in netlist.layout.stackup.planes}
    if not planes:
        return zones, ()

    out: list[SNode] = []
    tied: list[str] = []
    for zone in zones:
        layers = _zone_layers(zone)
        name = zone.child("net_name")
        carried = name.value() if name is not None else None
        if not layers or not layers <= planes or carried == reference:
            out.append(zone)
            continue
        fresh = SNode("zone")
        for atom in zone.atoms():
            fresh.add(atom)
        for child in zone.children():
            if child.name == "net":
                fresh.add(SNode("net").add(num(numbers.get(reference, 0))))
            elif child.name == "net_name":
                fresh.add(SNode("net_name").add(quoted(reference)))
            else:
                fresh.add(child)
        out.append(fresh)
        tied.extend(sorted(layers))
    return out, tuple(tied)


def _zone_layers(zone: SNode) -> set[str]:
    node = zone.child("layers") or zone.child("layer")
    return {str(atom.value) for atom in node.atoms()} if node is not None else set()


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
