# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Loading and schema-validating designs and part libraries.

pydantic's own error text is written for Python programmers and is missing the one
thing that matters most here: where in the file the problem is. This module turns a
``ValidationError`` into located :class:`~aipcb.diagnostics.Diagnostic` objects,
adds "did you mean" suggestions for misspelled fields, and rewrites the messages
that read badly out of context.
"""

from __future__ import annotations

import difflib
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from aipcb.diagnostics import Diagnostic, Report, Severity
from aipcb.model.design import Design
from aipcb.model.parts import Part, PartLibrary
from aipcb.source import LoadedSource, Loc, SourceError, SourceMap, load_yaml

__all__ = ["LoadedDesign", "load_design", "load_part_libraries", "validation_diagnostics"]


# ---------------------------------------------------------------------------
# mapping pydantic errors onto the source file
# ---------------------------------------------------------------------------

#: pydantic error types whose default message is unhelpful without the schema.
_MESSAGE_OVERRIDES = {
    "extra_forbidden": "unknown field",
    "missing": "required field is missing",
}


def _unwrap(annotation: Any) -> Any:
    """Strip Optional/Union/Annotated down to a single model class where possible."""
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    if origin in (Union, types.UnionType):
        candidates = [a for a in get_args(annotation) if a is not type(None)]
        return candidates[0] if len(candidates) == 1 else annotation
    return annotation


def _model_at(root: type[BaseModel], path: tuple[Any, ...]) -> type[BaseModel] | None:
    """Walk a loc path from ``root`` and return the model class it lands in.

    Used to offer field-name suggestions: to say "did you mean ``footprint``" we
    have to know which model the unknown key appeared in.
    """
    current: Any = root
    for element in path:
        if _is_model(current):
            field = current.model_fields.get(str(element))
            if field is None:
                return None
            current = _unwrap(field.annotation)
            continue
        # A container: the path element is a dict key or a list index, so descend
        # into the container's value type rather than treating it as a field name.
        origin, args = get_origin(current), get_args(current)
        if origin is dict and len(args) == 2:
            current = _unwrap(args[1])
        elif (origin in (list, set, frozenset) and args) or (origin is tuple and args):
            current = _unwrap(args[0])
        else:
            return None
    return current if _is_model(current) else None


def _is_model(obj: Any) -> bool:
    return isinstance(obj, type) and issubclass(obj, BaseModel)


def _field_names(model: type[BaseModel]) -> list[str]:
    """Field names as they are written in YAML, preferring aliases."""
    return sorted(
        (f.alias or name) for name, f in model.model_fields.items()
    )


def _suggest(unknown: str, options: list[str]) -> str | None:
    match = difflib.get_close_matches(unknown, options, n=1, cutoff=0.6)
    return match[0] if match else None


def validation_diagnostics(
    exc: ValidationError,
    smap: SourceMap,
    root_model: type[BaseModel],
    *,
    prefix: tuple[str | int, ...] = (),
) -> list[Diagnostic]:
    """Convert a pydantic ``ValidationError`` into located diagnostics."""
    out: list[Diagnostic] = []
    for err in exc.errors():
        loc_path = tuple(err["loc"])
        etype = str(err["type"])
        message = _MESSAGE_OVERRIDES.get(etype, err["msg"])
        hint: str | None = None

        if etype == "extra_forbidden" and loc_path:
            bad = str(loc_path[-1])
            owner = _model_at(root_model, loc_path[:-1])
            message = f"unknown field {bad!r}"
            if owner is not None:
                options = _field_names(owner)
                near = _suggest(bad, options)
                hint = (
                    f"did you mean {near!r}?"
                    if near
                    else f"allowed fields here: {', '.join(options)}"
                )
        elif etype == "missing" and loc_path:
            message = f"required field {str(loc_path[-1])!r} is missing"
        elif etype == "value_error":
            # Our own validators already phrase these for a human; keep them as-is
            # but strip pydantic's "Value error, " prefix.
            message = message.removeprefix("Value error, ")
        elif etype in ("string_pattern_mismatch", "string_too_short"):
            ctx = err.get("ctx") or {}
            pattern = ctx.get("pattern")
            if pattern:
                hint = f"must match the pattern {pattern}"

        full_path = prefix + loc_path
        out.append(
            Diagnostic(
                severity=Severity.ERROR,
                code=f"schema-{etype.replace('_', '-')}",
                message=message,
                loc=smap.get(loc_path),
                hint=hint,
                path=full_path,
                context={"input": _short(err.get("input"))},
            )
        )
    return out


def _short(value: Any, limit: int = 120) -> Any:
    """Shrink an offending value so reports stay readable."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {"<keys>": sorted(map(str, value))[:12]}
    if isinstance(value, (list, tuple)):
        return [_short(v, 40) for v in value[:6]]
    return value


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


