"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Functional tests for the VERIFY operation *through the dispatcher*.

Verify must locate the archive in the real ``<name>/<timestamp>/<name>.<ext>``
layout (the same layout backup writes and restore reads) and its return value
must reflect the backup's health (True = healthy, False = missing/unusable) so
the container exits with the right code. Regression guard for the bug where
verify treated ``<output>/<name>`` (a directory) as the archive file and always
returned True regardless of the outcome.
"""

from __future__ import annotations

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


def _verify(output_dir, name, *, password=""):
    cfg = Config(
        OPERATION="VERIFY",
        BACKUP_FILE_NAME=name,
        OUTPUT_PATH=str(output_dir),
        ENCRYPTION_KEY=password,
        LOG_OUTPUT=["console"],
        LOGS_PATH=None,
    )
    return Docker_Volume_Manager(cfg).run()


def test_verify_locates_valid_backup_in_timestamped_layout(
    tmp_path, clean_env, reset_dvm_logger
):
    # A healthy backup under <out>/demo/<ts>/demo.tar.zst must verify True.
    output_dir = _make_backup(tmp_path, "demo", compression="zstd")
    assert _verify(output_dir, "demo") is True


def test_verify_encrypted_backup_is_healthy(tmp_path, clean_env, reset_dvm_logger):
    output_dir = _make_backup(tmp_path, "enc", compression="gz", password="pw")
    assert _verify(output_dir, "enc", password="pw") is True


def test_verify_encrypted_wrong_password_fails(tmp_path, clean_env, reset_dvm_logger):
    output_dir = _make_backup(tmp_path, "enc", compression="gz", password="right")
    assert _verify(output_dir, "enc", password="wrong") is False


def test_verify_missing_backup_returns_false(tmp_path, clean_env, reset_dvm_logger):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    assert _verify(output_dir, "does-not-exist") is False
