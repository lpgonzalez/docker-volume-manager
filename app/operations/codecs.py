"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Single source of truth for compression codecs.

Each :class:`Codec` ties a logical compression name (``none`` / ``gz`` / ``zstd``)
to everything the rest of the app needs to know about that format:

- the archive extension (``.tar`` / ``.tar.gz`` / ``.tar.zst``),
- the Python ``tarfile`` read/write modes,
- the subprocess compressor argv used by the encrypted pipeline,
- the ``tar`` flags needed to decompress on restore,
- extra read-only aliases restore/verify still accept (``.tgz`` / ``.tzst``).

Adding or removing a codec means editing ONE entry here instead of the handful
of scattered tables this module replaces.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Codec:
    name: str  # "none" | "gz" | "zstd"
    ext: str  # canonical archive extension, e.g. ".tar.gz"
    tar_write_mode: str  # Python tarfile write mode, e.g. "w:gz"
    tar_read_mode: str  # Python tarfile read mode, e.g. "r:gz"
    # `tar -x` flags to decompress this format (empty for raw tar).
    system_tar_decompress: tuple[str, ...] = ()
    # Extra extensions restore/verify accept on read (never produced on write).
    read_aliases: tuple[str, ...] = ()
    # Factory for the encrypted-pipeline compressor argv (None for raw tar).
    compressor: Callable[[], list[str] | None] = field(default=lambda: None)


def _gz_argv() -> list[str]:
    # pigz (multi-threaded) when available, else gzip.
    binary = "pigz" if shutil.which("pigz") else "gzip"
    return [binary, "-c"]


def _zstd_argv() -> list[str]:
    # `-T0` joined: zstd parses `-T` as an attached short option.
    # `-19` is the max practical level — close to xz without the `--ultra -22`
    # memory blow-up (which adds only 2-3% ratio at 5-10x the memory cost).
    return ["zstd", "-c", "-q", "-T0", "-19"]


_CODECS: dict[str, Codec] = {
    "none": Codec("none", ".tar", "w", "r:"),
    "gz": Codec(
        "gz",
        ".tar.gz",
        "w:gz",
        "r:gz",
        system_tar_decompress=("-z",),
        read_aliases=(".tgz",),
        compressor=_gz_argv,
    ),
    "zstd": Codec(
        "zstd",
        ".tar.zst",
        "w:zst",
        "r:zst",
        system_tar_decompress=("-I", "zstd"),
        read_aliases=(".tzst",),
        compressor=_zstd_argv,
    ),
}

DEFAULT_CODEC = "zstd"

# Archive extensions restore/verify recognise, longest-first so ".tar" never
# shadows ".tar.gz" / ".tar.zst" during suffix matching.
ARCHIVE_EXTS: list[str] = sorted(
    {c.ext for c in _CODECS.values()}, key=len, reverse=True
)


def normalize_name(level: str | None) -> str:
    """Map any user-supplied compression label to a known codec name.

    Unknown / empty values fall back to the default (zstd). Used backup-side
    where the compression is a configuration value, not a filename.
    """
    if not level:
        return DEFAULT_CODEC
    name = level.strip().lower()
    return name if name in _CODECS else DEFAULT_CODEC


def for_name(name: str) -> Codec:
    """Codec for a logical name (normalised; unknown → default)."""
    return _CODECS[normalize_name(name)]


def write_mode(name: str) -> str:
    """Python tarfile write mode for a logical compression name."""
    return for_name(name).tar_write_mode


def ext_for(name: str) -> str:
    """Archive extension for a logical compression name."""
    return for_name(name).ext


def compressor_argv(name: str) -> list[str] | None:
    """Encrypted-pipeline compressor argv for a compression name.

    Exact (non-defaulting) lookup: only ``gz`` / ``zstd`` yield an argv; ``none``
    and anything unrecognised return ``None`` (i.e. "stream raw tar, no
    compressor"). This keeps the pipeline safe even if handed a stale name.
    """
    codec = _CODECS.get((name or "").strip().lower())
    return codec.compressor() if codec else None


def codec_for_path(path: str) -> Codec | None:
    """Resolve the codec from an archive filename by longest-suffix match.

    A trailing ``.gpg`` is stripped first so encrypted archives match on their
    inner extension. Returns ``None`` when nothing matches (caller decides the
    fallback, e.g. tarfile auto-detect).
    """
    lower = path.lower()
    if lower.endswith(".gpg"):
        lower = lower[: -len(".gpg")]
    best: tuple[int, Codec] | None = None
    for codec in _CODECS.values():
        for ext in (codec.ext, *codec.read_aliases):
            if lower.endswith(ext) and (best is None or len(ext) > best[0]):
                best = (len(ext), codec)
    return best[1] if best else None


def read_mode_for_path(path: str) -> str:
    """Python tarfile read mode for an archive filename ('r:*' if unknown)."""
    codec = codec_for_path(path)
    return codec.tar_read_mode if codec else "r:*"


def system_tar_decompress_flags(path: str) -> list[str]:
    """`tar -x` decompression flags for an archive filename ([] if unknown)."""
    codec = codec_for_path(path)
    return list(codec.system_tar_decompress) if codec else []


def inner_archive_suffix(enc_path: str) -> str:
    """Inner ``.tar[.xx]`` suffix of an encrypted archive (for tempfile naming)."""
    codec = codec_for_path(enc_path)
    return codec.ext if codec else ".tar"
