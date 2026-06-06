"""Unit tests for the Config dataclass, validation, and logger wiring."""

from __future__ import annotations

import logging

import pytest

from config import (
    LOGGER_NAME,
    VALID_COMPRESSION,
    VALID_LOG_LEVELS,
    VALID_OPERATIONS,
    Config,
)

# ---------------------------------------------------------------------------
# Required fields + per-operation rules
# ---------------------------------------------------------------------------


def test_defaults_fail_without_backup_file_name():
    with pytest.raises(ValueError, match="BACKUP_FILE_NAME is required"):
        Config()


def test_copy_does_not_require_backup_file_name():
    cfg = Config(OPERATION="COPY")
    assert cfg.OPERATION == "COPY"
    assert cfg.BACKUP_FILE_NAME == ""


def test_rename_does_not_require_backup_file_name():
    cfg = Config(OPERATION="RENAME")
    assert cfg.OPERATION == "RENAME"


@pytest.mark.parametrize("op", sorted(VALID_OPERATIONS))
def test_all_valid_operations_accepted(op):
    kwargs = {"OPERATION": op}
    if op not in {"COPY", "RENAME"}:
        kwargs["BACKUP_FILE_NAME"] = "test"
    cfg = Config(**kwargs)
    assert op == cfg.OPERATION


def test_invalid_operation_rejected():
    with pytest.raises(ValueError, match="OPERATION must be one of"):
        Config(OPERATION="NUKE", BACKUP_FILE_NAME="x")


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", sorted(VALID_COMPRESSION))
def test_valid_compression_levels(level):
    cfg = Config(BACKUP_FILE_NAME="x", COMPRESSION=level)
    assert level == cfg.COMPRESSION


def test_default_compression_is_zstd():
    cfg = Config(BACKUP_FILE_NAME="x")
    assert cfg.COMPRESSION == "ZSTD"


def test_invalid_compression_rejected():
    with pytest.raises(ValueError, match="COMPRESSION must be one of"):
        Config(BACKUP_FILE_NAME="x", COMPRESSION="ULTRA")


@pytest.mark.parametrize("legacy", ["LOW", "MEDIUM", "HIGH"])
def test_legacy_compression_levels_rejected(legacy):
    """LOW / MEDIUM / HIGH (gz/bz2/xz) are no longer valid backup options."""
    with pytest.raises(ValueError, match="COMPRESSION must be one of"):
        Config(BACKUP_FILE_NAME="x", COMPRESSION=legacy)


def test_compression_normalised_to_upper_case():
    cfg = Config(BACKUP_FILE_NAME="x", COMPRESSION="zstd")
    assert cfg.COMPRESSION == "ZSTD"


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parity", [0, 1, 50, 100])
def test_parity_within_range(parity):
    cfg = Config(BACKUP_FILE_NAME="x", PARITY=parity)
    assert parity == cfg.PARITY


@pytest.mark.parametrize("parity", [-1, 101, 1000])
def test_parity_out_of_range_rejected(parity):
    with pytest.raises(ValueError, match="PARITY must be between 0 and 100"):
        Config(BACKUP_FILE_NAME="x", PARITY=parity)


def test_parity_non_integer_rejected():
    with pytest.raises(ValueError, match="PARITY must be an integer"):
        Config(BACKUP_FILE_NAME="x", PARITY="lots")


# ---------------------------------------------------------------------------
# Log level / output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", sorted(VALID_LOG_LEVELS))
def test_log_level_accepted(level):
    cfg = Config(BACKUP_FILE_NAME="x", LOG_LEVEL=level)
    assert level == cfg.LOG_LEVEL


def test_log_level_invalid_rejected():
    with pytest.raises(ValueError, match="LOG_LEVEL must be one of"):
        Config(BACKUP_FILE_NAME="x", LOG_LEVEL="VERBOSE")


def test_log_output_defaults_to_console_when_empty():
    cfg = Config(BACKUP_FILE_NAME="x", LOG_OUTPUT=[])
    assert cfg.LOG_OUTPUT == ["console"]


