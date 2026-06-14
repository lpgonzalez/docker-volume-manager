"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Integration: backup/restore against a HOST path via --input-host/--output-host.

A "host path" is mounted into a helper container by dockerd, so the test only
needs the Docker socket — no extra bind mounts on the test container, which is
exactly the socket-only workflow the feature targets. A real, daemon-resolvable
host path is obtained from a throwaway volume's Mountpoint.
"""

from __future__ import annotations

import pytest

import cli

pytestmark = pytest.mark.slow


def _assert_file(docker_client, volume, rel_path, expected):
    content = docker_client.run_throwaway(
        ["cat", f"/target/{rel_path}"],
        volumes={volume: {"bind": "/target", "mode": "ro"}},
    )
    assert expected in content, (
        f"expected {expected!r} in {rel_path} of {volume!r}, got {content!r}"
    )


def test_backup_to_host_path_then_restore_from_it(
    docker_client, volume_factory, populate_volume, cli_runner
):
    source = volume_factory()
    # A real host directory the daemon can bind-mount: another volume's mountpoint.
    host_store = volume_factory()
    restored = volume_factory()
    populate_volume(source, "echo host-roundtrip > /target/data.txt")

    host_dir = docker_client.inspect_volume(host_store).mountpoint
    assert host_dir, "volume has no Mountpoint"

    # Backup: volume source → HOST-path destination (helper bind-mounts host_dir).
    r1 = cli_runner.invoke(
        cli.app,
        [
            "backup",
            "--input-volume",
            source,
            "--output-host",
            host_dir,
            "-n",
            "hp",
            "-c",
            "GZ",
        ],
    )
    assert r1.exit_code == 0, r1.output

    # Restore: HOST-path source → volume destination.
    r2 = cli_runner.invoke(
        cli.app,
        ["restore", "--input-host", host_dir, "--output-volume", restored, "-n", "hp"],
    )
    assert r2.exit_code == 0, r2.output

    _assert_file(docker_client, restored, "data.txt", "host-roundtrip")


def test_verify_a_backup_on_a_host_path(
    docker_client, volume_factory, populate_volume, cli_runner
):
    source = volume_factory()
    host_store = volume_factory()
    populate_volume(source, "echo verify-host > /target/d.txt")
    host_dir = docker_client.inspect_volume(host_store).mountpoint

    assert (
        cli_runner.invoke(
            cli.app,
            ["backup", "--input-volume", source, "--output-host", host_dir, "-n", "hv"],
        ).exit_code
        == 0
    )
    # verify reads the backup from the host path via --output-host.
    result = cli_runner.invoke(
        cli.app, ["verify", "--output-host", host_dir, "-n", "hv"]
    )
    assert result.exit_code == 0, result.output
