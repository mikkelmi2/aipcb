#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Add (or verify) the SPDX licence header on every Python source file.

Apache-2.0 asks that each source file carry a notice. Ninety-odd files is past
the point where hand-editing is honest work, and well past the point where a
human would keep it right as files are added -- so this script both applies the
header and, with ``--check``, fails when a file is missing one. CI runs the
second mode; a contributor adding a file runs the first.

The header goes at the very top, above the module docstring, because a comment
is not a statement: the docstring stays the module's first *statement* and
``__doc__`` is unaffected. A shebang, where one exists, stays on line 1.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HOLDER = "The aipcb Authors"
YEAR = "2026"
HEADER = (
    f"# SPDX-FileCopyrightText: {YEAR} {HOLDER}\n"
    "# SPDX-License-Identifier: Apache-2.0\n"
)
MARKER = "SPDX-License-Identifier:"

#: Directories never walked: build output, caches, environments, and the
#: golden KiCad trees (which contain no Python and are compared byte-for-byte).
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "out",
}


def sources(root: Path) -> list[Path]:
    found = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts):
            continue
        found.append(path)
    return found


def apply(path: Path) -> bool:
    """Insert the header if absent. Returns True when the file was changed."""
    text = path.read_text(encoding="utf-8")
    if MARKER in text.split("\n\n", 1)[0] or MARKER in text[:400]:
        return False
    if text.startswith("#!"):
        shebang, _, rest = text.partition("\n")
        path.write_text(f"{shebang}\n{HEADER}{rest}", encoding="utf-8")
    else:
        path.write_text(HEADER + text, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report files missing the header and exit non-zero; change nothing",
    )
    args = parser.parse_args()

    files = sources(args.root)
    missing: list[Path] = []
    changed: list[Path] = []
    for path in files:
        if args.check:
            if MARKER not in path.read_text(encoding="utf-8")[:400]:
                missing.append(path)
        elif apply(path):
            changed.append(path)

    if args.check:
        for path in missing:
            print(f"missing SPDX header: {path.relative_to(args.root)}")
        print(f"{len(files) - len(missing)}/{len(files)} Python files carry an SPDX header")
        if missing:
            print("run: python tools/add_spdx.py")
            return 1
        return 0

    for path in changed:
        print(f"  + {path.relative_to(args.root)}")
    print(f"{len(changed)} file(s) changed, {len(files)} scanned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
