# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Structured, source-referenced diagnostics.

Every problem `aipcb` reports -- a schema violation, an unresolvable part, a DRC
error handed back by KiCad -- becomes a :class:`Diagnostic`. They all carry the
same shape so the agent's feedback loop has exactly one thing to parse, whether
the message came from our validator or from ``kicad-cli``.

A good diagnostic answers three questions: what is wrong, where is it, and what
would fix it. The ``hint`` field is for the third, and is worth filling in
wherever the answer is knowable.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from aipcb.source import Loc

__all__ = ["AipcbError", "Diagnostic", "Report", "Severity"]


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"error": 0, "warning": 1, "info": 2}[self.value]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One problem, located in the source that caused it."""

    severity: Severity
    code: str
    """A stable kebab-case identifier, e.g. ``unknown-part``. Safe to match on."""
    message: str
    loc: Loc | None = None
    hint: str | None = None
    path: tuple[str | int, ...] = ()
    """The path into the source data, e.g. ``("components", "U1", "part")``."""
    context: dict[str, Any] = field(default_factory=dict)
    """Extra machine-readable detail: the offending net, the KiCad violation type."""

    def render(self, *, color: bool = False) -> str:
        """One diagnostic as human-readable text."""
        tag = self.severity.value
        if color:
            hue = {"error": "31", "warning": "33", "info": "36"}[tag]
            tag = f"\x1b[{hue}m{tag}\x1b[0m"
        where = f"{self.loc}: " if self.loc else ""
        head = f"{where}{tag}[{self.code}]: {self.message}"
        lines = [head]
        if self.path:
            lines.append(f"  at: {format_path(self.path)}")
        if self.hint:
            lines.append(f"  hint: {self.hint}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """The JSON form, for ``--json`` output."""
        out: dict[str, Any] = {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
        }
        if self.loc:
            out["location"] = {
                "file": str(self.loc.file),
                "line": self.loc.line,
                "column": self.loc.col,
            }
        if self.path:
            out["path"] = list(self.path)
            out["path_text"] = format_path(self.path)
        if self.hint:
            out["hint"] = self.hint
        if self.context:
            out["context"] = self.context
        return out


def summarise(items: Iterable[str], limit: int = 8) -> str:
    """Join names for a message, truncating long lists rather than flooding one."""
    values = list(items)
    head = ", ".join(values[:limit])
    return head if len(values) <= limit else f"{head}, … {len(values) - limit} more"


def format_path(path: Iterable[str | int]) -> str:
    """Render a source path the way a person would write it: ``components.U1.part``."""
    parts: list[str] = []
    for element in path:
        if isinstance(element, int):
            parts.append(f"[{element}]")
        elif parts:
            parts.append(f".{element}")
        else:
            parts.append(str(element))
    return "".join(parts) or "<document>"


class Report:
    """A collection of diagnostics, plus the verdict they add up to."""

    __slots__ = ("diagnostics",)

    def __init__(self, diagnostics: Iterable[Diagnostic] = ()) -> None:
        self.diagnostics: list[Diagnostic] = list(diagnostics)

    def add(
        self,
        severity: Severity,
        code: str,
        message: str,
        *,
        loc: Loc | None = None,
        hint: str | None = None,
        path: tuple[str | int, ...] = (),
        **context: Any,
    ) -> Diagnostic:
        diag = Diagnostic(severity, code, message, loc, hint, path, context)
        self.diagnostics.append(diag)
        return diag

    def error(self, code: str, message: str, **kw: Any) -> Diagnostic:
        return self.add(Severity.ERROR, code, message, **kw)

    def warning(self, code: str, message: str, **kw: Any) -> Diagnostic:
        return self.add(Severity.WARNING, code, message, **kw)

    def info(self, code: str, message: str, **kw: Any) -> Diagnostic:
        return self.add(Severity.INFO, code, message, **kw)

    def extend(self, others: Iterable[Diagnostic]) -> None:
        self.diagnostics.extend(others)

    # -- verdict --------------------------------------------------------------

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def __len__(self) -> int:
        return len(self.diagnostics)

    def __iter__(self) -> Iterator[Diagnostic]:
        return iter(self.diagnostics)

    def __bool__(self) -> bool:
        return bool(self.diagnostics)

    # -- output ---------------------------------------------------------------

    def sorted(self) -> list[Diagnostic]:
        """Errors first, then by file position -- the order a reader wants."""
        return sorted(
            self.diagnostics,
            key=lambda d: (
                d.severity.rank,
                str(d.loc.file) if d.loc else "",
                d.loc.line if d.loc else 0,
                d.loc.col if d.loc else 0,
                d.code,
            ),
        )

    def render(self, *, color: bool = False, summary: bool = True) -> str:
        """The whole report as human-readable text."""
        if not self.diagnostics:
            return "ok: no problems found" if summary else ""
        blocks = [d.render(color=color) for d in self.sorted()]
        if summary:
            n_err, n_warn = len(self.errors), len(self.warnings)
            parts = []
            if n_err:
                parts.append(f"{n_err} error{'s' if n_err != 1 else ''}")
            if n_warn:
                parts.append(f"{n_warn} warning{'s' if n_warn != 1 else ''}")
            other = len(self.diagnostics) - n_err - n_warn
            if other:
                parts.append(f"{other} note{'s' if other != 1 else ''}")
            blocks.append("")
            blocks.append(", ".join(parts))
        return "\n".join(blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": {
                "error": len(self.errors),
                "warning": len(self.warnings),
                "total": len(self.diagnostics),
            },
            "diagnostics": [d.to_dict() for d in self.sorted()],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)


class AipcbError(Exception):
    """A failure that stops the run, carrying its diagnostics for reporting."""

    def __init__(self, message: str, report: Report | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.report = report if report is not None else Report()
