"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Unit tests for the Docker HEALTHCHECK probe. Regression guard for the bug where
the probe used the ``site``-injected ``exit`` builtin instead of ``sys.exit``.
"""

from __future__ import annotations

import pytest

import health_check


def _run_with_status(monkeypatch, tmp_path, contents=None):
    status_file = tmp_path / "app_status.txt"
    if contents is not None:
        status_file.write_text(contents)
    monkeypatch.setattr(health_check, "STATUS_FILE", str(status_file))
    with pytest.raises(SystemExit) as excinfo:
        health_check.check_health()
    return excinfo.value.code


def test_healthy_exits_zero(monkeypatch, tmp_path):
    assert _run_with_status(monkeypatch, tmp_path, "healthy") == 0


@pytest.mark.parametrize("status", ["unhealthy", "starting", "", "  weird  "])
def test_non_healthy_exits_one(monkeypatch, tmp_path, status):
    assert _run_with_status(monkeypatch, tmp_path, status) == 1


def test_missing_status_file_exits_one(monkeypatch, tmp_path):
    # No file written -> OSError -> exit 1 (not an uncaught traceback).
    assert _run_with_status(monkeypatch, tmp_path, contents=None) == 1
