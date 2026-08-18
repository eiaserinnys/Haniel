# haniel-runner.ps1 - Outer loop wrapper for haniel self-update
# See ADR-0002 for architecture details.
#
# This script is registered as the WinSW service, not haniel directly.
# It handles: prepare release → atomic current.txt switch → launch haniel.
# Exit code 10 from haniel means "self-update approved" → loop again.

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$ErrorActionPreference = "Continue"

# Load configuration from haniel-runner.conf
# Paths are relative to working directory (set by WinSW), not script location.
# See ADR-0003 for directory structure.
$RootDir = $PWD.Path
$ConfPath = Join-Path $RootDir "haniel-runner.conf"

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
$HanielReleaseRoot = $Config["HANIEL_RELEASE_ROOT"]
if (-not $HanielReleaseRoot) { $HanielReleaseRoot = ".local/haniel-releases" }
$HanielReleaseRetainExtra = 3
$retainExtraRaw = $Config["HANIEL_RELEASE_RETAIN_EXTRA"]
if ($retainExtraRaw) {
    if (-not [int]::TryParse($retainExtraRaw, [ref]$HanielReleaseRetainExtra)) {
        Write-Error "HANIEL_RELEASE_RETAIN_EXTRA must be a non-negative integer"
        exit 1
    }
}
$HanielReleaseMinFreeMB = 5120
$minFreeRaw = $Config["HANIEL_RELEASE_MIN_FREE_MB"]
if ($minFreeRaw) {
    if (-not [int]::TryParse($minFreeRaw, [ref]$HanielReleaseMinFreeMB)) {
        Write-Error "HANIEL_RELEASE_MIN_FREE_MB must be a positive integer"
        exit 1
    }
}
if ($HanielReleaseRetainExtra -lt 0 -or $HanielReleaseMinFreeMB -lt 1) {
    Write-Error "Release retention must be non-negative and minimum free space must be positive"
    exit 1
}
$ConfigFile = $Config["CONFIG"]
$PythonBinConfig = $Config["PYTHON_BIN"]
$MaxGitFailures = [int]($Config["MAX_GIT_FAILURES"])
if (-not $MaxGitFailures) { $MaxGitFailures = 3 }

if (-not $HanielRepo -or -not $ConfigFile) {
    Write-Error "HANIEL_REPO and CONFIG must be set in haniel-runner.conf"
    exit 1
}

function Resolve-RunnerPath {
    param([string]$PathValue)
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RootDir $PathValue))
}

# Resolve paths relative to working directory
$RepoPath = Resolve-RunnerPath $HanielRepo
$ReleaseRoot = Resolve-RunnerPath $HanielReleaseRoot
$ConfigPath = Resolve-RunnerPath $ConfigFile
$RunnerScriptPath = (Resolve-Path $PSCommandPath).Path
$ReleaseHelper = Join-Path (Split-Path -Parent $RunnerScriptPath) "scripts/haniel_atomic_release.py"

