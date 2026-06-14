"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Unit tests for the helper-container pivot — side resolution and the mount/env
construction for remote (volume or host-path) sources/destinations. All Docker
interactions are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import typer

import pivot
from cli_shared import EXIT_VALIDATION


def _fake_client(exit_code: int = 0):
    client = MagicMock()
    client.ping.return_value = True
    client.volume_exists.return_value = True
    client.self_image.return_value = "img"
    client.run_helper_streaming.return_value = exit_code
    return client


# --- _resolve_side -----------------------------------------------------------


def test_resolve_side_local():
    s = pivot._resolve_side("input", None, None)
    assert (s.kind, s.mount_key, s.is_remote) == ("local", None, False)


def test_resolve_side_volume():
    s = pivot._resolve_side("input", "vol", None)
    assert (s.kind, s.mount_key, s.is_remote) == ("volume", "vol", True)


def test_resolve_side_host_absolute():
    s = pivot._resolve_side("output", None, "/host/p")
    assert (s.kind, s.mount_key, s.is_remote) == ("host", "/host/p", True)


def test_resolve_side_volume_and_host_mutually_exclusive():
    with pytest.raises(typer.Exit) as exc:
        pivot._resolve_side("input", "vol", "/host/p")
    assert exc.value.exit_code == EXIT_VALIDATION


def test_resolve_side_relative_host_rejected():
    with pytest.raises(typer.Exit) as exc:
        pivot._resolve_side("input", None, "relative/dir")
    assert exc.value.exit_code == EXIT_VALIDATION


# --- _pivot_if_remote --------------------------------------------------------


def test_pivot_pure_local_is_noop(monkeypatch):
    # No remote side → no pivot, and DockerClient is never constructed.
    monkeypatch.delenv("DVM_HELPER_MODE", raising=False)
    sentinel = {"built": False}

    def _boom():
        sentinel["built"] = True
        raise AssertionError("DockerClient should not be built for a local pivot")

    monkeypatch.setattr(pivot, "DockerClient", _boom)
    assert pivot._pivot_if_remote("backup", {"INPUT_PATH": "/dvm/source"}) is None
    assert sentinel["built"] is False


def test_pivot_refuses_inside_helper(monkeypatch):
    monkeypatch.setenv("DVM_HELPER_MODE", "1")
    with pytest.raises(typer.Exit) as exc:
        pivot._pivot_if_remote("backup", {}, input_host="/host/in")
    assert exc.value.exit_code == EXIT_VALIDATION


def test_pivot_host_source_volume_dest_builds_mounts_and_scrubs(monkeypatch):
    monkeypatch.delenv("DVM_HELPER_MODE", raising=False)
    client = _fake_client(exit_code=7)
    monkeypatch.setattr(pivot, "DockerClient", lambda: client)

    env = {
        "INPUT_PATH": "/dvm/source",
        "OUTPUT_PATH": "/dvm/dest",
        "INPUT_HOST": "/host/in",
        "OUTPUT_VOLUME": "vol",
        "FOO": "bar",
    }
    with pytest.raises(typer.Exit) as exc:
        pivot._pivot_if_remote(
            "backup",
            env,
            input_host="/host/in",
            output_volume="vol",
            input_mode="ro",
            output_mode="rw",
        )
    assert exc.value.exit_code == 7  # helper's exit code propagates

    _args, kwargs = client.run_helper_streaming.call_args
    mounts = kwargs["volume_mounts"]
    # Host path is a bind-mount key; volume name is a named-volume key.
    assert mounts["/host/in"] == {"bind": "/dvm/source", "mode": "ro"}
    assert mounts["vol"] == {"bind": "/dvm/dest", "mode": "rw"}

    helper_env = kwargs["env"]
    assert helper_env["INPUT_PATH"] == "/dvm/source"
    assert helper_env["OUTPUT_PATH"] == "/dvm/dest"
    assert helper_env["OPERATION"] == "BACKUP"
    assert helper_env["FOO"] == "bar"
    # Remote flags scrubbed so the helper never re-pivots.
    for key in ("INPUT_HOST", "OUTPUT_HOST", "INPUT_VOLUME", "OUTPUT_VOLUME"):
        assert key not in helper_env


def test_pivot_missing_volume_aborts(monkeypatch):
    monkeypatch.delenv("DVM_HELPER_MODE", raising=False)
    client = _fake_client()
    client.volume_exists.return_value = False
    monkeypatch.setattr(pivot, "DockerClient", lambda: client)
    with pytest.raises(typer.Exit) as exc:
        pivot._pivot_if_remote("backup", {}, output_volume="ghost")
    assert exc.value.exit_code == EXIT_VALIDATION
    client.run_helper_streaming.assert_not_called()
