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
haniel handles what it can mechanically (directories, git clone, venv, npm),
and **delegates to Claude Code** for anything requiring human conversation (secret collection, configuration choices).

Installation flow:
1. haniel starts itself as an MCP server
2. Processes mechanical steps (directories, clone, venv) first
3. Launches Claude Code for interactive steps if needed
4. Claude Code passes configuration values via haniel MCP tools
5. haniel completes remaining steps (file generation, service registration)

After installation, services can be started with `haniel run`.

**Resume support**: If installation is interrupted, re-running `haniel install` offers "start fresh / resume" choice.

### `haniel run` — Runtime mode

Uses only the `repos` and `services` sections of haniel.yaml.
git poll → pull → restart processes. That's all.
Does not touch .env, .mcp.json, venv, etc.

### `haniel status` — Status query

Displays current service state, repo state, last poll time, etc.
Also queryable via Claude Code when the MCP server is running.

### `haniel validate` — Configuration validation

Validates haniel.yaml:
- YAML syntax
- Required field presence
- Circular dependency detection (`after` field)
- Port conflict detection (`ready: port:*` duplicates)
- Duplicate repo path detection

Used for pre-run validation.

## Configuration file (`haniel.yaml`)

### Full structure

```yaml
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
  transport: sse
  port: 3200

repos:
  my-app:
    url: git@github.com:org/my-app.git
    branch: main
    path: ./.projects/my-app

  my-server:
    url: git@github.com:org/my-server.git
    branch: main
    path: ./.projects/my-server

  my-data:
    url: git@github.com:org/my-data.git
    branch: main
    path: ./.projects/my-data

services:
  mcp-server:
    run: ./runtime/mcp_venv/Scripts/python.exe -X utf8 -m myapp.mcp --transport=sse --port=3104
    cwd: ./workspace
    repo: my-app
    ready: port:3104
    restart_delay: 3
    hooks:
      post_pull: ./runtime/mcp_venv/Scripts/pip.exe install -r ./runtime/mcp_requirements.txt

  bot:
    run: ./runtime/venv/Scripts/python.exe -X utf8 -m myapp.bot
    cwd: ./workspace
    repo: my-app
    after: mcp-server
    shutdown:
      signal: SIGTERM
      timeout: 15
    hooks:
      post_pull: ./runtime/venv/Scripts/pip.exe install -r ./runtime/requirements.txt

  # ... (additional services omitted, full example below)

self:
  repo: haniel          # key from repos section
  auto_update: false    # default: false (opt-in)

install:
  requirements:
    python: ">=3.11"
    node: ">=18"
    winsw: true
    claude-code: true

  directories:
    - ./runtime
    - ./runtime/logs
    - ./runtime/data
    - ./workspace
    - ./workspace/.local
    - ./workspace/.local/artifacts
    - ./workspace/.local/incoming
    - ./workspace/.local/tmp

  environments:
    main-venv:
      type: python-venv
      path: ./runtime/venv
      requirements:
        - ./runtime/requirements.txt

    mcp-venv:
      type: python-venv
      path: ./runtime/mcp_venv
      requirements:
        - ./runtime/mcp_requirements.txt

    server-venv:
      type: python-venv
      path: ./server_runtime/venv
      requirements:
        - ./server_runtime/requirements.txt

    runtime-node:
      type: npm
      path: ./runtime

  configs:
    workspace-env:
      path: ./workspace/.env
      keys:
        # Slack
        - key: SLACK_BOT_TOKEN
          prompt: "Slack Bot Token (xoxb-...)"
        - key: SLACK_APP_TOKEN
          prompt: "Slack App Token (xapp-...)"
        - key: SLACK_MCP_XOXC_TOKEN
          prompt: "Slack MCP XOXC Token"
        - key: SLACK_MCP_XOXD_TOKEN
          prompt: "Slack MCP XOXD Token"
        - key: ALLOWED_USERS
          prompt: "Allowed user IDs (comma-separated)"
        - key: NOTIFY_CHANNEL
          prompt: "Notification channel ID"
        # API Keys
        - key: GEMINI_API_KEY
          prompt: "Gemini API Key"
        - key: OUTLINE_API_KEY
          prompt: "Outline API Key"
        - key: OUTLINE_API_URL
          prompt: "Outline API URL"
        - key: TRELLO_API_KEY
          prompt: "Trello API Key"
        - key: TRELLO_TOKEN
          prompt: "Trello Token"
        # Paths
        - key: LOG_PATH
          default: "{root}/runtime/logs"
        - key: SESSION_PATH
          default: "{root}/runtime/sessions"
        - key: MEMORY_PATH
          default: "{root}/runtime/memory"
        # Flags
        - key: DEBUG
          default: "false"

    workspace-mcp:
      path: ./workspace/.mcp.json
      content: |
        {
          "mcpServers": {
            "my-mcp": {
              "url": "http://localhost:3104/sse"
            },
            "haniel": {
              "url": "http://localhost:3200/sse"
            },
            "slack": {
              "url": "http://localhost:3101/sse"
            },
            "trello": {
              "url": "http://localhost:3102/sse"
            },
            "outline": {
              "url": "http://localhost:3103/sse"
            }
          }
        }

    webhook-config:
      path: ./runtime/data/watchdog_config.json
      keys:
        - key: slackWebhookUrl
          prompt: "Slack Webhook URL (for notifications)"

  service:
    name: haniel
    display: "Haniel Service Runner"
    working_directory: "{root}"
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
- npm install
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
  |     Is Claude Code installed?
  |     +- No: print installation instructions and abort
  |     +- Yes: continue
  |
  +- Phase 1: Mechanical installation (haniel alone)
  |     Load haniel.yaml
  |     Verify requirements (python, node, winsw)
  |       Unmet items -> record for Phase 2, Claude Code will guide user
  |     Create directories
  |     Clone repos (where possible)
  |     Set up environments (venv, npm)
  |     Generate static configs (content-based)
  |     |
  |     Compile results as JSON:
  |       - Completed steps
  |       - Failed steps with error messages
  |       - Configs requiring interactive input
  |     Save state to install.state
  |
  +- Phase 2: Interactive installation (Claude Code delegation)
  |     haniel starts itself as MCP server (install-only mode)
  |     Launch Claude Code session:
  |       claude -p --mcp-config haniel-install-mcp.json \
  |         "Continue the haniel installation. (install state JSON passed)"
  |     |
  |     Claude Code converses with user:
  |       - Guides resolution of failed steps
  |       - Collects secrets/config values
  |       - Runs validation if validate field exists
  |       - Passes values via haniel MCP tools:
  |           haniel_set_config(file="workspace-env", key="SLACK_BOT_TOKEN", value="xoxb-...")
  |           haniel_set_config(file="webhook-config", key="slackWebhookUrl", value="https://...")
  |       - When all values are filled:
  |           haniel_finalize_install()
  |     |
  |     Claude Code session ends (auto-exit on finalize)
  |
  +- Phase 3: Finalization (haniel alone)
  |     On finalize signal:
  |       Generate config files from collected values
  |       Register WinSW service
  |       Stop MCP server (exit install mode)
  |       Mark install.state as complete
  |
  +- Done
      "Installation complete. Start the service with 'sc start haniel'."
```

