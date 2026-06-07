"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

The ``dvm volumes`` sub-application: list / inspect / create / remove Docker
volumes. Requires the Docker socket to be mounted. Rendering helpers
(:func:`_render_volume_table`, :func:`_render_volume_details`) are shared with
the interactive wizard.
"""

from __future__ import annotations

import sys

import typer
from rich.prompt import Confirm
from rich.table import Table

from cli_shared import (
    EXIT_OK,
    EXIT_VALIDATION,
    _docker_fail,
    console,
    err_console,
)
from docker_client import DockerClient, DockerError, VolumeInfo, format_size

volumes_app = typer.Typer(
    name="volumes",
    help="Inspect Docker volumes (requires docker.sock mount).",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _render_volume_table(volumes: list[VolumeInfo], show_size: bool = False) -> None:
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
            console.print(f"  [dim]top-level entries ({len(info.top_entries)}):[/]")
            for entry in info.top_entries:
                console.print(f"    {entry}")
        else:
            console.print("  [dim]top-level entries:[/] [yellow](empty)[/]")


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
    except DockerError as exc:
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
            err_console.print(f"[yellow]Volume {name!r} already exists.[/]")
            raise typer.Exit(EXIT_VALIDATION)
        info = client.create_volume(name)
    except DockerError as exc:
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
    except DockerError as exc:
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
    except DockerError as exc:
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
    except DockerError as exc:
        _docker_fail(exc)
    _render_volume_details(info)
