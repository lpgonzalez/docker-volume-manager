"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Unit tests for the wizard's TAB-completion matchers. These exercise the readline
``completer(text, state)`` callables directly (no TTY needed).
"""

from __future__ import annotations

import os

import completion


def _collect(comp, text):
    """Drain a readline completer: call fn(text, 0..N) until it returns None."""
    out = []
    state = 0
    while (hit := comp.fn(text, state)) is not None:
        out.append(hit)
        state += 1
    return out


def test_words_completes_prefix_in_order():
    comp = completion.words(["backup", "restore", "rename", "verify"])
    assert _collect(comp, "re") == ["restore", "rename"]
    assert _collect(comp, "v") == ["verify"]
    assert _collect(comp, "") == ["backup", "restore", "rename", "verify"]
    assert _collect(comp, "zzz") == []


def test_words_accepts_any_iterable():
    # Wizard passes a dict (volume name -> info); iterating yields the names.
    comp = completion.words({"vol-a": 1, "vol-b": 2})
    assert _collect(comp, "vol-") == ["vol-a", "vol-b"]


def test_csv_words_matches_same_options():
    comp = completion.csv_words(["console", "file", "json_file"])
    # The matcher itself is prefix-based; comma handling is via delimiters.
    assert _collect(comp, "j") == ["json_file"]
    assert _collect(comp, "f") == ["file"]


def test_paths_completes_files_and_dirs(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "album.txt").write_text("x")
    (tmp_path / "beta.txt").write_text("y")
    comp = completion.paths()

    hits = _collect(comp, str(tmp_path) + "/al")
    # Directory gets a trailing separator so the next TAB descends into it.
    assert str(tmp_path / "alpha") + os.sep in hits
    assert str(tmp_path / "album.txt") in hits
    assert all("beta" not in h for h in hits)


def test_names_in_lists_only_subdirs(tmp_path):
    (tmp_path / "v1").mkdir()
    (tmp_path / "v2").mkdir()
    (tmp_path / "note.txt").write_text("x")
    comp = completion.names_in(str(tmp_path))
    assert _collect(comp, "") == ["v1", "v2"]
    assert _collect(comp, "v1") == ["v1"]


def test_names_in_missing_dir_is_empty():
    comp = completion.names_in("/no/such/dir")
    assert _collect(comp, "") == []


def test_completion_context_restores_previous(monkeypatch):
    # Entering/exiting must not raise and must restore the prior completer.
    import readline

    sentinel = object()

    def prior(text, state):
        return sentinel

    readline.set_completer(prior)
    with completion.words(["a", "b"]):
        assert readline.get_completer() is not prior
    assert readline.get_completer() is prior


def test_ask_installs_completion_and_returns(monkeypatch):
    seen = {}

    def fake_prompt_ask(prompt, **kwargs):
        import readline

        seen["completer_active"] = readline.get_completer() is not None
        seen["prompt"] = prompt
        return "typed-value"

    monkeypatch.setattr(completion.Prompt, "ask", staticmethod(fake_prompt_ask))
    result = completion.ask("Pick", completion.words(["x", "y"]), default="x")
    assert result == "typed-value"
    assert seen["prompt"] == "Pick"
    assert seen["completer_active"] is True
