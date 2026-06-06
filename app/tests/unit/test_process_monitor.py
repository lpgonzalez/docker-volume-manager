"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Unit tests for the subprocess progress + stall-watchdog monitor.
"""

from __future__ import annotations

import time

from process_monitor import (
    DEFAULT_STALL_TIMEOUT,
    ProcessMonitor,
    read_proc_io,
    stall_timeout_from_env,
)

# --- env parsing -----------------------------------------------------------


def test_stall_timeout_default(monkeypatch):
    monkeypatch.delenv("DVM_STALL_TIMEOUT", raising=False)
    assert stall_timeout_from_env() == DEFAULT_STALL_TIMEOUT


def test_stall_timeout_explicit(monkeypatch):
    monkeypatch.setenv("DVM_STALL_TIMEOUT", "60")
    assert stall_timeout_from_env() == 60


def test_stall_timeout_zero_disables(monkeypatch):
    monkeypatch.setenv("DVM_STALL_TIMEOUT", "0")
    assert stall_timeout_from_env() == 0


def test_stall_timeout_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("DVM_STALL_TIMEOUT", "soon")
    assert stall_timeout_from_env() == DEFAULT_STALL_TIMEOUT


# --- /proc/<pid>/io reader -------------------------------------------------


def test_read_proc_io_self_returns_counters():
    io = read_proc_io("self")  # /proc/self/io exists on Linux
    if io is None:  # non-Linux / restricted — nothing to assert
        return
    rchar, wchar = io
    assert rchar >= 0 and wchar >= 0


def test_read_proc_io_missing_pid_returns_none():
    assert read_proc_io(-1) is None


# --- watchdog behaviour ----------------------------------------------------


def _run_monitor(progress_fn, activity_fn, *, stall_timeout, sleep):
    killed = {"hit": False}

    def on_stall():
        killed["hit"] = True

    mon = ProcessMonitor(
        "test",
        1000,
        progress_fn=progress_fn,
        activity_fn=activity_fn,
        on_stall=on_stall,
        stall_timeout=stall_timeout,
        interval=0.05,
    )
    with mon:
        time.sleep(sleep)
    return mon, killed["hit"]


def test_monitor_detects_stall():
    # Activity reports a constant non-zero value: it arms the watchdog on the
    # first sample, then never advances -> stall.
    mon, killed = _run_monitor(
        progress_fn=lambda: 0,
        activity_fn=lambda: 10,
        stall_timeout=1,
        sleep=1.6,
    )
    assert mon.stalled is True
    assert killed is True


def test_monitor_no_stall_while_progressing():
    counter = {"n": 0}

    def activity():
        counter["n"] += 100
        return counter["n"]

    mon, killed = _run_monitor(
        progress_fn=lambda: counter["n"],
        activity_fn=activity,
        stall_timeout=1,
        sleep=1.6,
    )
    assert mon.stalled is False
    assert killed is False


def test_monitor_never_arms_without_activity():
    # activity stuck at 0 (e.g. /proc unreadable) must NOT trigger a false kill.
    mon, killed = _run_monitor(
        progress_fn=lambda: None,
        activity_fn=lambda: 0,
        stall_timeout=1,
        sleep=1.6,
    )
    assert mon.stalled is False
    assert killed is False
