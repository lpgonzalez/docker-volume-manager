"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Shared destination-overwrite helpers used by both restore and copy.

These encode one consistent policy for "the destination already has data":
- ``is_nonempty`` — does the destination contain anything?
- ``should_overwrite`` — resolve the overwrite decision from the explicit flag
  and interactivity (prompt on a TTY, default to overwrite when non-interactive).
- ``clear_directory_contents`` — wipe a directory's contents in place.
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger("dvm")

# Affirmative answers to the interactive overwrite prompt.
YES_VALUES = {"y", "yes", "Y", "YES"}


def is_nonempty(path: str) -> bool:
    """True if ``path`` contains entries. Unreadable paths count as non-empty
    so we never silently overwrite something we couldn't inspect."""
    try:
        return bool(os.listdir(path))
    except OSError:
        return True


def should_overwrite(dest: str, overwrite: bool) -> bool:
    """Resolve the overwrite decision.

    ``overwrite=True`` proceeds unconditionally. Otherwise, on a TTY the user is
    prompted; in a non-interactive context we default to overwrite (the caller
    is expected to pass ``--overwrite`` explicitly in automation).
    """
    if overwrite:
        logger.info("Overwrite policy: overwrite=True, proceeding without prompt.")
        return True

    if os.isatty(0):
        try:
            logger.info(
                "Destination %s contains files. overwrite=False — asking for confirmation.",
                dest,
            )
            resp = input(f"Destination '{dest}' is not empty. Overwrite? [Y/n]: ")
            if resp.strip() in YES_VALUES or resp.strip() == "":
                logger.info("User confirmed overwrite.")
                return True
            logger.info("User denied overwrite.")
            return False
        except Exception:
            logger.warning("Interactive confirmation failed; defaulting to overwrite")
            return True

    logger.info(
        "Non-interactive environment with overwrite=False: defaulting to overwrite. "
        "Pass --overwrite to suppress this fallback, or run with -it for confirmation."
    )
    return True


def clear_directory_contents(path: str) -> None:
    """Remove every entry inside ``path`` (the directory itself is kept)."""
    logger.info("Clearing contents of destination: %s", path)
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
        except Exception as e:
            logger.exception(
                "Failed to remove %s while clearing destination: %s", full, e
            )
            raise