### Install-only MCP tools

MCP tools active only in install mode. Claude Code uses these to drive the installation.

```
haniel_install_status()
  -> Returns current installation progress
    {
      "phase": "interactive",
      "completed": ["directories", "repos", "environments"],
      "failed": [{"step": "requirements", "detail": "winsw not found"}],
      "pending_configs": [
        {
          "name": "workspace-env",
          "path": "./workspace/.env",
          "missing_keys": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", ...],
          "filled_keys": ["LOG_PATH", "DEBUG", ...]
        }
      ]
    }

haniel_set_config(config_name, key, value)
  -> Sets a value for a specific config key
  -> Example: haniel_set_config("workspace-env", "SLACK_BOT_TOKEN", "xoxb-1234...")

haniel_get_config(config_name)
  -> Returns current state of a config (filled keys, missing keys)

haniel_retry_step(step_name)
  -> Retries a failed installation step
  -> Example: haniel_retry_step("requirements") — after user installs winsw

haniel_finalize_install()
  -> Verifies all required values are filled
  -> Generates config files, registers WinSW, exits install mode
  -> Sends session termination signal to Claude Code
```

### Instructions passed to Claude Code

When haniel invokes Claude Code, it passes these instructions as a prompt:

```
You are the haniel installation assistant.
Converse with the user to collect configuration values needed to run services.

Using haniel MCP tools:
1. Check current status with haniel_install_status()
2. If there are failed steps, guide the user through resolution
3. For each missing_key:
   - Explain the value's purpose and how to obtain it (reference the guide field)
   - Ask the user for the value
   - If a validate field exists, run the validation command and report the result
   - On validation success, set the value with haniel_set_config()
4. When all values are filled, call haniel_finalize_install()
   A successful call automatically ends the session.

Each config key has a prompt field hinting how to ask the user.
Keys with a default should prompt "Use default {default}?"
Keys with validate should be verified after input; re-prompt on failure.
```

