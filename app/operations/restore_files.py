"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Restore manager: locate a backup and restore it to a target directory preserving metadata.

Behaviour (high level):
- Validate that a backup base name (vol_name) exists under input_path and contains at least one
  timestamped subdirectory (YYYYmmdd_HHMM[_NN]).
- If TIMESTAMP not provided, selects the most recent timestamp subdirectory.
- Locates a backup archive inside that timestamp directory (supports .tar, .tar.gz, .tar.zst)
  and optionally encrypted files ending with .gpg.
- If parity (.par2) files exist for the archive, verifies and attempts repair prior to restore.
- If encrypted (.gpg), attempts decryption using ENCRYPTION_KEY env var (best-effort).
- Extracts the tar archive into output_path preserving owners, groups, permissions,
  timestamps and extended attributes. Restoring owners requires running as root; extraction
  uses numeric ownership (--numeric-owner / numeric_owner=True).
- Detailed logging and robust error handling consistent with the rest of the project.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile

import gnupg

from cli_shared import (
    EXIT_RESTORE_ARCHIVE_CORRUPT,
    EXIT_RESTORE_BACKUP_NOT_FOUND,
    EXIT_RESTORE_DECRYPT_FAILED,
    EXIT_RESTORE_DESTINATION_ERROR,
    EXIT_RESTORE_PARITY_REPAIR_FAILED,
    EXIT_RESTORE_UNSAFE_ARCHIVE,
)
from operations import codecs
from operations.fs_overwrite import (
    clear_directory_contents,
    is_nonempty,
    should_overwrite,
)
from process_monitor import ProcessMonitor, read_proc_io
from progress import status

logger = logging.getLogger("dvm")

# Supported archive extensions (longest first to match correctly).
_ARCHIVE_EXTS = codecs.ARCHIVE_EXTS

_PAX_MSG = (
    "Archives are created using PAX format and include uid/gid and uname/gname when resolvable. "
    "To restore original owners you must extract as root and use --same-owner / numeric_owner "
    "or extract with a tool that supports PAX owner restoration."
)


class RestoreError(Exception):
    """A restore failure carrying the specific CLI exit code for its cause.

    ``code`` defaults to ``EXIT_RESTORE_ARCHIVE_CORRUPT`` (a conservative
    "archive unusable") so any raise site that forgets to set one still maps to
    a restore-range code rather than the generic operation failure.
    """

    def __init__(self, message: str, code: int = EXIT_RESTORE_ARCHIVE_CORRUPT):
        super().__init__(message)
        self.code = code


def _is_timestamp_name(name: str) -> bool:
    # Matches YYYYmmdd_HHMM or YYYYmmdd_HHMM_NN
    return bool(re.match(r"^\d{8}_\d{4}(?:(_\d{2})?)?$", name))


def _find_backup_base(input_dir: str, vol_name: str) -> str:
    base = os.path.join(input_dir, vol_name)
    if not os.path.isdir(base):
        logger.error("Backup base directory not found: %s", base)
        raise FileNotFoundError(f"Backup base directory not found: {base}")
    return base


def _select_timestamp_dir(base_dir: str, requested_ts: str | None = None) -> str:
    entries = []
    try:
        for entry in os.listdir(base_dir):
            full = os.path.join(base_dir, entry)
            if os.path.isdir(full) and _is_timestamp_name(entry):
                entries.append((entry, full))
    except Exception:
        logger.exception("Failed to list backup base directory: %s", base_dir)
        raise

    if not entries:
        logger.error("No timestamped backup directories found in %s", base_dir)
        raise FileNotFoundError(f"No timestamped backups in {base_dir}")

    if requested_ts:
        # exact match required
        for name, full in entries:
            if name == requested_ts:
                logger.debug("Using requested timestamp directory: %s", full)
                return full
        logger.error("Requested timestamp %s not found in %s", requested_ts, base_dir)
        raise FileNotFoundError(
            f"Requested timestamp {requested_ts} not found in {base_dir}"
        )

    # select most recent by name ordering (timestamps formatted to sort lexicographically)
    entries.sort(key=lambda e: e[0], reverse=True)
    chosen = entries[0][1]
    logger.debug("Selected most recent timestamp directory: %s", chosen)
    return chosen


