"""Unit tests for the CLI layer — argument parsing & validation.

Operation runners (`_run_backup`, `_run_restore`, ...) are patched so tests
exercise only the parsing/dispatch/pivot-guard logic, never the underlying
tar/gpg/par2/Docker machinery.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import cli
import runners

# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


class TestBackup:
    def test_missing_name_fails(self, cli_runner, clean_env):
        result = cli_runner.invoke(cli.app, ["backup"])
        assert result.exit_code != 0

    def test_invalid_compression_fails(self, cli_runner, clean_env):
        # Compression validation lives inside _build_config (Config.__post_init__);
        # mock _run_operation as a safety net so we never actually execute the op
        # if validation unexpectedly passes.
        with patch.object(runners, "_run_operation") as mock_op:
            result = cli_runner.invoke(cli.app, ["backup", "-n", "x", "-c", "BOGUS"])
        assert result.exit_code == cli.EXIT_VALIDATION
        mock_op.assert_not_called()

    def test_parity_above_range_fails(self, cli_runner, clean_env):
        result = cli_runner.invoke(cli.app, ["backup", "-n", "x", "-p", "150"])
        assert result.exit_code != 0

    def test_parity_negative_fails(self, cli_runner, clean_env):
        result = cli_runner.invoke(cli.app, ["backup", "-n", "x", "-p", "-5"])
        assert result.exit_code != 0

    def test_valid_invocation_calls_runner(self, cli_runner, clean_env):
        with patch.object(cli, "_run_backup") as mock_run:
            result = cli_runner.invoke(
                cli.app,
                ["backup", "-n", "mybak", "-c", "ZSTD", "-p", "30"],
            )
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["name"] == "mybak"
        assert kwargs["compression"] == "ZSTD"
        assert kwargs["parity"] == 30
        assert result.exit_code == 0

    def test_default_compression_is_zstd(self, cli_runner, clean_env):
        with patch.object(cli, "_run_backup") as mock_run:
            cli_runner.invoke(cli.app, ["backup", "-n", "x"])
        assert mock_run.call_args.kwargs["compression"] == "ZSTD"

    @pytest.mark.parametrize("legacy", ["LOW", "MEDIUM", "HIGH"])
    def test_legacy_compression_aliases_rejected(self, cli_runner, clean_env, legacy):
        with patch.object(runners, "_run_operation") as mock_op:
            result = cli_runner.invoke(cli.app, ["backup", "-n", "x", "-c", legacy])
        assert result.exit_code == cli.EXIT_VALIDATION
        mock_op.assert_not_called()

    def test_env_vars_drive_defaults(self, cli_runner, monkeypatch, clean_env):
        monkeypatch.setenv("BACKUP_FILE_NAME", "env-bak")
        monkeypatch.setenv("COMPRESSION", "GZ")
        with patch.object(cli, "_run_backup") as mock_run:
            cli_runner.invoke(cli.app, ["backup"])
        kwargs = mock_run.call_args.kwargs
        assert kwargs["name"] == "env-bak"
        assert kwargs["compression"] == "GZ"

    def test_cli_flag_beats_env(self, cli_runner, monkeypatch, clean_env):
        monkeypatch.setenv("BACKUP_FILE_NAME", "env-bak")
        with patch.object(cli, "_run_backup") as mock_run:
            cli_runner.invoke(cli.app, ["backup", "-n", "cli-bak"])
        assert mock_run.call_args.kwargs["name"] == "cli-bak"

    def test_invalid_log_output_fails(self, cli_runner, clean_env):
        result = cli_runner.invoke(
            cli.app,
            ["backup", "-n", "x", "--log-output", "console,bogus"],
        )
        assert result.exit_code != 0

    def test_recipients_forwarded_to_runner(self, cli_runner, clean_env):
        with patch.object(cli, "_run_backup") as mock_run:
            cli_runner.invoke(
                cli.app,
                [
                    "backup",
                    "-n",
                    "x",
                    "-r",
                    "alice@example.com",
                    "-r",
                    "bob@example.com",
                ],
            )
        assert mock_run.call_args.kwargs["recipients"] == [
            "alice@example.com",
            "bob@example.com",
        ]

    def test_symmetric_and_recipients_are_mutually_exclusive(
        self, cli_runner, clean_env
    ):
        with patch.object(cli, "_run_backup") as mock_run:
            result = cli_runner.invoke(
                cli.app,
                [
                    "backup",
                    "-n",
                    "x",
                    "-k",
                    "secret",
                    "-r",
                    "alice@example.com",
                ],
            )
        assert result.exit_code == cli.EXIT_VALIDATION
        mock_run.assert_not_called()

    def test_zstd_compression_accepted(self, cli_runner, clean_env):
        with patch.object(cli, "_run_backup") as mock_run:
            result = cli_runner.invoke(cli.app, ["backup", "-n", "x", "-c", "ZSTD"])
        assert result.exit_code == 0
        assert mock_run.call_args.kwargs["compression"] == "ZSTD"

    def test_recipient_key_file_imports_and_passes_fingerprints(
        self, cli_runner, clean_env, tmp_path
    ):
        key_file = tmp_path / "alice.asc"
        key_file.write_text("ARMORED-BLOCK")

        with (
            patch.object(cli, "setup_recipient_keyring") as mock_setup,
            patch.object(cli, "tear_down_keyring") as mock_tear_down,
            patch.object(cli, "_run_backup") as mock_run,
        ):
            mock_setup.return_value = (str(tmp_path / "fake-homedir"), ["FPR_ABC"])
            cli_runner.invoke(cli.app, ["backup", "-n", "x", "-K", str(key_file)])

        mock_setup.assert_called_once_with([str(key_file)])
        assert mock_run.call_args.kwargs["recipients"] == ["FPR_ABC"]
        # Cleanup must run regardless of success.
        mock_tear_down.assert_called_once()

    def test_recipient_key_file_mutex_with_passphrase(
        self, cli_runner, clean_env, tmp_path
    ):
        key_file = tmp_path / "alice.asc"
        key_file.write_text("ARMORED-BLOCK")
        with patch.object(cli, "_run_backup") as mock_run:
            result = cli_runner.invoke(
                cli.app,
                [
                    "backup",
                    "-n",
                    "x",
                    "-k",
                    "secret",
                    "-K",
                    str(key_file),
                ],
            )
        assert result.exit_code == cli.EXIT_VALIDATION
        mock_run.assert_not_called()

    def test_recipient_key_file_rejected_with_volume_flags(
        self, cli_runner, clean_env, tmp_path
    ):
        key_file = tmp_path / "alice.asc"
        key_file.write_text("ARMORED-BLOCK")
        with (
            patch.object(cli, "_run_backup") as mock_run,
            patch.object(cli, "setup_recipient_keyring") as mock_setup,
        ):
            result = cli_runner.invoke(
                cli.app,
                [
                    "backup",
                    "-n",
                    "x",
                    "-K",
                    str(key_file),
                    "--input-volume",
                    "src",
                ],
            )
        assert result.exit_code == cli.EXIT_VALIDATION
        mock_setup.assert_not_called()
        mock_run.assert_not_called()

    def test_sign_key_forwarded_to_runner(self, cli_runner, clean_env):
        with patch.object(cli, "_run_backup") as mock_run:
            cli_runner.invoke(
                cli.app,
                [
                    "backup",
                    "-n",
                    "x",
                    "--sign-key",
                    "ABCD1234",
                    "--sign-key-passphrase",
                    "secret",
                ],
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs["sign_key"] == "ABCD1234"
        assert kwargs["sign_key_passphrase"] == "secret"

    def test_sign_key_from_env(self, cli_runner, monkeypatch, clean_env):
        monkeypatch.setenv("SIGN_KEY", "ENV_FPR")
        with patch.object(cli, "_run_backup") as mock_run:
            cli_runner.invoke(cli.app, ["backup", "-n", "x"])
        assert mock_run.call_args.kwargs["sign_key"] == "ENV_FPR"


# ---------------------------------------------------------------------------
# restore / verify / copy
# ---------------------------------------------------------------------------


class TestRestore:
    def test_missing_name_fails(self, cli_runner, clean_env):
        result = cli_runner.invoke(cli.app, ["restore"])
        assert result.exit_code != 0

    def test_valid_invocation(self, cli_runner, clean_env):
        with patch.object(cli, "_run_restore") as mock_run:
            cli_runner.invoke(
                cli.app,
                ["restore", "-n", "mybak", "-t", "20260101_0000"],
            )
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["name"] == "mybak"
        assert kwargs["timestamp"] == "20260101_0000"
        assert kwargs["overwrite"] is True

    def test_no_overwrite_flag(self, cli_runner, clean_env):
        with patch.object(cli, "_run_restore") as mock_run:
            cli_runner.invoke(cli.app, ["restore", "-n", "x", "--no-overwrite"])
        assert mock_run.call_args.kwargs["overwrite"] is False


class TestVerify:
    def test_missing_name_fails(self, cli_runner, clean_env):
        result = cli_runner.invoke(cli.app, ["verify"])
        assert result.exit_code != 0

    def test_valid_invocation(self, cli_runner, clean_env):
        with patch.object(cli, "_run_verify") as mock_run:
            cli_runner.invoke(cli.app, ["verify", "-n", "mybak"])
        mock_run.assert_called_once()


class TestCopy:
    def test_default_overwrite_true(self, cli_runner, clean_env):
        with patch.object(cli, "_run_copy") as mock_run:
            cli_runner.invoke(cli.app, ["copy"])
        assert mock_run.call_args.kwargs["overwrite"] is True

    def test_no_overwrite(self, cli_runner, clean_env):
        with patch.object(cli, "_run_copy") as mock_run:
            cli_runner.invoke(cli.app, ["copy", "--no-overwrite"])
        assert mock_run.call_args.kwargs["overwrite"] is False


# ---------------------------------------------------------------------------
# help & global surface
# ---------------------------------------------------------------------------


class TestHelp:
    def test_top_level_help_lists_all_subcommands(self, cli_runner):
        result = cli_runner.invoke(cli.app, ["--help"])
        assert result.exit_code == 0
        for sub in ("backup", "restore", "verify", "copy", "rename", "volumes"):
            assert sub in result.output

    def test_backup_help_lists_volume_flags(self, cli_runner):
        result = cli_runner.invoke(cli.app, ["backup", "--help"])
        assert result.exit_code == 0
        assert "--input-volume" in result.output
        assert "--output-volume" in result.output

    def test_volumes_help_lists_management_subcommands(self, cli_runner):
        result = cli_runner.invoke(cli.app, ["volumes", "--help"])
        assert result.exit_code == 0
        for sub in ("list", "inspect", "create", "remove"):
            assert sub in result.output


# ---------------------------------------------------------------------------
# Docker-unavailable error path (no socket mocked)
# ---------------------------------------------------------------------------


class TestDockerUnavailable:
    def test_volumes_list_exits_clean_without_socket(
        self, cli_runner, clean_env, monkeypatch
    ):
        from docker_client import DockerClient, DockerUnavailable

        def fake_list(self):
            raise DockerUnavailable("Docker socket not found at /var/run/docker.sock")

        monkeypatch.setattr(DockerClient, "list_volumes", fake_list)
        result = cli_runner.invoke(cli.app, ["volumes", "list"])
        assert result.exit_code == cli.EXIT_CONFIG

    def test_rename_refuses_without_ping(self, cli_runner, clean_env, monkeypatch):
        from docker_client import DockerClient

        monkeypatch.setattr(DockerClient, "ping", lambda self: False)
        result = cli_runner.invoke(cli.app, ["rename", "a", "b"])
        assert result.exit_code == cli.EXIT_CONFIG
