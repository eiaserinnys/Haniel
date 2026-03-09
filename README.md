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
- **Self-update**: Updates its own code via a two-loop architecture (see [ADR-0002](docs/adr/0002-self-update-architecture.md))

## What haniel doesn't care about

- What `.env` files contain (processes load their own)
- What processes actually do
- Business dependencies between services
- Port number semantics
- Host system configuration beyond what's in `haniel.yaml`

## Quick start

```yaml
# haniel.yaml
poll_interval: 60

repos:
  my-app:
    url: git@github.com:org/my-app.git
    branch: main
    path: ./projects/my-app

services:
  my-service:
    run: python -m my_app.server
    cwd: ./projects/my-app
    repo: my-app
    ready: port:8080
```

## Commands

```bash
# Set up the execution environment (directories, venvs, secrets via Claude Code)
haniel install haniel.yaml

# Start services and enter the poll loop
haniel run haniel.yaml

# Show current status
haniel status haniel.yaml

# Validate configuration without running
haniel validate haniel.yaml
```

## Installation

```bash
pip install haniel
```

Development:

```bash
git clone https://github.com/eiaserinnys/Haniel.git
cd Haniel
pip install -e ".[dev]"
```

## Documentation

- [Specifications](docs/specifications.md) - Full configuration reference and runtime behavior
- [ADR-0001: WinSW over NSSM](docs/adr/0001-winsw-over-nssm.md) - Windows service wrapper choice
- [ADR-0002: Self-update architecture](docs/adr/0002-self-update-architecture.md) - Two-loop self-update mechanism

## Development

```bash
pytest
pytest --cov=src/haniel
ruff check src/ tests/
ruff format src/ tests/
```

## License

MIT
