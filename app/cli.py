"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

"""
Docker Volume Manager CLI — typer + rich.

Each subcommand builds a Config and runs one operation. Environment variables
are honoured transparently as a fallback (typer's `envvar=`); explicit CLI
flags always take precedence.

The `interactive` subcommand drives a wizard (requires `docker run -it`).
"""

import os
import sys
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.traceback import install as install_rich_tracebacks

from config import (
    VALID_COMPRESSION,
    VALID_LOG_LEVELS,
    VALID_LOG_OUTPUTS,
    Config,
)
from docker_client import DockerClient, DockerUnavailable, VolumeInfo, format_size
from gpg_keyring import KeyringError, setup_recipient_keyring, tear_down_keyring

COMPRESSION_CHOICES = sorted(VALID_COMPRESSION)
LOG_LEVEL_CHOICES = sorted(VALID_LOG_LEVELS)

EXIT_OK = 0
EXIT_UNHANDLED = 1
EXIT_VALIDATION = 2
EXIT_CONFIG = 3
EXIT_OPERATION = 4

STATUS_FILE = "/dev/shm/app_status.txt"

_no_color = (
    os.getenv("DVM_USE_COLORS", "1") != "1" or os.getenv("NO_COLOR") is not None
)
console = Console(no_color=_no_color)
err_console = Console(stderr=True, no_color=_no_color)

install_rich_tracebacks(console=err_console, show_locals=False, suppress=[typer])

app = typer.Typer(
    name="dvm",
    help="Docker Volume Manager — backup, restore, verify and copy Docker volume contents.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

volumes_app = typer.Typer(
    name="volumes",
    help="Inspect Docker volumes (requires docker.sock mount).",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(volumes_app)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _update_status(status: str) -> None:
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w") as f:
            f.write(status)
    except Exception:
        # Healthcheck file is best-effort; failing here must not mask the real op result.
        pass


def _normalise_recipients(value) -> List[str]:
    """Normalise typer's List[str] + env-var fallback into a trim-deduped list."""
    if value is None:
        return []
    # Typer delivers a list when repeated flags are used; delivers a raw string
    # when the value came from the GPG_RECIPIENTS env var — split it there too.
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    cleaned: List[str] = []
    seen = set()
    for raw in items:
        s = str(raw).strip()
        if s and s not in seen:
            cleaned.append(s)
            seen.add(s)
    return cleaned


def _parse_log_output(value: str) -> List[str]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    invalid = [p for p in parts if p not in VALID_LOG_OUTPUTS]
    if not parts or invalid:
        raise typer.BadParameter(
            f"--log-output entries must be one or more of {sorted(VALID_LOG_OUTPUTS)} "
            f"(comma-separated). Invalid: {invalid or parts!r}"
        )
    return parts


def _build_config(**fields) -> Config:
    try:
        return Config(**fields)
    except ValueError as exc:
        err_console.print(f"[bold red]Invalid configuration:[/] {exc}")
        _update_status("unhealthy")
        raise typer.Exit(EXIT_VALIDATION)


def _run_operation(cfg: Config) -> None:
    op = cfg.OPERATION
    logger = cfg.setup_logger()
    _update_status("starting")
    logger.info("Starting operation: %s", op)

    try:
        from operations.docker_volume_manager import Docker_Volume_Manager  # noqa: WPS433
    except Exception as exc:
        logger.exception("Failed to import operations module: %s", exc)
        _update_status("unhealthy")
        raise typer.Exit(EXIT_CONFIG)

    try:
        success = Docker_Volume_Manager(cfg).run()
    except KeyboardInterrupt:
        logger.warning("Operation aborted by user: %s", op)
        _update_status("unhealthy")
        raise typer.Exit(EXIT_VALIDATION)
    except Exception as exc:
        logger.exception("Unhandled error during %s: %s", op, exc)
        _update_status("unhealthy")
        raise typer.Exit(EXIT_UNHANDLED)

    if success:
        logger.info("Operation completed successfully: %s", op)
        _update_status("healthy")
        raise typer.Exit(EXIT_OK)

    logger.error("Operation reported failure: %s", op)
    _update_status("unhealthy")
    raise typer.Exit(EXIT_OPERATION)


# ---------------------------------------------------------------------------
# Helper-container pivot (step 3b)
# ---------------------------------------------------------------------------


def _pivot_if_volumes(
    subcommand: str,
    env: Dict[str, Any],
    *,
    input_volume: Optional[str],
    output_volume: Optional[str],
    input_mode: str = "ro",
    output_mode: str = "rw",
) -> None:
    """
    If a volume flag is set and we're not already inside a helper, spawn a
    helper container with the volume(s) mounted, stream its logs, and exit
    with its status code.

    No-op when no volume flag is present; the caller proceeds normally.
    """
    if not (input_volume or output_volume):
        return
    if os.environ.get("DVM_HELPER_MODE") == "1":
        err_console.print(
            "[bold red]--input-volume / --output-volume cannot be used "
            "inside a helper container.[/]"
        )
        raise typer.Exit(EXIT_VALIDATION)

    client = DockerClient()
    if not client.ping():
        err_console.print(
            "[bold red]Docker unavailable.[/] "
            "Volume flags require the Docker socket. "
            "Re-run with: [dim]-v /var/run/docker.sock:/var/run/docker.sock[/]"
        )
        raise typer.Exit(EXIT_CONFIG)

    for kind, vol in (("input", input_volume), ("output", output_volume)):
        if not vol:
            continue
        try:
            exists = client.volume_exists(vol)
        except DockerUnavailable as exc:
            _docker_fail(exc)
        if not exists:
            err_console.print(
                f"[bold red]{kind.capitalize()} volume {vol!r} does not exist.[/] "
                f"Create it first with `dvm volumes create {vol}`."
            )
            raise typer.Exit(EXIT_VALIDATION)

    volume_mounts: Dict[str, Dict[str, str]] = {}
    helper_env: Dict[str, Any] = {k: v for k, v in env.items() if v is not None}
    if input_volume:
        volume_mounts[input_volume] = {"bind": "/app/input_dir", "mode": input_mode}
        helper_env["INPUT_PATH"] = "/app/input_dir"
    if output_volume:
        volume_mounts[output_volume] = {"bind": "/app/output_dir", "mode": output_mode}
        helper_env["OUTPUT_PATH"] = "/app/output_dir"

    # Scrub volume flags so the helper doesn't loop trying to pivot again.
    helper_env.pop("INPUT_VOLUME", None)
    helper_env.pop("OUTPUT_VOLUME", None)
    helper_env["OPERATION"] = subcommand.upper()

    mount_desc = ", ".join(
        f"{vol}→{m['bind']} ({m['mode']})" for vol, m in volume_mounts.items()
    )
    console.print(
        Panel.fit(
            f"[bold cyan]Running in helper container[/]\n"
            f"[dim]image:    [/]{client.self_image()}\n"
            f"[dim]mounts:   [/]{mount_desc}\n"
            f"[dim]operation:[/]{subcommand.upper()}",
            border_style="cyan",
        )
    )

    try:
        exit_code = client.run_helper_streaming(
            command=["python", "main.py", subcommand],
            volume_mounts=volume_mounts,
            env=helper_env,
            inherit_bind_mounts=True,
            tty=False,
        )
    except DockerUnavailable as exc:
        err_console.print(f"[bold red]Helper container error:[/] {exc}")
        raise typer.Exit(EXIT_CONFIG)

    raise typer.Exit(exit_code)


# ---------------------------------------------------------------------------
# Operation runners (shared by typer commands and interactive wizard)
# ---------------------------------------------------------------------------


def _run_backup(
    *,
    name: str,
    input_path: str,
    output_path: str,
    compression: str,
    parity: int,
    encryption_key: Optional[str],
    recipients: Optional[List[str]] = None,
    sign_key: Optional[str] = None,
    sign_key_passphrase: Optional[str] = None,
    log_level: str,
    log_output: List[str],
) -> None:
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
    timestamp: Optional[str],
    encryption_key: Optional[str],
    overwrite: bool,
    log_level: str,
    log_output: List[str],
) -> None:
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
    encryption_key: Optional[str],
    log_level: str,
    log_output: List[str],
) -> None:
    cfg = _build_config(
        OPERATION="VERIFY",
        BACKUP_FILE_NAME=name,
        OUTPUT_PATH=output_path,
        ENCRYPTION_KEY=encryption_key or "",
        LOG_LEVEL=log_level,
        LOG_OUTPUT=log_output,
    )
    _run_operation(cfg)


