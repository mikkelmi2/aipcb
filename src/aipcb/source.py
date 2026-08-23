# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Loading YAML source with position tracking.

Every diagnostic `aipcb` emits should point at the exact line and column of the
element that caused it, because the agent reading that diagnostic has to find and
fix the element. Standard ``yaml.safe_load`` throws positions away, so we compose
the node graph ourselves and walk it, producing the plain Python data *and* a
:class:`SourceMap` from path tuples to :class:`Loc` positions.

The walker also applies three strictness rules that plain YAML gets wrong for this
use case:

* ``yes``/``no``/``on``/``off``/``y``/``n`` stay strings. A net legitimately named
  ``NO`` or a part field ``ON`` must not silently become a boolean -- the "Norway
  problem". Only ``true``/``false`` produce booleans.
* Duplicate mapping keys are an error, not a silent last-one-wins overwrite.
* Non-string mapping keys are an error, so ``1:`` and ``"1":`` cannot collide.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = ["LoadedSource", "Loc", "SourceError", "SourceMap", "load_yaml"]

_TRUE = frozenset({"true", "True", "TRUE"})
_FALSE = frozenset({"false", "False", "FALSE"})
Path_ = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class Loc:
    """A position in a source file. Lines and columns are 1-based, as editors count."""

    file: Path
    line: int
    col: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"


class SourceError(Exception):
    """A YAML file could not be loaded. Carries a position when one is known."""

    def __init__(self, message: str, loc: Loc | None = None, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.loc = loc
        self.hint = hint

    def __str__(self) -> str:
        head = f"{self.loc}: {self.message}" if self.loc else self.message
        return f"{head}\n  hint: {self.hint}" if self.hint else head


class SourceMap:
    """Maps a path into the loaded data to its position in the file.

    A path is the tuple of keys and indices used to reach a value, so
    ``("components", "U1", "part")`` addresses the ``part:`` field of ``U1``.
    Lookups fall back to the nearest known ancestor, so a diagnostic about a field
    we did not individually record still points somewhere useful rather than
    nowhere.
    """

    __slots__ = ("_locs", "file")

    def __init__(self, file: Path) -> None:
        self.file = file
        self._locs: dict[Path_, Loc] = {}

    def record(self, path: Path_, loc: Loc) -> None:
        self._locs[path] = loc

    def get(self, path: Path_) -> Loc | None:
        """Return the position of ``path``, or of its nearest recorded ancestor."""
        probe: Path_ = tuple(path)
        while True:
            if (loc := self._locs.get(probe)) is not None:
                return loc
            if not probe:
                return None
            probe = probe[:-1]

    def __len__(self) -> int:
        return len(self._locs)


@dataclass(slots=True)
class LoadedSource:
    """The result of loading one YAML file."""

    data: dict[str, Any]
    smap: SourceMap
    path: Path
    text: str


def _mark_loc(node: yaml.Node, file: Path) -> Loc:
    mark = node.start_mark
    return Loc(file, mark.line + 1, mark.column + 1)


def _scalar(node: yaml.ScalarNode, loc: Loc) -> Any:
    """Resolve a scalar, keeping YAML 1.1's over-eager booleans as strings."""
    raw = node.value
    if node.tag == "tag:yaml.org,2002:bool":
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
        return raw  # yes/no/on/off/y/n -- keep the text the author wrote
    if node.tag == "tag:yaml.org,2002:int":
        try:
            return int(raw, 0) if raw.lower().startswith(("0x", "0o", "0b")) else int(raw)
        except ValueError:
            return raw
    if node.tag == "tag:yaml.org,2002:float":
        try:
            return float(raw)
        except ValueError:
            return raw
    if node.tag == "tag:yaml.org,2002:null":
        return None
    if node.tag == "tag:yaml.org,2002:timestamp":
        # Dates in a design file are almost certainly a revision string.
        return raw
    return raw


def _walk(node: yaml.Node, path: Path_, smap: SourceMap, file: Path) -> Any:
    loc = _mark_loc(node, file)
    smap.record(path, loc)

    if isinstance(node, yaml.ScalarNode):
        return _scalar(node, loc)

    if isinstance(node, yaml.SequenceNode):
        return [_walk(child, (*path, i), smap, file) for i, child in enumerate(node.value)]

    if isinstance(node, yaml.MappingNode):
        out: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                raise SourceError(
                    "mapping keys must be plain text",
                    _mark_loc(key_node, file),
                    hint="complex keys (lists or mappings used as keys) are not supported",
                )
            key = key_node.value
            if key in out:
                raise SourceError(
                    f"duplicate key {key!r}",
                    _mark_loc(key_node, file),
                    hint="the earlier definition would be silently discarded; "
                    "rename one of them",
                )
            out[key] = _walk(value_node, (*path, key), smap, file)
            # Recorded last so it wins over the value's own position: a diagnostic
            # about a component should point at `U1:`, not at the block beneath it.
            smap.record((*path, key), _mark_loc(key_node, file))
        return out

    raise SourceError(f"unsupported YAML node {type(node).__name__}", loc)


def load_yaml(path: Path) -> LoadedSource:
    """Load one YAML file into plain data plus a :class:`SourceMap`."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SourceError(f"no such file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise SourceError(f"{path} is not valid UTF-8 text") from exc

    smap = SourceMap(path)
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        loc = Loc(path, mark.line + 1, mark.column + 1) if mark else None
        hint = None
        # PyYAML renders the offending character escaped, so the message contains
        # the two characters backslash-t rather than a tab.
        if exc.problem and ("\t" in exc.problem or r"\t" in exc.problem or "tab" in exc.problem):
            hint = "YAML forbids tabs for indentation; use spaces"
        raise SourceError(exc.problem or "invalid YAML", loc, hint=hint) from exc
    except yaml.YAMLError as exc:
        raise SourceError(f"invalid YAML: {exc}") from exc

    if node is None:
        raise SourceError(f"{path} is empty")

    data = _walk(node, (), smap, path)
    if not isinstance(data, dict):
        raise SourceError(
            f"expected a mapping at the top level, found {type(data).__name__}",
            smap.get(()),
            hint="a design file starts with keys such as `name:`, `nets:` and `components:`",
        )
    return LoadedSource(data=data, smap=smap, path=path, text=text)
