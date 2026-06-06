"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Helper-container pivot. When an operation is given ``--input-volume`` /
``--output-volume``, it can't touch the Docker volume directly; instead it spawns
a sibling "helper" container with the volume(s) mounted at /app/input_dir and
/app/output_dir, streams that container's logs, and exits with its status code.
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
from docker_client import DockerClient, DockerUnavailable


def _pivot_if_volumes(
    subcommand: str,
    env: dict[str, Any],
    *,
    input_volume: str | None,
    output_volume: str | None,
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

    volume_mounts: dict[str, dict[str, str]] = {}
    helper_env: dict[str, Any] = {k: v for k, v in env.items() if v is not None}
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
        raise typer.Exit(EXIT_CONFIG) from None

    raise typer.Exit(exit_code)
