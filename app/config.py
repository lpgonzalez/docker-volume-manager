"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

"""
Typed, validated configuration object for Docker Volume Manager.

Two construction paths:
- `Config(...)`: explicit kwargs — used by the CLI with already-parsed values.
- `Config.from_env(**overrides)`: read from environment variables as a fallback
  for CI / embedded use. Explicit overrides win over env.

Once `setup_logger()` has been called once, any module can obtain the configured
logger via `logging.getLogger(LOGGER_NAME)` without importing this module — the
Python logging system keeps logger instances singleton by name.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

LOGGER_NAME = "dvm"

VALID_OPERATIONS = {"BACKUP", "RESTORE", "VERIFY", "COPY", "RENAME"}
VALID_COMPRESSION = {"NONE", "GZ", "ZSTD"}
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
VALID_LOG_OUTPUTS = {"console", "file", "json_file"}

_TRUTHY = {"1", "y", "yes", "true", "on"}
_FALSY = {"0", "n", "no", "false", "off"}


def _parse_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in _TRUTHY:
        return True
    if s in _FALSY:
        return False
    return default


class JsonFormatter(logging.Formatter):
    """Minimal JSON formatter — no external dependency on python-json-logger quirks."""

    def __init__(self, datefmt: Optional[str] = None):
        super().__init__(datefmt=datefmt)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class Config:
    """
    Validated runtime configuration for a single DVM operation.

    Instances are usually built two ways:

    - ``Config(OPERATION="BACKUP", BACKUP_FILE_NAME="foo", ...)``: explicit
      kwargs. The CLI uses this path with values already parsed by typer.
    - ``Config.from_env(**overrides)``: pull from environment variables; any
      explicit kwarg wins. Used when DVM is invoked via env-var-only contexts
      (CI, scheduled runs, helper containers spawned by the pivot).

    Validation runs in ``__post_init__`` and raises ``ValueError`` with a
    human-readable message if a field is malformed (unknown OPERATION,
    invalid COMPRESSION, PARITY out of range, missing BACKUP_FILE_NAME for
    operations that require it, conflicting encryption modes, etc.).

    Once a Config exists, ``setup_logger()`` configures the shared ``dvm``
    logger; the operations modules then obtain it via
    ``logging.getLogger("dvm")`` without re-importing this module.

    Attributes
    ----------
    OPERATION:
        BACKUP / RESTORE / VERIFY / COPY / RENAME (case-insensitive on input).
    BACKUP_FILE_NAME:
        Base name used to build the timestamped output directory and archive
        filename. Required for everything except COPY and RENAME.
    INPUT_PATH, OUTPUT_PATH:
        Container-side paths. Default to /app/input_dir and /app/output_dir;
        override via bind mounts or via the helper-pivot when volumes are used.
    COMPRESSION:
        NONE | GZ | ZSTD. ZSTD is the default and runs at level 19 with all
        cores. GZ uses pigz when available, falling back to gzip.
    PARITY:
        PAR2 redundancy percentage 0-100. 0 disables parity.
    ENCRYPTION_KEY:
        Symmetric GPG passphrase. Mutually exclusive with GPG_RECIPIENTS.
    GPG_RECIPIENTS:
        Asymmetric GPG recipient identifiers (emails / fingerprints).
        Mutually exclusive with ENCRYPTION_KEY. The CLI accepts paths to
        ``.asc`` files via ``--recipient-key-file`` and resolves them to
        fingerprints in a throwaway keyring before populating this field.
    SIGN_KEY, SIGN_KEY_PASSPHRASE:
        Signing key fingerprint and (optional) passphrase. Produces a
        detached ``.sig`` next to the archive.
    TIMESTAMP:
        Specific backup timestamp (``YYYYmmdd_HHMM[_NN]``) for restore.
        ``None`` selects the most recent backup automatically.
    COPY_OVERWRITE:
        ``True`` proceeds without prompting when the destination is non-empty.
    LOG_LEVEL, LOG_OUTPUT, LOGS_PATH:
        Logging knobs — level, list of sinks (subset of console/file/json_file)
        and the directory holding file/json logs.
    USE_COLORS:
        Toggle ANSI colours in the verification summary. Honours ``NO_COLOR``.
    """

    OPERATION: str = "BACKUP"
    BACKUP_FILE_NAME: str = ""
    INPUT_PATH: str = "/app/input_dir"
    OUTPUT_PATH: str = "/app/output_dir"
    COMPRESSION: str = "ZSTD"
    PARITY: int = 0
    ENCRYPTION_KEY: str = ""
    GPG_RECIPIENTS: List[str] = field(default_factory=list)
    SIGN_KEY: str = ""
    SIGN_KEY_PASSPHRASE: str = ""
    TIMESTAMP: Optional[str] = None
    COPY_OVERWRITE: bool = True
    LOG_LEVEL: str = "INFO"
    LOG_OUTPUT: List[str] = field(default_factory=lambda: ["console"])
    LOGS_PATH: Optional[str] = "/app/logs"
    USE_COLORS: bool = True

    def __post_init__(self) -> None:
        self.OPERATION = str(self.OPERATION).upper()
        if self.OPERATION not in VALID_OPERATIONS:
            raise ValueError(
                f"OPERATION must be one of {sorted(VALID_OPERATIONS)} (got {self.OPERATION!r})"
            )

        self.COMPRESSION = str(self.COMPRESSION).upper()
        if self.COMPRESSION not in VALID_COMPRESSION:
            raise ValueError(
                f"COMPRESSION must be one of {sorted(VALID_COMPRESSION)} (got {self.COMPRESSION!r})"
            )

        try:
            self.PARITY = int(self.PARITY)
        except Exception as exc:
            raise ValueError(
                f"PARITY must be an integer (got {self.PARITY!r})"
            ) from exc
        if not (0 <= self.PARITY <= 100):
            raise ValueError(f"PARITY must be between 0 and 100 (got {self.PARITY})")

        self.LOG_LEVEL = str(self.LOG_LEVEL).upper()
        if self.LOG_LEVEL not in VALID_LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(VALID_LOG_LEVELS)} (got {self.LOG_LEVEL!r})"
            )

        normalised: List[str] = []
        for raw in self.LOG_OUTPUT or []:
            s = str(raw).strip()
            if s in VALID_LOG_OUTPUTS and s not in normalised:
                normalised.append(s)
        self.LOG_OUTPUT = normalised or ["console"]

        if self.OPERATION not in {"COPY", "RENAME"} and not self.BACKUP_FILE_NAME:
            raise ValueError(
                f"BACKUP_FILE_NAME is required for operation {self.OPERATION}"
            )

        self.ENCRYPTION_KEY = self.ENCRYPTION_KEY or ""
        self.GPG_RECIPIENTS = [
            r.strip() for r in (self.GPG_RECIPIENTS or []) if r and str(r).strip()
        ]
        self.SIGN_KEY = (self.SIGN_KEY or "").strip()
        self.SIGN_KEY_PASSPHRASE = self.SIGN_KEY_PASSPHRASE or ""
        if self.ENCRYPTION_KEY and self.GPG_RECIPIENTS:
            raise ValueError(
                "ENCRYPTION_KEY (symmetric) and GPG_RECIPIENTS (asymmetric) are "
                "mutually exclusive — choose one encryption mode"
            )
        self.TIMESTAMP = self.TIMESTAMP or None
        self.COPY_OVERWRITE = bool(self.COPY_OVERWRITE)
        self.USE_COLORS = bool(self.USE_COLORS)

        if self.LOGS_PATH:
            try:
                os.makedirs(self.LOGS_PATH, exist_ok=True)
            except Exception:
                # Log dir unavailable — file / json_file handlers will be skipped silently.
                self.LOGS_PATH = None

    # ------------------------------------------------------------------
    # Alternative construction
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Build from environment variables. Any explicit kwarg wins over env."""

        def pick(name: str, default):
            if name in overrides and overrides[name] is not None:
                return overrides[name]
            return os.getenv(name, default)

        raw_outputs = pick("LOG_OUTPUT", "console")
        if isinstance(raw_outputs, str):
            log_output = [o.strip() for o in raw_outputs.split(",") if o.strip()]
        else:
            log_output = list(raw_outputs)

        raw_recipients = pick("GPG_RECIPIENTS", "")
        if isinstance(raw_recipients, str):
            recipients = [r.strip() for r in raw_recipients.split(",") if r.strip()]
        else:
            recipients = list(raw_recipients or [])

        use_colors = _parse_bool(pick("DVM_USE_COLORS", "1"), True)
        if os.getenv("NO_COLOR") is not None:
            use_colors = False

        return cls(
            OPERATION=pick("OPERATION", "BACKUP"),
            BACKUP_FILE_NAME=pick("BACKUP_FILE_NAME", ""),
            INPUT_PATH=pick("INPUT_PATH", "/app/input_dir"),
            OUTPUT_PATH=pick("OUTPUT_PATH", "/app/output_dir"),
            COMPRESSION=pick("COMPRESSION", "ZSTD"),
            PARITY=pick("PARITY", 0),
            ENCRYPTION_KEY=pick("ENCRYPTION_KEY", ""),
            GPG_RECIPIENTS=recipients,
            SIGN_KEY=pick("SIGN_KEY", ""),
            SIGN_KEY_PASSPHRASE=pick("SIGN_KEY_PASSPHRASE", ""),
            TIMESTAMP=pick("TIMESTAMP", None) or None,
            COPY_OVERWRITE=_parse_bool(pick("COPY_OVERWRITE", "Y"), True),
            LOG_LEVEL=pick("LOG_LEVEL", "INFO"),
            LOG_OUTPUT=log_output,
            LOGS_PATH=pick("LOGS_PATH", "/app/logs"),
            USE_COLORS=use_colors,
        )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def setup_logger(self) -> logging.Logger:
        """Configure the shared `dvm` logger. Safe to call multiple times (handlers reset)."""
        logger = logging.getLogger(LOGGER_NAME)
        level = getattr(logging, self.LOG_LEVEL, logging.INFO)
        logger.setLevel(level)
        logger.handlers = []
        logger.propagate = False

        text_fmt = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        json_fmt = JsonFormatter()

        if "console" in self.LOG_OUTPUT:
            try:
                from rich.logging import RichHandler

                ch = RichHandler(
                    rich_tracebacks=True,
                    show_path=False,
                    show_time=True,
                    markup=False,
                    log_time_format="[%X]",
                )
            except Exception:
                ch = logging.StreamHandler()
                ch.setFormatter(text_fmt)
            ch.setLevel(level)
            logger.addHandler(ch)

        if "file" in self.LOG_OUTPUT and self.LOGS_PATH:
            try:
                fh = logging.FileHandler(
                    os.path.join(self.LOGS_PATH, "docker_volume_manager.log")
                )
                fh.setLevel(level)
                fh.setFormatter(text_fmt)
                logger.addHandler(fh)
            except Exception:
                logger.warning(
                    "Cannot create file logger; continuing without file handler"
                )

        if "json_file" in self.LOG_OUTPUT and self.LOGS_PATH:
            try:
                jfh = logging.FileHandler(
                    os.path.join(self.LOGS_PATH, "docker_volume_manager.json")
                )
                jfh.setLevel(level)
                jfh.setFormatter(json_fmt)
                logger.addHandler(jfh)
            except Exception:
                logger.warning(
                    "Cannot create json file logger; continuing without json_file handler"
                )

        return logger

    def get_logger(self) -> logging.Logger:
        """Return the configured logger, setting it up lazily if needed."""
        logger = logging.getLogger(LOGGER_NAME)
        if not logger.handlers:
            return self.setup_logger()
        return logger

    def boot_info(self) -> None:
        log = self.get_logger()
        log.info("-= Starting DOCKER-VOLUME-MANAGER =-")
        log.info("\tLOG_LEVEL: %s", self.LOG_LEVEL)
        log.info("\tLOG_OUTPUT: %s", self.LOG_OUTPUT)
        log.info("\tOPERATION: %s", self.OPERATION)
        log.info("\tBACKUP_FILE_NAME: %s", self.BACKUP_FILE_NAME)
        log.info("\tCOMPRESSION: %s", self.COMPRESSION)
        log.debug("\tENCRYPTION_KEY present: %s", bool(self.ENCRYPTION_KEY))
        log.debug("\tLOGS_PATH: %s", self.LOGS_PATH)
        log.debug("\tCOPY_OVERWRITE: %s", self.COPY_OVERWRITE)
