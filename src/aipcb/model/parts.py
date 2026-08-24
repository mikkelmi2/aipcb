# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The component database: parts, their pinouts, and their KiCad bindings.

A *part* is a purchasable thing (``C_100n_0402``, ``ATtiny85-20PU``). It binds a
logical pinout to a KiCad symbol and footprint, and records the electrical limits
that later milestones check designs against.

Pin electrical types use KiCad's own vocabulary rather than a private one, because
they drive ERC directly: getting ``power_in`` versus ``passive`` right is the
difference between ERC catching an unpowered rail and staying silent about it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "LIB_ID_RE",
    "Assembly",
    "ElectricalType",
    "Limits",
    "Part",
    "PartLibrary",
    "Pin",
    "Supplier",
]

#: A KiCad library identifier, ``Library:Item``.
LIB_ID_RE = re.compile(r"^[^\s:][^:]*:[^\s:][^:]*$")

PartName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.+\-/]*$", min_length=1)]


class ElectricalType(StrEnum):
    """KiCad pin electrical types. These are what ERC reasons about."""

    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"
    TRI_STATE = "tri_state"
    PASSIVE = "passive"
    FREE = "free"
    UNSPECIFIED = "unspecified"
    POWER_IN = "power_in"
    POWER_OUT = "power_out"
    OPEN_COLLECTOR = "open_collector"
    OPEN_EMITTER = "open_emitter"
    NO_CONNECT = "no_connect"


