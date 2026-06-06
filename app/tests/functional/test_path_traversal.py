"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Functional tests for archive extraction safety. The pre-scan in
``_extract_archive`` is authoritative because extraction is delegated to system
``tar`` (which does not re-validate), so escaping members and links must be
rejected before any extraction happens.
"""

from __future__ import annotations

import tarfile

import pytest

from operations.restore_files import RestoreError, _extract_archive


def _make_tar(path, build):
    with tarfile.open(path, "w") as tf:
        build(tf)


def _add_symlink(tf, name, linkname):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = linkname
    tf.addfile(info)


def _add_file(tf, name, data=b"hello"):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    import io

    tf.addfile(info, io.BytesIO(data))


def test_member_name_traversal_is_rejected(tmp_path):
    archive = tmp_path / "evil.tar"
    _make_tar(archive, lambda tf: _add_file(tf, "../escape.txt"))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(RestoreError):
        _extract_archive(str(archive), str(dest))


def test_symlink_with_absolute_target_is_rejected(tmp_path):
    archive = tmp_path / "evil.tar"
    _make_tar(archive, lambda tf: _add_symlink(tf, "link", "/etc/passwd"))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(RestoreError):
        _extract_archive(str(archive), str(dest))


def test_symlink_escaping_relative_target_is_rejected(tmp_path):
    archive = tmp_path / "evil.tar"
    _make_tar(archive, lambda tf: _add_symlink(tf, "link", "../../etc/passwd"))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(RestoreError):
        _extract_archive(str(archive), str(dest))


def test_safe_relative_symlink_extracts_fine(tmp_path):
    archive = tmp_path / "ok.tar"

    def build(tf):
        _add_file(tf, "real.txt", b"payload")
        _add_symlink(tf, "alias.txt", "real.txt")

    _make_tar(archive, build)
    dest = tmp_path / "out"
    dest.mkdir()
    # Must not raise: target stays inside dest_dir.
    _extract_archive(str(archive), str(dest))
    assert (dest / "real.txt").read_bytes() == b"payload"
    assert (dest / "alias.txt").exists()