def _run_copy(
    *,
    input_path: str,
    output_path: str,
    overwrite: bool,
    log_level: str,
    log_output: List[str],
) -> None:
    cfg = _build_config(
        OPERATION="COPY",
        INPUT_PATH=input_path,
        OUTPUT_PATH=output_path,
        COPY_OVERWRITE=overwrite,
        LOG_LEVEL=log_level,
        LOG_OUTPUT=log_output,
    )
    _run_operation(cfg)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@app.command("backup")
def backup(
    name: str = typer.Option(
        ..., "--name", "-n", envvar="BACKUP_FILE_NAME", help="Backup base name."
    ),
    input_path: str = typer.Option(
        "/app/input_dir",
        "--input",
        "-i",
        envvar="INPUT_PATH",
        help="Source directory (bind mount or volume mountpoint).",
    ),
    input_volume: Optional[str] = typer.Option(
        None,
        "--input-volume",
        envvar="INPUT_VOLUME",
        help="Read source from a Docker volume (pivots to a helper container).",
    ),
    output_path: str = typer.Option(
        "/app/output_dir",
        "--output",
        "-o",
        envvar="OUTPUT_PATH",
        help="Destination directory for the resulting archive(s).",
    ),
    output_volume: Optional[str] = typer.Option(
        None,
        "--output-volume",
        envvar="OUTPUT_VOLUME",
        help="Write archive to a Docker volume (pivots to a helper container).",
    ),
    compression: str = typer.Option(
        "ZSTD",
        "--compression",
        "-c",
        envvar="COMPRESSION",
        case_sensitive=False,
        help="Compression algorithm: NONE | GZ | ZSTD. ZSTD (default) uses "
             "level 19 with all cores. GZ is for universal interop. Legacy "
             "tar.bz2 / tar.xz archives still restore but cannot be created.",
    ),
    parity: int = typer.Option(
        0,
        "--parity",
        "-p",
        envvar="PARITY",
        min=0,
        max=100,
        help="PAR2 parity percentage (0 disables parity).",
    ),
    encryption_key: Optional[str] = typer.Option(
        None,
        "--encryption-key",
        "-k",
        envvar="ENCRYPTION_KEY",
        help="GPG symmetric passphrase. Mutually exclusive with --recipient.",
    ),
    recipients: Optional[List[str]] = typer.Option(
        None,
        "--recipient",
        "-r",
        envvar="GPG_RECIPIENTS",
        help="GPG public-key recipient (repeatable). Looked up in the system "
             "keyring — mount it via `-v ~/.gnupg:/root/.gnupg:ro`.",
    ),
    recipient_files: Optional[List[str]] = typer.Option(
        None,
        "--recipient-key-file",
        "-K",
        help="Path to a GPG public-key file (.asc) to use as recipient (repeatable). "
             "Imported into a throwaway keyring at runtime — no keyring mount needed. "
             "Mutually exclusive with --recipient and --encryption-key.",
    ),
    sign_key: Optional[str] = typer.Option(
        None,
        "--sign-key",
        envvar="SIGN_KEY",
        help="Fingerprint or user-id of a GPG private key to produce a detached "
             "signature alongside the archive (`<archive>.sig`). Requires the key "
             "to be present in the active keyring (mount ~/.gnupg).",
    ),
    sign_key_passphrase: Optional[str] = typer.Option(
        None,
        "--sign-key-passphrase",
        envvar="SIGN_KEY_PASSPHRASE",
        help="Passphrase for the signing key when it is protected.",
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", "-l", envvar="LOG_LEVEL", case_sensitive=False
    ),
    log_output: str = typer.Option(
        "console",
        "--log-output",
        envvar="LOG_OUTPUT",
        help="Comma-separated: console, file, json_file.",
    ),
) -> None:
    """Create a compressed (and optionally encrypted / parity-protected) backup."""
    # Typer's envvar for List[str] doesn't split — do it ourselves if it came from env.
    recipient_list = _normalise_recipients(recipients)
    file_list = list(recipient_files) if recipient_files else []

    # Mutual exclusion of the three encryption modes.
    encryption_modes = sum(
        bool(x) for x in (encryption_key, recipient_list, file_list)
    )
    if encryption_modes > 1:
        err_console.print(
            "[bold red]--encryption-key, --recipient and --recipient-key-file "
            "are mutually exclusive — pick one encryption mode.[/]"
        )
        raise typer.Exit(EXIT_VALIDATION)

    keyring_homedir: Optional[str] = None
    if file_list:
        if input_volume or output_volume:
            err_console.print(
                "[bold red]--recipient-key-file is not yet supported with volume "
                "flags.[/] Mount your keyring (`-v ~/.gnupg:/root/.gnupg:ro`) and "
                "use --recipient instead."
            )
            raise typer.Exit(EXIT_VALIDATION)
        try:
            keyring_homedir, fingerprints = setup_recipient_keyring(file_list)
        except KeyringError as exc:
            err_console.print(f"[bold red]Recipient keyring setup failed:[/] {exc}")
            raise typer.Exit(EXIT_CONFIG)
        os.environ["GNUPGHOME"] = keyring_homedir
        recipient_list = fingerprints
        console.print(
            f"[dim]Imported {len(file_list)} key file(s) → "
            f"{len(fingerprints)} fingerprint(s) into ephemeral keyring.[/]"
        )

    try:
        _pivot_if_volumes(
            subcommand="backup",
            env={
                "BACKUP_FILE_NAME": name,
                "INPUT_PATH": input_path,
                "OUTPUT_PATH": output_path,
                "COMPRESSION": compression,
                "PARITY": parity,
                "ENCRYPTION_KEY": encryption_key,
                "GPG_RECIPIENTS": ",".join(recipient_list) if recipient_list else None,
                "SIGN_KEY": sign_key,
                "SIGN_KEY_PASSPHRASE": sign_key_passphrase,
                "LOG_LEVEL": log_level,
                "LOG_OUTPUT": log_output,
            },
            input_volume=input_volume,
            output_volume=output_volume,
            input_mode="ro",
            output_mode="rw",
        )
        _run_backup(
            name=name,
            input_path=input_path,
            output_path=output_path,
            compression=compression,
            parity=parity,
            encryption_key=encryption_key,
            recipients=recipient_list,
            sign_key=sign_key,
            sign_key_passphrase=sign_key_passphrase,
            log_level=log_level,
            log_output=_parse_log_output(log_output),
        )
    finally:
        if keyring_homedir:
            tear_down_keyring(keyring_homedir)
            os.environ.pop("GNUPGHOME", None)