def test_log_output_filters_invalid_entries():
    cfg = Config(
        BACKUP_FILE_NAME="x",
        LOG_OUTPUT=["console", "bogus", "file"],
    )
    assert cfg.LOG_OUTPUT == ["console", "file"]


def test_log_output_deduplicates():
    cfg = Config(
        BACKUP_FILE_NAME="x",
        LOG_OUTPUT=["console", "console", "file"],
    )
    assert cfg.LOG_OUTPUT == ["console", "file"]


# ---------------------------------------------------------------------------
# Misc normalisation
# ---------------------------------------------------------------------------


def test_encryption_key_none_becomes_empty():
    cfg = Config(BACKUP_FILE_NAME="x", ENCRYPTION_KEY=None)
    assert cfg.ENCRYPTION_KEY == ""


def test_timestamp_empty_string_becomes_none():
    cfg = Config(BACKUP_FILE_NAME="x", TIMESTAMP="")
    assert cfg.TIMESTAMP is None


# ---------------------------------------------------------------------------
# GPG recipients (asymmetric encryption)
# ---------------------------------------------------------------------------


def test_gpg_recipients_default_empty():
    cfg = Config(BACKUP_FILE_NAME="x")
    assert cfg.GPG_RECIPIENTS == []


def test_gpg_recipients_stripped_and_filtered():
    cfg = Config(
        BACKUP_FILE_NAME="x",
        GPG_RECIPIENTS=[" alice@example.com ", "", "bob@example.com", "  "],
    )
    assert cfg.GPG_RECIPIENTS == ["alice@example.com", "bob@example.com"]


def test_symmetric_and_asymmetric_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        Config(
            BACKUP_FILE_NAME="x",
            ENCRYPTION_KEY="secret",
            GPG_RECIPIENTS=["alice@example.com"],
        )


def test_from_env_parses_gpg_recipients_csv(monkeypatch, clean_env):
    monkeypatch.setenv("BACKUP_FILE_NAME", "x")
    monkeypatch.setenv("GPG_RECIPIENTS", "alice@example.com, bob@example.com")
    cfg = Config.from_env()
    assert cfg.GPG_RECIPIENTS == ["alice@example.com", "bob@example.com"]


# ---------------------------------------------------------------------------
# ZSTD compression
# ---------------------------------------------------------------------------


def test_gz_compression_accepted():
    cfg = Config(BACKUP_FILE_NAME="x", COMPRESSION="GZ")
    assert cfg.COMPRESSION == "GZ"


def test_zstd_compression_case_insensitive():
    cfg = Config(BACKUP_FILE_NAME="x", COMPRESSION="zstd")
    assert cfg.COMPRESSION == "ZSTD"


# ---------------------------------------------------------------------------
# Detached signing
# ---------------------------------------------------------------------------


def test_sign_key_default_empty():
    cfg = Config(BACKUP_FILE_NAME="x")
    assert cfg.SIGN_KEY == ""
    assert cfg.SIGN_KEY_PASSPHRASE == ""


def test_sign_key_normalised():
    cfg = Config(
        BACKUP_FILE_NAME="x",
        SIGN_KEY="  ABCDEF1234567890  ",
        SIGN_KEY_PASSPHRASE="pwd",
    )
    assert cfg.SIGN_KEY == "ABCDEF1234567890"
    assert cfg.SIGN_KEY_PASSPHRASE == "pwd"


def test_from_env_reads_sign_key(monkeypatch, clean_env):
    monkeypatch.setenv("BACKUP_FILE_NAME", "x")
    monkeypatch.setenv("SIGN_KEY", "FPR")
    monkeypatch.setenv("SIGN_KEY_PASSPHRASE", "pwd")
    cfg = Config.from_env()
    assert cfg.SIGN_KEY == "FPR"
    assert cfg.SIGN_KEY_PASSPHRASE == "pwd"


def test_operation_normalised_to_upper_case():
    cfg = Config(OPERATION="backup", BACKUP_FILE_NAME="x")
    assert cfg.OPERATION == "BACKUP"


