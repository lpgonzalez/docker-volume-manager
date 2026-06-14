"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Helper-container pivot. When an operation's source/destination is "remote" — a
``--input-volume`` / ``--output-volume`` (Docker volume) or a ``--input-host`` /
``--output-host`` (absolute host path) — the main process can't touch it
directly; instead it spawns a sibling "helper" container with the source/dest
mounted at /dvm/source and /dvm/dest, streams that container's logs, and exits
with its status code. A bare directly-mounted path (the socket-less fallback) is
not remote and runs in-process.
"""

from __future__ import annotations

import os
from typing import Any

import typer
from rich.panel import Panel

from cli_shared import (
    EXIT_CONFIG,
    EXIT_VALIDATION,
    _docker_fail,
    console,
    err_console,
)
from docker_client import DockerClient, DockerError


class _Side:
    """Resolved source/destination side. ``mount_key`` is the volume name or the
    absolute host path (None for a local, in-process path)."""

    def __init__(self, kind: str, mount_key: str | None):
        self.kind = kind  # "local" | "volume" | "host"
        self.mount_key = mount_key
        self.is_remote = kind in ("volume", "host")


def _resolve_side(label: str, volume: str | None, host: str | None) -> _Side:
    """Map a side's (volume, host) flags to a :class:`_Side`.

    ``--*-volume`` and ``--*-host`` are mutually exclusive; a host path must be
    absolute (docker-py would treat a relative key as a volume name). When
    neither is set the side is local (handled in-process by the caller).
    """
    if volume and host:
        err_console.print(
            f"[bold red]--{label}-volume and --{label}-host are mutually "
            "exclusive — pick one.[/]"
        )
        raise typer.Exit(EXIT_VALIDATION)
    if volume:
        return _Side("volume", volume)
    if host:
        if not os.path.isabs(host):
            err_console.print(
                f"[bold red]--{label}-host must be an absolute path[/] (got "
                f"{host!r}); a relative path would be treated as a volume name."
            )
            raise typer.Exit(EXIT_VALIDATION)
        return _Side("host", host)
    return _Side("local", None)


def _pivot_if_remote(
    subcommand: str,
    env: dict[str, Any],
    *,
    input_volume: str | None = None,
    input_host: str | None = None,
    output_volume: str | None = None,
    output_host: str | None = None,
    input_mode: str = "ro",
    output_mode: str = "rw",
) -> None:
    """
    If a source/destination side is remote (a Docker volume or a host path) and
    we're not already inside a helper, spawn a helper container with the
    source/dest mounted at /dvm/source / /dvm/dest, stream its logs, and exit
    with its status code.

    No-op when both sides are local; the caller proceeds in-process.
    """
    src = _resolve_side("input", input_volume, input_host)
    dst = _resolve_side("output", output_volume, output_host)
    if not (src.is_remote or dst.is_remote):
        return
    if os.environ.get("DVM_HELPER_MODE") == "1":
        err_console.print(
            "[bold red]Volume / host-path flags cannot be used inside a helper "
            "container.[/]"
        )
        raise typer.Exit(EXIT_VALIDATION)

    client = DockerClient()
    if not client.ping():
        err_console.print(
            "[bold red]Docker unavailable.[/] "
            "Volume / host-path flags require the Docker socket. "
            "Re-run with: [dim]-v /var/run/docker.sock:/var/run/docker.sock[/]"
        )
        raise typer.Exit(EXIT_CONFIG)

    # Validate volume sides exist (host paths are dockerd-resolved at run time).
    for label, side in (("input", src), ("output", dst)):
        if side.kind != "volume":
            continue
        try:
            exists = client.volume_exists(side.mount_key)
        except DockerError as exc:
            _docker_fail(exc)
        if not exists:
            err_console.print(
                f"[bold red]{label.capitalize()} volume {side.mount_key!r} does "
                f"not exist.[/] Create it with `dvm volumes create {side.mount_key}`."
            )
            raise typer.Exit(EXIT_VALIDATION)

    volume_mounts: dict[str, dict[str, str]] = {}
    helper_env: dict[str, Any] = {k: v for k, v in env.items() if v is not None}
    if src.is_remote:
        volume_mounts[src.mount_key] = {"bind": "/dvm/source", "mode": input_mode}
        helper_env["INPUT_PATH"] = "/dvm/source"
    if dst.is_remote:
        volume_mounts[dst.mount_key] = {"bind": "/dvm/dest", "mode": output_mode}
        helper_env["OUTPUT_PATH"] = "/dvm/dest"

    # Scrub remote flags so the helper doesn't loop trying to pivot again.
    for key in ("INPUT_VOLUME", "OUTPUT_VOLUME", "INPUT_HOST", "OUTPUT_HOST"):
        helper_env.pop(key, None)
    helper_env["OPERATION"] = subcommand.upper()

    mount_desc = ", ".join(
        f"{key}→{m['bind']} ({m['mode']})" for key, m in volume_mounts.items()
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
    except DockerError as exc:
        err_console.print(f"[bold red]Helper container error:[/] {exc}")
        raise typer.Exit(EXIT_CONFIG) from None

    raise typer.Exit(exit_code)
