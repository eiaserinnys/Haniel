# Configuration Reference

This document describes every field in `haniel.yaml`.
The source of truth is the Pydantic models in `src/haniel/config/model.py`.

## Minimal example

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

This is enough to run haniel with self-update only. See [`haniel.yaml.example`](../haniel.yaml.example) for a full multi-service setup.

## Top-level fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `poll_interval` | int | `60` | Seconds between git fetch polls |
| `auto_apply` | bool | `true` | If `false`, detected changes are shown in dashboard but not auto-applied. Manual "Update" from dashboard is still possible |
| `shutdown` | object | — | Global shutdown behavior |
| `backoff` | object | — | Restart backoff and circuit breaker |
| `webhooks` | list | — | Notification endpoints |
| `mcp` | object | — | MCP server settings |
| `dashboard` | object | — | Built-in web dashboard settings |
| `repos` | map | `{}` | Git repositories to track |
| `services` | map | `{}` | Processes to manage |
| `self` | object | — | Self-update mechanism |
| `orchestrator_client` | object | — | Remote orchestrator node connection |
| `install` | object | — | Installation phase (used only by `haniel install`) |

## `shutdown`

Global defaults for graceful shutdown. Per-service overrides are available under `services.*.shutdown`.

```yaml
shutdown:
  timeout: 10
  kill_timeout: 30
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timeout` | int | `10` | Seconds to wait for graceful shutdown |
| `kill_timeout` | int | `30` | Seconds before SIGKILL after timeout |
| `signal` | string | `SIGTERM` | Signal to send for graceful shutdown |
| `method` | string | — | `http` to send HTTP shutdown request instead of signal |
| `endpoint` | string | — | HTTP endpoint for shutdown (when `method: http`) |

## `backoff`

Controls restart behavior when a service crashes.

```yaml
backoff:
  base_delay: 5
  max_delay: 300
  circuit_breaker: 5
  circuit_window: 300
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `base_delay` | int | `5` | Initial delay before restart (seconds) |
| `max_delay` | int | `300` | Maximum delay between restarts (seconds). Delay doubles each time up to this cap |
| `circuit_breaker` | int | `5` | Number of failures before the circuit breaker trips |
| `circuit_window` | int | `300` | Time window for counting failures (seconds) |

When the circuit breaker trips, the service enters `CIRCUIT_OPEN` state and stops restarting.
Use `haniel_enable(service)` MCP tool or the dashboard to re-enable it.

## `webhooks`

List of notification endpoints. haniel sends alerts on deployments, crashes, failures, and self-update events.

```yaml
webhooks:
  - url: https://hooks.slack.com/services/T.../B.../...
    format: slack
  - url: https://discord.com/api/webhooks/...
    format: discord
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | *required* | Webhook URL |
| `format` | string | `json` | Format: `slack` (Block Kit), `discord`, or `json` |

## `mcp`

MCP (Model Context Protocol) server settings. Allows Claude Code to query status and control haniel.

```yaml
mcp:
  enabled: true
  transport: streamable_http
  port: 3200
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether the MCP server is active |
| `transport` | string | `streamable_http` | Transport type: `streamable_http` or `stdio` |
| `port` | int | `3200` | Port for the MCP server |

When the dashboard is enabled, it shares the same port and Starlette server.

## `dashboard`

Built-in web dashboard for browser-based service management.

```yaml
dashboard:
  enabled: true
  port: null    # null = share MCP port
  token: "your-secret-token"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether the dashboard is active |
| `port` | int | — | Dedicated port. If omitted, shares the MCP server port |
| `token` | string | — | Bearer token for API/WebSocket authentication. If omitted, dashboard is accessible without auth (a warning is logged) |

All `/api/*` and `/ws` endpoints require `Authorization: Bearer <token>` when token is set.

## `repos`

Map of git repositories to track. Keys are arbitrary names used to reference repos from services.

