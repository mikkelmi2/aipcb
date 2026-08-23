# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The semantic schematic layer -- layer 1 of the architecture.

This is the format a designer (human or agent) actually writes. It is netlist-first
and intent-carrying: a component records not just *what* it is but *why* it is
there (``role``, ``for``, ``reason``), and a net records its electrical character
rather than just its name.

Nothing here describes geometry. Placement and routing intent live in
:mod:`aipcb.model.layout` (layer 2), and neither layer says anything about the
KiCad files that layer 3 compiles them into.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aipcb.model.board import Board
from aipcb.model.common import NET_NAME_PATTERN
from aipcb.model.highspeed import PairTransition
from aipcb.model.layout import Layout, NetClass
from aipcb.model.mech import Fanout, MechPlacement
from aipcb.model.pours import Pour, Stitching
from aipcb.model.simulation import SimulationSettings

__all__ = [
    "KNOWN_NET_CLASSES",
    "KNOWN_ROLES",
    "NET_NAME_RE",
    "REFDES_RE",
    "Component",
    "Constraint",
    "Design",
    "Group",
    "Instance",
    "KeepApart",
    "MaxDistance",
    "Module",
    "Net",
    "Param",
]

#: Reference designators: one or more letters followed by a number, e.g. ``U1``, ``TP12``.
REFDES_RE = re.compile(r"^[A-Z]{1,4}[0-9]+$")
#: Net names. Permissive, but no whitespace or characters that break KiCad's parser.
NET_NAME_RE = re.compile(NET_NAME_PATTERN)
#: Identifiers for modules, instances and local component names.
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A ``{{ param }}`` reference, which module parameters substitute into.
TEMPLATE_RE = re.compile(r"^\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}$")

Ident = Annotated[str, Field(pattern=IDENT_RE.pattern)]
NetName = Annotated[str, Field(pattern=NET_NAME_PATTERN)]
#: How a mechanical block names a component: its source name, or its path inside a
#: module instance (``supply1.C1``).
PartRef = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")]

#: Roles carry design intent. Unknown roles are allowed but reported as a warning,
#: because a typo'd role silently disables the checks that key off it.
KNOWN_ROLES = frozenset({
    "decoupling", "bulk", "bypass", "pull_up", "pull_down", "series", "termination",
    "snubber", "current_limit", "feedback", "divider", "filter", "crystal_load",
    "esd", "reverse_protection", "sense", "load", "indicator", "test_point",
    "mcu", "regulator", "connector", "passive", "power", "level_shifter", "oscillator",
    # M11: two roles that are not descriptions but behaviours. `edge_connector`
    # turns on the card-edge integration checks; `ac_coupling` binds a series
    # capacitor to the high-speed pair it sits in.
    "edge_connector", "ac_coupling",
})

KNOWN_NET_CLASSES = frozenset({
    "power", "ground", "signal", "analog", "diff_pair", "high_speed", "clock", "usb",
})


