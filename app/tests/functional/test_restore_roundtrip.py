"""
Functional tests: backup → restore round-trip across compression, encryption
and parity combinations. Uses the rich `data_tree` fixture and compares source
vs restored tree with `tree_fingerprint` (structure + content + mode + ownership).
"""

from __future__ import annotations

import pytest

from operations.backup_files import BackupManager
from operations.restore_files import restore

# (compression, password, parity_percentage) — parity=0 means no parity.
# Primary matrix — only the supported (post-cleanup) algorithms.
# Legacy bz2/xz round-trips live in test_legacy_decompression.py to keep
# this matrix aligned with what the CLI currently accepts.
MATRIX = [
    ("gz", None, 0),
    ("zstd", None, 0),
    ("gz", "pw-test", 0),
    ("zstd", "pw-test", 0),
    ("zstd", "pw-test", 10),
]


@pytest.mark.parametrize("compression,password,parity", MATRIX)
def test_backup_then_restore_is_bit_equivalent(
    tmp_path, data_tree, fingerprint, compression, password, parity
):
    source = data_tree
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    restored = tmp_path / "restored"
    restored.mkdir()

    manager = BackupManager(
        vol_name="roundtrip",
        input_path=str(source),
        output_path=str(backup_dir),
        compression=compression,
        password=password,
        create_parity=bool(parity),
        parity_percentage=parity or None,
    )

    if password:
        manager.compress_and_encrypt_pipeline()
    else:
        archive = manager.compress()
        if parity:
            manager.create_parity_file(archive, parity)

    before = fingerprint(source)

    restore(
        vol_name="roundtrip",
        input_path=str(backup_dir),
        output_path=str(restored),
        encryption_key=password,
        overwrite=True,
    )

    after = fingerprint(restored)

    # Entry set must match
    assert set(after) == set(before), (
        f"restore differs — missing={set(before) - set(after)!r}, "
        f"extra={set(after) - set(before)!r}"
    )

    # Compare every entry. When running as root (inside the test image) tar
    # preserves UIDs/GIDs; outside root they collapse to the extracting user.
    import os

    compare_ownership = os.geteuid() == 0
    for rel in before:
        b_type, b_size, b_mode, b_uid, b_gid, b_content = before[rel]
        a_type, a_size, a_mode, a_uid, a_gid, a_content = after[rel]
        assert a_type == b_type, f"type mismatch at {rel}"
        assert a_size == b_size, f"size mismatch at {rel}"
        assert a_mode == b_mode, f"mode mismatch at {rel}"
        assert a_content == b_content, f"content mismatch at {rel}"
        if compare_ownership:
            assert a_uid == b_uid, f"uid mismatch at {rel}"
            assert a_gid == b_gid, f"gid mismatch at {rel}"
