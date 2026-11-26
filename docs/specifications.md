# haniel

The Angel of Blood and Pact. A configuration-based, intentionally indifferent service runner.

## Core concept

haniel doesn't care what it runs.
It checks git repos, pulls changes when detected, and starts processes using the commands specified in config.
Whether it's a Slack bot, an MCP server, or a web dashboard, everything is just a "process" to haniel.

What haniel knows:
- What's written in the config file (YAML)
- git fetch/pull results
- Whether a process is alive

What haniel doesn't care about:
- `.env` contents and semantics — **it never reads, loads, or injects them into child processes**
- `.mcp.json` contents — it only generates them during install, never touches them at runtime
- What processes actually do
- Business dependencies between processes
- Port number semantics
- The rest of the host system configuration

Processes are responsible for their own environment variables, config files, and dependencies.
haniel just executes commands.

## Four modes

### `haniel install` — Installation mode

Builds the entire execution environment on a bare machine.
haniel handles what it can mechanically (directories, git clone, venv, npm/pnpm),
and **delegates to Claude Code** for anything requiring human conversation (secret collection, configuration choices).

Installation flow:
1. haniel starts itself as an MCP server
2. Processes mechanical steps (directories, clone, venv) first
3. Launches Claude Code for interactive steps if needed
4. Claude Code passes configuration values via haniel MCP tools
5. haniel completes remaining steps (file generation, service registration)

After installation, services can be started with `haniel run`.

**Resume support**: If installation is interrupted, re-running `haniel install` asks "start fresh / resume" in a terminal. When it cannot ask (`--skip-interactive`, or no terminal) it stops until you pass `--state-policy resume` or `--state-policy start-fresh`. `--resume` is the deprecated name of `--state-policy resume`.

**Options**: `--dry-run` prints the plan. `--skip-interactive` skips the Claude Code step, so Claude Code does not have to be installed; a config key without a `default` then stops finalization with "Not all required configs are filled".

### `haniel run` — Runtime mode

The main operational mode. Starts all services, enters the poll loop, and runs until shutdown.

`haniel run [CONFIG]` uses `./haniel.yaml` when `CONFIG` is omitted and exits with code 2 if that file does not exist. A second `haniel run` for the same config prints one line (config path and the running pid) and exits with code 1. Exit code 10 means an approved self-update and 11 a restart request; the service wrapper reads both. `--dry-run` prints the startup order without starting anything.

At startup:
- Loads `haniel.yaml` (all sections: `repos`, `services`, `mcp`, `dashboard`, etc.) and logs a WARNING for each unknown key or bad value it finds; it does not refuse to start
- Starts the MCP server (Streamable HTTP on port 3200 by default) only when an `mcp:` block with `enabled: true` is present. If the port is in use an ERROR goes to `haniel.log` and the services keep running
- Mounts the web dashboard if a `dashboard:` block with `enabled: true` is present (shares the MCP port)
- Clones every repo whose `path` does not exist (not the `self` repo); a failed clone logs an ERROR with the repo name and URL
- Fetches every managed repository in parallel regardless of `auto_apply`
- Starts services of unchanged repositories first, then builds each updated
  repository on its own lane and starts only that lane's services
- Enters the poll loop

At runtime:
- git poll → change detection → apply (if `auto_apply` is true) or display in dashboard (if false)
- Health monitoring with exponential backoff and circuit breaker
- Config reload via MCP tool or dashboard API (no process restart needed)
- Config CRUD via dashboard API (add/update/remove services and repos at runtime)

Does not touch .env, .mcp.json, venv, etc.

### `haniel status` — Status query

`haniel status [CONFIG]` first reads the owner record that `haniel run` keeps for this config and prints `Running (pid N)` or `Not running`. For a running instance it then asks `GET http://<mcp.host>:<mcp.port>/api/status` and prints the real service state, repo state, last poll time, and poll count. That needs `mcp.enabled` and `dashboard.enabled` to be true in the config, and `dashboard.token` is sent as a Bearer token when set. When it cannot ask (a block is missing, the connection fails, the token is rejected), it prints the reason and lists the services and repos from the config only, marked "not runtime state". `--json` prints the same as JSON with `source: runtime` or `source: config`.
The same information is available to Claude Code through MCP when the server is running.

### `haniel validate` — Configuration validation

Validates haniel.yaml:
- YAML syntax
- Required field presence
- Unknown keys at any depth, with the closest known name as a hint
- Unusable values: `ready` format, `shutdown.signal`, negative `restart_delay` and `backoff` delays, empty repo `url`, empty `orchestrator_client.url` or `token`
- Circular dependency detection (`after` field)
- Port conflict detection (`ready: port:*` duplicates)
- Duplicate repo path detection