@app.command("restore")
def restore(
    name: str = typer.Option(
        ..., "--name", "-n", envvar="BACKUP_FILE_NAME", help="Backup base name to restore."
    ),
    input_path: str = typer.Option(
        "/app/input_dir",
        "--input",
        "-i",
        envvar="INPUT_PATH",
        help="Directory containing the timestamped backup subdirectories.",
    ),
    input_volume: Optional[str] = typer.Option(
        None,
        "--input-volume",
        envvar="INPUT_VOLUME",
        help="Read backup from a Docker volume (pivots to a helper; mounted rw for par2 repair).",
    ),
    output_path: str = typer.Option(
        "/app/output_dir",
        "--output",
        "-o",
        envvar="OUTPUT_PATH",
        help="Target directory for the restored content.",
    ),
    output_volume: Optional[str] = typer.Option(
        None,
        "--output-volume",
        envvar="OUTPUT_VOLUME",
        help="Restore content into a Docker volume (pivots to a helper container).",
    ),
    timestamp: Optional[str] = typer.Option(
        None,
        "--timestamp",
        "-t",
        envvar="TIMESTAMP",
        help="Specific backup timestamp (YYYYmmdd_HHMM[_NN]). Latest is used if omitted.",
    ),
    encryption_key: Optional[str] = typer.Option(
        None, "--encryption-key", "-k", envvar="ENCRYPTION_KEY"
    ),
    overwrite: bool = typer.Option(
        True,
        "--overwrite/--no-overwrite",
        envvar="COPY_OVERWRITE",
        help="Overwrite non-empty destination without prompting.",
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", "-l", envvar="LOG_LEVEL", case_sensitive=False
    ),
    log_output: str = typer.Option("console", "--log-output", envvar="LOG_OUTPUT"),
) -> None:
    """Restore an existing backup into OUTPUT_PATH."""
    _pivot_if_volumes(
        subcommand="restore",
        env={
            "BACKUP_FILE_NAME": name,
            "INPUT_PATH": input_path,
            "OUTPUT_PATH": output_path,
            "TIMESTAMP": timestamp,
            "ENCRYPTION_KEY": encryption_key,
            "COPY_OVERWRITE": "Y" if overwrite else "N",
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": log_output,
        },
        input_volume=input_volume,
        output_volume=output_volume,
        input_mode="rw",
        output_mode="rw",
    )
    _run_restore(
        name=name,
        input_path=input_path,
        output_path=output_path,
        timestamp=timestamp,
        encryption_key=encryption_key,
        overwrite=overwrite,
        log_level=log_level,
        log_output=_parse_log_output(log_output),
    )


