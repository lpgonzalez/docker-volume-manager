"""Functional tests: backup + verify end-to-end using the system's tar/gpg/par2."""

from __future__ import annotations

import glob
import os

import pytest

from operations.backup_files import BackupManager
from operations.verify_backup import BackupVerifier

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_input(tmp_path):
    """A tiny input tree — enough to exercise the tar + compression pipeline."""
    input_dir = tmp_path / "input"
    (input_dir / "nested").mkdir(parents=True)
    (input_dir / "test.txt").write_text("hello world")
    (input_dir / "nested" / "deep.txt").write_text("deep content")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return input_dir, output_dir


# ---------------------------------------------------------------------------
# Plain compression — supported algorithms only (NONE / GZ / ZSTD).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "compression,expected_ext",
    [("none", ".tar"), ("gz", ".tar.gz"), ("zstd", ".tar.zst")],
)
def test_backup_all_compressions_verify_ok(simple_input, compression, expected_ext):
    input_dir, output_dir = simple_input
    manager = BackupManager(
        vol_name=f"t-{compression}",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression=compression,
        password=None,
        create_parity=False,
    )
    archive = manager.compress()
    assert os.path.isfile(archive)
    assert archive.endswith(expected_ext)

    report = BackupVerifier(archive).verify_all()
    assert report["backup_exists"] is True
    assert report["can_decompress"] is True
    assert report["is_encrypted"] is False


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


def test_backup_with_symmetric_encryption_verify_ok(simple_input):
    input_dir, output_dir = simple_input
    manager = BackupManager(
        vol_name="t-enc",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        password="testpass",
        create_parity=False,
    )
    archive = manager.compress_and_encrypt_pipeline()
    assert archive.endswith(".gpg") and os.path.isfile(archive)

    report = BackupVerifier(archive, password="testpass").verify_all()
    assert report["is_encrypted"] is True
    assert report["can_decrypt"] is True
    assert report["can_decompress"] is True


def test_backup_wrong_password_cannot_decrypt(simple_input):
    input_dir, output_dir = simple_input
    manager = BackupManager(
        vol_name="t-wrong-pw",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        password="right",
        create_parity=False,
    )
    archive = manager.compress_and_encrypt_pipeline()

    report = BackupVerifier(archive, password="wrong").verify_all()
    assert report["is_encrypted"] is True
    assert report["can_decrypt"] is False


# ---------------------------------------------------------------------------
# Parity — with -n1 we expect exactly two par2 files (index + one volume).
# ---------------------------------------------------------------------------


def test_backup_with_parity_produces_single_volume(simple_input):
    input_dir, output_dir = simple_input
    manager = BackupManager(
        vol_name="t-parity",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        password="pw",
        create_parity=True,
        parity_percentage=10,
    )
    archive = manager.compress_and_encrypt_pipeline()
    assert os.path.isfile(archive + ".par2")
    volumes = glob.glob(archive + ".vol*.par2")
    assert len(volumes) == 1, f"expected single volume par2 file, got {volumes!r}"

    report = BackupVerifier(archive, password="pw").verify_all()
    assert report["parity_files_exist"] is True
    assert report["parity_valid"] is True


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_backup_missing_input_raises(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    missing = tmp_path / "does-not-exist"
    manager = BackupManager(
        vol_name="t-missing",
        input_path=str(missing),
        output_path=str(output_dir),
        compression="gz",
    )
    with pytest.raises(FileNotFoundError):
        manager.compress()