Errors give exit code 1. A config with no services only gets a warning. `--no-plain-secrets` additionally rejects plain-text secrets. MCP and REST config saves apply the same unknown-key and value checks to the entry being written and reject it; `haniel run` logs them as warnings only.

Used for pre-run validation.

### What the CLI does not do

`haniel` has no command to start, stop, restart, or pull a service. Those are MCP tools (`haniel_restart`, `haniel_stop`, `haniel_start`, `haniel_pull`, ...), dashboard buttons, and REST calls. The `handover` and `lifecycle` commands are internal tools used by the self-update wrapper and the running instance, not part of the user-facing CLI, and are hidden from `haniel --help`.

## Configuration file (`haniel.yaml`)

> **Full field reference**: See [configuration.md](configuration.md) for every field, type, and default value.

This section shows the overall structure and annotated examples.

### Full structure

```yaml
auto_apply: true               # If false, changes shown in dashboard but not auto-applied

poll_interval: 60

shutdown:
  timeout: 10
  kill_timeout: 30

backoff:
  base_delay: 5
  max_delay: 300
  circuit_breaker: 5
  circuit_window: 300

webhooks:
  - url: https://hooks.slack.com/services/T.../B.../...
    format: slack
  - url: https://discord.com/api/webhooks/...
    format: discord

mcp:
  enabled: true
  transport: streamable_http   # currently ignored; always Streamable HTTP
  port: 3200

dashboard:
  enabled: true
  port: null                   # currently ignored; shares the MCP port
  token: "your-secret-token"   # Bearer token for API/WebSocket auth

repos:
  my-app:
    url: git@github.com:org/my-app.git
    branch: main
    path: ./.services/my-app
    release_manifest: deploy/release-manifest.json

  my-lib:
    url: git@github.com:org/my-lib.git
    branch: main
    path: ./.services/my-lib

services:
  mcp-server:
    run: ./runtime/venv/Scripts/python.exe -m myapp.mcp --port=3104
    cwd: ./.services/my-app
    repo: my-app
    ready: port:3104
    ready_timeout: 120
    restart_delay: 3
    hooks:
      post_pull: ./runtime/venv/Scripts/pip.exe install -r requirements.txt
      pre_start: echo "checking prerequisites..."
      timeout: 900

  bot:
    run: ./runtime/venv/Scripts/python.exe -m myapp.bot
    cwd: ./.services/my-app
    repo: my-app
    after: mcp-server
    shutdown:
      signal: SIGTERM
      timeout: 15
    hooks:
      post_pull: ./runtime/venv/Scripts/pip.exe install -r requirements.txt
      timeout: 900

self:
  repo: haniel
  auto_update: false

install:
  requirements:
    python: ">=3.11"
    node: ">=18"
    winsw: true
    claude-code: true

  directories:
    - ./runtime
    - ./runtime/logs

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

  configs:
    app-env:
      path: ./.services/my-app/.env
      keys:
        - key: SLACK_BOT_TOKEN
          prompt: "Slack Bot Token (xoxb-...)"
          guide: "https://api.slack.com/apps -> OAuth & Permissions"
        - key: DEBUG
          default: "false"

    app-mcp:
      path: ./.services/my-app/.mcp.json
      content: |
        { "mcpServers": { "haniel": { "url": "http://localhost:3200/mcp/" } } }

  service:
    name: haniel
    display: "Haniel Service Runner"
    working_directory: "{root}"
    service_account:
      username: ".\\myuser"
      allow_service_logon: true
    environment:
      PYTHONUTF8: "1"
```

## `install` section details

### Core design: Claude Code delegation

The installation process has two types of work.

**Mechanical work** — haniel performs directly:
- System requirements verification
- Directory creation
- git clone
- venv creation, pip install
- npm/pnpm install + build commands
- Static file generation (.mcp.json, etc.)
- WinSW service registration

**Interactive work** — delegated to Claude Code:
- Secret collection (Slack tokens, API keys, etc.)
- Issuing instructions ("Get your Bot Token from this URL")
- Configuration choices ("Choose the channel for notifications")
- Troubleshooting ("git clone failed. Is your SSH key configured?")
- Value validation (when `validate` field is present)

haniel doesn't need to build a CLI UI for installation.
Human conversation is handled by Claude Code;
haniel just receives values via MCP tools and writes them to files.

### Full installation flow

