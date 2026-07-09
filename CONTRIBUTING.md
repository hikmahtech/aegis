# Contributing to AEGIS

AEGIS is a personal project built to be **forked and configured for your own
life**. That shapes how to contribute: most people will fork and adapt rather
than send changes upstream, and that's the intended path. But bug fixes,
portability improvements (making something that's hardcoded to the maintainer's
setup configurable), and docs are very welcome.

## Ground rules

- **Keep it fork-friendly.** Don't hardcode anything specific to one deployment
  — a host, an ID, a cluster name, a currency, a timezone. If you need a value
  that varies per install, add a setting (env var via `AEGIS_*` / admin UI),
  default it to something neutral, and document it. This is the single most
  useful kind of contribution.
- **No secrets, ever.** Never commit real credentials, tokens, or `.env`
  contents. Tests use obviously-fake fixtures. See [SECURITY.md](SECURITY.md).
- **Small, focused PRs.** One concern per PR; a reviewer should be able to hold
  the whole diff in their head.

## Dev setup

```bash
# 1. Clone your fork
git clone https://github.com/<you>/aegis && cd aegis

# 2. Python env + all three packages (editable, with dev extras)
python -m venv .venv && source .venv/bin/activate
pip install -e "core[dev]" -e "worker[dev]" -e "comms[dev]"

# 3. Real Postgres for the test suite (no DB mocks) — port 25432
docker compose up -d postgres

# 4. Admin SPA (only if you touch admin-panel/frontend/)
cd admin-panel/frontend && npm ci && npm run build && cd -
```

To run the whole thing locally, `cp config/.env.example config/.env`, fill in
the admin login, and `docker compose up -d` (see
[docs/development.md](docs/development.md)).

## Before you open a PR

```bash
pytest            # full suite — needs the Postgres from step 3
ruff check .      # lint — this is what CI enforces
ruff format .     # format (but NOT on core/src/aegis/services/chat.py — see note)
```

- **CI is test-only.** The GitHub Actions workflows run `ruff check` + `pytest`
  on every push/PR and nothing else — no deploy, no secrets — so a PR from a
  fork runs cleanly with zero configuration. DB-dependent tests self-skip when
  no Postgres is reachable.
- **`chat.py` formatting:** `core/src/aegis/services/chat.py` has local-ruff
  version drift — do **not** run `ruff format` on it (it rewrites the whole
  file). Write already-formatted edits; `ruff check` must still pass.
- **Adding a flow / chat tool / connector:** the conventions live in
  [CLAUDE.md](CLAUDE.md) and [docs/development.md](docs/development.md) —
  they're explicit lists (nothing is auto-discovered), so follow the checklist
  there.

## Reporting bugs & requesting features

Use [GitHub Issues](https://github.com/hikmahtech/aegis/issues). For bugs,
include repro steps, the relevant file paths, and what you expected. For a
security issue, do **not** open a public issue — see [SECURITY.md](SECURITY.md).

## License

By contributing you agree your contributions are licensed under the project's
[MIT License](LICENSE).
