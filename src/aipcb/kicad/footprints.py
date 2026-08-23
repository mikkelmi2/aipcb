# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Locating KiCad footprints (``.kicad_mod`` inside ``.pretty`` directories).

The board writer needs a footprint's full definition to place it, and validation
needs to know whether a footprint exists at all -- a design that names a footprint
KiCad cannot find produces a board with a hole in it, and the only warning is deep
inside a DRC report.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from aipcb.kicad.sexpr import SExprError, SNode, parse

__all__ = [
    "FOOTPRINT_DIR_ENV",
    "Footprint",
    "FootprintNotFound",
    "default_footprint_dirs",
    "footprint_exists",
    "resolve_footprint",
]

FOOTPRINT_DIR_ENV = "AIPCB_FOOTPRINT_DIR"

_STANDARD_DIRS = (
    "/usr/share/kicad/footprints",
    "/usr/local/share/kicad/footprints",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    "C:/Program Files/KiCad/9.0/share/kicad/footprints",
    "C:/Program Files/KiCad/8.0/share/kicad/footprints",
)


class FootprintNotFound(LookupError):
    """A footprint library or footprint could not be located."""


@dataclass(frozen=True, slots=True)
class Footprint:
    """A footprint definition and the pads it exposes."""

    name: str
    library: str
    path: Path
    node: SNode

    @property
    def lib_id(self) -> str:
        return f"{self.library}:{self.name}"

    def pad_numbers(self) -> tuple[str, ...]:
        """Every pad number, including duplicates collapsed.

        Multiple pads legitimately share a number -- thermal paddles, split
        connector shells -- so this returns the distinct set, sorted.
        """
        numbers = {
            value
            for pad in self.node.children("pad")
            if (value := pad.value(0)) is not None and value != ""
        }
        return tuple(sorted(numbers, key=_pad_key))


def _pad_key(number: str) -> tuple[int, int, str]:
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


def default_footprint_dirs() -> tuple[Path, ...]:
    override = os.environ.get(FOOTPRINT_DIR_ENV)
    candidates = [Path(override)] if override else []
    candidates += [Path(p) for p in _STANDARD_DIRS]
    return tuple(p for p in candidates if p.is_dir())


@lru_cache(maxsize=512)
def resolve_footprint(lib_id: str) -> Footprint:
    """Resolve a ``Library:Footprint`` identifier against the installed libraries."""
    library, _, name = lib_id.partition(":")
    if not library or not name:
        raise FootprintNotFound(
            f"{lib_id!r} is not a library id; expected the form 'Library:Footprint'"
        )
    for directory in default_footprint_dirs():
        path = directory / f"{library}.pretty" / f"{name}.kicad_mod"
        if path.is_file():
            try:
                node = parse(path.read_text(encoding="utf-8"))
            except (OSError, SExprError) as exc:
                raise FootprintNotFound(f"cannot read {path}: {exc}") from exc
            return Footprint(name, library, path, node)

    searched = ", ".join(str(d) for d in default_footprint_dirs()) or "no directories"
    pretty = [d / f"{library}.pretty" for d in default_footprint_dirs()]
    if not any(p.is_dir() for p in pretty):
        raise FootprintNotFound(
            f"footprint library {library!r} not found (searched: {searched}). "
            f"Set {FOOTPRINT_DIR_ENV} if KiCad's libraries live elsewhere."
        )
    raise FootprintNotFound(f"footprint {name!r} is not in library {library!r}")


def footprint_exists(lib_id: str) -> bool:
    try:
        resolve_footprint(lib_id)
    except FootprintNotFound:
        return False
    return True


@dataclass(frozen=True, slots=True)
class Extent:
    """An axis-aligned box in a footprint's own coordinate system, in millimetres."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def padded(self, margin: float) -> Extent:
        return Extent(
            self.min_x - margin, self.min_y - margin,
            self.max_x + margin, self.max_y + margin,
        )


def _extend(bounds: list[float], x: float, y: float) -> None:
    bounds[0] = min(bounds[0], x)
    bounds[1] = min(bounds[1], y)
    bounds[2] = max(bounds[2], x)
    bounds[3] = max(bounds[3], y)


def footprint_extent(footprint: Footprint) -> Extent:
    """Measure how much room a footprint needs.

    The courtyard is the layer that exists to answer exactly this question -- it is
    the manufacturer's statement of the space the part must be given -- so it is
    used when present. Falling back to pad extents underestimates a part with a
    body wider than its pads, so the fallback adds a margin.
    """
    courtyard = [math.inf, math.inf, -math.inf, -math.inf]
    pads = [math.inf, math.inf, -math.inf, -math.inf]

    for node in footprint.node.items:
        if not isinstance(node, SNode):
            continue
        layer = node.get("layer") or ""
        target = courtyard if layer.endswith(".CrtYd") else None

        if node.name in ("fp_line", "fp_rect") and target is not None:
            for corner in ("start", "end"):
                point = node.child(corner)
                if point is not None:
                    _extend(target, float(point.value(0) or 0), float(point.value(1) or 0))
        elif node.name in ("fp_poly", "fp_circle") and target is not None:
            pts = node.child("pts")
            if pts is not None:
                for xy in pts.children("xy"):
                    _extend(target, float(xy.value(0) or 0), float(xy.value(1) or 0))
        elif node.name == "pad":
            at = node.child("at")
            size = node.child("size")
            if at is None or size is None:
                continue
            px, py = float(at.value(0) or 0), float(at.value(1) or 0)
            half_w = float(size.value(0) or 0) / 2
            half_h = float(size.value(1) or 0) / 2
            rotation = float(at.value(2) or 0) % 180
            if abs(rotation - 90) < 1e-6:
                half_w, half_h = half_h, half_w
            _extend(pads, px - half_w, py - half_h)
            _extend(pads, px + half_w, py + half_h)

    if courtyard[0] != math.inf:
        return Extent(*courtyard)
    if pads[0] != math.inf:
        return Extent(*pads).padded(0.5)
    return Extent(-1.0, -1.0, 1.0, 1.0)


def footprint_pad_nets(footprint: Footprint) -> tuple[str, ...]:
    """Pad numbers in the order they appear, duplicates included."""
    return tuple(
        value for pad in footprint.node.children("pad") if (value := pad.value(0))
    )
