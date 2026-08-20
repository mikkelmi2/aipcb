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

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "LIB_ID_RE",
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


class Supplier(Strict):
    """Optional sourcing information. Carried through to the BOM, never required."""

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
    dnp: bool = Field(default=False, description="Do not populate.")

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
