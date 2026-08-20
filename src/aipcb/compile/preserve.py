"""Rebuilding a board without destroying what a human changed in KiCad.

`aipcb` generates boards, but people finish them. Someone opens the `.kicad_pcb`,
nudges a connector to line up with the enclosure, routes a differential pair by
hand, pours a ground zone. The next `aipcb build` must not throw that away.

The rule is one sentence: **the source owns what it declares; everything else
belongs to the person who drew it.**

Applying it needs two questions answered per element:

*Is this the same element?* — by UUID. Ours are hashes of source paths, so a
footprint's identity survives rebuilds, renames of unrelated parts, and reordering.

*Has the source changed its mind about it?* — by fingerprint. Each generated
footprint carries a hash of the source facts that determine it: its part, its
footprint, its pin-to-net connections, which side it goes on, and any placement
constraint that names it. If that hash still matches, the source has nothing new to
say and the human's position stands. If it differs, the source has changed and wins.

Anything in the board we did not generate — tracks, vias, zones, added graphics —
is preserved untouched, because a UUID we do not recognise is by definition
somebody else's work.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode
from aipcb.netlist import ElabComponent, Netlist

__all__ = [
    "FINGERPRINT_PROPERTY",
    "PRESERVED_ITEMS",
    "MergeStats",
    "component_fingerprint",
    "merge_board",
]

#: Where a footprint records what the source said about it when it was generated.
FINGERPRINT_PROPERTY = "aipcb.fingerprint"

#: Top-level board items that are never generated, and so always belong to a human.
PRESERVED_ITEMS = frozenset(
    {
        "segment", "arc", "via", "zone", "group", "dimension", "image",
        "gr_text", "gr_text_box", "gr_poly", "gr_curve", "gr_bbox", "target",
    }
)

#: Graphics we do generate, and therefore own -- but only on the layer we draw on.
_OWNED_EDGE_LAYER = "Edge.Cuts"


@dataclass(slots=True)
class MergeStats:
    """What survived a rebuild, and what did not."""

    kept_positions: list[str] = field(default_factory=list)
    moved_by_source: list[str] = field(default_factory=list)
    kept_items: dict[str, int] = field(default_factory=dict)
    dropped_items: dict[str, int] = field(default_factory=dict)
    removed_components: list[str] = field(default_factory=list)
    added_components: list[str] = field(default_factory=list)

    @property
    def preserved_count(self) -> int:
        return sum(self.kept_items.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kept_positions": sorted(self.kept_positions),
            "moved_by_source": sorted(self.moved_by_source),
            "kept_items": dict(sorted(self.kept_items.items())),
            "dropped_items": dict(sorted(self.dropped_items.items())),
            "removed_components": sorted(self.removed_components),
            "added_components": sorted(self.added_components),
        }


# ---------------------------------------------------------------------------
# fingerprints
# ---------------------------------------------------------------------------


def component_fingerprint(component: ElabComponent, netlist: Netlist) -> str:
    """Hash the source facts that determine where and what a footprint is.

    Deliberately *not* everything about the component. A changed ``reason:`` or a
    corrected typo in a description says nothing about placement, and forcing a
    hand-placed part back to the grid over a comment edit would make the feature
    useless. What counts is the part, the footprint, the connections, the side, and
    any constraint that names this component.
    """
    constraints = sorted(
        f"{c.kind}:{','.join(sorted(c.members))}"
        for c in netlist.constraints
        if component.refdes in c.members or component.path_text in c.members
    )
    rules = sorted(
        f"{rule.side}:{rule.region_mm}:{rule.orientation_deg}"
        for rule in (netlist.layout.placement.rules if netlist.layout else ())
        if component.refdes in rule.members or component.path_text in rule.members
    )
    parts = [
        component.path_text,
        component.part_name,
        component.part.footprint if component.part else "",
        ";".join(f"{pin}={net}" for pin, net in sorted(component.connections.items())),
        ";".join(constraints),
        ";".join(rules),
        "dnp" if component.dnp else "",
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def _read_fingerprint(footprint: SNode) -> str | None:
    for prop in footprint.children("property"):
        if prop.value(0) == FINGERPRINT_PROPERTY:
            return prop.value(1)
    return None


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


def merge_board(
    generated: SNode,
    existing: SNode,
    netlist: Netlist,
    report: Report | None = None,
) -> tuple[SNode, MergeStats]:
    """Fold a freshly generated board into an existing, possibly hand-edited one.

    Returns the merged tree and a summary of what was preserved. ``generated`` is
    modified in place and returned.
    """
    stats = MergeStats()

    existing_footprints = {
        uuid: node
        for node in existing.children("footprint")
        if (uuid := node.get("uuid")) is not None
    }
    generated_footprints = {
        uuid: node
        for node in generated.children("footprint")
        if (uuid := node.get("uuid")) is not None
    }

    by_uuid = {c.uuid: c for c in netlist.components.values()}
    _merge_footprints(
        generated_footprints, existing_footprints, by_uuid, netlist, stats
    )

    stats.removed_components = [
        str(_reference(node))
        for uuid, node in sorted(existing_footprints.items())
        if uuid not in generated_footprints
    ]
    stats.added_components = [
        str(_reference(node))
        for uuid, node in sorted(generated_footprints.items())
        if uuid not in existing_footprints
    ]

    live_nets = _live_net_codes(generated)
    _carry_over_items(generated, existing, live_nets, stats)
    _carry_over_outline(generated, existing, netlist, stats)

    if report is not None and (stats.preserved_count or stats.kept_positions):
        _report(stats, report)
    return generated, stats


def _merge_footprints(
    generated: dict[str, SNode],
    existing: dict[str, SNode],
    by_uuid: dict[str, ElabComponent],
    netlist: Netlist,
    stats: MergeStats,
) -> None:
    for uuid, fresh in generated.items():
        component = by_uuid.get(uuid)
        if component is None:
            continue
        fingerprint = component_fingerprint(component, netlist)
        previous = existing.get(uuid)
        if previous is None:
            continue

        if _read_fingerprint(previous) != fingerprint:
            # The source changed something that determines this footprint, so the
            # source wins and the freshly computed placement stands.
            stats.moved_by_source.append(component.refdes)
            continue

        _adopt_placement(fresh, previous)
        stats.kept_positions.append(component.refdes)


def _adopt_placement(fresh: SNode, previous: SNode) -> None:
    """Take the human's position, rotation, side and text placement."""
    for token in ("at", "layer"):
        kept = previous.child(token)
        if kept is not None:
            fresh.replace(token, kept)

    # Field text that someone dragged should stay where they dragged it.
    previous_props = {
        prop.value(0): prop for prop in previous.children("property")
    }
    for prop in fresh.children("property"):
        old = previous_props.get(prop.value(0))
        if old is None:
            continue
        for token in ("at", "layer", "effects", "unlocked"):
            kept = old.child(token)
            if kept is not None:
                prop.replace(token, kept)

    for token in ("locked", "attr"):
        kept = previous.child(token)
        if kept is not None and token == "locked":
            fresh.replace(token, kept)


