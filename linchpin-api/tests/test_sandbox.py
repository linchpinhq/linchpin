"""Unit tests for the Docker sandbox module."""

from __future__ import annotations

import io
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from app.sandbox import DockerSandbox, ExecResult, ResourceMount, SandboxError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tar(name: str, content: bytes) -> bytes:
    """Build a tar archive containing a single file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    buf.seek(0)
    return buf.read()


def _mock_client() -> MagicMock:
    """Return a mock docker.DockerClient."""
    return MagicMock()


# ---------------------------------------------------------------------------
# ExecResult dataclass
# ---------------------------------------------------------------------------

class TestExecResult:
    def test_fields(self):
        r = ExecResult(stdout="hello", stderr="", exit_code=0)
        assert r.stdout == "hello"
        assert r.stderr == ""
        assert r.exit_code == 0

    def test_frozen(self):
        r = ExecResult(stdout="", stderr="", exit_code=0)
        with pytest.raises(AttributeError):
            r.stdout = "nope"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DockerSandbox.create
# ---------------------------------------------------------------------------

class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_container_id(self):
        client = _mock_client()
        container = MagicMock()
        container.id = "abc123"
        client.containers.run.return_value = container

        sandbox = DockerSandbox(client=client)
        cid = await sandbox.create("myimage:latest", "linchpin-none")

        assert cid == "abc123"
        client.containers.run.assert_called_once_with(
            "myimage:latest",
            command="sleep infinity",
            network="linchpin-none",
            detach=True,
            stdin_open=True,
            tty=False,
            volumes=None,
        )

    @pytest.mark.asyncio
    async def test_create_with_mounts_passes_volumes(self):
        """PR3 — mounts list should translate to docker-py ``volumes`` dict."""
        client = _mock_client()
        container = MagicMock()
        container.id = "mnt123"
        client.containers.run.return_value = container

        mounts = [
            ResourceMount(host_path="/var/lib/linchpin/files/aa/bb/abcd", container_path="/mnt/data.csv"),
            ResourceMount(host_path="/var/lib/linchpin/files/cc/dd/cdef", container_path="/mnt/extra/file.txt", mode="ro"),
        ]
        sandbox = DockerSandbox(client=client)
        cid = await sandbox.create("img", "linchpin-none", mounts=mounts)

        assert cid == "mnt123"
        called_volumes = client.containers.run.call_args.kwargs["volumes"]
        assert called_volumes == {
            "/var/lib/linchpin/files/aa/bb/abcd": {"bind": "/mnt/data.csv", "mode": "ro"},
            "/var/lib/linchpin/files/cc/dd/cdef": {"bind": "/mnt/extra/file.txt", "mode": "ro"},
        }

    @pytest.mark.asyncio
    async def test_create_with_empty_mounts_passes_none(self):
        client = _mock_client()
        container = MagicMock()
        container.id = "x"
        client.containers.run.return_value = container

        sandbox = DockerSandbox(client=client)
        await sandbox.create("img", "net", mounts=[])

        # Empty list should normalize to None so docker-py treats the container
        # as unmounted (matches v0.1 behavior pre-PR3).
        assert client.containers.run.call_args.kwargs["volumes"] is None

    @pytest.mark.asyncio
    async def test_create_rejects_non_absolute_host_path(self):
        client = _mock_client()
        sandbox = DockerSandbox(client=client)

        with pytest.raises(SandboxError, match="host_path must be absolute"):
            await sandbox.create(
                "img",
                "net",
                mounts=[ResourceMount(host_path="relative/path", container_path="/mnt/x")],
            )
        # No container.run call should have happened — validation fails before docker.
        client.containers.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_rejects_non_absolute_container_path(self):
        client = _mock_client()
        sandbox = DockerSandbox(client=client)

        with pytest.raises(SandboxError, match="container_path must be absolute"):
            await sandbox.create(
                "img",
                "net",
                mounts=[ResourceMount(host_path="/abs/path", container_path="relative")],
            )
        client.containers.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_rejects_duplicate_host_path(self):
        """docker-py ``volumes=`` is keyed by host_path; two mounts sharing
        a host_path would silently collapse into the last one. Refuse
        loudly so the API never claims state='mounted' for a bind that
        didn't actually reach docker."""
        client = _mock_client()
        sandbox = DockerSandbox(client=client)

        with pytest.raises(SandboxError, match="duplicate host_path"):
            await sandbox.create(
                "img",
                "net",
                mounts=[
                    ResourceMount(host_path="/var/lib/linchpin/files/aa/bb/dup", container_path="/mnt/a"),
                    ResourceMount(host_path="/var/lib/linchpin/files/aa/bb/dup", container_path="/mnt/b"),
                ],
            )
        client.containers.run.assert_not_called()

    def test_build_volumes_preserves_distinct_host_paths(self):
        """Sanity check: two distinct host_paths produce two volume entries."""
        from app.sandbox import DockerSandbox
        volumes = DockerSandbox._build_volumes([
            ResourceMount(host_path="/host/a", container_path="/c/1"),
            ResourceMount(host_path="/host/b", container_path="/c/2"),
        ])
        assert volumes == {
            "/host/a": {"bind": "/c/1", "mode": "ro"},
            "/host/b": {"bind": "/c/2", "mode": "ro"},
        }

    def test_build_volumes_raises_on_duplicate_host_path(self):
        """Direct unit on the staticmethod — no docker mocks needed."""
        from app.sandbox import DockerSandbox
        with pytest.raises(SandboxError, match="duplicate host_path"):
            DockerSandbox._build_volumes([
                ResourceMount(host_path="/host/a", container_path="/c/1"),
                ResourceMount(host_path="/host/a", container_path="/c/2"),
            ])

    @pytest.mark.asyncio
    async def test_create_pulls_on_image_not_found(self):
        from docker.errors import ImageNotFound

        client = _mock_client()
        container = MagicMock()
        container.id = "pulled123"

        # First call raises ImageNotFound, second succeeds
        client.containers.run.side_effect = [
            ImageNotFound("not found"),
            container,
        ]

        sandbox = DockerSandbox(client=client)
        cid = await sandbox.create("myimage:latest", "linchpin-open")

        assert cid == "pulled123"
        client.images.pull.assert_called_once_with("myimage:latest")

    @pytest.mark.asyncio
    async def test_create_raises_sandbox_error_on_failure(self):
        from docker.errors import DockerException

        client = _mock_client()
        client.containers.run.side_effect = DockerException("boom")

        sandbox = DockerSandbox(client=client)
        with pytest.raises(SandboxError, match="Failed to create container"):
            await sandbox.create("bad:image", "linchpin-none")


