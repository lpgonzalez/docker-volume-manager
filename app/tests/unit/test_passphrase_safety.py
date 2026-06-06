"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Unit tests proving GPG passphrases never reach the process argv (which is
world-readable via ``ps`` / ``/proc/<pid>/cmdline``). They must travel through a
file descriptor / stdin instead.
"""

from __future__ import annotations

import os

from operations import backup_files
from operations.backup_files import BackupManager, _passphrase_read_fd


def test_passphrase_read_fd_carries_secret_then_eof():
    fd = _passphrase_read_fd("s3cr3t")
    try:
        assert os.read(fd, 64) == b"s3cr3t\n"
        # Write end is closed, so the next read sees EOF.
        assert os.read(fd, 64) == b""
    finally:
        os.close(fd)


class _FakeProc:
    def __init__(self):
        self.returncode = 0
        self.stderr = b""


def test_sign_archive_passes_passphrase_via_stdin_not_argv(monkeypatch, tmp_path):
    archive = tmp_path / "vol.tar.zst"
    archive.write_bytes(b"not a real archive")

    captured = {}

    def fake_run(cmd, input=None, capture_output=False, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = input
        return _FakeProc()

    monkeypatch.setattr(backup_files.subprocess, "run", fake_run)

    mgr = BackupManager(
        vol_name="vol",
        input_path=str(tmp_path),
        output_path=str(tmp_path),
        sign_key="DEADBEEF",
        sign_key_passphrase="top-secret-pass",
    )
    mgr.sign_archive(str(archive))

    # The secret must NOT appear anywhere in the command line.
    assert "top-secret-pass" not in captured["cmd"]
    # It must be delivered on stdin with loopback pinentry + --passphrase-fd 0.
    assert captured["input"] == b"top-secret-pass\n"
    assert "--passphrase-fd" in captured["cmd"]
    assert "0" in captured["cmd"]
    assert "--passphrase" not in captured["cmd"]


def test_sign_archive_without_passphrase_sends_no_stdin(monkeypatch, tmp_path):
    archive = tmp_path / "vol.tar.zst"
    archive.write_bytes(b"x")
    captured = {}

    def fake_run(cmd, input=None, capture_output=False, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = input
        return _FakeProc()

    monkeypatch.setattr(backup_files.subprocess, "run", fake_run)

    mgr = BackupManager(
        vol_name="vol",
        input_path=str(tmp_path),
        output_path=str(tmp_path),
        sign_key="DEADBEEF",
    )
    mgr.sign_archive(str(archive))

    assert captured["input"] is None
    assert "--passphrase-fd" not in captured["cmd"]