@app.command("verify")
def verify(
    name: str = typer.Option(
        ..., "--name", "-n", envvar="BACKUP_FILE_NAME", help="Backup base name to verify."
    ),
    output_path: str = typer.Option(
        "/app/output_dir",
        "--output",
        "-o",
        envvar="OUTPUT_PATH",
        help="Directory containing the backup to check.",
    ),
    output_volume: Optional[str] = typer.Option(
        None,
        "--output-volume",
        envvar="OUTPUT_VOLUME",
        help="Verify a backup stored in a Docker volume (pivots to a helper; "
             "mounted rw for par2 repair).",
    ),
    encryption_key: Optional[str] = typer.Option(
        None, "--encryption-key", "-k", envvar="ENCRYPTION_KEY"
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", "-l", envvar="LOG_LEVEL", case_sensitive=False
    ),
    log_output: str = typer.Option("console", "--log-output", envvar="LOG_OUTPUT"),
) -> None:
    """Verify a backup: existence, decryption, decompression and PAR2 parity."""
    _pivot_if_volumes(
        subcommand="verify",
        env={
            "BACKUP_FILE_NAME": name,
            "OUTPUT_PATH": output_path,
            "ENCRYPTION_KEY": encryption_key,
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": log_output,
        },
        input_volume=None,
        output_volume=output_volume,
        output_mode="rw",
    )
    _run_verify(
        name=name,
        output_path=output_path,
        encryption_key=encryption_key,
        log_level=log_level,
        log_output=_parse_log_output(log_output),
    )


@app.command("copy")
def copy(
    input_path: str = typer.Option(
        "/app/input_dir", "--input", "-i", envvar="INPUT_PATH"
    ),
    input_volume: Optional[str] = typer.Option(
        None,
        "--input-volume",
        envvar="INPUT_VOLUME",
        help="Source is a Docker volume (pivots to a helper container).",
    ),
    output_path: str = typer.Option(
        "/app/output_dir", "--output", "-o", envvar="OUTPUT_PATH"
    ),
    output_volume: Optional[str] = typer.Option(
        None,
        "--output-volume",
        envvar="OUTPUT_VOLUME",
        help="Destination is a Docker volume (pivots to a helper container).",
    ),
    overwrite: bool = typer.Option(
        True,
        "--overwrite/--no-overwrite",
        envvar="COPY_OVERWRITE",
        help="Overwrite destination contents without prompting.",
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", "-l", envvar="LOG_LEVEL", case_sensitive=False
    ),
    log_output: str = typer.Option("console", "--log-output", envvar="LOG_OUTPUT"),
) -> None:
    """Mirror INPUT_PATH into OUTPUT_PATH (no compression or encryption)."""
    _pivot_if_volumes(
        subcommand="copy",
        env={
            "INPUT_PATH": input_path,
            "OUTPUT_PATH": output_path,
            "COPY_OVERWRITE": "Y" if overwrite else "N",
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": log_output,
        },
        input_volume=input_volume,
        output_volume=output_volume,
        input_mode="ro",
        output_mode="rw",
    )
    _run_copy(
        input_path=input_path,
        output_path=output_path,
        overwrite=overwrite,
        log_level=log_level,
        log_output=_parse_log_output(log_output),
    )


# ---------------------------------------------------------------------------
# Rename (Docker volume)
# ---------------------------------------------------------------------------


@app.command("rename")
def rename(
    source: str = typer.Argument(..., help="Existing Docker volume name."),
    target: str = typer.Argument(..., help="New volume name (must not exist)."),
    keep_source: bool = typer.Option(
        False,
        "--keep-source",
        help="Don't delete the source volume after a successful rename.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Rename even if the source volume is in use by a container.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (for scripts)."
    ),
) -> None:
    """Rename a Docker volume: create target, copy with verify, delete source."""
    from operations.rename_volume import RenameError, rename_volume  # noqa: WPS433

    client = DockerClient()
    if not client.ping():
        err_console.print(
            "[bold red]Docker unavailable.[/] "
            "Rename requires the Docker socket. "
            "Re-run with: [dim]-v /var/run/docker.sock:/var/run/docker.sock[/]"
        )
        raise typer.Exit(EXIT_CONFIG)

    try:
        if source == target:
            err_console.print("[bold red]Source and target are identical.[/]")
            raise typer.Exit(EXIT_VALIDATION)
        if not client.volume_exists(source):
            err_console.print(
                f"[bold red]Source volume {source!r} does not exist.[/]"
            )
            raise typer.Exit(EXIT_VALIDATION)
        if client.volume_exists(target):
            err_console.print(
                f"[bold red]Target volume {target!r} already exists.[/]"
            )
            raise typer.Exit(EXIT_VALIDATION)
        with console.status(
            f"[bold blue]Inspecting {source}...", spinner="dots"
        ):
            source_info = client.inspect_volume(source, with_size=True)
    except DockerUnavailable as exc:
        _docker_fail(exc)

    users = (
        ", ".join(source_info.containers)
        if source_info.containers
        else "[yellow]— orphan —[/]"
    )
    console.print(
        Panel.fit(
            f"[bold cyan]Rename Docker volume[/]\n"
            f"[dim]source:       [/]{source}\n"
            f"[dim]size:         [/]{format_size(source_info.size_bytes)}\n"
            f"[dim]used by:      [/]{users}\n"
            f"[dim]target:       [/]{target}\n"
            f"[dim]keep source:  [/]{'yes' if keep_source else 'no (delete after verify)'}\n"
            f"[dim]force in-use: [/]{'yes' if force else 'no'}",
            border_style="cyan",
        )
    )

    if source_info.containers and not force:
        err_console.print(
            "[bold red]Source is in use.[/] Pass [cyan]--force[/] to proceed "
            "(risk of corruption if containers write during copy)."
        )
        raise typer.Exit(EXIT_VALIDATION)

    if not yes:
        if not sys.stdin.isatty():
            err_console.print(
                "[bold red]Non-interactive run and --yes not set.[/] "
                "Refusing to rename without confirmation."
            )
            raise typer.Exit(EXIT_VALIDATION)
        if not Confirm.ask(
            f"Proceed with rename {source!r} → {target!r}?", default=False
        ):
            console.print("[yellow]Aborted.[/]")
            raise typer.Exit(EXIT_OK)

    try:
        result = rename_volume(
            source=source,
            target=target,
            keep_source=keep_source,
            force=force,
            client=client,
        )
    except RenameError as exc:
        err_console.print(f"[bold red]Rename failed:[/] {exc}")
        raise typer.Exit(EXIT_OPERATION)
    except DockerUnavailable as exc:
        _docker_fail(exc)

    console.print(
        f"[bold green]✓[/] Renamed [bold]{result.source}[/] → [bold]{result.target}[/] "
        f"({result.files_copied} files, {format_size(result.bytes_copied)})"
    )
    if not keep_source and not result.source_deleted:
        err_console.print(
            f"[yellow]Warning:[/] source volume {result.source!r} could not be "
            "removed; remove it manually."
        )


