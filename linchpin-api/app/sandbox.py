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
from docker.errors import APIError, BuildError, DockerException, ImageNotFound, NotFound

from app.models import EnvironmentPackages, derived_image_tag


# v0.2.0 item #13 — sessions default to the richer linchpin-sandbox image
# (Python 3.12, Node 20, Go 1.22, Rust 1.77, Java 17, Ruby 3.1, PHP 8.2,
# GCC 13, plus psql/redis-cli/rg/tree/htop). Dockerfile lives at
# linchpin-sandbox/Dockerfile in the repo. Override with LINCHPIN_SANDBOX_IMAGE
# for tests or for ops to roll back to a slimmer image.
DEFAULT_BASE_IMAGE = "linchpinhq/sandbox:v0.2.0"

logger = logging.getLogger("linchpin-api.sandbox")


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

    async def ensure_image(
        self,
        *,
        base_image: str,
        packages: EnvironmentPackages | None,
    ) -> str:
        """Return the image tag to use for a session, building if needed.

        Empty/None ``packages`` returns ``base_image`` unchanged. Non-empty
        package sets are baked into a content-hashed derived image; the
        Docker daemon's image cache makes the second call a no-op.
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

    async def ensure_image(
        self,
        *,
        base_image: str,
        packages: EnvironmentPackages | None,
    ) -> str:
        """Build (if absent) and return the derived-image tag for ``packages``.

        v0.2.0 item #3. The derived image is keyed by a content hash of the
        normalized package set, so two environments with the same package
        list share one image and the second session pays zero build cost.
        Empty/None packages returns ``base_image`` unchanged.
        """
        if packages is None or packages.is_empty():
            return base_image

        tag = derived_image_tag(packages)
        assert tag is not None, "non-empty packages must yield a tag"

        # Fast path: image already cached on this host's daemon.
        try:
            await asyncio.to_thread(self._client.images.get, tag)
            return tag
        except ImageNotFound:
            pass

        dockerfile = self._generate_dockerfile(base_image, packages)
        logger.info(
            "building derived sandbox image %s from %s (apt=%d pip=%d npm=%d "
            "cargo=%d gem=%d go=%d)",
            tag,
            base_image,
            len(packages.apt),
            len(packages.pip),
            len(packages.npm),
            len(packages.cargo),
            len(packages.gem),
            len(packages.go),
        )
        try:
            await asyncio.to_thread(
                self._build_image,
                dockerfile=dockerfile,
                tag=tag,
            )
        except BuildError as exc:
            raise SandboxError(
                f"Failed to build derived image {tag!r} from {base_image!r}: {exc}"
            ) from exc
        except DockerException as exc:
            raise SandboxError(
                f"Docker error while building {tag!r}: {exc}"
            ) from exc
        return tag

    @staticmethod
    def _generate_dockerfile(
        base_image: str, packages: EnvironmentPackages
    ) -> str:
        """Render a single-stage Dockerfile that installs *packages* on top
        of *base_image*.

        Package names are pre-validated against ``_PACKAGE_NAME_RE`` (in
        ``models.py``) so embedding them directly in the RUN line is safe.
        Each manager gets its own RUN to keep the image layer cache useful
        across edits that only touch one ecosystem.
        """
        lines: list[str] = [f"FROM {base_image}", ""]

        if packages.apt:
            joined = " ".join(packages.apt)
            lines.append(
                "RUN apt-get update "
                f"&& apt-get install -y --no-install-recommends {joined} "
                "&& rm -rf /var/lib/apt/lists/*"
            )
        if packages.pip:
            joined = " ".join(packages.pip)
            # --break-system-packages is required on Debian trixie's
            # PEP 668-marked python3.13. Safe inside the sandbox image.
            lines.append(
                f"RUN pip install --no-cache-dir --break-system-packages {joined}"
            )
        if packages.npm:
            joined = " ".join(packages.npm)
            lines.append(
                f"RUN npm install -g --no-fund --no-audit {joined}"
            )
        if packages.cargo:
            joined = " ".join(packages.cargo)
            lines.append(f"RUN cargo install --locked {joined}")
        if packages.gem:
            joined = " ".join(packages.gem)
            lines.append(f"RUN gem install --no-document {joined}")
        if packages.go:
            # `go install <pkg>@<ver>` is the modern (1.16+) form. One RUN
            # per package keeps each install's failure isolated in logs.
            for pkg in packages.go:
                lines.append(f"RUN go install {pkg}")

        return "\n".join(lines) + "\n"

    def _build_image(self, *, dockerfile: str, tag: str) -> None:
        """Synchronous docker-py build helper.

        Passes the rendered Dockerfile as ``fileobj`` — docker-py treats it
        as the build context when ``custom_context`` is False. No COPY/ADD
        instructions are emitted, so no extra files are needed in the tar.
        """
        fileobj = io.BytesIO(dockerfile.encode("utf-8"))
        self._client.images.build(
            fileobj=fileobj,
            tag=tag,
            rm=True,
            forcerm=True,
            pull=False,
        )

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
            # docker-py's ``volumes=`` dict is keyed by host_path: two mounts
            # sharing a host_path silently collapse into the last one. The
            # FileStore is content-addressed, so two distinct file_ids can
            # share the same storage_path (and therefore the same host_path).
            # Refuse loudly rather than ship a session whose container is
            # missing a mount the API said was 'mounted'.
            if m.host_path in volumes:
                raise SandboxError(
                    "duplicate host_path in mounts: "
                    f"{m.host_path!r} appears for both "
                    f"{volumes[m.host_path]['bind']!r} and {m.container_path!r}"
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
