"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

import gzip
import io
import logging
import os
import subprocess
import tarfile

import gnupg

from operations import codecs

try:
    # PEP 784 — Python 3.14+ standard library zstd support.
    from compression import zstd as _zstd
except ImportError:
    _zstd = None

logger = logging.getLogger("dvm")


class BackupVerifier:
    """
    Audit a single backup archive.

    Verification is **read-only by default**: :py:meth:`verify_all` inspects
    the archive and classifies its PAR2 state without ever rewriting it. PAR2
    repair (which mutates the archive in place) only runs when the verifier is
    constructed with ``repair=True``.

    The full report dict returned by :py:meth:`verify_all` covers:

    - ``backup_exists`` (bool): the archive file is present and readable.
    - ``is_encrypted`` (bool): true iff the file ends in ``.gpg``.
    - ``can_decrypt`` (bool): for encrypted archives, gpg accepted the
      provided passphrase or the active keyring contains the recipient key.
    - ``can_decompress`` (bool): the (optionally decrypted) tar archive
      can be parsed by Python ``tarfile`` **as it currently sits on disk**.
      Re-checked after a successful PAR2 repair (``repair=True`` only).
    - ``parity_files_exist`` (bool): one or more ``.par2`` files were found
      next to the archive.
    - ``parity_valid`` (bool): par2 verify reports the archive intact (rc 0).
    - ``parity_repairable`` (bool|None): ``None`` when parity is valid or
      absent; ``True`` when the archive is damaged but par2 has enough
      redundancy to repair it; ``False`` when the damage exceeds par2's
      redundancy (unrecoverable).
    - ``parity_recovered`` (bool|None): ``None`` in read-only mode (no repair
      attempted). With ``repair=True`` and a repairable archive, whether
      ``par2 repair`` brought it back to a verifiable state.

    Parameters
    ----------
    backup_path:
        Absolute path of the archive to inspect.
    password:
        Optional passphrase. Required to decrypt symmetric ``.gpg`` archives
        when checking ``can_decrypt`` and ``can_decompress``.
    repair:
        When True, a damaged-but-repairable archive is repaired in place with
        ``par2 repair``. Defaults to False — verification never modifies the
        backup unless explicitly asked.
    """

    def __init__(self, backup_path: str, password: str = "", repair: bool = False):
        self.backup_path = backup_path
        self.password = password
        self.repair = repair
        try:
            self.gpg = gnupg.GPG()
            logger.debug("GPG instance initialized for verification")
        except Exception as e:
            logger.error("Failed to initialize GPG: %s", e)
            self.gpg = None

        # Find all .par2 files related to the backup and mark whether parity files exist.
        try:
            self.par2_files: list[str] = self._find_par2_files()
            self.verify_parity = len(self.par2_files) > 0
            logger.debug("Found par2 files: %s", self.par2_files)
        except Exception:
            logger.exception("Error while scanning for .par2 files")
            self.par2_files = []
            self.verify_parity = False

    def backup_exists(self) -> bool:
        exists = os.path.isfile(self.backup_path)
        logger.debug("backup_exists(%s) -> %s", self.backup_path, exists)
        return exists

    def is_encrypted(self) -> bool:
        encrypted = self.backup_path.endswith(".gpg")
        logger.debug("is_encrypted(%s) -> %s", self.backup_path, encrypted)
        return encrypted

    def can_decrypt(self) -> bool:
        if not self.is_encrypted():
            logger.debug("can_decrypt: file not encrypted -> True")
            return True
        if not self.gpg:
            logger.error("can_decrypt: GPG not initialized")
            return False
        try:
            logger.debug("Attempting to decrypt %s in-memory", self.backup_path)
            with open(self.backup_path, "rb") as f:
                result = self.gpg.decrypt_file(f, passphrase=self.password)
            ok = getattr(result, "ok", False)
            logger.debug(
                "GPG decrypt result.ok=%s status=%s",
                ok,
                getattr(result, "status", None),
            )
            return ok
        except Exception:
            logger.exception("Exception during decryption attempt")
            return False

    def can_decompress(self) -> bool:
        path = self.backup_path
        data = None
        # Per-codec decompression testers, keyed by logical name.
        mem_tests = {
            "gz": self._test_gzip,
            "zstd": self._test_zstd,
            "none": self._test_tar,
        }
        file_tests = {
            "gz": self._test_gzip_file,
            "zstd": self._test_zstd_file,
            "none": self._test_tar_file,
        }
        try:
            if path.endswith(".gpg"):
                if not self.gpg:
                    logger.error(
                        "can_decompress: GPG not initialized for encrypted file"
                    )
                    return False
                logger.debug("Decrypting %s into memory for decompression test", path)
                with open(path, "rb") as f:
                    result = self.gpg.decrypt_file(f, passphrase=self.password)
                    if not getattr(result, "ok", False) or not getattr(
                        result, "data", None
                    ):
                        logger.warning(
                            "Decryption failed or produced no data for %s", path
                        )
                        return False
                    data = result.data
                logger.debug(
                    "Decryption produced %d bytes", len(data) if data is not None else 0
                )

                codec = codecs.codec_for_path(path)
                if codec:
                    return mem_tests[codec.name](data)

                # Fallback: attempt to detect compression format automatically.
                logger.debug(
                    "Unknown compressed extension for %s, trying automatic detection",
                    path,
                )
                return (
                    self._test_gzip(data)
                    or self._test_zstd(data)
                    or self._test_tar(data)
                )
            else:
                codec = codecs.codec_for_path(path)
                if codec:
                    return file_tests[codec.name](path)

                # Fallback: try all supported archive formats.
                logger.debug(
                    "Unknown archive extension for %s, trying automatic detection", path
                )
                return (
                    self._test_gzip_file(path)
                    or self._test_zstd_file(path)
                    or self._test_tar_file(path)
                )
        except Exception:
            logger.exception("Error during can_decompress for %s", path)
            return False

    def _test_gzip(self, data: bytes) -> bool:
        try:
            with (
                gzip.GzipFile(fileobj=io.BytesIO(data)) as gz,
                tarfile.open(fileobj=gz) as tar,
            ):
                tar.getmembers()
            logger.debug("_test_gzip: success")
            return True
        except Exception:
            logger.debug("_test_gzip: failed")
            return False

    def _test_tar(self, data: bytes) -> bool:
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                tar.getmembers()
            logger.debug("_test_tar: success")
            return True
        except Exception:
            logger.debug("_test_tar: failed")
            return False

    def _test_zstd(self, data: bytes) -> bool:
        if _zstd is None:
            logger.debug("_test_zstd: compression.zstd unavailable")
            return False
        try:
            with (
                _zstd.ZstdFile(io.BytesIO(data), mode="rb") as zst,
                tarfile.open(fileobj=zst) as tar,
            ):
                tar.getmembers()
            logger.debug("_test_zstd: success")
            return True
        except Exception:
            logger.debug("_test_zstd: failed")
            return False

    def _test_gzip_file(self, path: str) -> bool:
        try:
            with (
                gzip.open(path, "rb") as gz,
                tarfile.open(fileobj=gz) as tar,
            ):
                tar.getmembers()
            logger.debug("_test_gzip_file(%s): success", path)
            return True
        except Exception:
            logger.debug("_test_gzip_file(%s): failed", path)
            return False

    def _test_zstd_file(self, path: str) -> bool:
        if _zstd is None:
            logger.debug("_test_zstd_file(%s): compression.zstd unavailable", path)
            return False
        try:
            with (
                _zstd.ZstdFile(path, mode="rb") as zst,
                tarfile.open(fileobj=zst) as tar,
            ):
                tar.getmembers()
            logger.debug("_test_zstd_file(%s): success", path)
            return True
        except Exception:
            logger.debug("_test_zstd_file(%s): failed", path)
            return False

    def _test_tar_file(self, path: str) -> bool:
        try:
            with tarfile.open(path, "r") as tar:
                tar.getmembers()
            logger.debug("_test_tar_file(%s): success", path)
            return True
        except Exception:
            logger.debug("_test_tar_file(%s): failed", path)
            return False

    def _find_par2_file(self):
        # Kept for compatibility, delegates to the newer implementation.
        files = self._find_par2_files()
        return files[0] if files else None

    def _find_par2_files(self):
        """
        Return a list of .par2 files related to self.backup_path.
        Search variants: with/without .gpg, main .par2 and block files (*.vol*.par2).
        """
        import glob

        base = self.backup_path
        candidates: list[str] = []
        # Base variants: include both the path as-is and without a trailing .gpg suffix.
        bases = [base]
        if base.endswith(".gpg"):
            bases.append(base[:-4])
        # Generate search patterns to cover expected naming variations.
        patterns = []
        for b in bases:
            patterns.append(b + ".par2")
            patterns.append(b + ".vol*.par2")
            root, _ = os.path.splitext(b)
            patterns.append(root + ".par2")
            patterns.append(root + ".vol*.par2")
        # Search filesystem and collect unique results.
        for p in patterns:
            matches = glob.glob(p)
            for m in matches:
                if os.path.isfile(m) and m not in candidates:
                    candidates.append(m)
        logger.debug("_find_par2_files patterns=%s -> found=%s", patterns, candidates)
        return candidates

    def parity_files_exist(self) -> bool:
        exist = len(getattr(self, "par2_files", [])) > 0
        logger.debug("parity_files_exist -> %s", exist)
        return exist

    def _main_par2_file(self) -> str | None:
        files = getattr(self, "par2_files", []) or []
        if not files:
            return None
        # Prefer the index .par2 file (one that is not a volume block, i.e. does
        # not contain ".vol"); par2 auto-loads the sibling .vol*.par2 blocks.
        return next((f for f in files if ".vol" not in os.path.basename(f)), files[0])

    def classify_parity(self) -> str:
        """Read-only PAR2 classification — never rewrites the archive.

        Runs ``par2 verify`` and maps its result to one of:

        - ``"valid"``: archive intact (par2 rc 0).
        - ``"repairable"``: archive damaged, but par2 has enough redundancy to
          repair it (par2 rc 1 / "Repair is possible").
        - ``"unrepairable"``: archive damaged beyond par2's redundancy (par2
          rc 2 / "Repair is not possible"), or par2 could not be run.
        """
        main = self._main_par2_file()
        if not main:
            logger.debug("classify_parity: no par2 files found")
            return "unrepairable"
        logger.info("Verifying parity using: %s", main)
        try:
            result = subprocess.run(
                ["par2", "verify", main],
                capture_output=True,
                check=False,
            )
        except Exception:
            logger.exception("Exception while running par2 verify")
            return "unrepairable"

        rc = result.returncode
        out = (result.stdout + result.stderr).decode(errors="ignore")
        logger.debug("par2 verify rc=%s output=%s", rc, out)
        if rc == 0:
            return "valid"
        # Damaged. Prefer the textual verdict (stable across par2 builds), and
        # fall back to the exit code: par2cmdline returns 1 (repair possible)
        # or 2 (repair not possible).
        low = out.lower()
        if "repair is not possible" in low:
            return "unrepairable"
        if "repair is possible" in low or "repair is required" in low:
            return "repairable"
        return "repairable" if rc == 1 else "unrepairable"

    def verify_parity_files(self) -> bool:
        """Backward-compatible boolean: True only when parity is fully valid."""
        return self.classify_parity() == "valid"

    def try_recover_with_parity(self) -> bool:
        files = getattr(self, "par2_files", []) or []
        if not files:
            logger.debug("try_recover_with_parity: no par2 files found")
            return False
        main = next((f for f in files if ".vol" not in os.path.basename(f)), files[0])
        logger.info("Attempting parity repair using: %s", main)
        try:
            result = subprocess.run(
                ["par2", "repair", main],
                capture_output=True,
                check=False,
            )
            logger.debug(
                "par2 repair rc=%s stdout=%s stderr=%s",
                result.returncode,
                result.stdout.decode(errors="ignore"),
                result.stderr.decode(errors="ignore"),
            )
            return result.returncode == 0
        except Exception:
            logger.exception("Exception while running par2 repair")
            return False

    def verify_all(self) -> dict[str, object]:
        """
        Run every applicable check and return a structured report.

        Read-only by default: PAR2 parity is *classified* (valid / repairable /
        unrepairable) without modifying the archive. Only when the verifier was
        built with ``repair=True`` and the archive is repairable does ``par2
        repair`` run in place; ``can_decompress`` is then re-evaluated so the
        report reflects the post-repair state.

        Returns
        -------
        dict
            Keys: ``backup_exists``, ``is_encrypted``, ``can_decrypt``,
            ``can_decompress``, ``parity_files_exist``, ``parity_valid``,
            ``parity_repairable``, ``parity_recovered``. See class docstring
            for semantics.
        """
        report = {}
        report["backup_exists"] = self.backup_exists()
        if not report["backup_exists"]:
            # Ensure all report keys exist even when the backup file is missing.
            report["is_encrypted"] = False
            report["can_decrypt"] = False
            report["can_decompress"] = False
            report["parity_files_exist"] = False
            report["parity_valid"] = False
            report["parity_repairable"] = None
            report["parity_recovered"] = None
            logger.warning("verify_all: backup does not exist: %s", self.backup_path)
            return report

        report["is_encrypted"] = self.is_encrypted()
        report["can_decrypt"] = self.can_decrypt()
        report["can_decompress"] = self.can_decompress()
        # Always include parity-related keys in the report.
        report["parity_files_exist"] = self.parity_files_exist()
        report["parity_repairable"] = None
        report["parity_recovered"] = None
        if report["parity_files_exist"]:
            state = self.classify_parity()  # read-only
            report["parity_valid"] = state == "valid"
            if not report["parity_valid"]:
                report["parity_repairable"] = state == "repairable"
                # Repair is strictly opt-in: never mutate the archive unless the
                # caller asked for it AND par2 has the redundancy to succeed.
                if self.repair and report["parity_repairable"]:
                    report["parity_recovered"] = self.try_recover_with_parity()
                    if report["parity_recovered"]:
                        # par2 rewrote the archive — re-check decompression so
                        # the report reflects the post-repair state.
                        report["can_decompress"] = self.can_decompress()
        else:
            report["parity_valid"] = False

        logger.info(
            "verify_all report: %s",
            {
                k: report[k]
                for k in [
                    "backup_exists",
                    "is_encrypted",
                    "can_decrypt",
                    "can_decompress",
                    "parity_files_exist",
                    "parity_valid",
                    "parity_repairable",
                ]
            },
        )
        return report
