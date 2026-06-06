"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Progress reporting that adapts to TTY / non-TTY contexts.

TTY + rich available:
- `ProgressReporter`: rich.Progress bar on stderr (reflows on SIGWINCH).
- `status`: rich.Status spinner on stderr.

Non-TTY (docker run -d, CI, redirection, file logs):
- `ProgressReporter`: throttled logger.info lines so log aggregators see
  progress without ANSI noise.
- `status`: no-op; callers emit their own "starting / finished" milestones.

Two units are supported:
- "files" (default): count-based bar; non-TTY logs at ~10% increments.
- "bytes": size-based bar with transfer speed + ETA; non-TTY logs are throttled
  by elapsed time AND percentage delta, and carry rate/ETA. Used by the live
  byte-level progress of backup/restore (see ``process_monitor``).

Rich output goes to stderr exclusively so stdout remains usable for piping.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger("dvm")

# Non-TTY byte-mode log throttle: emit at most every N seconds AND every M%.
_LOG_MIN_INTERVAL_S = 5.0
_LOG_MIN_PCT_DELTA = 5


def _is_tty() -> bool:
    try:
        return bool(sys.stderr.isatty())
    except Exception:
        return False


def _format_size(num: float) -> str:
    """Human-readable byte size (binary units). Mirrors docker_client.format_size."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PiB"


def _format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _try_build_progress(unit: str):
    try:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )
    except Exception:
        return None

    if unit == "bytes":
        amount_columns = (DownloadColumn(), TransferSpeedColumn())
    else:
        amount_columns = (MofNCompleteColumn(),)

    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        *amount_columns,
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=Console(stderr=True),
        transient=False,
        refresh_per_second=8,
    )


class ProgressReporter:
    """Reports progress for an operation with a known total.

    ``unit="files"`` (default) is count-based; ``unit="bytes"`` renders a
    size/speed bar and emits richer (rate + ETA) throttled logs off-TTY.
    """

    def __init__(
        self, description: str, total: int, *, unit: str = "files", enabled: bool = True
    ):
        self.description = description
        self.unit = unit
        self.total = max(0, int(total))
        self.enabled = enabled and self.total > 0
        self._processed = 0
        self._tty = _is_tty()
        self._progress = None
        self._task_id = None
        # files-mode throttle: log once per ~10% of count.
        self._log_step = max(1, self.total // 10) if self.total else 1
        # bytes-mode throttle state.
        self._start = time.monotonic()
        self._last_log_time = 0.0
        self._last_log_pct = -1

    def __enter__(self) -> ProgressReporter:
        self._start = time.monotonic()
        if not self.enabled or not self._tty:
            return self
        self._progress = _try_build_progress(self.unit)
        if self._progress is None:
            return self
        try:
            self._progress.start()
            self._task_id = self._progress.add_task(self.description, total=self.total)
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

    def update_description(self, description: str) -> None:
        """Update the bar label (e.g. to show a running file count in bytes mode)."""
        self.description = description
        if self._progress is not None and self._task_id is not None:
            try:
                self._progress.update(self._task_id, description=description)
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

        # Non-TTY throttled logging. Skip once we've already logged 100%.
        if previous >= self.total:
            return
        if self.unit == "bytes":
            self._log_bytes()
        else:
            self._log_files()

    def _log_files(self) -> None:
        if self._processed % self._log_step == 0 or self._processed >= self.total:
            pct = int(self._processed * 100 / self.total) if self.total else 100
            logger.info(
                "%s progress: %d/%d (%d%%)",
                self.description,
                min(self._processed, self.total),
                self.total,
                min(pct, 100),
            )

    def _log_bytes(self) -> None:
        done = min(self._processed, self.total)
        pct = int(done * 100 / self.total) if self.total else 100
        now = time.monotonic()
        done_total = done >= self.total
        # Throttle by time AND pct delta, but always emit the final 100% once.
        if not done_total and (
            now - self._last_log_time < _LOG_MIN_INTERVAL_S
            or pct - self._last_log_pct < _LOG_MIN_PCT_DELTA
        ):
            return
        self._last_log_time = now
        self._last_log_pct = pct
        elapsed = now - self._start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (self.total - done) / rate if rate > 0 and not done_total else 0
        logger.info(
            "%s progress: %s/%s (%d%%) at %s/s, elapsed %s, eta %s",
            self.description,
            _format_size(done),
            _format_size(self.total),
            min(pct, 100),
            _format_size(rate),
            _format_duration(elapsed),
            _format_duration(eta) if eta else "—",
        )

    def set_progress(self, current: int) -> None:
        """Set absolute progress (for callers driven by subprocess byte counts)."""
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