```yaml
repos:
  my-app:
    url: git@github.com:org/my-app.git
    branch: main
    path: ./.services/my-app
    release_manifest: deploy/release-manifest.json
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | *required* | Git clone URL (HTTPS or SSH) |
| `branch` | string | `main` | Branch to track |
| `path` | string | *required* | Local path, relative to `haniel.yaml` location |
| `release_manifest` | string | — | Repository-relative path to a `haniel.release.v1` deployment contract |

**Poll behavior**: Every `poll_interval` seconds, haniel runs `git fetch` and compares local HEAD with remote. If they differ, changes are detected.

**Auto-clone**: During `haniel install` or first `haniel run`, missing repos are cloned automatically.

### Migration-aware release manifests

A repository that changes persistent data should own an explicit release contract. The manifest keeps the old processes available through build and migration preflight. Haniel stops the affected local processes before any declared backup. A repository that shares its database with writers on other nodes must prove cluster-wide write fencing in its own preflight before it may declare a destructive restore; stopping local services alone is not a quiescence guarantee.

```json
{
  "schema_version": "haniel.release.v1",
  "release_id": "2026-07-19-task-migration",
  "migration": {
    "destructive": true,
    "preflight": {
      "name": "migration-preflight",
      "command": "node scripts/migrate.mjs preflight"
    },
    "backup": {
      "name": "database-backup",
      "command": "node scripts/migrate.mjs backup"
    },
    "verify_backup": {
      "name": "verify-database-backup",
      "command": "node scripts/migrate.mjs verify-backup"
    },
    "apply": {
      "name": "apply-migrations",
      "command": "node scripts/migrate.mjs apply"
    }
  },
  "post_start_verify": [
    {
      "name": "release-health",
      "command": "node scripts/release-health.mjs"
    }
  ],
  "build_retry": {
    "max_attempts": 4,
    "initial_backoff_seconds": 5,
    "max_backoff_seconds": 20,
    "total_grace_seconds": 60
  },
  "post_start_verify_retry": {
    "max_attempts": 4,
    "initial_backoff_seconds": 5,
    "max_backoff_seconds": 20,
    "total_grace_seconds": 60
  },
  "recovery": {
    "strategy": "roll_forward",
    "command": {
      "name": "migration-recovery",
      "command": "node scripts/migrate.mjs recover"
    },
    "fallback": {
      "name": "restore-database-backup",
      "command": "node scripts/migrate.mjs restore-backup"
    }
  }
}
```

Every command runs without a shell in the repository root. Commands receive `HANIEL_DEPLOY_REPO`, `HANIEL_RELEASE_ID`, `HANIEL_PREVIOUS_HEAD`, and `HANIEL_TARGET_HEAD`. `environment_service` is optional and must only be used when a manifest truly targets one stable Haniel service key; deployment-specific service names should stay out of repository manifests. A destructive migration is rejected unless both backup commands are present. The build phase retries the complete set of affected-service `post_pull` hooks with bounded exponential backoff, so these hooks must be safe to rerun. Post-start verification retries only the failed command; commands that already passed are not repeated. Both retry policies default to four attempts with 5-second initial and 20-second maximum backoff inside one shared 60-second wait budget. A shutdown request interrupts either retry wait. If verification is still failing after services reached readiness, Haniel records `verification_failed` and reports failure while preserving the target HEAD and ready processes. Failures before readiness retain the declared rollback or roll-forward recovery. Roll-forward recovery must declare a `fallback`: when recovery itself still fails, Haniel runs that command and then rolls code and processes back to the previous release. The repository decides whether the fallback preserves the new data layout or restores a verified backup. A database restore is safe only after the repository verifies a cluster-wide write fence; a rollback-compatible migration should use a data-preserving fallback instead.

The conventional path is `deploy/release-manifest.json`. If a fetched remote branch introduces a valid manifest at that path while `haniel.yaml` has no `release_manifest`, Haniel atomically adds the one field before pulling any new code, preserves a checksum-addressed backup of the previous config, and immediately uses the manifest state machine. If Haniel was started without a writable `config_path`, it blocks the pull and keeps the previous checkout.

The transition can also be inspected or applied out of band. `check` is read-only and exits 2 while activation is required. `apply` requires the fresh checksum printed by `check`, so a concurrent config edit cannot be overwritten.

```bash
python -m haniel.config.release_activation check --config /path/to/haniel.yaml --repo soulstream
python -m haniel.config.release_activation apply --config /path/to/haniel.yaml --repo soulstream --expected-sha256 <sha256-from-check>
```

The same state machine is used by an approved pull and by Haniel startup updates. Startup does not rerun legacy `post_pull` or start calls for manifest-owned services; the handover starts and verifies those services exactly once, then the remaining services continue in dependency order. The journal under `.haniel/deployments/` records `build`, `preflight`, `backing_up`, `migrating`, `starting`, `verifying`, `recovering`, and the terminal state. A repeated approval of the same successful target commit and release ID is a no-op. Haniel reports success only after every affected service is ready and every post-start verification succeeds. A recovered deployment still reports failure, preserving the distinction between release success and restored availability.

## `services`

Map of services (processes) to manage. YAML order determines startup order.

```yaml
services:
  mcp-server:
    run: ./venv/Scripts/python.exe -m myapp.mcp --port=3104
    cwd: ./.services/my-app
    repo: my-app
    ready: port:3104
    ready_timeout: 120
    restart_delay: 3
    enabled: true
    reflect: false
    hooks:
      post_pull: ./venv/Scripts/pip.exe install -r requirements.txt
      pre_start: echo "starting..."
      timeout: 900

  bot:
    run: ./venv/Scripts/python.exe -m myapp.bot
    cwd: ./.services/my-app
    repo: my-app
    after: mcp-server
    shutdown:
      signal: SIGTERM
      timeout: 15
    hooks:
      post_pull: ./venv/Scripts/pip.exe install -r requirements.txt
      timeout: 900
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `run` | string | *required* | Command to execute |
| `cwd` | string | — | Working directory. If omitted, uses `haniel.yaml` location |
| `repo` | string | — | Key from `repos`. When this repo has changes, the service is restarted |
| `restart_delay` | int | — | Fixed delay before crash-restart. If omitted, backoff strategy is used |
| `after` | string or list | `[]` | Service(s) to wait for before starting |
| `ready` | string | — | Ready condition (see below) |
| `ready_timeout` | int | `60` | Maximum seconds to wait for the ready condition |
| `shutdown` | object | — | Per-service shutdown override (same fields as global `shutdown`) |
| `enabled` | bool | `true` | If `false`, service is skipped |
| `hooks` | object | — | Lifecycle hooks |
| `reflect` | bool | `false` | Whether this service exposes a `/reflect` endpoint for service introspection |