# ---------------------------------------------------------------------------
# DockerSandbox.exec
# ---------------------------------------------------------------------------

class TestExec:
    @pytest.mark.asyncio
    async def test_exec_returns_result(self):
        client = _mock_client()
        container = MagicMock()
        container.exec_run.return_value = (0, (b"out", b"err"))
        client.containers.get.return_value = container

        sandbox = DockerSandbox(client=client)
        result = await sandbox.exec("cid", "echo hi")

        assert result == ExecResult(stdout="out", stderr="err", exit_code=0)
        container.exec_run.assert_called_once_with(
            ["bash", "-c", "echo hi"], demux=True
        )

    @pytest.mark.asyncio
    async def test_exec_handles_none_output(self):
        client = _mock_client()
        container = MagicMock()
        container.exec_run.return_value = (1, (None, None))
        client.containers.get.return_value = container

        sandbox = DockerSandbox(client=client)
        result = await sandbox.exec("cid", "false")

        assert result.stdout == ""
        assert result.stderr == ""
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_exec_not_found(self):
        from docker.errors import NotFound

        client = _mock_client()
        client.containers.get.side_effect = NotFound("gone")

        sandbox = DockerSandbox(client=client)
        with pytest.raises(SandboxError, match="not found"):
            await sandbox.exec("missing", "ls")


# ---------------------------------------------------------------------------
# DockerSandbox.write_file / read_file
# ---------------------------------------------------------------------------

