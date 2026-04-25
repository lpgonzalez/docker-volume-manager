"""Integration-tier fixtures: require a reachable Docker daemon."""

from __future__ import annotations

import os
import uuid

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests when no docker.sock is mounted."""
    if os.path.exists("/var/run/docker.sock"):
        return
    skip = pytest.mark.skip(reason="Docker socket not available; mount /var/run/docker.sock to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="module")
def docker_client():
    """Shared DockerClient for a module; skips the module if the daemon isn't reachable."""
    from docker_client import DockerClient

    client = DockerClient()
    if not client.ping():
        pytest.skip("Docker daemon not reachable via /var/run/docker.sock")
    return client


@pytest.fixture
def throwaway_volume(docker_client):
    """Create a uniquely-named volume; remove it (best effort) after the test."""
    name = f"dvm-test-{uuid.uuid4().hex[:8]}"
    docker_client.create_volume(name)
    yield name
    try:
        if docker_client.volume_exists(name):
            docker_client.remove_volume(name, force=True)
    except Exception:
        pass


@pytest.fixture
def unique_volume_name():
    """Generate a unique, unreserved volume name; caller is responsible for cleanup."""
    return f"dvm-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def volume_factory(docker_client):
    """
    Factory that creates throwaway Docker volumes on demand and cleans them
    all up at the end of the test. Prefer this over `throwaway_volume` when
    a test needs more than one volume (rename, backup/restore roundtrip, ...).
    """
    created = []

    def _make(name: str | None = None) -> str:
        n = name or f"dvm-test-{uuid.uuid4().hex[:8]}"
        docker_client.create_volume(n)
        created.append(n)
        return n

    yield _make

    for n in created:
        try:
            if docker_client.volume_exists(n):
                docker_client.remove_volume(n, force=True)
        except Exception:
            pass


@pytest.fixture
def populate_volume(docker_client):
    """Callable fixture: run a shell script with a volume bind-mounted at /target."""
    def _populate(volume_name: str, script: str) -> None:
        docker_client.run_throwaway(
            ["sh", "-c", script],
            volumes={volume_name: {"bind": "/target", "mode": "rw"}},
        )
    return _populate
