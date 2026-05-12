# Contributing to Linchpin

Thanks for your interest in Linchpin. PRs, issues, and design discussions are all welcome.

## Quick checklist

1. Open an issue first for non-trivial changes — a 1-paragraph problem statement saves us all time.
2. Branch from `main`, keep PRs focused, and make sure tests pass.
3. Sign off your commits (see [DCO](#developer-certificate-of-origin-dco) below). The CI bot enforces this.
4. By contributing you license your work under [Apache-2.0](LICENSE).

## Development

```bash
# API
cd linchpin-api
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
alembic upgrade head
pytest

# Connector
cd ../linchpin-connector
uv pip install -e ".[dev]"
pytest

# Console
cd ../linchpin-console
pnpm install && pnpm dev
```

See [README.md](README.md) for the full stack quickstart and [ARCHITECTURE.md](ARCHITECTURE.md) for how it's wired together.

## Developer Certificate of Origin (DCO)

We use the [DCO](https://developercertificate.org/) instead of a CLA. Each commit must include a `Signed-off-by:` line indicating that you wrote (or have the right to submit) the change under the project's license.

Add it automatically with `git commit -s`:

```bash
git commit -s -m "feat(orchestrator): add request idempotency"
```

The resulting commit footer looks like:

```
Signed-off-by: Your Name <your.email@example.com>
```

By signing off you certify the [DCO 1.1](https://developercertificate.org/) — that you have the right to submit the work under the project's license.

## What we look for in PRs

- Tests for new behavior, regression tests for fixes.
- Documentation updates when public API surface changes (README, ARCHITECTURE.md, route docs).
- Small, focused commits — one concern per PR.
- Honest commit messages: lead with what changed.

## Reporting security issues

Don't open a public issue. Email security disclosures to security@linchpin.work (or open a private GitHub Security Advisory). We'll acknowledge within 72 hours and coordinate disclosure.

## License

By contributing you agree your contributions are licensed under [Apache-2.0](LICENSE), with the patent grant in section 3 of that license applying to your contributions.
