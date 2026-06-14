"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Unit tests for the wizard's 3-way location picker and host-path browser.
Prompts and helper-container listings are stubbed.
"""

from __future__ import annotations

import wizard
from wizard_store import LocalDirStore


class _FakeStore:
    label = "/fake"
    mount_key = "myvol"


def _kind(monkeypatch, value):
    """Stub completion.ask: return `value` for the kind prompt, "/typed" otherwise."""
    monkeypatch.setattr(
        wizard.completion,
        "ask",
        lambda prompt, *a, **k: value if "location type" in prompt else "/typed/path",
    )


def test_location_local(monkeypatch):
    _kind(monkeypatch, "local")
    path, vol, host, store = wizard._wizard_location("L", "/dvm/source", purpose="src")
    assert (path, vol, host) == ("/typed/path", None, None)
    assert isinstance(store, LocalDirStore)


def test_location_volume(monkeypatch):
    _kind(monkeypatch, "volume")
    fake = _FakeStore()
    monkeypatch.setattr(wizard, "_wizard_volume_location", lambda purpose: fake)
    path, vol, host, store = wizard._wizard_location("L", "/dvm/dest", purpose="dst")
    assert (path, vol, host) == ("/dvm/dest", "myvol", None)
    assert store is fake


def test_location_host(monkeypatch):
    _kind(monkeypatch, "host")
    fake = _FakeStore()
    monkeypatch.setattr(
        wizard, "_wizard_host_location", lambda purpose: ("/host/p", fake)
    )
    path, vol, host, store = wizard._wizard_location("L", "/dvm/dest", purpose="dst")
    assert (path, vol, host) == ("/dvm/dest", None, "/host/p")
    assert store is fake


def test_location_volume_cancel_falls_back_to_local(monkeypatch):
    _kind(monkeypatch, "volume")
    monkeypatch.setattr(wizard, "_wizard_volume_location", lambda purpose: None)
    _path, vol, host, store = wizard._wizard_location("L", "/dvm/source", purpose="src")
    assert (vol, host) == (None, None)
    assert isinstance(store, LocalDirStore)


# --- host browser navigation -------------------------------------------------


def _browse(monkeypatch, subdirs, answers):
    monkeypatch.setattr(wizard, "list_host_subdirs", lambda c, p: subdirs)
    it = iter(answers)
    monkeypatch.setattr(wizard.wizard_ui, "select_paged", lambda *a, **k: next(it))


def test_browse_use_current(monkeypatch):
    _browse(monkeypatch, ["a", "b"], [wizard._HB_USE])
    assert wizard._wizard_browse_host(object(), "/start") == "/start"


def test_browse_descend_then_use(monkeypatch):
    _browse(monkeypatch, ["sub"], ["sub", wizard._HB_USE])
    assert wizard._wizard_browse_host(object(), "/start") == "/start/sub"


def test_browse_go_up_then_use(monkeypatch):
    _browse(monkeypatch, [], [wizard._HB_UP, wizard._HB_USE])
    assert wizard._wizard_browse_host(object(), "/a/b") == "/a"


def test_browse_type_absolute_path(monkeypatch):
    _browse(monkeypatch, [], ["/jump/here", wizard._HB_USE])
    assert wizard._wizard_browse_host(object(), "/start") == "/jump/here"


def test_browse_cancel(monkeypatch):
    _browse(monkeypatch, [], [None])
    assert wizard._wizard_browse_host(object(), "/x") is None