def _find_archive_in_ts_dir(ts_dir: str) -> tuple[str, bool]:
    """
    Returns tuple (archive_path, encrypted_flag)
    Looks for supported archive file names inside ts_dir.
    """
    candidates: list[str] = []
    try:
        for fname in os.listdir(ts_dir):
            # skip directories
            full = os.path.join(ts_dir, fname)
            if not os.path.isfile(full):
                continue
            # consider both raw archive and encrypted (.gpg) variants
            lower = fname.lower()
            for ext in _ARCHIVE_EXTS:
                if lower.endswith(ext + ".gpg"):
                    candidates.append(full)
                    break
                if lower.endswith(ext):
                    candidates.append(full)
                    break
    except Exception:
        logger.exception("Failed to scan timestamp directory for archives: %s", ts_dir)
        raise

    if not candidates:
        logger.error(
            "No archive file found in %s (supported: %s)",
            ts_dir,
            ", ".join(_ARCHIVE_EXTS),
        )
        raise FileNotFoundError(f"No archive found in {ts_dir}")

    # choose most recently modified candidate
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    chosen = candidates[0]
    encrypted = chosen.lower().endswith(".gpg")
    logger.info("Selected archive for restore: %s (encrypted=%s)", chosen, encrypted)
    return chosen, encrypted


def _has_parity_for(path: str) -> str | None:
    """
    If a .par2 file exists referring to the archive, return its path, else None.
    Look for both <archive>.par2 and <archive_basename>.par2
    """
    par2_candidate = path + ".par2"
    if os.path.isfile(par2_candidate):
        return par2_candidate
    # sometimes par2 file has same base name with .par2 in dir
    base = os.path.basename(path)
    dirname = os.path.dirname(path)
    try:
        for fname in os.listdir(dirname):
            if fname.lower().endswith(".par2") and fname.startswith(base):
                return os.path.join(dirname, fname)
    except Exception:
        logger.debug("Could not scan directory for par2 files: %s", dirname)
    return None


def _run_par2_verify_or_repair(par2_path: str, archive_path: str) -> None:
    """
    Run 'par2 verify' and if necessary 'par2 repair'. Raises RestoreError on failure.
    """
    logger.info("Parity file detected: %s. Verifying integrity.", par2_path)
    try:
        proc = subprocess.run(
            ["par2", "verify", par2_path], capture_output=True, text=True
        )
        logger.debug("par2 verify stdout: %s", proc.stdout.strip())
        if proc.returncode == 0:
            logger.info("parity verify OK for %s", archive_path)
            return
        logger.warning(
            "par2 verify reported issues (rc=%s). Attempting repair.", proc.returncode
        )
        proc2 = subprocess.run(
            ["par2", "repair", par2_path, archive_path], capture_output=True, text=True
        )
        logger.debug("par2 repair stdout: %s", proc2.stdout.strip())
        if proc2.returncode != 0:
            logger.error(
                "par2 repair failed (rc=%s). stdout: %s stderr: %s",
                proc2.returncode,
                proc2.stdout,
                proc2.stderr,
            )
            raise RestoreError("par2 repair failed", EXIT_RESTORE_PARITY_REPAIR_FAILED)
        logger.info("par2 repair succeeded for %s", archive_path)
    except FileNotFoundError:
        logger.error("par2 executable not found; cannot verify/repair parity.")
        raise RestoreError(
            "par2 not available", EXIT_RESTORE_PARITY_REPAIR_FAILED
        ) from None
    except Exception as e:
        logger.exception("Unexpected par2 error: %s", e)
        raise RestoreError("par2 error", EXIT_RESTORE_PARITY_REPAIR_FAILED) from e


