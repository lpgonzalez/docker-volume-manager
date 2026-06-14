"""Integration tests: `dvm verify` against a backup stored in a Docker volume."""

from __future__ import annotations

import pytest

import cli
from wizard_store import VolumeStore

pytestmark = pytest.mark.slow


def _make_backup(cli_runner, source: str, store: str, name: str, **extra_args):
    args = [
        "backup",
        "--input-volume",
        source,
        "--output-volume",
        store,
        "-n",
        name,
        "-c",
        "GZ",
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

    assert (
        _make_backup(
            cli_runner, source, store, "encrypted-verify", **{"-k": "pw"}
        ).exit_code
        == 0
    )

    result = cli_runner.invoke(
        cli.app,
        [
            "verify",
            "--output-volume",
            store,
            "-n",
            "encrypted-verify",
            "-k",
            "pw",
        ],
    )
    assert result.exit_code == 0


def test_verify_no_repair_then_autorepair_in_volume(
    docker_client, volume_factory, populate_volume, cli_runner
):
    """A damaged-but-recoverable backup in a volume: `--no-repair` audits it
    read-only (unhealthy, untouched); the default verify then auto-repairs it in
    place through the rw-mounted pivot."""
    source = volume_factory()
    store = volume_factory()
    populate_volume(source, "head -c 200000 /dev/urandom > /target/data.bin")

    assert _make_backup(cli_runner, source, store, "dmg", **{"-p": 30}).exit_code == 0

    # Corrupt 32 bytes in the middle of the archive, in place (BusyBox dd).
    archive = docker_client.run_throwaway(
        ["sh", "-c", "find /target -name 'dmg.tar.gz' | head -1"],
        volumes={store: {"bind": "/target", "mode": "ro"}},
    ).strip()
    assert archive, "archive not found in store volume"
    docker_client.run_throwaway(
        [
            "sh",
            "-c",
            f"dd if=/dev/zero of={archive} bs=1 count=32 seek=2048 conv=notrunc",
        ],
        volumes={store: {"bind": "/target", "mode": "rw"}},
    )

    # --no-repair: read-only audit → unhealthy, archive left damaged.
    ro = cli_runner.invoke(
        cli.app, ["verify", "--output-volume", store, "-n", "dmg", "--no-repair"]
    )
    assert ro.exit_code != 0
    combined = (ro.stdout or "") + (ro.stderr or "")
    assert "RECOVERABLE" in combined or "repairable" in combined.lower()

    # Default verify: auto-repairs the archive in place → healthy.
    fixed = cli_runner.invoke(
        cli.app, ["verify", "--output-volume", store, "-n", "dmg"]
    )
    assert fixed.exit_code == 0

    # A subsequent verify still passes (already repaired).
    again = cli_runner.invoke(
        cli.app, ["verify", "--output-volume", store, "-n", "dmg"]
    )
    assert again.exit_code == 0


def test_volume_store_lists_backups_inside_a_volume(
    docker_client, volume_factory, populate_volume, cli_runner
):
    """The wizard's VolumeStore enumerates names/timestamps/archives inside a
    Docker volume via throwaway containers — the listing the redesigned wizard
    relies on for volume-mode restore/verify."""
    source = volume_factory()
    store = volume_factory()
    populate_volume(source, "echo data > /target/d.txt")
    assert _make_backup(cli_runner, source, store, "wiz", **{"-p": 30}).exit_code == 0

    vs = VolumeStore(store, docker_client)
    assert "wiz" in vs.list_names()
    timestamps = vs.list_timestamps("wiz")
    assert timestamps, "expected at least one timestamped backup"
    assert vs.has_archive("wiz", timestamps[0]) is True
    assert vs.has_archive("wiz", "20000101_0000") is False
