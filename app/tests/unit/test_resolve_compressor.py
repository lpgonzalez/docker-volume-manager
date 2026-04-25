"""Unit tests for backup_files._resolve_compressor."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from operations.backup_files import _resolve_compressor


def test_none_returns_none():
    assert _resolve_compressor("none") is None


def test_unknown_returns_none():
    assert _resolve_compressor("bogus") is None


@patch("operations.backup_files.shutil.which", return_value=None)
def test_gz_falls_back_to_gzip_when_pigz_missing(_which):
    assert _resolve_compressor("gz") == ["gzip", "-c"]


@patch("operations.backup_files.shutil.which", return_value="/usr/bin/pigz")
def test_gz_prefers_pigz_when_available(_which):
    assert _resolve_compressor("gz") == ["pigz", "-c"]


def test_zstd_uses_joined_T0_flag_quiet_and_max_level():
    assert _resolve_compressor("zstd") == ["zstd", "-c", "-q", "-T0", "-19"]


def test_legacy_bz2_returns_none():
    """bz2 was removed from the supported set; resolver returns None."""
    assert _resolve_compressor("bz2") is None


def test_legacy_xz_returns_none():
    assert _resolve_compressor("xz") is None
