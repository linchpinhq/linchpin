"""Docker sandbox for per-session container management.

Provides a protocol-based abstraction over container runtimes and a
concrete Docker implementation using the docker-py SDK.

Requirements: 14.1, 14.2, 14.3, 14.5
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound


DEFAULT_BASE_IMAGE = "ubuntu:22.04"


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Result of executing a command inside a container."""

    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class ResourceMount:
    """A single host->container bind mount for a session resource.

    v0.2 PR3 (per eng-review decision D1) uses plain Docker ``-v`` bind
    mounts rather than overlay2+tmpfs. ``mode='ro'`` is the default — file
    resources are read-only. The deliverables-output bind (mode='rw') is
    handled separately by PR5.
    """

    host_path: str
    container_path: str
    mode: Literal["ro", "rw"] = "ro"


class SandboxProtocol(Protocol):
    """Abstract sandbox interface for container runtimes."""

    async def create(
        self,
        image: str,
        network: str,
        mounts: Sequence[ResourceMount] | None = None,
    ) -> str:
        """Create and start a container, returning its id.

        ``mounts`` is an optional list of host->container bind mounts applied
        at boot. Empty/None preserves v0.1 behavior (no mounts).
        """
        ...

    async def exec(self, container_id: str, command: str) -> ExecResult:
        """Run *command* inside the container and return the result."""
        ...

    async def write_file(
        self, container_id: str, path: str, content: str
    ) -> None:
        """Write *content* to *path* inside the container."""
        ...

    async def read_file(self, container_id: str, path: str) -> str:
        """Read and return the contents of *path* from the container."""
        ...

    async def destroy(self, container_id: str) -> None:
        """Stop and remove the container."""
        ...


