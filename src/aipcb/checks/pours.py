# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Validating pours and stitching before anything is built.

Every check here answers a question that is cheap now and expensive later. A pour
on a net that does not exist emits a zone attached to nothing, which fills with
nothing and looks exactly like a plane that is simply empty. A region outside the
outline emits a zone KiCad clips to nothing, silently. Two zones overlapping on one
layer with the same priority is the one case where KiCad's own behaviour is
genuinely undefined -- whichever fills first keeps the copper -- so it is an error
here rather than a coin toss in the fill.

The rule these follow is the project's usual one: an error is something that cannot
be true, a warning is something that is probably not meant, and anything requiring
a guess is neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aipcb.compile.frame import BoardFrame, frame_for
from aipcb.diagnostics import Report
from aipcb.model.board import tessellate
from aipcb.model.layout import copper_layer_names
from aipcb.model.pours import Pour
from aipcb.netlist import Netlist

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from shapely.geometry.base import BaseGeometry

__all__ = ["run_pour_checks"]

#: Overlap smaller than this is two regions touching, not two regions fighting.
_OVERLAP_EPSILON = 1e-6


def run_pour_checks(netlist: Netlist, report: Report) -> None:
    """Every check on ``pours:`` and ``stitching:``, in the order a reader wants."""
    if not netlist.pours and not netlist.stitching:
        return
    layers = _copper_layers(netlist)
    _check_nets(netlist, report)
    _check_layers(netlist, report, layers)
    frame = frame_for(netlist)
    _check_regions(netlist, frame, report)
    _check_priorities(netlist, frame, report)
    _check_pads(netlist, report)
    _check_stitching(netlist, report, layers)


def _copper_layers(netlist: Netlist) -> tuple[str, ...]:
    count = netlist.layout.stackup.copper_layers if netlist.layout else 2
    return copper_layer_names(count)


def _path(block: str, index: int) -> tuple[str | int, ...]:
    return (block, index)


def _check_nets(netlist: Netlist, report: Report) -> None:
    """A pour on a net nothing connects to is copper that joins nothing."""
    known = ", ".join(sorted(netlist.nets)[:10])
    declared: list[tuple[str, int, str]] = [
        ("pours", index, pour.net) for index, pour in enumerate(netlist.pours)
    ]
    declared += [
        ("stitching", index, stitching.net)
        for index, stitching in enumerate(netlist.stitching)
    ]
    for block, index, net in declared:
        if net in netlist.nets:
            continue
        report.error(
            "pour-unknown-net",
            f"`{block}:` names net {net!r}, which this design does not have",
            loc=netlist.locs.get(_path(block, index)),
            path=_path(block, index),
            hint=f"nets available: {known}",
        )


def _check_layers(netlist: Netlist, report: Report, layers: tuple[str, ...]) -> None:
    """A pour on a layer the stackup does not have is a typo, not a plane."""
    for index, pour in enumerate(netlist.pours):
        for layer in pour.copper_layers:
            if layer in layers:
                continue
            report.error(
                "pour-unknown-layer",
                f"the {pour.net} pour names {layer}, which is not one of this "
                f"{len(layers)}-layer board's copper layers",
                loc=netlist.locs.get(_path("pours", index)),
                path=_path("pours", index),
                hint=f"copper layers available: {', '.join(layers)}",
            )


def _check_regions(netlist: Netlist, frame: BoardFrame | None, report: Report) -> None:
    """A region outside the outline pours nothing, and says so nowhere."""
    if frame is None:
        for index, pour in enumerate(netlist.pours):
            if pour.region is None:
                continue
            report.error(
                "pour-region-without-frame",
                f"the {pour.net} pour gives a `region:`, but the design declares no "
                "`board:` outline for those coordinates to be in",
                loc=netlist.locs.get(_path("pours", index)),
                path=_path("pours", index),
                hint="add a `board:` block with an `outline:`, or pour the whole "
                "board with `scope: board`",
            )
        return

    from shapely.geometry import Polygon as ShapelyPolygon

    board = ShapelyPolygon(frame.polygon(), frame.cutout_polygons())
    for index, pour in enumerate(netlist.pours):
        shape = _region_shape(pour, frame)
        if shape is None:
            continue
        if not shape.intersects(board):
            report.error(
                "pour-region-outside-board",
                f"the {pour.net} pour's {pour.region.label if pour.region else ''} "
                "region lies entirely outside the board outline",
                loc=netlist.locs.get(_path("pours", index)),
                path=_path("pours", index),
                hint="region coordinates are in the board frame -- millimetres, Y "
                "up, origin at the outline's bottom-left corner",
            )
        elif not board.contains(shape):
            report.warning(
                "pour-region-crosses-edge",
                f"the {pour.net} pour's region reaches outside the board outline; "
                "KiCad will clip it",
                loc=netlist.locs.get(_path("pours", index)),
                path=_path("pours", index),
                hint="harmless if deliberate -- a region drawn generously so it "
                "reaches the edge -- but worth saying on purpose",
            )


