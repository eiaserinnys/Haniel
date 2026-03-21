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

**Step 2 — Pull and restart**

For a specific repo:

```
haniel_pull(repo=<name>)
```

This pulls the repo AND restarts all services that depend on it.
Tell the user which services will restart before doing this.

For a specific service (pulls its repo and restarts just that service):

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

```
haniel_create_repo_config(name=<repo-name>, config={
  "url": "https://github.com/org/repo",
  "branch": "main",
  "path": "repos/<repo-name>"
})
```

**Step 2 — Add the service**

```
haniel_create_service_config(name=<service-name>, config={
  "repo": "<repo-name>",
  "run": "<start command>",
  "cwd": "<working directory>",
  "ready": "<readiness check if applicable>"
})
```

Ask the user for any details you don't have — the start command, working directory,
environment variables, dependencies on other services, etc.

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
