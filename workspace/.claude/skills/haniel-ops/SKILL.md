---
name: haniel-ops
description: >
  Service management operations using Haniel MCP tools. Covers health checks, deploying updates,
  adding/removing services, reading logs, and troubleshooting.
  Use this skill whenever the user mentions service management, service status, health checks,
  deploying updates, restarting services, adding or removing services, reading logs, or
  troubleshooting service issues. Also triggers on: "서비스 관리", "서비스 상태", "서비스 추가",
  "서비스 삭제", "배포", "업데이트", "로그 확인", "헬스 체크", "health check", "deploy",
  "restart service", "add service", "remove service", "check updates", "pull repo".
  Even if the user doesn't use these exact phrases, if they're asking about anything related to
  running services, checking what's deployed, or why something isn't working — use this skill.
---

# Haniel Service Operations

This skill covers day-to-day service management through Haniel's MCP tools.
Every workflow follows one principle: **understand before you act**.
Read logs, check status, and confirm with the user before making changes.

## Safety Rules

These apply to every workflow below. Internalize them — don't treat them as a checklist.

1. **Investigate first.** Before restarting or changing anything, read the logs and understand the
   current state. A blind restart can mask the real problem or make things worse.

2. **Confirm before acting.** Any operation that stops, restarts, or removes a service needs the
   user's approval first. Explain what you're about to do and what the impact will be.

3. **Self-update and self-restart both end the session.** `haniel_update(service='haniel')` pulls
   code and restarts; `haniel_restart(service='haniel')` restarts without pulling. Both terminate
   this agent session. Always warn the user explicitly before doing either.

4. **Reload after config changes.** Any call to `create_service_config`, `update_service_config`,
   `delete_service_config`, `create_repo_config`, `update_repo_config`, or `delete_repo_config`
   modifies `haniel.yaml` but does NOT apply the changes. Call `haniel_reload` afterward to apply.

5. **Pulling a repo restarts its dependents.** `haniel_pull` triggers a restart of every service
   that depends on that repo. Before pulling, check which services will be affected and let the
   user know.

## Tool Reference

| Tool | What it does | Side effects |
|---|---|---|
| `haniel_check_updates` | Checks all repos for pending upstream changes | None (read-only) |
| `haniel_read_logs` | Reads service logs, optional grep filter | None (read-only) |
| `haniel_pull` | Pulls a repo and restarts dependent services | **Restarts services** |
| `haniel_update` | Pulls + restarts a single service (or Haniel itself) | **Restarts service** |
| `haniel_reload` | Reloads haniel.yaml without stopping anything | Applies config changes |
| `haniel_start` | Starts a stopped service | Starts process |
| `haniel_stop` | Stops a running service | **Stops process** |
| `haniel_restart` | Stops then starts a service | **Restarts process** |
| `haniel_enable` | Resets circuit breaker for a failed service | Re-enables auto-start |
| `haniel_create_service_config` | Adds a new service to haniel.yaml | Config change (needs reload) |
| `haniel_update_service_config` | Modifies an existing service in haniel.yaml | Config change (needs reload) |
| `haniel_delete_service_config` | Removes a service from haniel.yaml | Config change (needs reload) |
| `haniel_create_repo_config` | Adds a new repo to haniel.yaml | Config change (needs reload) |
| `haniel_update_repo_config` | Modifies an existing repo in haniel.yaml | Config change (needs reload) |
| `haniel_delete_repo_config` | Removes a repo from haniel.yaml | Config change (needs reload) |

## Config Reference

### Service config fields

```yaml
name:
  run: "python -m my_app"       # Required. The command to start the service.
  cwd: "./repos/my-app"         # Required. Working directory for the run command.
  repo: "my-repo"               # Optional. Links the service to a repo (for auto-deploy).
  after:                        # Optional. Services that must be running before this one starts.
    - other-service
  ready: "port:8080"            # Optional. How Haniel knows the service is up. See below.
  enabled: true                 # Optional. Set false to prevent Haniel from starting this service.
  hooks:
    post_pull: "pip install -r requirements.txt"  # Runs after git pull, before service restart.
    pre_start: null             # Runs just before the service process is launched.
  shutdown:
    signal: SIGTERM             # Signal sent to stop the process (default: SIGTERM).
    timeout: 15                 # Seconds to wait for graceful shutdown before escalating.
    method: null                # null or "http". If "http", sends a request to endpoint instead.
    endpoint: null              # HTTP endpoint to call for shutdown (if method is "http").
```

**`ready` condition types:**

| Format | Example | What it waits for |
|---|---|---|
| `port:N` | `port:8080` | TCP port N to accept connections |
| `delay:N` | `delay:5` | N seconds to pass after process starts |
| `log:pattern` | `log:Server started` | A matching string to appear in stdout |
| `http:url` | `http:http://localhost:8080/health` | The URL to return HTTP 200 |

If `ready` is omitted, Haniel considers the service ready immediately after process launch.

**`after` and startup ordering:**

