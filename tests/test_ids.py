# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Deterministic identity -- the property everything else is built on."""

from __future__ import annotations

import subprocess
import sys

from aipcb.ids import element_uuid, net_codes, source_key


class TestElementUuid:
    def test_is_stable_within_a_process(self) -> None:
        assert element_uuid("component", "U1") == element_uuid("component", "U1")

    def test_is_stable_across_processes(self) -> None:
        """A UUID that changed per interpreter would churn every generated file."""
        code = "from aipcb.ids import element_uuid; print(element_uuid('component', 'U1'))"
        first = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        second = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": "1"},
        ).stdout.strip()
        assert first == second == element_uuid("component", "U1")

    def test_distinct_paths_give_distinct_uuids(self) -> None:
        assert element_uuid("component", "U1") != element_uuid("component", "U2")
        assert element_uuid("component", "U1") != element_uuid("net", "U1")

    def test_hierarchical_paths_do_not_collide(self) -> None:
        assert element_uuid("component", "a", "b") != element_uuid("component", "a.b")

    def test_separator_in_a_name_cannot_forge_another_path(self) -> None:
        assert source_key("comp", "a/b") != source_key("comp/a", "b")
        assert element_uuid("comp", "a/b") != element_uuid("comp/a", "b")

    def test_looks_like_a_uuid(self) -> None:
        import uuid

        value = element_uuid("component", "U1")
        assert uuid.UUID(value).version == 5


class TestNetCodes:
    def test_starts_at_one(self) -> None:
        """KiCad reserves net code 0 for the unconnected net."""
        assert min(net_codes(["A", "B", "C"]).values()) == 1

    def test_is_dense_and_sorted(self) -> None:
        assert net_codes(["VCC", "GND", "SDA"]) == {"GND": 1, "SDA": 2, "VCC": 3}

    def test_is_deterministic(self) -> None:
        names = ["b", "a", "c"]
        assert net_codes(names) == net_codes(reversed(names))

    def test_duplicates_collapse(self) -> None:
        assert net_codes(["A", "A", "B"]) == {"A": 1, "B": 2}
