"""Integration tests: DockerClient lifecycle and introspection against a live daemon."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Basic availability
# ---------------------------------------------------------------------------


def test_ping_returns_true(docker_client):
    assert docker_client.ping() is True


# ---------------------------------------------------------------------------
# Volume lifecycle: inspect, list, exists, remove
# ---------------------------------------------------------------------------


def test_inspect_returns_expected_metadata(docker_client, throwaway_volume):
    info = docker_client.inspect_volume(throwaway_volume)
    assert info.name == throwaway_volume
    assert info.driver == "local"
    assert info.scope == "local"
    assert info.mountpoint  # non-empty


def test_list_contains_throwaway(docker_client, throwaway_volume):
    names = [v.name for v in docker_client.list_volumes()]
    assert throwaway_volume in names


def test_list_is_alphabetically_sorted(docker_client):
    names = [v.name.lower() for v in docker_client.list_volumes()]
    assert names == sorted(names)


def test_volume_exists(docker_client, throwaway_volume):
    assert docker_client.volume_exists(throwaway_volume) is True
    assert docker_client.volume_exists(f"missing-{throwaway_volume}") is False


# ---------------------------------------------------------------------------
# run_throwaway + introspection helpers
# ---------------------------------------------------------------------------


def test_run_throwaway_returns_stdout(docker_client, throwaway_volume):
    out = docker_client.run_throwaway(
        ["sh", "-c", "echo hello-from-helper"],
        volumes={throwaway_volume: {"bind": "/target", "mode": "ro"}},
    )
    assert "hello-from-helper" in out


def test_volume_size_empty_volume(docker_client, throwaway_volume):
    size = docker_client.volume_size(throwaway_volume)
    assert size is not None
    assert size >= 0


def test_volume_ls_empty_returns_empty_list(docker_client, throwaway_volume):
    entries = docker_client.volume_ls(throwaway_volume)
    assert entries == []


def test_volume_ls_with_content(docker_client, throwaway_volume):
    # Populate the volume via a rw helper
    docker_client.run_throwaway(
        [
            "sh",
            "-c",
            "touch /target/foo && mkdir -p /target/bar && echo hi > /target/bar/baz",
        ],
        volumes={throwaway_volume: {"bind": "/target", "mode": "rw"}},
    )
    entries = docker_client.volume_ls(throwaway_volume)
    assert entries is not None
    # `ls -1Ap` marks directories with a trailing /
    assert "foo" in entries
    assert "bar/" in entries


def test_volume_size_after_populating(docker_client, throwaway_volume):
    docker_client.run_throwaway(
        ["sh", "-c", "dd if=/dev/zero of=/target/blob bs=4096 count=10 2>/dev/null"],
        volumes={throwaway_volume: {"bind": "/target", "mode": "rw"}},
    )
    size = docker_client.volume_size(throwaway_volume)
    assert size is not None
    # At least the 40 KiB of data we wrote.
    assert size >= 4096 * 10