### `requirements` — System requirements

```yaml
install:
  requirements:
    python: ">=3.11"
    node: ">=18"
    winsw: true
    claude-code: true     # Verified in Phase 0 (required prerequisite)
```

claude-code is verified separately in Phase 0 (installation cannot proceed without it).
Others are verified in Phase 1, but failures don't abort — they're recorded.
Claude Code guides the user through resolution in Phase 2.

### `directories` — Directory creation

```yaml
install:
  directories:
    - ./runtime
    - ./runtime/logs
    - ./workspace
```

Creates missing directories. Skips existing ones.
Processed mechanically in Phase 1.

### `environments` — Execution environments

```yaml
install:
  environments:
    {name}:
      type: python-venv | npm
      path: {directory}
      requirements:
        - {requirements.txt path}
```

Processed mechanically in Phase 1.
On failure, recorded and troubleshot by Claude Code in Phase 2.

### `configs` — Configuration file definitions

Two modes:

**Static (`content`)** — Auto-generated in Phase 1:
```yaml
configs:
  workspace-mcp:
    path: ./workspace/.mcp.json
    content: |
      { "mcpServers": { ... } }
```

**Interactive (`keys`)** — Collected by Claude Code in Phase 2:
```yaml
configs:
  workspace-env:
    path: ./workspace/.env
    keys:
      - key: SLACK_BOT_TOKEN
        prompt: "Slack Bot Token (xoxb-...)"
        guide: "https://api.slack.com/apps -> select app -> OAuth & Permissions -> Bot User OAuth Token"
        validate: "curl -s -H 'Authorization: Bearer {value}' https://slack.com/api/auth.test | jq -e '.ok'"
      - key: LOG_PATH
        default: "{root}/runtime/logs"
```

- `prompt`: Description for Claude Code when asking the user
- `guide`: URL or instructions for obtaining/verifying the value. Claude Code presents this to the user
- `validate`: Validation command. `{value}` is substituted with the input. Claude Code runs this and reports results
- `default`: Default value. `{root}` is substituted with the absolute path of haniel.yaml's location

If the file already exists, existing values are preserved; only new keys appear as missing_keys.

### `service` — WinSW service registration

```yaml
install:
  service:
    name: haniel
    display: "Haniel Service Runner"
    working_directory: "{root}"
    environment:
      PYTHONUTF8: "1"
```

Registered as a Windows service via WinSW in Phase 3 (finalize).

## `repos` section

Defines repositories. Auto-cloned during install, auto-polled at runtime.

```yaml
repos:
  {name}:
    url: {git clone URL}           # Required. Clone and fetch target
    branch: {branch name}          # Required. Branch to track
    path: {local path}             # Required. Local path (relative to haniel.yaml)
```

**Auto-clone flow (install):**
1. Check if `path` exists
2. If missing: `git clone --branch {branch} {url} {path}`
3. If present: verify `.git` is valid and remote URL matches
4. On mismatch: error (no automatic correction)

**Runtime poll:**
1. `git fetch origin {branch}`
2. If `local_head != remote_head`: changes detected

