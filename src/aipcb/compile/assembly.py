# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Assembly outputs: the BOM and the pick-and-place file an assembler accepts (M21).

A fabricator takes Gerbers. An *assembler* takes two more files, and they are the
ones this module writes: a bill of materials saying what to buy, and a centroid
file -- CPL, pick-and-place, XYRS, the name depends on who you ask -- saying where
each part goes and which way round it faces.

**The coordinates are KiCad's, not ours.** :func:`read_positions` parses the
placement CSV ``kicad-cli pcb export pos`` already produces, rather than deriving
positions from the board a second time. That is not laziness: the Gerbers are
plotted with ``--use-drill-file-origin`` and the placement file with the same, so
the two agree by construction. A second derivation would be a second chance to
disagree with the copper, and a centroid file that disagrees with the copper is how
a reel of capacitors ends up 0.5 mm from its pads.

**What is fab-specific and what is not.** Every assembler wants the same five facts
about placement and roughly the same four about purchasing. They spell them
differently, they disagree about which parts belong in the centroid at all, and one
of them wants a column the other has never heard of. So the *rows* are computed
once, here, and the *formats* are a table of column names and small per-fab rules --
see :data:`FORMATS`. ADR 0015 records what was measured about each.

**Rotation is the part to be afraid of, and this module does not pretend to solve
it.** See :func:`cpl_rows` for the convention, and ADR 0015 §4 for why no
correction table ships with this project.
"""

from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode
from aipcb.model.parts import Assembly, Part
from aipcb.netlist import Netlist

__all__ = [
    "FORMATS",
    "AssemblyFormat",
    "PlacedPart",
    "assembly_of",
    "bom_rows",
    "cpl_rows",
    "export_assembly",
    "overlay_svg",
    "read_positions",
    "write_bom",
    "write_cpl",
]


@dataclass(frozen=True, slots=True)
class PlacedPart:
    """One component, as both files need to see it."""

    refdes: str
    value: str
    """What the schematic calls it. The fab's "Comment" column."""
    package: str
    """The footprint's own name, without its library prefix."""
    footprint: str
    """The full KiCad library id."""
    x: float
    y: float
    rotation: float
    side: str
    """``top`` or ``bottom``, as KiCad spells it."""
    assembly: Assembly
    placed: bool = True
    """Whether KiCad gave this part a row in the placement file.

    A footprint marked "exclude from position files" -- a card-edge finger field,
    a fiducial, a bit of mounting hardware -- has no centroid and belongs in no
    centroid file. It may still be something to *buy*, so it stays on the bill.
    """
    mpn: str | None = None
    manufacturer: str | None = None
    supplier_refs: dict[str, str] = field(default_factory=dict)
    description: str | None = None

    @property
    def fitted(self) -> bool:
        """Whether a machine is meant to place this part."""
        return self.assembly in (Assembly.SMT, Assembly.THT)


@dataclass(frozen=True, slots=True)
class AssemblyFormat:
    """One assembler's idea of what these two files look like."""

    name: str
    bom_columns: tuple[str, ...]
    cpl_columns: tuple[str, ...]
    side_names: tuple[str, str]
    """What this fab calls the top and bottom sides, in that order."""
    supplier_key: str | None = None
    """Which entry of ``supplier_refs`` fills this fab's own part-number column."""
    centroid_is_smt_only: bool = False
    """PCBWay: "Only surface mounting parts are listed in the Centroid.\""""
    bom_includes_dnp: bool = True
    """Whether a do-not-populate part appears on the BOM, marked, or not at all."""
    max_designators_per_line: int | None = None
    """JLCPCB documents a limit of 200. Exceeding it is a rejected upload."""