### `ready` condition syntax

```yaml
ready: port:3104           # Ready when port enters LISTEN state
ready: delay:5             # Ready after 5 seconds
ready: "log:Server started"  # Ready when pattern appears in stdout/stderr
ready: http://localhost:3104/health  # Ready when GET returns 2xx
```

If `ready` is absent and another service depends on this one via `after`, `delay:3` is used as the default.

### `after` ordering

```yaml
# Single dependency
after: mcp-server

# Multiple dependencies (all must be ready)
after:
  - mcp-server
  - database
```

Services without `after` start immediately in YAML order.

### `hooks`

```yaml
hooks:
  post_pull: pip install -r requirements.txt   # After git pull
  pre_start: ./scripts/check-prerequisites.sh  # Before service start
  timeout: 900                                 # Per-hook timeout in seconds
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `post_pull` | string | — | Command to run after `git pull` (dependency installs, builds, etc.) |
| `pre_start` | string | — | Command to run before service start |
| `timeout` | int | `900` | Maximum seconds allowed for each lifecycle hook |

After a repository changes, `post_pull` runs only for affected services whose
`repo` points to that repository. Dependency-only services still participate in
stop/start and readiness ordering, but their unchanged repositories are not rebuilt.

Non-zero exit codes trigger a webhook notification. Service startup continues regardless.

Do not place destructive schema migrations in `pre_start`: it is a per-service lifecycle hook and is not a release transaction. Use the repository `release_manifest` so migration, recovery, readiness, and final verification share one auditable control path.

## `self`

Self-update mechanism. haniel can poll its own repo and update itself.

```yaml
self:
  repo: haniel
  auto_update: false
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `repo` | string | *required* | Key from `repos` identifying haniel's own repo |
| `auto_update` | bool | `false` | If `true`, update immediately. If `false`, enter pending state and wait for approval |

When changes are detected:
- `auto_update: true` — Shuts down all services and exits with code 10
- `auto_update: false` — Sends webhook notification, waits for `haniel_approve_update()` MCP tool call or dashboard approval

The wrapper script (`haniel-runner.ps1` on Windows, `haniel-runner.sh` on Linux) interprets exit code 10 as "update me," runs `git pull` + `pip install`, and relaunches. See [ADR-0002](adr/0002-self-update-architecture.md).

## `orchestrator_client`

Connects this node to a remote Haniel orchestrator.

