"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Paged, numbered selection prompt for the interactive wizard.

Renders a candidate list as a numbered rich table and lets the user pick by
global number or by name (with TAB completion). When the list spans more than
one page, ``n``/``p`` navigate. Blank input returns the default; with
``allow_custom`` a typed value that isn't in the list is returned verbatim (used
for the backup-name prompt, where a brand-new name is valid).
"""

from __future__ import annotations

from collections.abc import Callable

from rich.table import Table

import completion
from cli_shared import console, err_console


def _render_page(
    chunk: list[str],
    start: int,
    page: int,
    pages: int,
    total: int,
    note_fn: Callable[[str], str] | None,
    title: str | None,
) -> None:
    table = Table(show_header=True, header_style="bold cyan", title=title)
    table.add_column("#", justify="right", style="dim")
    table.add_column("name")
    if note_fn:
        table.add_column("", style="dim")
    for i, item in enumerate(chunk):
        number = str(start + i + 1)
        if note_fn:
            table.add_row(number, item, note_fn(item) or "")
        else:
            table.add_row(number, item)
    console.print(table)
    if pages > 1:
        console.print(
            f"[dim]Page {page + 1}/{pages} · {total} items · "
            "type [b]n[/b]/[b]p[/b] to navigate[/]"
        )


def select_paged(
    prompt: str,
    items: list[str],
    *,
    page_size: int = 20,
    default: str | None = None,
    allow_custom: bool = False,
    note_fn: Callable[[str], str] | None = None,
    title: str | None = None,
) -> str | None:
    """Numbered, paged selection.

    Returns the chosen item, ``default`` on blank input, a custom string when
    ``allow_custom`` and the entry isn't in the list, or ``None`` when the list
    is empty and no custom entry is allowed.
    """
    if not items:
        if allow_custom:
            raw = completion.ask(
                prompt, completion.words([]), default=default or ""
            ).strip()
            return raw or default
        return default

    total = len(items)
    pages = (total + page_size - 1) // page_size
    nav = ["n", "p"] if pages > 1 else []
    completer = completion.words(items + nav)
    page = 0
    while True:
        start = page * page_size
        chunk = items[start : start + page_size]
        _render_page(chunk, start, page, pages, total, note_fn, title)

        hint = "number or name"
        if pages > 1:
            hint += ", [n]ext/[p]rev"
        raw = completion.ask(
            f"{prompt} ({hint})", completer, default=default or ""
        ).strip()

        if not raw:
            return default
        low = raw.lower()
        if pages > 1 and low in {"n", "next"}:
            page = (page + 1) % pages
            continue
        if pages > 1 and low in {"p", "prev"}:
            page = (page - 1) % pages
            continue
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < total:
                return items[idx]
            err_console.print(f"[red]Number out of range (1-{total}).[/]")
            continue
        if raw in items:
            return raw
        if allow_custom:
            return raw
        err_console.print(f"[red]No match for {raw!r}. Pick a number or a name.[/]")
