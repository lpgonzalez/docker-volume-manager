"""Integration tests: rename behaviour when the source volume is in use.

The helper container holding the volume is launched via docker-py directly
(not via DVM) so we get full control over its lifecycle.
"""

from __future__ import annotations

import os
import uuid

import pytest

from operations.rename_volume import RenameError, rename_volume

pytestmark = pytest.mark.slow


@pytest.fixture
def user_container_factory(docker_client):
    """Spawn detached containers that hold a volume; stop them at teardown."""
    import docker as docker_mod

    low = docker_mod.DockerClient(base_url="unix:///var/run/docker.sock")
    image = os.environ.get("DVM_HELPER_IMAGE", "docker_volume_manager:2.0")
    spawned = []

    def _spawn(volume_name: str):
        ctr = low.containers.run(
            image=image,
            command=["sh", "-c", "sleep 300"],
            entrypoint=[""],
            volumes={volume_name: {"bind": "/data", "mode": "rw"}},
            detach=True,
            remove=False,
            name=f"dvm-test-user-{uuid.uuid4().hex[:8]}",
        )
        spawned.append(ctr)
        return ctr

    yield _spawn

    for ctr in spawned:
        try:
            ctr.kill()
        except Exception:
            pass
        try:
            ctr.remove(force=True)
        except Exception:
            pass


def test_rename_refuses_in_use_without_force(
    docker_client, volume_factory, populate_volume, user_container_factory
):
    source = volume_factory()
    populate_volume(source, "echo hi > /target/file.txt")

    user_container_factory(source)

    target = f"dvm-test-renamed-{uuid.uuid4().hex[:8]}"
    try:
        with pytest.raises(RenameError, match="in use"):
            rename_volume(source, target, client=docker_client)
        # Source must still exist; target must NOT have been created.
        assert docker_client.volume_exists(source) is True
        assert docker_client.volume_exists(target) is False
    finally:
        if docker_client.volume_exists(target):
            docker_client.remove_volume(target, force=True)


def test_rename_in_use_with_force_completes_copy_but_leaves_source(
    docker_client, volume_factory, populate_volume, user_container_factory
):
    """With force=True the copy proceeds; source can't be removed while the
    user container holds it, so source_deleted is False and the caller is
    warned (not raised). Target ends up with a copy of the data."""
    source = volume_factory()
    populate_volume(source, "echo payload > /target/file.txt")

    user_container_factory(source)

    target = f"dvm-test-renamed-force-{uuid.uuid4().hex[:8]}"
    try:
        result = rename_volume(source, target, force=True, client=docker_client)
        assert result.target == target
        # User container still holds source → Docker refused deletion.
        assert result.source_deleted is False
        assert docker_client.volume_exists(source) is True
        assert docker_client.volume_exists(target) is True

        # Target should have the copied payload.
        content = docker_client.run_throwaway(
            ["cat", "/target/file.txt"],
            volumes={target: {"bind": "/target", "mode": "ro"}},
        )
        assert "payload" in content
    finally:
        if docker_client.volume_exists(target):
            docker_client.remove_volume(target, force=True)
