"""Unit tests for progress.ProgressReporter and progress.status()."""

from __future__ import annotations

import pytest

from progress import ProgressReporter, status


@pytest.fixture
def no_tty(monkeypatch):
    """Force _is_tty() to False for deterministic non-TTY behaviour."""
    monkeypatch.setattr("progress._is_tty", lambda: False)


# ---------------------------------------------------------------------------
# Enablement
# ---------------------------------------------------------------------------


def test_disabled_when_total_zero():
    pr = ProgressReporter("test", total=0)
    assert pr.enabled is False


def test_disabled_explicitly():
    pr = ProgressReporter("test", total=10, enabled=False)
    assert pr.enabled is False


def test_enabled_with_positive_total():
    pr = ProgressReporter("test", total=10)
    assert pr.enabled is True


def test_negative_total_disables():
    pr = ProgressReporter("test", total=-5)
    assert pr.total == 0
    assert pr.enabled is False


# ---------------------------------------------------------------------------
# Non-TTY: throttled logger fallback
# ---------------------------------------------------------------------------


def test_non_tty_emits_throttled_logs(no_tty, caplog, reset_dvm_logger):
    caplog.set_level("INFO", logger="dvm")
    pr = ProgressReporter("Copying", total=100)
    with pr:
        for _ in range(100):
            pr.advance()
    logs = [r for r in caplog.records if "progress:" in r.getMessage()]
    # Roughly one log per 10% increment; be generous on bounds.
    assert 9 <= len(logs) <= 11
    # Final line must reach 100%
    assert any("100%" in r.getMessage() for r in logs)


def test_non_tty_stops_logging_after_total_reached(no_tty, caplog, reset_dvm_logger):
    caplog.set_level("INFO", logger="dvm")
    # Once processed >= total, further over-advances must not re-emit progress
    # lines (they'd spam file/JSON log handlers).
    pr = ProgressReporter("Copying", total=100)
    with pr:
        for _ in range(300):
            pr.advance()
    logs = [r for r in caplog.records if "progress:" in r.getMessage()]
    # Expected: 10 logs (at processed=10,20,...,100). Allow a small margin for
    # rounding edges.
    assert 9 <= len(logs) <= 11


def test_disabled_reporter_emits_nothing(no_tty, caplog, reset_dvm_logger):
    caplog.set_level("INFO", logger="dvm")
    pr = ProgressReporter("nop", total=0)
    with pr:
        for _ in range(100):
            pr.advance()
    assert [r for r in caplog.records if "progress:" in r.getMessage()] == []


# ---------------------------------------------------------------------------
# set_progress (used for par2 subprocess %)
# ---------------------------------------------------------------------------


def test_set_progress_advances_forward(no_tty):
    pr = ProgressReporter("Par2", total=100)
    with pr:
        pr.set_progress(25)
        pr.set_progress(75)
    assert pr._processed == 75


def test_set_progress_does_not_regress(no_tty):
    pr = ProgressReporter("Par2", total=100)
    with pr:
        pr.set_progress(50)
        pr.set_progress(30)
    assert pr._processed == 50


def test_set_progress_clamped_to_total(no_tty):
    pr = ProgressReporter("Par2", total=100)
    with pr:
        pr.set_progress(250)
    assert pr._processed == 100


def test_set_progress_on_disabled_is_noop(no_tty):
    pr = ProgressReporter("Par2", total=0)
    with pr:
        pr.set_progress(50)
    assert pr._processed == 0


# ---------------------------------------------------------------------------
# Negative / zero advances
# ---------------------------------------------------------------------------


def test_advance_zero_is_noop(no_tty, caplog, reset_dvm_logger):
    caplog.set_level("INFO", logger="dvm")
    pr = ProgressReporter("x", total=10)
    with pr:
        pr.advance(0)
    assert [r for r in caplog.records if "progress:" in r.getMessage()] == []


def test_advance_negative_is_noop(no_tty):
    pr = ProgressReporter("x", total=10)
    with pr:
        pr.advance(-3)
    assert pr._processed == 0


# ---------------------------------------------------------------------------
# status() context manager
# ---------------------------------------------------------------------------


def test_status_is_noop_without_tty(no_tty):
    # Must not raise, must not require a rich Live renderer.
    with status("Working"):
        pass


def test_status_handles_rich_failure_silently(monkeypatch):
    """If rich.Console.status explodes for any reason the context still yields."""
    monkeypatch.setattr("progress._is_tty", lambda: True)
    import rich.console

    class Boom:
        def status(self, *_, **__):
            raise RuntimeError("simulated")

    monkeypatch.setattr(rich.console, "Console", lambda *a, **kw: Boom())
    with status("Working"):
        pass
