"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Backup manager: create tar archives with maximum metadata preservation (PAX
format), optionally encrypt and/or create parity files.

Notes about restoration:
- Archives store numeric uid/gid and, when resolvable, uname/gname. Restoring
  the original owners requires extracting as root with numeric ownership, e.g.:
    tar --same-owner --numeric-owner -xpf archive.tar.gz -C /target
  (zstd archives need ``--zstd``), or with Python's tarfile:
    with tarfile.open("archive.tar.zst") as t:
        t.extractall(path="/target", numeric_owner=True, filter="fully_trusted")
- Extracting as a non-root user sets owner/group to the extracting user
  (expected).
"""

import grp
import logging
import os
import pwd
import re
import subprocess
import tarfile
import tempfile
import time

from operations import codecs
from process_monitor import ProcessMonitor, read_proc_io
from progress import ProgressReporter, status

logger = logging.getLogger("dvm")


def _passphrase_read_fd(passphrase: str) -> int:
    """Stage ``passphrase`` in an OS pipe and return its read end.

    The fd is meant for ``gpg --passphrase-fd``: this keeps the secret out of
    the process argv (which is world-readable via ``ps`` / ``/proc/<pid>/cmdline``).
    The caller must pass the fd in ``Popen(pass_fds=...)`` and close its own copy
    afterwards. Passphrases are tiny, so the write never blocks on the pipe buffer.
    """
    read_fd, write_fd = os.pipe()
    os.write(write_fd, (passphrase + "\n").encode("utf-8"))
    os.close(write_fd)
    return read_fd


def _uname_from_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except Exception:
        return ""


def _gname_from_gid(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except Exception:
        return ""


class BackupManager:
    """
    Build archives from a source directory with optional encryption,
    parity protection, and detached signing.

    Two compression code paths exist:

    - :py:meth:`compress`: pure-Python ``tarfile`` (PAX format). Used when no
      encryption is requested. Preserves uid/gid/uname/gname/mtime/mode and
      attempts xattrs. Single-threaded.
    - :py:meth:`compress_and_encrypt_pipeline`: shell pipeline
      ``tar | <compressor> | gpg``. Used when encryption is requested.
      Multi-threaded compressor (pigz / zstd ``-T0``) and AES-NI-accelerated
      gpg.

    After producing the archive, :py:meth:`create_parity_file` may add PAR2
    files and :py:meth:`sign_archive` may add a detached signature.

    Parameters
    ----------
    vol_name:
        Logical name of the backup. The output goes to
        ``<output_path>/<vol_name>/<YYYYmmdd_HHMM>/<vol_name>.<ext>``.
    input_path:
        Directory whose **contents** are archived (the directory itself is
        not the top-level entry — it's flattened).
    output_path:
        Parent directory under which the timestamped subdirectory is created.
    compression:
        One of ``"none"``, ``"gz"``, ``"zstd"``. Other strings produce a
        ``.tar`` archive (no compression) for resilience.
    password:
        Symmetric GPG passphrase. Mutually exclusive with ``gpg_recipients``.
    gpg_recipients:
        Asymmetric GPG recipient identifiers (emails / fingerprints).
    create_parity, parity_percentage:
        Whether to compute PAR2 parity, and the redundancy percentage (1-100).
    sign_key, sign_key_passphrase:
        Optional detached signing — produces ``<archive>.sig`` after the
        pipeline finishes. The signing key must be reachable in the active
        ``GNUPGHOME`` (or the system keyring).
    """

    def __init__(
        self,
        vol_name: str,
        input_path: str,
        output_path: str,
        compression: str = "gz",
        password: str | None = None,
        gpg_recipients: list[str] | None = None,
        create_parity: bool = False,
        parity_percentage: int | None = None,
        sign_key: str | None = None,
        sign_key_passphrase: str | None = None,
    ):
        self.vol_name = vol_name
        self.input_path = os.path.abspath(input_path)
        self.output_path = output_path
        self.compression = compression  # expected values: gz, zstd, none
        self.password = password
        self.gpg_recipients = gpg_recipients or []
        self.sign_key = sign_key or None
        self.sign_key_passphrase = sign_key_passphrase or None
        self.create_parity = create_parity
        self.parity_percentage = (
            parity_percentage
            if parity_percentage and 1 <= parity_percentage <= 100
            else 10
        )

        logger.debug(
            "BackupManager initialized vol=%s input=%s output=%s compression=%s parity=%s",
            self.vol_name,
            self.input_path,
            self.output_path,
            self.compression,
            self.parity_percentage if self.create_parity else 0,
        )

    # -------------------------
    # Helpers for operation logging / extras
    # -------------------------
    def _operation_extras(self) -> str:
        parts = []
        if self.password or self.gpg_recipients:
            parts.append("ENCRYPTION")
        if self.create_parity:
            parts.append("PARITY")
        if not parts:
            return ""
        if len(parts) == 1:
            return f" (with {parts[0]})"
        return f" (with {parts[0]} and {parts[1]})"

    def _log_operation_start(self, op_name: str) -> None:
        extras = self._operation_extras()
        logger.info("Starting operation: %s%s", op_name, extras)

    def _log_operation_end(self, op_name: str, success: bool = True) -> None:
        extras = self._operation_extras()
        if success:
            logger.info("Operation completed successfully: %s%s", op_name, extras)
        else:
            logger.error("Operation FAILED: %s%s", op_name, extras)
        logger.info("-" * 50)

    # -------------------------
    # Helpers for output layout
    # -------------------------
    def _prepare_output_dirs(self) -> tuple[str, str]:
        """
        Ensure output_path exists and is writable, then create:
        output_path/<vol_name>/<YYYYmmdd_HHMM>[_NN]/
        Returns (backup_dir, timestamp_dir)
        """
        if not os.path.isdir(self.output_path):
            logger.error("Output path does not exist: %s", self.output_path)
            raise FileNotFoundError(f"Output path does not exist: {self.output_path}")

        # quick writable check
        try:
            testfile = os.path.join(self.output_path, f".write_test_{int(time.time())}")
            with open(testfile, "w") as f:
                f.write("ok")
            os.remove(testfile)
        except Exception as exc:
            logger.error("Output path is not writable: %s", self.output_path)
            raise PermissionError(
                f"Output path is not writable: {self.output_path}"
            ) from exc

        backup_dir = os.path.join(self.output_path, self.vol_name)
        try:
            os.makedirs(backup_dir, exist_ok=True)
            logger.debug("Ensured backup directory exists: %s", backup_dir)
        except Exception as e:
            logger.exception("Failed to create backup directory %s: %s", backup_dir, e)
            raise

        # use deterministic timestamp format for directory name
        base_timestamp = time.strftime("%Y%m%d_%H%M", time.localtime())
        candidate = base_timestamp
        counter = 1
        while os.path.exists(os.path.join(backup_dir, candidate)):
            candidate = f"{base_timestamp}_{counter:02d}"
            counter += 1

        ts_dir = os.path.join(backup_dir, candidate)
        try:
            os.makedirs(ts_dir, exist_ok=False)
            logger.info("Created timestamp directory for this run: %s", ts_dir)
        except Exception as e:
            logger.exception("Failed to create timestamp directory %s: %s", ts_dir, e)
            raise

        return backup_dir, ts_dir

    # -------------------------
    # Compression helpers (PAX + explicit meta)
    # -------------------------
    def _get_ext(self) -> str:
        return codecs.ext_for(self.compression)

    def _open_tar(self, path: str, mode: str) -> tarfile.TarFile:
        """
        Open tarfile using PAX format to maximize metadata preservation.

        For zstd archives we request the same compression level the subprocess
        pipeline uses (-19) — Python tarfile gained zstd support via PEP 784
        in 3.14 and accepts `compresslevel` 1-22 for `w:zst`.
        """
        kwargs = {"format": tarfile.PAX_FORMAT}
        if mode == "w:zst":
            kwargs["compresslevel"] = 19
        try:
            return tarfile.open(path, mode, **kwargs)
        except TypeError:
            # Older Python or tarfile build that doesn't accept the kwarg.
            kwargs.pop("compresslevel", None)
            return tarfile.open(path, mode, **kwargs)
        except Exception:
            return tarfile.open(path, mode)

    def _add_path_preserve(self, tar: tarfile.TarFile, path: str, arcname: str) -> None:
        """
        Add path to tar preserving uid/gid/uname/gname/mtime/mode as best-effort.
        Handles regular files, directories, symlinks and special files.
        """
        try:
            st = os.lstat(path)
            tarinfo = tarfile.TarInfo(name=arcname)
            tarinfo.mtime = int(getattr(st, "st_mtime", int(time.time())))
            tarinfo.mode = int(getattr(st, "st_mode", 0)) & 0o7777
            tarinfo.uid = int(getattr(st, "st_uid", 0))
            tarinfo.gid = int(getattr(st, "st_gid", 0))
            tarinfo.uname = _uname_from_uid(tarinfo.uid) or ""
            tarinfo.gname = _gname_from_gid(tarinfo.gid) or ""

            # Also add numeric fields to PAX headers for broader compatibility
            try:
                pax = {}
                pax["uid"] = str(tarinfo.uid)
                pax["gid"] = str(tarinfo.gid)
                if tarinfo.uname:
                    pax["uname"] = tarinfo.uname
                if tarinfo.gname:
                    pax["gname"] = tarinfo.gname
                tarinfo.pax_headers = pax
            except Exception:
                # non-fatal; continue
                pass

            if os.path.islink(path):
                tarinfo.type = tarfile.SYMTYPE
                tarinfo.linkname = os.readlink(path)
                tar.addfile(tarinfo)
            elif os.path.isdir(path):
                tarinfo.type = tarfile.DIRTYPE
                if not tarinfo.name.endswith("/"):
                    tarinfo.name = tarinfo.name + "/"
                tarinfo.size = 0
                tar.addfile(tarinfo)
            elif os.path.isfile(path):
                tarinfo.type = tarfile.REGTYPE
                tarinfo.size = (
                    st.st_size if getattr(st, "st_size", None) is not None else 0
                )
                with open(path, "rb") as f:
                    tar.addfile(tarinfo, fileobj=f)
            else:
                # other special files: fallback to tar.add
                tar.add(path, arcname=arcname, recursive=False)
        except Exception as e:
            logger.debug(
                "Detailed add failed for %s: %s. Falling back to tar.add()", path, e
            )
            try:
                tar.add(path, arcname=arcname)
            except Exception as e2:
                logger.exception("Fallback tar.add also failed for %s: %s", path, e2)
                raise

    # -------------------------
    # Compression with progress
    # -------------------------
    def compress(self) -> str:
        """
        Create an unencrypted tar archive of ``input_path``'s contents.

        Uses PAX format and explicit ``TarInfo`` population to maximise
        metadata retention (uid/gid/uname/gname/mtime/mode). For zstd, passes
        ``compresslevel=19`` to ``tarfile.open`` (Python 3.14+, PEP 784).

        Returns
        -------
        str
            Absolute path of the created archive.

        Raises
        ------
        FileNotFoundError
            If ``input_path`` does not exist or is not a directory.
        Exception
            Any tarfile / OS error during archive creation. Partial output
            files are best-effort cleaned up before re-raising.
        """
        self._log_operation_start("BACKUP")
        logger.info(
            "Starting compression: %s -> %s (compression=%s)",
            self.input_path,
            self.output_path,
            self.compression,
        )

        if not os.path.isdir(self.input_path):
            logger.error(
                "Input path does not exist or is not a directory: %s", self.input_path
            )
            self._log_operation_end("BACKUP", success=False)
            raise FileNotFoundError(f"Input path not found: {self.input_path}")

        _backup_dir, ts_dir = self._prepare_output_dirs()

        ext = self._get_ext()
        archive_name = f"{self.vol_name}{ext}"
        archive_path = os.path.join(ts_dir, archive_name)

        # Collect items to archive to provide progress metrics
        file_list = []
        dir_list = []
        total_bytes = 0
        for root, dirs, files in os.walk(self.input_path):
            for d in dirs:
                dir_list.append(os.path.join(root, d))
            for f in files:
                fp = os.path.join(root, f)
                file_list.append(fp)
                try:
                    total_bytes += os.lstat(fp).st_size
                except OSError:
                    pass
        total_files = len(file_list)
        logger.info(
            "Compressing %d files (%d bytes) into %s",
            total_files,
            total_bytes,
            archive_path,
        )

        try:
            # zstd: Python 3.14's tarfile supports "w:zst" directly (PEP 784).
            mode = codecs.write_mode(self.compression)
            tar = self._open_tar(archive_path, mode)
            try:
                for dpath in dir_list:
                    rel = os.path.relpath(dpath, start=self.input_path)
                    arcname = rel if rel != "." else ""
                    if arcname == "":
                        continue
                    if not arcname.endswith("/"):
                        arcname = arcname + "/"
                    self._add_path_preserve(tar, dpath, arcname=arcname)

                with ProgressReporter(
                    f"Compressing (0/{total_files} files)", total_bytes, unit="bytes"
                ) as pr:
                    done_files = 0
                    for filepath in file_list:
                        rel = os.path.relpath(filepath, start=self.input_path)
                        if rel == ".":
                            continue
                        try:
                            fsize = os.lstat(filepath).st_size
                        except OSError:
                            fsize = 0
                        self._add_path_preserve(tar, filepath, arcname=rel)
                        done_files += 1
                        pr.advance(fsize)
                        pr.update_description(
                            f"Compressing ({done_files}/{total_files} files)"
                        )
            finally:
                try:
                    tar.close()
                except Exception:
                    pass

            logger.info("Compression finished: %s", archive_path)
            logger.info(
                "To restore original owners, extract as root and use --same-owner / numeric_owner options if available."
            )
            self._log_operation_end("BACKUP", success=True)
            return archive_path
        except Exception as e:
            logger.exception("Compression failed for %s: %s", archive_path, e)
            try:
                if os.path.exists(archive_path):
                    os.remove(archive_path)
                    logger.debug("Removed partial archive: %s", archive_path)
            except Exception:
                logger.debug("Failed to remove partial archive: %s", archive_path)
            self._log_operation_end("BACKUP", success=False)
            raise

    # -------------------------
    # Encrypted pipeline (the only encryption path)
    # -------------------------
    def compress_and_encrypt_pipeline(self) -> str:
        """
        Encrypted-backup pipeline: ``tar | <compressor> | gpg``.

        Uses three Popen processes connected through OS pipes. Compressor is
        chosen by the codec registry (:mod:`operations.codecs`, multi-threaded
        when available).
        gpg runs in symmetric mode when ``self.password`` is set, or in
        public-key mode against ``self.gpg_recipients``.

        Returns
        -------
        str
            Path of the produced ``<archive>.gpg`` file.

        Raises
        ------
        FileNotFoundError
            ``input_path`` does not exist.
        ValueError
            Neither ``password`` nor ``gpg_recipients`` was provided.
        RuntimeError
            ``tar``, the compressor, or ``gpg`` exited non-zero. Partial
            output is best-effort cleaned before re-raising.
        """
        self._log_operation_start("BACKUP")
        logger.info("Starting pipeline compression + encryption (tar | comp | gpg)")

        if not os.path.isdir(self.input_path):
            logger.error("Input path does not exist: %s", self.input_path)
            self._log_operation_end("BACKUP", success=False)
            raise FileNotFoundError(f"Input path not found: {self.input_path}")

        _backup_dir, ts_dir = self._prepare_output_dirs()

        ext = self._get_ext()
        archive_name = f"{self.vol_name}{ext}.gpg"
        archive_path = os.path.join(ts_dir, archive_name)

        total_files = 0
        total_bytes = 0
        for root, _dirs, files in os.walk(self.input_path):
            total_files += len(files)
            for fname in files:
                try:
                    total_bytes += os.lstat(os.path.join(root, fname)).st_size
                except OSError:
                    pass
        logger.info(
            "Pipeline will process approximately %d files (%d bytes)",
            total_files,
            total_bytes,
        )

        # Build tar command with PAX format and archive contents ('.')
        tar_cmd = ["tar", "--format=pax", "-c", "-C", self.input_path, "."]
        comp_cmd = codecs.compressor_argv(self.compression) or []

        # The passphrase fd (symmetric mode) is created just before spawning gpg
        # so an early failure can't leak it; tracked here for cleanup.
        passphrase_fd: int | None = None
        if self.password:
            passphrase_fd = _passphrase_read_fd(self.password)
            gpg_cmd = [
                "gpg",
                "--batch",
                "--yes",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                str(passphrase_fd),
                "--symmetric",
                "-o",
                archive_path,
            ]
        elif self.gpg_recipients:
            # --batch --yes so a TTY-less run never blocks on a trust prompt;
            # trust-model always mirrors the throwaway-keyring design.
            gpg_cmd = [
                "gpg",
                "--batch",
                "--yes",
                "--trust-model",
                "always",
                "--encrypt",
                "-o",
                archive_path,
            ]
            for recipient in self.gpg_recipients:
                gpg_cmd += ["--recipient", recipient]
        else:
            logger.error(
                "Neither password nor gpg_recipients provided for pipeline encryption"
            )
            self._log_operation_end("BACKUP", success=False)
            raise ValueError(
                "Either password or gpg_recipients must be specified for encryption."
            )

        tar_proc = comp_proc = gpg_proc = None
        # Intermediate stderr goes to temp files, NOT PIPE: with PIPE we'd only
        # drain it after gpg.communicate(), so a chatty tar/compressor (e.g. many
        # "file changed as we read it" warnings on a live volume) could fill the
        # ~64 KB pipe buffer, block on write, and deadlock the whole pipeline.
        # Deliberately not context-managed: these outlive the `with` and are
        # closed in the finally block after the subprocesses have been waited on.
        tar_err = tempfile.TemporaryFile()  # noqa: SIM115
        comp_err = tempfile.TemporaryFile()  # noqa: SIM115
        try:
            logger.info("Starting tar process: %s", " ".join(tar_cmd))
            tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=tar_err)
            prev_proc = tar_proc

            if comp_cmd:
                logger.info("Starting compression process: %s", " ".join(comp_cmd))
                comp_proc = subprocess.Popen(
                    comp_cmd,
                    stdin=tar_proc.stdout,
                    stdout=subprocess.PIPE,
                    stderr=comp_err,
                )
                if tar_proc.stdout:
                    tar_proc.stdout.close()
                prev_proc = comp_proc

            logger.info("Starting gpg process")
            gpg_proc = subprocess.Popen(
                gpg_cmd,
                stdin=prev_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Inherit only the passphrase read-end (symmetric mode); harmless
                # empty tuple otherwise.
                pass_fds=(passphrase_fd,) if passphrase_fd is not None else (),
            )
            if prev_proc and prev_proc.stdout:
                prev_proc.stdout.close()
            # Parent no longer needs the passphrase fd once gpg holds its copy.
            if passphrase_fd is not None:
                os.close(passphrase_fd)
                passphrase_fd = None

            # Live progress + stall watchdog, both fed by /proc/<pid>/io: progress
            # tracks how many input bytes tar has read; the watchdog watches total
            # I/O across all three processes and kills them if it flatlines.
            pipeline_procs = [
                p for p in (tar_proc, comp_proc, gpg_proc) if p is not None
            ]

            def _progress_bytes():
                io = read_proc_io(tar_proc.pid)
                return io[0] if io else None  # tar rchar == input consumed

            _activity_seen = [0]

            def _activity_bytes():
                total = sum(
                    io[0] + io[1]
                    for p in pipeline_procs
                    if (io := read_proc_io(p.pid)) is not None
                )
                _activity_seen[0] = max(_activity_seen[0], total)
                return _activity_seen[0]

            def _kill_pipeline():
                for p in pipeline_procs:
                    try:
                        p.kill()
                    except Exception:
                        pass

            with ProcessMonitor(
                "Backup (compress + encrypt)",
                total_bytes,
                progress_fn=_progress_bytes,
                activity_fn=_activity_bytes,
                on_stall=_kill_pipeline,
            ) as monitor:
                _gpg_out, gpg_err = gpg_proc.communicate()
                gpg_rc = gpg_proc.returncode
                tar_rc = tar_proc.wait() if tar_proc else 0
                comp_rc = comp_proc.wait() if comp_proc else 0

            if monitor.stalled:
                raise RuntimeError(
                    f"Backup pipeline stalled (no I/O for {monitor.stall_timeout}s) "
                    "and was terminated."
                )

            # Intermediate stderr was buffered to temp files; read it now.
            def _log_err_file(handle, label: str) -> None:
                try:
                    handle.seek(0)
                    data = handle.read()
                    if data:
                        logger.debug(
                            "%s stderr: %s", label, data.decode(errors="ignore")
                        )
                except Exception:
                    logger.debug("Failed to read %s stderr", label)

            _log_err_file(tar_err, "tar")
            if comp_proc:
                _log_err_file(comp_err, "comp")

            try:
                if gpg_proc and gpg_proc.stderr:
                    if gpg_err:
                        logger.debug("gpg stderr: %s", gpg_err.decode(errors="ignore"))
                    gpg_proc.stderr.close()
                if gpg_proc and gpg_proc.stdout:
                    gpg_proc.stdout.close()
            except Exception:
                logger.debug("Failed to close gpg pipes")

            if tar_rc != 0:
                logger.error("tar command failed (rc=%s)", tar_rc)
                raise RuntimeError("tar command failed")
            if comp_cmd and comp_rc != 0:
                logger.error("compression command failed (rc=%s)", comp_rc)
                raise RuntimeError("compression command failed")
            if gpg_rc != 0:
                logger.error(
                    "gpg command failed (rc=%s) stderr=%s",
                    gpg_rc,
                    gpg_err.decode(errors="ignore") if gpg_err else "",
                )
                raise RuntimeError(
                    f"gpg command failed: {gpg_err.decode(errors='ignore') if gpg_err else ''}"
                )

            logger.info("Pipeline finished, archive created: %s", archive_path)
            logger.info(
                "To restore original owners, extract as root and use --same-owner / numeric_owner options if available."
            )
            if self.create_parity:
                logger.info("Generating parity files for %s", archive_path)
                self._create_parity(archive_path, self.parity_percentage)

            self._log_operation_end("BACKUP", success=True)
            return archive_path
        except Exception as e:
            try:
                if os.path.exists(archive_path):
                    os.remove(archive_path)
                    logger.debug(
                        "Removed incomplete pipeline archive: %s", archive_path
                    )
            except Exception:
                logger.debug(
                    "Failed to remove incomplete pipeline archive: %s", archive_path
                )
            logger.exception("compress_and_encrypt_pipeline failed: %s", e)
            self._log_operation_end("BACKUP", success=False)
            raise
        finally:
            # Close the passphrase fd if an early failure left it open.
            if passphrase_fd is not None:
                try:
                    os.close(passphrase_fd)
                except OSError:
                    pass
            for handle in (tar_err, comp_err):
                try:
                    handle.close()
                except Exception:
                    pass
            for proc in (tar_proc, comp_proc, gpg_proc):
                if proc is None:
                    continue
                try:
                    if getattr(proc, "stdout", None):
                        try:
                            proc.stdout.close()
                        except Exception:
                            pass
                    if getattr(proc, "stderr", None):
                        try:
                            proc.stderr.close()
                        except Exception:
                            pass
                except Exception:
                    pass

    # -------------------------
    # Parity with streaming logs (reduced verbosity)
    # -------------------------
    def create_parity_file(self, file_path: str, parity_percentage: int):
        return self._create_parity(file_path, parity_percentage)

    def _create_parity(self, file_path: str, parity_percentage: int):
        logger.info(
            "Creating parity files for %s (%%=%s)", file_path, parity_percentage
        )
        if not os.path.isfile(file_path):
            logger.error("File for parity not found: %s", file_path)
            raise FileNotFoundError(f"File for parity not found: {file_path}")

        # -n1 collapses all recovery blocks into a single .vol*.par2 file, so the
        # backup ends up with exactly two parity artefacts: the index (<name>.par2)
        # and one volume file. par2 has no embedded-parity mode, but this is the
        # most compact, portable layout that standard par2 tools can still verify.
        par2_file = file_path + ".par2"
        cmd = [
            "par2",
            "create",
            f"-r{parity_percentage}",
            "-n1",
            par2_file,
            file_path,
        ]
        proc = None
        # Match any "NN%" / "NN.NN%" pattern — par2 emits both "Processing: X%"
        # and "Constructing: X%" during different phases, and we want to feed both
        # into the progress reporter rather than spamming the log.
        pct_re = re.compile(r"([0-9]+(?:\.[0-9]+)?)%")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with ProgressReporter("Creating parity", 100) as pr:
                for raw_line in proc.stdout:
                    line = raw_line.strip()
                    m = pct_re.search(line)
                    if m:
                        try:
                            pr.set_progress(int(float(m.group(1))))
                        except Exception:
                            logger.info("par2: %s", line)
                    elif line:
                        logger.info("par2: %s", line)
            rc = proc.wait()
            if rc != 0:
                logger.error("par2 create failed (rc=%s)", rc)
                try:
                    if os.path.exists(par2_file):
                        os.remove(par2_file)
                except Exception:
                    logger.debug("Failed to remove partial par2 file: %s", par2_file)
                raise RuntimeError(f"par2 create failed (rc={rc})")
            logger.info("Parity files created for %s", file_path)
            return par2_file
        except Exception as e:
            logger.exception(
                "Unexpected error while creating parity for %s: %s", file_path, e
            )
            raise
        finally:
            try:
                if proc and getattr(proc, "stdout", None):
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
            except Exception:
                pass

    # -------------------------
    # Detached signing
    # -------------------------
    def sign_archive(self, archive_path: str) -> str | None:
        """
        Produce a detached GPG signature next to the archive. Returns the
        signature path (`<archive>.sig`) on success, or None when no signing
        key is configured.

        The active GNUPGHOME (system or temporary) must contain the private
        signing key. Use `gpg --verify <archive>.sig <archive>` to verify.
        """
        if not self.sign_key:
            return None
        if not os.path.isfile(archive_path):
            raise FileNotFoundError(f"Archive to sign not found: {archive_path}")

        sig_path = archive_path + ".sig"
        cmd = ["gpg", "--batch", "--yes"]
        sign_input: bytes | None = None
        if self.sign_key_passphrase:
            # Feed the passphrase via stdin (fd 0) with loopback pinentry, never
            # via argv (which is world-readable through ps / /proc/<pid>/cmdline).
            cmd += ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
            sign_input = (self.sign_key_passphrase + "\n").encode("utf-8")
        cmd += [
            "--local-user",
            self.sign_key,
            "--detach-sign",
            "--output",
            sig_path,
            archive_path,
        ]

        logger.info("Creating detached signature with key %s", self.sign_key)
        try:
            with status("Signing archive"):
                proc = subprocess.run(cmd, input=sign_input, capture_output=True)
        except Exception as exc:
            logger.exception("Signing subprocess failed to start: %s", exc)
            raise

        if proc.returncode != 0:
            stderr = (
                proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
            )
            logger.error("Signing failed (rc=%s): %s", proc.returncode, stderr.strip())
            try:
                if os.path.exists(sig_path):
                    os.remove(sig_path)
            except Exception:
                pass
            raise RuntimeError(f"GPG sign failed (rc={proc.returncode})")
        logger.info("Detached signature written: %s", sig_path)
        return sig_path

    # -------------------------
    # Misc utilities
    # -------------------------
    def get_directory_statistics(self, path: str | None = None):
        if path is None:
            path = self.input_path
        num_files = 0
        num_dirs = 0
        for _, dirs, files in os.walk(path):
            num_files += len(files)
            num_dirs += len(dirs)
        logger.debug(
            "Directory stats for %s: files=%d dirs=%d", path, num_files, num_dirs
        )
        return num_files, num_dirs
