"""Checking that parts bind to KiCad symbols and footprints that actually exist.

A part declares a symbol, a footprint, and a pinout. All three can drift: a symbol
gets renamed between KiCad versions, a footprint moves library, a datasheet is
transcribed with a pin missing. Every one of those is cheap to catch here and
expensive to catch later -- a pin-number mismatch between the part database and the
symbol produces a schematic that silently connects the wrong things.

These checks need KiCad's libraries on disk. When they are absent the checks skip
with an explicit note rather than failing, so CI without KiCad still runs.
"""

from __future__ import annotations

from collections.abc import Mapping

from aipcb.diagnostics import Report, summarise
from aipcb.kicad.footprints import FootprintNotFound, default_footprint_dirs, resolve_footprint
from aipcb.kicad.symbols import SymbolNotFound, default_symbol_dirs, resolve_symbol
from aipcb.loader import LoadedDesign
from aipcb.model.design import Component
from aipcb.model.parts import Part
from aipcb.source import Loc

__all__ = ["check_kicad_bindings", "libraries_available"]


def libraries_available() -> bool:
    """True when KiCad's stock symbol and footprint libraries can be found."""
    return bool(default_symbol_dirs()) and bool(default_footprint_dirs())


def check_kicad_bindings(loaded: LoadedDesign, report: Report) -> None:
    """Verify every part used by the design against the installed KiCad libraries."""
    if not libraries_available():
        report.info(
            "kicad-libraries-missing",
            "KiCad's symbol and footprint libraries were not found, so symbol and "
            "footprint bindings were not verified",
            hint="install KiCad, or set AIPCB_SYMBOL_DIR and AIPCB_FOOTPRINT_DIR",
        )
        return

    used = _parts_in_use(loaded)
    for name in sorted(used):
        part = loaded.parts.get(name)
        if part is None:
            continue
        loc = loaded.part_sources.get(name)
        path: tuple[str | int, ...] = ("parts", name)
        _check_symbol(name, part, loc, path, report)
        _check_footprint(name, part, loc, path, report)


def _parts_in_use(loaded: LoadedDesign) -> set[str]:
    """Only check parts the design actually references.

    A shared library may hold parts for boards that are not being built; reporting
    on those would bury the design's own problems.
    """
    used: set[str] = set()

    def collect(components: Mapping[str, Component]) -> None:
        for component in components.values():
            # A templated part name is only known once the module is instantiated;
            # those are picked up from the instantiation site below.
            if "{{" not in component.part:
                used.add(component.part)

    collect(loaded.design.components)
    for module in loaded.design.modules.values():
        collect(module.components)
    # Parts supplied as module arguments are named at the instantiation site.
    for instance in loaded.design.instances.values():
        for value in instance.params.values():
            if isinstance(value, str) and value in loaded.parts:
                used.add(value)
    for module in loaded.design.modules.values():
        for parameter in module.params.values():
            if isinstance(parameter.default, str) and parameter.default in loaded.parts:
                used.add(parameter.default)
    return used


def _check_symbol(
    name: str,
    part: Part,
    loc: Loc | None,
    path: tuple[str | int, ...],
    report: Report,
) -> None:
    try:
        symbol = resolve_symbol(part.symbol)
    except SymbolNotFound as exc:
        report.error(
            "unknown-symbol",
            f"part {name!r} binds to symbol {part.symbol!r}: {exc}",
            loc=loc,
            path=(*path, "symbol"),
            hint="check the spelling against KiCad's symbol libraries",
            part=name,
        )
        return

    declared = set(part.pins)
    actual = set(symbol.pin_numbers())

    if missing := sorted(declared - actual, key=str):
        report.error(
            "pin-not-in-symbol",
            f"part {name!r} declares pin{'s' if len(missing) != 1 else ''} "
            f"{', '.join(missing)}, which symbol {part.symbol!r} does not have",
            loc=loc,
            path=(*path, "pins"),
            hint=f"the symbol's pins are: {', '.join(sorted(actual, key=str))}",
            part=name,
        )
    if extra := sorted(actual - declared, key=str):
        report.warning(
            "pin-missing-from-part",
            f"symbol {part.symbol!r} has {len(extra)} pin"
            f"{'s' if len(extra) != 1 else ''} that part {name!r} does not declare: "
            f"{summarise(extra, 10)}",
            loc=loc,
            path=(*path, "pins"),
            hint="designs cannot connect to an undeclared pin; add it, or leave it "
            "out deliberately if the package really has no such pin",
            part=name,
        )


def _check_footprint(
    name: str,
    part: Part,
    loc: Loc | None,
    path: tuple[str | int, ...],
    report: Report,
) -> None:
    try:
        footprint = resolve_footprint(part.footprint)
    except FootprintNotFound as exc:
        report.error(
            "unknown-footprint",
            f"part {name!r} binds to footprint {part.footprint!r}: {exc}",
            loc=loc,
            path=(*path, "footprint"),
            hint="check the spelling against KiCad's footprint libraries",
            part=name,
        )
        return

    pads = set(footprint.pad_numbers())
    if not pads:
        return
    if missing := sorted(set(part.pins) - pads, key=str):
        report.error(
            "pin-not-in-footprint",
            f"part {name!r} declares pin{'s' if len(missing) != 1 else ''} "
            f"{', '.join(missing)}, which footprint {part.footprint!r} has no pad for",
            loc=loc,
            path=(*path, "footprint"),
            hint=f"the footprint's pads are: {', '.join(sorted(pads, key=str))}",
            part=name,
        )
