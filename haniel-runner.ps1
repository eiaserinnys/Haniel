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
$ConfigFile = $Config["CONFIG"]
$MaxGitFailures = [int]($Config["MAX_GIT_FAILURES"])
if (-not $MaxGitFailures) { $MaxGitFailures = 3 }

if (-not $HanielRepo -or -not $ConfigFile) {
    Write-Error "HANIEL_REPO and CONFIG must be set in haniel-runner.conf"
    exit 1
}

# Resolve paths relative to working directory
$RepoPath = Join-Path $RootDir $HanielRepo
$ConfigPath = Join-Path $RootDir $ConfigFile

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

function Add-Step {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$ErrorMessage = $null
    )
    if ([string]::IsNullOrWhiteSpace($ErrorMessage) -and -not $Ok) {
        $ErrorMessage = "$Name failed (no message)"
    }
    $step = [ordered]@{
        name  = $Name
        ok    = $Ok
        error = $ErrorMessage
    }
    [void]$script:LastUpdateSteps.Add($step)
    if (-not $Ok -and -not $script:LastUpdateError) {
        $script:LastUpdateError = "$Name failed: $ErrorMessage"
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
            steps       = @($script:LastUpdateSteps)
            error       = $script:LastUpdateError
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

function Update-HanielRepo {
    $gitFailures = 0

    # ── git fetch ──────────────────────────────────────────
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
        Add-Step -Name "git_fetch" -Ok $false -ErrorMessage "git fetch failed $MaxGitFailures times"
        Send-Webhook "git fetch failed $MaxGitFailures times. Launching with current code." "error"
        return $false
    }
    Add-Step -Name "git_fetch" -Ok $true

    # ── git reset --hard origin/<branch> ───────────────────
    try {
        $branch = & git -C $RepoPath rev-parse --abbrev-ref HEAD 2>&1
        if ($LASTEXITCODE -ne 0) { $branch = "main" }
        $resetOutput = & git -C $RepoPath reset --hard "origin/$branch" 2>&1
        if ($LASTEXITCODE -ne 0) {
            $msg = ($resetOutput | Out-String).Trim()
            if ([string]::IsNullOrWhiteSpace($msg)) {
                $msg = "git reset exited with code $LASTEXITCODE"
            }
            Add-Step -Name "git_reset" -Ok $false -ErrorMessage $msg
            Send-Webhook "git reset --hard failed. Launching with current code." "warning"
            return $false
        }
        Add-Step -Name "git_reset" -Ok $true
    } catch {
        Add-Step -Name "git_reset" -Ok $false -ErrorMessage "$_"
        Send-Webhook "git reset failed: $_" "warning"
        return $false
    }

    # ── pip install -e . ───────────────────────────────────
    try {
        $pipOutput = & pip install -e $RepoPath 2>&1
        if ($LASTEXITCODE -ne 0) {
            $msg = (($pipOutput | Select-Object -Last 20) -join "`n")
            if ([string]::IsNullOrWhiteSpace($msg)) {
                $msg = "pip install exited with code $LASTEXITCODE"
            }
            Add-Step -Name "pip_install" -Ok $false -ErrorMessage $msg
            Send-Webhook "pip install failed. Attempting to launch with previous code." "warning"
        } else {
            Add-Step -Name "pip_install" -Ok $true
        }
    } catch {
        Add-Step -Name "pip_install" -Ok $false -ErrorMessage "$_"
        Send-Webhook "pip install exception: $_" "warning"
    }

    # ── pnpm install + build dashboard (optional) ──────────
    # 두 단계는 try/catch를 분리하여, 예외 시 정확한 단계 이름을 기록한다.
    $DashboardPath = Join-Path $RepoPath "dashboard"
    if (Test-Path $DashboardPath) {
        Write-Host "[haniel-runner] Building dashboard..."
        $installOk = $false
        try {
            $pnpmInstallOutput = & pnpm --dir $DashboardPath install 2>&1
            if ($LASTEXITCODE -ne 0) {
                $msg = (($pnpmInstallOutput | Select-Object -Last 20) -join "`n")
                if ([string]::IsNullOrWhiteSpace($msg)) { $msg = "pnpm install exited with code $LASTEXITCODE" }
                Add-Step -Name "pnpm_install" -Ok $false -ErrorMessage $msg
                Send-Webhook "Dashboard pnpm install failed. Launching with previous build." "warning"
            } else {
                Add-Step -Name "pnpm_install" -Ok $true
                $installOk = $true
            }
        } catch {
            Add-Step -Name "pnpm_install" -Ok $false -ErrorMessage "$_"
            Send-Webhook "Dashboard pnpm install exception: $_" "warning"
        }

        if ($installOk) {
            try {
                $pnpmBuildOutput = & pnpm --dir $DashboardPath build 2>&1
                if ($LASTEXITCODE -ne 0) {
                    $msg = (($pnpmBuildOutput | Select-Object -Last 20) -join "`n")
                    if ([string]::IsNullOrWhiteSpace($msg)) { $msg = "pnpm build exited with code $LASTEXITCODE" }
                    Add-Step -Name "pnpm_build" -Ok $false -ErrorMessage $msg
                    Send-Webhook "Dashboard build failed. Launching with previous build." "warning"
                } else {
                    Add-Step -Name "pnpm_build" -Ok $true
                }
            } catch {
                Add-Step -Name "pnpm_build" -Ok $false -ErrorMessage "$_"
                Send-Webhook "Dashboard build exception: $_" "warning"
            }
        }
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

while ($true) {
    if (-not $skipUpdate) {
        Write-Host "[haniel-runner] Updating haniel repository..."
        $script:LastUpdateSteps      = New-Object System.Collections.ArrayList
        $script:LastUpdateError      = $null
        $script:LastUpdateStartedAt  = (Get-Date).ToString('o')
        $script:LastUpdateFinishedAt = $null
        Update-HanielRepo | Out-Null
        $script:LastUpdateFinishedAt = (Get-Date).ToString('o')
        $updateOk = ($null -eq $script:LastUpdateError)
        Write-SelfUpdateMarker -Ok $updateOk
    }
    $skipUpdate = $false

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
