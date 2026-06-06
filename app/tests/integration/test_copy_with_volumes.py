"""Integration tests: `dvm copy` with --input-volume / --output-volume.

Each test exercises the full helper-pivot code path: the outer CliRunner
invocation detects volume flags, spawns a helper container with both volumes
mounted, streams logs back, and waits for exit.
"""

from __future__ import annotations

import pytest

import cli

pytestmark = pytest.mark.slow


def test_copy_volume_to_volume(
    docker_client, volume_factory, populate_volume, cli_runner
):
    """volume → volume via `dvm copy --input-volume --output-volume`."""
    src = volume_factory()
    dst = volume_factory()

    populate_volume(
        src,
        "echo alpha > /target/file1.txt && "
        "mkdir -p /target/sub && "
        "echo bravo > /target/sub/file2.txt && "
        "ln -s file1.txt /target/link",
    )

    result = cli_runner.invoke(
        cli.app,
        ["copy", "--input-volume", src, "--output-volume", dst],
    )
    assert result.exit_code == 0, (result.stderr or "") + (result.stdout or "")

    entries = docker_client.volume_ls(dst)
    assert entries is not None
    assert "file1.txt" in entries
    assert "sub/" in entries
    assert "link" in entries

    file1 = docker_client.run_throwaway(
        ["cat", "/target/file1.txt"],
        volumes={dst: {"bind": "/target", "mode": "ro"}},
    )
    assert "alpha" in file1

    file2 = docker_client.run_throwaway(
        ["cat", "/target/sub/file2.txt"],
        volumes={dst: {"bind": "/target", "mode": "ro"}},
    )
    assert "bravo" in file2


def test_copy_refuses_without_existing_target_volume(
    docker_client, volume_factory, populate_volume, cli_runner
):
    """`--output-volume` referencing a missing volume exits with validation error."""
    src = volume_factory()
    populate_volume(src, "echo data > /target/file.txt")

    result = cli_runner.invoke(
        cli.app,
        ["copy", "--input-volume", src, "--output-volume", "dvm-definitely-missing"],
    )
    assert result.exit_code == cli.EXIT_VALIDATION


def test_copy_refuses_missing_input_volume(docker_client, volume_factory, cli_runner):
    dst = volume_factory()
    result = cli_runner.invoke(
        cli.app,
        ["copy", "--input-volume", "dvm-definitely-missing", "--output-volume", dst],
    )
    assert result.exit_code == cli.EXIT_VALIDATION
