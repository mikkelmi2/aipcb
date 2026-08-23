# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Round-trip every KiCad file we can find, and prove nothing is lost.

This is the evidence behind ADR 0001. The claim that our S-expression layer is
lossless is only worth anything if it is checked against real KiCad files rather
than against files we wrote ourselves, so this test runs over the libraries shipped
with the installed KiCad: demos, symbol libraries, footprint libraries, templates.

By default it samples deterministically to keep the suite fast. Set
``AIPCB_FULL_CORPUS=1`` to run all ~16,000 files (about three minutes).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aipcb.kicad.footprints import default_footprint_dirs
from aipcb.kicad.sexpr import dump, dump_all, parse, parse_all
from aipcb.kicad.symbols import default_symbol_dirs

from .conftest import needs_kicad_libraries

KICAD_EXTENSIONS = {".kicad_sch", ".kicad_pcb", ".kicad_sym", ".kicad_mod", ".kicad_wks"}
SAMPLE_SIZE = 400

#: Files shipped with KiCad 9.0.8 that are genuinely malformed -- an independent
#: paren count ends at depth -7 with 16 top-level roots. Rejecting them is correct.
KNOWN_BAD = ("royalblue54L_feather",)


def _corpus_roots() -> list[Path]:
    roots: list[Path] = [*default_symbol_dirs()[:1], *default_footprint_dirs()[:1]]
    for directory in default_symbol_dirs()[:1]:
        for sibling in ("demos", "template"):
            candidate = directory.parent / sibling
            if candidate.is_dir():
                roots.append(candidate)
    return roots


def _corpus_files(full: bool) -> list[Path]:
    files = [
        path
        for root in _corpus_roots()
        for path in root.rglob("*")
        if path.suffix in KICAD_EXTENSIONS
        and path.is_file()
        and not any(bad in path.parts for bad in KNOWN_BAD)
    ]
    files.sort()
    if full or len(files) <= SAMPLE_SIZE:
        return files
    # Sample by a hash of the path so the selection is stable across runs and
    # machines, but still spread across every library rather than clustered.
    return sorted(
        files,
        key=lambda p: hashlib.sha256(str(p).encode()).hexdigest(),
    )[:SAMPLE_SIZE]


@needs_kicad_libraries
def test_corpus_round_trips_losslessly(full_corpus: bool) -> None:
    files = _corpus_files(full_corpus)
    assert files, "no KiCad files found to test against"

    failures: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(f"{path}: unreadable ({exc})")
            continue

        parser, writer = (
            (parse_all, dump_all) if path.suffix == ".kicad_dru" else (parse, dump)
        )
        try:
            tree = parser(text)
        except Exception as exc:
            failures.append(f"{path}: parse failed ({exc})")
            continue

        try:
            again = parser(writer(tree))  # type: ignore[operator]
        except Exception as exc:
            failures.append(f"{path}: re-parse of our own output failed ({exc})")
            continue

        if again != tree:
            failures.append(f"{path}: tree changed across a round trip")
        elif writer(again) != writer(tree):  # type: ignore[operator]
            failures.append(f"{path}: output is not stable")

    assert not failures, (
        f"{len(failures)} of {len(files)} files did not round-trip:\n"
        + "\n".join(failures[:20])
    )


@needs_kicad_libraries
def test_uuid_tokens_survive_a_round_trip() -> None:
    """The specific regression that ruled out kiutils.

    kiutils rewrites KiCad 9's ``uuid`` tokens as KiCad 6 ``tstamp`` tokens, which
    destroys the identity every part of this toolchain depends on. Whatever we do
    to a file, every UUID must come back unchanged.
    """
    boards = [
        p
        for root in _corpus_roots()
        for p in root.rglob("*.kicad_pcb")
        if not any(bad in p.parts for bad in KNOWN_BAD)
    ]
    if not boards:
        pytest.skip("no demo boards installed")

    board = max(boards, key=lambda p: p.stat().st_size)
    text = board.read_text(encoding="utf-8")
    original = text.count("(uuid ")
    assert original > 0, "expected the test board to contain uuid tokens"

    rewritten = dump(parse(text))
    assert rewritten.count("(uuid ") == original
    assert "tstamp" not in rewritten