## `services` section

Defines services. **YAML order determines startup order.**

```yaml
services:
  {name}:
    run: {command}                 # Required. Execution command
    cwd: {directory}               # Optional. Working directory (default: haniel.yaml location)
    repo: {repo name}              # Optional. Restart when this repo changes
    restart_delay: {seconds}       # Optional. Delay before crash restart (default: backoff)
    after: {service name}          # Optional. Wait for this service to be ready
    ready: {condition}             # Optional. "Ready" judgment condition
    shutdown:                      # Optional. Shutdown method
      signal: SIGTERM              #   Default: SIGTERM
      timeout: 15                  #   Graceful wait (default: global shutdown.timeout)
      method: http                 #   Optional. If http, send HTTP shutdown request
      endpoint: /shutdown          #   Used when method: http
    enabled: true                  # Optional. If false, skip (default: true)
    hooks:                         # Optional. Lifecycle hooks
      post_pull: {command}         #   Run after git pull
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

If `ready` is absent and the service is referenced by `after`, `delay:3` is used as default.

### `after` startup ordering syntax

```yaml
# Single dependency
after: mcp-server

# Multiple dependencies (all must be ready)
after:
  - mcp-server
  - database
```

Services without `after` start immediately in YAML order.

### `hooks` lifecycle hooks

```yaml
hooks:
  post_pull: {command}            # Run after git pull (builds, dependency installs, etc.)
```

haniel doesn't care what hooks do.
Non-zero exit codes trigger a webhook notification; service startup continues regardless.

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
- `auto_update: false` — Sends webhook, enters pending state, waits for `haniel_approve_update()` MCP tool call

The wrapper script (`haniel-runner.ps1`) interprets exit code 10 as a signal to update and restart.

## Log handling

haniel captures each service's stdout/stderr and writes them to per-service log files.

```
{haniel.yaml location}/logs/
+-- mcp-server.log
+-- database.log
+-- bot.log
+-- ...
```

- Services may create their own log files separately
- haniel only captures stdout/stderr
- The `ready: log:{pattern}` feature uses this captured output
- Log rotation is under consideration (external tool vs built-in)

## Runtime behavior cycle

### Startup

```
haniel run
  +- Load haniel.yaml
  +- Check repos:
  |    Path exists? -> OK
  |    Path missing? -> git clone (recover from missed install)
  |    Clone failed? -> webhook alert, skip dependent services
  +- Start services sequentially:
  |    In YAML order:
  |      enabled == false? -> skip
  |      has after? -> wait for target service's ready condition
  |      Create process with run command
  |      Begin stdout/stderr capture
  |      If ready condition exists, wait for it (timeout = warning, startup continues)
  |      Webhook: "Service started"
  +- Enter poll loop
```

### Poll loop

```
Every poll_interval:

  [Phase 1: Change detection]
  For each repo:
    git fetch origin {branch}
    Compare local_head vs remote_head
    Changes detected? -> Add to change list

  [Phase 2: Apply changes]
  If repos have changes:
    Compute list of dependent services

    a. Webhook: "Changes detected - {repo}: {commits}"
    b. Graceful shutdown of dependent services in reverse order
       SIGTERM -> timeout wait -> SIGKILL
       If shutdown takes too long, webhook alert
    c. Webhook: "Applying changes"
    d. git pull
    e. Run post_pull hook (if any)
    f. Restart dependent services in forward order
    g. Webhook: "Startup complete"

  [Phase 3: Health check]
  For each service:
    Is the process alive?

    Alive -> pass
    Dead ->
      Check circuit breaker:
        >= circuit_breaker failures within circuit_window -> stop starting + webhook
        Otherwise -> backoff then restart + webhook
```

### Graceful shutdown

```
Service shutdown request:
  shutdown.method == http?
    -> Send HTTP request to shutdown.endpoint
    -> Wait for response
  Otherwise:
    -> Send shutdown.signal (default: SIGTERM)

  Wait for shutdown.timeout
    +- Exited within timeout -> done
    +- Timeout exceeded ->
         Webhook: "Graceful shutdown failed, force killing"
         SIGKILL until kill_timeout
         +- Exited -> done
         +- Still alive -> Webhook: "Force kill failed, manual intervention required"
