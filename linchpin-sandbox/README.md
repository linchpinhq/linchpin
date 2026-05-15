# linchpin-sandbox

Base image for per-session agent containers. Sessions in `linchpin-api` boot
this image (or whatever `LINCHPIN_SANDBOX_IMAGE` is set to) and bind-mount
file resources + the deliverables outputs directory into it.

The image targets the "richer base" specified in v0.2.0 Requirements item
#13 — every byte here is something agents would otherwise wait minutes to
apt-install at session boot.

## Contents

| Tool / runtime | Version | Notes |
|---|---|---|
| Python | 3.12 | `python` + `python3` both point here |
| Node.js | 20.x | via NodeSource APT repo |
| Go | 1.22.10 | `/usr/local/go/bin` on PATH |
| Rust | 1.77.0 | rustup, minimal profile (no docs / extra components) |
| Java | 17 (OpenJDK) | Debian Bookworm ships 17; 21 needs a third-party APT source we deferred |
| Ruby | 3.1 | Bookworm default; 3.3 needs rbenv we deferred |
| PHP | 8.2 | Bookworm default; 8.3 needs Sury repo we deferred |
| GCC / G++ | 13 | via `gcc-13` apt package, `update-alternatives` points `gcc`/`g++` at 13 |
| `psql`, `redis-cli` | latest from apt | per spec |
| `rg`, `tree`, `htop`, `git` | latest from apt | per spec |
| `iputils-ping`, `dnsutils`, `net-tools` | — | basic networking debug |

> **Version drift from spec**: Java 21 / Ruby 3.3 / PHP 8.3 each needed a
> separate APT source on Debian Bookworm. v0.2.0 ships the latest version
> available from the base repo to minimize supply-chain surface; bumping
> to spec-pinned versions is its own follow-on RFC.

## Build

```bash
docker build -t linchpinhq/sandbox:v0.2.0 linchpin-sandbox/
```

## Self-check

The image carries a manifest at `/etc/linchpin/sandbox-manifest.json` that
sessions can read to verify they're running on the expected image:

```bash
docker run --rm linchpinhq/sandbox:v0.2.0 cat /etc/linchpin/sandbox-manifest.json
```

## Bumping the image

When you change the version pins above:

1. Bump the `LABEL org.linchpin.sandbox.version` and the `tag` field of the
   manifest in `Dockerfile`.
2. Update the version pins in this README.
3. Update `DEFAULT_BASE_IMAGE` in `linchpin-api/app/sandbox.py`.
4. Push the new tag to the registry (CI handles this on tagged releases).

## Mount layout

The image creates two well-known paths the session orchestrator binds into:

| Path | Mode | Used for |
|---|---|---|
| `/mnt/data/` | read-only | File resources passed at session create (`resources[]` with `type=file`) |
| `/mnt/session/outputs/` | writable | Deliverables — anything the agent writes here is auto-registered via the Files API |