```
haniel install haniel.yaml
  |
  +- Check previous install state
  |     install.state file exists?
  |     +- Exists and incomplete -> "resume / start fresh" prompt
  |     +- Missing or complete -> start fresh
  |
  +- Phase 0: Bootstrap
  |     Will Phase 2 run? (no --skip-interactive and a key without value/default)
  |     +- No: Claude Code is not needed, continue
  |     +- Yes: is Claude Code installed?
  |           +- No: print installation instructions and abort
  |           +- Yes: continue
  |
  +- Phase 1: Mechanical installation (haniel alone)
  |     Load haniel.yaml
  |     Verify requirements (python, node, winsw)
  |       Unmet items -> record for Phase 2, Claude Code will guide user
  |     Create directories
  |     Clone repos (where possible)
  |     Set up environments (venv, npm/pnpm, build)
  |     Generate static configs (content-based)
  |     |
  |     Compile results as JSON:
  |       - Completed steps
  |       - Failed steps with error messages
  |       - Configs requiring interactive input
  |     Save state to install.state
  |
  +- Phase 2: Interactive installation (Claude Code delegation)
  |     haniel starts an install-only MCP server on port mcp.port + 1 (default 3201)
  |     Launch Claude Code session via claude-agent-sdk:
  |       Passes install state + MCP config
  |     |
  |     Claude Code converses with user:
  |       - Queries install state via haniel_install_status()
  |       - Queries config details via haniel_get_config(name)
  |       - Guides resolution of failed steps
  |       - Collects secrets/config values
  |     |
  |     When Claude Code session ends, haniel proceeds to finalize
  |
  +- Phase 3: Finalization (haniel alone)
  |     Run every key's validate command on its final value; any failure
  |       stops here (exit code 1) before a file is written
  |     Generate config files from collected values
  |     Register the service (WinSW / systemd / LaunchAgent)
  |     Stop MCP server (exit install mode)
  |     Mark install.state as complete
  |
  +- Done
      "Installation complete." followed by the start command for this OS:
        Windows `sc start <name>`, Linux `sudo systemctl start <name>`,
        macOS `launchctl bootstrap gui/<uid> <plist>`
```

### Install-only MCP tools

Two read-only MCP tools are exposed during install mode on port `mcp.port + 1`:

```
haniel_install_status()
  -> Returns current installation progress
    {
      "phase": "interactive",
      "completed": ["directories", "repos", "environments"],
      "failed": [{"step": "requirements", "detail": "winsw not found"}],
      "pending_configs": [
        {
          "name": "app-env",
          "path": "./.services/my-app/.env",
          "missing_keys": ["SLACK_BOT_TOKEN"],
          "filled_keys": ["DEBUG"]
        }
      ]
    }

haniel_get_config(config_name)
  -> Returns current state of a specific config (filled keys, missing keys, descriptions)
```

Config value collection and finalization are handled by haniel directly, not via MCP tools.

### `requirements` — System requirements

```yaml
install:
  requirements:
    python: ">=3.11"
    node: ">=18"
    winsw: true
    claude-code: true     # Verified in Phase 0 (required prerequisite)
```

claude-code is verified separately in Phase 0, and only when the interactive phase will run (otherwise it is not needed).
Others are verified in Phase 1, but failures don't abort — they're recorded.
Claude Code guides the user through resolution in Phase 2.

### `directories` — Directory creation

```yaml
install:
  directories:
    - ./runtime
    - ./runtime/logs
```

Creates missing directories. Skips existing ones.
Processed mechanically in Phase 1.

### `environments` — Execution environments

```yaml
install:
  environments:
    {name}:
      type: python-venv | npm | pnpm
      path: {directory}
      requirements:              # For python-venv only
        - {requirements.txt path}
      build: {command}           # Optional post-install build step
```

Processed mechanically in Phase 1.
On failure, recorded and troubleshot by Claude Code in Phase 2.

### `configs` — Configuration file definitions

Two modes:

**Static (`content`)** — Auto-generated in Phase 1:
```yaml
configs:
  app-mcp:
    path: ./.services/my-app/.mcp.json
    content: |
      { "mcpServers": { ... } }
```

**Interactive (`keys`)** — Collected by Claude Code in Phase 2:
```yaml
configs:
  app-env:
    path: ./.services/my-app/.env
    keys:
      - key: SLACK_BOT_TOKEN
        prompt: "Slack Bot Token (xoxb-...)"
        guide: "https://api.slack.com/apps -> OAuth & Permissions -> Bot User OAuth Token"
        validate: "curl -s -H 'Authorization: Bearer {value}' https://slack.com/api/auth.test | jq -e '.ok'"
        description: "OAuth token for the Slack bot to send/receive messages"
      - key: LOG_PATH
        default: "{root}/runtime/logs"
```