#: The three output shapes, measured from each fab's published requirements rather
#: than inferred from somebody's exporter. Sources are in ADR 0015 §2.
#:
#: `generic` is not a fab. It is every field this project knows, in a stable order,
#: for the assembler who has their own template or who wants to see everything --
#: and it is the format that never drops a part, which makes it the one to read when
#: a fab-specific file looks short.
FORMATS: dict[str, AssemblyFormat] = {
    "jlcpcb": AssemblyFormat(
        name="jlcpcb",
        bom_columns=("Comment", "Designator", "Footprint", "JLCPCB Part #"),
        cpl_columns=("Designator", "Mid X", "Mid Y", "Rotation", "Layer"),
        side_names=("Top", "Bottom"),
        supplier_key="lcsc",
        max_designators_per_line=200,
    ),
    "pcbway": AssemblyFormat(
        name="pcbway",
        bom_columns=(
            "Line#",
            "Quantity Per Part Number",
            "Reference Designator",
            "Part Number",
            "Part Description",
            "Package",
            "Type",
            "Manufacturers Name",
            "Manufacturers Part Number",
            "Distributors Part Number",
        ),
        cpl_columns=("Designator", "X", "Y", "Rotation", "Side"),
        side_names=("Top", "Bottom"),
        centroid_is_smt_only=True,
    ),
    "generic": AssemblyFormat(
        name="generic",
        bom_columns=(
            "Designators",
            "Quantity",
            "Value",
            "Footprint",
            "Package",
            "Assembly",
            "Manufacturer",
            "MPN",
            "Supplier Refs",
            "Description",
        ),
        cpl_columns=(
            "Designator", "X (mm)", "Y (mm)", "Rotation (deg)", "Side", "Assembly",
        ),
        side_names=("top", "bottom"),
    ),
}

#: KiCad's placement CSV, as `kicad-cli pcb export pos --format csv` writes it.
_POS_COLUMNS = ("Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side")

#: One row of it: reference, value, package, x, y, rotation, side.
PositionRow = tuple[str, str, str, float, float, float, str]


def _natural(refdes: str) -> tuple[str, int, str]:
    """Sort key that puts C2 before C10, which no plain string sort does.

    Assemblers read these files with their eyes as well as their machines, and a
    designator column that runs C1, C10, C11, C2 reads as a mistake even when it is
    not. It also has to be *stable*: the same board must produce the same file
    byte for byte, so the fallback is the whole string rather than anything
    locale-dependent.
    """
    match = re.match(r"^([A-Za-z_]*)(\d*)(.*)$", refdes)
    if match is None:  # pragma: no cover - the regex matches every string
        return (refdes, 0, "")
    prefix, digits, rest = match.groups()
    return (prefix, int(digits) if digits else 0, rest)


def read_positions(pos_csv: Path) -> list[PositionRow]:
    """Parse KiCad's placement CSV into tuples, in the order KiCad wrote them.

    Kept deliberately dumb. Everything this module knows about *where* a part is
    comes through here, so the one thing this function must not do is compute.
    """
    rows: list[PositionRow] = []
    with pos_csv.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in _POS_COLUMNS if c not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(
                f"{pos_csv.name} is not a KiCad placement file: no "
                f"{', '.join(missing)} column"
            )
        for row in reader:
            rows.append(
                (
                    row["Ref"],
                    row["Val"],
                    row["Package"],
                    float(row["PosX"]),
                    float(row["PosY"]),
                    float(row["Rot"]),
                    row["Side"].strip().lower(),
                )
            )
    return rows


def assembly_of(part: Part | None, board: SNode, refdes: str) -> Assembly:
    """What the assembler should do with this part, declared or measured.

    A declared ``assembly:`` wins. When the part says nothing, the answer is read
    off the footprint KiCad actually placed: its ``(attr ...)`` flags say ``smd``
    or ``through_hole``, and where they say neither the pads themselves do.

    **This is why the field does not simply default to ``smt``.** Four of this
    repository's bundled examples are built around through-hole parts -- the
    breakout headers in `congestion`, the DIP-8 in `mcu-4layer` whose two rows of
    pins are the obstacle that board exists to route around, the IDC and pin
    headers in `backplane`, the power header in `ldo-supply`. A default of ``smt``
    would have told PCBWay to feed all of them to a placement machine, and PCBWay's
    centroid file is documented as surface-mount only, so the error would have been
    silent in exactly the file that is supposed to catch it.
    """
    if part is not None and part.assembly is not None:
        return part.assembly
    if part is not None and part.dnp:
        return Assembly.DNP
    footprint = _footprint_of(board, refdes)
    if footprint is None:
        return Assembly.SMT
    attr = footprint.child("attr")
    flags = {a.value for a in attr.atoms()} if attr is not None else set()
    if "through_hole" in flags:
        return Assembly.THT
    if "smd" in flags:
        return Assembly.SMT
    # No attribute either way: ask the pads. A footprint with any plated
    # through-hole pad is one a pick-and-place machine cannot fit.
    for pad in footprint.children("pad"):
        if (pad.value(1) or "") in ("thru_hole", "np_thru_hole"):
            return Assembly.THT
    return Assembly.SMT


