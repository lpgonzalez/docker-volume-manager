"""Functional tests: CopyManager dir → dir with metadata preservation."""

from __future__ import annotations

import os

import pytest

from operations.copy_files import CopyManager


def test_copy_preserves_tree(tmp_path, data_tree, fingerprint):
    source = data_tree
    destination = tmp_path / "dst"
    destination.mkdir()

    CopyManager(str(source), str(destination), overwrite=True).copy()

    before = fingerprint(source)
    after = fingerprint(destination)
    assert set(after) == set(before)

    compare_ownership = os.geteuid() == 0
    for rel in before:
        b_type, b_size, b_mode, b_uid, b_gid, b_content = before[rel]
        a_type, a_size, a_mode, a_uid, a_gid, a_content = after[rel]
        assert a_type == b_type, f"type mismatch at {rel}"
        assert a_size == b_size, f"size mismatch at {rel}"
        assert a_content == b_content, f"content mismatch at {rel}"
        # CopyManager uses shutil.copy2 + chown; ownership preserved only as root.
        if compare_ownership:
            assert a_uid == b_uid, f"uid mismatch at {rel}"
            assert a_gid == b_gid, f"gid mismatch at {rel}"


def test_copy_missing_source_raises(tmp_path):
    missing = tmp_path / "does-not-exist"
    destination = tmp_path / "dst"
    destination.mkdir()
    with pytest.raises(FileNotFoundError):
        CopyManager(str(missing), str(destination), overwrite=True).copy()


def test_copy_empty_source_raises(tmp_path):
    empty_src = tmp_path / "empty"
    empty_src.mkdir()
    destination = tmp_path / "dst"
    destination.mkdir()
    with pytest.raises(ValueError, match="empty"):
        CopyManager(str(empty_src), str(destination), overwrite=True).copy()
