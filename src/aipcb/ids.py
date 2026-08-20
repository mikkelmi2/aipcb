"""Deterministic UUIDs derived from source paths.

Every element `aipcb` emits into a KiCad file carries a UUID that is a pure
function of *what the element is in the source*, never of when it was generated or
what order it was generated in. This one property does a lot of work:

* Rebuilding an unchanged design produces byte-identical files, so ``git diff``
  shows only what actually changed.
* ``kicad-cli``'s ERC and DRC reports identify violations by UUID, so a violation
  maps straight back to the source element that owns it (M4) with no positional
  guessing.
* A rebuild can tell which elements in a human-edited board are still the same
  ones, which is what lets manual routing survive (M6).

The scheme is UUID version 5 -- SHA-1 over a fixed namespace plus a canonical
element key -- so it is stable across machines, Python versions and runs, and needs
no state on disk.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

__all__ = ["AIPCB_NAMESPACE", "element_uuid", "net_codes", "source_key"]

#: The project's own UUID namespace. Generated once and fixed forever: changing it
#: would renumber every element of every existing board.
AIPCB_NAMESPACE = uuid.UUID("6f1a5e2c-9b3d-5a47-8c21-1d4e7f0a9b63")


def source_key(*parts: str | int) -> str:
    """Build the canonical key for an element from its path in the source.

    Parts are joined with ``/`` and the separator is escaped inside parts, so that
    ``("comp", "a/b")`` and ``("comp/a", "b")`` cannot collide.
    """
    if not parts:
        raise ValueError("a source key needs at least one part")
    return "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def element_uuid(*parts: str | int) -> str:
    """The UUID of the element identified by ``parts``.

    >>> element_uuid("component", "U1")
    '76d1ceda-6a12-5031-88a9-d667de280c45'
    """
    return str(uuid.uuid5(AIPCB_NAMESPACE, source_key(*parts)))


def net_codes(names: Iterable[str]) -> dict[str, int]:
    """Assign KiCad net codes to net names, deterministically.

    Codes are handed out in sorted-name order starting at 1, because KiCad expects
    small sequential integers and writes them that way itself. Sorting rather than
    hashing keeps the numbers idiomatic and the output reproducible; the cost is
    that adding a net shifts the codes after it alphabetically, which is harmless
    because KiCad renumbers nets on load anyway. Code 0 is reserved by KiCad for
    the unconnected net and is never assigned.
    """
    return {name: i for i, name in enumerate(sorted(set(names)), start=1)}
