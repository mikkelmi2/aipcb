#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Fail unless every commit in a range carries a matching ``Signed-off-by`` line.

The Developer Certificate of Origin (https://developercertificate.org/) is
asserted per commit, by the author, in the commit message. This checks that the
assertion is there and that it names the commit's own author -- a sign-off
copied from somewhere else certifies nothing.

Usage:  check_dco.py <base-sha> <head-sha>

Merge commits are exempt: their content is the commits they merge, each of
which is checked on its own, and a maintainer pressing the merge button is not
authoring anything to certify.
"""

from __future__ import annotations

import subprocess
import sys

SEP = "\x1e"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    base, head = argv[1], argv[2]

    raw = git(
        "log", "--no-merges", f"--format=%H%x1f%an%x1f%ae%x1f%B{SEP}", f"{base}..{head}"
    )
    commits = [c for c in raw.split(SEP) if c.strip()]
    if not commits:
        print("no non-merge commits in range; nothing to check")
        return 0

    failures: list[tuple[str, str, str]] = []
    for entry in commits:
        sha, name, email, body = entry.strip().split("\x1f", 3)
        expected = f"Signed-off-by: {name} <{email}>"
        signoffs = [
            line.strip()
            for line in body.splitlines()
            if line.strip().lower().startswith("signed-off-by:")
        ]
        if not signoffs:
            failures.append((sha, f"{name} <{email}>", "no Signed-off-by line"))
        elif expected not in signoffs:
            failures.append(
                (sha, f"{name} <{email}>", f"no sign-off matching the author; found: {signoffs}")
            )

    checked = len(commits)
    if failures:
        print(f"{len(failures)} of {checked} commit(s) are not signed off:\n")
        for sha, author, why in failures:
            print(f"  {sha[:12]}  {author}")
            print(f"      {why}")
            print(f"      expected: Signed-off-by: {author}")
        print(
            "\nAdd the line with `git commit -s`. To fix commits already made:\n"
            "  git rebase --signoff origin/master\n"
            "then force-push the branch. CONTRIBUTING.md has the details."
        )
        return 1

    print(f"all {checked} commit(s) signed off")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
