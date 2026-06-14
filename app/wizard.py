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

import completion
import wizard_ui
from cli_shared import (
    COMPRESSION_CHOICES,
    EXIT_VALIDATION,
    LOG_LEVEL_CHOICES,
    _parse_log_output,
    console,
    err_console,
)
from docker_client import DockerClient, DockerError, VolumeInfo, format_size
from runners import _run_backup, _run_copy, _run_restore, _run_verify
from volumes_cli import _render_volume_details, _render_volume_table
from wizard_store import BackupStore, LocalDirStore, VolumeStore

# Choice lists shared between rich's validation and TAB completion.
_OPERATIONS = ["backup", "restore", "verify", "copy", "rename", "volumes", "quit"]
_VOLUME_ACTIONS = ["list", "inspect", "create", "rename", "remove", "quit"]
_LOG_OUTPUTS = ["console", "file", "json_file"]


def _ensure_tty() -> None:
    if not sys.stdin.isatty():
        err_console.print(
            "[bold red]Interactive mode requires a TTY.[/] "
            "Launch the container with `docker run -it` or use a non-interactive subcommand."
        )
        raise typer.Exit(EXIT_VALIDATION)


def _wizard_location(
    label: str,
    default_path: str,
    *,
    purpose: str,
) -> tuple[str, str | None, BackupStore]:
    """Pick the location first: a bind-mount path or a Docker volume.

    Returns ``(path, volume_name, store)``. ``store`` lets the caller list and
    validate backups uniformly (``LocalDirStore`` for a path, ``VolumeStore`` for
    a volume). For a volume, ``path`` is the ``/app/*`` placeholder the pivot
    overrides; ``volume_name`` is None for a path.
    """
    if Confirm.ask(f"Use a Docker volume as {purpose}?", default=False):
        store = _wizard_volume_location(purpose)
        if store is not None:
            return default_path, store.volume, store
        # Socket unavailable / no volumes / cancelled → fall back to path entry.

    path = completion.ask(label, completion.paths(), default=default_path)
    return path, None, LocalDirStore(path)


def _wizard_volume_location(purpose: str) -> VolumeStore | None:
    """Pick a Docker volume by number or name (paged). None to fall back to a path."""
    client = DockerClient()
    if not client.ping():
        err_console.print(
            "[yellow]Docker socket unavailable — falling back to path entry.[/]"
        )
        return None
    try:
        volumes = client.list_volumes()
    except DockerError as exc:
        err_console.print(f"[red]{exc}[/]")
        return None
    if not volumes:
        console.print(
            "[yellow]No Docker volumes found — falling back to path entry.[/]"
        )
        return None

    info = {v.name: v for v in volumes}
    names = sorted(info)

    def note(name: str) -> str:
        v = info[name]
        return "in use: " + ", ".join(v.containers) if v.containers else "orphan"

    picked = wizard_ui.select_paged(
        f"Volume for {purpose} (blank to type a path instead)",
        names,
        note_fn=note,
        title="Docker volumes",
    )
    if not picked:
        return None
    return VolumeStore(picked, client)


def _wizard_pick_existing_backup(store: BackupStore) -> tuple[str | None, str | None]:
    """Pick an existing backup (name + timestamp) from ``store``, validating each step.

    Returns ``(name, timestamp)``, or ``(None, None)`` when there's nothing to
    pick or the user cancels. Used by restore and verify.
    """
    names = store.list_names()
    if not names:
        err_console.print(f"[yellow]No backups found in {store.label}.[/]")
        return None, None

    while True:
        name = wizard_ui.select_paged(
            "Backup base name", names, title=f"Backups in {store.label}"
        )
        if name is None:
            return None, None
        timestamps = store.list_timestamps(name)
        if not timestamps:
            err_console.print(
                f"[red]{name!r} contains no dated backups — pick another.[/]"
            )
            continue
        ts = wizard_ui.select_paged(
            "Backup timestamp",
            timestamps,
            default=timestamps[0],
            note_fn=lambda t, _latest=timestamps[0]: "latest" if t == _latest else "",
            title=f"Timestamps for {name}",
        )
        if ts is None:
            ts = timestamps[0]
        if not store.has_archive(name, ts):
            err_console.print(
                f"[red]No backup archive inside {name}/{ts} — pick another.[/]"
            )
            continue
        return name, ts


