"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

Per-operation exit codes (through the real CLI).

Each distinct failure mode must surface as its own process exit code (the
tens digit identifies the operation, the units the reason). These run the typer
app in-process via CliRunner — no Docker daemon needed — and assert the code.
"""

from __future__ import annotations

import cli
from cli_shared import (
    EXIT_BACKUP_INPUT_NOT_FOUND,
    EXIT_COPY_INPUT_NOT_FOUND,
    EXIT_COPY_OVERWRITE_REFUSED,
    EXIT_OK,
    EXIT_RESTORE_BACKUP_NOT_FOUND,
    EXIT_RESTORE_DECRYPT_FAILED,
    OperationError,
)
from config import Config
from operations.backup_files import BackupManager
from operations.docker_volume_manager import Docker_Volume_Manager


def _seed(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text("payload")
    out = tmp_path / "out"
    out.mkdir()
    return src, out


def _run(**cfg_kwargs):
    """Run an operation through the dispatcher; return 0 or the OperationError code.

    Used where CliRunner's stream capture clashes with library threads (gnupg)
    or where a path is only reachable by patching an internal decision.
    """
    cfg = Config(LOG_OUTPUT=["console"], LOGS_PATH=None, **cfg_kwargs)
    try:
        return 0 if Docker_Volume_Manager(cfg).run() else 4
    except OperationError as exc:
        return exc.code


# ---- BACKUP 1x ----------------------------------------------------------------


def test_backup_input_not_found(tmp_path, cli_runner, clean_env, reset_dvm_logger):
    out = tmp_path / "out"
    out.mkdir()
    result = cli_runner.invoke(
        cli.app,
        ["backup", "-n", "x", "-i", str(tmp_path / "nope"), "-o", str(out)],
    )
    assert result.exit_code == EXIT_BACKUP_INPUT_NOT_FOUND


# ---- RESTORE 2x ---------------------------------------------------------------


def test_restore_backup_not_found(tmp_path, cli_runner, clean_env, reset_dvm_logger):
    src, out = _seed(tmp_path)  # src has no backup layout for "ghost"
    result = cli_runner.invoke(
        cli.app, ["restore", "-n", "ghost", "-i", str(src), "-o", str(out)]
    )
    assert result.exit_code == EXIT_RESTORE_BACKUP_NOT_FOUND


def test_restore_wrong_password(tmp_path, clean_env, reset_dvm_logger):
    # Dispatcher-level: gnupg spawns reader threads that race CliRunner's closed
    # stream capture, so exercise run() directly and assert the typed code.
    src, out = _seed(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    BackupManager(
        vol_name="enc",
        input_path=str(src),
        output_path=str(store),
        compression="gz",
        password="right",
    ).compress_and_encrypt_pipeline()

    assert (
        _run(
            OPERATION="RESTORE",
            BACKUP_FILE_NAME="enc",
            INPUT_PATH=str(store),
            OUTPUT_PATH=str(out),
            ENCRYPTION_KEY="wrong",
        )
        == EXIT_RESTORE_DECRYPT_FAILED
    )


# ---- COPY 4x ------------------------------------------------------------------


def test_copy_input_not_found(tmp_path, cli_runner, clean_env, reset_dvm_logger):
    out = tmp_path / "out"
    out.mkdir()
    result = cli_runner.invoke(
        cli.app, ["copy", "-i", str(tmp_path / "nope"), "-o", str(out)]
    )
    assert result.exit_code == EXIT_COPY_INPUT_NOT_FOUND


def test_copy_overwrite_refused(tmp_path, monkeypatch, clean_env, reset_dvm_logger):
    # The overwrite decision defaults to "overwrite" in non-interactive runs, so
    # force a refusal (as an interactive "no" would) to reach the 42 path.
    src, out = _seed(tmp_path)
    (out / "existing.txt").write_text("keep me")
    import operations.copy_files as copy_files

    monkeypatch.setattr(copy_files, "should_overwrite", lambda dest, overwrite: False)
    assert (
        _run(OPERATION="COPY", INPUT_PATH=str(src), OUTPUT_PATH=str(out))
        == EXIT_COPY_OVERWRITE_REFUSED
    )


def test_copy_success_is_zero(tmp_path, cli_runner, clean_env, reset_dvm_logger):
    src, out = _seed(tmp_path)
    result = cli_runner.invoke(cli.app, ["copy", "-i", str(src), "-o", str(out)])
    assert result.exit_code == EXIT_OK
