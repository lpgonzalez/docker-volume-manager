"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Rename a Docker volume.

Docker has no native `docker volume rename`. This module implements it as:
  1. Pre-stats of source (file count + total bytes).
  2. Create target volume.
  3. Copy contents via a helper container running `python main.py copy` — this
     reuses the tested CopyManager (metadata preservation + progress bars).
  4. Post-stats of target; fail if they don't match source.
  5. Remove source (unless keep_source=True).

Rollback: if any step after target creation fails, the target volume is
removed so the source remains authoritative.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cli_shared import (
    EXIT_RENAME_COPY_FAILED,
    EXIT_RENAME_SOURCE_MISSING,
    EXIT_RENAME_TARGET_EXISTS,
    EXIT_RENAME_VOLUME_IN_USE,
    EXIT_VALIDATION,
)
from docker_client import DockerClient, DockerError, format_size

logger = logging.getLogger("dvm")


class RenameError(RuntimeError):
    """Raised when a rename fails; callers should surface this to the user.

    Carries the specific CLI exit code for the failure (``code``), defaulting to
    the generic rename-copy failure so any unannotated raise still lands in the
    rename range.
    """

    def __init__(self, message: str, code: int = EXIT_RENAME_COPY_FAILED):
        super().__init__(message)
        self.code = code


@dataclass
class RenameResult:
    source: str
    target: str
    files_copied: int
    bytes_copied: int
    source_deleted: bool


def rename_volume(
    source: str,
    target: str,
    *,
    keep_source: bool = False,
    force: bool = False,
    client: DockerClient | None = None,
) -> RenameResult:
    """Rename a Docker volume. See module docstring for the algorithm."""

    if source == target:
        raise RenameError("Source and target names are identical", EXIT_VALIDATION)

    client = client or DockerClient()

    try:
        if not client.volume_exists(source):
            raise RenameError(
                f"Source volume {source!r} does not exist", EXIT_RENAME_SOURCE_MISSING
            )
        if client.volume_exists(target):
            raise RenameError(
                f"Target volume {target!r} already exists", EXIT_RENAME_TARGET_EXISTS
            )
        source_info = client.inspect_volume(source)
    except DockerError as exc:
        raise RenameError(str(exc)) from exc

    if source_info.containers and not force:
        raise RenameError(
            f"Source volume {source!r} is in use by: "
            f"{', '.join(source_info.containers)}. Pass force=True to proceed "
            "(risk of corruption if containers write during copy).",
            EXIT_RENAME_VOLUME_IN_USE,
        )

    logger.info("Reading source stats for %s...", source)
    source_stats = _volume_stats(client, source)
    logger.info(
        "Source %s: %d files, %s",
        source,
        source_stats["files"],
        format_size(source_stats["bytes"]),
    )

    logger.info("Creating target volume: %s", target)
    try:
        client.create_volume(target)
    except DockerError as exc:
        raise RenameError(f"Failed to create target volume {target!r}: {exc}") from exc

    try:
        logger.info("Copying data %s -> %s (via helper container)", source, target)
        _copy_contents(client, source, target)

        logger.info("Verifying target %s...", target)
        target_stats = _volume_stats(client, target)
        logger.info(
            "Target %s: %d files, %s",
            target,
            target_stats["files"],
            format_size(target_stats["bytes"]),
        )

        if target_stats != source_stats:
            raise RenameError(
                f"Verification failed: source {source_stats} != target {target_stats}"
            )
        logger.info("Verification passed: source and target stats match")

    except Exception:
        logger.error("Rename failed; rolling back target volume %s", target)
        try:
            client.remove_volume(target, force=True)
            logger.info("Rollback complete: target %s removed", target)
        except Exception as exc:
            logger.error(
                "Rollback: failed to remove target %s: %s — remove manually",
                target,
                exc,
            )
        raise

    source_deleted = False
    if not keep_source:
        logger.info("Removing source volume: %s", source)
        try:
            client.remove_volume(source, force=force)
            source_deleted = True
            logger.info("Source %s removed", source)
        except Exception as exc:
            logger.error(
                "Target %s has data, but source %s could not be removed: %s. "
                "Remove manually.",
                target,
                source,
                exc,
            )
    else:
        logger.info("Keeping source volume %s (keep_source=True)", source)

    return RenameResult(
        source=source,
        target=target,
        files_copied=source_stats["files"],
        bytes_copied=source_stats["bytes"],
        source_deleted=source_deleted,
    )


def _volume_stats(client: DockerClient, name: str) -> dict[str, int]:
    """Return {'files': N, 'bytes': M} for a volume. Raises RenameError on failure."""
    try:
        output = client.run_throwaway(
            [
                "bash",
                "-c",
                "set -euo pipefail; "
                "find /target -mindepth 1 -type f | wc -l; "
                "du -sb /target | cut -f1",
            ],
            volumes={name: {"bind": "/target", "mode": "ro"}},
        )
    except DockerError as exc:
        raise RenameError(f"Cannot read volume stats for {name!r}: {exc}") from exc

    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RenameError(f"Unexpected stats output for {name!r}: {output!r}")
    try:
        return {"files": int(lines[0]), "bytes": int(lines[1])}
    except ValueError as exc:
        raise RenameError(
            f"Cannot parse stats output for {name!r}: {output!r}"
        ) from exc


def _copy_contents(client: DockerClient, source: str, target: str) -> None:
    """Spawn a helper running `python main.py copy` with both volumes mounted."""
    exit_code = client.run_helper_streaming(
        command=["python", "main.py", "copy"],
        volume_mounts={
            source: {"bind": "/dvm/source", "mode": "ro"},
            target: {"bind": "/dvm/dest", "mode": "rw"},
        },
        env={
            "OPERATION": "COPY",
            "COPY_OVERWRITE": "Y",
            "LOG_LEVEL": "INFO",
            "LOG_OUTPUT": "console",
        },
        inherit_bind_mounts=False,
        tty=False,
    )
    if exit_code != 0:
        raise RenameError(
            f"Copy helper failed with exit code {exit_code} "
            "(see log output above for details)"
        )
