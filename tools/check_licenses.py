#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Fail if aipcb's *runtime* dependency closure gains a non-permissive licence.

Every runtime dependency today is BSD/MIT-family. That is not an accident worth
trusting: a transitive dependency can change licence in a point release, and
nobody would notice until a downstream user with a legal department did. This
script exists to notice.

It walks the closure from the ``dependencies`` declared in ``pyproject.toml``
-- not the whole environment, because dev tools (pytest, mypy, ruff) are not
distributed with anything and their licences do not propagate. Extras are
skipped for the same reason unless named with ``--extra``.

Exit status is 0 when every distribution in the closure resolves to an allowed
licence, 1 otherwise. Run it in CI; run it by hand before a release.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

#: SPDX identifiers and legacy classifier names we accept without discussion.
#: All are permissive: they impose attribution, not source-disclosure or
#: relicensing obligations, so they compose with Apache-2.0 in a distributed
#: artifact. Adding to this list is a deliberate act, reviewed like any other.
ALLOWED = {
    "0bsd",
    "apache-2.0",
    "apache software license",
    "bsd",
    "bsd license",
    "bsd 2-clause",
    "bsd 3-clause",
    "bsd-2-clause",
    "bsd-3-clause",
    "cc0-1.0",
    "isc",
    "isc license (iscl)",
    "isc license",
    "mit",
    "mit license",
    "mit-0",
    "mit-cmu",
    "psf-2.0",
    "psfl",
    "python software foundation license",
    "unlicense",
    "zlib",
}

#: Distributions the closure names but that ship with the interpreter or are
#: otherwise not redistributed by us.
IGNORE = {"python"}


def declared_requirements(pyproject: Path, extras: list[str]) -> list[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]
    reqs = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            raise SystemExit(f"no such extra in pyproject.toml: {extra!r}")
        reqs.extend(optional[extra])
    return reqs


def licence_of(name: str) -> str | None:
    """The licence a distribution declares, or ``None`` if it is not installed.

    PEP 639 moved the answer from the free-text ``License`` field to
    ``License-Expression``; older wheels only carry ``License ::`` classifiers,
    and some carry the full licence *text* in ``License``. All three are read,
    newest convention first, and a ``License`` field long enough to be a licence
    body rather than a name is discarded in favour of the classifiers.
    """
    try:
        meta = md.metadata(name)
    except md.PackageNotFoundError:
        return None
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    legacy = (meta.get("License") or "").strip()
    if legacy and len(legacy) <= 60 and "\n" not in legacy:
        return legacy
    if classifiers:
        return " AND ".join(classifiers)
    return legacy or ""


def allowed(expression: str) -> bool:
    """True when *every* alternative in a compound expression is permissive.

    ``AND`` means all apply, so all must pass. ``OR`` means the recipient may
    choose, so one passing would be enough -- but we require all, because
    picking the permissive arm of a dual licence is a decision for a human to
    record, not for a CI script to make silently.
    """
    parts = (
        expression.replace("(", " ")
        .replace(")", " ")
        .replace(" AND ", "|")
        .replace(" OR ", "|")
        .split("|")
    )
    return all(p.strip().lower() in ALLOWED for p in parts if p.strip())


def closure(roots: list[str]) -> dict[str, str]:
    """Every distribution reachable from *roots* through runtime requirements.

    Requirements guarded by an ``extra ==`` marker are skipped: they are not
    installed unless somebody asks for the extra, so they are not part of what
    a plain ``pip install aipcb`` redistributes.
    """
    found: dict[str, str] = {}
    pending = [Requirement(r).name for r in roots]
    while pending:
        name = pending.pop()
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in found or key in IGNORE:
            continue
        licence = licence_of(name)
        if licence is None:
            found[key] = "NOT INSTALLED"
            continue
        found[key] = licence or "UNDECLARED"
        for raw in md.requires(name) or []:
            req = Requirement(raw)
            if req.marker is not None and "extra" in str(req.marker):
                continue
            pending.append(req.name)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pyproject", type=Path, default=Path(__file__).resolve().parent.parent / "pyproject.toml"
    )
    parser.add_argument(
        "--extra", action="append", default=[], help="also check this optional-dependency group"
    )
    args = parser.parse_args()

    roots = declared_requirements(args.pyproject, args.extra)
    resolved = closure(roots)

    bad: list[tuple[str, str]] = []
    width = max(len(n) for n in resolved) if resolved else 0
    for name in sorted(resolved):
        licence = resolved[name]
        if licence == "NOT INSTALLED":
            status = "SKIP"
        elif allowed(licence):
            status = "ok"
        else:
            status = "DENY"
            bad.append((name, licence))
        print(f"  {status:4}  {name:{width}}  {licence}")

    print(f"\n{len(resolved)} distributions in the runtime closure of {len(roots)} declared roots")
    if bad:
        print("\nnot on the permissive allowlist:")
        for name, licence in bad:
            print(f"  {name}: {licence}")
        print(
            "\nIf this licence is in fact acceptable, add it to ALLOWED in this file\n"
            "in the same commit, so the decision is reviewed rather than assumed."
        )
        return 1
    print("all permissive (BSD/MIT-family or equivalent)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
