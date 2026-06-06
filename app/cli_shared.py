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
from docker_client import DockerUnavailable

COMPRESSION_CHOICES = sorted(VALID_COMPRESSION)
LOG_LEVEL_CHOICES = sorted(VALID_LOG_LEVELS)

# Typed exit codes — the container's contract with orchestration / health checks.
EXIT_OK = 0
EXIT_UNHANDLED = 1
EXIT_VALIDATION = 2
EXIT_CONFIG = 3
EXIT_OPERATION = 4

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


def _docker_fail(exc: DockerUnavailable) -> None:
    err_console.print(f"[bold red]Docker unavailable:[/] {exc}")
    raise typer.Exit(EXIT_CONFIG)
