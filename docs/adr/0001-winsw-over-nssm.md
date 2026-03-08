# 1. WinSW over NSSM for Windows Service Management

## Status

Accepted (2026-03-08)

## Context

haniel uses a Windows service wrapper to register itself as a Windows service.
The original choice was NSSM (Non-Sucking Service Manager), but NSSM has a critical
problem for our use case: it pops up an ACL GUI dialog during service registration.

This GUI popup blocks non-interactive automation entirely. Since haniel's installation
is designed to be driven by Claude Code (an AI coding agent), any GUI interaction is
a hard blocker.

### Alternatives Considered

**pywin32 ServiceFramework**: Python-native Windows service support. Rejected because
pywin32's ServiceFramework has an official, unresolved incompatibility with Python
virtual environments (https://github.com/mhammond/pywin32/issues/1450). haniel's core
installation pattern creates venvs for managed services, so depending on a tool that
breaks in venvs is unacceptable.

**WinSW v2.12.0 (self-contained)**: A single 18MB exe that uses XML config files.
Fully CLI-automatable with `--no-elevate` flag (gives clear error instead of UAC popup
if not admin). MIT licensed.

## Decision

Use WinSW v2.12.0 self-contained exe as the Windows service wrapper.

Key design choices:
- **Self-contained build** (not .NET-dependent) to minimize host requirements
- **`--no-elevate` flag** on all WinSW commands to prevent UAC popups
- **XML config co-located** with service exe: `{name}.exe` + `{name}.xml`
- **`bin/winsw.exe`** as canonical location, discovered by walking up from config_dir
- **`install-haniel.ps1`** downloads WinSW and enforces admin privileges at script start

## Consequences

### Gained

- Fully automatable service registration — no GUI, no user interaction needed
- Single-file deployment — no installer, no registry modifications beyond service registration
- XML config is human-readable and version-controllable
- `sc start/stop` commands work natively (WinSW registers standard Windows services)

### Lost

- NSSM's virtual service account support (WinSW runs as LocalSystem by default)
- NSSM's I/O redirection features (haniel handles its own log capture)
- Community familiarity — NSSM is more widely known

### Accepted Limitations

- WinSW v2.12.0 is the last stable release; v3.x exists but is less proven
- 18MB binary size for the self-contained build (acceptable for a service runner)
- XML generation via string concatenation — works with proper escaping but is fragile;
  a future improvement could use `xml.etree.ElementTree`
