# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Emitting the project-level files KiCad expects beside a schematic.

A ``.kicad_sch`` embeds copies of the symbols it uses, so it opens and checks
correctly on its own -- but KiCad still reports every symbol as coming from a
library it does not know about, and a human opening the file cannot update a symbol
or place a new one. A project-local ``sym-lib-table`` naming just the libraries this
design actually uses fixes both, and keeps the project self-describing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aipcb.kicad.sexpr import SNode, dump, quoted, sym
from aipcb.model.layout import NetClass

__all__ = [
    "DRC_SEVERITIES",
    "build_fp_lib_table",
    "build_project",
    "build_sym_lib_table",
    "render_lib_table",
    "render_project",
]

#: KiCad resolves these against its own installation, so the table stays portable.
SYMBOL_DIR_VAR = "${KICAD9_SYMBOL_DIR}"
FOOTPRINT_DIR_VAR = "${KICAD9_FOOTPRINT_DIR}"

_TABLE_VERSION = "7"

#: KiCad's own default board constraints, as it writes them into a fresh project.
#: A design whose net classes ask for something *tighter* than one of these has to
#: say so here, or KiCad checks the board against a rule the source never agreed to
#: and reports every fine-pitch via as a violation. A design that stays inside the
#: defaults leaves them alone, so its project file is byte-for-byte what it always
#: was.
KICAD_DEFAULT_CONSTRAINTS = {
    "min_track_width": 0.0,
    "min_via_diameter": 0.5,
    "min_through_hole_diameter": 0.3,
    "min_copper_edge_clearance": 0.5,
}

#: DRC rules KiCad silences by default, promoted so that ``check`` can see them.
#:
#: ``kicad-cli pcb drc --severity-all`` includes ``error``, ``warning`` and
#: ``exclusion`` -- and *not* ``ignore``. A rule KiCad defaults to ``ignore`` is
#: therefore dropped inside KiCad, before any report reaches us, and nothing in the
#: report says a category was suppressed: the ``included_severities`` field still
#: reads ``["error", "warning", "exclusion"]``. That is a silent hole in an
#: otherwise exact pipeline, so this closes it by naming the severity we want
#: rather than inheriting whatever the tool happens to default to.
#:
#: Measured on KiCad 9.0.8 (M13.5), by building a board that violates each rule and
#: running DRC with and without the pin:
#:
#: * ``missing_courtyard`` -- six footprints stripped of their courtyard, zero
#:   violations reported; pinned, six. This one matters most: ``courtyards_overlap``
#:   is how a placement is checked, and a footprint with no courtyard opts out of it
#:   without saying so.
#: * ``pth_inside_courtyard`` -- fourteen through-hole pads inside a courtyard, zero
#:   reported; pinned, fourteen.
#: * ``npth_inside_courtyard`` and ``footprint_filters_mismatch`` -- no example in
#:   the corpus can violate these, so they are pinned on the evidence of KiCad's own
#:   shipped project templates rather than on a measurement here.
#:
#: ``warning`` and not ``error``: all four are advisory about how a board was drawn,
#: not statements that it is wrong. Re-measure at each KiCad major, as ADR 0009 says
#: -- the defaults are the tool's, not ours, and they move.
DRC_SEVERITIES = {
    "footprint_filters_mismatch": "warning",
    "missing_courtyard": "warning",
    "npth_inside_courtyard": "warning",
    "pth_inside_courtyard": "warning",
}


def _entry(name: str, uri: str) -> SNode:
    return SNode("lib").add(
        SNode("name").add(quoted(name)),
        SNode("type").add(quoted("KiCad")),
        SNode("uri").add(quoted(uri)),
        SNode("options").add(quoted("")),
        SNode("descr").add(quoted("")),
    )


def build_sym_lib_table(libraries: set[str]) -> SNode:
    """A ``sym-lib-table`` naming each symbol library the design draws from."""
    table = SNode("sym_lib_table").add(SNode("version").add(sym(_TABLE_VERSION)))
    for name in sorted(libraries):
        table.add(_entry(name, f"{SYMBOL_DIR_VAR}/{name}.kicad_sym"))
    return table


def build_fp_lib_table(libraries: set[str]) -> SNode:
    """An ``fp-lib-table`` naming each footprint library the design draws from."""
    table = SNode("fp_lib_table").add(SNode("version").add(sym(_TABLE_VERSION)))
    for name in sorted(libraries):
        table.add(_entry(name, f"{FOOTPRINT_DIR_VAR}/{name}.pretty"))
    return table


def render_lib_table(table: SNode) -> str:
    return dump(table)


# ---------------------------------------------------------------------------
# the .kicad_pro project file
# ---------------------------------------------------------------------------