# ---------------------------------------------------------------------------
# Config.from_env
# ---------------------------------------------------------------------------


class TestConfigFromEnv:
    def test_reads_all_supported_fields(self, monkeypatch, tmp_path, clean_env):
        monkeypatch.setenv("OPERATION", "BACKUP")
        monkeypatch.setenv("BACKUP_FILE_NAME", "env-bak")
        monkeypatch.setenv("COMPRESSION", "ZSTD")
        monkeypatch.setenv("PARITY", "25")
        monkeypatch.setenv("ENCRYPTION_KEY", "secret")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("LOG_OUTPUT", "console,file")
        monkeypatch.setenv("LOGS_PATH", str(tmp_path / "logs"))

        cfg = Config.from_env()

        assert cfg.OPERATION == "BACKUP"
        assert cfg.BACKUP_FILE_NAME == "env-bak"
        assert cfg.COMPRESSION == "ZSTD"
        assert cfg.PARITY == 25
        assert cfg.ENCRYPTION_KEY == "secret"
        assert cfg.LOG_LEVEL == "DEBUG"
        assert cfg.LOG_OUTPUT == ["console", "file"]

    def test_overrides_win_over_env(self, monkeypatch, clean_env):
        monkeypatch.setenv("BACKUP_FILE_NAME", "from-env")
        cfg = Config.from_env(BACKUP_FILE_NAME="override")
        assert cfg.BACKUP_FILE_NAME == "override"

    def test_no_color_forces_use_colors_false(self, monkeypatch, clean_env):
        monkeypatch.setenv("BACKUP_FILE_NAME", "x")
        monkeypatch.setenv("NO_COLOR", "1")
        cfg = Config.from_env()
        assert cfg.USE_COLORS is False

    def test_copy_overwrite_parses_truthy_strings(self, monkeypatch, clean_env):
        monkeypatch.setenv("OPERATION", "COPY")
        monkeypatch.setenv("COPY_OVERWRITE", "no")
        cfg = Config.from_env()
        assert cfg.COPY_OVERWRITE is False

        monkeypatch.setenv("COPY_OVERWRITE", "yes")
        cfg = Config.from_env()
        assert cfg.COPY_OVERWRITE is True


# ---------------------------------------------------------------------------
# Logger wiring
# ---------------------------------------------------------------------------


class TestConfigLogger:
    def test_setup_logger_attaches_handlers_for_file_and_json(
        self, tmp_path, reset_dvm_logger
    ):
        cfg = Config(
            BACKUP_FILE_NAME="x",
            LOG_OUTPUT=["file", "json_file"],
            LOGS_PATH=str(tmp_path / "logs"),
        )
        log = cfg.setup_logger()
        assert log.name == LOGGER_NAME
        assert len(log.handlers) == 2
        assert all(isinstance(h, logging.FileHandler) for h in log.handlers)

    def test_setup_logger_console_handler_present(self, tmp_path, reset_dvm_logger):
        cfg = Config(
            BACKUP_FILE_NAME="x",
            LOG_OUTPUT=["console"],
            LOGS_PATH=str(tmp_path / "logs"),
        )
        log = cfg.setup_logger()
        assert len(log.handlers) == 1

    def test_get_logger_lazy_initialises(self, tmp_path, reset_dvm_logger):
        cfg = Config(BACKUP_FILE_NAME="x", LOGS_PATH=str(tmp_path / "logs"))
        log = cfg.get_logger()
        assert len(log.handlers) >= 1

    def test_logs_path_unwritable_falls_back_silently(
        self, tmp_path, reset_dvm_logger, monkeypatch
    ):
        bad = tmp_path / "readonly"
        bad.mkdir()
        monkeypatch.setattr(
            "os.makedirs",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("read only")),
        )
        cfg = Config(BACKUP_FILE_NAME="x", LOGS_PATH=str(bad / "x"))
        assert cfg.LOGS_PATH is None
