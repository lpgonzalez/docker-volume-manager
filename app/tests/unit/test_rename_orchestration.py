"""Unit tests for rename_volume() — all Docker interactions are mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from docker_client import DockerClient, DockerUnavailable, VolumeInfo
from operations.rename_volume import RenameError, rename_volume


def _make_client(
    *,
    source_exists: bool = True,
    target_exists: bool = False,
    source_containers=None,
    copy_exit_code: int = 0,
    source_stats=None,
    target_stats=None,
) -> MagicMock:
    """Build a DockerClient mock preloaded with canned answers for a rename flow."""
    source_containers = source_containers or []
    source_stats = source_stats or {"files": 10, "bytes": 10240}
    if target_stats is None:
        target_stats = source_stats

    client = MagicMock(spec=DockerClient)

    def volume_exists(name):
        return {"src": source_exists, "dst": target_exists}.get(name, False)

    client.volume_exists.side_effect = volume_exists
    client.inspect_volume.return_value = VolumeInfo(
        name="src", driver="local", containers=list(source_containers)
    )
    client.create_volume.return_value = VolumeInfo(name="dst", driver="local")
    client.run_helper_streaming.return_value = copy_exit_code

    # run_throwaway is called twice: pre-copy stats, post-copy stats.
    client.run_throwaway.side_effect = [
        f"{source_stats['files']}\n{source_stats['bytes']}\n",
        f"{target_stats['files']}\n{target_stats['bytes']}\n",
    ]
    return client


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRenameHappyPath:
    def test_full_flow(self):
        client = _make_client()
        result = rename_volume("src", "dst", client=client)

        client.create_volume.assert_called_once_with("dst")
        assert client.run_helper_streaming.called

        call = client.run_helper_streaming.call_args
        assert call.kwargs["command"] == ["python", "main.py", "copy"]
        mounts = call.kwargs["volume_mounts"]
        assert mounts["src"] == {"bind": "/app/input_dir", "mode": "ro"}
        assert mounts["dst"] == {"bind": "/app/output_dir", "mode": "rw"}

        client.remove_volume.assert_called_once_with("src", force=False)
        assert result.source == "src"
        assert result.target == "dst"
        assert result.source_deleted is True
        assert result.files_copied == 10
        assert result.bytes_copied == 10240


class TestKeepSource:
    def test_skips_source_removal(self):
        client = _make_client()
        result = rename_volume("src", "dst", keep_source=True, client=client)
        client.remove_volume.assert_not_called()
        assert result.source_deleted is False


# ---------------------------------------------------------------------------
# Validation failures (no state changes)
# ---------------------------------------------------------------------------


class TestValidation:
    def test_source_equals_target(self):
        client = MagicMock(spec=DockerClient)
        with pytest.raises(RenameError, match="identical"):
            rename_volume("same", "same", client=client)
        client.create_volume.assert_not_called()

    def test_missing_source(self):
        client = _make_client(source_exists=False)
        with pytest.raises(RenameError, match="does not exist"):
            rename_volume("src", "dst", client=client)
        client.create_volume.assert_not_called()

    def test_existing_target(self):
        client = _make_client(target_exists=True)
        with pytest.raises(RenameError, match="already exists"):
            rename_volume("src", "dst", client=client)
        client.create_volume.assert_not_called()

    def test_source_in_use_without_force(self):
        client = _make_client(source_containers=["web"])
        with pytest.raises(RenameError, match="in use by"):
            rename_volume("src", "dst", client=client)
        client.create_volume.assert_not_called()

    def test_source_in_use_with_force_proceeds(self):
        client = _make_client(source_containers=["web"])
        result = rename_volume("src", "dst", force=True, client=client)
        client.remove_volume.assert_called_once_with("src", force=True)
        assert result.source_deleted is True


# ---------------------------------------------------------------------------
# Rollback paths
# ---------------------------------------------------------------------------


class TestRollback:
    def test_copy_failure_removes_target_and_keeps_source(self):
        client = _make_client(copy_exit_code=1)
        with pytest.raises(RenameError):
            rename_volume("src", "dst", client=client)

        removed = [c.args[0] for c in client.remove_volume.call_args_list]
        assert removed == ["dst"]  # never touches src

    def test_verify_mismatch_rolls_back_target(self):
        client = _make_client(
            source_stats={"files": 10, "bytes": 10240},
            target_stats={"files": 9, "bytes": 10000},
        )
        with pytest.raises(RenameError, match="Verification failed"):
            rename_volume("src", "dst", client=client)

        removed = [c.args[0] for c in client.remove_volume.call_args_list]
        assert removed == ["dst"]

    def test_create_target_failure_does_not_require_rollback(self):
        client = _make_client()
        client.create_volume.side_effect = DockerUnavailable("quota exceeded")
        with pytest.raises(RenameError, match="create target volume"):
            rename_volume("src", "dst", client=client)
        client.remove_volume.assert_not_called()

    def test_source_removal_failure_reports_warning(self, caplog):
        client = _make_client()
        client.remove_volume.side_effect = [
            DockerUnavailable("volume busy"),  # first call (source removal)
        ]
        caplog.set_level("ERROR", logger="dvm")
        result = rename_volume("src", "dst", client=client)
        # Target data is in place but source wasn't removed.
        assert result.source_deleted is False
        assert any("could not be removed" in r.getMessage() for r in caplog.records)