class TestWriteFile:
    @pytest.mark.asyncio
    async def test_write_file_puts_archive(self):
        client = _mock_client()
        container = MagicMock()
        client.containers.get.return_value = container

        sandbox = DockerSandbox(client=client)
        await sandbox.write_file("cid", "/tmp/hello.txt", "world")

        container.put_archive.assert_called_once()
        call_args = container.put_archive.call_args
        assert call_args[0][0] == "/tmp"  # dest dir

    @pytest.mark.asyncio
    async def test_write_file_not_found(self):
        from docker.errors import NotFound

        client = _mock_client()
        client.containers.get.side_effect = NotFound("gone")

        sandbox = DockerSandbox(client=client)
        with pytest.raises(SandboxError, match="not found"):
            await sandbox.write_file("missing", "/tmp/x", "data")


class TestReadFile:
    @pytest.mark.asyncio
    async def test_read_file_returns_content(self):
        client = _mock_client()
        container = MagicMock()
        tar_bytes = _make_tar("hello.txt", b"file content")
        container.get_archive.return_value = (iter([tar_bytes]), {})
        client.containers.get.return_value = container

        sandbox = DockerSandbox(client=client)
        content = await sandbox.read_file("cid", "/tmp/hello.txt")

        assert content == "file content"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self):
        from docker.errors import NotFound

        client = _mock_client()
        client.containers.get.side_effect = NotFound("gone")

        sandbox = DockerSandbox(client=client)
        with pytest.raises(SandboxError, match="not found"):
            await sandbox.read_file("missing", "/tmp/x")


# ---------------------------------------------------------------------------
# DockerSandbox.destroy
# ---------------------------------------------------------------------------

class TestDestroy:
    @pytest.mark.asyncio
    async def test_destroy_stops_and_removes(self):
        client = _mock_client()
        container = MagicMock()
        client.containers.get.return_value = container

        sandbox = DockerSandbox(client=client)
        await sandbox.destroy("cid")

        container.stop.assert_called_once_with(timeout=5)
        container.remove.assert_called_once_with(force=True)

    @pytest.mark.asyncio
    async def test_destroy_ignores_not_found(self):
        from docker.errors import NotFound

        client = _mock_client()
        client.containers.get.side_effect = NotFound("already gone")

        sandbox = DockerSandbox(client=client)
        # Should not raise
        await sandbox.destroy("gone")

    @pytest.mark.asyncio
    async def test_destroy_raises_on_other_error(self):
        from docker.errors import DockerException

        client = _mock_client()
        container = MagicMock()
        container.stop.side_effect = DockerException("oops")
        client.containers.get.return_value = container

        sandbox = DockerSandbox(client=client)
        with pytest.raises(SandboxError, match="Failed to destroy"):
            await sandbox.destroy("cid")


# ---------------------------------------------------------------------------
# ensure_docker_networks
# ---------------------------------------------------------------------------

from app.sandbox import ensure_docker_networks