def _decrypt_gpg_file(enc_path: str, passphrase: str | None, out_dir: str) -> str:
    """
    Decrypt .gpg file to a temporary file inside out_dir using gnupg library.
    Returns path to decrypted file. Raises RestoreError on failure.
    """
    logger.info("Attempting to decrypt %s", enc_path)
    if not passphrase:
        logger.error("No ENCRYPTION_KEY provided for decryption")
        raise RestoreError("Missing decryption passphrase", EXIT_RESTORE_DECRYPT_FAILED)

    try:
        g = gnupg.GPG()
    except Exception as exc:
        logger.exception("Failed to initialize GPG library")
        raise RestoreError(
            "GPG initialization failed", EXIT_RESTORE_DECRYPT_FAILED
        ) from exc

    # Preserve the inner compression extension in the tempfile name so
    # _determine_tar_mode picks the right decompressor later
    # (`foo.tar.zst.gpg` -> suffix `.tar.zst`).
    inner_suffix = codecs.inner_archive_suffix(enc_path)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix="dvm_decrypted_", dir=out_dir, suffix=inner_suffix
    )
    os.close(tmp_fd)
    try:
        with open(enc_path, "rb") as f:
            status = g.decrypt_file(f, passphrase=passphrase, output=tmp_path)
        if not status.ok:
            logger.error(
                "GPG decryption failed: %s", getattr(status, "status", "<no status>")
            )
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            raise RestoreError("GPG decryption failed", EXIT_RESTORE_DECRYPT_FAILED)
        logger.info("Decryption successful -> %s", tmp_path)
        return tmp_path
    except Exception as e:
        logger.exception("Decryption error for %s: %s", enc_path, e)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise RestoreError("Decryption failed", EXIT_RESTORE_DECRYPT_FAILED) from e


def _determine_tar_mode(archive_name: str) -> str:
    return codecs.read_mode_for_path(archive_name)


def _log_archive_owner_summary(archive_path: str) -> None:
    """Log a short summary of uid/gid values found in the archive to help debugging ownership issues."""
    try:
        mode = _determine_tar_mode(archive_path)
        with tarfile.open(archive_path, mode) as tf:
            uids = {}
            for m in tf.getmembers():
                uids.setdefault((m.uid, m.uname), 0)
                uids[(m.uid, m.uname)] += 1
            if not uids:
                logger.debug("Archive %s contains no members", archive_path)
                return
            # log up to 5 entries
            items = list(uids.items())[:5]
            logger.info(
                "Archive owner summary for %s: %s",
                archive_path,
                ", ".join([f"uid={k[0]}(uname={k[1]}) count={v}" for k, v in items]),
            )
            # if everything is root/0, warn
            if len(uids) == 1 and next(iter(uids.keys()))[0] == 0:
                logger.warning(
                    "All members in archive are owned by uid=0 (root). This usually means the backup ran in an environment where host UIDs were mapped to root (e.g. inside a container). "
                    "To preserve original host uids, run the backup process where original ownership is visible to the process, or ensure volume mounts preserve UID mapping."
                )
    except Exception:
        logger.debug("Failed to read archive for owner summary: %s", archive_path)


