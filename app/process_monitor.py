"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Background monitor for long subprocess operations (backup pipeline, restore
extraction). A single sampling loop serves two purposes from one signal:

1. Live progress — reads how many bytes the worker has processed and drives a
   byte-mode :class:`~progress.ProgressReporter` (rich bar on a TTY, throttled
   logs otherwise).
2. Stall watchdog — tracks an I/O activity counter; if it stops advancing for
   ``stall_timeout`` seconds, the worker is presumed hung and ``on_stall`` is
   invoked (the caller kills it). The watchdog only arms once it has observed I/O
   at least once, so a missing/unreadable ``/proc`` never triggers a false kill.

Progress/activity come from Linux ``/proc/<pid>/io`` (``rchar`` = bytes read via
syscalls, ``wchar`` = bytes written). On non-Linux or when ``/proc`` is
unreadable the monitor degrades gracefully: no progress advance, no watchdog.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from progress import ProgressReporter

logger = logging.getLogger("dvm")

DEFAULT_STALL_TIMEOUT = 300  # seconds; override via DVM_STALL_TIMEOUT (0 = off).
_SAMPLE_INTERVAL_S = 0.5


def stall_timeout_from_env() -> int:
    """Resolve the watchdog stall window from DVM_STALL_TIMEOUT (default 300s).

    ``0`` (or a bogus value) disables the watchdog; progress is still shown.
    """
    raw = os.environ.get("DVM_STALL_TIMEOUT")
    if raw is None:
        return DEFAULT_STALL_TIMEOUT
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid DVM_STALL_TIMEOUT=%r; using default %ds",
            raw,
            DEFAULT_STALL_TIMEOUT,
        )
        return DEFAULT_STALL_TIMEOUT


def read_proc_io(pid: int) -> tuple[int, int] | None:
    """Return ``(rchar, wchar)`` for a pid from /proc, or None if unavailable."""
    try:
        with open(f"/proc/{pid}/io") as f:
            rchar = wchar = 0
            for line in f:
                key, _, value = line.partition(":")
                key = key.strip()
                if key == "rchar":
                    rchar = int(value)
                elif key == "wchar":
                    wchar = int(value)
            return rchar, wchar
    except OSError, ValueError:
        return None


class ProcessMonitor:
    """Drive a byte progress bar + stall watchdog for a subprocess operation.

    Parameters
    ----------
    description:
        Progress bar / log label.
    total_bytes:
        Expected total for the progress bar (input size for backup, archive size
        for restore). Progress % is best-effort.
    progress_fn:
        Returns the current processed-byte count (monotonic), or None when
        momentarily unreadable (e.g. the worker just exited).
    activity_fn:
        Returns a monotonically-increasing I/O counter used for stall detection.
    on_stall:
        Called once if a stall is detected (the caller kills the worker).
    stall_timeout:
        Seconds of zero I/O progress before declaring a stall. 0 disables it.
    """

    def __init__(
        self,
        description: str,
        total_bytes: int,
        *,
        progress_fn: Callable[[], int | None],
        activity_fn: Callable[[], int | None],
        on_stall: Callable[[], None],
        stall_timeout: int | None = None,
        interval: float = _SAMPLE_INTERVAL_S,
    ):
        self.description = description
        self.total_bytes = total_bytes
        self._progress_fn = progress_fn
        self._activity_fn = activity_fn
        self._on_stall = on_stall
        self.stall_timeout = (
            stall_timeout_from_env() if stall_timeout is None else stall_timeout
        )
        self._interval = interval
        self._reporter = ProgressReporter(description, total_bytes, unit="bytes")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stalled = False

    def __enter__(self) -> ProcessMonitor:
        self._reporter.__enter__()
        self._thread = threading.Thread(
            target=self._run, name="dvm-monitor", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 4)
        # Snap the bar to 100% on a clean finish so it doesn't end mid-way.
        if not self.stalled:
            try:
                self._reporter.set_progress(self.total_bytes)
            except Exception:
                pass
        self._reporter.__exit__(exc_type, exc_val, exc_tb)

    def _run(self) -> None:
        armed = False
        last_activity = 0
        last_activity_at = time.monotonic()
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break
            try:
                current = self._progress_fn()
                if current is not None:
                    self._reporter.set_progress(current)

                activity = self._activity_fn()
                now = time.monotonic()
                if activity is not None and activity > last_activity:
                    last_activity = activity
                    last_activity_at = now
                    armed = True  # only watch for stalls once real I/O is seen

                if (
                    armed
                    and self.stall_timeout > 0
                    and now - last_activity_at >= self.stall_timeout
                ):
                    logger.error(
                        "%s: no I/O for %ds — declaring stall and terminating worker.",
                        self.description,
                        self.stall_timeout,
                    )
                    self.stalled = True
                    self._reporter.update_description(f"{self.description} [STALLED]")
                    try:
                        self._on_stall()
                    except Exception:
                        logger.exception("on_stall callback failed")
                    return
            except Exception:
                logger.debug("Monitor sample failed", exc_info=True)
