"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Regression test for the interactive backup wizard. The wizard must dispatch
through the shared pivot + runner helpers (not the typer command objects, which
would leak ``OptionInfo`` sentinels for unset options) and pass every value it
collects through cleanly.
"""

from __future__ import annotations

import wizard


def test_wizard_backup_passes_collected_values_to_runner(monkeypatch):
    captured = {}

    # Stub the interactive prompts so the wizard runs non-interactively.
    monkeypatch.setattr(
        wizard, "_wizard_path_or_volume", lambda *a, **k: ("/app/input_dir", None)
    )
    monkeypatch.setattr(wizard, "_wizard_encryption_choice", lambda: (None, []))
    monkeypatch.setattr(wizard, "_wizard_common", lambda: ("INFO", ["console"]))
    monkeypatch.setattr(wizard.Prompt, "ask", staticmethod(lambda *a, **k: "ZSTD"))
    monkeypatch.setattr(wizard.IntPrompt, "ask", staticmethod(lambda *a, **k: 0))

    # Intercept the runner (it owns the pivot + dispatch); we only assert that the
    # wizard plumbs the collected values through correctly.
    monkeypatch.setattr(wizard, "_run_backup", lambda **k: captured.update(k))

    wizard._wizard_run_backup()

    assert captured, "_run_backup was never reached"
    # The wizard does not collect signing options — they must arrive as None.
    assert captured["sign_key"] is None
    assert captured["sign_key_passphrase"] is None
    # No encryption chosen → recipients is an empty list (never an OptionInfo).
    assert captured["recipients"] == []
    assert captured["log_output"] == ["console"]