class TestEnsureDockerNetworks:
    @pytest.mark.asyncio
    async def test_creates_all_networks(self):
        client = _mock_client()

        await ensure_docker_networks(client=client)

        assert client.networks.create.call_count == 3
        calls = client.networks.create.call_args_list

        # First call: linchpin-none (internal)
        assert calls[0].args[0] == "linchpin-none"
        assert calls[0].kwargs["driver"] == "bridge"
        assert calls[0].kwargs["internal"] is True
        assert calls[0].kwargs["check_duplicate"] is True

        # Second call: linchpin-open (not internal)
        assert calls[1].args[0] == "linchpin-open"
        assert calls[1].kwargs["driver"] == "bridge"
        assert calls[1].kwargs["internal"] is False
        assert calls[1].kwargs["check_duplicate"] is True

        # Third call: linchpin-limited (internal in v0.2.0 — proxy in v0.2.x)
        assert calls[2].args[0] == "linchpin-limited"
        assert calls[2].kwargs["driver"] == "bridge"
        assert calls[2].kwargs["internal"] is True
        assert calls[2].kwargs["check_duplicate"] is True

    @pytest.mark.asyncio
    async def test_skips_existing_networks(self):
        from docker.errors import APIError

        client = _mock_client()
        # All networks already exist — 409 Conflict
        resp = MagicMock()
        resp.status_code = 409
        client.networks.create.side_effect = APIError(
            "Conflict", response=resp, explanation="already exists"
        )

        # Should not raise
        await ensure_docker_networks(client=client)
        assert client.networks.create.call_count == 3

    @pytest.mark.asyncio
    async def test_skips_first_creates_rest(self):
        from docker.errors import APIError

        client = _mock_client()
        resp = MagicMock()
        resp.status_code = 409
        network_mock = MagicMock()

        # First network exists, other two are new
        client.networks.create.side_effect = [
            APIError("Conflict", response=resp, explanation="already exists"),
            network_mock,
            network_mock,
        ]

        await ensure_docker_networks(client=client)
        assert client.networks.create.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_sandbox_error_on_api_error(self):
        from docker.errors import APIError

        client = _mock_client()
        resp = MagicMock()
        resp.status_code = 500
        client.networks.create.side_effect = APIError(
            "Server Error", response=resp, explanation="internal error"
        )

        with pytest.raises(SandboxError, match="Failed to create Docker network"):
            await ensure_docker_networks(client=client)


# ---------------------------------------------------------------------------
# v0.2.0 item #3 — ensure_image / derived images
# ---------------------------------------------------------------------------

class TestEnsureImage:
    @pytest.mark.asyncio
    async def test_empty_packages_returns_base_image(self):
        """No packages requested → no build, base_image returned as-is."""
        from app.models import EnvironmentPackages
        client = _mock_client()
        sandbox = DockerSandbox(client=client)

        result = await sandbox.ensure_image(
            base_image="linchpinhq/sandbox:v0.2.0",
            packages=EnvironmentPackages(),
        )

        assert result == "linchpinhq/sandbox:v0.2.0"
        client.images.get.assert_not_called()
        client.images.build.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_packages_returns_base_image(self):
        client = _mock_client()
        sandbox = DockerSandbox(client=client)

        result = await sandbox.ensure_image(
            base_image="linchpinhq/sandbox:v0.2.0",
            packages=None,
        )

        assert result == "linchpinhq/sandbox:v0.2.0"
        client.images.build.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_cached_image_without_rebuild(self):
        """If the derived image already exists locally, no build happens."""
        from app.models import EnvironmentPackages, derived_image_tag
        client = _mock_client()
        # images.get succeeds → cached
        client.images.get.return_value = MagicMock()
        sandbox = DockerSandbox(client=client)

        packages = EnvironmentPackages(apt=["jq"])
        result = await sandbox.ensure_image(
            base_image="linchpinhq/sandbox:v0.2.0",
            packages=packages,
        )

        assert result == derived_image_tag(packages)
        client.images.get.assert_called_once()
        client.images.build.assert_not_called()

    @pytest.mark.asyncio
    async def test_builds_image_when_missing(self):
        """First call with a fresh package set triggers a build."""
        from docker.errors import ImageNotFound
        from app.models import EnvironmentPackages, derived_image_tag
        client = _mock_client()
        client.images.get.side_effect = ImageNotFound("not cached yet")
        sandbox = DockerSandbox(client=client)

        packages = EnvironmentPackages(apt=["jq", "tree"], pip=["httpx"])
        expected_tag = derived_image_tag(packages)
        result = await sandbox.ensure_image(
            base_image="linchpinhq/sandbox:v0.2.0",
            packages=packages,
        )

        assert result == expected_tag
        client.images.build.assert_called_once()
        kwargs = client.images.build.call_args.kwargs
        assert kwargs["tag"] == expected_tag
        # Single-file Dockerfile context, no COPY/ADD
        rendered = kwargs["fileobj"].getvalue().decode("utf-8")
        assert rendered.startswith("FROM linchpinhq/sandbox:v0.2.0")
        assert "apt-get install -y --no-install-recommends jq tree" in rendered
        assert "pip install" in rendered and "httpx" in rendered

    @pytest.mark.asyncio
    async def test_build_failure_raises_sandbox_error(self):
        from docker.errors import BuildError, ImageNotFound
        from app.models import EnvironmentPackages
        client = _mock_client()
        client.images.get.side_effect = ImageNotFound("not cached")
        client.images.build.side_effect = BuildError(reason="apt failed", build_log=[])
        sandbox = DockerSandbox(client=client)

        packages = EnvironmentPackages(apt=["nonexistent-package-xyz"])

        with pytest.raises(SandboxError, match="Failed to build derived image"):
            await sandbox.ensure_image(
                base_image="linchpinhq/sandbox:v0.2.0",
                packages=packages,
            )