def _footprint_of(board: SNode, refdes: str) -> SNode | None:
    for footprint in board.children("footprint"):
        for prop in footprint.children("property"):
            if prop.value(0) == "Reference" and prop.value(1) == refdes:
                return footprint
    return None


def placed_parts(
    board: SNode, netlist: Netlist, positions: Sequence[PositionRow]
) -> list[PlacedPart]:
    """Join the design's components to KiCad's placement rows.

    **Driven by the netlist and not by the placement file**, which is the direction
    that cannot lose a part. KiCad omits a footprint from the position file whenever
    it is marked "exclude from position files", and that mark says "no machine
    places this" -- it does not say "nobody buys this". A battery holder, a heatsink,
    a card-edge finger field: the first two are on the bill and off the centroid,
    the third is on neither, and only the design knows which is which.

    Anything in the placement file that the design does not know about is kept as
    well, because a part on the board and not in the netlist is a discrepancy the
    assembler should see rather than one this function should hide.
    """
    by_refdes = {row[0]: row for row in positions}
    parts: list[PlacedPart] = []
    seen: set[str] = set()
    order = [*netlist.components, *(r for r in by_refdes if r not in netlist.components)]
    for refdes in order:
        if refdes in seen:
            continue
        seen.add(refdes)
        row = by_refdes.get(refdes)
        _, value, package, x, y, rotation, side = row or (
            refdes, "", "", 0.0, 0.0, 0.0, "top",
        )
        component = netlist.components.get(refdes)
        part = component.part if component is not None else None
        mpn, manufacturer = part.procurement() if part is not None else (None, None)
        kind = assembly_of(part, board, refdes)
        if component is not None and component.dnp and kind is not Assembly.NONE:
            # A design may mark one *instance* do-not-populate without the part
            # itself being a DNP part. The instance wins: it is the more specific
            # statement, and it is the one on this board.
            kind = Assembly.DNP
        parts.append(
            PlacedPart(
                refdes=refdes,
                # What a human reads in the BOM's "Comment" column, and what an
                # assembler quotes an unmarked passive from. The source's own
                # display value first -- "100n" -- because the board and the
                # schematic both carry the *part name* there ("C_100N_0603"),
                # which is this project's identifier and not a component value.
                value=(
                    (component.value if component is not None else None)
                    or (part.value if part is not None else None)
                    or value
                ),
                package=package or (part.footprint.split(":")[-1] if part else ""),
                footprint=(part.footprint if part is not None else package) or package,
                x=x,
                y=y,
                rotation=rotation,
                side=side,
                assembly=kind,
                placed=row is not None,
                mpn=mpn,
                manufacturer=manufacturer,
                supplier_refs=dict(part.supplier_refs) if part is not None else {},
                description=part.description if part is not None else None,
            )
        )
    return sorted(parts, key=lambda p: _natural(p.refdes))


def _normalise_rotation(degrees: float) -> float:
    """Fold a rotation into [0, 360), which is the range every fab's sample uses.

    KiCad writes signed degrees, counter-clockwise positive -- `-90` where JLCPCB's
    documented convention would write `270`. The two describe the same physical
    orientation, and folding is a presentation choice rather than a correction: no
    part turns.
    """
    folded = math.fmod(degrees, 360.0)
    if folded < 0:
        folded += 360.0
    # -0.0 and 360.0 both round to a value that should print as 0.
    return 0.0 if math.isclose(folded, 360.0, abs_tol=1e-9) else folded + 0.0


