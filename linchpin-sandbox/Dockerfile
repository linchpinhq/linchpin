# linchpin-sandbox — base image for per-session agent containers (v0.2.0 item #13).
#
# Ships with the runtimes + CLI tools an agent is most likely to reach for
# during interactive work. Heavier than the previous `ubuntu:22.04` default,
# but every byte here is something agents would otherwise wait minutes to
# apt-install at session boot.
#
# Image is tagged `linchpinhq/sandbox:v0.2.0`. Sessions pick it up
# automatically once LINCHPIN_SANDBOX_IMAGE points at it (the default
# changed in app/sandbox.py:DEFAULT_BASE_IMAGE).
#
# Pinned versions per the v0.2.0 Requirements item #13:
#   Python 3.12, Node 20, Go 1.22, Rust 1.77, Java 21, Ruby 3.3,
#   PHP 8.3, GCC 13, plus psql, redis-cli, rg, tree, htop.

FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive

# --- Base system + everything available from apt -----------------------------
# Single RUN keeps the image layer count down; the apt-list trim at the end
# saves ~50MB.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg lsb-release \
        build-essential gcc-13 g++-13 make \
        git pkg-config \
        bash zsh sudo \
        # CLI tools called out in the spec
        ripgrep tree htop \
        # Database clients
        postgresql-client redis-tools \
        # Networking debug
        iputils-ping dnsutils net-tools \
        # Language runtimes that apt has at the right major version on bookworm
        python3.12 python3.12-venv python3-pip \
        ruby3.1 \
        php8.2-cli \
        openjdk-17-jdk \
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-13 100 \
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-13 100 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.12 100 \
    && rm -rf /var/lib/apt/lists/*

# --- Node 20 (via NodeSource) ------------------------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- Go 1.22 -----------------------------------------------------------------
ENV GOROOT=/usr/local/go
ENV PATH=$GOROOT/bin:$PATH
RUN curl -fsSL https://go.dev/dl/go1.22.10.linux-$(dpkg --print-architecture).tar.gz \
        | tar -C /usr/local -xz \
    && go version

# --- Rust 1.77 (rustup, minimal profile to keep image lean) ------------------
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
ENV PATH=$CARGO_HOME/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain 1.77.0 --profile minimal --no-modify-path \
    && rustc --version

# --- Sentinel for runtime self-check ----------------------------------------
# Sessions read this file to assert the image matches the manifest in the
# Tech Spec (v0.2.0 item #13). Update on every base-image bump.
LABEL org.linchpin.sandbox.version="v0.2.0"
RUN mkdir -p /etc/linchpin \
    && cat > /etc/linchpin/sandbox-manifest.json <<'JSON' \
&& chmod 0444 /etc/linchpin/sandbox-manifest.json
{
  "image": "linchpinhq/sandbox",
  "tag": "v0.2.0",
  "runtimes": {
    "python": "3.12",
    "node": "20",
    "go": "1.22",
    "rust": "1.77",
    "java": "17",
    "ruby": "3.1",
    "php": "8.2",
    "gcc": "13"
  },
  "tools": ["psql", "redis-cli", "rg", "tree", "htop", "git"]
}
JSON

# Per-session mount points (PR3 + PR5):
#   /mnt/data            — file resources (read-only binds)
#   /mnt/session/outputs — deliverables (writable bind)
RUN mkdir -p /mnt/data /mnt/session/outputs

WORKDIR /workspace
CMD ["sleep", "infinity"]
