"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Docker Volume Manager CLI — typer + rich.

This module is the thin composition layer: it builds the typer app, defines the
scripted subcommands (backup / restore / verify / copy / rename / interactive),
mounts the `volumes` sub-app, and wires everything together. The heavy lifting
lives in focused modules:

- cli_shared : exit codes, consoles, parsing helpers.
- pivot      : helper-container pivot for volume operations.
- runners    : Config building + operation dispatch + typed exit codes.
- volumes_cli: the `dvm volumes` sub-application.
- wizard     : the interactive (`dvm interactive`) experience.

Environment variables are honoured transparently as a fallback (typer's
`envvar=`); explicit CLI flags always take precedence.
"""

from __future__ import annotations

import os
import sys

import typer
from rich.panel import Panel
from rich.prompt import Confirm
from rich.traceback import install as install_rich_tracebacks

import wizard
from cli_shared import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_OPERATION,
    EXIT_VALIDATION,
    _docker_fail,
    _normalise_recipients,
    _parse_log_output,
    console,
    err_console,
)
from docker_client import DockerClient, DockerUnavailable, format_size
from gpg_keyring import KeyringError, setup_recipient_keyring, tear_down_keyring
from pivot import _pivot_if_volumes
from runners import _run_backup, _run_copy, _run_restore, _run_verify
from volumes_cli import volumes_app

install_rich_tracebacks(console=err_console, show_locals=False, suppress=[typer])

app = typer.Typer(
    name="dvm",
    help="Docker Volume Manager — backup, restore, verify and copy Docker volume contents.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(volumes_app)


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
    input_volume: str | None = typer.Option(
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
    output_volume: str | None = typer.Option(
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
        "level 19 with all cores. GZ is for universal interop.",
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
    encryption_key: str | None = typer.Option(
        None,
        "--encryption-key",
        "-k",
        envvar="ENCRYPTION_KEY",
        help="GPG symmetric passphrase. Mutually exclusive with --recipient.",
    ),
    recipients: list[str] | None = typer.Option(
        None,
        "--recipient",
        "-r",
        envvar="GPG_RECIPIENTS",
        help="GPG public-key recipient (repeatable). Looked up in the system "
        "keyring — mount it via `-v ~/.gnupg:/root/.gnupg:ro`.",
    ),
    recipient_files: list[str] | None = typer.Option(
        None,
        "--recipient-key-file",
        "-K",
        help="Path to a GPG public-key file (.asc) to use as recipient (repeatable). "
        "Imported into a throwaway keyring at runtime — no keyring mount needed. "
        "Mutually exclusive with --recipient and --encryption-key.",
    ),
    sign_key: str | None = typer.Option(
        None,
        "--sign-key",
        envvar="SIGN_KEY",
        help="Fingerprint or user-id of a GPG private key to produce a detached "
        "signature alongside the archive (`<archive>.sig`). Requires the key "
        "to be present in the active keyring (mount ~/.gnupg).",
    ),
    sign_key_passphrase: str | None = typer.Option(
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
    encryption_modes = sum(bool(x) for x in (encryption_key, recipient_list, file_list))
    if encryption_modes > 1:
        err_console.print(
            "[bold red]--encryption-key, --recipient and --recipient-key-file "
            "are mutually exclusive — pick one encryption mode.[/]"
        )
        raise typer.Exit(EXIT_VALIDATION)

    keyring_homedir: str | None = None
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
            raise typer.Exit(EXIT_CONFIG) from None
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
        ...,
        "--name",
        "-n",
        envvar="BACKUP_FILE_NAME",
        help="Backup base name to restore.",
    ),
    input_path: str = typer.Option(
        "/app/input_dir",
        "--input",
        "-i",
        envvar="INPUT_PATH",
        help="Directory containing the timestamped backup subdirectories.",
    ),
    input_volume: str | None = typer.Option(
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
    output_volume: str | None = typer.Option(
        None,
        "--output-volume",
        envvar="OUTPUT_VOLUME",
        help="Restore content into a Docker volume (pivots to a helper container).",
    ),
    timestamp: str | None = typer.Option(
        None,
        "--timestamp",
        "-t",
        envvar="TIMESTAMP",
        help="Specific backup timestamp (YYYYmmdd_HHMM[_NN]). Latest is used if omitted.",
    ),
    encryption_key: str | None = typer.Option(
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
        ...,
        "--name",
        "-n",
        envvar="BACKUP_FILE_NAME",
        help="Backup base name to verify.",
    ),
    output_path: str = typer.Option(
        "/app/output_dir",
        "--output",
        "-o",
        envvar="OUTPUT_PATH",
        help="Directory containing the backup to check.",
    ),
    output_volume: str | None = typer.Option(
        None,
        "--output-volume",
        envvar="OUTPUT_VOLUME",
        help="Verify a backup stored in a Docker volume (pivots to a helper; "
        "mounted rw for par2 repair).",
    ),
    encryption_key: str | None = typer.Option(
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
    input_volume: str | None = typer.Option(
        None,
        "--input-volume",
        envvar="INPUT_VOLUME",
        help="Source is a Docker volume (pivots to a helper container).",
    ),
    output_path: str = typer.Option(
        "/app/output_dir", "--output", "-o", envvar="OUTPUT_PATH"
    ),
    output_volume: str | None = typer.Option(
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
    from operations.rename_volume import RenameError, rename_volume

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
            err_console.print(f"[bold red]Source volume {source!r} does not exist.[/]")
            raise typer.Exit(EXIT_VALIDATION)
        if client.volume_exists(target):
            err_console.print(f"[bold red]Target volume {target!r} already exists.[/]")
            raise typer.Exit(EXIT_VALIDATION)
        with console.status(f"[bold blue]Inspecting {source}...", spinner="dots"):
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
        raise typer.Exit(EXIT_OPERATION) from None
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


@app.command("interactive")
def interactive() -> None:
    """Launch the interactive wizard (requires `docker run -it`)."""
    wizard.run_interactive()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