def _live_net_codes(generated: SNode) -> set[str]:
    return {
        code
        for node in generated.children("net")
        if (code := node.value(0)) is not None
    }


def _carry_over_items(
    generated: SNode, existing: SNode, live_nets: set[str], stats: MergeStats
) -> None:
    """Copy across everything we never generate.

    Copper that belongs to a net the design no longer has is dropped rather than
    carried: an orphaned track is a short waiting to happen, and keeping it would
    mean the board no longer matches the schematic.
    """
    for item in existing.children():
        if item.name not in PRESERVED_ITEMS:
            continue
        net = item.get("net")
        if net is not None and net not in live_nets:
            stats.dropped_items[item.name] = stats.dropped_items.get(item.name, 0) + 1
            continue
        generated.add(item)
        stats.kept_items[item.name] = stats.kept_items.get(item.name, 0) + 1


def _carry_over_outline(
    generated: SNode, existing: SNode, netlist: Netlist, stats: MergeStats
) -> None:
    """Keep a hand-drawn board edge unless the source declares one.

    Drawing the outline in KiCad is a normal workflow -- it is where the mechanical
    constraints are visible. A design that says nothing about its outline has not
    claimed ownership of it, so whatever is already there stays.
    """
    declares_outline = netlist.layout is not None and netlist.layout.outline is not None
    if declares_outline:
        return

    generated_edges = [
        item
        for item in generated.children()
        if item.name.startswith("gr_") and item.get("layer") == _OWNED_EDGE_LAYER
    ]
    existing_edges = [
        item
        for item in existing.children()
        if item.name.startswith("gr_") and item.get("layer") == _OWNED_EDGE_LAYER
    ]
    if not existing_edges:
        return

    for edge in generated_edges:
        generated.items.remove(edge)
    for edge in existing_edges:
        generated.add(edge)
        stats.kept_items[edge.name] = stats.kept_items.get(edge.name, 0) + 1


def _reference(footprint: SNode) -> str | None:
    for prop in footprint.children("property"):
        if prop.value(0) == "Reference":
            return prop.value(1)
    return footprint.get("uuid")


def _report(stats: MergeStats, report: Report) -> None:
    kept = ", ".join(f"{n} {name}" for name, n in sorted(stats.kept_items.items()))
    details = []
    if stats.kept_positions:
        details.append(f"{len(stats.kept_positions)} hand-placed footprints")
    if kept:
        details.append(kept)
    if details:
        report.info(
            "preserved-edits",
            "kept manual work from the existing board: " + "; ".join(details),
            hint="run `aipcb build --fresh` to regenerate from source instead",
        )
    if stats.moved_by_source:
        report.info(
            "placement-regenerated",
            f"{len(stats.moved_by_source)} footprint"
            f"{'s' if len(stats.moved_by_source) != 1 else ''} moved because the "
            f"source changed: {', '.join(sorted(stats.moved_by_source))}",
            hint="the source owns what it declares; their positions were recomputed",
        )
    if stats.dropped_items:
        dropped = ", ".join(f"{n} {name}" for name, n in sorted(stats.dropped_items.items()))
        report.warning(
            "dropped-orphaned-copper",
            f"removed copper belonging to nets the design no longer has: {dropped}",
            hint="an orphaned track is a short waiting to happen",
        )
