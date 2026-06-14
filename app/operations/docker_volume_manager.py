"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Docker_Volume_Manager: orchestrates operations BACKUP, RESTORE, VERIFY, COPY
"""

import logging
import os

from cli_shared import (
    EXIT_BACKUP_COMPRESSION_FAILED,
    EXIT_BACKUP_ENCRYPTION_FAILED,
    EXIT_BACKUP_INPUT_NOT_FOUND,
    EXIT_BACKUP_OUTPUT_NOT_WRITABLE,
    EXIT_COPY_INPUT_NOT_FOUND,
    EXIT_COPY_OUTPUT_NOT_WRITABLE,
    EXIT_COPY_OVERWRITE_REFUSED,
    EXIT_VERIFY_BACKUP_MISSING,
    EXIT_VERIFY_CORRUPT_REPAIRABLE,
    EXIT_VERIFY_DAMAGED,
    EXIT_VERIFY_DECRYPT_FAILED,
    EXIT_VERIFY_REPAIR_FAILED,
    EXIT_VERIFY_UNREPAIRABLE,
    OperationError,
)
from config import Config
from operations import codecs
from operations.backup_files import BackupManager
from operations.copy_files import CopyManager
from operations.restore_files import RestoreError
from operations.restore_files import restore as restore_backup
from operations.verify_backup import BackupVerifier

logger = logging.getLogger("dvm")

# Maps a verify outcome code (see _classify_outcome) to its process exit code.
# Healthy outcomes (INTACT / INTACT_NO_PARITY / REPAIRED) never reach this map.
_VERIFY_OUTCOME_EXIT = {
    "CORRUPT_UNREPAIRABLE": EXIT_VERIFY_UNREPAIRABLE,
    "CORRUPT_REPAIRABLE": EXIT_VERIFY_CORRUPT_REPAIRABLE,
    "REPAIR_FAILED": EXIT_VERIFY_REPAIR_FAILED,
    "DECRYPT_FAILED": EXIT_VERIFY_DECRYPT_FAILED,
    "DAMAGED_NO_PARITY": EXIT_VERIFY_DAMAGED,
    "DECOMPRESS_FAILED": EXIT_VERIFY_DAMAGED,
    "BACKUP_MISSING": EXIT_VERIFY_BACKUP_MISSING,
}


def _operation_extras(
    password: str | None, gpg_recipients: list | None, create_parity: bool
) -> str:
    extras = []
    if password or (gpg_recipients and len(gpg_recipients) > 0):
        extras.append("ENCRYPTION")
    if create_parity:
        extras.append("PARITY")
    if not extras:
        return ""
    if len(extras) == 1:
        return f" (with {extras[0]})"
    return f" (with {' and '.join(extras)})"


class Docker_Volume_Manager:
    def __init__(self, config: Config):
        self.config = config
        self.logger = self.config.get_logger()
        self.logger.debug(
            "Docker_Volume_Manager initialized with config: %s",
            {
                "OPERATION": getattr(self.config, "OPERATION", None),
                "INPUT_PATH": getattr(self.config, "INPUT_PATH", None),
                "OUTPUT_PATH": getattr(self.config, "OUTPUT_PATH", None),
                "COMPRESSION": getattr(self.config, "COMPRESSION", None),
                "PARITY": getattr(self.config, "PARITY", None),
            },
        )

    def run(self) -> bool:
        try:
            op = getattr(self.config, "OPERATION", "BACKUP").upper()
            self.logger.info("Running operation: %s", op)
            if op == "BACKUP":
                return self.backup()
            elif op == "RESTORE":
                return self.restore()
            elif op == "VERIFY":
                return self.verify()
            elif op == "COPY":
                return self.copy()
            else:
                self.logger.error("Invalid operation specified: %s", op)
                return False
        except OperationError:
            # Typed failures carry their own exit code — let the runner map them.
            raise
        except Exception:
            self.logger.exception("Unhandled exception in run()")
            return False

    def map_compression(self, level: str) -> str:
        # Both backup and restore are limited to the modern set:
        # NONE (raw tar), GZ (universal interop), ZSTD (default best-in-class).
        # Legacy .tar.bz2 / .tar.xz are no longer produced nor restored.
        # The codec registry is the single source of truth for the mapping.
        mapped = codecs.normalize_name(level)
        self.logger.debug("Mapped compression level %s -> %s", level, mapped)
        return mapped

    def backup(self) -> bool:
        try:
            create_parity = bool(getattr(self.config, "PARITY", 0) > 0)
            password = getattr(self.config, "ENCRYPTION_KEY", None) or None
            gpg_recipients = (
                list(getattr(self.config, "GPG_RECIPIENTS", []) or []) or None
            )

            self.logger.info(
                "Starting operation: BACKUP%s",
                _operation_extras(password, gpg_recipients, create_parity),
            )

            if create_parity:
                self.logger.info(
                    "Starting backup with parity (%s%%)", self.config.PARITY
                )
            else:
                self.logger.info("Starting backup without parity")

            if not os.path.isdir(self.config.INPUT_PATH):
                raise OperationError(
                    EXIT_BACKUP_INPUT_NOT_FOUND,
                    f"Input path does not exist or is not a directory: "
                    f"{self.config.INPUT_PATH}",
                )
            try:
                os.makedirs(self.config.OUTPUT_PATH, exist_ok=True)
            except Exception as e:
                raise OperationError(
                    EXIT_BACKUP_OUTPUT_NOT_WRITABLE,
                    f"Unable to ensure output directory {self.config.OUTPUT_PATH}: {e}",
                ) from e

            manager = BackupManager(
                vol_name=self.config.BACKUP_FILE_NAME,
                input_path=self.config.INPUT_PATH,
                output_path=self.config.OUTPUT_PATH,
                compression=self.map_compression(self.config.COMPRESSION),
                password=(
                    self.config.ENCRYPTION_KEY if self.config.ENCRYPTION_KEY else None
                ),
                gpg_recipients=gpg_recipients,
                create_parity=create_parity,
                parity_percentage=getattr(self.config, "PARITY", 0),
                sign_key=getattr(self.config, "SIGN_KEY", "") or None,
                sign_key_passphrase=(
                    getattr(self.config, "SIGN_KEY_PASSPHRASE", "") or None
                ),
            )

            backup_file = None
            try:
                if self.config.ENCRYPTION_KEY or gpg_recipients:
                    mode = "symmetric" if self.config.ENCRYPTION_KEY else "public-key"
                    self.logger.info("Encryption enabled for backup (%s).", mode)
                    backup_file = manager.compress_and_encrypt_pipeline()
                else:
                    self.logger.warning(
                        "Encryption disabled; producing unencrypted backup."
                    )
                    backup_file = manager.compress()
                    if create_parity:
                        self.logger.info(
                            "Creating parity files for unencrypted backup."
                        )
                        manager.create_parity_file(backup_file, self.config.PARITY)
            except FileNotFoundError as exc:
                raise OperationError(
                    EXIT_BACKUP_INPUT_NOT_FOUND, f"Backup input not found: {exc}"
                ) from exc
            except PermissionError as exc:
                raise OperationError(
                    EXIT_BACKUP_OUTPUT_NOT_WRITABLE,
                    f"Backup output not writable: {exc}",
                ) from exc
            except Exception as exc:
                # tar | compressor | gpg pipeline (or parity) failed. A gpg-stage
                # failure is an encryption problem; everything else is archive
                # production.
                if "gpg" in str(exc).lower():
                    raise OperationError(
                        EXIT_BACKUP_ENCRYPTION_FAILED,
                        f"Backup encryption failed: {exc}",
                    ) from exc
                raise OperationError(
                    EXIT_BACKUP_COMPRESSION_FAILED,
                    f"Backup archive production failed: {exc}",
                ) from exc

            # Detached signature is the final step: it covers whatever the
            # pipeline produced (encrypted-or-not), so a verifier with the
            # signer's public key can confirm authorship without decrypting.
            sig_file = None
            if self.config.SIGN_KEY:
                try:
                    sig_file = manager.sign_archive(backup_file)
                except Exception as exc:
                    raise OperationError(
                        EXIT_BACKUP_ENCRYPTION_FAILED,
                        f"Detached signature failed for {backup_file}: {exc}",
                    ) from exc

            self.logger.info(
                "Operation completed successfully: BACKUP%s - %s%s",
                _operation_extras(password, gpg_recipients, create_parity),
                backup_file,
                f" (+ signature: {sig_file})" if sig_file else "",
            )
            return True
        except OperationError:
            raise
        except Exception:
            self.logger.exception("Backup failed due to unexpected error")
            return False

    def copy(self) -> bool:
        """Copy operation: copy all files from INPUT_PATH into OUTPUT_PATH (mirror)."""
        try:
            # COPY has no encryption/parity/backup-name requirement
            self.logger.info("Starting operation: COPY")

            if not os.path.isdir(self.config.INPUT_PATH):
                raise OperationError(
                    EXIT_COPY_INPUT_NOT_FOUND,
                    f"Input path does not exist or is not a directory: "
                    f"{self.config.INPUT_PATH}",
                )
            try:
                # Ensure output exists; CopyManager validates writability + clearing.
                os.makedirs(self.config.OUTPUT_PATH, exist_ok=True)
            except Exception as e:
                raise OperationError(
                    EXIT_COPY_OUTPUT_NOT_WRITABLE,
                    f"Unable to ensure output directory {self.config.OUTPUT_PATH}: {e}",
                ) from e

            copier = CopyManager(
                input_path=self.config.INPUT_PATH,
                output_path=self.config.OUTPUT_PATH,
                overwrite=self.config.COPY_OVERWRITE,
            )

            # Perform copy; CopyManager asks/decides about overwriting destination.
            try:
                dest_dir = copier.copy()
            except (FileNotFoundError, ValueError) as exc:
                raise OperationError(
                    EXIT_COPY_INPUT_NOT_FOUND, f"Copy source problem: {exc}"
                ) from exc
            except PermissionError as exc:
                msg = str(exc).lower()
                if "not writable" in msg:
                    raise OperationError(
                        EXIT_COPY_OUTPUT_NOT_WRITABLE,
                        f"Copy destination not writable: {exc}",
                    ) from exc
                # Overwrite declined by policy / user.
                raise OperationError(
                    EXIT_COPY_OVERWRITE_REFUSED,
                    f"Copy destination not overwritten: {exc}",
                ) from exc

            self.logger.info("Operation completed successfully: COPY - %s", dest_dir)
            return True
        except OperationError:
            raise
        except Exception:
            self.logger.exception("Copy operation failed")
            return False

    def restore(self) -> bool:
        """
        Restore operation: locate a backup by BACKUP_FILE_NAME and restore into OUTPUT_PATH.
        Uses optional TIMESTAMP (from env/config) and ENCRYPTION_KEY from config.
        """
        try:
            self.logger.info("Starting operation: RESTORE")

            vol_name = self.config.BACKUP_FILE_NAME
            if not vol_name:
                self.logger.error("No BACKUP_FILE_NAME configured; cannot restore")
                return False

            input_path = self.config.INPUT_PATH
            output_path = self.config.OUTPUT_PATH
            timestamp = self.config.TIMESTAMP
            encryption_key = self.config.ENCRYPTION_KEY or None

            self.logger.debug(
                "Restore parameters: vol=%s input=%s output=%s timestamp=%s",
                vol_name,
                input_path,
                output_path,
                timestamp,
            )

            restored_path = restore_backup(
                vol_name=vol_name,
                input_path=input_path,
                output_path=output_path,
                timestamp=timestamp,
                encryption_key=encryption_key,
                overwrite=self.config.COPY_OVERWRITE,
            )

            self.logger.info(
                "Restore operation finished successfully -> %s", restored_path
            )
            return True
        except OperationError:
            raise
        except RestoreError as re:
            # RestoreError carries the specific exit code for its failure mode.
            from cli_shared import EXIT_OPERATION

            raise OperationError(
                getattr(re, "code", EXIT_OPERATION), f"Restore failed: {re}"
            ) from re
        except Exception:
            self.logger.exception("Restore operation failed unexpectedly")
            return False

    def verify(self) -> bool:
        try:
            self.logger.info(
                "Verifying backups in: %s", getattr(self.config, "OUTPUT_PATH", None)
            )
            backup_basename = getattr(self.config, "BACKUP_FILE_NAME", None)
            if not backup_basename:
                self.logger.error("No BACKUP_FILE_NAME configured")
                return False

            output_dir = getattr(self.config, "OUTPUT_PATH", ".")
            # Locate the actual archive with the SAME logic restore uses: backups
            # live at <output>/<name>/<timestamp>/<name>.<ext>, not directly under
            # <output>. (Previously verify treated <output>/<name> — a directory —
            # as the archive file, so it never found a real backup.)
            from operations.restore_files import (
                _find_archive_in_ts_dir,
                _find_backup_base,
                _select_timestamp_dir,
            )

            timestamp = getattr(self.config, "TIMESTAMP", None) or None
            try:
                base_dir = _find_backup_base(output_dir, backup_basename)
                ts_dir = _select_timestamp_dir(base_dir, timestamp)
                backup_path, _encrypted = _find_archive_in_ts_dir(ts_dir)
            except FileNotFoundError as exc:
                raise OperationError(
                    EXIT_VERIFY_BACKUP_MISSING, f"No backup found to verify: {exc}"
                ) from exc
            self.logger.info("Selected backup for verification: %s", backup_path)

            password = getattr(self.config, "ENCRYPTION_KEY", "") or ""
            # Auto-repair is the default: par2 exists to recover, so a recoverable
            # archive is fixed in place unless the caller passed --no-repair.
            repair = bool(getattr(self.config, "REPAIR", True))
            verifier = BackupVerifier(backup_path, password=password, repair=repair)
            report: dict[str, object] = verifier.verify_all()

            self.print_verification_summary(report)
            for key, value in report.items():
                self.logger.info("verify.%s = %s", key, value)

            outcome, healthy, message, level = self._classify_outcome(report, repair)
            getattr(self.logger, level, self.logger.info)(message)
            self.logger.info("verify.outcome = %s", outcome)
            self.logger.info(
                "Verification completed (outcome=%s, healthy=%s)", outcome, healthy
            )
            if not healthy:
                raise OperationError(_VERIFY_OUTCOME_EXIT.get(outcome, 4), message)
            return True
        except OperationError:
            raise
        except Exception:
            self.logger.exception("Backup verification failed unexpectedly")
            return False

    def _classify_outcome(
        self, report: dict[str, object], repair: bool
    ) -> tuple[str, bool, str, str]:
        """Reduce the verification report to a single named outcome.

        Returns ``(outcome, healthy, message, log_level)``. ``outcome`` is a
        stable code (see README) modelling every terminal state — intact,
        repaired, or one of the distinct failure modes — so the operator always
        knows exactly what happened and whether the backup file was modified.
        """
        if not report.get("backup_exists", False):
            return (
                "BACKUP_MISSING",
                False,
                "Backup file not found — nothing to verify.",
                "error",
            )

        if report.get("is_encrypted") and not report.get("can_decrypt", True):
            return (
                "DECRYPT_FAILED",
                False,
                "Backup is encrypted but cannot be decrypted with the provided "
                "key — integrity could not be checked.",
                "error",
            )

        parity_valid = report.get("parity_valid")
        repairable = report.get("parity_repairable")
        recovered = report.get("parity_recovered")
        can_decompress = report.get("can_decompress", True)

        if report.get("parity_files_exist", False):
            if parity_valid:
                if not can_decompress:
                    return (
                        "DECOMPRESS_FAILED",
                        False,
                        "Parity reports the archive intact, yet it does not "
                        "decompress — it may be truncated beyond par2's coverage.",
                        "error",
                    )
                return ("INTACT", True, "Backup is intact (parity valid).", "info")
            # Parity invalid → the archive is corrupt.
            if recovered:
                return (
                    "REPAIRED",
                    True,
                    "Backup was CORRUPT and has been REPAIRED in place with par2. "
                    "The backup file was MODIFIED and is valid again.",
                    "warning",
                )
            if repair and repairable and not recovered:
                return (
                    "REPAIR_FAILED",
                    False,
                    "Backup is CORRUPT and looked recoverable, but par2 repair "
                    "FAILED. The backup file is still damaged.",
                    "error",
                )
            if repairable:
                return (
                    "CORRUPT_REPAIRABLE",
                    False,
                    "Backup is CORRUPT but RECOVERABLE. Repair is disabled "
                    "(--no-repair) — re-run without it to fix the file in place.",
                    "error",
                )
            return (
                "CORRUPT_UNREPAIRABLE",
                False,
                "Backup is CORRUPT and CANNOT be repaired — the damage exceeds "
                "the PAR2 redundancy. This is data loss; restore from another copy.",
                "error",
            )

        # No parity files: integrity rests entirely on decompression.
        if not can_decompress:
            return (
                "DAMAGED_NO_PARITY",
                False,
                "Backup cannot be decompressed and has no PAR2 parity to recover "
                "from — it is unusable.",
                "error",
            )
        return (
            "INTACT_NO_PARITY",
            True,
            "Backup is readable (no parity files present to protect it).",
            "info",
        )

    def print_verification_summary(self, report: dict[str, object]) -> None:
        use_colors = bool(getattr(self.config, "USE_COLORS", True))
        OK = "\033[92m✔\033[0m" if use_colors else "OK"
        FAIL = "\033[91m✘\033[0m" if use_colors else "FAIL"
        WARN = "\033[93m⚠\033[0m" if use_colors else "WARN"

        def status(val: object) -> str:
            if val is True:
                return OK
            elif val is False:
                return FAIL
            else:
                return WARN

        try:
            print("\n====== Backup Verification Summary ======")
            print(f"Backup exists:         {status(report.get('backup_exists'))}")
            print(f"Is encrypted:          {status(report.get('is_encrypted'))}")
            print(f"Can decrypt:           {status(report.get('can_decrypt'))}")
            print(f"Can decompress:        {status(report.get('can_decompress'))}")
            if report.get("parity_files_exist"):
                print(f"Parity files exist:    {OK}")
                print(f"Parity valid:          {status(report.get('parity_valid'))}")
                if report.get("parity_valid") is False:
                    print(
                        f"Parity repairable:     {status(report.get('parity_repairable'))}"
                    )
                    # Only meaningful once a repair was actually attempted.
                    if report.get("parity_recovered") is not None:
                        print(
                            f"Parity recovered:      {status(report.get('parity_recovered'))}"
                        )
                    elif report.get("parity_repairable"):
                        print(
                            "                       → re-run without --no-repair to fix"
                        )
            else:
                print("Parity files exist:    (none)")
            print("========================================\n")
        except Exception:
            self.logger.exception("Failed to print verification summary")
