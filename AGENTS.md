# Agent Notes

## Project Type
Single-package Python 3.12 asyncio service. No test suite in repo. No linter/formatter config present.

## Run / Entry Point
```bash
pip install -r requirements.txt
python -m src
```
Entry is `src/__main__.py`. Do not guess a `main.py` at root.

## Configuration Split
- `.env` — environment variables (API keys, Telegram, logging, API server toggles).
- `config.yml` — domains, zones, node IPs, check intervals.

Both files are required at runtime. Copy from `.env.example` and `config.example.yml`.

### Env var substitution in config.yml
`config.yml` supports `${VAR}` syntax (substituted at load time). This is implemented in `Config._substitute_env_vars()`.

### Runtime mutation
`config.yml` is mutated at runtime by the HTTP API, by `Config.update_check_interval()`, and by
auto-discovery (`src/discovery.py` → `Config.add_discovered_nodes()`, opt-in via `auto-discovery.enabled`).
It is also mutated by `/maintenance` and `/api/maintenance` (`Config.add_maintenance()` / `remove_maintenance()`).
The file is rewritten with `yaml.dump(..., sort_keys=False)`, which drops comments. Mutations edit
`_raw_config` by index so `${VAR}` placeholders survive. `Config._save()` copies the old file to
`<STATE_FILE dir>/config-backups/` first (10 kept), writes temp+rename, and falls back to an in-place
write on `EBUSY` (Docker single-file bind mount). On any failure the in-memory config is reloaded from disk.
`Config.reload()` validates and restores the previous config if the new file is invalid.

## Health pipeline
Per cycle (`MonitoringService._run_cycle`): panel status (`is_node_healthy`: connected, not disabled)
→ optional TCP probe (`src/probe.py`) → maintenance filter → flap protection
(`fail-threshold` / `recover-threshold` streaks) → DNS + host sync, skipped while paused. Node alerts fire on
*effective* flips only. `_previous_node_states` is the effective state and is what `StateStore` persists.

## Docker
Primary deployment is Docker. Use `docker compose up -d` (image from DockerHub) or `docker compose -f docker-compose.dev.yml up -d` (local build with `src/` mounted read-only).

## HTTP API
- Optional FastAPI server toggled via `API_ENABLED` in `.env`.
- `API_TOKEN` is **required** when `API_ENABLED=true` and must be exactly a **64-character lowercase hex string** (regex `^[0-9a-f]{64}$`). Generate with `openssl rand -hex 32`.
- Auth header: `X-API-Key: <token>`.
- Apex (root) zones use `name: "@"` in YAML and must be URL-encoded as `%40` in API paths (`PATCH /api/config/domains/example.com/zones/%40`).
- Swagger UI at `/api/docs` only when `API_DOCS=true`.

## CLI (inside container)
The Dockerfile installs a `cli` command:
```bash
docker exec -it remnawave-cloudflare-nodes cli
```
Options: Show config, Validate config, Reload config (hot).

## Hot Reload
The process handles `SIGHUP` to reload `config.yml` and `hosts.yml` from disk without restart
(`Config.reload()`). The `cli` tool sends `SIGHUP` to PID 1 inside the container.

**`.env` is NOT reloaded.** Every env-backed setting (`LOG_LEVEL`, all `TELEGRAM_*` toggles,
`DISABLE_UNREACHABLE_HOSTS`, `API_*`) needs a container restart. Under Docker `env_file` the
variables are injected into the process environment, which `load_dotenv()` cannot override.

## Shutdown
`SIGTERM`/`SIGINT` are handled via `loop.add_signal_handler`, which sets an `asyncio.Event`.
Do not go back to `signal.signal` + raising `SystemExit`: that surfaces the exception in the
event loop rather than in the coroutine, so the `except` blocks never fire and shutdown
waits out the full check interval.

## Telegram commands
`src/telegram/commands.py` (`CommandBot`, opt-in via `TELEGRAM_COMMANDS_ENABLED`) long-polls on the
notifier's `Bot` with `handle_signals=False, close_bot_session=False`. It only answers `TELEGRAM_CHAT_ID`
(optionally narrowed by `TELEGRAM_COMMAND_USERS`). `/reload` goes through the same async `reload_config()`
as `SIGHUP`, which holds `MonitoringService.lock` so the config never changes under a running cycle.

## Persistent State
`StateStore` (`src/state.py`) keeps node/host transition state, the DNS pause, the maintenance set seen
last cycle, and the summary statistics (`src/summary.py`) in `STATE_FILE`
(default `data/state.json`, needs a mounted volume). Writes are atomic (temp file + rename).
A non-writable directory degrades to in-memory state with a warning — never a crash.

## CI / Release
- `.github/workflows/dockerhub-publish.yaml` — builds and pushes multi-arch (`linux/amd64`, `linux/arm64`) image on `v*` tags.
- `.github/workflows/dockerhub-publish-dev.yaml` — pushes `hteppl/remnawave-cloudflare-nodes:dev` on every push to `dev` branch.

## Migration Context
From v1.x → v1.4, `logging`, `api`, and `telegram` blocks moved from `config.yml` to `.env`. Old blocks in `config.yml` are ignored but should be removed. See `docs/MIGRATION.md`.

## Key Dependencies
`requirements.txt` is a **fully pinned lock file** (direct + transitive) generated from
`requirements.in`. Edit `requirements.in` and regenerate; never loosen the pins in
`requirements.txt`. Unpinned versions previously let a stale `remnawave` SDK ship in a
rebuilt image and silently break host sync against panel v2.8.

- `fastapi==0.136.1` + `uvicorn==0.46.0` (API server)
- `remnawave==2.8.0` (panel client — must match the panel's major schema)
- `cloudflare==5.6.0` (DNS)
- `aiogram==3.30.0` (Telegram bot)
- `httpx==0.27.2` (used directly for panel host calls)
- `python-dotenv==1.2.2`, `pyyaml==6.0.3`

### Panel schema coupling
`src/panel/client.py` deliberately does **not** use the SDK's `HostResponseDto`. It parses
`/api/hosts` into a minimal `PanelHost` (uuid, address, remark, isDisabled) so unrelated
panel schema changes cannot break host synchronisation. Node fetching still uses the SDK.

## Code Layout
```
src/
  __main__.py          # asyncio entry point: signal handling + monitoring loop + optional API server
  config.py            # Config class: loads .env + YAML, env substitution, hot reload, mutations
  monitoring_service.py # Health check orchestration and DNS sync
  panel/               # Remnawave API client and node health logic
  cloudflare_dns/      # Cloudflare API client and DNS record manager
  api/                 # FastAPI app with auth dependency
  telegram/            # Telegram notifier and event formatting
  cli.py               # Interactive CLI for show / validate / reload
  discovery.py         # Auto-discovery of panel nodes into config.yml
  probe.py             # Optional TCP reachability probe
  summary.py           # Uptime statistics and the periodic summary
  state.py             # Persistent state (state.json)
  i18n/                # Fluent runtime translations (en, ru)
  utils/               # logger, DNS helpers, time helpers
```
