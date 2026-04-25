"""
Functional tests: archive corruption detection and PAR2 repair.

No Docker required — these just produce an archive in tmp_path, tamper a few
bytes, and verify that `BackupVerifier` detects the damage (and that PAR2
recovery kicks in when parity files are present).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from operations.backup_files import BackupManager
from operations.verify_backup import BackupVerifier


@pytest.fixture
def populated_input(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    # A payload large enough to survive a single-byte corruption in its middle.
    (input_dir / "payload.bin").write_bytes(b"A" * 64 * 1024)
    for i in range(5):
        (input_dir / f"note-{i}.txt").write_text(f"note {i}\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return input_dir, output_dir


def _corrupt_byte(archive_path: str, offset: int = 1024) -> None:
    with open(archive_path, "r+b") as f:
        f.seek(offset)
        f.write(b"\xff\xff\xff\xff")


def test_verify_detects_corruption_without_parity(populated_input):
    input_dir, output_dir = populated_input
    manager = BackupManager(
        vol_name="corrupt-no-parity",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        create_parity=False,
    )
    archive = manager.compress()
    assert os.path.isfile(archive)

    _corrupt_byte(archive)

    report = BackupVerifier(archive).verify_all()
    assert report["backup_exists"] is True
    assert report["can_decompress"] is False


def test_verify_repairs_corruption_with_parity(populated_input):
    input_dir, output_dir = populated_input
    manager = BackupManager(
        vol_name="corrupt-with-parity",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        create_parity=True,
        parity_percentage=30,
    )
    archive = manager.compress()
    manager.create_parity_file(archive, 30)

    _corrupt_byte(archive, offset=2048)

    report = BackupVerifier(archive).verify_all()
    assert report["parity_files_exist"] is True
    # Parity reported invalid pre-repair, but recovery must have succeeded.
    # After recovery par2 restores the archive, so decompression works again.
    assert report.get("parity_recovered", False) is True or report["parity_valid"] is True
    assert report["can_decompress"] is True


def test_verify_catastrophic_corruption_cannot_be_recovered(populated_input):
    """Too much damage → par2 can't repair → verify reports the failure cleanly."""
    input_dir, output_dir = populated_input
    manager = BackupManager(
        vol_name="catastrophic",
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        create_parity=True,
        parity_percentage=5,  # low redundancy
    )
    archive = manager.compress()
    manager.create_parity_file(archive, 5)

    # Overwrite ~half of the archive with garbage — beyond recovery capacity.
    size = os.path.getsize(archive)
    with open(archive, "r+b") as f:
        f.seek(size // 4)
        f.write(os.urandom(size // 2))

    report = BackupVerifier(archive).verify_all()
    # We don't assert a specific combination beyond "recovery failed and
    # decompression fails". Depending on par2 version either parity_recovered
    # is False or can_decompress is False — both are acceptable failure signals.
    recovered = report.get("parity_recovered", False)
    decomp_ok = report.get("can_decompress", False)
    assert not (recovered and decomp_ok), (
        "heavy corruption should not pass both recovery and decompression"
    )
