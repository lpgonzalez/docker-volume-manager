"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

TAB completion for the interactive wizard, built on the stdlib ``readline``
module — no extra dependency.

rich's ``Prompt.ask`` renders the prompt then calls the builtin ``input()``,
which transparently uses ``readline`` for line editing and completion whenever
the module has been imported. So we just install a completer for the duration of
a single prompt and tear it down afterwards. Everything degrades to a no-op when
``readline`` is unavailable (e.g. a Python build without it), so callers can wrap
every prompt unconditionally.

Usage::

    from completion import ask, words, paths

    op = ask("Select operation", words(["backup", "restore"]), choices=[...])
    src = ask("Source path", paths(), default="/dvm/source")
"""

from __future__ import annotations

import glob
import os
from collections.abc import Callable, Iterable

from rich.prompt import Prompt

try:
    import readline
except ImportError:  # pragma: no cover - readline missing is a degraded build
    readline = None  # type: ignore[assignment]

# GNU readline binds completion to TAB with "tab: complete"; the libedit shim
# (shipped on some platforms) needs a different incantation.
_BIND = (
    "bind ^I rl_complete"
    if readline is not None and "libedit" in (readline.__doc__ or "")
    else "tab: complete"
)

# Default word delimiters: whitespace only, so a single word completes whole.
_WORD_DELIMS = " \t\n"
# For comma-separated lists each token completes independently.
_CSV_DELIMS = " \t\n,"
# Paths are taken as the entire line buffer (no delimiters), so slashes and
# spaces inside a path don't split the token.
_PATH_DELIMS = ""

Matcher = Callable[[str, int], "str | None"]


class Completion:
    """A readline completer scoped to a single prompt via ``with``/``ask``.

    Restores whatever completer/delimiters were active before, so installing one
    prompt's completer never leaks into the next.
    """

    def __init__(self, fn: Matcher, delims: str):
        self.fn = fn
        self.delims = delims

    def __enter__(self) -> Completion:
        if readline is None:
            return self
        self._prev_fn = readline.get_completer()
        self._prev_delims = readline.get_completer_delims()
        readline.set_completer(self.fn)
        readline.set_completer_delims(self.delims)
        readline.parse_and_bind(_BIND)
        return self

    def __exit__(self, *exc: object) -> None:
        if readline is None:
            return
        readline.set_completer(self._prev_fn)
        readline.set_completer_delims(self._prev_delims)


def _word_matcher(options: Iterable[str]) -> Matcher:
    opts = list(options)

    def match(text: str, state: int) -> str | None:
        hits = [o for o in opts if o.startswith(text)]
        return hits[state] if state < len(hits) else None

    return match


def _path_matcher(text: str, state: int) -> str | None:
    expanded = os.path.expanduser(text or "")
    try:
        raw = sorted(glob.glob(expanded + "*"))
    except OSError:
        raw = []
    # Append a separator to directories so the next TAB descends into them.
    hits = [p + os.sep if os.path.isdir(p) else p for p in raw]
    return hits[state] if state < len(hits) else None


def _subdir_names(base_dir: str) -> list[str]:
    try:
        return sorted(
            name
            for name in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, name))
        )
    except OSError:
        return []


def words(options: Iterable[str]) -> Completion:
    """Complete from a fixed list of words (operation names, log levels, ...)."""
    return Completion(_word_matcher(options), _WORD_DELIMS)


def csv_words(options: Iterable[str]) -> Completion:
    """Complete each comma-separated token from a fixed list (e.g. log outputs)."""
    return Completion(_word_matcher(options), _CSV_DELIMS)


def paths() -> Completion:
    """Complete filesystem paths from the current line buffer."""
    return Completion(_path_matcher, _PATH_DELIMS)


def names_in(base_dir: str) -> Completion:
    """Complete from the immediate sub-directory names of ``base_dir``.

    Used for backup base names, which live as ``<base_dir>/<name>/<timestamp>/``.
    Returns an empty completer when the directory can't be read.
    """
    return Completion(_word_matcher(_subdir_names(base_dir)), _WORD_DELIMS)


def ask(prompt: str, completion: Completion, **kwargs: object) -> str:
    """``rich.Prompt.ask`` with ``completion`` installed for this prompt only."""
    with completion:
        return Prompt.ask(prompt, **kwargs)