def cpl_rows(parts: Iterable[PlacedPart], fmt: AssemblyFormat) -> list[dict[str, str]]:
    """The centroid file's rows, in this fab's spelling.

    **The rotation convention, and what this project does and does not claim.**
    Both fabs researched for ADR 0015 document the same convention KiCad already
    writes: degrees, counter-clockwise positive. So the *convention* needs no
    transform and none is applied -- only a fold into [0, 360).

    What differs, and what has broken real boards, is the *zero reference*: a fab
    whose parts library holds a package at a different orientation than the KiCad
    footprint does needs a per-package offset, and neither fab publishes that
    offset as data. Community projects maintain lookup tables of it and are candid
    that the tables are incomplete and that two components sharing a footprint can
    still need different values. **So no correction table ships here.** A part that
    needs one declares it, and the overlay in :func:`overlay_svg` is what a human
    checks before spending money. ADR 0015 §4 has the argument and the sources.
    """
    rows: list[dict[str, str]] = []
    for part in parts:
        if not part.fitted or not part.placed:
            continue
        if fmt.centroid_is_smt_only and part.assembly is not Assembly.SMT:
            continue
        top, bottom = fmt.side_names
        values = {
            "designator": part.refdes,
            "x": f"{part.x:.4f}",
            "y": f"{part.y:.4f}",
            "rotation": f"{_normalise_rotation(part.rotation):.4f}",
            "side": top if part.side == "top" else bottom,
            "assembly": part.assembly.value,
        }
        by_column = {
            "Designator": values["designator"],
            "Mid X": values["x"],
            "Mid Y": values["y"],
            "X": values["x"],
            "Y": values["y"],
            "X (mm)": values["x"],
            "Y (mm)": values["y"],
            "Rotation": values["rotation"],
            "Rotation (deg)": values["rotation"],
            "Layer": values["side"],
            "Side": values["side"],
            "Assembly": values["assembly"],
        }
        rows.append({column: by_column[column] for column in fmt.cpl_columns})
    return rows


@dataclass(frozen=True, slots=True)
class BomLine:
    """One purchasable line: a part, and every designator that wants one."""

    designators: tuple[str, ...]
    part: PlacedPart

    @property
    def quantity(self) -> int:
        return len(self.designators)


def bom_lines(parts: Iterable[PlacedPart]) -> list[BomLine]:
    """Group placed parts into purchase lines, deterministically.

    Grouped by what a buyer actually orders -- the manufacturer part number where
    there is one, and the value-and-footprint pair where there is not, which is how
    an assembler quotes an unmarked passive. Parts marked ``none`` are not parts and
    never appear; ``dnp`` lines are kept here and dropped per format later.

    **The assembly kind is part of the key**, and it has to be. Two parts can share
    a part number and still need different lines: one fitted and one
    do-not-populate is the common case, and merging them puts a DNP marking on
    components that must be fitted or takes it off components that must not be.
    PCBWay's ``Type`` column has the same problem from the other end -- one line
    cannot be both surface-mount and through-hole.
    """
    groups: dict[tuple[str, ...], list[PlacedPart]] = {}
    for part in parts:
        if part.assembly is Assembly.NONE:
            continue
        identity = (part.mpn,) if part.mpn else ("", part.value, part.footprint)
        key = (part.assembly.value, *identity)
        groups.setdefault(tuple(k or "" for k in key), []).append(part)
    lines = [
        BomLine(
            designators=tuple(sorted((p.refdes for p in members), key=_natural)),
            part=sorted(members, key=lambda p: _natural(p.refdes))[0],
        )
        for members in groups.values()
    ]
    return sorted(lines, key=lambda line: _natural(line.designators[0]))


def bom_rows(parts: Iterable[PlacedPart], fmt: AssemblyFormat) -> list[dict[str, str]]:
    """The bill of materials, in this fab's spelling."""
    rows: list[dict[str, str]] = []
    for index, line in enumerate(bom_lines(parts), start=1):
        part = line.part
        if part.assembly is Assembly.DNP and not fmt.bom_includes_dnp:
            continue
        designators = ",".join(line.designators)
        supplier_ref = (
            part.supplier_refs.get(fmt.supplier_key, "") if fmt.supplier_key else ""
        )
        comment = part.value
        if part.assembly is Assembly.DNP:
            # Said in the column a human reads, because neither fab documents a
            # machine-readable way to say it and a silent DNP is a fitted part.
            comment = f"{comment} (DNP - DO NOT POPULATE)"
        by_column = {
            "Comment": comment,
            "Designator": designators,
            "Designators": designators,
            "Reference Designator": designators,
            "Footprint": part.footprint,
            "Package": part.package,
            "JLCPCB Part #": supplier_ref,
            "Line#": str(index),
            "Quantity": str(line.quantity),
            "Quantity Per Part Number": str(line.quantity),
            "Part Number": part.mpn or "",
            "Part Description": part.description or part.value,
            "Type": _pcbway_type(part.assembly),
            "Manufacturers Name": part.manufacturer or "",
            "Manufacturers Part Number": part.mpn or "",
            "Distributors Part Number": ";".join(
                f"{k}:{v}" for k, v in sorted(part.supplier_refs.items())
            ),
            "Value": part.value,
            "Assembly": part.assembly.value,
            "Manufacturer": part.manufacturer or "",
            "MPN": part.mpn or "",
            "Supplier Refs": ";".join(
                f"{k}:{v}" for k, v in sorted(part.supplier_refs.items())
            ),
            "Description": part.description or "",
        }
        rows.append({column: by_column[column] for column in fmt.bom_columns})
    return rows