class DockerSandbox:
    """Docker-based sandbox using the docker-py SDK.

    All blocking docker-py calls are wrapped with ``asyncio.to_thread``
    so they can be used from async code without blocking the event loop.
    """

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(
        self,
        image: str,
        network: str,
        mounts: Sequence[ResourceMount] | None = None,
    ) -> str:
        """Pull (if needed) and start a container on *network*.

        ``mounts`` is applied as docker-py ``volumes={host: {bind, mode}}``.
        Each ResourceMount is validated for absolute paths; the FileStore
        path is the source of truth for host_path resolution at call sites.

        Returns the container id.
        """
        image = image or os.getenv("LINCHPIN_SANDBOX_IMAGE", DEFAULT_BASE_IMAGE)
        volumes = self._build_volumes(mounts)

        try:
            container = await asyncio.to_thread(
                self._client.containers.run,
                image,
                command="sleep infinity",
                network=network,
                detach=True,
                stdin_open=True,
                tty=False,
                volumes=volumes,
            )
            return container.id
        except ImageNotFound:
            # Attempt to pull the image and retry
            try:
                await asyncio.to_thread(self._client.images.pull, image)
            except DockerException as pull_err:
                raise SandboxError(
                    f"Failed to pull image '{image}': {pull_err}"
                ) from pull_err

            container = await asyncio.to_thread(
                self._client.containers.run,
                image,
                command="sleep infinity",
                network=network,
                detach=True,
                stdin_open=True,
                tty=False,
                volumes=volumes,
            )
            return container.id
        except DockerException as exc:
            raise SandboxError(
                f"Failed to create container from '{image}': {exc}"
            ) from exc

    @staticmethod
    def _build_volumes(
        mounts: Sequence[ResourceMount] | None,
    ) -> dict[str, dict[str, str]] | None:
        """Translate ResourceMount list into docker-py ``volumes`` dict.

        Returns ``None`` (not ``{}``) when mounts is empty so docker-py
        treats the container as un-mounted, matching v0.1 behavior.
        """
        if not mounts:
            return None
        volumes: dict[str, dict[str, str]] = {}
        for m in mounts:
            if not m.host_path.startswith("/"):
                raise SandboxError(
                    f"ResourceMount.host_path must be absolute, got {m.host_path!r}"
                )
            if not m.container_path.startswith("/"):
                raise SandboxError(
                    f"ResourceMount.container_path must be absolute, got {m.container_path!r}"
                )
            volumes[m.host_path] = {"bind": m.container_path, "mode": m.mode}
        return volumes

    async def exec(self, container_id: str, command: str) -> ExecResult:
        """Execute *command* inside the container via ``bash -c``."""
        try:
            container = await asyncio.to_thread(
                self._client.containers.get, container_id
            )
            exit_code, output = await asyncio.to_thread(
                container.exec_run,
                ["bash", "-c", command],
                demux=True,
            )
            stdout = (output[0] or b"").decode("utf-8", errors="replace")
            stderr = (output[1] or b"").decode("utf-8", errors="replace")
            return ExecResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        except NotFound:
            raise SandboxError(f"Container '{container_id}' not found")
        except DockerException as exc:
            raise SandboxError(
                f"Failed to exec in container '{container_id}': {exc}"
            ) from exc

    async def write_file(
        self, container_id: str, path: str, content: str
    ) -> None:
        """Write *content* to *path* inside the container using a tar archive."""
        try:
            container = await asyncio.to_thread(
                self._client.containers.get, container_id
            )

            # Build an in-memory tar with the file
            data = content.encode("utf-8")
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name=os.path.basename(path))
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            buf.seek(0)

            dest_dir = os.path.dirname(path) or "/"
            await asyncio.to_thread(container.put_archive, dest_dir, buf)
        except NotFound:
            raise SandboxError(f"Container '{container_id}' not found")
        except DockerException as exc:
            raise SandboxError(
                f"Failed to write file '{path}' in container '{container_id}': {exc}"
            ) from exc

    async def read_file(self, container_id: str, path: str) -> str:
        """Read the contents of *path* from the container."""
        try:
            container = await asyncio.to_thread(
                self._client.containers.get, container_id
            )
            bits, _stat = await asyncio.to_thread(container.get_archive, path)

            # bits is a generator of chunks; reassemble and extract
            raw = b"".join(bits)
            buf = io.BytesIO(raw)
            with tarfile.open(fileobj=buf, mode="r") as tar:
                member = tar.getmembers()[0]
                f = tar.extractfile(member)
                if f is None:
                    raise SandboxError(
                        f"Cannot read '{path}': not a regular file"
                    )
                return f.read().decode("utf-8", errors="replace")
        except NotFound:
            raise SandboxError(f"Container '{container_id}' not found")
        except DockerException as exc:
            raise SandboxError(
                f"Failed to read file '{path}' from container '{container_id}': {exc}"
            ) from exc

    async def destroy(self, container_id: str) -> None:
        """Stop and remove the container, releasing resources."""
        try:
            container = await asyncio.to_thread(
                self._client.containers.get, container_id
            )
            await asyncio.to_thread(container.stop, timeout=5)
            await asyncio.to_thread(container.remove, force=True)
        except NotFound:
            # Already gone — nothing to do
            pass
        except DockerException as exc:
            raise SandboxError(
                f"Failed to destroy container '{container_id}': {exc}"
            ) from exc


async def ensure_docker_networks(
    client: docker.DockerClient | None = None,
) -> None:
    """Pre-create the linchpin Docker networks if they don't already exist.

    Creates two networks:
    - ``linchpin-none``: internal bridge network (no external access)
    - ``linchpin-open``: bridge network (unrestricted external access)

    Idempotent — silently skips networks that already exist.

    Requirements: 14.4, 5.3, 5.4
    """
    _client = client or docker.from_env()

    networks = [
        {"name": "linchpin-none", "driver": "bridge", "internal": True},
        {"name": "linchpin-open", "driver": "bridge", "internal": False},
    ]

    for net in networks:
        try:
            await asyncio.to_thread(
                _client.networks.create,
                net["name"],
                driver=net["driver"],
                internal=net["internal"],
                check_duplicate=True,
            )
            logging.getLogger("linchpin-api").info(
                "Created Docker network '%s'", net["name"]
            )
        except APIError as exc:
            # 409 Conflict means the network already exists — skip it
            if exc.status_code == 409:
                logging.getLogger("linchpin-api").info(
                    "Docker network '%s' already exists, skipping", net["name"]
                )
            else:
                raise SandboxError(
                    f"Failed to create Docker network '{net['name']}': {exc}"
                ) from exc


class SandboxError(Exception):
    """Descriptive error raised by sandbox operations (Req 14.5)."""
