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
| `poll_interval` | int | `60` | Seconds from the start of one git fetch poll to the start of the next. A poll that runs longer than this is followed by the next one at once. A scheduled service restart does not wait for the next poll |
| `git_fetch_timeout` | int | `900` | Maximum seconds for each polling Git fetch |
| `auto_apply` | bool | `true` | If `false`, detected changes are shown in dashboard but not auto-applied. Manual "Update" from dashboard is still possible. Repository-level `auto_apply` values are ignored while this is `false` |
| `secrets_file` | string | — | dotenv file containing values for secret references |
| `shutdown` | object | — | Global shutdown behavior |
| `backoff` | object | — | Restart backoff and circuit breaker |
| `webhooks` | list | — | Notification endpoints |
| `mcp` | object | — | MCP server settings. Without this block the MCP server is not started and neither is the dashboard API |
| `dashboard` | object | — | Built-in web dashboard settings. Without this block there is no dashboard and `haniel status` cannot ask the running instance |
| `repos` | map | `{}` | Git repositories to track |
| `services` | map | `{}` | Processes to manage |
| `self` | object | — | Self-update mechanism |
| `orchestrator_client` | object | — | Remote orchestrator node connection |
| `install` | object | — | Installation phase (used only by `haniel install`) |

## Validation

`haniel validate haniel.yaml` loads the file and reports every problem it finds, then exits 1 if any is an error. It checks:

- YAML syntax, required fields, and field types.
- **Unknown keys** at any depth, with the path and the closest known name (`unknown key services.api.redy (did you mean ready?)`, `unknown key serivces (did you mean services?)`).
- **Unusable values**: a `ready` that is not `port:<1-65535>`, `delay:<seconds>`, `log:<pattern>`, or `http:<url>`; a `shutdown.signal` (global or per service) that is not a signal name such as `SIGTERM`; a negative `restart_delay`, `backoff.base_delay`, or `backoff.max_delay`; an empty repo `url`; an empty `orchestrator_client.url` or `token`.
- References and structure: an `after` or `repo` that names nothing, `after` cycles, duplicate `ready: port:` numbers, duplicate repo paths.
- **No services** is a warning, not an error: an empty file or a misspelled top-level `services` key passes with `no services are defined`.

Where the same file is read elsewhere the reaction differs on purpose:

| Where | Unknown key or bad value |
|-------|--------------------------|
| `haniel validate` | Error, exit code 1 |
| MCP and REST saves (`haniel_create_service_config`, `haniel_update_service_config`, the repo equivalents, `haniel_register_service`, `/api/config/...`) | The service or repo being written is rejected with the reasons. Old problems in other entries do not block the save |
| `haniel run` and config reload | Logged as a WARNING (at startup, on the console or in the service journal and in `haniel.log`), and haniel starts anyway. A `ready` that cannot be used also logs a WARNING when the service starts, and the service is treated as running |

## Saving configuration through tools

MCP tools and the REST config API rewrite the whole file from the loaded configuration; they do not edit it in place. Comments are removed, a key that the loader does not know (for example one left on another entry) is dropped, and defaults the file did not spell out are written. The previous file is kept as `haniel.yaml.bak` next to it. Keep comments you care about out of files that tools save, or edit the file by hand and call `haniel_reload`.

## Logs

haniel writes each service's stdout and stderr to `<folder of haniel.yaml>/logs/<service>.log`, and its own messages to `<folder of haniel.yaml>/logs/haniel.log`. The logs do not go into the service's repository, whatever `cwd` is. Read them with `haniel_read_logs` (a service that is not in the config is an error), the `haniel://logs/<service>` resource, or the files directly. haniel keeps the last 1000 lines per service in memory for those queries and does not rotate files.

## Secret references

Set `secrets_file` to a dotenv file, using a path relative to the directory
containing `haniel.yaml` (or an absolute path). The file uses `NAME=value`
assignments. Its values are read without environment-variable interpolation
and are not added to the Haniel process environment.

