"""Unit tests for the compression codec registry (operations.codecs)."""

from __future__ import annotations

from unittest.mock import patch

from operations import codecs
from operations.codecs import compressor_argv


def test_none_returns_none():
    assert compressor_argv("none") is None


def test_unknown_returns_none():
    assert compressor_argv("bogus") is None


@patch("operations.codecs.shutil.which", return_value=None)
def test_gz_falls_back_to_gzip_when_pigz_missing(_which):
    assert compressor_argv("gz") == ["gzip", "-c"]


@patch("operations.codecs.shutil.which", return_value="/usr/bin/pigz")
def test_gz_prefers_pigz_when_available(_which):
    assert compressor_argv("gz") == ["pigz", "-c"]


def test_zstd_uses_joined_T0_flag_quiet_and_max_level():
    assert compressor_argv("zstd") == ["zstd", "-c", "-q", "-T0", "-19"]


def test_legacy_bz2_returns_none():
    """bz2 was removed from the supported set; resolver returns None."""
    assert compressor_argv("bz2") is None


def test_legacy_xz_returns_none():
    assert compressor_argv("xz") is None


# --- registry mappings -----------------------------------------------------


def test_normalize_name_defaults_to_zstd():
    assert codecs.normalize_name(None) == "zstd"
    assert codecs.normalize_name("bogus") == "zstd"
    assert codecs.normalize_name("GZ") == "gz"


def test_ext_and_write_mode():
    assert codecs.ext_for("none") == ".tar"
    assert codecs.ext_for("gz") == ".tar.gz"
    assert codecs.ext_for("zstd") == ".tar.zst"
    assert codecs.write_mode("none") == "w"
    assert codecs.write_mode("zstd") == "w:zst"


def test_read_mode_for_path_matches_longest_suffix():
    assert codecs.read_mode_for_path("backup.tar") == "r:"
    assert codecs.read_mode_for_path("backup.tar.gz") == "r:gz"
    assert codecs.read_mode_for_path("backup.tar.zst") == "r:zst"
    # encrypted: inner extension wins after stripping .gpg
    assert codecs.read_mode_for_path("backup.tar.zst.gpg") == "r:zst"
    # aliases
    assert codecs.read_mode_for_path("backup.tgz") == "r:gz"
    # unknown → auto-detect
    assert codecs.read_mode_for_path("mystery.bin") == "r:*"


def test_system_tar_decompress_flags():
    assert codecs.system_tar_decompress_flags("a.tar") == []
    assert codecs.system_tar_decompress_flags("a.tar.gz") == ["-z"]
    assert codecs.system_tar_decompress_flags("a.tar.zst") == ["-I", "zstd"]


def test_inner_archive_suffix():
    assert codecs.inner_archive_suffix("a.tar.gz.gpg") == ".tar.gz"
    assert codecs.inner_archive_suffix("a.tar.zst.gpg") == ".tar.zst"
    assert codecs.inner_archive_suffix("a.tar.gpg") == ".tar"
