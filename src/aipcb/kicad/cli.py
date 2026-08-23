# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Driving ``kicad-cli``.

KiCad is the backend of this toolchain, not a library dependency: we shell out to
``kicad-cli`` so everything runs headless in CI without linking against KiCad's
Python API. This module is the single place that knows how to find it, run it, and
turn its absence into a clear message rather than a traceback.

One narrow exception, decided in `ADR 0009 <../../../docs/decisions/0009-pours.md>`_
and confined to :mod:`aipcb.kicad.fill`: `kicad-cli` 9.0.8 cannot fill a zone, so a
design that declares ``pours:`` reaches KiCad's own filler through a ``pcbnew``
*subprocess*. The rule that survives is "no ``pcbnew`` in the aipcb package's own
process", which is why that module is a separate one and why nothing here imports
it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "KICAD_CLI_ENV",
    "KicadCliMissing",
    "KicadRun",
    "find_kicad_cli",
    "kicad_version",
    "require_kicad_cli",
    "run_kicad",
]

#: Overrides the executable, for machines where it is not on PATH.
KICAD_CLI_ENV = "AIPCB_KICAD_CLI"

_MISSING_MESSAGE = (
    "kicad-cli was not found on PATH.\n"
    "  aipcb uses KiCad as its backend for ERC, DRC and fabrication output.\n"
    "  Install KiCad 8 or 9, or point aipcb at an existing install with "
    f"{KICAD_CLI_ENV}=/path/to/kicad-cli"
)


class KicadCliMissing(RuntimeError):
    """Raised when ``kicad-cli`` is needed but not installed."""

    def __init__(self) -> None:
        super().__init__(_MISSING_MESSAGE)


@dataclass(frozen=True, slots=True)
class KicadRun:
    """The result of one ``kicad-cli`` invocation."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def command(self) -> str:
        return " ".join(self.args)


def find_kicad_cli() -> str | None:
    """Locate ``kicad-cli``, honouring the override environment variable."""
    override = os.environ.get(KICAD_CLI_ENV)
    if override:
        path = Path(override)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return shutil.which(override)
    return shutil.which("kicad-cli")


def require_kicad_cli() -> str:
    """Return the path to ``kicad-cli`` or raise :class:`KicadCliMissing`."""
    found = find_kicad_cli()
    if found is None:
        raise KicadCliMissing
    return found


def kicad_version() -> str | None:
    """Return KiCad's version string, or ``None`` if it is not installed."""
    executable = find_kicad_cli()
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def run_kicad(*args: str, cwd: Path | None = None, timeout: float = 300) -> KicadRun:
    """Run ``kicad-cli`` with ``args``.

    Never raises on a non-zero exit: several ``kicad-cli`` subcommands use the exit
    code to report findings rather than failure, so the caller decides what a given
    code means.
    """
    executable = require_kicad_cli()
    argv = (executable, *args)
    env = dict(os.environ)
    # KiCad writes user configuration on first run and prints a notice about it to
    # stdout, which would otherwise contaminate parsed output.
    env.setdefault("KICAD_STDLIB_NO_CONFIG_WARNING", "1")
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return KicadRun(argv, 124, "", f"kicad-cli timed out after {timeout}s: {exc}")
    except OSError as exc:
        return KicadRun(argv, 127, "", f"could not run kicad-cli: {exc}")
    return KicadRun(argv, result.returncode, result.stdout, result.stderr)