class Strict(BaseModel):
    """Base model that rejects unknown fields.

    A typo such as ``fooprint:`` must be an error rather than a silently ignored
    key: the agent needs to be told, not left wondering why nothing changed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Pin(Strict):
    """One pin of a part."""

    type: ElectricalType = ElectricalType.PASSIVE
    name: str | None = Field(
        default=None,
        description="Functional name, e.g. VCC or PB0. Defaults to the pin's key.",
    )
    description: str | None = None


class Limits(Strict):
    """Absolute-maximum ratings, used by later design-rule checks."""

    voltage_max_v: float | None = Field(default=None, gt=0)
    voltage_min_v: float | None = None
    current_max_a: float | None = Field(default=None, gt=0)
    power_max_w: float | None = Field(default=None, gt=0)
    temp_max_c: float | None = None


class Assembly(StrEnum):
    """What an assembler is meant to do with a part (M21a).

    ``smt`` and ``tht`` are placement instructions and differ where it matters: a
    PCBWay centroid file lists *only* surface-mount parts, so a through-hole part
    that claims to be surface-mount is a part the machine will try to place.

    ``dnp`` is a part that is on the board and must not be fitted. ``none`` is a
    footprint that is not a part at all -- a card-edge finger field, a fiducial, a
    mounting slot -- and it is a distinct case from ``dnp`` because "do not fit
    this component" and "this is not a component" are different sentences to say
    to an assembler, and only the first belongs on a BOM at all.

    Unset is not the same as ``smt``. When a part says nothing,
    :func:`aipcb.compile.assembly.assembly_of` reads the answer off the footprint's
    own pads, which is a measurement rather than a default -- see its docstring for
    why a default of ``smt`` would have been wrong on four of this repository's own
    bundled examples.
    """

    SMT = "smt"
    THT = "tht"
    DNP = "dnp"
    NONE = "none"


class Supplier(Strict):
    """Optional sourcing information. Carried through to the BOM, never required.

    ``manufacturer`` and ``mpn`` are also spelled at the top level of :class:`Part`,
    which is where M21a put them and where new designs should say them. Both
    spellings validate and :meth:`Part.procurement` folds them together; declaring
    the same field in both places with different values is an error rather than a
    silent winner.
    """

    manufacturer: str | None = None
    mpn: str | None = None
    distributor: str | None = None
    sku: str | None = None
    datasheet: str | None = None


class Part(Strict):
    """A part definition, as it appears in a library file."""

    symbol: str = Field(description='KiCad symbol library id, e.g. "Device:C".')
    footprint: str = Field(description='KiCad footprint library id, e.g. "Device:R_0402".')
    description: str | None = None
    value: str | None = Field(
        default=None,
        description="Value shown on the schematic. Defaults to the part's own name.",
    )
    keywords: tuple[str, ...] = ()
    pins: dict[str, Pin] = Field(
        default_factory=dict,
        description="Pin number (as printed in the footprint) to pin definition.",
    )
    limits: Limits = Field(default_factory=Limits)
    supplier: Supplier = Field(default_factory=Supplier)
    mpn: str | None = Field(
        default=None,
        description="Manufacturer part number -- the thing an assembler orders.",
    )
    manufacturer: str | None = Field(
        default=None, description='Who makes it, e.g. "STMicroelectronics".',
    )
    supplier_refs: dict[str, str] = Field(
        default_factory=dict,
        description="Per-supplier part id, e.g. `{lcsc: C432211}`. A map rather "
                    "than a pair of fields, because a part is orderable from more "
                    "than one place and the fab-specific BOM wants that fab's id.",
    )
    assembly: Assembly | None = Field(
        default=None,
        description="What the assembler does with it: smt, tht, dnp or none. "
                    "Unset means 'read it off the footprint', which is what "
                    "`aipcb export --bom` does.",
    )
    dnp: bool = Field(default=False, description="Do not populate.")
    refdes_prefix: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{1,4}$",
        description="Designator letters for parts of this kind: R, C, U, J. Used when "
                    "a designator has to be assigned automatically.",
    )

    @model_validator(mode="after")
    def _one_spelling_per_field(self) -> Part:
        """``mpn:`` and ``supplier.mpn:`` may both exist; they may not disagree."""
        for name in ("mpn", "manufacturer"):
            top, nested = getattr(self, name), getattr(self.supplier, name)
            if top is not None and nested is not None and top != nested:
                raise ValueError(
                    f"{name} is declared twice and the two disagree: "
                    f"{name}: {top!r} against supplier.{name}: {nested!r}. "
                    f"Say it once -- at the top level is the current spelling."
                )
        return self

    def procurement(self) -> tuple[str | None, str | None]:
        """The effective ``(mpn, manufacturer)``, whichever spelling declared them."""
        return (
            self.mpn or self.supplier.mpn,
            self.manufacturer or self.supplier.manufacturer,
        )

    @field_validator("supplier_refs")
    @classmethod
    def _check_supplier_refs(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if not key.strip() or not value.strip():
                raise ValueError("supplier_refs keys and values must not be blank")
        return v

    @field_validator("symbol", "footprint")
    @classmethod
    def _check_lib_id(cls, v: str) -> str:
        if not LIB_ID_RE.match(v):
            raise ValueError(
                f"{v!r} is not a KiCad library id; expected the form 'Library:Item', "
                'e.g. "Capacitor_SMD:C_0402_1005Metric"'
            )
        return v

    @field_validator("pins")
    @classmethod
    def _check_pins(cls, v: dict[str, Pin]) -> dict[str, Pin]:
        if not v:
            raise ValueError("a part must declare at least one pin")
        for number in v:
            if not number.strip():
                raise ValueError("pin numbers must not be blank")
        return v

    def pin_by_name(self, name: str) -> str | None:
        """Return the pin *number* whose functional name is ``name``.

        Designs connect by whichever is clearer -- ``VCC`` or ``8`` -- so both are
        resolvable. Matching is case-insensitive because datasheets are not
        consistent about it.
        """
        folded = name.casefold()
        for number, pin in self.pins.items():
            if (pin.name or number).casefold() == folded:
                return number
        return None

    def resolve_pin(self, ref: str) -> str | None:
        """Resolve a pin reference (number or functional name) to a pin number."""
        if ref in self.pins:
            return ref
        return self.pin_by_name(ref)


class PartLibrary(Strict):
    """The contents of one library file."""

    parts: dict[PartName, Part] = Field(default_factory=dict)
