"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Extended-attribute fidelity: a backup must round-trip xattrs (used by POSIX
ACLs, SELinux labels, capabilities) so restored volumes keep their security
metadata. Covers both the encrypted pipeline (system tar --xattrs) and the
unencrypted Python path (SCHILY.xattr PAX records).
"""

from __future__ import annotations

import os

import pytest

from operations.backup_files import BackupManager
from operations.restore_files import restore


def _xattrs_supported(path: str) -> bool:
    try:
        os.setxattr(path, "user.dvm_probe", b"1")
        os.removexattr(path, "user.dvm_probe")
        return True
    except OSError:
        return False


@pytest.mark.parametrize(
    "compression,password",
    [("gz", None), ("zstd", None), ("zstd", "pw-test")],
)
def test_xattrs_survive_backup_restore(tmp_path, compression, password):
    source = tmp_path / "src"
    source.mkdir()
    target = source / "data.bin"
    target.write_bytes(b"payload")

    if not _xattrs_supported(str(target)):
        pytest.skip("filesystem does not support user xattrs")
    os.setxattr(str(target), "user.comment", b"hello-xattr")

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    restored = tmp_path / "restored"
    restored.mkdir()

    mgr = BackupManager(
        vol_name="xa",
        input_path=str(source),
        output_path=str(backup_dir),
        compression=compression,
        password=password,
    )
    if password:
        mgr.compress_and_encrypt_pipeline()
    else:
        mgr.compress()

    restore(
        vol_name="xa",
        input_path=str(backup_dir),
        output_path=str(restored),
        encryption_key=password,
        overwrite=True,
    )

    rf = restored / "data.bin"
    assert rf.read_bytes() == b"payload"
    assert "user.comment" in os.listxattr(str(rf)), (
        f"xattr lost (compression={compression}, encrypted={bool(password)})"
    )
    assert os.getxattr(str(rf), "user.comment") == b"hello-xattr"
