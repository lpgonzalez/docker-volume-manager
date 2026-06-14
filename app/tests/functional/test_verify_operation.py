"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Functional tests for the VERIFY operation *through the dispatcher*.

Verify must locate the archive in the real ``<name>/<timestamp>/<name>.<ext>``
layout (the same layout backup writes and restore reads). A healthy (or
auto-repaired) backup makes ``run()`` return True; every unhealthy outcome
raises :class:`OperationError` carrying the specific verify exit code (30-35).
Regression guard for the bug where verify treated ``<output>/<name>`` (a
directory) as the archive file and always returned True regardless of outcome.
"""

from __future__ import annotations

import glob
import os

from cli_shared import (
    EXIT_VERIFY_BACKUP_MISSING,
    EXIT_VERIFY_CORRUPT_REPAIRABLE,
    EXIT_VERIFY_DECRYPT_FAILED,
    EXIT_VERIFY_UNREPAIRABLE,
    OperationError,
)
from config import Config
from operations.backup_files import BackupManager
from operations.docker_volume_manager import Docker_Volume_Manager


def _make_backup(tmp_path, name, *, compression="zstd", password=None):
    input_dir = tmp_path / "in"
    (input_dir / "sub").mkdir(parents=True)
    (input_dir / "a.txt").write_text("hello world")
    (input_dir / "sub" / "b.txt").write_text("deep content")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    mgr = BackupManager(
        vol_name=name,
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression=compression,
        password=password,
    )
    if password:
        mgr.compress_and_encrypt_pipeline()
    else:
        mgr.compress()
    return output_dir


def _make_backup_with_parity(tmp_path, name, *, parity=30):
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    (input_dir / "payload.bin").write_bytes(b"Z" * 64 * 1024)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    mgr = BackupManager(
        vol_name=name,
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        create_parity=True,
        parity_percentage=parity,
    )
    archive = mgr.compress()
    mgr.create_parity_file(archive, parity)
    return output_dir, archive


def _corrupt(archive, offset=2048, n=4):
    with open(archive, "r+b") as f:
        f.seek(offset)
        f.write(b"\xff" * n)


def _verify(output_dir, name, *, password="", repair=None, timestamp=None):
    """Run verify through the dispatcher and return its exit code.

    0 = healthy/auto-repaired; otherwise the OperationError's specific code.
    repair=None omits REPAIR so the Config default (auto-repair) is exercised.
    """
    kwargs = {
        "OPERATION": "VERIFY",
        "BACKUP_FILE_NAME": name,
        "OUTPUT_PATH": str(output_dir),
        "ENCRYPTION_KEY": password,
        "TIMESTAMP": timestamp,
        "LOG_OUTPUT": ["console"],
        "LOGS_PATH": None,
    }
    if repair is not None:
        kwargs["REPAIR"] = repair
    try:
        return 0 if Docker_Volume_Manager(Config(**kwargs)).run() else 4
    except OperationError as exc:
        return exc.code


def test_verify_locates_valid_backup_in_timestamped_layout(
    tmp_path, clean_env, reset_dvm_logger
):
    # A healthy backup under <out>/demo/<ts>/demo.tar.zst must verify (exit 0).
    output_dir = _make_backup(tmp_path, "demo", compression="zstd")
    assert _verify(output_dir, "demo") == 0


def test_verify_encrypted_backup_is_healthy(tmp_path, clean_env, reset_dvm_logger):
    output_dir = _make_backup(tmp_path, "enc", compression="gz", password="pw")
    assert _verify(output_dir, "enc", password="pw") == 0


def test_verify_encrypted_wrong_password_fails(tmp_path, clean_env, reset_dvm_logger):
    output_dir = _make_backup(tmp_path, "enc", compression="gz", password="right")
    assert _verify(output_dir, "enc", password="wrong") == EXIT_VERIFY_DECRYPT_FAILED


def test_verify_missing_backup_returns_false(tmp_path, clean_env, reset_dvm_logger):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    assert _verify(output_dir, "does-not-exist") == EXIT_VERIFY_BACKUP_MISSING


def test_verify_default_autorepairs_damaged_recoverable(
    tmp_path, clean_env, reset_dvm_logger
):
    # Default verify (no REPAIR set) auto-repairs a recoverable archive in place
    # and reports healthy (exit 0); a subsequent verify then passes cleanly.
    output_dir, archive = _make_backup_with_parity(tmp_path, "dmg")
    _corrupt(archive)
    with open(archive, "rb") as f:
        before = f.read()

    assert _verify(output_dir, "dmg") == 0  # auto-repaired → healthy

    with open(archive, "rb") as f:
        assert f.read() != before, "auto-repair should have rewritten the archive"
    assert _verify(output_dir, "dmg") == 0


def test_verify_no_repair_damaged_is_unhealthy_and_untouched(
    tmp_path, clean_env, reset_dvm_logger
):
    # --no-repair (REPAIR=False) audits read-only: specific exit code, file intact.
    output_dir, archive = _make_backup_with_parity(tmp_path, "dmg")
    _corrupt(archive)
    with open(archive, "rb") as f:
        before = f.read()

    assert _verify(output_dir, "dmg", repair=False) == EXIT_VERIFY_CORRUPT_REPAIRABLE

    with open(archive, "rb") as f:
        assert f.read() == before, "--no-repair must not modify the archive"


def test_verify_unrepairable_is_unhealthy_even_with_autorepair(
    tmp_path, clean_env, reset_dvm_logger
):
    # Damage beyond redundancy: even default auto-repair can't fix it → data loss.
    output_dir, archive = _make_backup_with_parity(tmp_path, "dead", parity=5)
    size = os.path.getsize(archive)
    with open(archive, "r+b") as f:
        f.seek(size // 4)
        f.write(os.urandom(size // 2))

    assert _verify(output_dir, "dead") == EXIT_VERIFY_UNREPAIRABLE
    # Sanity: the par2 index is still where we expect it.
    assert glob.glob(str(archive) + "*.par2")


def test_verify_explicit_timestamp(tmp_path, clean_env, reset_dvm_logger):
    # A specific timestamp verifies; a non-existent one reports BACKUP_MISSING.
    output_dir, archive = _make_backup_with_parity(tmp_path, "ts")
    ts = os.path.basename(os.path.dirname(archive))
    assert _verify(output_dir, "ts", timestamp=ts) == 0
    assert (
        _verify(output_dir, "ts", timestamp="20000101_0000")
        == EXIT_VERIFY_BACKUP_MISSING
    )