```yaml
orchestrator_client:
  enabled: true
  url: wss://haniel.example.com/ws/node
  token: secret
  node_id: eias-linegames-wsl
  hostname: eias-linegames-wsl
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether to connect to the orchestrator |
| `url` | string | *required* | Orchestrator WebSocket URL |
| `token` | string | *required* | Authentication token shared with the orchestrator |
| `node_id` | string | *required* | Stable node identifier used in deploy IDs |
| `hostname` | string | OS hostname | Hostname reported to the orchestrator. Set this when multiple Haniel nodes share the same physical machine |
| `reconnect_base` | float | `1.0` | Initial reconnect delay in seconds |
| `reconnect_max` | float | `60.0` | Maximum reconnect delay in seconds |
| `ping_interval` | float | `10.0` | WebSocket ping interval in seconds |
| `ping_timeout` | float | `10.0` | WebSocket ping timeout in seconds |

## `install`

Used only by `haniel install`. Defines the execution environment to be set up on a bare machine.

Installation has four phases:
1. **Phase 0 (Bootstrap)**: Verify Claude Code is installed
2. **Phase 1 (Mechanical)**: Directories, git clone, venv, npm/pnpm — haniel performs directly
3. **Phase 2 (Interactive)**: Secret collection, config choices — delegated to Claude Code
4. **Phase 3 (Finalize)**: Config file generation, WinSW service registration

### `install.requirements`

```yaml
install:
  requirements:
    python: ">=3.11"
    node: ">=18"
    winsw: true
    claude-code: true
```

System prerequisites. `claude-code` is verified in Phase 0 (required). Others are verified in Phase 1; failures are recorded and Claude Code guides the user through resolution in Phase 2.

### `install.directories`

```yaml
install:
  directories:
    - ./runtime
    - ./runtime/logs
    - ./runtime/data
```

Directories to create. Missing ones are created; existing ones are skipped. Processed in Phase 1.

### `install.environments`

```yaml
install:
  environments:
    main-venv:
      type: python-venv
      path: ./runtime/venv
      requirements:
        - ./requirements.txt

    frontend:
      type: pnpm
      path: ./.services/dashboard
      build: pnpm run build
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | string | *required* | `python-venv`, `npm`, or `pnpm` |
| `path` | string | *required* | Path to environment directory |
| `requirements` | list | — | Requirements files (for `python-venv` type) |
| `build` | string | — | Build command to run after install (e.g. `pnpm run build`) |

Processed in Phase 1. On failure, troubleshot by Claude Code in Phase 2.

### `install.configs`

Two modes for config file creation:

**Static** — auto-generated in Phase 1:
```yaml
install:
  configs:
    app-mcp:
      path: ./.services/my-app/.mcp.json
      content: |
        { "mcpServers": { "haniel": { "url": "http://localhost:3200/mcp/http" } } }
```

**Interactive** — collected by Claude Code in Phase 2:
```yaml
install:
  configs:
    app-env:
      path: ./.services/my-app/.env
      keys:
        - key: SLACK_BOT_TOKEN
          prompt: "Slack Bot Token (xoxb-...)"
          guide: "https://api.slack.com/apps → OAuth & Permissions → Bot User OAuth Token"
          validate: "curl -s -H 'Authorization: Bearer {value}' https://slack.com/api/auth.test | jq -e '.ok'"
        - key: DEBUG
          default: "false"
          description: "Enable debug logging"
```

**Key fields:**

| Field | Type | Description |
|-------|------|-------------|
| `key` | string | Environment variable or config key name |
| `prompt` | string | Description shown when asking the user |
| `guide` | string | URL or instructions for obtaining the value |
| `validate` | string | Validation command. `{value}` is substituted. Non-zero exit = invalid |
| `default` | string | Default value. `{root}` is substituted with haniel.yaml's directory |
| `description` | string | Human-readable description for AI-assisted setup |

If the config file already exists, existing values are preserved; only new keys appear as missing.

### `install.service`

```yaml
install:
  service:
    name: haniel
    display: "Haniel Service Runner"
    working_directory: "{root}"
    service_account:
      username: ".\\myuser"
      password: "secret"
      allow_service_logon: true
    environment:
      PYTHONUTF8: "1"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | *required* | Windows service name |
| `display` | string | — | Display name in Services panel |
| `working_directory` | string | `{root}` | Working directory. `{root}` = haniel.yaml location |
| `service_account` | object | — | Run under a specific user account (default: LocalSystem) |
| `environment` | map | — | Environment variables injected into the service |

**`service_account` fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `username` | string | *required* | Account name (e.g. `.\\username` for local accounts) |
| `password` | string | — | Account password |
| `allow_service_logon` | bool | `true` | Grant "Log on as a service" right to the account |

Registered as a Windows service via WinSW in Phase 3.
