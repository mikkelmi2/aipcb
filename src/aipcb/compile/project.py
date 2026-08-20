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
) -> dict[str, Any]:
    """Build the ``.kicad_pro`` contents.

    KiCad needs a project file for two reasons that matter here: without one it
    ignores the project-local library tables, and the board's design rules -- track
    widths, clearances, differential-pair geometry -- live in the project rather
    than in the board file. So this is where layer 2's ``net_classes:`` actually
    lands, and what DRC will later check against.
    """
    classes = [_default_net_class()]
    for name in sorted(net_classes):
        classes.append(_net_class_json(name, net_classes[name]))

    patterns = [
        {"netclass": net_classes_name, "pattern": net}
        for net, net_classes_name in sorted(net_assignments.items())
        if net_classes_name in net_classes
    ]

    return {
        "board": {"design_settings": {}, "layer_presets": [], "viewports": []},
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
