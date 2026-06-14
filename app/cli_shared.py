"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Shared primitives for the CLI package: exit codes, the status file, rich
consoles, choice lists and small parsing/helper functions used by the command,
pivot, runner, volume and wizard modules. Kept dependency-free of the other CLI
modules so they can all import from here without import cycles.
"""

from __future__ import annotations

import os

import typer
from rich.console import Console

from config import VALID_COMPRESSION, VALID_LOG_LEVELS, VALID_LOG_OUTPUTS
from docker_client import DockerError

COMPRESSION_CHOICES = sorted(VALID_COMPRESSION)
LOG_LEVEL_CHOICES = sorted(VALID_LOG_LEVELS)

# Typed exit codes — the container's contract with orchestration / health checks.
#
# Layout: 0-3 are generic framework codes; operation failures use per-operation
# ranges where the tens digit identifies the operation and the units the reason
# (BACKUP 1x, RESTORE 2x, VERIFY 3x, COPY 4x, RENAME 5x, VOLUMES 6x). EXIT_OK
# means success (verify: intact OR auto-repaired). EXIT_OPERATION stays as a
# generic operation-failure fallback for anything not mapped to a specific code.
EXIT_OK = 0
EXIT_UNHANDLED = 1
EXIT_VALIDATION = 2
EXIT_CONFIG = 3
EXIT_OPERATION = 4  # generic operation failure (fallback)

# BACKUP 10-19
EXIT_BACKUP_INPUT_NOT_FOUND = 10
EXIT_BACKUP_OUTPUT_NOT_WRITABLE = 11
EXIT_BACKUP_COMPRESSION_FAILED = 12
EXIT_BACKUP_ENCRYPTION_FAILED = 13

# RESTORE 20-29
EXIT_RESTORE_BACKUP_NOT_FOUND = 20
EXIT_RESTORE_DECRYPT_FAILED = 21
EXIT_RESTORE_ARCHIVE_CORRUPT = 22
EXIT_RESTORE_PARITY_REPAIR_FAILED = 23
EXIT_RESTORE_UNSAFE_ARCHIVE = 24
EXIT_RESTORE_DESTINATION_ERROR = 25

# VERIFY 30-39
EXIT_VERIFY_UNREPAIRABLE = 30
EXIT_VERIFY_CORRUPT_REPAIRABLE = 31
EXIT_VERIFY_REPAIR_FAILED = 32
EXIT_VERIFY_DECRYPT_FAILED = 33
EXIT_VERIFY_DAMAGED = 34
EXIT_VERIFY_BACKUP_MISSING = 35

# COPY 40-49
EXIT_COPY_INPUT_NOT_FOUND = 40
EXIT_COPY_OUTPUT_NOT_WRITABLE = 41
EXIT_COPY_OVERWRITE_REFUSED = 42

# RENAME 50-59
EXIT_RENAME_SOURCE_MISSING = 50
EXIT_RENAME_TARGET_EXISTS = 51
EXIT_RENAME_VOLUME_IN_USE = 52
EXIT_RENAME_COPY_FAILED = 53

# VOLUMES 60-69
EXIT_VOLUME_NOT_FOUND = 60
EXIT_VOLUME_ALREADY_EXISTS = 61
EXIT_VOLUME_IN_USE = 62


class OperationError(Exception):
    """Raised by an operation to signal a specific, typed failure.

    Carries the exit code the CLI should terminate with (one of the
    ``EXIT_*`` constants) plus a human-readable message. The central runner
    catches this and maps ``code`` straight to ``typer.Exit`` so each distinct
    failure mode surfaces as its own process exit code.
    """

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


STATUS_FILE = "/dev/shm/app_status.txt"

_no_color = os.getenv("DVM_USE_COLORS", "1") != "1" or os.getenv("NO_COLOR") is not None
console = Console(no_color=_no_color)
err_console = Console(stderr=True, no_color=_no_color)


def _update_status(status: str) -> None:
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w") as f:
            f.write(status)
    except Exception:
        # Healthcheck file is best-effort; failing here must not mask the real op result.
        pass


def _normalise_recipients(value) -> list[str]:
    """Normalise typer's list[str] + env-var fallback into a trim-deduped list."""
    if value is None:
        return []
    # Typer delivers a list when repeated flags are used; delivers a raw string
    # when the value came from the GPG_RECIPIENTS env var — split it there too.
    items = value.split(",") if isinstance(value, str) else list(value)
    cleaned: list[str] = []
    seen = set()
    for raw in items:
        s = str(raw).strip()
        if s and s not in seen:
            cleaned.append(s)
            seen.add(s)
    return cleaned


def _parse_log_output(value: str) -> list[str]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    invalid = [p for p in parts if p not in VALID_LOG_OUTPUTS]
    if not parts or invalid:
        raise typer.BadParameter(
            f"--log-output entries must be one or more of {sorted(VALID_LOG_OUTPUTS)} "
            f"(comma-separated). Invalid: {invalid or parts!r}"
        )
    return parts


def _docker_fail(exc: DockerError) -> None:
    err_console.print(f"[bold red]Docker error:[/] {exc}")
    raise typer.Exit(EXIT_CONFIG)