class Strict(BaseModel):
    """Base model rejecting unknown fields, so typos surface as errors."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Net(Strict):
    """An electrical net and what is electrically true about it."""

    net_class: str = Field(
        default="signal", alias="class",
        description=f"One of {', '.join(sorted(KNOWN_NET_CLASSES))}, or a custom class "
                    "defined under `net_classes:`.",
    )
    voltage: float | None = Field(default=None, description="Nominal voltage in volts.")
    max_current_a: float | None = Field(default=None, gt=0)
    impedance_ohm: float | None = Field(
        default=None, gt=0, description="Target single-ended or differential impedance.",
    )
    diff_pair: NetName | None = Field(
        default=None, description="Partner net of a differential pair.",
    )
    description: str | None = None
    routing: Literal["auto", "manual"] | None = Field(
        default=None,
        description="Overrides the net class's `routing:` for this one net. "
        "`manual` keeps aipcb's router off it entirely -- the copper comes from a "
        "hand route in KiCad or from an external router through the DSN/SES bridge.",
    )
    reason: str | None = Field(
        default=None, description="Why this net exists or why its attributes are set.",
    )


class Component(Strict):
    """One component instance, with the intent behind it."""

    part: str = Field(description="A part name from the component database.")
    pins: dict[str, NetName] = Field(
        default_factory=dict,
        description="Pin number or functional pin name, to the net it connects to.",
    )
    role: str | None = Field(
        default=None, description=f"What this component is for, e.g. {sorted(KNOWN_ROLES)[:3]}.",
    )
    for_: str | None = Field(
        default=None, alias="for",
        description="The component this one serves, e.g. a decoupling cap's IC.",
    )
    reason: str | None = Field(default=None, description="Free text explaining the choice.")
    value: str | None = Field(default=None, description="Overrides the part's default value.")
    refdes: str | None = Field(
        default=None,
        description="Explicit reference designator. Defaults to the component's key at "
                    "the top level, or an auto-assigned one inside a module.",
    )
    dnp: bool = False
    count: int | str = Field(
        default=1,
        description="Stamp out N identical components, suffixed _1..._N. May be a "
                    "`{{ param }}` reference inside a module.",
    )

    @field_validator("count")
    @classmethod
    def _count_value(cls, v: int | str) -> int | str:
        if isinstance(v, str):
            if not TEMPLATE_RE.match(v.strip()):
                raise ValueError(
                    f"count must be a whole number or a parameter reference such as "
                    f"'{{{{ n }}}}', got {v!r}"
                )
            return v
        if not 1 <= v <= 64:
            raise ValueError(f"count must be between 1 and 64, got {v}")
        return v

    @field_validator("role")
    @classmethod
    def _role_shape(cls, v: str | None) -> str | None:
        if v is not None and not IDENT_RE.match(v):
            raise ValueError(f"role {v!r} must be a plain identifier, e.g. 'decoupling'")
        return v

    @field_validator("refdes")
    @classmethod
    def _refdes_shape(cls, v: str | None) -> str | None:
        if v is not None and not REFDES_RE.match(v):
            raise ValueError(
                f"refdes {v!r} must be letters followed by digits, e.g. 'U1' or 'TP12'"
            )
        return v


class Param(Strict):
    """A module parameter -- the closest thing this format has to a function argument."""

    type: Literal["int", "float", "str", "bool", "part", "net"] = "str"
    default: Any = None
    description: str | None = None
    required: bool = False

    @model_validator(mode="after")
    def _default_or_required(self) -> Param:
        if self.required and self.default is not None:
            raise ValueError("a required parameter cannot also have a default")
        return self


class MaxDistance(Strict):
    """Keep components close. The canonical case is a decoupling cap's loop area."""

    kind: Literal["max_distance"]
    between: tuple[str, ...] = Field(min_length=2)
    mm: float = Field(gt=0)
    reason: str


class KeepApart(Strict):
    """Keep components separated -- thermal, noise, or high-voltage isolation."""

    kind: Literal["keep_apart"]
    between: tuple[str, ...] = Field(min_length=2)
    mm: float = Field(gt=0)
    reason: str


class Group(Strict):
    """Place these components as one cluster."""

    kind: Literal["group"]
    members: tuple[str, ...] = Field(min_length=2)
    reason: str
    name: Ident | None = None


Constraint = Annotated[
    MaxDistance | KeepApart | Group,
    Field(discriminator="kind"),
]


class Instance(Strict):
    """An instantiation of a module -- the format's equivalent of a call."""

    module: Ident
    params: dict[str, Any] = Field(default_factory=dict)
    connect: dict[str, NetName] = Field(
        default_factory=dict, description="Module port name to the net it binds to.",
    )
    reason: str | None = None


