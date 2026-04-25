"""
Functional tests: detached signing of backup archives.

Generates a throwaway GPG keypair in tmp_path so the test is self-contained
and doesn't touch the user's keyring.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from operations.backup_files import BackupManager


def _have_gpg() -> bool:
    return shutil.which("gpg") is not None


pytestmark = pytest.mark.skipif(not _have_gpg(), reason="gpg binary not available")


@pytest.fixture
def gpg_keypair(tmp_path, monkeypatch):
    """Generate an Ed25519 GPG keypair in a private homedir; yield (homedir, fingerprint)."""
    homedir = tmp_path / ".gnupg"
    homedir.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(homedir))

    # `--quick-gen-key` is the fastest path; ed25519 is sub-second on every
    # modern CPU and avoids waiting for the entropy pool.
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--passphrase", "",
            "--pinentry-mode", "loopback",
            "--quick-gen-key",
            "DVM Test <test@example.com>",
            "ed25519",
            "default",
            "0",
        ],
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        check=True, capture_output=True, text=True,
    )
    fingerprint = None
    for line in result.stdout.splitlines():
        if line.startswith("fpr:"):
            fingerprint = line.split(":")[9]
            break
    assert fingerprint, "Failed to extract fingerprint from generated key"
    return str(homedir), fingerprint


@pytest.fixture
def populated_input(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "data.txt").write_text("payload to sign")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return input_dir, output_dir


def test_sign_archive_produces_verifiable_signature(populated_input, gpg_keypair):
    input_dir, output_dir = populated_input
    homedir, fingerprint = gpg_keypair

    manager = BackupManager(
        vol_name="signed",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        sign_key=fingerprint,
    )
    archive = manager.compress()
    sig = manager.sign_archive(archive)

    assert sig == archive + ".sig"
    assert os.path.isfile(sig)

    # Verification should pass against the same keyring.
    proc = subprocess.run(
        ["gpg", "--verify", sig, archive],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")


def test_sign_archive_noop_without_key(populated_input):
    input_dir, output_dir = populated_input
    manager = BackupManager(
        vol_name="unsigned",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        sign_key=None,
    )
    archive = manager.compress()
    sig = manager.sign_archive(archive)
    assert sig is None
    assert not os.path.exists(archive + ".sig")


def test_sign_archive_fails_when_key_missing(populated_input, tmp_path, monkeypatch):
    """Signing with a key that doesn't exist in the active keyring fails cleanly."""
    input_dir, output_dir = populated_input
    empty_homedir = tmp_path / ".gnupg-empty"
    empty_homedir.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(empty_homedir))

    manager = BackupManager(
        vol_name="bad-sign",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        sign_key="0000DEADBEEFNOPE",
    )
    archive = manager.compress()
    with pytest.raises(RuntimeError, match="GPG sign failed"):
        manager.sign_archive(archive)
    assert not os.path.exists(archive + ".sig")
