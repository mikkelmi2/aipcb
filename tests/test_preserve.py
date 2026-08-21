"""Incremental builds: rebuilding without destroying what a human changed.

The rule under test is one sentence: the source owns what it declares, and
everything else belongs to the person who drew it.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from aipcb.compile.build import build_design, compile_netlist
from aipcb.compile.preserve import FINGERPRINT_PROPERTY, component_fingerprint
from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode, dump, num, parse, quoted, sym

from .conftest import REPO_ROOT, needs_kicad_libraries

pytestmark = needs_kicad_libraries


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Callable[[str | None], tuple[Path, Path]]:
    """A copy of the usb-port example that tests can edit freely."""
    shutil.copytree(REPO_ROOT / "examples" / "library", tmp_path / "library")
    (tmp_path / "usb-port").mkdir()
    original = (REPO_ROOT / "examples" / "usb-port" / "design.yaml").read_text()

    def make(source: str | None = None) -> tuple[Path, Path]:
        design = tmp_path / "usb-port" / "design.yaml"
        design.write_text(source if source is not None else original, encoding="utf-8")
        return design, tmp_path / "out"

    make.original = original  # type: ignore[attr-defined]
    return make


def board_tree(out: Path) -> SNode:
    return parse(next(out.glob("*.kicad_pcb")).read_text(encoding="utf-8"))


def write_board(out: Path, tree: SNode) -> None:
    next(out.glob("*.kicad_pcb")).write_text(dump(tree), encoding="utf-8")


def footprint(tree: SNode, refdes: str) -> SNode:
    for fp in tree.children("footprint"):
        for prop in fp.children("property"):
            if prop.value(0) == "Reference" and prop.value(1) == refdes:
                return fp
    raise AssertionError(f"no footprint {refdes}")


def position(tree: SNode, refdes: str) -> tuple[str | None, ...]:
    at = footprint(tree, refdes).child("at")
    assert at is not None
    return (at.value(0), at.value(1), at.value(2))


def move(tree: SNode, refdes: str, x: float, y: float, rotation: float = 0) -> None:
    footprint(tree, refdes).replace(
        "at", SNode("at").add(num(x), num(y), num(rotation))
    )


def hand_track(tree: SNode, net_name: str, uuid: str) -> None:
    code = next(n.value(0) for n in tree.children("net") if n.value(1) == net_name)
    tree.add(
        SNode("segment").add(
            SNode("start").add(num(10), num(10)),
            SNode("end").add(num(20), num(10)),
            SNode("width").add(num(0.25)),
            SNode("layer").add(quoted("F.Cu")),
            SNode("net").add(sym(str(code))),
            SNode("uuid").add(quoted(uuid)),
        )
    )


# ---------------------------------------------------------------------------
# fingerprints
# ---------------------------------------------------------------------------


class TestFingerprint:
    def _fingerprints(self, source: str | None = None) -> dict[str, str]:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        netlist = compile_netlist(design, Report())
        return {
            c.refdes: component_fingerprint(c, netlist)
            for c in netlist.sorted_components()
        }

    def test_is_stable(self) -> None:
        assert self._fingerprints() == self._fingerprints()

    def test_changes_when_the_part_changes(self, workspace) -> None:
        design, _ = workspace()
        before = compile_netlist(design, Report())
        design.write_text(
            workspace.original.replace(  # type: ignore[attr-defined]
                "  R1:\n    part: R_22R_0603", "  R1:\n    part: R_10K_0603"
            ),
            encoding="utf-8",
        )
        after = compile_netlist(design, Report())
        assert component_fingerprint(
            before.components["R1"], before
        ) != component_fingerprint(after.components["R1"], after)

    def test_ignores_changes_that_do_not_affect_placement(self, workspace) -> None:
        """Editing a comment must not shove a hand-placed part back to the grid."""
        design, _ = workspace()
        before = compile_netlist(design, Report())
        design.write_text(
            workspace.original.replace(  # type: ignore[attr-defined]
                "reason: The 22 ohm series resistors set", "reason: Rewritten. They set"
            ),
            encoding="utf-8",
        )
        after = compile_netlist(design, Report())
        assert component_fingerprint(
            before.components["R1"], before
        ) == component_fingerprint(after.components["R1"], after)


# ---------------------------------------------------------------------------
# preserving
# ---------------------------------------------------------------------------


class TestPreservesManualWork:
    def test_first_build_records_a_fingerprint(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        tree = board_tree(out)
        for fp in tree.children("footprint"):
            keys = {p.value(0) for p in fp.children("property")}
            assert FINGERPRINT_PROPERTY in keys

    def test_unchanged_rebuild_keeps_every_position(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        result = build_design(design, out_dir=out)
        assert result.merge is not None
        netlist = compile_netlist(design, Report())
        assert set(result.merge.kept_positions) == set(netlist.components)
        assert result.merge.moved_by_source == []

    def test_hand_placed_footprint_stays_put(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        tree = board_tree(out)
        move(tree, "J1", 5.5, 7.25, 90)
        write_board(out, tree)

        build_design(design, out_dir=out)
        assert position(board_tree(out), "J1") == ("5.5", "7.25", "90")

    def test_hand_routed_copper_survives(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        tree = board_tree(out)
        hand_track(tree, "VBUS", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        write_board(out, tree)

        result = build_design(design, out_dir=out)
        after = board_tree(out)
        assert any(
            s.get("uuid") == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            for s in after.children("segment")
        )
        assert result.merge is not None
        assert result.merge.kept_items["segment"] == 1

    def test_hand_drawn_zones_survive(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        tree = board_tree(out)
        code = next(n.value(0) for n in tree.children("net") if n.value(1) == "GND")
        tree.add(
            SNode("zone").add(
                SNode("net").add(sym(str(code))),
                SNode("layer").add(quoted("B.Cu")),
                SNode("uuid").add(quoted("11111111-2222-3333-4444-555555555555")),
            )
        )
        write_board(out, tree)

        build_design(design, out_dir=out)
        assert len(list(board_tree(out).children("zone"))) == 1

    def test_dragged_field_text_stays_where_it_was_dragged(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        tree = board_tree(out)
        reference = next(
            p for p in footprint(tree, "R1").children("property") if p.value(0) == "Reference"
        )
        reference.replace("at", SNode("at").add(num(3.5), num(-4.5), num(90)))
        write_board(out, tree)

        build_design(design, out_dir=out)
        moved = next(
            p
            for p in footprint(board_tree(out), "R1").children("property")
            if p.value(0) == "Reference"
        )
        at = moved.child("at")
        assert at is not None and (at.value(0), at.value(1)) == ("3.5", "-4.5")


class TestSourceWins:
    def test_a_changed_component_is_replaced(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        tree = board_tree(out)
        move(tree, "R1", 3.0, 3.0)
        move(tree, "C1", 4.0, 4.0)
        write_board(out, tree)

        design.write_text(
            workspace.original.replace(  # type: ignore[attr-defined]
                "  R1:\n    part: R_22R_0603", "  R1:\n    part: R_10K_0603"
            ),
            encoding="utf-8",
        )
        result = build_design(design, out_dir=out)
        after = board_tree(out)

        assert position(after, "R1")[:2] != ("3", "3"), "the source change should have won"
        assert position(after, "C1")[:2] == ("4", "4"), "C1's source did not change"
        assert result.merge is not None
        assert result.merge.moved_by_source == ["R1"]

    def test_fresh_discards_manual_work(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        tree = board_tree(out)
        move(tree, "J1", 5.5, 7.25)
        hand_track(tree, "VBUS", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        write_board(out, tree)

        result = build_design(design, out_dir=out, fresh=True)
        after = board_tree(out)
        assert position(after, "J1")[:2] != ("5.5", "7.25")
        assert not list(after.children("segment"))
        assert result.merge is None

    def test_deleted_component_loses_its_footprint(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        # Remove C2 and everything that referenced it.
        trimmed = workspace.original.replace(  # type: ignore[attr-defined]
            """  C2:
    part: C_100N_0603
    role: decoupling
    for: J1
    reason: The ceramic the bulk capacitor is too slow to be.
    pins:
      "1": VBUS
      "2": GND