def _wizard_new_backup_name(store: BackupStore) -> str:
    """Prompt for a backup name (new or existing) — existing names are listed."""
    existing = store.list_names()
    if existing:
        console.print(
            f"[dim]Existing names in {store.label} are listed — reuse one to add a "
            "new dated backup, or type a fresh name.[/]"
        )
    while True:
        name = wizard_ui.select_paged(
            "Backup base name (new or existing)",
            existing,
            allow_custom=True,
            title=(f"Existing backups in {store.label}" if existing else None),
        )
        if name:
            return name
        err_console.print("[red]A backup name is required.[/]")


def _wizard_common() -> tuple[str, list[str]]:
    levels = [c.lower() for c in LOG_LEVEL_CHOICES]
    log_level = completion.ask(
        "Log level", completion.words(levels), choices=levels, default="info"
    ).upper()
    while True:
        raw = completion.ask(
            "Log outputs (comma-separated: console, file, json_file)",
            completion.csv_words(_LOG_OUTPUTS),
            default="console",
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
            op = completion.ask(
                "\nSelect operation",
                completion.words(_OPERATIONS),
                choices=_OPERATIONS,
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
    enc_modes = ["none", "passphrase", "recipients"]
    mode = completion.ask(
        "Encryption",
        completion.words(enc_modes),
        choices=enc_modes,
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
    input_path, input_volume, _ = _wizard_location(
        "Source path (the data to back up)", "/dvm/source", purpose="source"
    )
    output_path, output_volume, dest_store = _wizard_location(
        "Destination path (where the backup is written)",
        "/dvm/dest",
        purpose="destination",
    )
    if not dest_store.is_writable():
        err_console.print(
            f"[bold red]Destination {dest_store.label} is not writable.[/] "
            "Choose another destination."
        )
        return
    name = _wizard_new_backup_name(dest_store)
    comp_choices = [c.lower() for c in COMPRESSION_CHOICES]
    compression = completion.ask(
        "Compression algorithm",
        completion.words(comp_choices),
        choices=comp_choices,
        default="zstd",
    ).upper()
    parity = IntPrompt.ask("Parity percentage (0-100, 0 disables)", default=0)
    while not (0 <= parity <= 100):
        err_console.print("[red]Parity must be between 0 and 100.[/]")
        parity = IntPrompt.ask("Parity percentage", default=0)
    passphrase, recipients = _wizard_encryption_choice()
    log_level, log_output_list = _wizard_common()
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
        input_volume=input_volume,
        output_volume=output_volume,
        log_level=log_level,
        log_output=log_output_list,
    )


def _wizard_run_restore() -> None:
    console.print(
        "[dim]Restore reads a backup from a [bold]source[/] directory and writes its "
        "contents into a separate [bold]destination[/]. The source is where the backup "
        "lives — the dir that contains [bold]<name>/<timestamp>/…[/] (e.g. the dir you "
        "backed up to). Source and destination must differ.[/]"
    )
    input_path, input_volume, src_store = _wizard_location(
        "Backup source dir (holds <name>/<timestamp>/)",
        "/dvm/dest",
        purpose="backup source (where the backup is stored)",
    )
    name, timestamp = _wizard_pick_existing_backup(src_store)
    if name is None:
        return
    output_path, output_volume, _ = _wizard_location(
        "Restore destination dir (where files are written)",
        "/dvm/source",
        purpose="restore destination",
    )
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
    _run_restore(
        name=name,
        input_path=input_path,
        output_path=output_path,
        timestamp=timestamp,
        encryption_key=encryption_key,
        overwrite=overwrite,
        input_volume=input_volume,
        output_volume=output_volume,
        log_level=log_level,
        log_output=log_output_list,
    )


def _wizard_run_verify() -> None:
    output_path, output_volume, store = _wizard_location(
        "Backup directory", "/dvm/dest", purpose="backup location"
    )
    name, timestamp = _wizard_pick_existing_backup(store)
    if name is None:
        return
    encrypted = Confirm.ask("Is the backup encrypted?", default=False)
    encryption_key = Prompt.ask("Passphrase", password=True) if encrypted else None
    repair = Confirm.ask(
        "Auto-repair the backup in place if it is damaged but recoverable? "
        "(answer no for a read-only audit that never modifies the file)",
        default=True,
    )
    log_level, log_output_list = _wizard_common()
    _run_verify(
        name=name,
        output_path=output_path,
        encryption_key=encryption_key,
        output_volume=output_volume,
        timestamp=timestamp,
        repair=repair,
        log_level=log_level,
        log_output=log_output_list,
    )


def _wizard_run_copy() -> None:
    input_path, input_volume, _ = _wizard_location(
        "Source path", "/dvm/source", purpose="source"
    )
    output_path, output_volume, dest_store = _wizard_location(
        "Destination path", "/dvm/dest", purpose="destination"
    )
    if not dest_store.is_writable():
        err_console.print(
            f"[bold red]Destination {dest_store.label} is not writable.[/] "
            "Choose another destination."
        )
        return
    overwrite = Confirm.ask("Overwrite destination contents?", default=True)
    log_level, log_output_list = _wizard_common()
    _run_copy(
        input_path=input_path,
        output_path=output_path,
        overwrite=overwrite,
        input_volume=input_volume,
        output_volume=output_volume,
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
        action = completion.ask(
            "\nVolume action",
            completion.words(_VOLUME_ACTIONS),
            choices=_VOLUME_ACTIONS,
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


def _pick_volume(volumes: list[VolumeInfo], prompt: str) -> VolumeInfo | None:
    """Prompt for a volume by 1-based index or name (table rendered by caller).

    Returns the selected VolumeInfo, or None on blank input / no match — the
    caller decides what that means (cancel, abort, re-prompt, ...).
    """
    by_name = {v.name: v for v in volumes}
    choice = completion.ask(prompt, completion.words(by_name), default="").strip()
    if not choice:
        return None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(volumes):
            return volumes[idx]
    elif choice in by_name:
        return by_name[choice]
    err_console.print(f"[red]No volume matches {choice!r}.[/]")
    return None


def _wizard_volumes_show_list(client: DockerClient) -> None:
    try:
        volumes = client.list_volumes()
    except DockerError as exc:
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
    except DockerError as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    if not volumes:
        console.print("[yellow]No Docker volumes found.[/]")
        return
    _render_volume_table(volumes, show_size=False)
    picked = _pick_volume(volumes, "Volume [cyan]#[/] or [cyan]name[/] to inspect")
    if picked is None:
        return
    try:
        with console.status(f"[bold blue]Inspecting {picked.name}...", spinner="dots"):
            info = client.inspect_volume(
                picked.name, with_size=True, with_contents=True
            )
    except DockerError as exc:
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
    except DockerError as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    console.print(
        f"[bold green]✓[/] Created volume [bold]{info.name}[/] "
        f"[dim]at {info.mountpoint}[/]"
    )


def _wizard_volumes_remove(client: DockerClient) -> None:
    try:
        volumes = client.list_volumes()
    except DockerError as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    if not volumes:
        console.print("[yellow]No Docker volumes found.[/]")
        return
    _render_volume_table(volumes, show_size=False)
    target = _pick_volume(volumes, "Volume [cyan]#[/] or [cyan]name[/] to remove")
    if target is None:
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
    except DockerError as exc:
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
    except DockerError as exc:
        err_console.print(f"[red]{exc}[/]")
        return
    if not volumes:
        console.print("[yellow]No Docker volumes found.[/]")
        return

    _render_volume_table(volumes, show_size=False)
    by_name = {v.name: v for v in volumes}

    source_info = _pick_volume(
        volumes, "Source volume [cyan]#[/] or [cyan]name[/] (blank to cancel)"
    )
    if source_info is None:
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
    except DockerError as exc:
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
