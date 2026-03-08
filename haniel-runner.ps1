# haniel-runner.ps1 - Outer loop wrapper for haniel self-update
# See ADR-0002 for architecture details.
#
# This script is registered as the WinSW service, not haniel directly.
# It handles: git fetch → git reset --hard → pip install → launch haniel.
# Exit code 10 from haniel means "self-update approved" → loop again.

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$ErrorActionPreference = "Continue"

# Load configuration from haniel-runner.conf
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfPath = Join-Path $ScriptDir "haniel-runner.conf"

if (-not (Test-Path $ConfPath)) {
    Write-Error "Configuration file not found: $ConfPath"
    exit 1
}

# Parse key=value config file
$Config = @{}
Get-Content $ConfPath | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $Config[$parts[0].Trim()] = $parts[1].Trim()
        }
    }
}

$WebhookUrl = $Config["WEBHOOK_URL"]
$HanielRepo = $Config["HANIEL_REPO"]
$ConfigFile = $Config["CONFIG"]
$MaxGitFailures = [int]($Config["MAX_GIT_FAILURES"])
if (-not $MaxGitFailures) { $MaxGitFailures = 3 }

if (-not $HanielRepo -or -not $ConfigFile) {
    Write-Error "HANIEL_REPO and CONFIG must be set in haniel-runner.conf"
    exit 1
}

# Resolve paths relative to script directory
$RepoPath = Join-Path $ScriptDir $HanielRepo
$ConfigPath = Join-Path $ScriptDir $ConfigFile

function Send-Webhook {
    param([string]$Message, [string]$Level = "info")

    if (-not $WebhookUrl) { return }

    $emoji = switch ($Level) {
        "error"   { ":rotating_light:" }
        "warning" { ":warning:" }
        default   { ":information_source:" }
    }

    $body = @{
        text = "$emoji *haniel-runner*: $Message"
    } | ConvertTo-Json -Compress

    try {
        Invoke-RestMethod -Uri $WebhookUrl -Method Post -Body $body `
            -ContentType "application/json" -TimeoutSec 10 | Out-Null
    } catch {
        Write-Warning "Webhook failed: $_"
    }
}

function Update-HanielRepo {
    $gitFailures = 0

    # git fetch
    while ($gitFailures -lt $MaxGitFailures) {
        try {
            $fetchResult = & git -C $RepoPath fetch origin 2>&1
            if ($LASTEXITCODE -eq 0) {
                break
            }
            $gitFailures++
            Write-Warning "git fetch failed (attempt $gitFailures/$MaxGitFailures): $fetchResult"
            Start-Sleep -Seconds 5
        } catch {
            $gitFailures++
            Write-Warning "git fetch exception (attempt $gitFailures/$MaxGitFailures): $_"
            Start-Sleep -Seconds 5
        }
    }

    if ($gitFailures -ge $MaxGitFailures) {
        Send-Webhook "git fetch failed $MaxGitFailures times. Launching with current code." "error"
        return $false
    }

    # git reset --hard origin/main
    try {
        $branch = & git -C $RepoPath rev-parse --abbrev-ref HEAD 2>&1
        if ($LASTEXITCODE -ne 0) { $branch = "main" }
        & git -C $RepoPath reset --hard "origin/$branch" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Send-Webhook "git reset --hard failed. Launching with current code." "warning"
            return $false
        }
    } catch {
        Send-Webhook "git reset failed: $_" "warning"
        return $false
    }

    # pip install -e .
    try {
        & pip install -e $RepoPath 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Send-Webhook "pip install failed. Attempting to launch with previous code." "warning"
        }
    } catch {
        Send-Webhook "pip install exception: $_" "warning"
    }

    return $true
}

# Validate repo path exists
if (-not (Test-Path $RepoPath)) {
    Send-Webhook "Haniel repo not found at $RepoPath" "error"
    Write-Error "Repo not found: $RepoPath"
    exit 1
}

# Main loop
$EXIT_SELF_UPDATE = 10

while ($true) {
    Write-Host "[haniel-runner] Updating haniel repository..."
    Update-HanielRepo | Out-Null

    Write-Host "[haniel-runner] Launching haniel..."
    & python -m haniel.cli run $ConfigPath
    $exitCode = $LASTEXITCODE

    Write-Host "[haniel-runner] haniel exited with code: $exitCode"

    if ($exitCode -eq 0) {
        # Clean shutdown (sc stop)
        Write-Host "[haniel-runner] Clean shutdown. Exiting wrapper."
        exit 0
    }
    elseif ($exitCode -eq $EXIT_SELF_UPDATE) {
        # Self-update approved — loop again to fetch + reinstall + restart
        Write-Host "[haniel-runner] Self-update requested. Looping..."
        Send-Webhook "Self-update initiated. Updating and restarting..." "info"
        Start-Sleep -Seconds 5  # Prevent tight loops
    }
    else {
        # Crash or unexpected exit — let WinSW onfailure handle it
        Write-Host "[haniel-runner] Unexpected exit code $exitCode. Exiting wrapper."
        Send-Webhook "haniel exited with unexpected code $exitCode." "error"
        exit $exitCode
    }
}
