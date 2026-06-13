"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Docker_Volume_Manager: orchestrates operations BACKUP, RESTORE, VERIFY, COPY
"""

import logging
import os

from config import Config
from operations import codecs
from operations.backup_files import BackupManager
from operations.copy_files import CopyManager
from operations.restore_files import RestoreError
from operations.restore_files import restore as restore_backup
from operations.verify_backup import BackupVerifier

logger = logging.getLogger("dvm")


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
                self.logger.error(
                    "Input path does not exist or is not a directory: %s",
                    self.config.INPUT_PATH,
                )
                return False
            try:
                os.makedirs(self.config.OUTPUT_PATH, exist_ok=True)
            except Exception as e:
                self.logger.error(
                    "Unable to ensure output directory %s: %s",
                    self.config.OUTPUT_PATH,
                    e,
                )
                return False

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
                    self.logger.info("Creating parity files for unencrypted backup.")
                    manager.create_parity_file(backup_file, self.config.PARITY)

            # Detached signature is the final step: it covers whatever the
            # pipeline produced (encrypted-or-not), so a verifier with the
            # signer's public key can confirm authorship without decrypting.
            sig_file = None
            if self.config.SIGN_KEY:
                try:
                    sig_file = manager.sign_archive(backup_file)
                except Exception as exc:
                    self.logger.exception(
                        "Detached signature failed for %s: %s", backup_file, exc
                    )
                    return False

            self.logger.info(
                "Operation completed successfully: BACKUP%s - %s%s",
                _operation_extras(password, gpg_recipients, create_parity),
                backup_file,
                f" (+ signature: {sig_file})" if sig_file else "",
            )
            return True
        except Exception:
            self.logger.exception("Backup failed due to unexpected error")
            return False

    def copy(self) -> bool:
        """Copy operation: copy all files from INPUT_PATH into OUTPUT_PATH (mirror)."""
        try:
            # COPY has no encryption/parity/backup-name requirement
            self.logger.info("Starting operation: COPY")

            if not os.path.isdir(self.config.INPUT_PATH):
                self.logger.error(
                    "Input path does not exist or is not a directory: %s",
                    self.config.INPUT_PATH,
                )
                return False
            try:
                # Ensure output exists; CopyManager will validate writability and handle clearing
                os.makedirs(self.config.OUTPUT_PATH, exist_ok=True)
            except Exception as e:
                self.logger.error(
                    "Unable to ensure output directory %s: %s",
                    self.config.OUTPUT_PATH,
                    e,
                )
                return False

            copier = CopyManager(
                input_path=self.config.INPUT_PATH,
                output_path=self.config.OUTPUT_PATH,
                overwrite=self.config.COPY_OVERWRITE,
            )

            # Perform copy; CopyManager will ask/decide about overwriting destination contents.
            dest_dir = copier.copy()

            self.logger.info("Operation completed successfully: COPY - %s", dest_dir)
            return True
        except PermissionError as pe:
            # user aborted or permission issues
            self.logger.error("Copy aborted: %s", pe)
            return False
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
        except RestoreError as re:
            self.logger.error("Restore failed: %s", re)
            return False
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
                self.logger.error("No backup found to verify: %s", exc)
                return False
            self.logger.info("Selected backup for verification: %s", backup_path)

            password = getattr(self.config, "ENCRYPTION_KEY", "") or ""
            verifier = BackupVerifier(backup_path, password=password)
            report: dict[str, object] = verifier.verify_all()

            self.print_verification_summary(report)
            for key, value in report.items():
                self.logger.info("verify.%s = %s", key, value)

            healthy = True
            if not report.get("backup_exists", False):
                self.logger.error("Backup file does not exist: %s", backup_path)
                healthy = False
            if report.get("is_encrypted") and not report.get("can_decrypt", True):
                self.logger.error(
                    "Backup is encrypted but cannot be decrypted with provided key."
                )
                healthy = False
            if not report.get("can_decompress", True):
                self.logger.error("Backup cannot be decompressed.")
                healthy = False
            if report.get("parity_files_exist", False):
                if not report.get("parity_valid", True):
                    if report.get("parity_recovered", False):
                        self.logger.warning("Parity invalid but recovery succeeded.")
                    else:
                        self.logger.error("Parity invalid and recovery failed.")
                        healthy = False
                else:
                    self.logger.info("Parity files present and valid.")
            else:
                self.logger.info("No parity files present for this backup.")

            self.logger.info("Verification completed (healthy=%s)", healthy)
            return healthy
        except Exception:
            self.logger.exception("Backup verification failed unexpectedly")
            return False

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
            if "parity_files_exist" in report:
                print(
                    f"Parity files exist:    {status(report.get('parity_files_exist'))}"
                )
                print(f"Parity valid:          {status(report.get('parity_valid'))}")
                if report.get("parity_valid") is False:
                    print(
                        f"Parity recovered:      {status(report.get('parity_recovered'))}"
                    )
            print("========================================\n")
        except Exception:
            self.logger.exception("Failed to print verification summary")