- `prompt`: Description for Claude Code when asking the user
- `guide`: URL or instructions for obtaining/verifying the value. Claude Code presents this to the user
- `validate`: Validation command, run by haniel (not by Claude Code) with `{value}` replaced by the value as written, no quoting or escaping, so a value containing `'`, `"`, `$`, or a backtick can break the command. It runs through the platform shell in the folder of `haniel.yaml` with a 60-second limit; a non-zero exit means invalid. It runs when the value is set in the interactive phase (a rejected value is sent back to Claude Code to ask again) and again, for every final value including defaults and values read from an existing file, just before the files are written. A failure there stops the install with exit code 1, writes no file, and records a `config-generation` failure; `--state-policy resume` continues after the value is fixed. The error shows the key, the command template, the exit code, and the last 5 output lines.
- `default`: Default value. `{root}` is substituted with the absolute path of haniel.yaml's location
- `description`: Human-readable description for AI-assisted setup

If the file already exists, existing values are preserved; only new keys appear as missing_keys.

### `service` — Service registration

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

- `service_account`: Run the Windows service under a specific user instead of LocalSystem. `allow_service_logon` grants the "Log on as a service" right automatically.
- Registered in Phase 3 (finalize): a Windows service via WinSW, a systemd unit on Linux (`/etc/systemd/system/<name>.service`, `User=` set to the user who ran `sudo` or the installing user; with a `self:` block `ExecStart` runs `haniel-runner.sh` with a generated `haniel-runner.conf`, otherwise `haniel run haniel.yaml`), or a user LaunchAgent on macOS. The installer enables the Linux unit but does not start it.

## `repos` section

Defines repositories. Auto-cloned during install, auto-polled at runtime.

```yaml
repos:
  {name}:
    url: {git clone URL}           # Required. Clone and fetch target
    branch: {branch name}          # Default: main. Branch to track
    path: {local path}             # Required. Local path (relative to haniel.yaml)
    hooks:                         # Optional. Legacy repository hook
      post_pull: {command}         # Once per updated repo, before service hooks
      timeout: 900                 # Per-hook timeout in seconds
    release_manifest: {path}       # Optional. Repository-relative haniel.release.v1 contract
```

Legacy startup updates and approved pulls run the repository hook in the repo
directory, including for repos without services. On startup rollback it runs
again before service hooks. Manifest deployments warn and skip this hook.
If a merge checkout has uncommitted tracked changes after a startup hook fails,
Haniel leaves it untouched and blocks that repository's services.

Repositories with a release manifest use the migration-aware deployment state machine during both approved pulls and startup updates. Build and migration preflight complete while old processes remain available during a live handover. Haniel then stops affected local processes, runs any declared backup gate, performs migration and process handover, waits for every affected service readiness condition, executes the manifest's post-start verification commands, and only then reports success. Stopping local services does not prove that a shared database is quiescent; a destructive restore fallback requires repository-owned cluster-wide write-fence evidence. Failures enter the manifest's declared automatic recovery path and remain a failed deployment even when availability is restored.

**Auto-clone flow (install and run):** `haniel run` clones a missing repo on startup too (except the `self` repo).
1. Check if `path` exists
2. If missing: `git clone --branch {branch} {url} {path}`
3. If present: verify `.git` is valid and remote URL matches
4. On mismatch: error (no automatic correction)

**Runtime poll:**
1. `git fetch origin {branch}`
2. If `local_head != remote_head`: changes detected
3. `get_pending_changes()` retrieves commit list and diff stats for display in dashboard

## `services` section

Defines services. **YAML order determines startup order.**

```yaml
services:
  {name}:
    run: {command}                 # Required. Execution command
    cwd: {directory}               # Optional. Working directory (default: haniel.yaml location)
    repo: {repo name}              # Optional. Restart when this repo changes
    restart_delay: {seconds}       # Optional. Delay before crash restart (default: backoff)
    after: {service name}          # Optional. Start only once this service is running and ready
    ready: {condition}             # Optional. "Ready" judgment condition
    ready_timeout: {seconds}       # Optional. Ready deadline (default: 60)
    shutdown:                      # Optional. Shutdown method
      signal: SIGTERM              #   Default: SIGTERM
      timeout: 15                  #   Graceful wait (default: global shutdown.timeout)
      method: http                 #   Optional. If http, send HTTP shutdown request
      endpoint: /shutdown          #   Used when method: http
    enabled: true                  # Optional. If false, skip (default: true)
    hooks:                         # Optional. Lifecycle hooks
      post_pull: {command}         #   Run after git pull
      pre_start: {command}         #   Run before service start
      timeout: 900                 #   Per-hook timeout in seconds (default: 900)
```

haniel just executes the `run` command.
Processes load their own .env, config files, and environment variables.
haniel injects nothing into process environments.

### `ready` condition syntax

