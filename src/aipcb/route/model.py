# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The topological routing model -- what the source stores about a route.

A route is stored as a *sketch*, not as geometry: the ordered list of obstacles it
passes and the side it passes them on. Nothing here is a coordinate, and that is
the point. Coordinates would be invalidated by any placement change, whereas a
sketch stays meaningful — which is what makes re-tightening after a move cheap
(see :doc:`../decisions/0006-routing-approach`).

Sides are recorded relative to the direction of travel, from the route's ``from``
pad to its ``to`` pad. Absolute compass directions are not invariant under rotating
or mirroring the board, and read wrongly the moment a route doubles back.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aipcb.model.common import Layer

__all__ = [
    "NODE_RE",
    "Pass",
    "RouteTopology",
    "Side",
    "ViaHop",
    "Waypoint",
    "parse_node",
]

#: A pad reference (``U1.7``), a component body (``U1``), or a named via (``via:v1``).
NODE_RE = re.compile(r"^(?:via:[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[^\s]+)?)$")

Side = Literal["left", "right"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Pass(Strict):
    """The route passes an obstacle, on one side of it."""

    kind: Literal["pass"] = "pass"
    obstacle: str = Field(
        description="A pad (`U1.7`), a component body (`U1`), or a via (`via:name`).",
    )
    side: Side = Field(
        description="Which side the route passes on, looking along the direction of "
                    "travel from `from` to `to`.",
    )
    reason: str | None = None

    @field_validator("obstacle")
    @classmethod
    def _shape(cls, v: str) -> str:
        if not NODE_RE.match(v):
            raise ValueError(
                f"{v!r} is not an obstacle reference; expected a pad such as 'U1.7', "
                "a component such as 'U1', or a via such as 'via:v1'"
            )
        return v


class ViaHop(Strict):
    """The route changes layer here.

    A via is a topological node, not a coordinate: where it lands is decided by the
    stretcher. Naming one lets other routes refer to it as an obstacle.
    """

    kind: Literal["via"] = "via"
    to_layer: Layer
    name: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Lets other routes name this via as an obstacle.",
    )
    reason: str | None = None


Waypoint = Annotated[Pass | ViaHop, Field(discriminator="kind")]


class RouteTopology(Strict):
    """One route: how a connection gets from one pad to another.

    A net with more than two pads needs more than one route -- the set of routes on
    a net forms its routing tree. Nets with no route at all are left for the
    auto-router.
    """

    net: str
    from_: str = Field(alias="from", description="Starting pad, e.g. `J1.3`.")
    to: str = Field(description="Ending pad, e.g. `R1.1`.")
    layer: Layer = Field(default="F.Cu", description="The layer the route starts on.")
    passes: tuple[Waypoint, ...] = Field(
        default=(),
        description="Obstacles passed, in order of travel. Empty means a direct run.",
    )
    reason: str | None = None

    @field_validator("passes", mode="before")
    @classmethod
    def _infer_kind(cls, value: object) -> object:
        """Let a waypoint's fields imply its kind.

        The discriminated union needs a `kind` tag, but making every waypoint carry
        `kind: pass` is noise on the common case -- and noise in a format an agent
        writes by hand is a source of errors, not clarity. A waypoint naming an
        `obstacle` is a pass; one naming a `to_layer` is a via hop.
        """
        if not isinstance(value, (list, tuple)):
            return value
        out = []
        for item in value:
            if isinstance(item, dict) and "kind" not in item:
                item = dict(item)
                if "obstacle" in item:
                    item["kind"] = "pass"
                elif "to_layer" in item:
                    item["kind"] = "via"
            out.append(item)
        return out

    @field_validator("from_", "to")
    @classmethod
    def _endpoint_shape(cls, v: str) -> str:
        if "." not in v or not NODE_RE.match(v):
            raise ValueError(
                f"{v!r} is not a pad reference; expected 'REFDES.PAD', e.g. 'U1.7'"
            )
        return v

    @model_validator(mode="after")
    def _endpoints_differ(self) -> RouteTopology:
        if self.from_ == self.to:
            raise ValueError(f"a route cannot start and end at the same pad ({self.to})")
        return self

    @property
    def via_hops(self) -> tuple[ViaHop, ...]:
        return tuple(w for w in self.passes if isinstance(w, ViaHop))

    @property
    def obstacles(self) -> tuple[Pass, ...]:
        return tuple(w for w in self.passes if isinstance(w, Pass))

    def layers_used(self) -> tuple[str, ...]:
        """Every layer this route touches, in order."""
        layers = [self.layer]
        for hop in self.via_hops:
            layers.append(hop.to_layer)
        return tuple(layers)

    def key(self) -> str:
        """A stable identity for this route, used for deterministic UUIDs."""
        return f"{self.net}/{self.from_}>{self.to}"


def parse_node(reference: str) -> tuple[str, str | None]:
    """Split an obstacle reference into its owner and, for a pad, its pad number.

    >>> parse_node("U1.7")
    ('U1', '7')
    >>> parse_node("U1")
    ('U1', None)
    >>> parse_node("via:v1")
    ('via:v1', None)
    """
    if reference.startswith("via:"):
        return reference, None
    owner, _, pad = reference.partition(".")
    return owner, pad or None