```

## MCP server

haniel also runs as an MCP server, allowing Claude Code to query status and control services.

### Resources (read)

```
haniel://status                    -> All service statuses
haniel://status/{service}          -> Specific service status
haniel://repos                     -> Repo statuses (HEAD, last fetch, etc.)
haniel://logs/{service}?lines=50   -> Recent logs
```

### Tools (write)

```
haniel_restart(service)            -> Restart a specific service
haniel_stop(service)               -> Stop a specific service
haniel_start(service)              -> Start a specific service
haniel_pull(repo)                  -> Immediately pull a repo + restart dependent services
haniel_enable(service)             -> Re-enable a service stopped by circuit breaker
haniel_reload()                    -> Reload haniel.yaml (keep processes, update config only)
haniel_approve_update()            -> Approve a pending self-update
```

## Dry-run mode

Preview the plan without executing:

```bash
haniel install --dry-run haniel.yaml
```

Example output:
```
[dry-run] Phase 1: Mechanical installation
  - Requirements check: python >=3.11, node >=18, winsw, claude-code
  - Directory creation: ./runtime, ./runtime/logs, ./workspace, ...
  - Repository clone: my-app -> ./.projects/my-app
  - Environments: main-venv (python-venv), runtime-node (npm)
  - Configs (static): workspace-mcp -> ./workspace/.mcp.json

[dry-run] Phase 2: Interactive installation (Claude Code)
  - Configs (interactive): workspace-env -> ./workspace/.env
    - Collect: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ...
    - Defaults: LOG_PATH, SESSION_PATH, DEBUG, ...

[dry-run] Phase 3: Finalization
  - Register WinSW service: haniel
```

## Repository structure

```
haniel/
+-- src/
|   +-- haniel/
|       +-- __init__.py
|       +-- __main__.py          # CLI entry point (install / run / status / validate)
|       +-- config/              # YAML parsing, validation
|       +-- installer/
|       |   +-- __init__.py
|       |   +-- orchestrator.py  # Phase 1-2-3 flow control
|       |   +-- mechanical.py    # Phase 1: directories, clone, venv, npm
|       |   +-- interactive.py   # Phase 2: Claude Code session management
|       |   +-- finalize.py      # Phase 3: file generation, WinSW registration
|       +-- core/
|       |   +-- runner.py        # Run mode: poll loop
|       |   +-- process.py       # Process creation, monitoring, shutdown
|       |   +-- git.py           # git fetch, pull, clone
|       |   +-- health.py        # Health checks, circuit breaker
|       +-- integrations/
|       |   +-- webhook.py       # Slack/Discord/JSON notifications (Block Kit)
|       |   +-- mcp_server.py    # MCP server (run + install modes)
|       +-- platform/
|           +-- __init__.py
|           +-- windows.py       # Windows Job Object, SIGTERM emulation
|           +-- posix.py         # Unix signal handling
+-- tests/
+-- docs/
|   +-- specifications.md       # This document
|   +-- adr/
|       +-- 0001-winsw-over-nssm.md
|       +-- 0002-self-update-architecture.md
+-- haniel-runner.ps1            # PowerShell wrapper for self-update
+-- pyproject.toml
+-- haniel.example.yaml
```

## Open items

1. **requirements.txt source**: Whether runtime is a separate repo or included in the main repo affects environments config
2. **Per-process dotenv loading**: If existing code relies on supervisor's environment variable inheritance, code changes are needed
3. **Windows SIGTERM**: Replace with CTRL_BREAK_EVENT or HTTP shutdown endpoint
4. **Log rotation**: Built-in vs external tool
5. **Port number duplication**: Same port specified in both services' `run` and configs' `.mcp.json`
6. **Claude Code invocation**: Exact CLI interface for `claude -p` prompt mode + `--mcp-config` combination
7. **Install MCP vs run MCP**: Whether the same MCP server exposes different tools by mode, or separate servers
8. **Multi-environment support**: Review if dev/staging/prod separation is needed in the future
