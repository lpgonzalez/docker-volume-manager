"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Unit tests for the wizard's paged selector. Input is fed by stubbing
``completion.ask``; the rendered table is allowed to print (captured by pytest).
"""

from __future__ import annotations

import completion
import wizard_ui


def _feed(monkeypatch, answers):
    """Stub completion.ask to return queued answers in order."""
    queue = list(answers)

    def fake_ask(prompt, comp, **kwargs):
        return queue.pop(0)

    monkeypatch.setattr(completion, "ask", fake_ask)


def test_select_by_number(monkeypatch):
    _feed(monkeypatch, ["2"])
    assert wizard_ui.select_paged("Pick", ["a", "b", "c"]) == "b"


def test_select_by_name(monkeypatch):
    _feed(monkeypatch, ["c"])
    assert wizard_ui.select_paged("Pick", ["a", "b", "c"]) == "c"


def test_blank_returns_default(monkeypatch):
    _feed(monkeypatch, [""])
    assert wizard_ui.select_paged("Pick", ["a", "b"], default="b") == "b"


def test_allow_custom_returns_typed_value(monkeypatch):
    _feed(monkeypatch, ["brand-new"])
    assert (
        wizard_ui.select_paged("Name", ["old1", "old2"], allow_custom=True)
        == "brand-new"
    )


def test_invalid_then_valid_reprompts(monkeypatch):
    _feed(monkeypatch, ["zzz", "1"])
    assert wizard_ui.select_paged("Pick", ["a", "b"]) == "a"


def test_out_of_range_number_reprompts(monkeypatch):
    _feed(monkeypatch, ["99", "2"])
    assert wizard_ui.select_paged("Pick", ["a", "b"]) == "b"


def test_pagination_next_then_global_index(monkeypatch):
    items = [f"item{i:02d}" for i in range(25)]
    # 'n' advances to page 2, then global number 11 selects items[10].
    _feed(monkeypatch, ["n", "11"])
    assert wizard_ui.select_paged("Pick", items, page_size=10) == items[10]


def test_empty_list_with_custom(monkeypatch):
    _feed(monkeypatch, ["typed"])
    assert wizard_ui.select_paged("Name", [], allow_custom=True) == "typed"


def test_empty_list_without_custom_returns_default(monkeypatch):
    # No prompt should be issued; default is returned directly.
    called = {"n": 0}

    def fake_ask(*a, **k):
        called["n"] += 1
        return ""

    monkeypatch.setattr(completion, "ask", fake_ask)
    assert wizard_ui.select_paged("Pick", [], default=None) is None
    assert called["n"] == 0
