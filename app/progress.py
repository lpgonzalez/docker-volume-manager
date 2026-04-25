"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

"""
Progress reporting that adapts to TTY / non-TTY contexts.

TTY + rich available:
- `ProgressReporter`: rich.Progress bar on stderr (reflows on SIGWINCH).
- `status`: rich.Status spinner on stderr.

Non-TTY (docker run -d, CI, redirection, file logs):
- `ProgressReporter`: throttled logger.info lines at ~10% increments so log
  aggregators still see progress without ANSI noise.
- `status`: no-op; callers emit their own "starting / finished" milestones.

Rich output goes to stderr exclusively so stdout remains usable for piping.
"""

import logging
import sys
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger("dvm")


def _is_tty() -> bool:
    try:
        return bool(sys.stderr.isatty())
    except Exception:
        return False


def _try_build_progress():
    try:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )
    except Exception:
        return None

    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=Console(stderr=True),
        transient=False,
        refresh_per_second=8,
    )


class ProgressReporter:
    """Reports progress for operations with a known total count."""

    def __init__(self, description: str, total: int, *, enabled: bool = True):
        self.description = description
        self.total = max(0, int(total))
        self.enabled = enabled and self.total > 0
        self._processed = 0
        self._tty = _is_tty()
        self._progress = None
        self._task_id = None
        self._log_step = max(1, self.total // 10) if self.total else 1

    def __enter__(self) -> "ProgressReporter":
        if not self.enabled or not self._tty:
            return self
        self._progress = _try_build_progress()
        if self._progress is None:
            return self
        try:
            self._progress.start()
            self._task_id = self._progress.add_task(
                self.description, total=self.total
            )
        except Exception:
            self._progress = None
            self._task_id = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._progress is not None:
            try:
                self._progress.stop()
            except Exception:
                pass

    def advance(self, step: int = 1) -> None:
        if not self.enabled or step <= 0:
            return
        previous = self._processed
        self._processed += step

        if self._progress is not None and self._task_id is not None:
            try:
                self._progress.advance(self._task_id, step)
            except Exception:
                pass
            return

        # Non-TTY throttled logging. Skip emitting once we've already logged
        # 100% — further over-advances shouldn't spam the file/JSON handlers.
        if previous >= self.total:
            return
        if (
            self._processed % self._log_step == 0
            or self._processed >= self.total
        ):
            pct = int(self._processed * 100 / self.total) if self.total else 100
            logger.info(
                "%s progress: %d/%d (%d%%)",
                self.description,
                min(self._processed, self.total),
                self.total,
                min(pct, 100),
            )

    def set_progress(self, current: int) -> None:
        """Set absolute progress (for callers driven by subprocess percentages)."""
        if not self.enabled:
            return
        current = max(0, min(int(current), self.total))
        delta = current - self._processed
        if delta > 0:
            self.advance(delta)


@contextmanager
def status(description: str) -> Iterator[None]:
    """Spinner for long opaque operations. No-op in non-TTY contexts."""
    if not _is_tty():
        yield
        return

    try:
        from rich.console import Console
    except Exception:
        yield
        return

    try:
        with Console(stderr=True).status(f"[bold blue]{description}", spinner="dots"):
            yield
    except Exception:
        # Never block the underlying operation if rich fails.
        yield
