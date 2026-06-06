"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Interactive wizard (the ``dvm interactive`` command). Drives backup / restore /
verify / copy / rename / volume operations through rich prompts, then dispatches
through the same pivot + runner helpers the scripted commands use (never through
the typer command objects — calling those directly leaks ``OptionInfo``
sentinels for unset options).

Requires a TTY (``docker run -it``).
"""

from __future__ import annotations

import sys

import typer
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt

from cli_shared import (
    COMPRESSION_CHOICES,
    EXIT_VALIDATION,
    LOG_LEVEL_CHOICES,
    _parse_log_output,
    console,
    err_console,
)
from docker_client import DockerClient, DockerUnavailable, VolumeInfo, format_size
from pivot import _pivot_if_volumes
from runners import _run_backup, _run_copy, _run_restore, _run_verify
from volumes_cli import _render_volume_details, _render_volume_table


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
) -> tuple[str, str | None]:
    """Ask whether input/output is a bind-mount path or a Docker volume.

    Returns (path, volume_name). When the user picks a volume, path is the
    default /app/* placeholder (the pivot will override it).
    """
    use_volume = Confirm.ask(f"Use a Docker volume as {purpose}?", default=False)
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
        err_console.print(
            f"[red]No volume matches {choice!r}. Try again or leave blank.[/]"
        )


def _wizard_common() -> tuple[str, list[str]]:
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


def run_interactive() -> None:
    """Launch the interactive wizard loop (requires a TTY)."""
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

            # Each run helper raises typer.Exit when done. Catch it so one op
            # finishing (or failing) doesn't terminate the whole wizard session.
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
                    err_console.print(f"[yellow]Operation exited with code {code}.[/]")
            except Exception as exc:
                err_console.print(f"[bold red]Unhandled error:[/] {exc}")
        except KeyboardInterrupt:
            err_console.print("\n[yellow]Aborted by user.[/]")
            return


def _wizard_encryption_choice() -> tuple[str | None, list[str]]:
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
    _pivot_if_volumes(
        subcommand="backup",
        env={
            "BACKUP_FILE_NAME": name,
            "INPUT_PATH": input_path,
            "OUTPUT_PATH": output_path,
            "COMPRESSION": compression,
            "PARITY": parity,
            "ENCRYPTION_KEY": passphrase,
            "GPG_RECIPIENTS": ",".join(recipients) if recipients else None,
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": ",".join(log_output_list),
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
        encryption_key=passphrase,
        recipients=recipients,
        sign_key=None,
        sign_key_passphrase=None,
        log_level=log_level,
        log_output=log_output_list,
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
    overwrite = Confirm.ask("Overwrite destination without prompting?", default=True)
    log_level, log_output_list = _wizard_common()
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
            "LOG_OUTPUT": ",".join(log_output_list),
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
        log_output=log_output_list,
    )


def _wizard_run_verify() -> None:
    name = Prompt.ask("Backup base name")
    output_path, output_volume = _wizard_path_or_volume(
        "Backup directory", "/app/output_dir", purpose="backup location"
    )
    encrypted = Confirm.ask("Is the backup encrypted?", default=False)
    encryption_key = Prompt.ask("Passphrase", password=True) if encrypted else None
    log_level, log_output_list = _wizard_common()
    _pivot_if_volumes(
        subcommand="verify",
        env={
            "BACKUP_FILE_NAME": name,
            "OUTPUT_PATH": output_path,
            "ENCRYPTION_KEY": encryption_key,
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": ",".join(log_output_list),
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
        log_output=log_output_list,
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
    _pivot_if_volumes(
        subcommand="copy",
        env={
            "INPUT_PATH": input_path,
            "OUTPUT_PATH": output_path,
            "COPY_OVERWRITE": "Y" if overwrite else "N",
            "LOG_LEVEL": log_level,
            "LOG_OUTPUT": ",".join(log_output_list),
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
        log_output=log_output_list,
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
    target: str | None = None
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
        with console.status(f"[bold blue]Inspecting {target}...", spinner="dots"):
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
    target: VolumeInfo | None = None
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


def _wizard_rename(client: DockerClient | None = None) -> None:
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
    source_info: VolumeInfo | None = None
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

    from operations.rename_volume import RenameError, rename_volume

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
