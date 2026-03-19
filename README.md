# haniel

[![CI](https://github.com/eiaserinnys/haniel/actions/workflows/test.yml/badge.svg)](https://github.com/eiaserinnys/haniel/actions/workflows/test.yml)

Configuration-based, intentionally indifferent service runner.

haniel monitors git repositories, pulls changes, and restarts processes.
Whether it's a Slack bot, an MCP server, or a web dashboard, haniel treats everything as just a "process."

## What haniel does

- **Git polling**: Watches configured repositories for new commits
- **Process management**: Starts, stops, and restarts services based on YAML config
- **Lifecycle hooks**: Runs `pre_start` and `post_pull` commands (dependency installs, builds, etc.)
- **Health monitoring**: Detects crashes and restarts with exponential backoff + circuit breaker
- **Dependency ordering**: Starts services in the right order using `after` and `ready` conditions
- **Webhook notifications**: Sends alerts on deployments, crashes, and failures (Slack, Discord, JSON)
- **MCP server**: Exposes status and control tools for Claude Code integration via Streamable HTTP
- **Web dashboard**: React-based management UI with real-time WebSocket updates and Claude Code chat panel
- **Config API**: Runtime YAML configuration CRUD (add/update/remove services and repos without restart)
- **Self-update**: Updates its own code via a two-loop architecture

## What haniel doesn't care about

- What `.env` files contain (processes load their own)
- What processes actually do
- Business dependencies between services
- Port number semantics
- Host system configuration beyond what's in `haniel.yaml`

## Quick start

### Prerequisites

- Windows 10+ with PowerShell 5.1+
- **Administrator privileges** (required for service registration and PATH modification)

### One-liner install

Open PowerShell **as Administrator** (right-click → "Run as Administrator") and run:

```powershell
irm https://raw.githubusercontent.com/eiaserinnys/haniel/main/install-haniel.ps1 | iex
```

The bootstrap script handles everything:

| Step | What it does |
|------|-------------|
| 0. Git | Checks for Git, offers to install via winget if missing |
| 1. Python | Checks for Python 3.11+, offers to install via winget if missing |
| 2. Directory | Creates root directory, downloads WinSW |
| 3. Clone | Clones haniel into `.self/`, creates venv, installs |
| 4. Config | Downloads your `haniel.yaml` to root |
| 5. Install | Runs `haniel install` (directories, venvs, WinSW registration) |
| 6. Start | Starts the Windows service via `sc start` |

After completion, haniel is running as a Windows service and polling for updates.

### Directory layout

```
{root}/                      # e.g. C:\Services\Haniel
+-- haniel.yaml              # Single config for all services
+-- .self/                   # haniel's own repo (self-update)
|   +-- .venv/               # haniel's Python venv
|   +-- src/haniel/...
|   +-- haniel-runner.ps1
+-- .services/               # Managed service repos
    +-- some-service-a/
    +-- some-service-b/
```

Adding a new service = edit `haniel.yaml` + restart. No re-bootstrapping needed.
See [ADR-0003](docs/adr/0003-directory-structure.md) for details.

### Self-managing config

The included [`haniel.yaml`](haniel.yaml) is a minimal config where haniel manages and updates only itself:

```yaml
poll_interval: 300

repos:
  haniel:
    url: https://github.com/eiaserinnys/haniel.git
    branch: main
    path: ./.self

self:
  repo: haniel
  auto_update: false
```

haniel polls its own repo every 5 minutes. When it detects a new version, it sends a webhook notification and waits for approval.

### Approving a self-update

When haniel detects changes to its own repo, it enters a pending state. Approve via the MCP tool (from Claude Code):

```
haniel_approve_update()
```

haniel exits with code 10. The wrapper script (`haniel-runner.ps1`) picks this up, runs `git pull` + `pip install`, and restarts haniel with the new code.

For automatic updates without approval, set `auto_update: true`.

## Self-update architecture

haniel uses a two-loop design to solve the "surgeon can't operate on themselves" problem:

```
WinSW (Windows service)
  +-- haniel-runner.ps1 (outer loop -- survives updates)
       +-- haniel run (inner loop -- the actual service)
```

- **Inner loop** (`haniel run`): Monitors repos, manages services. When it detects changes to its own repo, it exits with code 10.
- **Outer loop** (`haniel-runner.ps1`): Interprets exit code 10 as "update me," runs `git pull` + `pip install`, and relaunches haniel.
- **Exit code 0**: Clean shutdown — outer loop exits too.
- **Other exit codes**: Crash — outer loop exits with the same code.

See [ADR-0002](docs/adr/0002-self-update-architecture.md) for the full decision record.

## Web dashboard

haniel includes a built-in web dashboard for managing services through a browser.

### Enabling the dashboard

```yaml
dashboard:
  enabled: true
  port: 3200       # Shares port with MCP server
  token: "secret"  # Bearer token for API/WebSocket authentication
```

The dashboard is mounted on the same Starlette server as the MCP endpoint. No additional port is needed.

### Features

- **Service management**: Start, stop, restart, enable/disable services
- **Repository status**: View repo state, trigger manual pulls
- **Real-time updates**: WebSocket event stream (state changes, repo updates, self-update notifications)
- **Config editor**: Add, update, remove services and repos at runtime (atomic write with backup)
- **Dependency graph**: Visual display of service dependency relationships
- **Log viewer**: Live service log output
- **Claude Code chat**: Integrated chat panel powered by `claude-agent-sdk`
- **Self-update control**: Approve pending updates from the browser

### REST API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Overall haniel status |
| GET | `/api/services` | All services with state |
| POST | `/api/services/{name}/start` | Start a service |
| POST | `/api/services/{name}/stop` | Stop a service |
| POST | `/api/services/{name}/restart` | Restart a service |
| POST | `/api/services/{name}/enable` | Toggle service enabled/disabled |
| GET | `/api/services/{name}/logs` | Service log output |
| GET | `/api/repos` | Repository statuses |
| POST | `/api/repos/{name}/pull` | Trigger manual pull |
| POST | `/api/self-update/approve` | Approve pending self-update |
| POST | `/api/reload` | Reload haniel.yaml |
| GET | `/api/config` | Full YAML config |
| GET/PUT/POST/DELETE | `/api/config/services/{name}` | Service CRUD |
| GET/PUT/POST/DELETE | `/api/config/repos/{name}` | Repository CRUD |

All `/api/*` and `/ws` endpoints require `Authorization: Bearer <token>`.

### WebSocket

- `/ws` — Real-time event stream (`state_change`, `repo_change`, `self_update_pending`, `reload_complete`)
- `/ws/chat` — Claude Code chat session bridge

## Managing multiple services

For a full multi-service setup with dependency ordering, lifecycle hooks, and Claude Code-assisted installation, see [`haniel.yaml.example`](haniel.yaml.example).

```bash
# Preview what install would do
haniel install --dry-run haniel.yaml

# Validate configuration
haniel validate haniel.yaml

# Show current status
haniel status haniel.yaml
```

## Configuration highlights

Beyond the basics, haniel supports:

| Feature | Config key | Description |
|---------|-----------|-------------|
| Auto-apply control | `services.*.auto_apply` | `false` to detect changes without auto-restarting; apply manually via dashboard |
| Pre-start hooks | `services.*.hooks.pre_start` | Commands to run before service start (in addition to `post_pull`) |
| Build step | `install.environments.*.build` | Post-install build command (e.g. `pnpm run build`) |
| pnpm support | `install.environments.*.type: pnpm` | pnpm as environment type alongside `python-venv` and `npm` |
| Service account | `install.service.service_account` | Run the Windows service under a specific user (`username`, `password`) |
| Cogito reflect | `services.*.reflect` | Expose `/reflect` endpoint for service introspection |

See [Specifications](docs/specifications.md) for the full configuration reference.

## Commands

| Command | Description |
|---------|-------------|
| `haniel install <config>` | Set up execution environment (dirs, venvs, secrets via Claude Code) |
| `haniel run <config>` | Start services and enter the poll loop |
| `haniel status <config>` | Show service and repository status |
| `haniel validate <config>` | Check configuration validity |

## MCP integration

haniel exposes its state and control interface through the Model Context Protocol.

**Transport**: Streamable HTTP (default), SSE also supported via `mcp.transport` config.

**Resources** (read-only):
- `haniel://status` — Overall status
- `haniel://repos` — Repository information
- `haniel://services/{name}/logs` — Service logs

**Tools** (control):
- `haniel_start`, `haniel_stop`, `haniel_restart` — Service lifecycle
- `haniel_pull` — Trigger repository pull
- `haniel_enable` — Toggle service enabled/disabled
- `haniel_reload` — Reload configuration
- `haniel_approve_update` — Approve pending self-update

Connect from Claude Code with:

```json
{
  "mcpServers": {
    "haniel": {
      "type": "http",
      "url": "http://localhost:3200/mcp/http"
    }
  }
}
```

## Documentation

- [Configuration Reference](docs/configuration.md) — Every field in `haniel.yaml` explained
- [Specifications](docs/specifications.md) — Architecture, runtime behavior, and installation flow
- [ADR-0001: WinSW over NSSM](docs/adr/0001-winsw-over-nssm.md) — Windows service wrapper choice
- [ADR-0002: Self-update architecture](docs/adr/0002-self-update-architecture.md) — Two-loop self-update mechanism
- [ADR-0003: Directory structure](docs/adr/0003-directory-structure.md) — `.self/` + `.services/` layout

## Development

```bash
git clone https://github.com/eiaserinnys/haniel.git
cd haniel
pip install -e ".[dev]"
pytest
```

**Requirements**: Python 3.11+, `claude-agent-sdk` (bundled as a dependency for the dashboard chat panel).

The dashboard frontend is a separate React app under `dashboard/`:

```bash
cd dashboard
pnpm install
pnpm run build    # Build static assets (served by haniel at runtime)
pnpm run dev      # Dev server with hot reload
```

## License

MIT