def build_project(
    project: str,
    sheet_uuid: str,
    net_classes: Mapping[str, NetClass],
    net_assignments: Mapping[str, str],
    edge_clearance: float | None = None,
) -> dict[str, Any]:
    """Build the ``.kicad_pro`` contents.

    KiCad needs a project file for two reasons that matter here: without one it
    ignores the project-local library tables, and the board's design rules -- track
    widths, clearances, differential-pair geometry -- live in the project rather
    than in the board file. So this is where layer 2's ``net_classes:`` actually
    lands, and what DRC will later check against.

    ``edge_clearance`` is the other half of ``board.edge_clearance``: the router
    keeps copper that far from the outline and from every cutout, and this is what
    makes KiCad check the same figure. A design that states nothing leaves the rule
    alone, so KiCad uses its own default and the two tools still agree.

    Every project also pins :data:`DRC_SEVERITIES`, so the rules KiCad silences by
    default reach ``check`` instead of vanishing inside ``kicad-cli``.
    """
    classes = [_default_net_class()]
    for name in sorted(net_classes):
        classes.append(_net_class_json(name, net_classes[name]))

    patterns = [
        {"netclass": net_classes_name, "pattern": net}
        for net, net_classes_name in sorted(net_assignments.items())
        if net_classes_name in net_classes
    ]

    rules: dict[str, Any] = {}
    if edge_clearance is not None:
        rules["min_copper_edge_clearance"] = edge_clearance
    rules.update(_tighter_than_default(net_classes))
    settings: dict[str, Any] = {"rule_severities": dict(DRC_SEVERITIES)}
    if rules:
        settings["rules"] = dict(sorted(rules.items()))

    return {
        "board": {
            "design_settings": settings,
            "layer_presets": [],
            "viewports": [],
        },
        "boards": [],
        "cvpcb": {"equivalence_files": []},
        "erc": {"erc_exclusions": [], "meta": {"version": 0}, "rule_severities": {}},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": f"{project}.kicad_pro", "version": 3},
        "net_settings": {
            "classes": classes,
            "meta": {"version": 4},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": patterns,
        },
        "pcbnew": {
            "last_paths": {
                "gencad": "", "idf": "", "netlist": "", "plot": "",
                "pos_files": "", "specctra_dsn": "", "step": "", "svg": "", "vrml": "",
            },
            "page_layout_descr_file": "",
        },
        "schematic": {
            "legacy_lib_dir": "",
            "legacy_lib_list": [],
            "meta": {"version": 1},
            "page_layout_descr_file": "",
        },
        "sheets": [[sheet_uuid, "Root"]],
        "text_variables": {},
    }


def _tighter_than_default(net_classes: Mapping[str, NetClass]) -> dict[str, float]:
    """Board constraints the design needs relaxed below KiCad's defaults.

    A 0.5 mm-pitch package cannot be escaped with 0.5 mm vias, so a design that says
    ``via_diameter_mm: 0.4`` means it -- and KiCad has to be told, or its own
    minimum-via rule reports every one of them. Only the values that are actually
    tighter are written, so nothing changes for a board built to comfortable rules.
    """
    if not net_classes:
        return {}
    smallest = {
        "min_track_width": min(c.trace_width_mm for c in net_classes.values()),
        "min_via_diameter": min(c.via_diameter_mm for c in net_classes.values()),
        "min_through_hole_diameter": min(c.via_drill_mm for c in net_classes.values()),
    }
    return {
        rule: round(value, 4)
        for rule, value in smallest.items()
        if value < KICAD_DEFAULT_CONSTRAINTS[rule] - 1e-9
    }


def _default_net_class() -> dict[str, Any]:
    """KiCad requires a class literally named ``Default``; every net falls back to it."""
    return _net_class_json("Default", NetClass())


def _net_class_json(name: str, net_class: NetClass) -> dict[str, Any]:
    gap = net_class.diff_pair_gap_mm or net_class.clearance_mm
    width = net_class.diff_pair_width_mm or net_class.trace_width_mm
    return {
        "bus_width": 12,
        "clearance": net_class.clearance_mm,
        "diff_pair_gap": gap,
        "diff_pair_via_gap": gap,
        "diff_pair_width": width,
        "line_style": 0,
        "microvia_diameter": 0.508,
        "microvia_drill": 0.127,
        "name": name,
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        # KiCad resolves overlapping assignments by priority, lowest first. Default
        # takes the sentinel KiCad itself uses so every explicit class outranks it.
        "priority": 2147483647 if name == "Default" else 0,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": net_class.trace_width_mm,
        "via_diameter": net_class.via_diameter_mm,
        "via_drill": net_class.via_drill_mm,
        "wire_width": 6,
    }


def render_project(project_data: dict[str, Any]) -> str:
    """Serialise a project file deterministically: sorted keys, no timestamps."""
    return json.dumps(project_data, indent=2, sort_keys=True) + "\n"
