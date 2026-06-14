"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Regression tests for the interactive wizard flows. Each must dispatch through
the shared pivot + runner helpers (not the typer command objects, which would
leak ``OptionInfo`` sentinels) and plumb every collected value through cleanly —
including the location-first ordering and the backup name/timestamp picked from
the store.
"""

from __future__ import annotations

import wizard


class _FakeStore:
    label = "/app/store"

    def is_writable(self):
        return True


def _patch_common(monkeypatch):
    monkeypatch.setattr(wizard, "_wizard_encryption_choice", lambda: (None, []))
    monkeypatch.setattr(wizard, "_wizard_common", lambda: ("INFO", ["console"]))


def test_wizard_backup_passes_collected_values_to_runner(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        wizard,
        "_wizard_location",
        lambda *a, **k: ("/app/input_dir", None, _FakeStore()),
    )
    monkeypatch.setattr(wizard, "_wizard_new_backup_name", lambda store: "myvol")
    _patch_common(monkeypatch)
    monkeypatch.setattr(wizard.completion, "ask", staticmethod(lambda *a, **k: "zstd"))
    monkeypatch.setattr(wizard.IntPrompt, "ask", staticmethod(lambda *a, **k: 0))
    monkeypatch.setattr(wizard, "_run_backup", lambda **k: captured.update(k))

    wizard._wizard_run_backup()

    assert captured, "_run_backup was never reached"
    assert captured["name"] == "myvol"
    # The wizard does not collect signing options — they must arrive as None.
    assert captured["sign_key"] is None
    assert captured["sign_key_passphrase"] is None
    # No encryption chosen → recipients is an empty list (never an OptionInfo).
    assert captured["recipients"] == []
    assert captured["log_output"] == ["console"]


def test_wizard_verify_plumbs_name_and_timestamp(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        wizard,
        "_wizard_location",
        lambda *a, **k: ("/app/output_dir", None, _FakeStore()),
    )
    monkeypatch.setattr(
        wizard, "_wizard_pick_existing_backup", lambda store: ("yadee", "20260613_2210")
    )
    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda *a, **k: False))
    _patch_common(monkeypatch)
    monkeypatch.setattr(wizard, "_run_verify", lambda **k: captured.update(k))

    wizard._wizard_run_verify()

    assert captured["name"] == "yadee"
    assert captured["timestamp"] == "20260613_2210"
    assert captured["repair"] is False  # Confirm stubbed to "no"


def test_wizard_verify_aborts_when_no_backup_selected(monkeypatch):
    reached = {"run": False}
    monkeypatch.setattr(
        wizard,
        "_wizard_location",
        lambda *a, **k: ("/app/output_dir", None, _FakeStore()),
    )
    monkeypatch.setattr(
        wizard, "_wizard_pick_existing_backup", lambda store: (None, None)
    )
    monkeypatch.setattr(wizard, "_run_verify", lambda **k: reached.update(run=True))

    wizard._wizard_run_verify()
    assert reached["run"] is False  # no backup → never dispatches


def test_wizard_restore_plumbs_name_and_timestamp(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        wizard,
        "_wizard_location",
        lambda *a, **k: ("/app/input_dir", None, _FakeStore()),
    )
    monkeypatch.setattr(
        wizard, "_wizard_pick_existing_backup", lambda store: ("yadee", "20260613_2210")
    )
    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(wizard.Prompt, "ask", staticmethod(lambda *a, **k: ""))
    _patch_common(monkeypatch)
    monkeypatch.setattr(wizard, "_run_restore", lambda **k: captured.update(k))

    wizard._wizard_run_restore()

    assert captured["name"] == "yadee"
    assert captured["timestamp"] == "20260613_2210"
