"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Unit tests for the wizard's backup-store abstraction (local dir + volume).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from docker_client import DockerError
from wizard_store import LocalDirStore, VolumeStore


def _make_layout(root):
    # root/yadee/<ts>/yadee.tar.zst  +  an older gz  +  a name with no backups
    (root / "yadee" / "20260613_2210").mkdir(parents=True)
    (root / "yadee" / "20260613_2210" / "yadee.tar.zst").write_text("x")
    (root / "yadee" / "20260101_0900").mkdir(parents=True)
    (root / "yadee" / "20260101_0900" / "yadee.tar.gz").write_text("y")
    (root / "empty-name").mkdir()  # a dir but no timestamp subdirs
    (root / "loose.txt").write_text("not a dir")


def test_local_list_names_only_subdirs(tmp_path):
    _make_layout(tmp_path)
    store = LocalDirStore(str(tmp_path))
    assert store.list_names() == ["empty-name", "yadee"]


def test_local_list_timestamps_newest_first(tmp_path):
    _make_layout(tmp_path)
    store = LocalDirStore(str(tmp_path))
    assert store.list_timestamps("yadee") == ["20260613_2210", "20260101_0900"]
    assert store.list_timestamps("empty-name") == []


def test_local_has_archive(tmp_path):
    _make_layout(tmp_path)
    store = LocalDirStore(str(tmp_path))
    assert store.has_archive("yadee", "20260613_2210") is True
    # A timestamp dir with no archive file → False.
    (tmp_path / "yadee" / "20260201_1200").mkdir()
    (tmp_path / "yadee" / "20260201_1200" / "notes.md").write_text("z")
    assert store.has_archive("yadee", "20260201_1200") is False


def test_local_is_writable(tmp_path):
    assert LocalDirStore(str(tmp_path)).is_writable() is True
    assert LocalDirStore(str(tmp_path / "missing")).is_writable() is False


def test_local_missing_dir_degrades_to_empty(tmp_path):
    store = LocalDirStore(str(tmp_path / "nope"))
    assert store.list_names() == []
    assert store.list_timestamps("x") == []
    assert store.has_archive("x", "y") is False


# --- VolumeStore: throwaway `find` output is parsed (client mocked) ----------


def test_volume_list_names_parses_basenames():
    client = MagicMock()
    client.run_throwaway.return_value = "/target/yadee\n/target/other\n"
    store = VolumeStore("vol", client=client)
    assert store.list_names() == ["other", "yadee"]


def test_volume_list_timestamps_filters_and_sorts():
    client = MagicMock()
    client.run_throwaway.return_value = (
        "/target/yadee/20260101_0900\n/target/yadee/20260613_2210\n/target/yadee/junk\n"
    )
    store = VolumeStore("vol", client=client)
    assert store.list_timestamps("yadee") == ["20260613_2210", "20260101_0900"]


def test_volume_has_archive_detects_extension():
    client = MagicMock()
    store = VolumeStore("vol", client=client)
    client.run_throwaway.return_value = "/target/yadee/20260613_2210/yadee.tar.zst\n"
    assert store.has_archive("yadee", "20260613_2210") is True
    client.run_throwaway.return_value = "/target/yadee/20260613_2210/readme.txt\n"
    assert store.has_archive("yadee", "20260613_2210") is False


def test_volume_docker_error_degrades_to_empty():
    client = MagicMock()
    client.run_throwaway.side_effect = DockerError("socket gone")
    store = VolumeStore("vol", client=client)
    assert store.list_names() == []
    assert store.is_writable() is True