```yaml
ready: port:{port}                # Ready when the port enters LISTEN state
ready: delay:{seconds}            # Ready after specified time elapses
ready: log:{pattern}              # Ready when pattern appears in stdout/stderr
ready: http:{url}                 # Ready when GET request returns 2xx
```

If `ready` is absent, the service counts as ready once its process is running; there is no default delay.

### `after` startup ordering syntax

```yaml
# Single dependency
after: mcp-server

# Multiple dependencies (all must be ready)
after:
  - mcp-server
  - database
```

A service with `after` starts only when each dependency is running and, if it has `ready`, that condition holds; haniel waits up to the dependency's `ready_timeout` (default 60 seconds). If a dependency does not get ready, the service is skipped (`<name> skipped: dependency <dep> failed`). It is retried only when the dependency is itself waiting for a restart, so with an open circuit it stays down until started by hand. Services that do not depend on each other do not wait for each other.

Services without `after` start immediately in YAML order.

### `hooks` lifecycle hooks

```yaml
hooks:
  post_pull: {command}            # Run after git pull (builds, dependency installs, etc.)
  pre_start: {command}            # Run before service start
  timeout: 900                    # Maximum seconds for each hook
```

haniel doesn't care what hooks do.
Hook failures are reported; startup rollback and completed-build records
determine whether services can start.
For repository deploys, dependency-only services are restarted in order but do not
run `post_pull` unless their own `repo` is the repository being changed.

## Service health states

Each service has one of seven states:

```
STOPPED ──start──> STARTING ──ready condition met──> READY
                       |                                |
                       +──no ready condition──> RUNNING |
                                                        |
CIRCUIT_OPEN <──threshold── CRASHED <──process dies──---+
     |                         |
     +──reset──> STOPPED       +──backoff──> STARTING (retry)
```

| State | Description |
|-------|-------------|
| `STOPPED` | Initial state. Also set after clean shutdown or circuit breaker reset |
| `STARTING` | Process has been spawned, waiting for ready condition |
| `READY` | Ready condition met. Resets failure count and backoff |
| `RUNNING` | Running without a ready condition. Resets failure count and backoff only after the process has stayed up for more than 10 seconds, so a service that dies right after starting accumulates failures and opens the circuit |
| `STOPPING` | Graceful shutdown in progress |
| `CRASHED` | Process exited unexpectedly. Increments failure count, calculates backoff |
| `CIRCUIT_OPEN` | Too many failures within the circuit window. Service will not auto-restart |

**Backoff formula**: `base_delay * 2^(failures - 1)`, capped at `max_delay`.

**Circuit breaker**: Trips when `>=circuit_breaker` failures occur within `circuit_window` seconds. `haniel_reset_circuit(service)` resets only the breaker. `haniel_enable(service)` is the pair of `haniel_disable`: it sets `enabled: true`, resets the breaker, and starts the service (the dashboard and REST `enable` do the same).

## Self-update

haniel can update its own code using a two-loop architecture.
See [ADR-0002](adr/0002-self-update-architecture.md) for full details.

```yaml
self:
  repo: haniel          # key from repos section
  auto_update: false    # default: false (approval required)
```

When changes are detected in the self-repo:
- `auto_update: true` — Immediately shuts down services and exits with code 10
- `auto_update: false` — Sends webhook, enters pending state, and waits for approval. The global `auto_apply` does not change this: a self-update waits for approval by default

Approve a pending self-update from the orchestrator's Pending screen (web hub or iOS app) when the node is connected to one, with `haniel_update(service="haniel")` or `haniel_pull(repo=<self repo>)` over MCP, or with "Approve" in the local dashboard (`POST /api/self-update/approve`).

**Approving restarts the Haniel process, so every service on this node stops for a moment.** Services that the active release manifest declares `overlap` or `adopt` are kept running and handed over to the next Haniel. `haniel_restart(service="haniel")` restarts Haniel without an update (exit code 11) with the same effect on services.

The wrapper script (`haniel-runner.ps1` on Windows, `haniel-runner.sh` on Linux and macOS) interprets exit code 10 as a signal to update and restart. On Linux the systemd unit runs the wrapper only when the config has a `self:` block.

## Log handling

haniel captures each service's stdout/stderr and writes them to per-service log files.

```
{folder of haniel.yaml}/logs/
+-- haniel.log          # haniel's own messages
+-- mcp-server.log
+-- bot.log
+-- ...
```

The logs always go to the `logs/` folder next to `haniel.yaml`, not to the service's `cwd` or repository.

- Services may create their own log files separately
- haniel only captures stdout/stderr
- The `ready: log:{pattern}` feature uses this captured output
- In-memory buffer holds the last 1000 lines per service for fast API queries
- Log rotation is not yet built-in; use external tools (e.g. `logrotate`) if needed

