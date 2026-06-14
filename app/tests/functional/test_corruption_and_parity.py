"""
Functional tests: archive corruption detection and PAR2 repair.

No Docker required — these just produce an archive in tmp_path, tamper a few
bytes, and verify that `BackupVerifier` detects the damage.

`verify_all()` is **read-only**: it classifies the parity state (valid /
repairable / unrepairable) but never rewrites the archive. PAR2 repair only
happens when the verifier is constructed with ``repair=True`` (the CLI's
``--repair`` flag / the wizard's repair prompt).
"""

from __future__ import annotations

import os

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


def _build_with_parity(input_dir, output_dir, name, parity_percentage):
    manager = BackupManager(
        vol_name=name,
        input_path=str(input_dir),
        output_path=str(output_dir),
        compression="gz",
        create_parity=True,
        parity_percentage=parity_percentage,
    )
    archive = manager.compress()
    manager.create_parity_file(archive, parity_percentage)
    return archive


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


def test_verify_is_readonly_and_reports_repairable(populated_input):
    """Default verify must NOT touch the archive: it reports the damage as
    repairable and leaves the corrupted bytes in place."""
    input_dir, output_dir = populated_input
    archive = _build_with_parity(input_dir, output_dir, "corrupt-with-parity", 30)

    _corrupt_byte(archive, offset=2048)
    with open(archive, "rb") as f:
        before = f.read()

    report = BackupVerifier(archive).verify_all()  # repair defaults to False
    assert report["parity_files_exist"] is True
    assert report["parity_valid"] is False
    assert report["parity_repairable"] is True
    # Read-only: no repair was attempted, archive untouched, still undecompressable.
    assert report["parity_recovered"] is None
    assert report["can_decompress"] is False
    with open(archive, "rb") as f:
        after = f.read()
    assert after == before, "verify must not modify the archive"


def test_verify_with_repair_flag_recovers(populated_input):
    """With repair=True the archive is repaired in place and becomes valid."""
    input_dir, output_dir = populated_input
    archive = _build_with_parity(input_dir, output_dir, "repairable", 30)

    _corrupt_byte(archive, offset=2048)

    report = BackupVerifier(archive, repair=True).verify_all()
    assert report["parity_files_exist"] is True
    assert report["parity_valid"] is False
    assert report["parity_repairable"] is True
    assert report["parity_recovered"] is True
    # par2 restored the archive, so decompression works again.
    assert report["can_decompress"] is True


def test_verify_classifies_catastrophic_as_unrepairable(populated_input):
    """Too much damage → par2 can't repair → classified unrepairable, read-only."""
    input_dir, output_dir = populated_input
    archive = _build_with_parity(input_dir, output_dir, "catastrophic", 5)

    # Overwrite ~half of the archive with garbage — beyond recovery capacity.
    size = os.path.getsize(archive)
    with open(archive, "r+b") as f:
        f.seek(size // 4)
        f.write(os.urandom(size // 2))

    report = BackupVerifier(archive).verify_all()
    assert report["parity_valid"] is False
    assert report["parity_repairable"] is False
    assert report["parity_recovered"] is None
    assert report["can_decompress"] is False


def test_verify_repair_flag_cannot_recover_catastrophic(populated_input):
    """repair=True on irrecoverable damage reports the failure cleanly."""
    input_dir, output_dir = populated_input
    archive = _build_with_parity(input_dir, output_dir, "catastrophic-repair", 5)

    size = os.path.getsize(archive)
    with open(archive, "r+b") as f:
        f.seek(size // 4)
        f.write(os.urandom(size // 2))

    report = BackupVerifier(archive, repair=True).verify_all()
    assert report["parity_repairable"] is False
    # Unrepairable: no repair attempted (or attempted and failed) → not recovered.
    recovered = report.get("parity_recovered")
    decomp_ok = report.get("can_decompress", False)
    assert not (recovered and decomp_ok), (
        "heavy corruption should not pass both recovery and decompression"
    )
