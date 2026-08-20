"""Flattening a hierarchical design into a netlist.

Elaboration walks the instance tree, and for each module instance it:

1. binds the module's parameters, substituting them into the components it defines;
2. resolves the module's net names -- a name listed in ``ports`` becomes whatever
   the parent connected it to, any other name becomes a local net prefixed with the
   instance path, so two instances of the same module do not short together;
3. recurses into nested instances;
4. assigns reference designators, deterministically.

The result is a :class:`~aipcb.netlist.Netlist`. Problems are reported as
diagnostics rather than raised, so one run surfaces every problem in the design
instead of stopping at the first.
"""

from __future__ import annotations

import re
from typing import Any

from aipcb.diagnostics import Report
from aipcb.loader import LoadedDesign
from aipcb.model.design import (
    KNOWN_NET_CLASSES,
    KNOWN_ROLES,
    REFDES_RE,
    Component,
    Design,
    Instance,
    Module,
    Net,
    Param,
)
from aipcb.netlist import ElabComponent, ElabConstraint, ElabNet, Netlist, Node
from aipcb.source import SourceMap

__all__ = ["MAX_DEPTH", "elaborate"]

#: Guards against a module that instantiates itself, directly or through a cycle.
MAX_DEPTH = 32

_SUBST_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class _Elaborator:
    def __init__(self, loaded: LoadedDesign, report: Report) -> None:
        self.loaded = loaded
        self.design: Design = loaded.design
        self.smap: SourceMap = loaded.source.smap
        self.report = report
        self.components: list[ElabComponent] = []
        self.net_attrs: dict[str, tuple[Net, tuple[str | int, ...]]] = {}
        self.nodes: dict[str, list[Node]] = {}
        self.constraints: list[ElabConstraint] = []
        self._pending_refdes: list[tuple[ElabComponent, str]] = []

    # -- entry point -----------------------------------------------------------

    def run(self) -> Netlist:
        design = self.design

        for name, net in design.nets.items():
            self.net_attrs[name] = (net, ("nets", name))

        self._scope(
            components=design.components,
            instances=design.instances,
            constraints=design.constraints,
            net_map={},
            prefix=(),
            source_prefix=(),
            depth=0,
            local_nets=set(),
        )

        components = self._assign_refdes()
        nets = self._build_nets(components)
        return Netlist(
            name=design.name,
            revision=design.revision,
            description=design.description,
            components={c.refdes: c for c in components},
            nets=nets,
            constraints=tuple(self.constraints),
            net_classes=dict(design.net_classes),
            layout=design.layout,
        )

    # -- scope walking ---------------------------------------------------------

    def _scope(
        self,
        *,
        components: dict[str, Component],
        instances: dict[str, Instance],
        constraints: tuple[Any, ...],
        net_map: dict[str, str],
        prefix: tuple[str, ...],
        source_prefix: tuple[str | int, ...],
        depth: int,
        local_nets: set[str],
        params: dict[str, Any] | None = None,
    ) -> None:
        """Elaborate one scope: the design root, or one module instance."""
        params = params or {}

        def resolve_net(name: str) -> str:
            """Map a net name written inside this scope to its global name."""
            if name in net_map:
                return net_map[name]
            if not prefix:
                return name
            return ".".join((*prefix, name))

        for local_name, component in sorted(components.items()):
            comp_source = (*source_prefix, "components", local_name)
            for suffix, index in _repeat(component, params, self.report, self.smap, comp_source):
                name = f"{local_name}{suffix}"
                self._emit_component(
                    local_name=name,
                    component=component,
                    params={**params, "index": index},
                    prefix=prefix,
                    source_path=comp_source,
                    resolve_net=resolve_net,
                )

        for constraint, index in _numbered(constraints):
            self._emit_constraint(constraint, index, prefix, source_prefix)

        for inst_name, instance in sorted(instances.items()):
            self._emit_instance(
                inst_name=inst_name,
                instance=instance,
                prefix=prefix,
                source_prefix=(*source_prefix, "instances", inst_name),
                resolve_net=resolve_net,
                depth=depth,
                outer_params=params,
            )

    def _emit_instance(
        self,
        *,
        inst_name: str,
        instance: Instance,
        prefix: tuple[str, ...],
        source_prefix: tuple[str | int, ...],
        resolve_net: Any,
        depth: int,
        outer_params: dict[str, Any],
    ) -> None:
        loc = self.smap.get(source_prefix)
        if depth >= MAX_DEPTH:
            self.report.error(
                "module-recursion",
                f"module nesting exceeded {MAX_DEPTH} levels at "
                f"{'.'.join((*prefix, inst_name))}",
                loc=loc,
                path=source_prefix,
                hint="a module probably instantiates itself, directly or in a cycle",
            )
            return

        module = self.design.modules.get(instance.module)
        if module is None:
            known = ", ".join(sorted(self.design.modules)) or "none are defined"
            self.report.error(
                "unknown-module",
                f"instance {inst_name!r} refers to undefined module {instance.module!r}",
                loc=loc,
                path=(*source_prefix, "module"),
                hint=f"modules available: {known}",
            )
            return

        params = self._bind_params(module, instance, inst_name, source_prefix)
        net_map = self._bind_ports(module, instance, inst_name, source_prefix, resolve_net)

        self._scope(
            components=module.components,
            instances=module.instances,
            constraints=module.constraints,
            net_map=net_map,
            prefix=(*prefix, inst_name),
            source_prefix=source_prefix,
            depth=depth + 1,
            local_nets=set(module.nets),
            params=params,
        )

        # Nets declared inside the module but not exposed as ports still carry
        # attributes (voltage, class); register them under their hierarchical name.
        for net_name, net in module.nets.items():
            if net_name in module.ports:
                continue
            full = ".".join((*prefix, inst_name, net_name))
            self.net_attrs.setdefault(full, (net, (*source_prefix, "nets", net_name)))

    # -- parameters and ports --------------------------------------------------

    def _bind_params(
        self,
        module: Module,
        instance: Instance,
        inst_name: str,
        source_prefix: tuple[str | int, ...],
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, spec in module.params.items():
            if name in instance.params:
                values[name] = _coerce(
                    instance.params[name], spec, name, inst_name,
                    self.report, self.smap, (*source_prefix, "params", name),
                )
            elif spec.required:
                self.report.error(
                    "missing-param",
                    f"instance {inst_name!r} does not supply required parameter {name!r}",
                    loc=self.smap.get(source_prefix),
                    path=(*source_prefix, "params"),
                    hint=spec.description or f"expected a value of type {spec.type}",
                )
            else:
                values[name] = spec.default

        for extra in sorted(set(instance.params) - set(module.params)):
            known = ", ".join(sorted(module.params)) or "it declares none"
            self.report.error(
                "unknown-param",
                f"module {instance.module!r} has no parameter {extra!r}",
                loc=self.smap.get((*source_prefix, "params", extra)),
                path=(*source_prefix, "params", extra),
                hint=f"parameters of {instance.module!r}: {known}",
            )
        return values

    def _bind_ports(
        self,
        module: Module,
        instance: Instance,
        inst_name: str,
        source_prefix: tuple[str | int, ...],
        resolve_net: Any,
    ) -> dict[str, str]:
        net_map: dict[str, str] = {}
        for port in module.ports:
            if port in instance.connect:
                net_map[port] = resolve_net(instance.connect[port])
            else:
                self.report.error(
                    "unconnected-port",
                    f"instance {inst_name!r} leaves port {port!r} of module "
                    f"{instance.module!r} unconnected",
                    loc=self.smap.get((*source_prefix, "connect")),
                    path=(*source_prefix, "connect"),
                    hint=f"add `{port}: <net>` under `connect:`",
                )

        for extra in sorted(set(instance.connect) - set(module.ports)):
            known = ", ".join(module.ports) or "it declares none"
            self.report.error(
                "unknown-port",
                f"module {instance.module!r} has no port {extra!r}",
                loc=self.smap.get((*source_prefix, "connect", extra)),
                path=(*source_prefix, "connect", extra),
                hint=f"ports of {instance.module!r}: {known}",
            )
        return net_map

    # -- components ------------------------------------------------------------

    def _emit_component(
        self,
        *,
        local_name: str,
        component: Component,
        params: dict[str, Any],
        prefix: tuple[str, ...],
        source_path: tuple[str | int, ...],
        resolve_net: Any,
    ) -> None:
        loc = self.smap.get(source_path)
        hier = (*prefix, local_name)

        part_name = _substitute(component.part, params)
        part = self.loaded.parts.get(part_name)
        if part is None:
            self.report.error(
                "unknown-part",
                f"no part named {part_name!r} in the component database",
                loc=loc,
                path=(*source_path, "part"),
                hint=_nearest_part(part_name, self.loaded.parts),
                component=".".join(hier),
            )

        if component.role and component.role not in KNOWN_ROLES:
            self.report.warning(
                "unknown-role",
                f"role {component.role!r} is not one this toolchain knows about",
                loc=loc,
                path=(*source_path, "role"),
                hint="checks that key off roles will skip this component; "
                f"known roles include {', '.join(sorted(KNOWN_ROLES)[:6])}",
            )

        connections: dict[str, str] = {}
        for pin_ref, net_name in component.pins.items():
            resolved_net = resolve_net(_substitute(net_name, params))
            if part is None:
                connections[pin_ref] = resolved_net
                continue
            pin_number = part.resolve_pin(pin_ref)
            if pin_number is None:
                self.report.error(
                    "unknown-pin",
                    f"part {part_name!r} has no pin {pin_ref!r}",
                    loc=self.smap.get((*source_path, "pins", pin_ref)),
                    path=(*source_path, "pins", pin_ref),
                    hint=f"pins of {part_name!r}: {_pin_summary(part)}",
                    component=".".join(hier),
                )
                continue
            if pin_number in connections:
                self.report.error(
                    "duplicate-pin",
                    f"pin {pin_number!r} of {'.'.join(hier)} is connected twice",
                    loc=self.smap.get((*source_path, "pins", pin_ref)),
                    path=(*source_path, "pins", pin_ref),
                    hint=f"already connected to {connections[pin_number]!r}; a pin "
                    "can only be on one net",
                )
                continue
            connections[pin_number] = resolved_net

        elab = ElabComponent(
            refdes="",  # assigned later, once every component is known
            part_name=part_name,
            part=part,
            hier=hier,
            source_path=source_path,
            loc=loc,
            connections=connections,
            role=component.role,
            for_ref=_substitute(component.for_, params) if component.for_ else None,
            reason=component.reason,
            value=_substitute(component.value, params) if component.value else None,
            dnp=component.dnp,
        )
        self.components.append(elab)
        self._pending_refdes.append((elab, component.refdes or ""))

    # -- constraints -----------------------------------------------------------

    def _emit_constraint(
        self,
        constraint: Any,
        index: int,
        prefix: tuple[str, ...],
        source_prefix: tuple[str | int, ...],
    ) -> None:
        members = (
            constraint.members if constraint.kind == "group" else constraint.between
        )
        source_path = (*source_prefix, "constraints", index)
        resolved = tuple(".".join((*prefix, m)) for m in members)
        self.constraints.append(
            ElabConstraint(
                constraint=constraint,
                members=resolved,
                source_path=source_path,
                loc=self.smap.get(source_path),
            )
        )

    # -- reference designators -------------------------------------------------

    def _assign_refdes(self) -> list[ElabComponent]:
        """Give every component a unique refdes, deterministically.

        Explicit ``refdes:`` wins. A top-level component whose key already looks
        like a designator (``U1``) keeps it, which is what makes small flat designs
        read naturally. Everything else is numbered per prefix in sorted
        hierarchical-path order, so the same source always yields the same numbers.
        """
        taken: dict[str, ElabComponent] = {}
        assigned: list[ElabComponent] = []
        deferred: list[ElabComponent] = []

        ordered = sorted(self.components, key=lambda c: c.hier)
        explicit = {id(c): r for c, r in self._pending_refdes if r}

        for component in ordered:
            wanted = explicit.get(id(component))
            if wanted is None and len(component.hier) == 1 and REFDES_RE.match(component.hier[0]):
                wanted = component.hier[0]
            if wanted is None:
                deferred.append(component)
                continue
            if wanted in taken:
                self.report.error(
                    "duplicate-refdes",
                    f"reference designator {wanted!r} is used by both "
                    f"{taken[wanted].path_text} and {component.path_text}",
                    loc=component.loc,
                    path=component.source_path,
                    hint="set an explicit `refdes:` on one of them",
                )
                deferred.append(component)
                continue
            placed = _with_refdes(component, wanted)
            taken[wanted] = placed
            assigned.append(placed)

        counters: dict[str, int] = {}
        for component in deferred:
            prefix = _refdes_prefix(component)
            n = counters.get(prefix, 0)
            while True:
                n += 1
                candidate = f"{prefix}{n}"
                if candidate not in taken:
                    break
            counters[prefix] = n
            placed = _with_refdes(component, candidate)
            taken[candidate] = placed
            assigned.append(placed)

        return assigned

    # -- nets ------------------------------------------------------------------

    def _build_nets(self, components: list[ElabComponent]) -> dict[str, ElabNet]:
        nodes: dict[str, list[Node]] = {}
        for component in sorted(components, key=lambda c: c.hier):
            for pin_number, net_name in sorted(component.connections.items()):
                pin_name = pin_number
                if component.part is not None:
                    pin = component.part.pins.get(pin_number)
                    if pin is not None:
                        pin_name = pin.name or pin_number
                nodes.setdefault(net_name, []).append(
                    Node(component.refdes, pin_number, pin_name)
                )

        nets: dict[str, ElabNet] = {}
        for name in sorted(set(nodes) | set(self.net_attrs)):
            attrs, source_path = self.net_attrs.get(name, (None, ()))
            implicit = attrs is None
            if attrs is None:
                attrs = Net()
            if attrs.net_class not in KNOWN_NET_CLASSES and (
                attrs.net_class not in self.design.net_classes
            ):
                self.report.warning(
                    "unknown-net-class",
                    f"net {name!r} uses class {attrs.net_class!r}, which is neither "
                    "a built-in class nor defined under `net_classes:`",
                    loc=self.smap.get(source_path) if source_path else None,
                    path=(*source_path, "class") if source_path else (),
                    hint="built-in classes: " + ", ".join(sorted(KNOWN_NET_CLASSES)),
                )
            nets[name] = ElabNet(
                name=name,
                attrs=attrs,
                nodes=tuple(sorted(nodes.get(name, []), key=lambda n: (n.refdes, n.pin))),
                source_path=source_path,
                loc=self.smap.get(source_path) if source_path else None,
                implicit=implicit,
            )
        return nets


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _with_refdes(component: ElabComponent, refdes: str) -> ElabComponent:
    from dataclasses import replace

    return replace(component, refdes=refdes)


def _refdes_prefix(component: ElabComponent) -> str:
    """Pick a designator prefix: the component's own name, else its role, else ``U``."""
    leading = re.match(r"^([A-Za-z]+)", component.hier[-1])
    if leading:
        return leading.group(1).upper()
    return "U"


def _numbered(items: tuple[Any, ...]) -> list[tuple[Any, int]]:
    return [(item, i) for i, item in enumerate(items)]


def _repeat(
    component: Component,
    params: dict[str, Any],
    report: Report,
    smap: SourceMap,
    source_path: tuple[str | int, ...],
) -> list[tuple[str, int]]:
    """Expand ``count:`` into name suffixes. ``count: 1`` produces no suffix."""
    raw: Any = component.count
    if isinstance(raw, str):
        raw = _substitute(raw, params)
    try:
        count = int(raw)
    except (TypeError, ValueError):
        report.error(
            "bad-count",
            f"count must be a whole number, got {raw!r}",
            loc=smap.get(source_path),
            path=(*source_path, "count"),
        )
        count = 1
    if count <= 1:
        return [("", 1)]
    return [(f"_{i}", i) for i in range(1, count + 1)]


def _substitute(text: str | None, params: dict[str, Any]) -> Any:
    """Replace ``{{ param }}`` references.

    A string that is *only* a reference keeps the parameter's type, so
    ``count: "{{ n }}"`` yields an int. Otherwise the value is interpolated as text.
    Unknown names are left alone; the schema check that follows will complain about
    the literal braces, which is a clearer error than a silent empty string.
    """
    if text is None or not isinstance(text, str) or "{{" not in text:
        return text
    whole = _SUBST_RE.fullmatch(text.strip())
    if whole is not None and whole.group(1) in params:
        return params[whole.group(1)]
    return _SUBST_RE.sub(
        lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), text
    )


