"""Integration tests: `dvm verify` against a backup stored in a Docker volume."""

from __future__ import annotations

import pytest

import cli


pytestmark = pytest.mark.slow


def _make_backup(cli_runner, source: str, store: str, name: str, **extra_args):
    args = [
        "backup",
        "--input-volume", source,
        "--output-volume", store,
        "-n", name,
        "-c", "GZ",
    ]
    for flag, value in extra_args.items():
        args.extend([flag, str(value)])
    return cli_runner.invoke(cli.app, args)


def test_verify_healthy_backup_in_volume(
    docker_client, volume_factory, populate_volume, cli_runner
):
    source = volume_factory()
    store = volume_factory()
    populate_volume(source, "echo payload > /target/data.txt")

    assert _make_backup(cli_runner, source, store, "healthy").exit_code == 0

    result = cli_runner.invoke(
        cli.app,
        ["verify", "--output-volume", store, "-n", "healthy"],
    )
    assert result.exit_code == 0
    combined = (result.stdout or "") + (result.stderr or "")
    # Verification summary should report a decompressable, non-encrypted archive.
    assert "can_decompress" in combined.lower() or "Can decompress" in combined


def test_verify_encrypted_backup_in_volume(
    docker_client, volume_factory, populate_volume, cli_runner
):
    source = volume_factory()
    store = volume_factory()
    populate_volume(source, "echo secret > /target/data.txt")

    assert _make_backup(
        cli_runner, source, store, "encrypted-verify", **{"-k": "pw"}
    ).exit_code == 0

    result = cli_runner.invoke(
        cli.app,
        [
            "verify",
            "--output-volume", store,
            "-n", "encrypted-verify",
            "-k", "pw",
        ],
    )
    assert result.exit_code == 0
