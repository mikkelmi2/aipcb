"""Reading KiCad symbol libraries (``.kicad_sym``).

Two things need this. The part database wants to *verify* that a part's declared
pins actually match the KiCad symbol it binds to, so a design fails at
``aipcb validate`` rather than at ERC. And the schematic writer needs the symbol's
full definition, because a ``.kicad_sch`` embeds a copy of every symbol it uses in
its ``lib_symbols`` block.

Both are served by keeping the parsed :class:`~aipcb.kicad.sexpr.SNode` around
rather than converting it into a private model: the definition we embed is then
byte-for-byte what the library says, including graphics we have no interest in
understanding.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from aipcb.kicad.sexpr import Atom, SExprError, SNode, parse

__all__ = [
    "SYMBOL_DIR_ENV",
    "Symbol",
    "SymbolLibrary",
    "SymbolNotFound",
    "SymbolPin",
    "default_symbol_dirs",
    "resolve_symbol",
]

#: Overrides where symbol libraries are looked up.
SYMBOL_DIR_ENV = "AIPCB_SYMBOL_DIR"

_STANDARD_DIRS = (
    "/usr/share/kicad/symbols",
    "/usr/local/share/kicad/symbols",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
    "C:/Program Files/KiCad/9.0/share/kicad/symbols",
    "C:/Program Files/KiCad/8.0/share/kicad/symbols",
)


class SymbolNotFound(LookupError):
    """A symbol library or symbol could not be located."""


@dataclass(frozen=True, slots=True)
class SymbolPin:
    """One pin of a KiCad symbol, including where it sits in library space."""

    number: str
    name: str
    type: str
    """KiCad's electrical type: ``power_in``, ``passive``, ``bidirectional``, …"""
    unit: int = 1
    x: float = 0.0
    y: float = 0.0
    angle: float = 0.0
    """Direction from the pin's connection end *toward* the symbol body, in degrees."""
    length: float = 0.0

    @property
    def is_power(self) -> bool:
        return self.type in ("power_in", "power_out")

    @property
    def outward_angle(self) -> float:
        """The direction leading away from the body, where a stub and label belong."""
        return (self.angle + 180.0) % 360.0


@dataclass(frozen=True, slots=True)
class Symbol:
    """A symbol definition, plus the raw node for embedding into a schematic."""

    name: str
    library: str
    node: SNode
    pins: tuple[SymbolPin, ...]
    extends: str | None = None

    @property
    def lib_id(self) -> str:
        return f"{self.library}:{self.name}"

    def pin(self, number: str) -> SymbolPin | None:
        return next((p for p in self.pins if p.number == number), None)

    def pin_numbers(self) -> tuple[str, ...]:
        return tuple(p.number for p in self.pins)

    def get_property(self, key: str) -> str | None:
        """Read a symbol property such as ``Reference`` or ``Datasheet``."""
        for node in self.node.children("property"):
            atoms = node.atoms()
            if len(atoms) >= 2 and atoms[0].value == key:
                return atoms[1].value
        return None

    @property
    def reference_prefix(self) -> str:
        """The designator letter KiCad suggests for this symbol, e.g. ``U`` or ``R``."""
        value = self.get_property("Reference") or "U"
        return value.rstrip("?") or "U"


class SymbolLibrary:
    """One ``.kicad_sym`` file."""

    __slots__ = ("_root", "_symbols", "name", "path")

    def __init__(self, name: str, path: Path, root: SNode) -> None:
        self.name = name
        self.path = path
        self._root = root
        self._symbols: dict[str, SNode] = {}
        for node in root.children("symbol"):
            symbol_name = node.value(0)
            if symbol_name is not None:
                self._symbols[symbol_name] = node

    @classmethod
    def load(cls, path: Path, name: str | None = None) -> SymbolLibrary:
        try:
            root = parse(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SymbolNotFound(f"cannot read symbol library {path}: {exc}") from exc
        except SExprError as exc:
            raise SymbolNotFound(f"{path} is not a valid symbol library: {exc}") from exc
        if root.name != "kicad_symbol_lib":
            raise SymbolNotFound(
                f"{path} is a {root.name!r} file, not a symbol library"
            )
        return cls(name or path.stem, path, root)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._symbols))

    def get(self, name: str) -> Symbol:
        """Return a symbol, following ``extends`` to inherit a base symbol's pins."""
        node = self._symbols.get(name)
        if node is None:
            raise SymbolNotFound(
                f"symbol {name!r} is not in {self.name} "
                f"({len(self._symbols)} symbols available)"
            )
        extends = node.get("extends")
        pins = _collect_pins(node, name)
        if extends and not pins:
            # A derived symbol carries only overridden properties; its geometry and
            # pins come from the base symbol.
            pins = _collect_pins(self._symbols.get(extends, node), extends)
        return Symbol(
            name=name, library=self.name, node=node, pins=pins, extends=extends
        )


