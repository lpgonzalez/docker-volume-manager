"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Copy manager: copy all files from input_path into output_path.

Behaviour:
- Validate input is readable and contains files, and output is writable.
- If output contains files, apply the shared overwrite policy
  (see operations.fs_overwrite): overwrite unconditionally when the flag is set,
  prompt on a TTY, default to overwrite when non-interactive.
- Delete the *contents* of the output directory (the directory itself is kept)
  and copy input contents directly into output (no extra subdirs).
- Preserve permissions/timestamps with copy2/copystat, owner/group via chown
  (as root), and best-effort extended attributes when supported.
- Progress logged at a modest frequency (~5% increments).
"""

import logging
import os
import shutil

from operations.fs_overwrite import (
    clear_directory_contents,
    is_nonempty,
    should_overwrite,
)
from progress import ProgressReporter

logger = logging.getLogger("dvm")


def _copy_xattrs(src: str, dst: str) -> None:
    """Best-effort copy of extended attributes from src to dst (if supported)."""
    try:
        listx = getattr(os, "listxattr", None)
        getx = getattr(os, "getxattr", None)
        setx = getattr(os, "setxattr", None)
        if not (listx and getx and setx):
            return
        try:
            attrs = listx(src, follow_symlinks=False)
        except TypeError:
            attrs = listx(src)
        for k in attrs:
            try:
                try:
                    val = getx(src, k, follow_symlinks=False)
                except TypeError:
                    val = getx(src, k)
                try:
                    setx(dst, k, val, follow_symlinks=False)
                except TypeError:
                    setx(dst, k, val)
            except Exception as e:
                logger.debug(
                    "Failed to copy xattr %s from %s to %s: %s", k, src, dst, e
                )
    except Exception:
        logger.debug("xattr copy not supported on this platform or failed.")


class CopyManager:
    """
    Mirror the contents of ``input_path`` into ``output_path`` while
    preserving as much filesystem metadata as the platform supports.

    Per-file behaviour:

    - Regular files: ``shutil.copy2`` (mode + mtime + atime), ``chown``
      when running as root, best-effort xattrs.
    - Symlinks: ``os.symlink`` recreating the link target verbatim;
      ``chown`` with ``follow_symlinks=False`` when supported.
    - Directories: created with ``os.makedirs``; metadata copied via
      ``shutil.copystat`` after children are populated.

    Progress is reported through :class:`ProgressReporter` — rich bar on
    TTY, throttled INFO log lines otherwise.

    Parameters
    ----------
    input_path:
        Source directory. Must be a readable directory containing at least
        one file (empty trees are rejected to fail fast).
    output_path:
        Destination directory. Created if missing. If non-empty, behaviour
        depends on ``overwrite``.
    overwrite:
        ``True`` (default): proceed without prompting, clearing destination
        contents first. ``False``: prompt the user when stdin is a TTY,
        otherwise overwrite with a warning log.
    """

    def __init__(self, input_path: str, output_path: str, overwrite: bool = True):
        self.input_path = input_path
        self.output_path = output_path
        self.overwrite = overwrite
        logger.debug(
            "CopyManager initialized input=%s output=%s overwrite=%s",
            input_path,
            output_path,
            overwrite,
        )

    def _ensure_input_readable_and_nonempty(self) -> int:
        if not os.path.isdir(self.input_path):
            logger.error(
                "Input path does not exist or is not a directory: %s", self.input_path
            )
            raise FileNotFoundError(f"Input path not found: {self.input_path}")
        if not os.access(self.input_path, os.R_OK):
            logger.error("Input path is not readable: %s", self.input_path)
            raise PermissionError(f"Input path not readable: {self.input_path}")

        # count files
        total_files = 0
        for _, _, files in os.walk(self.input_path):
            total_files += len(files)
        if total_files == 0:
            logger.error("Input directory is empty: %s", self.input_path)
            raise ValueError(f"Input directory is empty: {self.input_path}")
        logger.debug("Input directory OK: %s (files=%d)", self.input_path, total_files)
        return total_files

    def _ensure_output_writable(self) -> None:
        # create output dir if missing
        try:
            os.makedirs(self.output_path, exist_ok=True)
        except Exception as e:
            logger.exception(
                "Unable to create output directory %s: %s", self.output_path, e
            )
            raise

        if not os.access(self.output_path, os.W_OK):
            logger.error("Output path is not writable: %s", self.output_path)
            raise PermissionError(f"Output path not writable: {self.output_path}")

        logger.debug("Output directory is writable: %s", self.output_path)

    def _set_owner(self, src: str, dst: str, follow_symlinks: bool = True) -> None:
        """Try to set owner/group on dst to match src (best-effort)."""
        try:
            st = os.stat(src, follow_symlinks=follow_symlinks)
            try:
                if follow_symlinks:
                    os.chown(dst, st.st_uid, st.st_gid)
                else:
                    lchown = getattr(os, "lchown", None)
                    if lchown:
                        lchown(dst, st.st_uid, st.st_gid)
                    else:
                        os.chown(dst, st.st_uid, st.st_gid)
            except PermissionError:
                logger.debug(
                    "Insufficient permissions to change owner/group for %s", dst
                )
            except Exception as e:
                logger.debug("Failed to set owner for %s: %s", dst, e)
        except Exception:
            logger.debug("Could not stat source to copy owner info: %s", src)

    def copy(self) -> str:
        """
        Perform the copy operation.
        Returns the destination path (same as self.output_path).
        """
        logger.info("Starting operation: COPY")
        logger.info("Starting copy: %s -> %s", self.input_path, self.output_path)

        # validations
        total_files = self._ensure_input_readable_and_nonempty()
        self._ensure_output_writable()

        # if destination contains files, prepare overwrite flow
        if is_nonempty(self.output_path):
            logger.info("Destination directory %s is not empty.", self.output_path)
            if not should_overwrite(self.output_path, self.overwrite):
                logger.error("Copy aborted by user - destination not overwritten.")
                logger.info("-" * 50)
                raise PermissionError(
                    "Copy aborted by user, destination not overwritten."
                )
            # proceed to clear destination contents
            clear_directory_contents(self.output_path)
        else:
            logger.debug(
                "Destination directory %s is empty; proceeding.", self.output_path
            )

        # Build list of directories and files to replicate (relative paths)
        dir_entries = []
        file_entries = []
        for root, dirs, files in os.walk(self.input_path):
            rel_root = os.path.relpath(root, self.input_path)
            for d in dirs:
                dir_entries.append((os.path.join(rel_root, d), os.path.join(root, d)))
            for f in files:
                file_entries.append((os.path.join(rel_root, f), os.path.join(root, f)))

        total_bytes = 0
        for _rel, src_full in file_entries:
            try:
                total_bytes += os.lstat(src_full).st_size
            except OSError:
                pass
        logger.info(
            "Copying %d files (%d bytes) to %s",
            total_files,
            total_bytes,
            self.output_path,
        )

        # Create directories first and attempt to copy their metadata and ownership
        for relpath, src_full in dir_entries:
            dest_dir = os.path.join(self.output_path, relpath)
            try:
                if os.path.islink(src_full):
                    try:
                        target = os.readlink(src_full)
                        if os.path.exists(dest_dir) or os.path.islink(dest_dir):
                            try:
                                if os.path.isdir(dest_dir) and not os.path.islink(
                                    dest_dir
                                ):
                                    shutil.rmtree(dest_dir)
                                else:
                                    os.remove(dest_dir)
                            except Exception:
                                pass
                        os.symlink(target, dest_dir)
                        self._set_owner(src_full, dest_dir, follow_symlinks=False)
                    except Exception as e:
                        logger.exception(
                            "Failed to replicate symlink dir %s -> %s: %s",
                            src_full,
                            dest_dir,
                            e,
                        )
                        raise
                else:
                    os.makedirs(dest_dir, exist_ok=True)
                    try:
                        shutil.copystat(src_full, dest_dir)
                    except Exception:
                        logger.debug(
                            "Failed to copystat for dir %s -> %s", src_full, dest_dir
                        )
                    self._set_owner(src_full, dest_dir, follow_symlinks=True)
                    _copy_xattrs(src_full, dest_dir)
            except Exception as e:
                logger.exception("Failed to create directory %s: %s", dest_dir, e)
                raise

        # Copy files preserving metadata and ownership. Progress adapts to TTY:
        # rich bar on terminal, throttled logger.info lines to file/json handlers.
        with ProgressReporter(
            f"Copying (0/{total_files} files)", total_bytes, unit="bytes"
        ) as pr:
            done_files = 0
            for relpath, src_full in file_entries:
                dest_full = os.path.join(self.output_path, relpath)
                try:
                    os.makedirs(os.path.dirname(dest_full), exist_ok=True)
                    if os.path.islink(src_full):
                        target = os.readlink(src_full)
                        if os.path.exists(dest_full) or os.path.islink(dest_full):
                            try:
                                if os.path.isdir(dest_full) and not os.path.islink(
                                    dest_full
                                ):
                                    shutil.rmtree(dest_full)
                                else:
                                    os.remove(dest_full)
                            except Exception:
                                pass
                        os.symlink(target, dest_full)
                        self._set_owner(src_full, dest_full, follow_symlinks=False)
                    else:
                        shutil.copy2(src_full, dest_full)
                        self._set_owner(src_full, dest_full, follow_symlinks=True)
                        _copy_xattrs(src_full, dest_full)

                    try:
                        fsize = os.lstat(src_full).st_size
                    except OSError:
                        fsize = 0
                    done_files += 1
                    pr.advance(fsize)
                    pr.update_description(f"Copying ({done_files}/{total_files} files)")
                except Exception as e:
                    logger.exception(
                        "Failed to copy %s -> %s: %s", src_full, dest_full, e
                    )
                    raise

        # Re-apply directory timestamps now that their children are in place:
        # creating files inside a directory bumps that directory's mtime, so the
        # copystat done at creation time was overwritten. Deepest-first so a
        # parent is never re-touched after its mtime is restored.
        for relpath, src_full in sorted(dir_entries, key=lambda e: -e[0].count(os.sep)):
            dest_dir = os.path.join(self.output_path, relpath)
            if os.path.islink(src_full):
                continue  # symlink dirs: nothing to re-stat
            try:
                shutil.copystat(src_full, dest_dir)
            except Exception:
                logger.debug("Failed to re-copystat dir %s -> %s", src_full, dest_dir)

        # try to copy top-level input dir metadata into destination root
        try:
            try:
                shutil.copystat(self.input_path, self.output_path)
            except Exception:
                logger.debug(
                    "Failed to copystat top-level directory %s -> %s",
                    self.input_path,
                    self.output_path,
                )
            self._set_owner(self.input_path, self.output_path, follow_symlinks=True)
            _copy_xattrs(self.input_path, self.output_path)
        except Exception:
            logger.debug("Failed to finalize top-level metadata copy")

        logger.info("Copy finished: %s", self.output_path)
        logger.info("-" * 50)
        return self.output_path