# ---------------------------------------------------------------------------
# Docker volume inspection
# ---------------------------------------------------------------------------


def _render_volume_table(volumes: List[VolumeInfo], show_size: bool = False) -> None:
    table = Table(
        title=f"Docker volumes ({len(volumes)})",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Name", style="bold")
    table.add_column("Driver")
    if show_size:
        table.add_column("Size", justify="right")
    table.add_column("Used by", overflow="fold")
    table.add_column("Mountpoint", overflow="fold", style="dim")
    for idx, v in enumerate(volumes, 1):
        users = ", ".join(v.containers) if v.containers else "[yellow]— orphan —[/]"
        row = [str(idx), v.name, v.driver]
        if show_size:
            row.append(format_size(v.size_bytes))
        row.extend([users, v.mountpoint])
        table.add_row(*row)
    console.print(table)


def _render_volume_details(info: VolumeInfo) -> None:
    console.print(f"\n[bold cyan]{info.name}[/]")
    console.print(f"  [dim]driver:    [/] {info.driver}")
    console.print(f"  [dim]scope:     [/] {info.scope}")
    console.print(f"  [dim]mountpoint:[/] {info.mountpoint}")
    console.print(f"  [dim]created_at:[/] {info.created_at or '—'}")
    if info.size_bytes is not None:
        console.print(f"  [dim]size:      [/] {format_size(info.size_bytes)}")
    if info.containers:
        console.print(f"  [dim]used by:   [/] {', '.join(info.containers)}")
    else:
        console.print("  [dim]used by:   [/] [yellow]— orphan —[/]")
    if info.labels:
        console.print("  [dim]labels:[/]")
        for k, v in info.labels.items():
            console.print(f"    {k}={v}")
    if info.options:
        console.print("  [dim]options:[/]")
        for k, v in info.options.items():
            console.print(f"    {k}={v}")
    if info.top_entries is not None:
        if info.top_entries:
            console.print(
                f"  [dim]top-level entries ({len(info.top_entries)}):[/]"
            )
            for entry in info.top_entries:
                console.print(f"    {entry}")
        else:
            console.print("  [dim]top-level entries:[/] [yellow](empty)[/]")


def _docker_fail(exc: DockerUnavailable) -> None:
    err_console.print(f"[bold red]Docker unavailable:[/] {exc}")
    raise typer.Exit(EXIT_CONFIG)


@volumes_app.command("list")
def volumes_list(
    show_size: bool = typer.Option(
        False,
        "--size/--no-size",
        help="Compute on-disk size per volume (spawns one helper container per volume).",
    ),
    orphans_only: bool = typer.Option(
        False, "--orphans", help="Only show volumes not mounted by any container."
    ),
) -> None:
    """List Docker volumes accessible from this container."""
    client = DockerClient()
    try:
        volumes = client.list_volumes()
    except DockerUnavailable as exc:
        _docker_fail(exc)

    if orphans_only:
        volumes = [v for v in volumes if not v.containers]

    if show_size and volumes:
        with console.status("[bold blue]Computing volume sizes...", spinner="dots"):
            for v in volumes:
                v.size_bytes = client.volume_size(v.name)

    if not volumes:
        console.print("[yellow]No matching Docker volumes.[/]")
        return
    _render_volume_table(volumes, show_size=show_size)


@volumes_app.command("create")
def volumes_create(
    name: str = typer.Argument(..., help="New volume name."),
) -> None:
    """Create a new Docker volume."""
    client = DockerClient()
    try:
        if client.volume_exists(name):
            err_console.print(
                f"[yellow]Volume {name!r} already exists.[/]"
            )
            raise typer.Exit(EXIT_VALIDATION)
        info = client.create_volume(name)
    except DockerUnavailable as exc:
        _docker_fail(exc)
    console.print(
        f"[bold green]✓[/] Created volume [bold]{info.name}[/] "
        f"[dim]at {info.mountpoint}[/]"
    )


@volumes_app.command("remove")
def volumes_remove(
    name: str = typer.Argument(..., help="Volume name to remove."),
    force: bool = typer.Option(
        False, "--force", "-f", help="Remove even if a container is using it."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Remove a Docker volume. Destructive — prompts unless --yes."""
    client = DockerClient()
    try:
        info = client.inspect_volume(name, with_size=False, with_contents=False)
    except DockerUnavailable as exc:
        _docker_fail(exc)

    if info.containers and not force:
        err_console.print(
            f"[bold red]Volume {name!r} is in use by:[/] {', '.join(info.containers)}\n"
            f"Pass [cyan]--force[/] to remove anyway."
        )
        raise typer.Exit(EXIT_VALIDATION)

    if not yes:
        if not sys.stdin.isatty():
            err_console.print(
                "[bold red]Non-interactive run and --yes not set.[/] "
                "Refusing to destroy a volume without confirmation."
            )
            raise typer.Exit(EXIT_VALIDATION)
        _render_volume_details(info)
        if not Confirm.ask(
            f"\n[bold red]Remove volume {name!r}?[/] This cannot be undone",
            default=False,
        ):
            console.print("[yellow]Aborted.[/]")
            raise typer.Exit(EXIT_OK)

    try:
        client.remove_volume(name, force=force)
    except DockerUnavailable as exc:
        _docker_fail(exc)
    console.print(f"[bold green]✓[/] Removed volume [bold]{name}[/]")


@volumes_app.command("inspect")
def volumes_inspect(
    name: str = typer.Argument(..., help="Volume name."),
    no_size: bool = typer.Option(
        False, "--no-size", help="Skip computing on-disk size."
    ),
    no_contents: bool = typer.Option(
        False, "--no-contents", help="Skip listing top-level contents."
    ),
    max_entries: int = typer.Option(
        50, "--max-entries", min=1, help="Max top-level entries to show."
    ),
) -> None:
    """Show detailed info for a Docker volume."""
    client = DockerClient()
    try:
        with console.status(f"[bold blue]Inspecting {name}...", spinner="dots"):
            info = client.inspect_volume(
                name,
                with_size=not no_size,
                with_contents=not no_contents,
                max_entries=max_entries,
            )
    except DockerUnavailable as exc:
        _docker_fail(exc)
    _render_volume_details(info)


# ---------------------------------------------------------------------------
# Interactive wizard
# ---------------------------------------------------------------------------


def _ensure_tty() -> None:
    if not sys.stdin.isatty():
        err_console.print(
            "[bold red]Interactive mode requires a TTY.[/] "
            "Launch the container with `docker run -it` or use a non-interactive subcommand."
        )
        raise typer.Exit(EXIT_VALIDATION)


def _wizard_path_or_volume(
    label: str,
    default_path: str,
    *,
    purpose: str,
) -> tuple[str, Optional[str]]:
    """Ask whether input/output is a bind-mount path or a Docker volume.

    Returns (path, volume_name). When the user picks a volume, path is the
    default /app/* placeholder (the pivot will override it).
    """
    use_volume = Confirm.ask(
        f"Use a Docker volume as {purpose}?", default=False
    )
    if not use_volume:
        return Prompt.ask(label, default=default_path), None

    client = DockerClient()
    if not client.ping():
        err_console.print(
            "[yellow]Docker socket unavailable — falling back to path entry.[/]"
        )
        return Prompt.ask(label, default=default_path), None

    try:
        volumes = client.list_volumes()
    except DockerUnavailable as exc:
        err_console.print(f"[red]{exc}[/]")
        return Prompt.ask(label, default=default_path), None

    if volumes:
        _render_volume_table(volumes, show_size=False)
    else:
        console.print("[yellow]No Docker volumes found.[/]")

    by_name = {v.name: v for v in volumes}
    while True:
        choice = Prompt.ask(
            f"Volume for {purpose} — [cyan]#[/] or [cyan]name[/] (blank to type a path)",
            default="",
        ).strip()
        if not choice:
            return Prompt.ask(label, default=default_path), None
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(volumes):
                return default_path, volumes[idx].name
        elif choice in by_name:
            return default_path, choice
        err_console.print(f"[red]No volume matches {choice!r}. Try again or leave blank.[/]")


def _wizard_common() -> tuple[str, List[str]]:
    log_level = Prompt.ask(
        "Log level", choices=[c.lower() for c in LOG_LEVEL_CHOICES], default="info"
    ).upper()
    while True:
        raw = Prompt.ask(
            "Log outputs (comma-separated: console, file, json_file)", default="console"
        )
        try:
            return log_level, _parse_log_output(raw)
        except typer.BadParameter as exc:
            err_console.print(f"[red]{exc.message}[/]")


@app.command("interactive")
def interactive() -> None:
    """Launch the interactive wizard (requires `docker run -it`)."""
    _ensure_tty()

    console.print(
        Panel.fit(
            "[bold cyan]Docker Volume Manager[/]\n"
            "[dim]Interactive mode — select 'quit' or press Ctrl+C to exit[/]",
            border_style="cyan",
        )
    )

    while True:
        try:
            op = Prompt.ask(
                "\nSelect operation",
                choices=[
                    "backup",
                    "restore",
                    "verify",
                    "copy",
                    "rename",
                    "volumes",
                    "quit",
                ],
                default="backup",
            ).lower()

            if op in {"quit", "q"}:
                return
            if op == "volumes":
                _wizard_volumes()
                continue
            if op == "rename":
                _wizard_rename()
                continue

            # Each operation delegates to the typer command, which raises
            # typer.Exit when it's done. We catch that so one op failing (or
            # even succeeding) doesn't terminate the whole wizard session.
            try:
                if op == "backup":
                    _wizard_run_backup()
                elif op == "restore":
                    _wizard_run_restore()
                elif op == "verify":
                    _wizard_run_verify()
                elif op == "copy":
                    _wizard_run_copy()
            except typer.Exit as exc:
                code = getattr(exc, "exit_code", 0)
                if code == 0:
                    console.print("[green]Operation finished.[/]")
                else:
                    err_console.print(
                        f"[yellow]Operation exited with code {code}.[/]"
                    )
            except Exception as exc:
                err_console.print(f"[bold red]Unhandled error:[/] {exc}")
        except KeyboardInterrupt:
            err_console.print("\n[yellow]Aborted by user.[/]")
            return


def _wizard_encryption_choice() -> tuple[Optional[str], List[str]]:
    """Ask for encryption mode and return (passphrase, recipients)."""
    mode = Prompt.ask(
        "Encryption",
        choices=["none", "passphrase", "recipients"],
        default="none",
    ).lower()
    if mode == "none":
        return None, []
    if mode == "passphrase":
        return Prompt.ask("Passphrase", password=True), []
    # recipients
    raw = Prompt.ask(
        "GPG recipient(s), comma-separated "
        "(requires the keyring to be mounted, e.g. -v ~/.gnupg:/root/.gnupg:ro)"
    )
    return None, [r.strip() for r in raw.split(",") if r.strip()]


def _wizard_run_backup() -> None:
    name = Prompt.ask("Backup base name")
    input_path, input_volume = _wizard_path_or_volume(
        "Source path", "/app/input_dir", purpose="source"
    )
    output_path, output_volume = _wizard_path_or_volume(
        "Destination path", "/app/output_dir", purpose="destination"
    )
    compression = Prompt.ask(
        "Compression algorithm",
        choices=[c.lower() for c in COMPRESSION_CHOICES],
        default="zstd",
    ).upper()
    parity = IntPrompt.ask("Parity percentage (0-100, 0 disables)", default=0)
    while not (0 <= parity <= 100):
        err_console.print("[red]Parity must be between 0 and 100.[/]")
        parity = IntPrompt.ask("Parity percentage", default=0)
    passphrase, recipients = _wizard_encryption_choice()
    log_level, log_output_list = _wizard_common()
    backup(
        name=name,
        input_path=input_path,
        input_volume=input_volume,
        output_path=output_path,
        output_volume=output_volume,
        compression=compression,
        parity=parity,
        encryption_key=passphrase,
        recipients=recipients or None,
        log_level=log_level,
        log_output=",".join(log_output_list),
    )


def _wizard_run_restore() -> None:
    name = Prompt.ask("Backup base name")
    input_path, input_volume = _wizard_path_or_volume(
        "Backup directory", "/app/input_dir", purpose="backup source"
    )
    output_path, output_volume = _wizard_path_or_volume(
        "Restore destination", "/app/output_dir", purpose="restore target"
    )
    timestamp = Prompt.ask("Timestamp (blank = latest)", default="") or None
    encrypted = Confirm.ask("Is the backup encrypted?", default=False)
    encryption_key = (
        Prompt.ask(
            "Passphrase (leave blank if public-key encrypted — your GPG "
            "keyring will be used automatically)",
            default="",
            password=True,
        )
        or None
        if encrypted
        else None
    )
    overwrite = Confirm.ask(
        "Overwrite destination without prompting?", default=True
    )
    log_level, log_output_list = _wizard_common()
    restore(
        name=name,
        input_path=input_path,
        input_volume=input_volume,
        output_path=output_path,
        output_volume=output_volume,
        timestamp=timestamp,
        encryption_key=encryption_key,
        overwrite=overwrite,
        log_level=log_level,
        log_output=",".join(log_output_list),
    )


def _wizard_run_verify() -> None:
    name = Prompt.ask("Backup base name")
    output_path, output_volume = _wizard_path_or_volume(
        "Backup directory", "/app/output_dir", purpose="backup location"
    )
    encrypted = Confirm.ask("Is the backup encrypted?", default=False)
    encryption_key = (
        Prompt.ask("Passphrase", password=True) if encrypted else None
    )
    log_level, log_output_list = _wizard_common()
    verify(
        name=name,
        output_path=output_path,
        output_volume=output_volume,
        encryption_key=encryption_key,
        log_level=log_level,
        log_output=",".join(log_output_list),
    )


def _wizard_run_copy() -> None:
    input_path, input_volume = _wizard_path_or_volume(
        "Source path", "/app/input_dir", purpose="source"
    )
    output_path, output_volume = _wizard_path_or_volume(
        "Destination path", "/app/output_dir", purpose="destination"
    )
    overwrite = Confirm.ask("Overwrite destination contents?", default=True)
    log_level, log_output_list = _wizard_common()
    copy(
        input_path=input_path,
        input_volume=input_volume,
        output_path=output_path,
        output_volume=output_volume,
        overwrite=overwrite,
        log_level=log_level,
        log_output=",".join(log_output_list),
    )


def _wizard_volumes() -> None:
    """Interactive volume explorer: list / inspect / create / remove."""
    client = DockerClient()
    if not client.ping():
        err_console.print(
            "[bold red]Docker socket unavailable.[/] "
            "Re-run with: [dim]-v /var/run/docker.sock:/var/run/docker.sock[/]"
        )
        return

    while True:
        action = Prompt.ask(
            "\nVolume action",
            choices=["list", "inspect", "create", "rename", "remove", "quit"],
            default="list",
        ).lower()
        if action in {"quit", "q"}:
            return
        if action == "list":
            _wizard_volumes_show_list(client)
        elif action == "inspect":
            _wizard_volumes_inspect_one(client)
        elif action == "create":
            _wizard_volumes_create(client)
        elif action == "rename":
            _wizard_rename(client=client)
        elif action == "remove":
            _wizard_volumes_remove(client)


def _wizard_volumes_show_list(client: DockerClient) -> None:
    try:
        volumes = client.list_volumes()
    except DockerUnavailable as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    if not volumes:
        console.print("[yellow]No Docker volumes found.[/]")
        return
    compute_sizes = Confirm.ask(
        "Compute on-disk size for each volume? (slower — one helper per volume)",
        default=False,
    )
    if compute_sizes:
        with console.status("[bold blue]Computing sizes...", spinner="dots"):
            for v in volumes:
                v.size_bytes = client.volume_size(v.name)
    _render_volume_table(volumes, show_size=compute_sizes)


def _wizard_volumes_inspect_one(client: DockerClient) -> None:
    try:
        volumes = client.list_volumes()
    except DockerUnavailable as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    if not volumes:
        console.print("[yellow]No Docker volumes found.[/]")
        return
    _render_volume_table(volumes, show_size=False)
    by_name = {v.name: v for v in volumes}
    choice = Prompt.ask(
        "Volume [cyan]#[/] or [cyan]name[/] to inspect",
        default="",
    ).strip()
    if not choice:
        return
    target: Optional[str] = None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(volumes):
            target = volumes[idx].name
    elif choice in by_name:
        target = choice
    if not target:
        err_console.print(f"[red]No volume matches {choice!r}.[/]")
        return
    try:
        with console.status(
            f"[bold blue]Inspecting {target}...", spinner="dots"
        ):
            info = client.inspect_volume(target, with_size=True, with_contents=True)
    except DockerUnavailable as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    _render_volume_details(info)


def _wizard_volumes_create(client: DockerClient) -> None:
    name = Prompt.ask("New volume name").strip()
    if not name:
        return
    try:
        if client.volume_exists(name):
            err_console.print(f"[yellow]Volume {name!r} already exists.[/]")
            return
        info = client.create_volume(name)
    except DockerUnavailable as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    console.print(
        f"[bold green]✓[/] Created volume [bold]{info.name}[/] "
        f"[dim]at {info.mountpoint}[/]"
    )


def _wizard_volumes_remove(client: DockerClient) -> None:
    try:
        volumes = client.list_volumes()
    except DockerUnavailable as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    if not volumes:
        console.print("[yellow]No Docker volumes found.[/]")
        return
    _render_volume_table(volumes, show_size=False)
    by_name = {v.name: v for v in volumes}
    choice = Prompt.ask(
        "Volume [cyan]#[/] or [cyan]name[/] to remove",
        default="",
    ).strip()
    if not choice:
        return
    target: Optional[VolumeInfo] = None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(volumes):
            target = volumes[idx]
    elif choice in by_name:
        target = by_name[choice]
    if not target:
        err_console.print(f"[red]No volume matches {choice!r}.[/]")
        return

    _render_volume_details(target)
    if target.containers:
        force = Confirm.ask(
            f"[yellow]Volume is in use by {', '.join(target.containers)}. "
            "Force removal?[/]",
            default=False,
        )
        if not force:
            console.print("[yellow]Aborted.[/]")
            return
    else:
        force = False

    if not Confirm.ask(
        f"[bold red]Remove volume {target.name!r}?[/] This cannot be undone",
        default=False,
    ):
        console.print("[yellow]Aborted.[/]")
        return
    try:
        client.remove_volume(target.name, force=force)
    except DockerUnavailable as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    console.print(f"[bold green]✓[/] Removed volume [bold]{target.name}[/]")


def _wizard_rename(client: Optional[DockerClient] = None) -> None:
    """Interactive rename: pick source from list, confirm target, execute."""
    if client is None:
        client = DockerClient()
        if not client.ping():
            err_console.print(
                "[bold red]Docker socket unavailable.[/] "
                "Re-run with: [dim]-v /var/run/docker.sock:/var/run/docker.sock[/]"
            )
            return

    try:
        volumes = client.list_volumes()
    except DockerUnavailable as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    if not volumes:
        console.print("[yellow]No Docker volumes found.[/]")
        return

    _render_volume_table(volumes, show_size=False)
    by_name = {v.name: v for v in volumes}

    choice = Prompt.ask(
        "Source volume [cyan]#[/] or [cyan]name[/] (blank to cancel)", default=""
    ).strip()
    if not choice:
        return
    source_info: Optional[VolumeInfo] = None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(volumes):
            source_info = volumes[idx]
    elif choice in by_name:
        source_info = by_name[choice]
    if not source_info:
        err_console.print(f"[red]No volume matches {choice!r}.[/]")
        return

    target = Prompt.ask(f"New name for {source_info.name!r}").strip()
    if not target:
        return
    if target == source_info.name:
        err_console.print("[red]Target must differ from source.[/]")
        return
    if target in by_name:
        err_console.print(f"[red]Volume {target!r} already exists.[/]")
        return

    force = False
    if source_info.containers:
        if not Confirm.ask(
            f"[yellow]Source is in use by {', '.join(source_info.containers)}. "
            "Force rename (risk of corruption if containers write)?[/]",
            default=False,
        ):
            console.print("[yellow]Aborted.[/]")
            return
        force = True

    keep_source = Confirm.ask(
        "Keep source volume after a successful rename?", default=False
    )

    tail = (
        "Source will be kept."
        if keep_source
        else "[bold red]Source will be deleted after verification.[/]"
    )
    if not Confirm.ask(
        f"Proceed with rename {source_info.name!r} → {target!r}? {tail}",
        default=False,
    ):
        console.print("[yellow]Aborted.[/]")
        return

    from operations.rename_volume import RenameError, rename_volume  # noqa: WPS433

    try:
        result = rename_volume(
            source=source_info.name,
            target=target,
            keep_source=keep_source,
            force=force,
            client=client,
        )
    except RenameError as exc:
        err_console.print(f"[bold red]Rename failed:[/] {exc}")
        return
    except DockerUnavailable as exc:
        err_console.print(f"[red]{exc}[/]")
        return

    console.print(
        f"[bold green]✓[/] Renamed [bold]{result.source}[/] → "
        f"[bold]{result.target}[/] "
        f"({result.files_copied} files, {format_size(result.bytes_copied)})"
    )
    if not keep_source and not result.source_deleted:
        err_console.print(
            f"[yellow]Warning:[/] source volume {result.source!r} could not be "
            "removed; remove it manually."
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