## Runtime behavior cycle

### Startup

```
haniel run
  +- Load haniel.yaml
  +- Start MCP server (Streamable HTTP on mcp.port)
  +- Mount dashboard (if dashboard.enabled)
  +- Check repos:
  |    Path exists? -> OK
  |    Path missing? -> git clone (recover from missed install)
  |    Clone failed? -> ERROR log with repo name and URL; the repo's services then fail to start (missing working directory)
  +- Fetch every managed repo in parallel (regardless of auto_apply)
  |    Pull changed repos, activate manifests, and stage config serially per repo
  +- Start services:
  |    Services whose repo was not updated (or that have no repo) start first,
  |      in YAML order, including pre_start / after / ready handling
  |    Each updated repository forms a lane with its services and manifest
  |      deployment targets (plus their after dependents):
  |        run that repo's legacy post_pull, then start only that lane's services
  |    Lanes run concurrently; a slow build in one lane does not hold the others
  +- Enter poll loop
```

### Poll loop

```
A poll starts poll_interval seconds after the previous poll started (a poll that
runs longer than the interval is followed by the next at once). A crashed service's
restart runs when its delay is up, between polls, without a fetch.

  [Phase 1: Change detection]
  For each repo:
    git fetch origin {branch}
    Compare local_head vs remote_head
    Changes detected? -> Add to change list, collect pending changes (commits, diff stats)

  [Phase 2: Apply changes]
  If repos have changes:
    auto_apply == false?
      -> Log detection, broadcast to dashboard via WebSocket
      -> Wait for manual "Update" from dashboard or haniel_pull() MCP call
      -> Skip Phase 2 steps below

    auto_apply == true? (default)
      Compute list of dependent services
      a. Webhook: "Changes detected - {repo}: {commits}"
      b. Graceful shutdown of dependent services in reverse order
         SIGTERM -> timeout wait -> SIGKILL
         If shutdown takes too long, webhook alert
      c. Webhook: "Applying changes"
      d. git pull
      e. Run post_pull hook (if any)
      f. Run pre_start hook (if any)
      g. Restart dependent services in forward order
      h. Webhook: "Startup complete"

  [Phase 3: Health check]
  For each service:
    Is the process alive?

    Alive -> pass
    Dead ->
      Check circuit breaker:
        >= circuit_breaker failures within circuit_window -> CIRCUIT_OPEN + webhook
        Otherwise -> backoff then restart + webhook
```

### Graceful shutdown

On Windows, haniel uses CTRL_BREAK_EVENT (via `GenerateConsoleCtrlEvent`) as the graceful shutdown signal, since Unix SIGTERM is not available. Processes are created with `CREATE_NEW_PROCESS_GROUP` to enable this. Each process is assigned to a Windows Job Object for reliable child process cleanup.

```
Service shutdown request:
  shutdown.method == http?
    -> Send HTTP request to shutdown.endpoint
    -> Wait for response
  Otherwise:
    -> Send CTRL_BREAK_EVENT (Windows) or SIGTERM (Unix)

  Wait for shutdown.timeout
    +- Exited within timeout -> done
    +- Timeout exceeded ->
         Webhook: "Graceful shutdown failed, force killing"
         TerminateJobObject (kills all child processes) or SIGKILL
         Wait for kill_timeout
         +- Exited -> done
         +- Still alive -> Webhook: "Force kill failed, manual intervention required"
```

## MCP server

haniel runs an MCP server using Streamable HTTP transport, allowing Claude Code to query status and control services.

The MCP server and dashboard share the same Starlette application and port.

### Resources (read)

```
haniel://status                    -> All service statuses
haniel://status/{service}          -> Specific service status
haniel://repos                     -> Repo statuses (HEAD, last fetch, pending changes)
haniel://logs/{service}?lines=50   -> Recent logs (max 10000 lines)
```

### Tools (write)

```
haniel_restart(service)            -> Restart a specific service
haniel_stop(service)               -> Stop a specific service
haniel_start(service)              -> Start a specific service
haniel_pull(repo)                  -> Immediately pull a repo + restart dependent services
haniel_update(service)             -> Pull the service's repo; service="haniel" approves a self-update
haniel_enable(service)             -> Undo haniel_disable: enabled: true, reset circuit, start
haniel_reset_circuit(service)      -> Reset only the circuit breaker
haniel_disable(service)            -> Set enabled: false and stop the service
haniel_reload()                    -> Reload haniel.yaml (keep processes, update config only)
```