def _coerce(
    value: Any,
    spec: Param,
    name: str,
    inst_name: str,
    report: Report,
    smap: SourceMap,
    source_path: tuple[str | int, ...],
) -> Any:
    """Check a parameter value against its declared type."""
    want = spec.type
    try:
        if want == "int":
            return int(value)
        if want == "float":
            return float(value)
        if want == "bool":
            if isinstance(value, bool):
                return value
            raise ValueError("expected true or false")
        return str(value)
    except (TypeError, ValueError) as exc:
        report.error(
            "bad-param-type",
            f"parameter {name!r} of instance {inst_name!r} expects {want}, "
            f"got {value!r} ({exc})",
            loc=smap.get(source_path),
            path=source_path,
        )
        return spec.default


def _nearest_part(name: str, parts: dict[str, Any]) -> str:
    import difflib

    near = difflib.get_close_matches(name, list(parts), n=3, cutoff=0.5)
    if near:
        return "did you mean " + ", ".join(repr(n) for n in near) + "?"
    if not parts:
        return "no part libraries are loaded; add one under `libraries:`"
    return f"{len(parts)} parts are available; run `aipcb parts` to list them"


def _pin_summary(part: Any, limit: int = 10) -> str:
    labels = [
        f"{number}" if not pin.name or pin.name == number else f"{number} ({pin.name})"
        for number, pin in list(part.pins.items())[:limit]
    ]
    more = "" if len(part.pins) <= limit else f", … {len(part.pins) - limit} more"
    return ", ".join(labels) + more


def elaborate(loaded: LoadedDesign, report: Report | None = None) -> Netlist:
    """Flatten a loaded design into a netlist, reporting problems as it goes."""
    report = report if report is not None else loaded.report
    return _Elaborator(loaded, report).run()
