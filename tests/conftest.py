"""Shared fixtures and skip conditions."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_DESIGNS = sorted(EXAMPLES.glob("*/design.yaml"))

#: Examples whose whole point is that they cannot be finished. `overconstrained`
#: is four wires that have to cross in one channel on one layer, which no router
#: can do; it exists to exercise the hand-over path, so "some nets are unrouted" is
#: the expected result rather than a failure. Everything it *does* route still has
#: to be DRC-clean.
UNROUTABLE_EXAMPLES = frozenset({"overconstrained"})


def kicad_libraries_present() -> bool:
    from aipcb.checks.kicad_bindings import libraries_available

    return libraries_available()


def kicad_cli_present() -> bool:
    from aipcb.kicad.cli import find_kicad_cli

    return find_kicad_cli() is not None


needs_kicad_libraries = pytest.mark.skipif(
    not kicad_libraries_present(),
    reason="KiCad's symbol/footprint libraries are not installed; "
    "set AIPCB_SYMBOL_DIR and AIPCB_FOOTPRINT_DIR to point at them",
)

needs_kicad_cli = pytest.mark.skipif(
    not kicad_cli_present(),
    reason="kicad-cli is not on PATH; install KiCad 8 or 9, or set AIPCB_KICAD_CLI",
)


@pytest.fixture(params=EXAMPLE_DESIGNS, ids=lambda p: p.parent.name)
def example_design(request: pytest.FixtureRequest) -> Path:
    """Each bundled example design, one per test run."""
    return request.param


@pytest.fixture
def write_design(tmp_path: Path) -> Callable[[str], Path]:
    """Write a design file into a temporary directory and return its path."""

    def _write(text: str, name: str = "design.yaml") -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def full_corpus() -> bool:
    """Whether to run the slow full-library corpus test."""
    return os.environ.get("AIPCB_FULL_CORPUS") == "1"