def _extract_archive(archive_path: str, dest_dir: str) -> None:
    """
    Extract archive_path into dest_dir attempting to preserve numeric owners.
    Prefer system 'tar' with --same-owner/--preserve-permissions, fallback to tarfile.
    Raises RestoreError on failure.
    """
    logger.info("Extracting archive %s -> %s", archive_path, dest_dir)
    mode = _determine_tar_mode(archive_path)
    logger.debug("Determined tar mode %s for %s", mode, archive_path)

    # First, open the archive to perform safety checks (avoid path traversal).
    # This pre-scan is authoritative: extraction is then delegated to system tar,
    # which does NOT re-validate, so any escaping member/link must be rejected here.
    try:
        with tarfile.open(archive_path, mode) as tf:

            def _is_within_directory(directory: str, target: str) -> bool:
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
                return os.path.commonpath([abs_directory]) == os.path.commonpath(
                    [abs_directory, abs_target]
                )

            for member in tf.getmembers():
                member_path = os.path.join(dest_dir, member.name)
                if not _is_within_directory(dest_dir, member_path):
                    logger.error(
                        "Potential path traversal detected in archive member: %s",
                        member.name,
                    )
                    raise RestoreError(
                        "Unsafe archive member path", EXIT_RESTORE_UNSAFE_ARCHIVE
                    )
                # Symlinks/hardlinks: the link target must also stay inside
                # dest_dir, else a later member could be written through it to an
                # arbitrary location. Absolute targets are rejected outright;
                # relative ones are resolved against the link's own directory.
                if member.issym() or member.islnk():
                    if os.path.isabs(member.linkname):
                        logger.error(
                            "Absolute link target in archive member %s -> %s",
                            member.name,
                            member.linkname,
                        )
                        raise RestoreError(
                            "Unsafe archive link target", EXIT_RESTORE_UNSAFE_ARCHIVE
                        )
                    link_base = os.path.dirname(member_path)
                    link_target = os.path.join(link_base, member.linkname)
                    if not _is_within_directory(dest_dir, link_target):
                        logger.error(
                            "Link target escapes destination in member %s -> %s",
                            member.name,
                            member.linkname,
                        )
                        raise RestoreError(
                            "Unsafe archive link target", EXIT_RESTORE_UNSAFE_ARCHIVE
                        )
    except tarfile.ReadError:
        logger.exception("Archive is unreadable or corrupted: %s", archive_path)
        raise RestoreError("Archive unreadable or corrupted") from None
    except Exception as e:
        logger.exception("Unexpected error while inspecting archive: %s", e)
        raise RestoreError("Archive inspection failed") from e

    # Try system tar first (preferred for correct owner restoration)
    tar_bin = shutil.which("tar")
    if tar_bin:
        cmd = [
            tar_bin,
            "--extract",
            "--file",
            archive_path,
            "--directory",
            dest_dir,
            "--same-owner",
            # Restore the exact numeric uid/gid from the archive instead of
            # mapping by user/group name. Critical for Docker volume restores:
            # services like PostgreSQL or web servers key off the numeric owner,
            # which must survive even when the target host lacks the same names.
            "--numeric-owner",
            "--preserve-permissions",
            # Restore POSIX ACLs + extended attributes (incl. SELinux labels)
            # that the backup captured with the matching flags.
            "--acls",
            "--xattrs",
            "--xattrs-include=*",
        ]
        # Insert decompression flags right after the tar binary (e.g. "-z" for
        # gzip, "-I zstd" for zstd). Driven by the codec registry.
        cmd[1:1] = codecs.system_tar_decompress_flags(archive_path)

        logger.debug("Attempting extraction with system tar: %s", " ".join(cmd))
        try:
            archive_size = os.path.getsize(archive_path)
        except OSError:
            archive_size = 0

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # Progress tracks bytes tar has read from the (compressed) archive;
            # the watchdog watches the same process's total I/O for stalls.
            def _progress_bytes():
                io = read_proc_io(proc.pid)
                return io[0] if io else None

            _seen = [0]

            def _activity_bytes():
                io = read_proc_io(proc.pid)
                total = (io[0] + io[1]) if io else 0
                _seen[0] = max(_seen[0], total)
                return _seen[0]

            def _kill():
                try:
                    proc.kill()
                except Exception:
                    pass

            with ProcessMonitor(
                "Restore (extract)",
                archive_size,
                progress_fn=_progress_bytes,
                activity_fn=_activity_bytes,
                on_stall=_kill,
            ) as monitor:
                out, err = proc.communicate()
            rc = proc.returncode

            if monitor.stalled:
                raise RestoreError(
                    f"Restore extraction stalled (no I/O for {monitor.stall_timeout}s) "
                    "and was terminated."
                )
            if rc == 0:
                logger.info("Extraction via system tar succeeded")
                return
            logger.warning(
                "System tar extraction failed (rc=%s). stdout: %s stderr: %s. "
                "Falling back to Python extractor.",
                rc,
                out.decode(errors="ignore").strip() if out else "",
                err.decode(errors="ignore").strip() if err else "",
            )
        except FileNotFoundError:
            logger.debug("System tar not found despite shutil.which; falling back")
        except RestoreError:
            raise
        except Exception:
            logger.exception(
                "System tar extraction raised unexpected error; falling back"
            )

    # Fallback to Python tarfile extraction. numeric_owner=True restores the
    # exact archived uid/gid (see the system-tar note above on why that matters).
    # filter="fully_trusted" preserves EVERYTHING — ownership, mode, and the
    # setuid/setgid/sticky bits — which is the whole point of a volume backup
    # tool. The stock "data"/"tar" filters strip those high bits, so we don't use
    # them; traversal safety is already guaranteed by the authoritative pre-scan
    # above (member names AND link targets are validated before we get here).
    try:
        with (
            tarfile.open(archive_path, mode) as tf,
            status("Extracting archive (python tarfile)"),
        ):
            tf.extractall(path=dest_dir, numeric_owner=True, filter="fully_trusted")
    except tarfile.ReadError:
        logger.exception("Archive is unreadable or corrupted: %s", archive_path)
        raise RestoreError("Archive unreadable or corrupted") from None
    except PermissionError as e:
        logger.exception("Permission error while extracting to %s: %s", dest_dir, e)
        raise RestoreError(
            "Permission denied during extraction", EXIT_RESTORE_DESTINATION_ERROR
        ) from e
    except Exception as e:
        logger.exception("Unexpected error during extraction: %s", e)
        raise RestoreError("Extraction failed") from e


