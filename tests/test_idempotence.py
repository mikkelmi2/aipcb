"""Every command is a function of its source, not of what it left behind.

M13.5 found `aipcb check` reading its own previous output back in. `check` routes
the board it builds and writes the copper into it; the next build read that copper
as a human's hand routing, preserved it, and routed the design again on top.
Checking one unchanged design three times into one directory took it from 1108 mm
of copper and 90/90 connections to 2462 mm and 59/90, inventing a clearance error
on the way -- on a design nobody had touched.

The fix was one argument (`fresh=True`), and it is held in place by
``test_check_loop.py::TestCheckIsAFunctionOfTheSource``. This file is the *sweep*
M13.6 asked for: the same two-run question put to every other command that writes
into a directory a later run may read. The class of defect is what matters, not
the one instance of it, and the way this class hides is that nobody runs the same
command twice into the same place.

What is deliberately **not** here: ``aipcb build`` over a board somebody has
edited. Preserving a human's work across a rebuild is what `build` is *for*, and
:func:`test_building_three_times_changes_nothing` pins the part of it that has to
hold anyway -- that `build`'s own output is not mistaken for a human's.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from aipcb.si.runner import slice_digest

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aipcb.cli", *args],
        capture_output=True, text=True, check=False,
    )


def _stable_bytes(paths: Iterator[Path]) -> dict[str, bytes]:
    """Every file's contents, minus the lines carrying a wall-clock timestamp.

    KiCad stamps a creation date into every Gerber it plots, so two identical
    exports differ by the second they ran in. That is the one difference this
    sweep has to look past; everything else is geometry.
    """
    out: dict[str, bytes] = {}
    for path in sorted(paths):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            out[path.name] = path.read_bytes()
            continue
        kept = [
            line
            for line in text.splitlines()
            if "CreationDate" not in line and "date 20" not in line
        ]
        out[path.name] = "\n".join(kept).encode("utf-8")
    return out


@needs_kicad_libraries
class TestBuildIsAFunctionOfTheSource:
    def test_building_three_times_changes_nothing(self, tmp_path: Path) -> None:
        """`build` does not mistake its own output for somebody's hand edit.

        `build` writes no copper, so the segments and vias that broke `check` are
        not in play -- but since M10 the source declares *zones*, and `preserve.py`
        calls a zone a human's. What stops them accumulating is the UUID guard: an
        item whose UUID the fresh board already carries is ours. This is that guard,
        measured rather than read.
        """
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        seen = set()
        for _ in range(3):
            assert _cli("build", str(design), "--out", str(tmp_path)).returncode == 0
            board = (tmp_path / "usb-port.kicad_pcb").read_text(encoding="utf-8")
            seen.add((board, board.count("(zone"), board.count("(segment")))
        assert len(seen) == 1, "three builds of one design gave more than one board"


@needs_kicad_cli
@needs_kicad_libraries
class TestExportIsAFunctionOfTheSource:
    def test_exporting_three_times_into_one_build_dir_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        """`--build-dir` keeps the intermediate board, so a later run can read it.

        Without the flag `export` builds into a throwaway directory and the
        question cannot arise; with it, the intermediate is exactly the kind of
        output-read-back-as-input that broke `check`. The fabrication package has
        to be a function of the design either way.
        """
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        build, out = tmp_path / "build", tmp_path / "out"
        seen = []
        for _ in range(3):
            run = _cli(
                "export", str(design), "--out", str(out), "--build-dir", str(build)
            )
            assert run.returncode == 0, run.stdout + run.stderr
            seen.append(
                (
                    (build / "usb-port.kicad_pcb").read_bytes(),
                    _stable_bytes(out.glob("*")),
                )
            )
        for index, (board, files) in enumerate(seen[1:], start=2):
            assert board == seen[0][0], f"export {index} built a different board"
            assert files == seen[0][1], f"export {index} plotted different fabrication files"


@needs_kicad_cli
@needs_kicad_libraries
class TestSimulateIsAFunctionOfTheSource:
    def test_slicing_three_times_into_one_out_dir_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        """The slice a second run hands the solver must be the first run's slice.

        ``simulate`` routes into a temporary directory of its own, so the board is
        safe; what is not obviously safe is ``--out``, which keeps the slice, the
        solver inputs and the fabrication package a later run overwrites in place.
        The digest is the cache key, so a slice that moved between two runs of one
        unchanged design would either re-solve for no reason or -- worse -- hand
        back a cached number measured on different geometry.
        """
        design = REPO_ROOT / "examples" / "mcu-4layer" / "design.yaml"
        work = tmp_path / "si"
        pair = work / "USB_DM+USB_DP"
        seen = []
        for _ in range(3):
            run = _cli("simulate", str(design), "--out", str(work), "--dry-run")
            assert run.returncode == 0, run.stdout + run.stderr
            seen.append(
                (
                    slice_digest(pair),
                    (pair / "slice.kicad_pcb").read_bytes(),
                    json.loads((pair / "slice.json").read_text(encoding="utf-8")),
                    _stable_bytes((pair / "fab").glob("*")),
                )
            )
        for index, row in enumerate(seen[1:], start=2):
            assert row[0] == seen[0][0], f"slice {index} has a different digest"
            assert row[1] == seen[0][1], f"slice {index} is a different board"
            assert row[2] == seen[0][2], f"slice {index} reports different geometry"
            assert row[3] == seen[0][3], f"slice {index} exported different Gerbers"


@needs_kicad_libraries
class TestSyncPlacementIsAFunctionOfItsInputs:
    def test_reporting_twice_changes_neither_the_source_nor_the_answer(
        self, tmp_path: Path
    ) -> None:
        """`sync-placement` reads a board and reports; without `--apply` it writes
        nothing at all, and the design file it was pointed at has to come back
        byte-identical. Run twice because "writes nothing" is a claim about the
        second run as much as the first.
        """
        design = tmp_path / "design.yaml"
        source = (REPO_ROOT / "examples" / "usb-port" / "design.yaml").read_text(
            encoding="utf-8"
        )
        library = REPO_ROOT / "examples" / "library"
        design.write_text(source.replace("- ../library/", f"- {library}/"), "utf-8")
        before = design.read_bytes()

        assert _cli("build", str(design), "--out", str(tmp_path)).returncode == 0
        board = tmp_path / "usb-port.kicad_pcb"
        answers = []
        for _ in range(2):
            run = _cli(
                "sync-placement", str(design), "--board", str(board), "--json"
            )
            assert run.returncode in (0, 1), run.stdout + run.stderr
            answers.append(run.stdout)
            assert design.read_bytes() == before, "a report rewrote the source"
        assert answers[0] == answers[1]


@needs_kicad_cli
@needs_kicad_libraries
def test_the_sweep_covers_every_command_that_writes_where_it_reads() -> None:
    """The list itself, so a twelfth command does not join quietly.

    `aipcb --help` names every command. Each one either writes nothing a later run
    reads, or has a test above. Adding a command with an output directory and no
    entry here fails this, which is the point: the defect class is "nobody ran it
    twice", and the way to keep closing it is to notice the new one.
    """
    run = _cli("--help")
    assert run.returncode == 0, run.stdout + run.stderr

    #: Commands that write into a directory a later run of the same command reads.
    #: Each has a three-run (or two-run) test in this file or in
    #: `test_check_loop.py::TestCheckIsAFunctionOfTheSource`.
    covered = {"build", "check", "export", "simulate", "sync-placement"}
    #: Commands that write nothing: they answer a question about the source on
    #: stdout and leave the filesystem alone.
    read_only = {"validate", "summary", "parts", "schema", "version", "route"}

    listed = {
        line.split()[0]
        for line in run.stdout.splitlines()
        if line.startswith("  ") and line.strip() and not line.strip().startswith("-")
    }
    listed = {name for name in listed if name.replace("-", "").isalpha()}
    unknown = listed - covered - read_only
    assert not unknown, (
        f"{sorted(unknown)} write output nobody has run twice; give each one a "
        "two-run test here or add it to `read_only` with a reason"
    )