def _pcbway_type(kind: Assembly) -> str:
    """PCBWay's `Type` column: "Surface mount, Thru-hole or Hybrid"."""
    if kind is Assembly.THT:
        return "Thru-hole"
    if kind is Assembly.DNP:
        return "DNP"
    return "Surface mount"


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[dict[str, str]]) -> None:
    """One CSV writer, with the line ending pinned so two runs match byte for byte."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def write_bom(path: Path, parts: Iterable[PlacedPart], fmt: AssemblyFormat) -> Path:
    _write_csv(path, fmt.bom_columns, bom_rows(parts, fmt))
    return path


def write_cpl(path: Path, parts: Iterable[PlacedPart], fmt: AssemblyFormat) -> Path:
    _write_csv(path, fmt.cpl_columns, cpl_rows(parts, fmt))
    return path


# -- the overlay ------------------------------------------------------------------
#
# Drawn from the CPL rows and from nothing else, which is the whole point of it. An
# overlay derived from the board would show that the board is right; what a person
# is about to spend money on is the *file*, so the file is what gets drawn.

#: The board outline is sketched in the placement file's own frame. KiCad stores
#: geometry with Y increasing downwards from the sheet corner; the placement file
#: is measured from the drill origin with Y increasing upwards, which is the
#: transform measured in ADR 0015 §3 and reproduced here.
_EDGE_LAYER = "Edge.Cuts"


def _drill_origin(board: SNode) -> tuple[float, float]:
    setup = board.child("setup")
    node = setup.child("aux_axis_origin") if setup is not None else None
    if node is None:
        return (0.0, 0.0)
    atoms = node.atoms()
    return (float(atoms[0].value), float(atoms[1].value))


def _edge_segments(board: SNode) -> list[tuple[float, float, float, float]]:
    """Every Edge.Cuts segment, in KiCad's own coordinates.

    Arcs are drawn as their chord. This is a sketch a human checks part positions
    against, not a fabrication outline -- the Gerber is the fabrication outline --
    and a chord is close enough to tell "inside the board" from "off the edge".
    """
    segments: list[tuple[float, float, float, float]] = []

    def on_edge(node: SNode) -> bool:
        layer = node.child("layer")
        return layer is not None and layer.value(0) == _EDGE_LAYER

    def point(node: SNode | None) -> tuple[float, float] | None:
        if node is None:
            return None
        atoms = node.atoms()
        if len(atoms) < 2:
            return None
        return (float(atoms[0].value), float(atoms[1].value))

    for node in board.children():
        if node.name not in ("gr_line", "gr_arc", "gr_rect", "gr_poly") or not on_edge(node):
            continue
        if node.name in ("gr_line", "gr_arc"):
            start, end = point(node.child("start")), point(node.child("end"))
            if start and end:
                segments.append((*start, *end))
        elif node.name == "gr_rect":
            start, end = point(node.child("start")), point(node.child("end"))
            if start and end:
                (x1, y1), (x2, y2) = start, end
                segments.extend(
                    [(x1, y1, x2, y1), (x2, y1, x2, y2), (x2, y2, x1, y2), (x1, y2, x1, y1)]
                )
        else:
            pts_node = node.child("pts")
            found = [point(p) for p in pts_node.children("xy")] if pts_node else []
            corners: list[tuple[float, float]] = [c for c in found if c is not None]
            if not corners:
                continue
            for first, second in zip(corners, [*corners[1:], corners[0]], strict=True):
                segments.append((first[0], first[1], second[0], second[1]))
    return segments


def overlay_svg(
    parts: Sequence[PlacedPart], board: SNode, side: str, *, title: str = ""
) -> str:
    """An eyeball check of one side of the placement file.

    Every part the CPL puts on this side, drawn where the CPL puts it, turned the
    way the CPL turns it, with its designator beside it and a dot marking where pin
    one ends up after that rotation. The dot is the point of the picture: a file
    can say `180` and look perfectly ordinary, and the only cheap way to notice
    that a diode is backwards is to see its cathode on the wrong end.

    The bottom side is drawn *as seen through the board* -- mirrored -- because that
    is how a bottom-side placement is inspected in the real world, with the board
    turned over.
    """
    origin_x, origin_y = _drill_origin(board)

    def to_frame(x: float, y: float) -> tuple[float, float]:
        return (x - origin_x, origin_y - y)

    edges = [
        (*to_frame(x1, y1), *to_frame(x2, y2))
        for x1, y1, x2, y2 in _edge_segments(board)
    ]
    here = [p for p in parts if p.side == side and p.fitted]
    xs = [v for e in edges for v in (e[0], e[2])] + [p.x for p in here]
    ys = [v for e in edges for v in (e[1], e[3])] + [p.y for p in here]
    if not xs or not ys:
        xs, ys = [0.0, 10.0], [0.0, 10.0]
    pad = 4.0
    min_x, max_x = min(xs) - pad, max(xs) + pad
    min_y, max_y = min(ys) - pad, max(ys) + pad
    width, height = max_x - min_x, max_y - min_y
    scale = 8.0                       # px per mm; legible at a glance, still small
    mirrored = side == "bottom"

    def place(x: float, y: float) -> tuple[float, float]:
        """Millimetres in the placement frame to SVG user units, Y down."""
        px = (max_x - x if mirrored else x - min_x) * scale
        return (px, (max_y - y) * scale)

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width * scale:.1f}" '
        f'height="{height * scale + 26:.1f}" '
        f'viewBox="0 0 {width * scale:.1f} {height * scale + 26:.1f}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="6" y="16" font-family="sans-serif" font-size="12" fill="#111">'
        f'{_escape(title or side)} &#8212; {len(here)} parts, from the placement file'
        f'{" (mirrored, as seen through the board)" if mirrored else ""}</text>',
        '<g transform="translate(0,26)">',
    ]
    for x1, y1, x2, y2 in edges:
        ax, ay = place(x1, y1)
        bx, by = place(x2, y2)
        out.append(
            f'<line x1="{ax:.2f}" y1="{ay:.2f}" x2="{bx:.2f}" y2="{by:.2f}" '
            f'stroke="#444" stroke-width="1.2"/>'
        )
    body = 1.1 * scale               # the marker, in px
    for part in here:
        cx, cy = place(part.x, part.y)
        # The placement file's rotation is counter-clockwise positive in a Y-up
        # frame; SVG's is clockwise positive in a Y-down one, so the sign flips.
        # A mirrored view flips it back.
        turn = part.rotation if mirrored else -part.rotation
        colour = "#b03030" if part.side == "top" else "#2f5fa8"
        out.append(f'<g transform="translate({cx:.2f},{cy:.2f}) rotate({turn:.2f})">')
        out.append(
            f'<rect x="{-body:.2f}" y="{-body * 0.6:.2f}" width="{body * 2:.2f}" '
            f'height="{body * 1.2:.2f}" fill="none" stroke="{colour}" stroke-width="1"/>'
        )
        out.append(
            f'<circle cx="{-body:.2f}" cy="{-body * 0.6:.2f}" r="{scale * 0.22:.2f}" '
            f'fill="{colour}"/>'
        )
        out.append(
            f'<line x1="0" y1="0" x2="{body * 1.6:.2f}" y2="0" stroke="{colour}" '
            f'stroke-width="1"/>'
        )
        out.append("</g>")
        out.append(
            f'<text x="{cx + body * 1.3:.2f}" y="{cy - body * 0.9:.2f}" '
            f'font-family="sans-serif" font-size="{scale * 1.1:.1f}" fill="#111">'
            f'{_escape(part.refdes)}</text>'
        )
    out.append("</g></svg>")
    return "\n".join(out) + "\n"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# -- the bundle -------------------------------------------------------------------


@dataclass(slots=True)
class AssemblyResult:
    """What an assembly export produced, and what it could not vouch for."""

    directory: Path
    files: list[Path] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    parts: list[PlacedPart] = field(default_factory=list)
    missing_mpn: list[str] = field(default_factory=list)
    """Designators an assembler cannot source. Empty is the bar for a real order."""
    ok: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "formats": sorted(self.formats),
            "files": sorted(p.name for p in self.files),
            "parts": len(self.parts),
            "fitted": sum(1 for p in self.parts if p.fitted),
            "missing_mpn": sorted(self.missing_mpn, key=_natural),
            "ok": self.ok,
        }


def export_assembly(
    board_path: Path,
    board: SNode,
    position_csv: Path,
    out_dir: Path,
    netlist: Netlist,
    report: Report,
    *,
    formats: Sequence[str] = ("generic",),
    bundle: bool = False,
) -> AssemblyResult:
    """Write the BOM, the CPL and the overlays for one board.

    ``position_csv`` is the placement file the Gerber export already produced. It is
    required rather than regenerated, so that the assembly package and the
    fabrication package are two views of one measurement.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result = AssemblyResult(directory=out_dir)
    parts = placed_parts(board, netlist, read_positions(position_csv))
    result.parts = parts

    # M21c's back-side guard. `side: back` validates and warns in the mechanical
    # model and then places on the front (M9), so a board that declares one has a
    # placement file that does not describe it. Refusing is the only honest answer:
    # an assembly package that quietly puts a back-side part on the front is worse
    # than no package.
    on_back = [p.refdes for p in parts if p.side == "bottom"]
    if on_back:
        report.error(
            "assembly-back-side-unsupported",
            f"{', '.join(sorted(on_back, key=_natural))} "
            f"{'is' if len(on_back) == 1 else 'are'} on the back of the board, and "
            "two-sided assembly export is not verified",
            hint="`side: back` is validated but not implemented (M9): the placer "
            "puts the part on the front, so a back-side placement file would not "
            "describe the board that was built. See docs/roadmap.md.",
        )
        result.ok = False
        return result

    for name in formats:
        fmt = FORMATS[name]
        stem = board_path.stem
        result.files.append(
            write_bom(out_dir / f"{stem}-bom-{fmt.name}.csv", parts, fmt)
        )
        result.files.append(
            write_cpl(out_dir / f"{stem}-cpl-{fmt.name}.csv", parts, fmt)
        )
        result.formats.append(fmt.name)

    for side in ("top", "bottom"):
        if not any(p.side == side and p.fitted for p in parts):
            continue
        target = out_dir / f"{board_path.stem}-placement-{side}.svg"
        target.write_text(
            overlay_svg(parts, board, side, title=f"{netlist.name} {side}"),
            encoding="utf-8",
        )
        result.files.append(target)

    for name in formats:
        fmt = FORMATS[name]
        if fmt.max_designators_per_line is None:
            continue
        over = [
            line.designators[0]
            for line in bom_lines(parts)
            if len(line.designators) > fmt.max_designators_per_line
        ]
        if over:
            report.warning(
                "assembly-bom-line-too-long",
                f"{len(over)} {fmt.name} bill-of-materials "
                f"{'line has' if len(over) == 1 else 'lines have'} more than "
                f"{fmt.max_designators_per_line} designators, which {fmt.name} "
                f"rejects: {', '.join(over)} and the parts grouped with them",
                hint="split the part into more than one library entry, or order "
                "that line separately",
            )

    result.missing_mpn = [p.refdes for p in parts if p.fitted and not p.mpn]
    if result.missing_mpn and any(f != "generic" for f in formats):
        listed = ", ".join(sorted(result.missing_mpn, key=_natural))
        report.warning(
            "assembly-missing-mpn",
            f"{len(result.missing_mpn)} placed "
            f"{'part has' if len(result.missing_mpn) == 1 else 'parts have'} no "
            f"manufacturer part number: {listed}",
            hint="an assembler cannot source these. Add `mpn:` (and the fab's own "
            "`supplier_refs:` id) to the part in its library file. The board and "
            "the placement file are unaffected.",
        )

    if bundle:
        result.files.append(_zip(board_path, out_dir, result))
    result.files = sorted(set(result.files))
    return result


def _zip(board_path: Path, out_dir: Path, result: AssemblyResult) -> Path:
    """One archive of everything an assembler is sent.

    Written with fixed timestamps and sorted members, because a package whose bytes
    change when nothing changed cannot be diffed -- and this repository's whole
    posture is that a rebuilt artefact is byte-identical or it is a change.
    """
    target = out_dir / f"{board_path.stem}-assembly.zip"
    members = sorted(p for p in result.files if p.is_file() and p != target)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return target