```yaml
secrets_file: .secrets/haniel.env
webhooks:
  - url: ${secret:WEBHOOK_URL}
slack:
  bot_token: ${secret:SLACK_BOT_TOKEN}
  app_token: ${secret:SLACK_APP_TOKEN}
  notify_user: U123456
install:
  configs:
    app-env:
      path: .services/app/.env
      keys:
        - key: API_KEY
          default: ${secret:APP_API_KEY}
```

References can occupy a whole value or be embedded in a string. They are
supported in `webhooks[].url`, `slack.bot_token`, `slack.app_token`,
`orchestrator_client.token`, `dashboard.token`,
`install.service.service_account.password`, and `install.configs` key defaults
and content. Other placeholders such as `${VAR}`, `$VAR`, and `{root}` keep
their existing meaning. A missing file or name prevents the configuration from
loading. The configuration model and edits retain references; install output
files receive the resolved values where needed.

Install output keeps resolved plaintext values. The WinSW XML file for the
service (`<service>.xml`) stores `install.service.service_account.password` in
its `<password>` element, and `haniel-runner.conf` stores the first resolved
webhook URL as `WEBHOOK_URL`. WinSW service registration and the bootstrap
runner (`haniel-runner.sh`, `haniel-runner.ps1`) cannot resolve
`${secret:...}` references themselves, so these files receive values.

On POSIX, Haniel writes these files with mode `600`. On Windows it does not
restrict their ACL, so on a machine shared by several users, narrow the
permissions by hand (for example with `icacls`). After changing a secret
value, re-run `haniel install` so the generated files receive the new value.

Run `haniel validate --no-plain-secrets haniel.yaml` to reject plain values in
Haniel's own secret fields and values of eight or more characters from the
secrets file appearing literally anywhere in the configuration. Existing plain
configurations continue to work without this option.

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
Use the `haniel_reset_circuit(service)` MCP tool to allow restarts again without touching `haniel.yaml`, or `haniel_enable(service)` to undo `haniel_disable`: it sets `enabled: true` in `haniel.yaml` (when it was disabled), resets the circuit breaker, and starts the service. An unknown service name is an error. Resetting the circuit of a service that is already running leaves it alone.

A failed service counts toward `circuit_breaker` until it proves it works. A service with a `ready` condition does once that condition holds. A service **without** `ready` must stay up for more than 10 seconds before its consecutive failure count returns to 0, so one that dies right after starting opens the circuit instead of restarting forever.

A scheduled restart (backoff or `restart_delay`) runs when it is due. It does not wait for the next git poll.

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
  host: 127.0.0.1
  transport: streamable_http
  port: 3200
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether the MCP server is active. This default applies when the `mcp:` block is present; with no block the MCP server is not started (the log says `MCP server not started: there is no mcp block in the config. To enable it, write mcp: {enabled: true}`; with `enabled: false` it says `MCP server not started: mcp.enabled is false`). Write `mcp: {enabled: true}` to turn it on |
| `host` | string | `127.0.0.1` | Bind address shared by MCP and dashboard |
| `transport` | string | `streamable_http` | Currently ignored: read and shown, but the transport is always Streamable HTTP |
| `port` | int | `3200` | Port for the MCP server |

When the dashboard is enabled, it shares the same port and Starlette server. If the port is already in use, an ERROR is written to `haniel.log` and the MCP server does not start; the services keep running.

Tool calls that name an unknown service or repo, or whose config change is rejected, return an MCP error result (`isError: true`) with the reason instead of a success message.

## `dashboard`

Built-in web dashboard for browser-based service management.