function Resolve-BootstrapPython {
    if ($PythonBinConfig) {
        return Resolve-RunnerPath $PythonBinConfig
    }
    $repoPython = Join-Path $RepoPath ".venv/Scripts/python.exe"
    if (Test-Path $repoPython -PathType Leaf) {
        return $repoPython
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

$BootstrapPython = Resolve-BootstrapPython
if (-not $BootstrapPython -or -not (Test-Path $BootstrapPython -PathType Leaf)) {
    Write-Error "Python executable not found. Set PYTHON_BIN in haniel-runner.conf."
    exit 1
}
if (-not (Test-Path $ReleaseHelper -PathType Leaf)) {
    Write-Error "Atomic release helper not found: $ReleaseHelper"
    exit 1
}
$PreparationResultPath = Join-Path $RootDir ".local/haniel_release_preparation.json"
$SelfUpdateExitMarker = Join-Path $RootDir ".local/self_update_exit_requested"
$script:ActiveRepo = $RepoPath
$script:ActivePython = $BootstrapPython
$script:ActiveCommit = $null
$script:PreparationResult = $null

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

function Write-SelfUpdateMarker {
    param([bool]$Ok)
    $markerDir  = Join-Path $RootDir ".local"
    $markerPath = Join-Path $markerDir "self_update_result.json"
    try {
        if (-not (Test-Path $markerDir)) {
            New-Item -ItemType Directory -Path $markerDir -Force | Out-Null
        }
        $payload = [ordered]@{
            version     = 1
            started_at  = $script:LastUpdateStartedAt
            finished_at = $script:LastUpdateFinishedAt
            ok          = $Ok
            steps       = @($script:PreparationResult.steps)
            error       = $script:PreparationResult.error
        }
        $json = $payload | ConvertTo-Json -Depth 5
        # UTF-8 without BOM (Python json.loads tolerates BOM but cleaner without)
        [System.IO.File]::WriteAllText(
            $markerPath,
            $json,
            (New-Object System.Text.UTF8Encoding($false))
        )
    } catch {
        Write-Warning "Failed to write self-update marker: $_"
    }
}

function Update-HanielRelease {
    if (Test-Path $PreparationResultPath) {
        Remove-Item $PreparationResultPath -Force
    }
    $arguments = @(
        $ReleaseHelper,
        "--source", $RepoPath,
        "--release-root", $ReleaseRoot,
        "--bootstrap-python", $BootstrapPython,
        "--result-json", $PreparationResultPath,
        "--max-git-failures", "$MaxGitFailures",
        "--retain-extra", "$HanielReleaseRetainExtra",
        "--min-free-mb", "$HanielReleaseMinFreeMB"
    )
    $currentPointerPath = Join-Path $ReleaseRoot "current.txt"
    $legacyCurrentPath = Join-Path $ReleaseRoot "current"
    if (-not (Test-Path $currentPointerPath -PathType Leaf) -and
        -not (Test-Path $legacyCurrentPath) -and
        (Test-Path $SelfUpdateExitMarker)) {
        $arguments += "--prefer-orig-head"
    }
    & $BootstrapPython @arguments
    $helperExitCode = $LASTEXITCODE
    if (-not (Test-Path $PreparationResultPath -PathType Leaf)) {
        $script:PreparationResult = [pscustomobject]@{
            ok = $false
            switched = $false
            active_repo = $null
            active_python = $null
            active_commit = $null
            steps = @()
            error = "atomic release helper did not write a result"
            error_code = $null
            warnings = @()
        }
    } else {
        try {
            $script:PreparationResult = Get-Content $PreparationResultPath -Raw | ConvertFrom-Json
        } catch {
            $script:PreparationResult = [pscustomobject]@{
                ok = $false
                switched = $false
                active_repo = $null
                active_python = $null
                active_commit = $null
                steps = @()
                error = "invalid atomic release helper result: $_"
                error_code = $null
                warnings = @()
            }
        }
    }
    $script:ActiveRepo = $script:PreparationResult.active_repo
    $script:ActivePython = $script:PreparationResult.active_python
    $script:ActiveCommit = $script:PreparationResult.active_commit
    if (@($script:PreparationResult.warnings).Count -gt 0) {
        $cleanupWarnings = @($script:PreparationResult.warnings) -join " | "
        Send-Webhook "Atomic release cleanup warning: $cleanupWarnings" "warning"
    }
    if ($helperExitCode -ne 0 -or -not $script:PreparationResult.ok) {
        $level = if ($script:PreparationResult.error_code -eq "insufficient_disk_space") { "warning" } else { "error" }
        if ($script:ActiveRepo -and $script:ActivePython) {
            Send-Webhook "Atomic release preparation failed: $($script:PreparationResult.error). Launching previous release." $level
        } else {
            Send-Webhook "Atomic release preparation failed: $($script:PreparationResult.error). No validated release is available." $level
        }
        return $false
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
$EXIT_RESTART = 11
$skipUpdate = $false
$writeSelfUpdateMarker = $false

while ($true) {
    if (-not $skipUpdate) {
        Write-Host "[haniel-runner] Preparing atomic Haniel release..."
        $script:LastUpdateStartedAt  = (Get-Date).ToString('o')
        $script:LastUpdateFinishedAt = $null
        Update-HanielRelease | Out-Null
        $script:LastUpdateFinishedAt = (Get-Date).ToString('o')
        if ($writeSelfUpdateMarker) {
            Write-SelfUpdateMarker -Ok ([bool]$script:PreparationResult.ok)
        }
        $writeSelfUpdateMarker = $false

        if (-not $script:ActiveRepo -or -not $script:ActivePython -or
            -not $script:ActiveCommit -or
            -not (Test-Path $script:ActivePython -PathType Leaf)) {
            $message = "No previously validated Haniel release is available; refusing to launch."
            Write-Error $message
            Send-Webhook $message "error"
            exit 1
        }

        $activeRunner = Join-Path $script:ActiveRepo "haniel-runner.ps1"
        if ($script:PreparationResult.switched -and (Test-Path $activeRunner -PathType Leaf)) {
            $activeRunnerPath = (Resolve-Path $activeRunner).Path
            if ($RunnerScriptPath -ne $activeRunnerPath) {
                Write-Host "[haniel-runner] Re-executing current release wrapper."
                & $activeRunner
                exit $LASTEXITCODE
            }
        }
    }
    $skipUpdate = $false

    Write-Host "[haniel-runner] Launching haniel..."
    $env:HANIEL_ACTIVE_SELF_HEAD = $script:ActiveCommit
    $runArguments = @("-m", "haniel.cli", "run", $ConfigPath)
    & $script:ActivePython @runArguments
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
        $writeSelfUpdateMarker = $true
        Start-Sleep -Seconds 5
    }
    elseif ($exitCode -eq $EXIT_RESTART) {
        # Restart requested — loop again without update
        Write-Host "[haniel-runner] Restart requested. Skipping update..."
        Send-Webhook "Restart initiated (no update)." "info"
        $skipUpdate = $true
        Start-Sleep -Seconds 3
    }
    else {
        # Crash or unexpected exit — let WinSW onfailure handle it
        Write-Host "[haniel-runner] Unexpected exit code $exitCode. Exiting wrapper."
        Send-Webhook "haniel exited with unexpected code $exitCode." "error"
        exit $exitCode
    }
}