class TestGenerateDockerfile:
    """v0.2.0 item #3 — Dockerfile rendering is the security boundary
    between caller-supplied package names and the build shell."""

    def test_starts_with_from(self):
        from app.models import EnvironmentPackages
        df = DockerSandbox._generate_dockerfile(
            "linchpinhq/sandbox:v0.2.0", EnvironmentPackages(apt=["jq"])
        )
        assert df.splitlines()[0] == "FROM linchpinhq/sandbox:v0.2.0"

    def test_omits_runs_for_empty_managers(self):
        from app.models import EnvironmentPackages
        df = DockerSandbox._generate_dockerfile(
            "base", EnvironmentPackages(pip=["httpx"])
        )
        assert "pip install" in df
        assert "apt-get install" not in df
        assert "npm install" not in df
        assert "cargo install" not in df
        assert "gem install" not in df
        assert "go install" not in df

    def test_one_run_per_go_package(self):
        from app.models import EnvironmentPackages
        df = DockerSandbox._generate_dockerfile(
            "base",
            EnvironmentPackages(go=["github.com/a/b@v1", "github.com/c/d@v2"]),
        )
        assert df.count("go install") == 2

    def test_pip_uses_break_system_packages_for_pep_668(self):
        from app.models import EnvironmentPackages
        df = DockerSandbox._generate_dockerfile(
            "base", EnvironmentPackages(pip=["httpx"])
        )
        assert "--break-system-packages" in df

    def test_apt_includes_list_cleanup(self):
        """Cleanup is non-negotiable — keeps the derived image lean."""
        from app.models import EnvironmentPackages
        df = DockerSandbox._generate_dockerfile(
            "base", EnvironmentPackages(apt=["jq"])
        )
        assert "rm -rf /var/lib/apt/lists/*" in df


class TestDerivedImageTag:
    """v0.2.0 item #3 — image-tag hashing must be deterministic and
    order-independent so that two environments declaring the same packages
    share one image."""

    def test_empty_returns_none(self):
        from app.models import EnvironmentPackages, derived_image_tag
        assert derived_image_tag(EnvironmentPackages()) is None
        assert derived_image_tag(None) is None

    def test_order_independent(self):
        from app.models import EnvironmentPackages, derived_image_tag
        a = EnvironmentPackages(apt=["jq", "tree"], pip=["httpx", "rich"])
        b = EnvironmentPackages(apt=["tree", "jq"], pip=["rich", "httpx"])
        assert derived_image_tag(a) == derived_image_tag(b)

    def test_different_managers_yield_different_tags(self):
        from app.models import EnvironmentPackages, derived_image_tag
        a = EnvironmentPackages(apt=["jq"])
        b = EnvironmentPackages(pip=["jq"])  # same name, different manager
        assert derived_image_tag(a) != derived_image_tag(b)

    def test_tag_format_is_repo_colon_12hex(self):
        from app.models import EnvironmentPackages, derived_image_tag
        tag = derived_image_tag(EnvironmentPackages(apt=["jq"]))
        assert tag is not None
        repo, hex_part = tag.split(":")
        assert repo == "linchpinhq/sandbox-env"
        assert len(hex_part) == 12
        assert all(c in "0123456789abcdef" for c in hex_part)
