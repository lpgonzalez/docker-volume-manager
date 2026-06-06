"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Metadata-fidelity tests for restore. Preserving the exact numeric uid/gid is
fundamental for this tool: Docker volume restores feed services (PostgreSQL, web
servers, ...) that break if ownership changes. The backup/restore roundtrip
covers the primary system-tar path; here we pin the Python *fallback* path,
which previously normalised ownership and silently lost it.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile

from operations import restore_files
from operations.restore_files import _extract_archive


def _make_pax_tar(path, name, *, uid, gid, mode, data=b"payload"):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tf:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.uid = uid
        info.gid = gid
        info.mode = mode
        tf.addfile(info, io.BytesIO(data))


def test_python_fallback_preserves_numeric_owner_and_mode(tmp_path, monkeypatch):
    archive = tmp_path / "vol.tar"
    _make_pax_tar(archive, "data.bin", uid=4242, gid=4243, mode=0o640)
    dest = tmp_path / "out"
    dest.mkdir()

    # Force the Python tarfile fallback (pretend system tar is unavailable).
    monkeypatch.setattr(restore_files.shutil, "which", lambda name: None)
    _extract_archive(str(archive), str(dest))

    extracted = dest / "data.bin"
    assert extracted.exists()
    st = extracted.lstat()
    # Mode is preserved regardless of privileges.
    assert stat.S_IMODE(st.st_mode) == 0o640
    # chown only happens as root (tarfile skips it otherwise), which is the
    # test-container default.
    if os.geteuid() == 0:
        assert st.st_uid == 4242
        assert st.st_gid == 4243


def test_python_fallback_preserves_setgid_bit(tmp_path, monkeypatch):
    # filter="data" would strip setuid/setgid/sticky; filter="tar" keeps them.
    archive = tmp_path / "vol.tar"
    _make_pax_tar(archive, "shared.bin", uid=0, gid=0, mode=0o2755)
    dest = tmp_path / "out"
    dest.mkdir()

    monkeypatch.setattr(restore_files.shutil, "which", lambda name: None)
    _extract_archive(str(archive), str(dest))

    st = (dest / "shared.bin").lstat()
    assert st.st_mode & stat.S_ISGID, "setgid bit was lost in the fallback path"
