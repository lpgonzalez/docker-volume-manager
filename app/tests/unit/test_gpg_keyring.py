"""Unit tests for gpg_keyring — mocked subprocess so no real gpg needed."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import gpg_keyring
from gpg_keyring import KeyringError, setup_recipient_keyring, tear_down_keyring


def _make_proc_result(stdout: str = "", returncode: int = 0):
    res = MagicMock()
    res.stdout = stdout
    res.stderr = b""
    res.returncode = returncode
    return res


def test_setup_refuses_empty_paths():
    with pytest.raises(KeyringError, match="No public key files"):
        setup_recipient_keyring([])


def test_setup_refuses_missing_file(tmp_path):
    with pytest.raises(KeyringError, match="not found"):
        setup_recipient_keyring([str(tmp_path / "missing.asc")])


def test_setup_imports_files_and_returns_fingerprints(tmp_path):
    # Two real .asc files (content irrelevant — gpg call is mocked).
    a = tmp_path / "alice.asc"
    a.write_text("ARMORED-BLOCK")
    b = tmp_path / "bob.asc"
    b.write_text("ARMORED-BLOCK")

    fpr_listing = (
        "tru::1:1700000000:0:3:1:5\n"
        "pub:u:255:22:ABCDEF1234567890ABCDEF1234567890ABCDEF12:1700000000:::u:::scESC:\n"
        "fpr:::::::::ABCDEF1234567890ABCDEF1234567890ABCDEF12:\n"
        "uid:u::::1700000000::HASH::Alice <alice@example.com>::::::::::0:\n"
        "pub:u:255:22:11112222333344445555666677778888AAAABBBB:1700000000:::u:::scESC:\n"
        "fpr:::::::::11112222333344445555666677778888AAAABBBB:\n"
        "uid:u::::1700000000::HASH2::Bob <bob@example.com>::::::::::0:\n"
    )

    with patch.object(gpg_keyring.subprocess, "run") as mock_run:
        mock_run.side_effect = [
            _make_proc_result(),  # import alice
            _make_proc_result(),  # import bob
            _make_proc_result(stdout=fpr_listing),  # list-keys
        ]
        homedir, fingerprints = setup_recipient_keyring([str(a), str(b)])

    try:
        assert os.path.isdir(homedir)
        assert fingerprints == [
            "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
            "11112222333344445555666677778888AAAABBBB",
        ]
        # gpg.conf must request always-trust so encryption doesn't prompt.
        gpg_conf = os.path.join(homedir, "gpg.conf")
        assert os.path.isfile(gpg_conf)
        with open(gpg_conf) as f:
            assert "trust-model always" in f.read()
    finally:
        tear_down_keyring(homedir)


def test_setup_dedups_repeated_fingerprints(tmp_path):
    a = tmp_path / "alice.asc"
    a.write_text("ARMORED-BLOCK")
    fpr_listing = (
        "fpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:\n"
        "fpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:\n"
    )
    with patch.object(gpg_keyring.subprocess, "run") as mock_run:
        mock_run.side_effect = [
            _make_proc_result(),
            _make_proc_result(stdout=fpr_listing),
        ]
        homedir, fingerprints = setup_recipient_keyring([str(a)])
    try:
        assert fingerprints == ["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]
    finally:
        tear_down_keyring(homedir)


def test_setup_fails_clean_on_import_error(tmp_path):
    a = tmp_path / "alice.asc"
    a.write_text("not a real key")

    import subprocess as real_subprocess

    err = real_subprocess.CalledProcessError(
        returncode=2, cmd=["gpg"], stderr=b"gpg: invalid armor"
    )
    with patch.object(gpg_keyring.subprocess, "run", side_effect=err):
        with pytest.raises(KeyringError, match="Failed to import"):
            setup_recipient_keyring([str(a)])


def test_setup_fails_when_no_keys_enumerated(tmp_path):
    a = tmp_path / "alice.asc"
    a.write_text("ARMORED-BLOCK")
    with patch.object(gpg_keyring.subprocess, "run") as mock_run:
        mock_run.side_effect = [
            _make_proc_result(),
            _make_proc_result(stdout=""),  # empty listing
        ]
        with pytest.raises(KeyringError, match="No keys could be enumerated"):
            setup_recipient_keyring([str(a)])


def test_tear_down_is_idempotent(tmp_path):
    # Should not raise even if the dir doesn't exist.
    tear_down_keyring(str(tmp_path / "no-such-dir"))
    tear_down_keyring("")
    tear_down_keyring(None)  # type: ignore[arg-type]