```yaml
dashboard:
  enabled: true
  port: null    # currently ignored; the dashboard shares the MCP port
  token: "your-secret-token"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether the dashboard is active |
| `port` | int | — | Currently ignored: read but not used. The dashboard always shares the MCP server port |
| `token` | string | — | Bearer token for API/WebSocket authentication. If omitted, dashboard is accessible without auth (a warning is logged) |

When a token is set, `/api/*` requires `Authorization: Bearer <token>`,
including for local requests. `/mcp` and `/mcp/*` require the same header only
for connections from outside the local computer, including when the dashboard
is disabled. Dashboard WebSocket connections use the token query parameter.

## `repos`

Map of git repositories to track. Keys are arbitrary names used to reference repos from services.

```yaml
repos:
  my-app:
    url: git@github.com:org/my-app.git
    branch: main
    path: ./.services/my-app
    release_manifest: deploy/release-manifest.json
    # Optional for a slow detached checkout, for example on Windows:
    staging_checkout_timeout: 1800
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | *required* | Git clone URL (HTTPS or SSH) |
| `branch` | string | `main` | Branch to track |
| `path` | string | *required* | Local path, relative to `haniel.yaml` location |
| `auto_apply` | boolean | `true` | Whether this repository may apply changes without approval while the global `auto_apply` is on. A repository can only disable its own updates; the value is ignored when the global switch is off |
| `hooks.post_pull` | string | — | Legacy repository build command, run once from the repository directory |
| `hooks.timeout` | int | `900` | Maximum seconds allowed for each repository hook |
| `release_manifest` | string | — | Repository-relative path to a `haniel.release.v1` deployment contract |
| `staging_checkout_timeout` | integer (1–7200) | `900` | Limit for this repository's detached release checkout only |

**Poll behavior**: Every `poll_interval` seconds, haniel runs `git fetch` and compares local HEAD with remote. If they differ, changes are detected.

**Auto-clone**: `haniel install` and `haniel run` clone a repo whose `path` does not exist, on its `branch`. In `haniel run` a failed clone logs an ERROR with the repo name and URL, and that repo's services then fail to start (their working directory is missing) until the path exists. A repo that was just cloned runs its `post_pull` like an updated repo. The `self` repo is not cloned by `run`; `haniel install` or the Windows bootstrap script put it in place.

`staging_checkout_timeout` applies only to `git worktree add --detach` while a manifest release target is staged. It does not alter `git_fetch_timeout` or the timeouts for other Git operations. A slow Windows checkout can therefore set `1800` for its one repository without widening the rest of the node.

Other Git operations without an explicit timeout, including `git reset --hard`,
have a 900-second limit. A manifest deployment's declared budget includes the
repository's staging checkout limit and, for force activation, the reset limit.

For legacy startup updates and approved pulls, the repository `post_pull` runs
once before its service hooks, even if no service belongs to the repository.
Manifest deployments warn and do not run the repository hook; configure build
steps for that path as service hooks instead.

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

Every command runs without a shell in the repository root. Commands receive `HANIEL_DEPLOY_REPO`, `HANIEL_RELEASE_ID`, `HANIEL_PREVIOUS_HEAD`, and `HANIEL_TARGET_HEAD`. `environment_service` is optional and must only be used when a manifest truly targets one stable Haniel service key; deployment-specific service names should stay out of repository manifests.

A manifest may also declare `service_restart_filters`: a map from Haniel service name to `ignore_paths`. When every path changed between the deployed commit and its predecessor matches one pattern for an affected service, Haniel does not stop or restart that service. Patterns are matched with `fnmatch.fnmatchcase` against repository-relative POSIX paths, so `*` also crosses `/`. A service that starts after (`after`) another affected service that still restarts is not skipped. In contract mode (`migration.operation` set), the skip applies for an upgrade when the database preflight reports an integer `pending_count` of `0`, or a positive integer `pending_count` and boolean `pending_additive_only: true`. Other counts or signals stop every affected service as before. Build hooks, readiness checks, and post-start verification still run for the full affected set, and the deployment journal records the services left running in `restart_skipped_services`.

A manifest may declare `service_restart_strategies` keyed by service name, with `{"mode": "overlap", "exit_timeout_sec": 60}`. The old and target manifests must both declare overlap and the service must be running; for contract upgrades, preflight must also report integer `pending_count: 0`, or a positive integer `pending_count` and boolean `pending_additive_only: true`. The first deployment adding the declaration therefore uses the normal stop/start path. Haniel starts the replacement beside the old process, waits for its configured readiness, then waits up to `exit_timeout_sec` for the old process to exit before sending SIGTERM and, after the shutdown timeout, SIGKILL. The service itself must implement the handover and support concurrent binding. Dependents can remain running when their own restart filters match. The attempt journal records selected services in `overlap_services`. A replacement that fails readiness is stopped while the old process remains in place.

`{"mode": "adopt"}` is the other value. It only changes the Haniel self-update: the running process is not stopped, no replacement is started, and the next Haniel takes over the same PID. It suits a service that cannot bind concurrently, so `overlap` is not possible. A manifest deployment restarts an `adopt` service by the ordinary stop and start, and `exit_timeout_sec` is not used. Older Haniel versions reject a manifest that contains `adopt` because the field is validated strictly. Update Haniel on every node before adding `adopt` to a manifest.

Every start of a service for which the current manifest declares overlap or adopt sends its stdout and stderr through two `haniel.core.log_sink` relay processes (`<haniel python> -m haniel.core.log_sink --service <name> --source stdout|stderr --log-file <log>`). The service remains Haniel's child, while the relays own the log file and keep appending `[timestamp] [stdout|stderr]` lines after Haniel itself is replaced. Haniel reaps the relays and closes its copies of their stdin pipes so each relay exits on service EOF. `=== Service started ===` and `=== Service stopped ===` markers remain Haniel's. Services with a `log:` readiness condition keep the direct pipe path.

A manifest can also declare `change_ignore_paths`, a list of patterns using the same matching rules (see `examples/release-manifest.json`, which lists `*.md`, `docs/**`, and `examples/**`). Without it every commit counts as a change: a commit that only touches `README.md` still creates a pending update and, once applied, restarts every service of the repo and the services after them. During polling, if every path between the local HEAD and the remote branch matches, Haniel creates no deployment pending and leaves the checkout unchanged. Unlike `service_restart_filters`, this prevents the deployment itself; the ignored commits are included when a later change requires deployment. Older Haniel versions reject manifests containing this field because unknown fields are forbidden. Update Haniel on every node before adding it to a manifest.

A destructive migration is rejected unless both backup commands are present. The build phase retries the complete set of affected-service `post_pull` hooks with bounded exponential backoff, so these hooks must be safe to rerun. Post-start verification retries only the failed command; commands that already passed are not repeated. Both retry policies default to four attempts with 5-second initial and 20-second maximum backoff inside one shared 60-second wait budget. A shutdown request interrupts either retry wait. If verification is still failing after services reached readiness, Haniel records `verification_failed` and reports failure while preserving the target HEAD and ready processes. Failures before readiness retain the declared rollback or roll-forward recovery. Roll-forward recovery must declare a `fallback`: when recovery itself still fails, Haniel runs that command and then rolls code and processes back to the previous release. The repository decides whether the fallback preserves the new data layout or restores a verified backup. A database restore is safe only after the repository verifies a cluster-wide write fence; a rollback-compatible migration should use a data-preserving fallback instead.

The conventional path is `deploy/release-manifest.json`. If a fetched remote branch introduces a valid manifest at that path while `haniel.yaml` has no `release_manifest`, Haniel atomically adds the one field before pulling any new code, preserves a checksum-addressed backup of the previous config, and immediately uses the manifest state machine. If Haniel was started without a writable `config_path`, it blocks the pull and keeps the previous checkout.

The transition can also be inspected or applied out of band. `check` is read-only and exits 2 while activation is required. `apply` requires the fresh checksum printed by `check`, so a concurrent config edit cannot be overwritten.

```bash
python -m haniel.config.release_activation check --config /path/to/haniel.yaml --repo soulstream
python -m haniel.config.release_activation apply --config /path/to/haniel.yaml --repo soulstream --expected-sha256 <sha256-from-check>
```

The same state machine is used by an approved pull and by Haniel startup updates. Startup updates pull only repositories whose effective `auto_apply` is enabled (both the global `auto_apply` and the repository-level `auto_apply` must be true; the repository-level value can only disable and is ignored while the global switch is off) and fetch those repositories in parallel. Repositories that may not apply keep their remote changes pending for approval, and their recorded HEAD is not advanced, so the next poll still detects and reports them. Services whose repositories were not updated start first; each updated repository then builds on its own startup lane, and only that lane's services start after its build and deployment. A slow `post_pull` in one lane therefore does not hold up services in other lanes. Startup does not rerun legacy `post_pull` or start calls for manifest-owned services; the handover starts and verifies those services exactly once. The journal under `.haniel/deployments/` records `build`, `preflight`, `migrating`, `starting`, `verifying`, `recovering`, and the terminal state. A repeated approval of the same successful target commit and release ID is a no-op. Haniel reports success only after every affected service is ready and every post-start verification succeeds. A recovered deployment still reports failure, preserving the distinction between release success and restored availability.

### Handover wait note

Pass `haniel handover --wait` explicitly for the caller's receipt window. It does not change the detached checkout timeout. `REQUEST_IN_PROGRESS` means the resident accepted the request but the caller stopped waiting before a terminal receipt; inspect the request's terminal journal/receipt rather than treating that message as the staging result. A terminal `PULL_TIMEOUT` is the actual checkout failure.

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
| `after` | string or list | `[]` | Service(s) that must be running and ready before this one starts (see below) |
| `ready` | string | — | Ready condition (see below) |
| `ready_timeout` | int | `60` | Maximum seconds to wait for the ready condition |
| `shutdown` | object | — | Per-service shutdown override (same fields as global `shutdown`) |
| `enabled` | bool | `true` | If `false`, service is skipped. `haniel_disable` writes `false`; `haniel_enable` writes `true` again and starts the service |
| `hooks` | object | — | Lifecycle hooks |
| `reflect` | bool | `false` | Currently ignored: read and shown, but nothing in haniel uses it |

### `ready` condition syntax

```yaml
ready: port:3104           # Ready when port enters LISTEN state
ready: delay:5             # Ready after 5 seconds
ready: "log:Server started"  # Ready when pattern appears in stdout/stderr
ready: http://localhost:3104/health  # Ready when GET returns 2xx
```

If `ready` is absent, the service counts as ready as soon as its process is running; there is no default delay. For `after` ordering that means "is running". A value that matches none of the four forms is reported by `haniel validate` (and logged as a WARNING by `haniel run`).

### `after` ordering

```yaml
# Single dependency
after: mcp-server

# Multiple dependencies (all must be ready)
after:
  - mcp-server
  - database
```

A service with `after` starts only when each dependency is running and, if it has a `ready` condition, that condition holds. haniel waits for it up to the dependency's `ready_timeout` (default 60 seconds); a dependency without `ready` is ready once it is running. If a dependency does not become ready in time or is not running, the service is **skipped** with `<name> skipped: dependency <dep> failed`. It is retried later only when the dependency itself is waiting for its own restart; if the dependency's circuit is open, it stays down until you start it. The same rule applies when a service restarts after a pull. Services that do not depend on each other do not wait for each other.

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

Hook failures are reported; the startup and rollback rules below determine
whether services can start.

For startup updates, haniel fetches every managed repository in parallel but
keeps pulls, manifest activation, and build records serialized per repository.
Services of repositories that were not updated start first; each updated
repository then runs its `post_pull` build on its own lane and only that lane's
services start afterwards. haniel records the commit whose `post_pull` completed
in `.haniel/legacy_post_pull.json` and flushes the checkout and marker to disk
before starting services. On the next start, a checkout whose HEAD differs from
that record is treated as an interrupted update and rebuilt once (`force`
strategy discards tracked changes first). If a startup pull's `post_pull` fails,
haniel returns to the pre-pull commit and reruns the repository and service builds;
if a merge checkout has uncommitted tracked changes, it does not reset and the
repository's services remain blocked. When rebuilding after rollback also fails
and the repository had a completed build record, services using the repository
are not started. A repository without any record (the first start of this
version, or a newly added repository) is not rebuilt: haniel records its current
HEAD as the completed build and starts its services, so the first start does not
hold the whole node down while every hook runs. If such a repository is pulled
during that same startup and its `post_pull` still fails after the rollback, the
error is reported, no record is written, and startup continues. Release
manifest builds flush the worktree the same way before deployment proceeds.

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

A self-update does not follow the global `auto_apply`: it waits for approval by default, even when the global `auto_apply` is `true`, and applies on its own only when `auto_update` is `true`.

When changes are detected:
- `auto_update: true` — Shuts down all services and exits with code 10
- `auto_update: false` — Sends webhook notification and waits for approval. Approve it from the orchestrator's Pending screen (web hub or iOS app) when the node is connected to one, with `haniel_update(service="haniel")` or `haniel_pull(repo=<the self repo>)` over MCP, or with "Approve" in the local dashboard (`POST /api/self-update/approve`).

**Approving a self-update restarts the Haniel process, so every service on this node stops for a moment.** Only services that an active release manifest declares `overlap` or `adopt` stay up (see below). The same is true of `haniel_restart(service="haniel")`, which restarts Haniel without updating it (exit code 11). Changes pending in other repos are not applied by the self-update; approve them after Haniel is back.

The wrapper script (`haniel-runner.ps1` on Windows, `haniel-runner.sh` on Linux and macOS) interprets exit code 10 as "update me," prepares a release for the new commit, switches to it, and relaunches; exit code 11 relaunches without updating. On Linux the systemd unit runs the wrapper only when the config has this `self:` block; without it the unit runs `haniel run` directly and an approved self-update cannot pull. See [ADR-0002](adr/0002-self-update-architecture.md).

During a self-update shutdown Haniel keeps overlap-declared relay-backed services running and atomically writes `.local/self_update_handover.json` with each service name, PID, repository, repository HEAD, and overlap exit timeout. The next Haniel reads and deletes that file once at startup. For an entry whose PID still matches the service, whose repository HEAD is unchanged, and whose current manifest still declares overlap, Haniel starts the replacement beside the old process, waits for its readiness, then waits for the old PID to exit before SIGTERM and, after the shutdown timeout, SIGKILL. The result is logged as `Self-update handover for <service>: ... method=self|term|kill`. Any mismatch or readiness failure stops the replacement and the old PID and starts the service through the normal path.

An `adopt`-declared relay-backed service is kept running the same way, and the state file entry carries `"mode": "adopt"` (a missing mode means overlap). The next Haniel starts no replacement. For an entry whose PID still matches the service, whose repository HEAD is unchanged, and whose current manifest still declares `adopt`, it registers the PID as the managed process and runs the service's ready check once, then logs `Adopted <service> (pid <N>) without restart`. Any mismatch (the PID is gone or is another process, the HEAD moved, the declaration changed) or a failed ready check retires the old PID through the same SIGTERM then SIGKILL steps and starts the service through the normal path. The adopted process is not a child, so liveness is judged by PID: about every 2 seconds on POSIX and every 10 seconds or more on Windows, where each check starts powershell. A PID that disappears without being stopped goes through the usual crash handling (restart delay, then a normal start) and its exit code is logged as unknown. Stopping the service (deployment restart, Haniel shutdown) uses the configured graceful shutdown, then the platform's terminate and kill with the same timeouts as any service. An adopted service counts as relay-backed, so the next self-update hands it over and adopts it again.

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
1. **Phase 0 (Bootstrap)**: Verify Claude Code is installed, but only when Phase 2 will actually run (no `--skip-interactive`, and at least one `install.configs` key without a value or `default`). Otherwise Claude Code is not needed.
2. **Phase 1 (Mechanical)**: Directories, git clone, venv, npm/pnpm — haniel performs directly
3. **Phase 2 (Interactive)**: Secret collection, config choices — delegated to Claude Code
4. **Phase 3 (Finalize)**: Config file generation, service registration (WinSW on Windows, systemd on Linux, a LaunchAgent on macOS)

`haniel install --dry-run` prints the plan. `--skip-interactive` skips Phase 2; a key without a `default` then stops Phase 3 with "Not all required configs are filled". If an `install.state` file from an earlier run exists, a terminal asks whether to start fresh or resume; without a terminal (or with `--skip-interactive`) pass `--state-policy resume` or `--state-policy start-fresh`. `--resume` is the old name of `--state-policy resume`.

### `install.requirements`

```yaml
install:
  requirements:
    python: ">=3.11"
    node: ">=18"
    winsw: true
    claude-code: true
```

System prerequisites. `claude-code` is verified in Phase 0, and only when the interactive phase will run (see above). Others are verified in Phase 1; failures are recorded and Claude Code guides the user through resolution in Phase 2. `winsw` is checked on Windows only and skipped elsewhere.

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
        { "mcpServers": { "haniel": { "url": "http://localhost:3200/mcp/" } } }
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
| `validate` | string | Validation command, run with `{value}` replaced (see below). Non-zero exit = invalid |
| `default` | string | Default value. `{root}` is substituted with haniel.yaml's directory |
| `description` | string | Human-readable description for AI-assisted setup |

If the config file already exists, existing values are preserved; only new keys appear as missing.

**`validate` command.** It runs through the platform shell (`sh -c` on POSIX, `cmd` on Windows) in the folder of `haniel.yaml`, with a 60-second limit, and a non-zero exit code means the value is invalid. It runs at two points:

- When a value is set in the interactive phase. A rejected value is not stored and Claude Code is told why, so it asks again.
- Right before the config files are written in Phase 3, for **every** value the file will contain: values from the interactive phase, `default` values, and values read from an existing file. This is why the same command can run twice for one interactive value. If any value fails, nothing is written, the install stops with exit code 1 and records a `config-generation` failure; fix the value and run `haniel install --state-policy resume`.

A failure message names `<config>.<key>`, the command as written in `haniel.yaml` (without the value), the exit code or "timed out after 60s", and the last 5 lines of output.

`{value}` is replaced by the value as is. It is **not** quoted or escaped, so that `'Authorization: Bearer {value}'` works as written. A value that contains `'`, `"`, `$`, a backtick, or a space can therefore break the command or change what it does. Write the command with that in mind and keep it to values you trust.

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
| `name` | string | *required* | Service name (the Windows service, the systemd unit `<name>.service`, or the LaunchAgent `com.haniel.<name>`) |
| `display` | string | — | Display name (Windows Services panel, systemd `Description`) |
| `working_directory` | string | `{root}` | Working directory. `{root}` = haniel.yaml location |
| `service_account` | object | — | Run under a specific user account (default: LocalSystem) |
| `environment` | map | — | Environment variables injected into the service |

**`service_account` fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `username` | string | *required* | Account name (e.g. `.\\username` for local accounts) |
| `password` | string | — | Account password |
| `allow_service_logon` | bool | `true` | Grant "Log on as a service" right to the account |

Registered in Phase 3:

- **Windows**: a service via WinSW. Start it with `sc start <name>`.
- **Linux**: a systemd unit `/etc/systemd/system/<name>.service` (needs root), followed by `systemctl daemon-reload` and `systemctl enable`. The unit has `User=` set to the user who invoked `sudo` (`SUDO_USER`), or to the installing user, plus `WorkingDirectory`, `Restart=always`, and any `environment` entries. With a `self:` block `ExecStart` is `/bin/bash <self repo>/haniel-runner.sh <root>/haniel-runner.conf` and the installer writes that `haniel-runner.conf`; it stops before writing the unit when the wrapper script or its release helper is missing. Without `self:` it is `<haniel> run haniel.yaml`. Without permission to write the unit the installer prints the unit text and the commands instead. Start it with `sudo systemctl start <name>`; the installer does not.
- **macOS**: a user LaunchAgent, not loaded until you run `launchctl bootstrap` (see the README).

`service_account` applies to Windows only.
