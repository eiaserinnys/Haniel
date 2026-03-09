# 3. Directory Structure Convention

## Status

Accepted (2026-03-09)

## Context

haniel is a service runner that manages git repositories and processes. It also manages
itself via a self-update mechanism (ADR-0002). The bootstrap script (`install-haniel.ps1`)
sets up the directory structure on a fresh machine.

The previous structure placed the haniel git repo at the installation root. This mixed
haniel's own source files with service data, and required a per-service subdirectory
(`.services/{name}/`) each with its own `haniel.yaml`. Adding a new service meant
re-running the bootstrap script.

### Problems with the previous structure

1. **Cluttered root**: haniel source files (`src/`, `tests/`, `pyproject.toml`) are
   visible at the top level alongside runtime data.

2. **Per-service configs**: Each service got its own `haniel.yaml` in `.services/{name}/`,
   implying separate haniel instances. This contradicts haniel's design as a single process
   managing multiple services.

3. **Adding services requires re-bootstrapping**: Instead of simply editing `haniel.yaml`
   and reloading, users had to re-run `install-haniel.ps1` with a new config URL.

4. **Log collision risk**: A shared `logs/` directory at root could cause naming conflicts
   between services from different configs.

## Decision

Adopt a convention where the installation root is a clean working directory, haniel's own
repo is isolated in `.self/`, and managed services are cloned into `.services/`:

```
{root}/                          # Clean working directory
+-- haniel.yaml                  # Single config for all services
+-- haniel-runner.conf           # Generated wrapper config
+-- {service-name}.exe           # WinSW service executable
+-- {service-name}.xml           # WinSW service config
+-- bin/
|   +-- winsw.exe
+-- .self/                       # haniel's own git repo
|   +-- .venv/                   # haniel's Python venv
|   +-- src/haniel/...
|   +-- haniel-runner.ps1        # Wrapper script (lives in repo)
|   +-- logs/                    # haniel's own operational logs
+-- .services/
    +-- some-service-a/          # git clone of soulstream
    |   +-- logs/                # stdout/stderr logs for soulstream services
    +-- some-service-b/          # git clone of seosoyoung
        +-- logs/                # stdout/stderr logs for seosoyoung services
```

### Key conventions

**One config to rule them all**: A single `haniel.yaml` at root defines all repos and
services. Adding a service = edit YAML + restart haniel.

**`.self/` for haniel itself**: The haniel git repo is cloned here. The `.venv/` for
haniel lives inside. Self-update (ADR-0002) operates on this directory.

**`.services/{name}/` per managed repo**: Each repo from the `repos` section (except
the `self` repo) is cloned here. Service logs go into `{repo}/logs/` to avoid cross-service
naming conflicts.

**WinSW files at root**: The service executable, XML config, and `haniel-runner.conf`
live at root alongside `haniel.yaml`. This is WinSW's requirement — the `.exe` and `.xml`
must share a directory.

### Path resolution in haniel-runner.ps1

The wrapper script resolves paths relative to its **working directory** (set by WinSW),
not relative to the script file location. This is necessary because the script lives in
`.self/` but operates on the root.

```powershell
# Before: relative to script location (breaks with .self/)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# After: relative to working directory (set by WinSW to root)
$RootDir = $PWD.Path
```

### haniel.yaml path convention

```yaml
repos:
  haniel:
    path: ./.self                    # Self-update repo
  soulstream:
    path: ./.services/some-service-a # Managed service repo
```

## Consequences

### Positive

- Root directory is clean — only config and WinSW files visible
- Single haniel.yaml manages everything; adding services is a YAML edit
- Per-repo log directories eliminate naming conflicts
- haniel source code is hidden in `.self/`, reducing confusion

### Negative

- The bootstrap script must create the structure rather than relying on git clone
  to produce the root directory
- `haniel-runner.ps1` must use CWD-relative paths, coupling it to WinSW's
  working directory setting