""",
            "",
        )
        assert trimmed != workspace.original  # type: ignore[attr-defined]
        design.write_text(trimmed, encoding="utf-8")

        result = build_design(design, out_dir=out)
        assert result.merge is not None
        assert result.merge.removed_components == ["C2"]
        with pytest.raises(AssertionError):
            footprint(board_tree(out), "C2")

    def test_orphaned_copper_is_dropped_with_a_warning(self, workspace) -> None:
        """A track on a net the design no longer has is a short waiting to happen."""
        design, out = workspace()
        build_design(design, out_dir=out)
        tree = board_tree(out)
        tree.add(
            SNode("segment").add(
                SNode("start").add(num(10), num(10)),
                SNode("end").add(num(20), num(10)),
                SNode("width").add(num(0.25)),
                SNode("layer").add(quoted("F.Cu")),
                SNode("net").add(sym("9999")),
                SNode("uuid").add(quoted("dddddddd-dddd-dddd-dddd-dddddddddddd")),
            )
        )
        write_board(out, tree)

        report = Report()
        result = build_design(design, out_dir=out, report=report)
        assert result.merge is not None
        assert result.merge.dropped_items == {"segment": 1}
        assert any(d.code == "dropped-orphaned-copper" for d in report)


class TestOutlineOwnership:
    def test_source_outline_wins_when_declared(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        tree = board_tree(out)
        for edge in [g for g in tree.children("gr_line") if g.get("layer") == "Edge.Cuts"]:
            tree.items.remove(edge)
        write_board(out, tree)

        build_design(design, out_dir=out)
        edges = [g for g in board_tree(out).children("gr_line") if g.get("layer") == "Edge.Cuts"]
        assert len(edges) == 4, "the design declares an outline, so it owns it"

    def test_hand_drawn_outline_is_kept_when_the_source_declares_none(
        self, workspace
    ) -> None:
        """Drawing the edge in KiCad is normal; a silent source has not claimed it."""
        source = workspace.original  # type: ignore[attr-defined]
        # Cut the design off before its `board:` block, so it declares no edge at
        # all -- neither the new block nor the old `layout.outline`.
        start = source.index("board:")
        design, out = workspace(source[:start])
        build_design(design, out_dir=out)

        tree = board_tree(out)
        for edge in [g for g in tree.children("gr_line") if g.get("layer") == "Edge.Cuts"]:
            tree.items.remove(edge)
        tree.add(
            SNode("gr_circle").add(
                SNode("center").add(num(50), num(50)),
                SNode("end").add(num(80), num(50)),
                SNode("layer").add(quoted("Edge.Cuts")),
                SNode("uuid").add(quoted("cccccccc-cccc-cccc-cccc-cccccccccccc")),
            )
        )
        write_board(out, tree)

        build_design(design, out_dir=out)
        after = board_tree(out)
        assert any(
            g.get("uuid") == "cccccccc-cccc-cccc-cccc-cccccccccccc"
            for g in after.children("gr_circle")
        )
        assert not [g for g in after.children("gr_line") if g.get("layer") == "Edge.Cuts"]


class TestDamagedBoard:
    def test_unparseable_board_warns_rather_than_crashing(self, workspace) -> None:
        design, out = workspace()
        build_design(design, out_dir=out)
        next(out.glob("*.kicad_pcb")).write_text("(kicad_pcb (version", encoding="utf-8")

        report = Report()
        build_design(design, out_dir=out, report=report)
        assert any(d.code == "existing-board-unparseable" for d in report)
        assert board_tree(out).name == "kicad_pcb"
