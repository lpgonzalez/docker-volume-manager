"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Backup-store abstraction for the interactive wizard.

The wizard needs to list and validate backups *before* dispatching, and it must
behave the same whether the backup lives in:

- a **local bind-mount directory**, directly visible to this container
  (``LocalDirStore``), or
- a **Docker volume** or an **absolute host path**, neither mounted into the
  wizard itself — reached by spawning a throwaway container that mounts it at
  ``/target`` (``RemoteStore``; ``VolumeStore``/``HostPathStore`` are thin,
  back-compat labelled subclasses).

All implement one interface so the wizard flow stays identical. Layout assumed
everywhere: ``<root>/<name>/<YYYYmmdd_HHMM[_NN]>/<name>.<ext>[.gpg]``.
"""

from __future__ import annotations

import os
import re
import shlex

from docker_client import DockerClient, DockerError
from operations import codecs

# Timestamp directory names produced by BackupManager (e.g. 20260613_2210, _01).
_TS_RE = re.compile(r"^\d{8}_\d{4}(?:_\d{2})?$")
# Archive suffixes plus their encrypted (.gpg) variants, as one tuple so a single
# str.endswith() call can match any of them.
_ARCHIVE_SUFFIXES = tuple(codecs.ARCHIVE_EXTS) + tuple(
    ext + ".gpg" for ext in codecs.ARCHIVE_EXTS
)


def is_timestamp(name: str) -> bool:
    return bool(_TS_RE.match(name))


def looks_like_archive(fname: str) -> bool:
    return fname.lower().endswith(_ARCHIVE_SUFFIXES)


class BackupStore:
    """Interface: list/validate backups regardless of where they live."""

    label: str

    def list_names(self) -> list[str]:
        """Candidate backup base names (immediate sub-directories), sorted."""
        raise NotImplementedError

    def list_timestamps(self, name: str) -> list[str]:
        """Timestamp dirs under ``name`` that look like backups, newest first."""
        raise NotImplementedError

    def has_archive(self, name: str, timestamp: str) -> bool:
        """True if ``name/timestamp`` holds a backup archive file."""
        raise NotImplementedError

    def is_writable(self) -> bool:
        """True if new backups can be written here (used by the backup flow)."""
        raise NotImplementedError


class LocalDirStore(BackupStore):
    """A backup store backed by a directory visible to this container."""

    def __init__(self, path: str):
        self.path = path
        self.label = path

    def list_names(self) -> list[str]:
        try:
            return sorted(
                n
                for n in os.listdir(self.path)
                if os.path.isdir(os.path.join(self.path, n))
            )
        except OSError:
            return []

    def list_timestamps(self, name: str) -> list[str]:
        base = os.path.join(self.path, name)
        try:
            entries = os.listdir(base)
        except OSError:
            return []
        ts = [
            n
            for n in entries
            if is_timestamp(n) and os.path.isdir(os.path.join(base, n))
        ]
        return sorted(ts, reverse=True)

    def has_archive(self, name: str, timestamp: str) -> bool:
        ts_dir = os.path.join(self.path, name, timestamp)
        try:
            return any(looks_like_archive(f) for f in os.listdir(ts_dir))
        except OSError:
            return False

    def is_writable(self) -> bool:
        return os.path.isdir(self.path) and os.access(self.path, os.W_OK)


class RemoteStore(BackupStore):
    """A backup store reachable only via a throwaway helper container.

    Each query spawns a short-lived container that bind-mounts the location at
    ``/target`` and runs ``find``. ``mount_key`` is either a **Docker volume
    name** or an **absolute host path** — docker-py resolves an absolute path as
    a bind mount and a bare name as a named volume, so the same code serves both.
    ``|| true`` keeps a missing sub-path from failing the container; any Docker
    error degrades to an empty result.
    """

    def __init__(
        self,
        mount_key: str,
        client: DockerClient | None = None,
        label: str | None = None,
    ):
        self.mount_key = mount_key
        self.client = client or DockerClient()
        self.label = label if label is not None else repr(mount_key)

    def _find(self, subpath: str, kind: str, *, mode: str = "ro") -> list[str]:
        target = "/target" + (f"/{subpath}" if subpath else "")
        script = (
            f"find {shlex.quote(target)} -mindepth 1 -maxdepth 1 -type {kind} "
            "2>/dev/null || true"
        )
        try:
            out = self.client.run_throwaway(
                ["sh", "-c", script],
                volumes={self.mount_key: {"bind": "/target", "mode": mode}},
            )
        except DockerError:
            return []
        return [
            os.path.basename(line.strip()) for line in out.splitlines() if line.strip()
        ]

    def list_names(self) -> list[str]:
        return sorted(self._find("", "d"))

    def list_timestamps(self, name: str) -> list[str]:
        ts = [n for n in self._find(name, "d") if is_timestamp(n)]
        return sorted(ts, reverse=True)

    def has_archive(self, name: str, timestamp: str) -> bool:
        files = self._find(f"{name}/{timestamp}", "f")
        return any(looks_like_archive(f) for f in files)

    def is_writable(self) -> bool:
        # A mounted volume/host path is writable by construction; the pivot
        # mounts it rw for the actual operation.
        return True


class VolumeStore(RemoteStore):
    """``RemoteStore`` for a Docker volume (back-compat name)."""

    def __init__(self, volume: str, client: DockerClient | None = None):
        super().__init__(volume, client, label=f"volume {volume!r}")


class HostPathStore(RemoteStore):
    """``RemoteStore`` for an absolute host directory path."""

    def __init__(self, path: str, client: DockerClient | None = None):
        super().__init__(path, client, label=f"host {path}")


def list_host_subdirs(client: DockerClient, host_path: str) -> list[str]:
    """Immediate sub-directory names of a host path, via a throwaway helper.

    Used by the wizard's host browser to descend the host filesystem without
    bind-mounting it into the main container. Empty on any Docker error.
    """
    return RemoteStore(host_path, client).list_names()
