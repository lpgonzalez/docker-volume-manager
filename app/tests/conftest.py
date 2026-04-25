"""
Shared fixtures and collection hooks for the DVM test suite.

Auto-applies tier markers based on the test file's directory so individual
test files don't need to repeat `@pytest.mark.unit/functional/integration`.
"""

from __future__ import annotations

import hashlib
import os
import random
import stat
from pathlib import Path
from typing import Dict, Tuple

import pytest


# ---------------------------------------------------------------------------
# Auto-marker
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    """Apply unit/functional/integration markers based on file path."""
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/tests/unit/" in path:
            item.add_marker(pytest.mark.unit)
        elif "/tests/functional/" in path:
            item.add_marker(pytest.mark.functional)
        elif "/tests/integration/" in path:
            item.add_marker(pytest.mark.integration)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_runner():
    """typer test runner with separated stdout/stderr."""
    from typer.testing import CliRunner

    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        # Newer click versions dropped mix_stderr — stdout and stderr are
        # already split by default.
        return CliRunner()


@pytest.fixture
def clean_env(monkeypatch):
    """Clear DVM-related environment variables so tests start from a blank slate."""
    keys = [
        "OPERATION",
        "BACKUP_FILE_NAME",
        "INPUT_PATH",
        "OUTPUT_PATH",
        "COMPRESSION",
        "PARITY",
        "ENCRYPTION_KEY",
        "TIMESTAMP",
        "COPY_OVERWRITE",
        "LOG_LEVEL",
        "LOG_OUTPUT",
        "LOGS_PATH",
        "DVM_USE_COLORS",
        "NO_COLOR",
        "INPUT_VOLUME",
        "OUTPUT_VOLUME",
        "DVM_HELPER_MODE",
        "DVM_HELPER_IMAGE",
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def reset_dvm_logger():
    """Detach handlers from the shared `dvm` logger so each test starts fresh."""
    import logging

    logger = logging.getLogger("dvm")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    logger.handlers = []
    logger.propagate = True
    yield logger
    # Close handlers the test attached before restoring the originals. Without
    # this, file descriptors from FileHandlers leak and Python emits a
    # ResourceWarning which pytest's filterwarnings=error would turn into a
    # test failure.
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
    logger.handlers = saved_handlers
    logger.level = saved_level
    logger.propagate = saved_propagate


# ---------------------------------------------------------------------------
# Data factory
# ---------------------------------------------------------------------------


@pytest.fixture
def data_tree(tmp_path, request) -> Path:
    """
    Reproducible source tree with varied file types, sizes, permissions and
    (when running as root) multiple UIDs/GIDs.

    Parametrize with the RNG seed via `indirect`:
        @pytest.mark.parametrize("data_tree", [42], indirect=True)
    """
    seed = getattr(request, "param", 42)
    rng = random.Random(seed)

    root = tmp_path / "src"
    root.mkdir()

    def mk_file(path: Path, size: int, mode: int, uid=None, gid=None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rng.randbytes(size))
        path.chmod(mode)
        if uid is not None and gid is not None:
            try:
                os.chown(path, uid, gid)
            except (PermissionError, OSError):
                pass

    def mk_dir(path: Path, mode: int = 0o755) -> None:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)

    def mk_symlink(link: Path, target: str) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

    # Top-level variety
    mk_dir(root / "empty")
    mk_file(root / "top.txt", 128, 0o644)
    mk_file(root / "exec.sh", 64, 0o755)
    mk_file(root / ".hidden", 32, 0o600)
    mk_file(root / "with spaces.dat", 256, 0o644)
    mk_file(root / "unicode-accénts.txt", 128, 0o644)

    # Nested tree
    mk_dir(root / "nested" / "deeper" / "deepest")
    mk_file(root / "nested" / "a.bin", 1024, 0o640)
    mk_file(root / "nested" / "deeper" / "b.bin", 4096, 0o600)
    mk_file(root / "nested" / "deeper" / "deepest" / "c.bin", 2048, 0o644)

    # Mid-sized file so compression has something to chew
    mk_file(root / "medium.bin", 256 * 1024, 0o644)

    # Symlinks (relative to the link's directory)
    mk_symlink(root / "rel-link", "top.txt")
    mk_symlink(root / "nested" / "rel-link", "a.bin")

    # UID / GID variety (only meaningful as root, which is the test-container default)
    if os.geteuid() == 0:
        mk_file(root / "uid-100.txt", 64, 0o644, uid=100, gid=100)
        mk_file(root / "uid-200.txt", 64, 0o640, uid=200, gid=200)
        mk_file(root / "nested" / "uid-1000.txt", 64, 0o600, uid=1000, gid=1000)

    return root


def _tree_fingerprint(
    root: Path, *, include_ownership: bool = True
) -> Dict[str, Tuple]:
    """
    Walk a directory tree and return a structural fingerprint.

    Dict shape: {rel_posix: (type, size, mode, uid|None, gid|None, hash_or_target)}
    where `hash_or_target` is sha256 for regular files, the link target for
    symlinks, and None for directories.
    """
    out: Dict[str, Tuple] = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        for name in sorted(dirnames + filenames):
            full = dp / name
            rel = full.relative_to(root).as_posix()
            st = full.lstat()
            mode = stat.S_IMODE(st.st_mode)
            uid = st.st_uid if include_ownership else None
            gid = st.st_gid if include_ownership else None
            if full.is_symlink():
                out[rel] = ("symlink", 0, mode, uid, gid, os.readlink(full))
            elif full.is_dir():
                out[rel] = ("dir", 0, mode, uid, gid, None)
            else:
                h = hashlib.sha256()
                with open(full, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                out[rel] = ("file", st.st_size, mode, uid, gid, h.hexdigest())
    return out


@pytest.fixture
def fingerprint():
    """Return the _tree_fingerprint helper so tests can diff two trees."""
    return _tree_fingerprint