def _collect_pins(node: SNode, name: str) -> tuple[SymbolPin, ...]:
    """Gather pins from a symbol's unit sub-symbols.

    KiCad nests pins inside child symbols named ``<symbol>_<unit>_<style>``. Style 0
    holds shared graphics; pins live in the style-1 bodies. We read every nested
    body and key on the pin number, so multi-unit parts (an opamp package with two
    amplifiers) yield one entry per physical pin.
    """
    pins: dict[str, SymbolPin] = {}
    for unit_node in node.children("symbol"):
        unit_name = unit_node.value(0) or ""
        unit_index = _unit_index(unit_name, name)
        for pin_node in unit_node.children("pin"):
            atoms = pin_node.atoms()
            electrical = atoms[0].value if atoms else "unspecified"
            number = pin_node.get("number") or ""
            if not number:
                continue
            pin_name = pin_node.get("name") or number
            at = pin_node.child("at")
            pins.setdefault(
                number,
                SymbolPin(
                    number,
                    pin_name,
                    electrical,
                    unit_index,
                    x=float(at.value(0) or 0.0) if at else 0.0,
                    y=float(at.value(1) or 0.0) if at else 0.0,
                    angle=float(at.value(2) or 0.0) if at else 0.0,
                    length=float(pin_node.get("length") or 0.0),
                ),
            )
    return tuple(sorted(pins.values(), key=lambda p: _pin_sort_key(p.number)))


def _unit_index(unit_name: str, symbol_name: str) -> int:
    suffix = unit_name.removeprefix(symbol_name + "_")
    head = suffix.split("_", 1)[0]
    return int(head) if head.isdigit() else 1


def _pin_sort_key(number: str) -> tuple[int, int, str]:
    """Sort pin ``2`` before ``10``, and numbers before names like ``A1``."""
    if number.isdigit():
        return (0, int(number), "")
    return (1, 0, number)


# ---------------------------------------------------------------------------
# locating libraries
# ---------------------------------------------------------------------------


def default_symbol_dirs() -> tuple[Path, ...]:
    """Where to look for KiCad's stock symbol libraries."""
    override = os.environ.get(SYMBOL_DIR_ENV)
    candidates = [Path(override)] if override else []
    candidates += [Path(p) for p in _STANDARD_DIRS]
    return tuple(p for p in candidates if p.is_dir())


@lru_cache(maxsize=64)
def _load_library(library: str) -> SymbolLibrary:
    for directory in default_symbol_dirs():
        path = directory / f"{library}.kicad_sym"
        if path.is_file():
            return SymbolLibrary.load(path, library)
    searched = ", ".join(str(d) for d in default_symbol_dirs()) or "no directories"
    raise SymbolNotFound(
        f"symbol library {library!r} not found (searched: {searched}). "
        f"Set {SYMBOL_DIR_ENV} if KiCad's libraries live elsewhere."
    )


def resolve_symbol(lib_id: str) -> Symbol:
    """Resolve a ``Library:Symbol`` identifier against the installed libraries."""
    library, _, name = lib_id.partition(":")
    if not library or not name:
        raise SymbolNotFound(
            f"{lib_id!r} is not a library id; expected the form 'Library:Symbol'"
        )
    return _load_library(library).get(name)


def flatten_symbol(symbol: Symbol, library: SymbolLibrary | None = None) -> SNode:
    """Return a self-contained definition suitable for a schematic's ``lib_symbols``.

    A schematic embeds a copy of every symbol it uses, and that copy has to stand on
    its own: KiCad will not go looking for a base symbol that a derived one extends.
    So a derived symbol -- ``ATtiny85-20P extends ATtiny25V-10P`` -- is merged into
    its base here, taking the base's geometry and pins and the derived symbol's
    properties.

    The node is renamed to the full library id, and nested unit bodies are renamed
    to match, because KiCad identifies units by the ``<symbol>_<unit>_<style>``
    convention and a mismatched prefix would leave them orphaned.
    """
    import copy

    node = copy.deepcopy(symbol.node)

    if symbol.extends and library is not None:
        base = copy.deepcopy(library.get(symbol.extends).node)
        # Keep the base's geometry and pins; let the derived symbol's properties win.
        overrides = {
            p.value(0): p for p in node.children("property") if p.value(0) is not None
        }
        merged = SNode(base.name)
        seen: set[str] = set()
        for item in base.items:
            if isinstance(item, SNode) and item.name == "property":
                key = item.value(0)
                if key in overrides:
                    merged.items.append(copy.deepcopy(overrides[key]))
                    seen.add(str(key))
                    continue
            if isinstance(item, SNode) and item.name == "extends":
                continue
            merged.items.append(item)
        for key, prop in overrides.items():
            if key not in seen:
                merged.items.append(copy.deepcopy(prop))
        node = merged
        _rename_units(node, symbol.extends, symbol.name)

    node.remove("extends")
    _set_name(node, symbol.lib_id)
    return node


def _set_name(node: SNode, name: str) -> None:
    """Replace a symbol node's leading name atom."""
    from aipcb.kicad.sexpr import quoted

    for i, item in enumerate(node.items):
        if isinstance(item, Atom):
            node.items[i] = quoted(name)
            return
    node.items.insert(0, quoted(name))


def _rename_units(node: SNode, old: str, new: str) -> None:
    """Rename nested ``<symbol>_<unit>_<style>`` bodies after a merge."""
    for unit in node.children("symbol"):
        current = unit.value(0)
        if current is None:
            continue
        if current.startswith(old + "_"):
            _set_name(unit, f"{new}_{current.removeprefix(old + chr(95))}")
