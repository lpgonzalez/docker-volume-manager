"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

import logging
import os
import shutil

from operations.auxiliary_methods import get_formatted_time
from progress import ProgressReporter

logger = logging.getLogger("dvm")

"""
Copy manager: copy all files from input_path into output_path.
Behaviour:
- Validate input is readable and contains files.
- Validate output is writable.
- If output contains files, inform the user and support an overwrite flow:
  * Default overwrite policy is 'Y' (yes) unless env COPY_OVERWRITE is set to another value.
  * If COPY_OVERWRITE is explicitly set to a non-yes value, and stdin is a TTY, ask for confirmation.
  * Non-interactive environments default to overwrite but log that fact.
- After confirming/deciding, delete *contents* of output directory (preserve directory itself)
  and copy input contents directly into output (no extra subdirs).
- Preserve permissions/timestamps with copy2/copystat, attempt to preserve owner/group (chown),
  and best-effort copy of extended attributes when supported.
- Progress logged at a modest frequency (~5% increments).
"""


YES_VALUES = {"y", "yes", "Y", "YES"}


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

    def _prompt_overwrite(self) -> bool:
        """Decide overwrite behaviour based on self.overwrite and interactivity."""
        if self.overwrite:
            logger.info(
                "Overwrite policy: overwrite=True, proceeding without prompt."
            )
            return True

        if os.isatty(0):
            try:
                logger.info(
                    "Destination %s contains files. overwrite=False — asking user for confirmation.",
                    self.output_path,
                )
                resp = input(
                    f"Destination '{self.output_path}' is not empty. Overwrite? [Y/n]: "
                )
                if resp.strip() in YES_VALUES or resp.strip() == "":
                    logger.info("User confirmed overwrite.")
                    return True
                logger.info("User denied overwrite.")
                return False
            except Exception:
                logger.warning(
                    "Interactive confirmation failed; defaulting to overwrite"
                )
                return True

        logger.info(
            "Non-interactive environment with overwrite=False: defaulting to overwrite. "
            "Pass --overwrite to suppress this fallback, or run with -it for confirmation."
        )
        return True

    def _clear_directory_contents(self, path: str) -> None:
        logger.info("Clearing contents of destination: %s", path)
        for entry in os.listdir(path):
            full = os.path.join(path, entry)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    # file or symlink
                    os.remove(full)
            except Exception as e:
                logger.exception(
                    "Failed to remove %s while clearing destination: %s", full, e
                )
                raise

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
        op_start_ts = get_formatted_time("%Y-%m-%d %H:%M:%S")
        logger.info("Starting operation: COPY")
        logger.info("Starting copy: %s -> %s", self.input_path, self.output_path)

        # validations
        total_files = self._ensure_input_readable_and_nonempty()
        self._ensure_output_writable()

        # if destination contains files, prepare overwrite flow
        dest_has_content = bool(os.listdir(self.output_path))
        if dest_has_content:
            logger.info("Destination directory %s is not empty.", self.output_path)
            do_overwrite = self._prompt_overwrite()
            if not do_overwrite:
                logger.error("Copy aborted by user - destination not overwritten.")
                logger.info("-" * 50)
                raise PermissionError(
                    "Copy aborted by user, destination not overwritten."
                )
            # proceed to clear destination contents
            self._clear_directory_contents(self.output_path)
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

        logger.info("Copying %d files to %s", total_files, self.output_path)

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
        with ProgressReporter("Copying", total_files) as pr:
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

                    pr.advance()
                except Exception as e:
                    logger.exception(
                        "Failed to copy %s -> %s: %s", src_full, dest_full, e
                    )
                    raise

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
