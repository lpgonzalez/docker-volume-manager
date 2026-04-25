"""Integration tests: end-to-end rename flow with real Docker volumes."""

from __future__ import annotations

import uuid

import pytest

from operations.rename_volume import RenameError, rename_volume


@pytest.fixture
def volume_with_data(docker_client, throwaway_volume):
    """Populate the throwaway volume with a small deterministic dataset."""
    docker_client.run_throwaway(
        [
            "sh",
            "-c",
            "mkdir -p /target/dir1/dir2 && "
            "echo file1 > /target/file1.txt && "
            "echo nested > /target/dir1/file2.txt && "
            "echo deep > /target/dir1/dir2/file3.txt && "
            "ln -s file1.txt /target/symlink",
        ],
        volumes={throwaway_volume: {"bind": "/target", "mode": "rw"}},
    )
    return throwaway_volume


def test_rename_happy_path(docker_client, volume_with_data):
    target = f"dvm-test-renamed-{uuid.uuid4().hex[:8]}"
    source = volume_with_data
    try:
        result = rename_volume(source, target, client=docker_client)

        assert result.source == source
        assert result.target == target
        assert result.source_deleted is True
        assert result.files_copied >= 3

        assert docker_client.volume_exists(source) is False
        assert docker_client.volume_exists(target) is True
        entries = docker_client.volume_ls(target)
        assert any("file1.txt" in e for e in (entries or []))
        assert any("dir1/" in e or "dir1" in e for e in (entries or []))
    finally:
        if docker_client.volume_exists(target):
            docker_client.remove_volume(target, force=True)


def test_rename_keep_source_preserves_original(docker_client, volume_with_data):
    target = f"dvm-test-renamed-{uuid.uuid4().hex[:8]}"
    source = volume_with_data
    try:
        result = rename_volume(
            source, target, keep_source=True, client=docker_client
        )
        assert result.source_deleted is False
        assert docker_client.volume_exists(source) is True
        assert docker_client.volume_exists(target) is True
    finally:
        if docker_client.volume_exists(target):
            docker_client.remove_volume(target, force=True)


def test_rename_target_already_exists(docker_client, throwaway_volume):
    conflict = f"dvm-test-conflict-{uuid.uuid4().hex[:8]}"
    docker_client.create_volume(conflict)
    try:
        with pytest.raises(RenameError, match="already exists"):
            rename_volume(throwaway_volume, conflict, client=docker_client)
    finally:
        docker_client.remove_volume(conflict, force=True)


def test_rename_source_not_found(docker_client):
    with pytest.raises(RenameError, match="does not exist"):
        rename_volume(
            f"dvm-no-such-{uuid.uuid4().hex[:8]}",
            f"dvm-target-{uuid.uuid4().hex[:8]}",
            client=docker_client,
        )


def test_rename_source_equals_target(docker_client, throwaway_volume):
    with pytest.raises(RenameError, match="identical"):
        rename_volume(throwaway_volume, throwaway_volume, client=docker_client)
