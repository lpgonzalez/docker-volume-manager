"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Operation runners shared by the typer commands and the interactive wizard.

Each ``_run_*`` builds a validated :class:`~config.Config` and dispatches it
through :func:`_run_operation`, which owns the status-file writes and the typed
exit codes.
"""

from __future__ import annotations

import typer

from cli_shared import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_OPERATION,
    EXIT_UNHANDLED,
    EXIT_VALIDATION,
    OperationError,
    _update_status,
    err_console,
)
from config import Config
from pivot import _pivot_if_remote


def _build_config(**fields) -> Config:
    try:
        return Config(**fields)
    except ValueError as exc:
        err_console.print(f"[bold red]Invalid configuration:[/] {exc}")
        _update_status("unhealthy")
        raise typer.Exit(EXIT_VALIDATION) from None


def _run_operation(cfg: Config) -> None:
    op = cfg.OPERATION
    logger = cfg.setup_logger()
    _update_status("starting")
    logger.info("Starting operation: %s", op)

    try:
        from operations.docker_volume_manager import (
            Docker_Volume_Manager,
        )
    except Exception as exc:
        logger.exception("Failed to import operations module: %s", exc)
        _update_status("unhealthy")
        raise typer.Exit(EXIT_CONFIG) from None

    try:
        success = Docker_Volume_Manager(cfg).run()
    except OperationError as exc:
        # Typed failure: map straight to its specific exit code.
        logger.error("%s failed (exit %s): %s", op, exc.code, exc.message)
        _update_status("unhealthy")
        raise typer.Exit(exc.code) from None
    except KeyboardInterrupt:
        logger.warning("Operation aborted by user: %s", op)
        _update_status("unhealthy")
        raise typer.Exit(EXIT_VALIDATION) from None
    except Exception as exc:
        logger.exception("Unhandled error during %s: %s", op, exc)
        _update_status("unhealthy")
        raise typer.Exit(EXIT_UNHANDLED) from None

    if success:
        logger.info("Operation completed successfully: %s", op)
        _update_status("healthy")
        raise typer.Exit(EXIT_OK)

    # No typed error but the operation reported a falsey result — generic failure.
    logger.error("Operation reported failure: %s", op)
    _update_status("unhealthy")
    raise typer.Exit(EXIT_OPERATION)


# Each runner first pivots to a helper container if a volume flag is set
# (no-op otherwise), then builds a validated Config and dispatches it. The pivot
# env mirrors the Config fields as env vars so the helper re-parses them through
# the same command path — built here once instead of in every CLI command and
# wizard step.


def _run_backup(
    *,
    name: str,
    input_path: str,
    output_path: str,
    compression: str,
    parity: int,
    encryption_key: str | None,
    recipients: list[str] | None = None,
    sign_key: str | None = None,
    sign_key_passphrase: str | None = None,
    input_volume: str | None = None,
    output_volume: str | None = None,
    input_host: str | None = None,
    output_host: str | None = None,
    log_level: str,
    log_output: list[str],
) -> None:
    _pivot_if_remote(
        subcommand="backup",
        env={
            "BACKUP_FILE_NAME": name,
            "INPUT_PATH": input_path,
            "OUTPUT_PATH": output_path,
            "COMPRESSION": compression,
            "PARITY": parity,
            "ENCRYPTION_KEY": encryption_key,
            "GPG_RECIPIENTS": ",".join(recipients) if recipients else None,
            "SIGN_KEY": sign_key,
            "SIGN_KEY_PASSPHRASE": sign_key_passphrase,
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": ",".join(log_output),
        },
        input_volume=input_volume,
        output_volume=output_volume,
        input_host=input_host,
        output_host=output_host,
        input_mode="ro",
        output_mode="rw",
    )
    cfg = _build_config(
        OPERATION="BACKUP",
        BACKUP_FILE_NAME=name,
        INPUT_PATH=input_path,
        OUTPUT_PATH=output_path,
        COMPRESSION=compression,
        PARITY=parity,
        ENCRYPTION_KEY=encryption_key or "",
        GPG_RECIPIENTS=recipients or [],
        SIGN_KEY=sign_key or "",
        SIGN_KEY_PASSPHRASE=sign_key_passphrase or "",
        LOG_LEVEL=log_level,
        LOG_OUTPUT=log_output,
    )
    _run_operation(cfg)


def _run_restore(
    *,
    name: str,
    input_path: str,
    output_path: str,
    timestamp: str | None,
    encryption_key: str | None,
    overwrite: bool,
    input_volume: str | None = None,
    output_volume: str | None = None,
    input_host: str | None = None,
    output_host: str | None = None,
    log_level: str,
    log_output: list[str],
) -> None:
    _pivot_if_remote(
        subcommand="restore",
        env={
            "BACKUP_FILE_NAME": name,
            "INPUT_PATH": input_path,
            "OUTPUT_PATH": output_path,
            "TIMESTAMP": timestamp,
            "ENCRYPTION_KEY": encryption_key,
            "COPY_OVERWRITE": "Y" if overwrite else "N",
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": ",".join(log_output),
        },
        input_volume=input_volume,
        output_volume=output_volume,
        input_host=input_host,
        output_host=output_host,
        input_mode="rw",
        output_mode="rw",
    )
    cfg = _build_config(
        OPERATION="RESTORE",
        BACKUP_FILE_NAME=name,
        INPUT_PATH=input_path,
        OUTPUT_PATH=output_path,
        TIMESTAMP=timestamp,
        ENCRYPTION_KEY=encryption_key or "",
        COPY_OVERWRITE=overwrite,
        LOG_LEVEL=log_level,
        LOG_OUTPUT=log_output,
    )
    _run_operation(cfg)


def _run_verify(
    *,
    name: str,
    output_path: str,
    encryption_key: str | None,
    output_volume: str | None = None,
    output_host: str | None = None,
    timestamp: str | None = None,
    repair: bool = True,
    log_level: str,
    log_output: list[str],
) -> None:
    _pivot_if_remote(
        subcommand="verify",
        env={
            "BACKUP_FILE_NAME": name,
            "OUTPUT_PATH": output_path,
            "ENCRYPTION_KEY": encryption_key,
            "TIMESTAMP": timestamp,
            "REPAIR": "Y" if repair else "N",
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": ",".join(log_output),
        },
        output_volume=output_volume,
        output_host=output_host,
        # Read-only verify only needs ro; repair must write back to the volume.
        output_mode="rw" if repair else "ro",
    )
    cfg = _build_config(
        OPERATION="VERIFY",
        BACKUP_FILE_NAME=name,
        OUTPUT_PATH=output_path,
        ENCRYPTION_KEY=encryption_key or "",
        TIMESTAMP=timestamp,
        REPAIR=repair,
        LOG_LEVEL=log_level,
        LOG_OUTPUT=log_output,
    )
    _run_operation(cfg)


def _run_copy(
    *,
    input_path: str,
    output_path: str,
    overwrite: bool,
    input_volume: str | None = None,
    output_volume: str | None = None,
    input_host: str | None = None,
    output_host: str | None = None,
    log_level: str,
    log_output: list[str],
) -> None:
    _pivot_if_remote(
        subcommand="copy",
        env={
            "INPUT_PATH": input_path,
            "OUTPUT_PATH": output_path,
            "COPY_OVERWRITE": "Y" if overwrite else "N",
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": ",".join(log_output),
        },
        input_volume=input_volume,
        output_volume=output_volume,
        input_host=input_host,
        output_host=output_host,
        input_mode="ro",
        output_mode="rw",
    )
    cfg = _build_config(
        OPERATION="COPY",
        INPUT_PATH=input_path,
        OUTPUT_PATH=output_path,
        COPY_OVERWRITE=overwrite,
        LOG_LEVEL=log_level,
        LOG_OUTPUT=log_output,
    )
    _run_operation(cfg)
