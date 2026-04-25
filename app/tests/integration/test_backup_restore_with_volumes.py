"""Integration tests: full backup → restore round-trip using Docker volumes.

Exercises the helper-pivot flow for both `dvm backup` and `dvm restore` with
--input-volume / --output-volume. The source, backup store and restore target
are all Docker volumes — avoiding the bind-mount-from-containerised-test
limitation.
"""

from __future__ import annotations

import os

import pytest

import cli


pytestmark = pytest.mark.slow

_POPULATE_SCRIPT = (
    "echo alpha > /target/a.txt && "
    "mkdir -p /target/dir && "
    "echo bravo > /target/dir/b.txt && "
    "mkdir -p /target/dir/deeper && "
    "echo charlie > /target/dir/deeper/c.txt && "
    "ln -s a.txt /target/link"
)


def _assert_file(docker_client, volume, rel_path, expected):
    content = docker_client.run_throwaway(
        ["cat", f"/target/{rel_path}"],
        volumes={volume: {"bind": "/target", "mode": "ro"}},
    )
    assert expected in content, (
        f"expected {expected!r} in {rel_path} of volume {volume!r}, got {content!r}"
    )


@pytest.mark.parametrize("compression", ["NONE", "GZ", "ZSTD"])
def test_backup_restore_volume_roundtrip(
    docker_client, volume_factory, populate_volume, cli_runner, compression
):
    """backup volume → store volume, restore store volume → target volume."""
    source = volume_factory()
    store = volume_factory()
    restored = volume_factory()

    populate_volume(source, _POPULATE_SCRIPT)

    backup_result = cli_runner.invoke(
        cli.app,
        [
            "backup",
            "--input-volume", source,
            "--output-volume", store,
            "-n", "my-backup",
            "-c", compression,
        ],
    )
    assert backup_result.exit_code == 0, (
        (backup_result.stderr or "") + (backup_result.stdout or "")
    )

    restore_result = cli_runner.invoke(
        cli.app,
        [
            "restore",
            "--input-volume", store,
            "--output-volume", restored,
            "-n", "my-backup",
        ],
    )
    assert restore_result.exit_code == 0, (
        (restore_result.stderr or "") + (restore_result.stdout or "")
    )

    _assert_file(docker_client, restored, "a.txt", "alpha")
    _assert_file(docker_client, restored, "dir/b.txt", "bravo")
    _assert_file(docker_client, restored, "dir/deeper/c.txt", "charlie")

    # Symlink preserved
    out = docker_client.run_throwaway(
        ["readlink", "/target/link"],
        volumes={restored: {"bind": "/target", "mode": "ro"}},
    )
    assert "a.txt" in out


def test_backup_restore_with_encryption(
    docker_client, volume_factory, populate_volume, cli_runner
):
    source = volume_factory()
    store = volume_factory()
    restored = volume_factory()
    populate_volume(source, _POPULATE_SCRIPT)

    passphrase = "integration-secret"

    backup_result = cli_runner.invoke(
        cli.app,
        [
            "backup",
            "--input-volume", source,
            "--output-volume", store,
            "-n", "encrypted-bak",
            "-c", "ZSTD",
            "-k", passphrase,
        ],
    )
    assert backup_result.exit_code == 0

    restore_result = cli_runner.invoke(
        cli.app,
        [
            "restore",
            "--input-volume", store,
            "--output-volume", restored,
            "-n", "encrypted-bak",
            "-k", passphrase,
        ],
    )
    assert restore_result.exit_code == 0

    _assert_file(docker_client, restored, "a.txt", "alpha")
    _assert_file(docker_client, restored, "dir/deeper/c.txt", "charlie")


def test_backup_with_encryption_and_parity(
    docker_client, volume_factory, populate_volume, cli_runner
):
    """Backup + encryption + parity produces the expected par2 artifacts."""
    source = volume_factory()
    store = volume_factory()
    populate_volume(source, _POPULATE_SCRIPT)

    passphrase = "pw-parity"
    backup_result = cli_runner.invoke(
        cli.app,
        [
            "backup",
            "--input-volume", source,
            "--output-volume", store,
            "-n", "with-parity",
            "-c", "ZSTD",
            "-p", "30",
            "-k", passphrase,
        ],
    )
    assert backup_result.exit_code == 0

    # Exactly one .par2 index file + one volume par2 (-n1 in backup_files).
    # Avoid `find -printf` — that's GNU-only, BusyBox find on Alpine doesn't have it.
    listing = docker_client.run_throwaway(
        ["sh", "-c", "find /target -name '*.par2' | sort"],
        volumes={store: {"bind": "/target", "mode": "ro"}},
    )
    par2_files = [
        os.path.basename(ln.strip())
        for ln in listing.splitlines()
        if ln.strip()
    ]
    assert len(par2_files) == 2, f"expected 2 par2 files, got: {par2_files!r}"
    assert any(f.endswith(".gpg.par2") for f in par2_files)
    assert any(".vol" in f for f in par2_files)
