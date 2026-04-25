"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

"""
Docker Engine API facade for Docker Volume Manager.

Thin wrapper over docker-py covering the subset of operations DVM needs:

- Ping / availability check.
- Volume lifecycle (list, inspect, create, remove).
- Volume introspection (containers mounting it, on-disk size, top-level
  listing) — size/contents require spawning a short-lived helper container
  that mounts the volume read-only.
- Generic `run_throwaway` primitive used by step 3b's input/output volume
  pivot flow and by the introspection helpers here.

All failures surface as `DockerUnavailable` carrying an actionable message —
the CLI prints that instead of a raw SDK traceback.

Security note: mounting /var/run/docker.sock grants the container effective
root on the host. Appropriate for local / trusted environments; audit before
exposing to untrusted input.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("dvm")

DEFAULT_SOCKET = "/var/run/docker.sock"
SELF_IMAGE_ENV = "DVM_HELPER_IMAGE"
FALLBACK_IMAGE = "docker_volume_manager:2.0"


class DockerUnavailable(RuntimeError):
    """Raised when the Docker daemon cannot be reached or used."""


@dataclass
class VolumeInfo:
    name: str
    driver: str = "unknown"
    mountpoint: str = ""
    labels: Dict[str, str] = field(default_factory=dict)
    options: Dict[str, str] = field(default_factory=dict)
    scope: str = "local"
    created_at: Optional[str] = None
    containers: List[str] = field(default_factory=list)
    size_bytes: Optional[int] = None
    top_entries: Optional[List[str]] = None


def _require_docker():
    try:
        import docker  # noqa: WPS433
        from docker import errors as _errors  # noqa: WPS433, F401
    except Exception as exc:
        raise DockerUnavailable(
            "`docker` Python SDK not installed in this image. Rebuild with the updated requirements."
        ) from exc
    return docker


class DockerClient:
    """
    Lazy facade over docker-py for the subset of API calls DVM uses.

    Connection is deferred until the first method call so importing this
    module never requires a reachable Docker daemon — useful in unit tests
    and when DVM is invoked for non-Docker work.

    All methods raise :class:`DockerUnavailable` (with an actionable message)
    when the socket is missing, the daemon is unreachable, or any underlying
    docker-py call fails for a reason DVM should expose to the user.
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET):
        self.socket_path = socket_path
        self._client = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        if self._client is not None:
            return self._client
        docker_mod = _require_docker()
        if not os.path.exists(self.socket_path):
            raise DockerUnavailable(
                f"Docker socket not found at {self.socket_path}. "
                "Re-run the container with: -v /var/run/docker.sock:/var/run/docker.sock"
            )
        try:
            client = docker_mod.DockerClient(base_url=f"unix://{self.socket_path}")
            client.ping()
        except DockerUnavailable:
            raise
        except Exception as exc:
            raise DockerUnavailable(
                f"Cannot reach Docker daemon via {self.socket_path}: {exc}"
            ) from exc
        self._client = client
        return client

    def ping(self) -> bool:
        try:
            self._connect()
            return True
        except DockerUnavailable:
            return False

    # ------------------------------------------------------------------
    # Volume lifecycle
    # ------------------------------------------------------------------

    def list_volumes(self) -> List[VolumeInfo]:
        """
        Return all Docker volumes sorted alphabetically (case-insensitive).

        For each volume, also resolves the list of containers that mount it
        by enumerating containers (running + stopped). When container
        enumeration fails (e.g. partial daemon responses), the ``containers``
        field is left empty rather than raising — listing volumes is
        considered the primary contract.
        """
        client = self._connect()
        try:
            raw = client.volumes.list()
        except Exception as exc:
            raise DockerUnavailable(f"Failed to list volumes: {exc}") from exc
        users = self._containers_by_volume()
        result = []
        for vol in raw:
            info = self._info_from(vol)
            info.containers = users.get(info.name, [])
            result.append(info)
        result.sort(key=lambda v: v.name.lower())
        return result

    def inspect_volume(
        self,
        name: str,
        *,
        with_size: bool = False,
        with_contents: bool = False,
        max_entries: int = 50,
    ) -> VolumeInfo:
        client = self._connect()
        from docker.errors import NotFound
        try:
            vol = client.volumes.get(name)
        except NotFound as exc:
            raise DockerUnavailable(f"Volume {name!r} does not exist") from exc
        except Exception as exc:
            raise DockerUnavailable(f"Failed to inspect volume {name!r}: {exc}") from exc
        info = self._info_from(vol)
        info.containers = self._containers_by_volume().get(info.name, [])
        if with_size:
            info.size_bytes = self.volume_size(name)
        if with_contents:
            info.top_entries = self.volume_ls(name, max_entries=max_entries)
        return info

    def create_volume(self, name: str) -> VolumeInfo:
        client = self._connect()
        try:
            vol = client.volumes.create(name=name)
        except Exception as exc:
            raise DockerUnavailable(f"Failed to create volume {name!r}: {exc}") from exc
        return self._info_from(vol)

    def remove_volume(self, name: str, force: bool = False) -> None:
        client = self._connect()
        from docker.errors import NotFound
        try:
            vol = client.volumes.get(name)
            vol.remove(force=force)
        except NotFound:
            logger.warning("Volume %s did not exist at removal time", name)
        except Exception as exc:
            raise DockerUnavailable(f"Failed to remove volume {name!r}: {exc}") from exc

    def volume_exists(self, name: str) -> bool:
        client = self._connect()
        from docker.errors import NotFound
        try:
            client.volumes.get(name)
            return True
        except NotFound:
            return False
        except Exception as exc:
            raise DockerUnavailable(f"Failed to check volume {name!r}: {exc}") from exc

    # ------------------------------------------------------------------
    # Volume introspection via throwaway helpers
    # ------------------------------------------------------------------

    def volume_size(self, name: str) -> Optional[int]:
        """Total on-disk size in bytes; None if probe fails."""
        try:
            out = self.run_throwaway(
                ["sh", "-c", "du -sb /target 2>/dev/null | cut -f1"],
                volumes={name: {"bind": "/target", "mode": "ro"}},
            ).strip()
            return int(out) if out else None
        except DockerUnavailable as exc:
            logger.debug("Size probe failed for %s: %s", name, exc)
            return None
        except Exception as exc:
            logger.debug("Size probe failed for %s: %s", name, exc)
            return None

    def volume_ls(self, name: str, max_entries: int = 50) -> Optional[List[str]]:
        """List top-level entries of the volume (one per line, trailing / marks dirs)."""
        try:
            cmd = f"ls -1Ap /target 2>/dev/null | head -n {int(max_entries)}"
            out = self.run_throwaway(
                ["sh", "-c", cmd],
                volumes={name: {"bind": "/target", "mode": "ro"}},
            )
            lines = [ln for ln in out.splitlines() if ln.strip()]
            return lines or []
        except DockerUnavailable as exc:
            logger.debug("Listing failed for %s: %s", name, exc)
            return None
        except Exception as exc:
            logger.debug("Listing failed for %s: %s", name, exc)
            return None

    # ------------------------------------------------------------------
    # Helper container spawning
    # ------------------------------------------------------------------

    def self_image(self) -> str:
        """Determine the image tag of THIS container so we can spawn siblings."""
        override = os.environ.get(SELF_IMAGE_ENV)
        if override:
            return override
        cid = self._self_container_id()
        if cid:
            client = self._connect()
            try:
                ctr = client.containers.get(cid)
                tags = ctr.image.tags or []
                if tags:
                    return tags[0]
            except Exception:
                pass
        return FALLBACK_IMAGE

    def self_mounts(self) -> List[Dict[str, Any]]:
        """Return the outer container's `Mounts` list (for bind-mount inheritance)."""
        cid = self._self_container_id()
        if not cid:
            return []
        client = self._connect()
        try:
            return client.containers.get(cid).attrs.get("Mounts") or []
        except Exception as exc:
            logger.debug("Failed to read own mounts: %s", exc)
            return []

    def run_helper_streaming(
        self,
        *,
        command: List[str],
        volume_mounts: Dict[str, Dict[str, str]],
        env: Optional[Dict[str, str]] = None,
        inherit_bind_mounts: bool = True,
        tty: bool = False,
    ) -> int:
        """
        Run a long-lived helper container and stream its logs back live.

        Used by :func:`cli._pivot_if_volumes` to execute volume-aware
        operations in a fresh container with the requested Docker volumes
        mounted at ``/app/input_dir`` / ``/app/output_dir``.

        Parameters
        ----------
        command:
            Argv for the helper (typically ``["python", "main.py", "<sub>"]``).
        volume_mounts:
            ``{volume_name: {"bind": "/path", "mode": "ro" | "rw"}}``. These
            override any inherited bind mounts at the same target path.
        env:
            Extra env vars for the helper. ``DVM_HELPER_MODE=1`` and
            ``PYTHONUNBUFFERED=1`` are always set.
        inherit_bind_mounts:
            When True, replicate the outer container's bind mounts so the
            helper inherits e.g. ``/app/output_dir`` from the host bind when
            only ``--input-volume`` was specified.
        tty:
            Allocate a pty for the helper. Default False so log streaming
            works through plain pipes.

        Returns
        -------
        int
            The helper container's exit code.

        Raises
        ------
        DockerUnavailable
            Daemon unreachable, image not found, or container failed to
            start. The container is force-removed on the way out regardless.
        """
        import sys as _sys  # local to avoid circular-import surprises

        client = self._connect()
        image = self.self_image()
        from docker.errors import ImageNotFound

        overridden_targets = {v["bind"] for v in volume_mounts.values()}
        mounts: Dict[str, Dict[str, str]] = dict(volume_mounts)
        if inherit_bind_mounts:
            for m in self.self_mounts():
                if m.get("Type") != "bind":
                    continue
                target = m.get("Destination")
                source = m.get("Source")
                if not (target and source):
                    continue
                if target in overridden_targets or source in mounts:
                    continue
                mounts[source] = {
                    "bind": target,
                    "mode": "rw" if m.get("RW", True) else "ro",
                }

        container_env: Dict[str, str] = {
            "DVM_HELPER_MODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        if env:
            for k, v in env.items():
                if v is None:
                    continue
                container_env[k] = str(v)

        try:
            container = client.containers.run(
                image=image,
                command=list(command),
                volumes=mounts,
                environment=container_env,
                tty=tty,
                stdin_open=False,
                detach=True,
                remove=False,
            )
        except ImageNotFound as exc:
            raise DockerUnavailable(
                f"Helper image {image!r} not found locally. "
                f"Set {SELF_IMAGE_ENV} or ensure the image exists."
            ) from exc
        except Exception as exc:
            raise DockerUnavailable(f"Helper container failed to start: {exc}") from exc

        try:
            for raw in container.logs(stream=True, follow=True, stdout=True, stderr=True):
                if isinstance(raw, (bytes, bytearray)):
                    line = raw.decode("utf-8", errors="replace")
                else:
                    line = str(raw)
                _sys.stderr.write(line)
                _sys.stderr.flush()
            status = container.wait()
            return int(status.get("StatusCode", 1))
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass

    def run_throwaway(
        self,
        command: Iterable[str],
        *,
        volumes: Dict[str, Dict[str, str]],
        image: Optional[str] = None,
    ) -> str:
        """Run a one-shot container, capture stdout, auto-remove. Override ENTRYPOINT so we can run arbitrary shell commands against the DVM image."""
        client = self._connect()
        image = image or self.self_image()
        from docker.errors import ContainerError, ImageNotFound
        try:
            output = client.containers.run(
                image=image,
                command=list(command),
                volumes=volumes,
                remove=True,
                stdout=True,
                stderr=False,
                entrypoint=[""],
            )
        except ImageNotFound as exc:
            raise DockerUnavailable(
                f"Helper image {image!r} not found locally. "
                f"Set env {SELF_IMAGE_ENV} to override or pull the image first."
            ) from exc
        except ContainerError as exc:
            raise DockerUnavailable(
                f"Helper container failed (rc={exc.exit_status}): "
                f"{(exc.stderr or b'').decode('utf-8', errors='replace') if isinstance(exc.stderr, (bytes, bytearray)) else exc.stderr}"
            ) from exc
        except Exception as exc:
            raise DockerUnavailable(f"Helper container error: {exc}") from exc
        if isinstance(output, (bytes, bytearray)):
            return output.decode("utf-8", errors="replace")
        return output or ""

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _info_from(self, vol: Any) -> VolumeInfo:
        attrs = vol.attrs or {}
        return VolumeInfo(
            name=vol.name,
            driver=attrs.get("Driver", "unknown"),
            mountpoint=attrs.get("Mountpoint", ""),
            labels=attrs.get("Labels") or {},
            options=attrs.get("Options") or {},
            scope=attrs.get("Scope", "local"),
            created_at=attrs.get("CreatedAt"),
        )

    def _containers_by_volume(self) -> Dict[str, List[str]]:
        client = self._connect()
        mapping: Dict[str, List[str]] = {}
        try:
            containers = client.containers.list(all=True)
        except Exception as exc:
            logger.debug("Container enumeration failed: %s", exc)
            return mapping
        for ctr in containers:
            for mount in ctr.attrs.get("Mounts") or []:
                if mount.get("Type") == "volume":
                    vol_name = mount.get("Name")
                    if vol_name:
                        mapping.setdefault(vol_name, []).append(ctr.name)
        return mapping

    def _self_container_id(self) -> Optional[str]:
        env_hostname = os.environ.get("HOSTNAME")
        if env_hostname:
            return env_hostname
        try:
            with open("/etc/hostname") as f:
                return f.read().strip() or None
        except Exception:
            return None


def format_size(n: Optional[int]) -> str:
    """Human-readable byte size. Accepts None for unknown."""
    if n is None:
        return "n/a"
    value = float(n)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} PiB"