The tools that change a config (`haniel_create_service_config`, `haniel_update_service_config`, the repo equivalents, `haniel_register_service`) take different argument names (`name` and `config`, or `service`/`repo` and `config`; see each tool's `inputSchema`) and reject an entry with unknown keys or bad values.

**Results.** A call that names an unknown service or repo, or whose config change is rejected, returns an MCP error result (`isError: true`) with the reason. A call that did nothing says so: `haniel_pull` and `haniel_update` answer `<name>: pulled; affected services restarted`, `<name>: no changes to pull`, `<name>: already pulling; this request was ignored`, or `<name>: self-update requested`. `haniel_read_logs` on a service that is not in the config is an error.

**Restarting Haniel itself.** `haniel_restart(service="haniel")`, `haniel_update(service="haniel")`, and `haniel_pull` on haniel's own repo restart the Haniel process, and every service on this node stops for a moment (services declared `overlap` or `adopt` excluded). `haniel_stop(service="haniel")` is refused.

### Claude Code connection

```json
{
  "mcpServers": {
    "haniel": {
      "type": "http",
      "url": "http://localhost:3200/mcp/"
    }
  }
}
```

## Web dashboard

haniel includes a React-based web dashboard for browser-based service management.

### Architecture

The dashboard frontend (React 19 + Vite + TypeScript) is pre-built and served as static files by the haniel backend. No separate Node.js server is needed at runtime.

```
Browser
  +-- REST API (fetch)     -> /api/*       -> dashboard/api.py, config_api.py
  +-- WebSocket            -> /ws          -> dashboard/ws.py (events)
  +-- WebSocket            -> /ws/chat     -> dashboard/chat_ws.py (Claude Code)
  +-- Static files         -> /*           -> dashboard/static.py (React SPA)
```

All `/api/*` and `/ws` requests require Bearer token authentication when `dashboard.token` is set.

### REST API

**Service management:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Overall haniel status |
| GET | `/api/services` | All services with state |
| POST | `/api/services/{name}/start` | Start a service |
| POST | `/api/services/{name}/stop` | Stop a service |
| POST | `/api/services/{name}/restart` | Restart a service |
| POST | `/api/services/{name}/enable` | Undo disable: `enabled: true`, reset circuit breaker, start |
| POST | `/api/services/{name}/disable` | Set `enabled: false` and stop |
| GET | `/api/services/{name}/logs` | Service log output (?lines=N, max 1000) |
| GET | `/api/repos` | Repository statuses |
| POST | `/api/repos/{name}/pull` | Pull + restart dependent services; the response says what happened (pulled, no changes, already pulling, self-update requested) |
| POST | `/api/self-update/approve` | Approve pending self-update; the Haniel process restarts and the node's services stop for a moment |
| POST | `/api/self/restart` | Restart the Haniel process (exit code 11); the node's services stop for a moment |
| POST | `/api/reload` | Reload haniel.yaml |

**Config CRUD** (runtime YAML modification):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config` | Full YAML config |
| GET | `/api/config/services` | All service configs |
| GET | `/api/config/repos` | All repo configs |
| PUT | `/api/config/services/{name}` | Update service config |
| POST | `/api/config/services` | Add new service (`{name, config}`) |
| DELETE | `/api/config/services/{name}` | Remove service (dependency check) |
| PUT | `/api/config/repos/{name}` | Update repo config |
| POST | `/api/config/repos` | Add new repo (`{name, config}`) |
| DELETE | `/api/config/repos/{name}` | Remove repo (reference check) |

Config mutations follow an atomic pattern: acquire write lock → read current YAML → modify → validate → backup → write → reload config. The write serializes the whole loaded configuration: comments and keys the loader does not know are lost and defaults are written out; the previous file is kept as `haniel.yaml.bak`. The service or repo being saved is rejected if it has unknown keys or bad values.

### WebSocket events

**`/ws`** — Real-time service/repo event stream:

| Event type | Payload | Trigger |
|------------|---------|---------|
| `init` | Full status snapshot | Client connects |
| `state_change` | service, old, new, timestamp | Service state transition |
| `repo_change` | repo, pending_changes, timestamp | Repository change detected |
| `self_update_pending` | repo, timestamp | Self-update waiting for approval |
| `reload_complete` | timestamp | Config reload finished |

**`/ws/chat`** — Claude Code chat session:

Client → Server:
- `{"type": "send_message", "session_id": "<uuid|null>", "text": "..."}` — Send message (null session_id = use last session)
- `{"type": "new_session"}` — Create new session
- `{"type": "list_sessions"}` — List all sessions

Server → Client:
- `{"type": "session_start", "session_id": "<uuid>", "is_new": true|false}` — Session opened
- `{"type": "text_delta", "delta": "..."}` — Streaming text chunk
- `{"type": "message_end", "session_id": "<uuid>"}` — Response complete
- `{"type": "sessions_list", "sessions": [...]}` — Session list response
- `{"type": "error", "error": "..."}` — Error

The chat panel uses `claude-agent-sdk` to manage Claude Code sessions. Session metadata is persisted to `chat_sessions.json` for resume support. SDK clients are cached per session to avoid MCP reconnection overhead.

## Dry-run mode

Preview the plan without executing:

```bash
haniel install --dry-run haniel.yaml
```

Example output:
```
[dry-run] Phase 1: Mechanical installation
  - Requirements check: python >=3.11, node >=18, winsw, claude-code
  - Directory creation: ./runtime, ./runtime/logs
  - Repository clone: my-app -> ./.services/my-app
  - Environments: main-venv (python-venv), frontend (pnpm + build)
  - Configs (static): app-mcp -> ./.services/my-app/.mcp.json

[dry-run] Phase 2: Interactive installation (Claude Code)
  - Configs (interactive): app-env -> ./.services/my-app/.env
    - Collect: SLACK_BOT_TOKEN
    - Defaults: DEBUG

[dry-run] Phase 3: Finalization
  - Register WinSW service: haniel
```

## Repository structure

```
haniel/
+-- src/
|   +-- haniel/
|       +-- __init__.py
|       +-- __main__.py            # python -m haniel entry point
|       +-- cli.py                 # Click CLI (install / run / status / validate)
|       +-- config/
|       |   +-- model.py           # Pydantic models for haniel.yaml
|       |   +-- validators.py      # Semantic validation (cycles, port conflicts, etc.)
|       +-- core/
|       |   +-- runner.py          # Poll loop, dependency graph, auto_apply logic
|       |   +-- process.py         # Process creation, monitoring, ready conditions
|       |   +-- git.py             # git clone, fetch, pull, pending changes
|       |   +-- health.py          # Service states, backoff, circuit breaker
|       |   +-- logs.py            # stdout/stderr capture, rolling buffer, pattern matching
|       |   +-- claude_session.py  # Claude Code sessions for dashboard chat panel
|       +-- dashboard/
|       |   +-- __init__.py        # Route setup, Bearer token auth middleware
|       |   +-- api.py             # REST API endpoints (status, services, repos)
|       |   +-- config_api.py      # Config CRUD API (runtime YAML modification)
|       |   +-- ws.py              # WebSocket event stream
|       |   +-- chat_ws.py         # Claude Code chat WebSocket
|       |   +-- static.py          # React SPA static file serving
|       +-- installer/
|       |   +-- orchestrator.py    # Phase 0-1-2-3 flow control
|       |   +-- mechanical.py      # Phase 1: directories, clone, venv, npm/pnpm
|       |   +-- interactive.py     # Phase 2: Claude Code session + install MCP tools
|       |   +-- finalize.py        # Phase 3: file generation, WinSW registration
|       |   +-- state.py           # Install state persistence (resume support)
|       |   +-- install_mcp_server.py  # Install-only MCP server
|       |   +-- utils.py           # Installer utilities
|       +-- integrations/
|       |   +-- mcp_server.py      # MCP Streamable HTTP server + dashboard mount
|       |   +-- webhook.py         # Slack/Discord/JSON webhook notifications
|       +-- platform/
|           +-- __init__.py        # PlatformHandler ABC + factory
|           +-- windows.py         # Job Object, CTRL_BREAK_EVENT, breakaway probing
|           +-- posix.py           # Unix signal handling
+-- dashboard/                     # React frontend (separate build)
|   +-- src/
|   |   +-- App.tsx                # 2-panel layout (services | chat)
|   |   +-- components/            # ServiceCard, RepoEditor, DependencyGraph, ChatPanel, etc.
|   |   +-- hooks/                 # useServices, useWebSocket, useChatWebSocket
|   |   +-- lib/api.ts             # REST API client
|   +-- package.json               # React 19, Vite 8, TypeScript 5.9, Tailwind CSS 4
+-- tests/                         # pytest suite (config, runner, process, git, health, etc.)
+-- docs/
|   +-- specifications.md          # This document
|   +-- configuration.md           # Full haniel.yaml field reference
|   +-- adr/
|       +-- 0001-winsw-over-nssm.md
|       +-- 0002-self-update-architecture.md
|       +-- 0003-directory-structure.md
+-- haniel-runner.ps1              # PowerShell wrapper for self-update
+-- haniel.yaml.example            # Full multi-service config example
+-- pyproject.toml                 # Python 3.11+, hatchling build
```

## Open items

1. **Log rotation**: Not yet built-in. Relying on external tools for now.
2. **Multi-environment support**: Review if dev/staging/prod separation is needed in the future.
