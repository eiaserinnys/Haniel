# haniel

<p align="center">
  <img src="docs/haniel-intro.png" alt="haniel" width="720">
</p>

[![CI](https://github.com/eiaserinnys/haniel/actions/workflows/test.yml/badge.svg)](https://github.com/eiaserinnys/haniel/actions/workflows/test.yml)

**A service runner that your AI agent can operate.**

haniel manages processes, polls git repos, and exposes everything through [MCP](https://modelcontextprotocol.io/) —
so Claude Code can deploy, restart, monitor, and configure your services through natural conversation.

```
You:    "Deploy the latest changes to api-server"
Claude: haniel_pull(repo="api") → haniel_restart(service="api-server")
        ✅ Pulled 3 commits, api-server restarted successfully.
```

## The problem

You have a few services running on a machine. You want to:
- Deploy by pulling from git, not by building Docker images
- Let your AI agent handle routine ops (restart, rollback, check logs)
- See what's happening from a dashboard

Existing tools don't quite fit:

| Tool | Gap |
|------|-----|
| **PM2 / systemd** | No AI interface. Agent must generate shell commands and parse text output. |
| **Docker Compose** | Assumes containerized workflows. Overkill when you just want to `git pull` and restart. |
| **Coolify / CapRover** | Full PaaS with their own deployment model. You're adopting a platform, not a tool. |

haniel is a **single YAML file** + a process that your AI agent already knows how to talk to.

## How it works

1. **You write `haniel.yaml`** — repos to poll, services to run, how they depend on each other
2. **haniel runs as a service** — polls git, manages processes, restarts on crash
3. **Claude Code connects via MCP** — every operation is a tool call, not a shell command

```yaml
# haniel.yaml
poll_interval: 60

repos:
  backend:
    url: https://github.com/you/backend.git
    branch: main
    path: ./.services/backend

services:
  api:
    run: python -m uvicorn app:main --port 8000
    cwd: ./.services/backend
    repo: backend
    hooks:
      post_pull: pip install -r requirements.txt
```

That's it. haniel watches the repo, pulls changes, runs the hook, and restarts the service.
Claude Code can do all of this on demand, or haniel does it automatically.

## What haniel does

- **Git polling** — watches repositories, pulls on new commits
- **Process management** — start, stop, restart with dependency ordering
- **Lifecycle hooks** — `pre_start` and `post_pull` commands (installs, builds, migrations)
- **Health monitoring** — crash detection, exponential backoff, circuit breaker
- **MCP server** — full control surface for Claude Code via Streamable HTTP
- **Web dashboard** — real-time UI with integrated Claude Code chat panel
- **Runtime config** — add/update/remove services and repos without restart
- **Self-update** — haniel updates its own code via a two-loop architecture
- **Webhook notifications** — Slack, Discord, or generic JSON on deploys and failures

## Quick start

### Prerequisites

- Python 3.11+
- Windows 10+ with PowerShell 5.1+ (Linux/macOS support planned)
- **Administrator privileges** for service registration

### One-liner install

Open PowerShell **as Administrator**:

```powershell
irm https://raw.githubusercontent.com/eiaserinnys/haniel/main/install-haniel.ps1 | iex
```

This clones haniel, creates a venv, registers a Windows service, and starts polling.

### Connect Claude Code

Add to your MCP config:

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

Now Claude Code can operate your infrastructure:

```
"What services are running?"          → haniel://status
"Show me api-server logs"             → haniel_read_logs(service="api-server")
"Restart the worker"                  → haniel_restart(service="worker")
"Check if there are pending updates"  → haniel_check_updates()
"Pull and deploy backend"             → haniel_update(service="api")
"Add a new service for the bot"       → haniel_create_service_config(...)
```

## MCP interface

**Resources** (read-only):
- `haniel://status` — overall status
- `haniel://repos` — repository information
- `haniel://services/{name}/logs` — service logs

**Tools** (control):
- `haniel_start`, `haniel_stop`, `haniel_restart` — service lifecycle
- `haniel_pull` — trigger git pull
- `haniel_enable` — toggle service on/off
- `haniel_reload` — reload configuration
- `haniel_check_updates` — check for pending changes
- `haniel_update` — pull + restart (or self-update)
- `haniel_read_logs` — read logs with optional grep
- Config CRUD — `haniel_create_service_config`, `haniel_update_service_config`, `haniel_delete_service_config`, and repo equivalents

## Web dashboard

```yaml
dashboard:
  enabled: true
  port: 3200       # Shares port with MCP server
  token: "secret"  # Bearer token for authentication
```

Mounted on the same server as MCP — no extra port needed.

- Service management (start/stop/restart/enable)
- Repository status and manual pull
- Real-time WebSocket updates
- Config editor with atomic write + backup
- Dependency graph visualization
- Live log viewer
- Claude Code chat panel
- Self-update approval

### REST API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Overall status |
| GET | `/api/services` | All services with state |
| POST | `/api/services/{name}/start` | Start a service |
| POST | `/api/services/{name}/stop` | Stop a service |
| POST | `/api/services/{name}/restart` | Restart a service |
| POST | `/api/services/{name}/enable` | Toggle enabled |
| GET | `/api/services/{name}/logs` | Service logs |
| GET | `/api/repos` | Repository statuses |
| POST | `/api/repos/{name}/pull` | Manual pull |
| POST | `/api/self-update/approve` | Approve self-update |
| POST | `/api/reload` | Reload config |
| GET/PUT/POST/DELETE | `/api/config/services/{name}` | Service CRUD |
| GET/PUT/POST/DELETE | `/api/config/repos/{name}` | Repo CRUD |

All endpoints require `Authorization: Bearer <token>`.

### WebSocket

- `/ws` — event stream (`state_change`, `repo_change`, `self_update_pending`)
- `/ws/chat` — Claude Code chat bridge

## Self-update

haniel solves the "surgeon can't operate on themselves" problem with a two-loop design:

```
WinSW (Windows service)
  └── haniel-runner.ps1  (outer loop — survives updates)
       └── haniel run    (inner loop — the actual service)
```

When haniel detects changes to its own repo, it exits with code 10.
The outer loop interprets this as "update me," runs `git pull` + `pip install`, and relaunches.

Approve updates from Claude Code (`haniel_update(service="haniel")`) or set `auto_update: true` for hands-free updates.

See [ADR-0002](docs/adr/0002-self-update-architecture.md) for details.

## Directory layout

```
{root}/
├── haniel.yaml              # Single config file
├── .self/                   # haniel's own repo (self-update)
│   ├── .venv/
│   ├── src/haniel/...
│   └── haniel-runner.ps1
└── .services/               # Managed service repos
    ├── backend/
    └── worker/
```

Adding a service = edit `haniel.yaml`. No re-bootstrapping needed.

## Commands

| Command | Description |
|---------|-------------|
| `haniel run <config>` | Start services and enter the poll loop |
| `haniel install <config>` | Set up environment (dirs, venvs, service registration) |
| `haniel status <config>` | Show service and repo status |
| `haniel validate <config>` | Check configuration validity |

## Documentation

- [Configuration Reference](docs/configuration.md) — every field in `haniel.yaml`
- [Specifications](docs/specifications.md) — architecture, runtime behavior, installation flow
- [ADR-0001: WinSW over NSSM](docs/adr/0001-winsw-over-nssm.md)
- [ADR-0002: Self-update architecture](docs/adr/0002-self-update-architecture.md)
- [ADR-0003: Directory structure](docs/adr/0003-directory-structure.md)

## Development

```bash
git clone https://github.com/eiaserinnys/haniel.git
cd haniel
pip install -e ".[dev]"
pytest
```

**Requirements**: Python 3.11+

Dashboard frontend (React):

```bash
cd dashboard
pnpm install
pnpm run build    # Static assets served by haniel
pnpm run dev      # Dev server with hot reload
```

## License

MIT