class Module(Strict):
    """A reusable, parameterised subcircuit.

    Modules may instantiate other modules, so designs are hierarchical. Ports are
    the only nets visible to the parent; every other net declared here is local and
    gets a hierarchical name during elaboration.
    """

    description: str | None = None
    params: dict[Ident, Param] = Field(default_factory=dict)
    ports: tuple[str, ...] = Field(
        default=(), description="Net names the parent connects to.",
    )
    nets: dict[NetName, Net] = Field(default_factory=dict)
    components: dict[Ident, Component] = Field(default_factory=dict)
    instances: dict[Ident, Instance] = Field(default_factory=dict)
    constraints: tuple[Constraint, ...] = ()

    @field_validator("ports")
    @classmethod
    def _ports_unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            dupes = sorted({p for p in v if v.count(p) > 1})
            raise ValueError(f"duplicate port names: {', '.join(dupes)}")
        for p in v:
            if not NET_NAME_RE.match(p):
                raise ValueError(f"port name {p!r} is not a valid net name")
        return v

    @model_validator(mode="after")
    def _no_empty_module(self) -> Module:
        if not self.components and not self.instances:
            raise ValueError("a module must contain at least one component or instance")
        return self


class Design(Strict):
    """A complete design -- the root of a ``design.yaml``."""

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_. \-]*$")
    revision: str = Field(default="A", min_length=1)
    description: str | None = None
    libraries: tuple[str, ...] = Field(
        default=(), description="Part-library files, relative to this design file.",
    )
    net_classes: dict[Ident, NetClass] = Field(
        default_factory=dict,
        description="Routing rules per net class. Layer 2; unused until the board is built.",
    )
    nets: dict[NetName, Net] = Field(default_factory=dict)
    components: dict[Ident, Component] = Field(default_factory=dict)
    modules: dict[Ident, Module] = Field(default_factory=dict)
    instances: dict[Ident, Instance] = Field(default_factory=dict)
    constraints: tuple[Constraint, ...] = ()
    layout: Layout | None = None
    board: Board | None = Field(
        default=None,
        description="The mechanical boundary: outline, cutouts, edge clearance. The "
        "frame every coordinate under `placement:` is given in.",
    )
    placement: dict[PartRef, MechPlacement] = Field(
        default_factory=dict,
        description="Components whose position is dictated from outside the "
        "electrical design: fixed, edge-constrained or region-constrained.",
    )
    fanout: dict[PartRef, Fanout] = Field(
        default_factory=dict,
        description="Packages that need pattern-based escape routing before the "
        "router can reach their pads.",
    )
    pours: tuple[Pour, ...] = Field(
        default=(),
        description="Copper pours: which net owns the free copper of which layer. "
        "Emitted as KiCad zones and filled by KiCad's own engine.",
    )
    transitions: tuple[PairTransition, ...] = Field(
        default=(),
        description="Differential-pair layer changes, each generated as one "
        "validated pattern with its return vias (M11c).",
    )
    stitching: tuple[Stitching, ...] = Field(
        default=(),
        description="Patterns of vias tying a net's pours together between layers.",
    )
    simulation: SimulationSettings = Field(
        default_factory=SimulationSettings,
        description="What `aipcb simulate` may assume: band, slice margin, mesh "
        "density and the thresholds a verdict is measured against (M12). Nothing "
        "here reaches the board, and a design that omits it simulates on defaults.",
    )

    @model_validator(mode="after")
    def _one_outline(self) -> Design:
        """The board edge is declared once, in one block, or not at all."""
        if self.board is not None and self.layout is not None and self.layout.outline:
            raise ValueError(
                "the board edge is declared twice: under `board.outline` and under "
                "`layout.outline`. Keep the `board:` block and delete the old one"
            )
        return self

    @model_validator(mode="after")
    def _placement_has_a_frame(self) -> Design:
        """Mechanical coordinates are meaningless without the outline they refer to."""
        if self.placement and self.board is None:
            raise ValueError(
                "`placement:` gives coordinates in the board frame, so the design "
                "needs a `board:` block with an `outline:` to define that frame"
            )
        return self

    @model_validator(mode="after")
    def _non_empty(self) -> Design:
        if not self.components and not self.instances:
            raise ValueError(
                "a design must declare at least one component or module instance"
            )
        return self