def _region_shape(pour: Pour, frame: BoardFrame) -> BaseGeometry | None:
    from shapely.geometry import Polygon as ShapelyPolygon

    ring = pour.ring()
    if ring is None:
        return None
    shape = ShapelyPolygon([frame.to_kicad(p) for p in tessellate(ring)])
    return shape if shape.is_valid else shape.buffer(0)


def _check_priorities(
    netlist: Netlist, frame: BoardFrame | None, report: Report
) -> None:
    """Two zones overlapping on one layer at one priority is a coin toss.

    KiCad pours in priority order and a zone keeps whatever an earlier one did not
    take, so equal priorities leave the result depending on the order the zones
    happen to sit in the file. Naming the winner is the source's job.
    """
    if frame is None:
        return
    from shapely.geometry import Polygon as ShapelyPolygon

    board = ShapelyPolygon(frame.polygon(), frame.cutout_polygons())
    shapes: dict[int, BaseGeometry] = {}
    for index, pour in enumerate(netlist.pours):
        shape = _region_shape(pour, frame)
        shapes[index] = board if shape is None else shape.intersection(board)

    for first in range(len(netlist.pours)):
        for second in range(first + 1, len(netlist.pours)):
            a, b = netlist.pours[first], netlist.pours[second]
            shared = set(a.copper_layers) & set(b.copper_layers)
            if not shared or a.priority != b.priority:
                continue
            overlap = shapes[first].intersection(shapes[second])
            if overlap.is_empty or overlap.area <= _OVERLAP_EPSILON:
                continue
            report.error(
                "pour-priority-tie",
                f"the {a.net} and {b.net} pours overlap on "
                f"{', '.join(sorted(shared))} and both have priority {a.priority}",
                loc=netlist.locs.get(_path("pours", second)),
                path=_path("pours", second),
                hint="give the one that should keep the copper the higher priority; "
                "with equal priorities which one wins depends on file order",
            )


def _check_pads(netlist: Netlist, report: Report) -> None:
    """A per-pad override that names no pad silences itself."""
    for index, pour in enumerate(netlist.pours):
        for override in pour.pad_connect:
            for pad in override.pads:
                refdes = pad.split(".", 1)[0]
                if refdes in netlist.components:
                    continue
                report.error(
                    "pour-unknown-pad",
                    f"`pad_connect:` names {pad}, and {refdes} is not a component "
                    "in this design",
                    loc=netlist.locs.get(_path("pours", index)),
                    path=_path("pours", index),
                    hint="a pad is named `U2.4`, or `U2.4#2` for the second pad "
                    "carrying that number",
                )


def _check_stitching(
    netlist: Netlist, report: Report, layers: tuple[str, ...]
) -> None:
    """Stitching that cannot reach a plane places nothing, which is worth saying."""
    poured: dict[str, set[str]] = {}
    for pour in netlist.pours:
        poured.setdefault(pour.net, set()).update(pour.copper_layers)

    for index, stitching in enumerate(netlist.stitching):
        path = _path("stitching", index)
        loc = netlist.locs.get(path)
        span = stitching.between or (layers[0], layers[-1])
        missing = [name for name in span if name not in layers]
        if missing:
            report.error(
                "stitching-unknown-layer",
                f"`stitching:` joins {', '.join(missing)}, which this "
                f"{len(layers)}-layer board does not have",
                loc=loc, path=path,
                hint=f"copper layers available: {', '.join(layers)}",
            )
            continue
        unpoured = [name for name in span if name not in poured.get(stitching.net, set())]
        if unpoured:
            report.warning(
                "stitching-without-pour",
                f"stitching {stitching.net} between {span[0]} and {span[1]}, but it "
                f"has no pour on {', '.join(unpoured)}",
                loc=loc, path=path,
                hint="a stitching via outside the pour joins nothing and reports as "
                "an unconnected item, so the generator will place none",
            )
        if stitching.around is not None and not _names_a_part(
            netlist, stitching.around
        ):
            report.error(
                "stitching-unknown-part",
                f"`stitching:` rings {stitching.around!r}, which is not a component "
                "in this design",
                loc=loc, path=path,
                hint=f"components available: {', '.join(sorted(netlist.components)[:10])}",
            )


def _names_a_part(netlist: Netlist, name: str) -> bool:
    return any(
        component.refdes == name or component.path_text == name
        for component in netlist.components.values()
    )
