# haniel

Configuration-based, intentionally indifferent service runner.

haniel monitors git repositories, pulls changes, and restarts processes.
Whether it's a Slack bot, an MCP server, or a web dashboard, haniel treats everything as just a "process."

## What haniel does

- **Git polling**: Watches configured repositories for new commits
- **Process management**: Starts, stops, and restarts services based on YAML config
- **Lifecycle hooks**: Runs post-pull commands (dependency installs, builds, etc.)
- **Health monitoring**: Detects crashes and restarts with exponential backoff + circuit breaker
- **Dependency ordering**: Starts services in the right order using `after` and `ready` conditions
- **Webhook notifications**: Sends alerts on deployments, crashes, and failures
- **MCP server**: Exposes status and control tools for Claude Code integration
- **Self-update**: Updates its own code via a two-loop architecture

## What haniel doesn't care about

- What `.env` files contain (processes load their own)
- What processes actually do
- Business dependencies between services
- Port number semantics
- Host system configuration beyond what's in `haniel.yaml`

## Installation

```bash
pip install haniel
```

## Quick start

The simplest use case is haniel managing and updating itself.

### 1. Create `haniel.yaml`

```yaml
poll_interval: 300

repos:
  haniel:
    url: git@github.com:eiaserinnys/Haniel.git
    branch: main
    path: ./haniel

self:
  repo: haniel
  auto_update: false

webhooks:
  - url: https://hooks.slack.com/services/YOUR/WEBHOOK/URL
    format: slack

mcp:
  enabled: true
  transport: sse
  port: 3200

install:
  requirements:
    python: ">=3.11"
  directories:
    - ./haniel
    - ./logs
  service:
    name: haniel
    display: "Haniel Service Runner"
    working_directory: "{root}"
    environment:
      PYTHONUTF8: "1"
```

### 2. Install and register as a Windows service

```bash
haniel install haniel.yaml
```

This clones the repo, validates requirements, and registers haniel as a Windows service via WinSW.

### 3. Start the service

```bash
sc start haniel
```

haniel now polls the repo every 5 minutes. When it detects changes to its own repo, it sends a webhook notification and waits for approval.

### 4. Approve a self-update

When haniel detects a new version of itself, it enters a pending state. Approve via the MCP tool (from Claude Code):

```
haniel_approve_update()
```

haniel exits with code 10. The wrapper script (`haniel-runner.ps1`) picks this up, runs `git pull` + `pip install`, and restarts haniel with the new code.

If you prefer automatic updates, set `auto_update: true` in the `self` section.

## Self-update architecture

haniel uses a two-loop design to solve the "surgeon can't operate on themselves" problem:

```
WinSW (Windows service)
  └─ haniel-runner.ps1 (outer loop — survives updates)
       └─ haniel run (inner loop — the actual service)
```

- **Inner loop** (`haniel run`): Monitors repos, manages services. When it detects changes to its own repo, it exits with code 10.
- **Outer loop** (`haniel-runner.ps1`): Interprets exit code 10 as "update me," runs `git pull` + `pip install`, and relaunches haniel.
- **Exit code 0**: Clean shutdown — outer loop exits too.
- **Other exit codes**: Crash — outer loop exits with the same code.

See [ADR-0002](docs/adr/0002-self-update-architecture.md) for the full decision record.

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

## Commands

| Command | Description |
|---------|-------------|
| `haniel install <config>` | Set up execution environment (dirs, venvs, secrets via Claude Code) |
| `haniel run <config>` | Start services and enter the poll loop |
| `haniel status <config>` | Show service and repository status |
| `haniel validate <config>` | Check configuration validity |

## Documentation

- [Specifications](docs/specifications.md) — Full configuration reference and runtime behavior
- [ADR-0001: WinSW over NSSM](docs/adr/0001-winsw-over-nssm.md) — Windows service wrapper choice
- [ADR-0002: Self-update architecture](docs/adr/0002-self-update-architecture.md) — Two-loop self-update mechanism

## Development

```bash
git clone https://github.com/eiaserinnys/Haniel.git
cd Haniel
pip install -e ".[dev]"
pytest
```

## License

MIT