class LoadedDesign:
    """A validated design plus everything needed to report about it."""

    __slots__ = ("design", "part_sources", "parts", "report", "source")

    def __init__(
        self,
        design: Design,
        source: LoadedSource,
        parts: dict[str, Part],
        part_sources: dict[str, Loc],
        report: Report,
    ) -> None:
        self.design = design
        self.source = source
        self.parts = parts
        self.part_sources = part_sources
        self.report = report

    @property
    def path(self) -> Path:
        return self.source.path

    def loc(self, *path: str | int) -> Loc | None:
        """Position of a source element, e.g. ``d.loc("components", "U1")``."""
        return self.source.smap.get(path)


def load_design(path: Path, *, report: Report | None = None) -> LoadedDesign:
    """Load ``design.yaml``, its part libraries, and validate the schema of both.

    Raises :class:`~aipcb.source.SourceError` if the file cannot be read at all.
    Schema problems are returned as diagnostics on the report and raise
    :class:`~aipcb.diagnostics.AipcbError` only via the caller's choice.
    """
    from aipcb.diagnostics import AipcbError

    report = report if report is not None else Report()
    loaded = load_yaml(path)

    try:
        design = Design.model_validate(loaded.data)
    except ValidationError as exc:
        report.extend(validation_diagnostics(exc, loaded.smap, Design))
        raise AipcbError(f"{path}: the design does not match the schema", report) from exc

    parts, part_sources = load_part_libraries(
        design.libraries, base=path.parent, report=report
    )
    return LoadedDesign(design, loaded, parts, part_sources, report)


def load_part_libraries(
    libraries: tuple[str, ...] | list[str],
    *,
    base: Path,
    report: Report,
) -> tuple[dict[str, Part], dict[str, Loc]]:
    """Load and merge part libraries, reporting conflicts rather than silently winning.

    Later libraries do not override earlier ones. A part defined twice is an error,
    because which definition wins would otherwise depend on list order -- a subtle
    way to build the wrong board.
    """
    parts: dict[str, Part] = {}
    origins: dict[str, Loc] = {}

    for index, entry in enumerate(libraries):
        lib_path = (base / entry).resolve()
        try:
            loaded = load_yaml(lib_path)
        except SourceError as exc:
            report.error(
                "library-unreadable",
                f"cannot read part library {entry!r}: {exc.message}",
                path=("libraries", index),
                hint="paths in `libraries:` are relative to the design file",
            )
            continue

        try:
            library = PartLibrary.model_validate(loaded.data)
        except ValidationError as exc:
            report.extend(validation_diagnostics(exc, loaded.smap, PartLibrary))
            continue

        for name, part in library.parts.items():
            if name in parts:
                report.error(
                    "duplicate-part",
                    f"part {name!r} is defined in more than one library",
                    loc=loaded.smap.get(("parts", name)),
                    path=("parts", name),
                    hint=f"already defined at {origins[name]}",
                )
                continue
            parts[name] = part
            loc = loaded.smap.get(("parts", name))
            if loc is not None:
                origins[name] = loc

    return parts, origins