def restore(
    vol_name: str,
    input_path: str,
    output_path: str,
    timestamp: str | None = None,
    encryption_key: str | None = None,
    overwrite: bool = True,
) -> str:
    """
    Restore a backup created by :class:`BackupManager`.

    Flow:

    1. Locate ``input_path/<vol_name>/<timestamp>/`` (or the most recent
       timestamp dir if ``timestamp`` is None).
    2. Pick the archive inside that timestamp dir, detecting compression
       and ``.gpg`` encryption from the filename suffix.
    3. If PAR2 files are present, run ``par2 verify``; on damage, attempt
       ``par2 repair`` in place. Failure here aborts the restore.
    4. If encrypted, decrypt to a tempfile preserving the inner suffix
       (``.tar.gz`` / ``.tar.zst``) so the next stage picks the right mode.
    5. If ``output_path`` is non-empty, honour the ``overwrite`` flag —
       overwrite=True clears it, overwrite=False prompts on TTY or aborts.
    6. Extract the archive preserving uid/gid/mode/mtime where possible
       (``--same-owner`` + ``numeric_owner`` when running as root).

    Parameters
    ----------
    vol_name:
        Backup base name. Must match how the backup was created.
    input_path:
        Directory containing the timestamped subdirectories.
    output_path:
        Where to extract. Created if missing.
    timestamp:
        Specific backup version. None picks the most recent.
    encryption_key:
        Symmetric passphrase. For asymmetric encryption, ensure the private
        key is in the active GNUPGHOME — this function does not import it.
    overwrite:
        ``True`` (default) clears a non-empty destination automatically.
        ``False`` prompts on TTY; in non-TTY contexts it overwrites with a
        warning log (legacy behaviour kept for backward compat).

    Returns
    -------
    str
        ``output_path`` (the restoration target).

    Raises
    ------
    RestoreError
        Any step failed cleanly (backup not found, archive unreadable,
        decryption failed, parity repair failed, extraction failed,
        destination overwrite refused). The exception message identifies
        which step.
    """
    logger.info(
        "Restore requested: vol=%s input=%s output=%s timestamp=%s",
        vol_name,
        input_path,
        output_path,
        timestamp,
    )
    logger.debug("Note: %s", _PAX_MSG)

    # Validate output path exists (create if missing)
    try:
        os.makedirs(output_path, exist_ok=True)
    except Exception as exc:
        logger.exception("Unable to create output path: %s", output_path)
        raise RestoreError(
            "Cannot prepare output directory", EXIT_RESTORE_DESTINATION_ERROR
        ) from exc

    # Locate backup base and timestamp dir
    try:
        base_dir = _find_backup_base(input_path, vol_name)
        ts_dir = _select_timestamp_dir(base_dir, timestamp)
    except Exception as e:
        logger.error("Failed to locate backup to restore: %s", e)
        raise RestoreError("Backup not found", EXIT_RESTORE_BACKUP_NOT_FOUND) from e

    # Find archive and whether encrypted
    try:
        archive_path, encrypted = _find_archive_in_ts_dir(ts_dir)
    except Exception as e:
        logger.error("No suitable archive found in %s: %s", ts_dir, e)
        raise RestoreError("Archive not found", EXIT_RESTORE_BACKUP_NOT_FOUND) from e

    # Check readability
    if not os.access(archive_path, os.R_OK):
        logger.error("Archive is not readable: %s", archive_path)
        raise RestoreError("Archive not readable")

    # Parity handling
    par2 = _has_parity_for(archive_path)
    if par2:
        try:
            _run_par2_verify_or_repair(par2, archive_path)
        except RestoreError as e:
            logger.error("Parity check/repair failed: %s", e)
            raise

    # If output directory is non-empty handle overwrite policy (mirror copy behaviour)
    try:
        if is_nonempty(output_path):
            logger.info(
                "Output directory %s is not empty; applying overwrite policy",
                output_path,
            )
            if not should_overwrite(output_path, overwrite):
                logger.error(
                    "Restore aborted: destination not overwritten as per policy"
                )
                raise RestoreError(
                    "Destination not overwritten per policy",
                    EXIT_RESTORE_DESTINATION_ERROR,
                )
            # clear contents
            try:
                clear_directory_contents(output_path)
            except Exception as e:
                logger.error("Failed to clear destination %s: %s", output_path, e)
                raise RestoreError(
                    "Failed to prepare destination for restore",
                    EXIT_RESTORE_DESTINATION_ERROR,
                ) from e
    except Exception as e:
        logger.error("Error while preparing destination: %s", e)
        raise RestoreError(
            "Destination preparation failed", EXIT_RESTORE_DESTINATION_ERROR
        ) from e

    # Decrypt if necessary
    temp_decrypted: str | None = None
    try:
        if encrypted:
            try:
                temp_decrypted = _decrypt_gpg_file(archive_path, encryption_key, ts_dir)
                working_archive = temp_decrypted
            except RestoreError:
                logger.error("Decryption failed for %s", archive_path)
                raise
        else:
            working_archive = archive_path

        if not os.path.isfile(working_archive) or not os.access(
            working_archive, os.R_OK
        ):
            logger.error("Working archive not accessible: %s", working_archive)
            raise RestoreError("Working archive not accessible")

        # Log owner summary from archive to help diagnose ownership restoration issues
        _log_archive_owner_summary(working_archive)

        # Extract into output_path
        _extract_archive(working_archive, output_path)

        logger.info("Restore completed into: %s", output_path)
        logger.info("Reminder: %s", _PAX_MSG)
        return output_path
    finally:
        # cleanup temporary decrypted file if any
        if temp_decrypted:
            try:
                os.remove(temp_decrypted)
                logger.debug("Removed temporary decrypted file: %s", temp_decrypted)
            except Exception:
                logger.debug(
                    "Failed to remove temporary decrypted file: %s", temp_decrypted
                )