Services listed in `after` must be in a running+ready state before this service starts.
Haniel uses topological sort (Kahn's algorithm) to determine the full startup order.
If a circular dependency is detected, Haniel will refuse to start the affected services.

### Repo config fields

```yaml
name:
  url: "https://github.com/org/repo"  # Required. Git remote URL.
  branch: "main"                       # Required. Branch to track.
  path: "./repos/my-repo"              # Required. Local path to clone into.
  pull_strategy: null                  # null (default merge) or "force" (git reset --hard origin/<branch>).
  hooks:
    post_pull: "pnpm install"          # Runs after every successful git pull for this repo.
```

**When to use `pull_strategy: "force"`:**

Use `force` only when the repo may accumulate local changes that would block a normal merge
(e.g. auto-generated files committed by the service itself, or a repo that is never edited locally).
For all other repos, leave `pull_strategy` as `null` — a failed merge will surface as an error
rather than silently discarding local work.

## Workflows

### 1. Health Check & Troubleshooting

When something seems wrong with a service, or the user asks you to check on things:

**Step 1 — Gather information (no user interaction needed)**

```
haniel_read_logs(service=<name>, lines=100)
```

If the user reported a specific error, use `grep` to narrow down:

```
haniel_read_logs(service=<name>, lines=200, grep="error")
```

**Step 2 — Diagnose**

Read the logs carefully. Look for:
- Repeated error patterns (crash loops)
- Timestamps — when did the problem start?
- Dependency failures — is this service failing because something else is down?

**Circuit breaker:** If a service crashes more than a configured number of times within a short
window, Haniel trips the circuit breaker and stops trying to restart it automatically. The service
will stay stopped until you reset it with `haniel_enable`. Always fix the root cause first —
resetting the circuit breaker without fixing the underlying problem just puts the service back into
a crash loop.

**Step 3 — Report and recommend**

Tell the user what you found in plain language, with the actual error in a blockquote.
Propose a fix. Common fixes:

- **Service crashed once** → `haniel_restart` (after user confirms)
- **Service in crash loop (circuit breaker tripped)** → Fix the root cause first, then `haniel_enable` + `haniel_start`
- **Config issue** → `haniel_update_service_config` + `haniel_reload`
- **Stale code** → `haniel_pull` or `haniel_update`

### 2. Deploying Updates

When the user wants to deploy the latest code:

**Step 1 — Check what's pending**

```
haniel_check_updates()
```

This shows which repos have upstream changes. Share the results with the user.

If `auto_apply` is `false` in haniel.yaml, Haniel detects changes but does not deploy them
automatically — they sit pending until manually triggered. `haniel_check_updates` will show
these as waiting. To deploy, proceed to Step 2.

**Step 2 — Pull and restart**

For a specific repo (restarts all services that depend on it):

```
haniel_pull(repo=<name>)
```

Tell the user which services will restart before doing this.

For a specific service only (pulls its repo and restarts just that service):

```
haniel_update(service=<name>)
```

**Step 3 — Verify**

After the update, read logs to confirm the service started cleanly:

```
haniel_read_logs(service=<name>, lines=50)
```

Look for startup messages and confirm there are no errors.

### 3. Adding a New Service

When the user wants to register a new service:

**Step 1 — Add the repo** (if it's not already registered)

Ask the user for the GitHub URL, the branch to track, and where to clone it.
If the repo contains generated or auto-committed files that could block merges, ask whether
`pull_strategy: "force"` is appropriate.

```
haniel_create_repo_config(name=<repo-name>, config={
  "url": "https://github.com/org/repo",
  "branch": "main",
  "path": "repos/<repo-name>",
  "pull_strategy": null,
  "hooks": {
    "post_pull": "pip install -r requirements.txt"  # omit if not needed
  }
})
```

**Step 2 — Add the service**

Ask the user for any details you don't have. Key questions:
- What command starts the service? (required)
- What is the working directory? (required)
- Does this service depend on other services being up first? (`after`)
- How do we know it's ready? (`ready` — port, delay, log pattern, or HTTP endpoint)
- Does the repo need any setup after a pull? (`hooks.post_pull`)

```
haniel_create_service_config(name=<service-name>, config={
  "repo": "<repo-name>",
  "run": "<start command>",
  "cwd": "<working directory>",
  "after": ["<dependency-service>"],      # omit if no dependencies
  "ready": "port:8080",                   # omit if no readiness check needed
  "hooks": {
    "post_pull": "<setup command>"        # omit if not needed
  }
})
```

**Step 3 — Apply and start**

```
haniel_reload()
haniel_start(service=<service-name>)
```

**Step 4 — Verify**

```
haniel_read_logs(service=<service-name>, lines=50)
```

Confirm the service started without errors. Report back to the user.

### 4. Removing a Service

When the user wants to remove a service:

**Step 1 — Stop the service**

```
haniel_stop(service=<service-name>)
```

Confirm with the user before stopping.

**Step 2 — Remove the service config**

```
haniel_delete_service_config(service=<service-name>)
```

**Step 3 — Optionally remove the repo config**

If no other services use the same repo, ask the user if they want to remove it too:

```
haniel_delete_repo_config(repo=<repo-name>)
```

**Step 4 — Apply**

```
haniel_reload()
```

### 5. Self-Update

When the user asks to update Haniel itself:

**Step 1 — Check if there's anything to update**

```
haniel_check_updates()
```

If there are no pending changes for the haniel repo, let the user know — a self-update
would just restart the agent for no reason.

**Step 2 — Warn the user:**
> Updating Haniel will restart the agent. This session will end immediately.
> Any work in progress will be interrupted. Are you sure?

Only proceed if the user explicitly confirms. Then:

```
haniel_update(service='haniel')
```

There is no step after this — the session terminates.
