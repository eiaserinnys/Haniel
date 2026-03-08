<#
.SYNOPSIS
    Haniel bootstrapper for Windows.

.DESCRIPTION
    Bootstraps haniel on a fresh Windows machine with a single command:

        irm https://raw.githubusercontent.com/eiaserinnys/Haniel/main/install-haniel.ps1 | iex

    The script handles prerequisite installation (Git, Python, WinSW),
    clones the haniel repository, downloads a service config file,
    and delegates all environment setup to `haniel install`.

.EXAMPLE
    # One-liner (downloads and runs)
    irm https://raw.githubusercontent.com/eiaserinnys/Haniel/main/install-haniel.ps1 | iex

.EXAMPLE
    # Direct execution with parameters
    .\install-haniel.ps1 -InstallPath "D:\Services\Haniel" -ConfigUrl "https://raw.githubusercontent.com/.../seosoyoung.yaml"

.NOTES
    Requires PowerShell 5.1+.
    Requires winget for automatic Git/Python installation.
    Administrator privileges required for service registration and PATH modification.
#>

[CmdletBinding()]
param(
    [Parameter(HelpMessage = "Haniel clone path")]
    [string]$InstallPath,

    [Parameter(HelpMessage = "Service config file raw URL")]
    [string]$ConfigUrl,

    [Parameter(HelpMessage = "Skip Git installation check")]
    [switch]$SkipGitCheck,

    [Parameter(HelpMessage = "Skip Python installation check")]
    [switch]$SkipPythonCheck
)

$ErrorActionPreference = "Stop"

# ============================================================
# Output helpers
# ============================================================

function Write-Header($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step($step, $text) {
    Write-Host ""
    Write-Host "[$step] $text" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Success($text) {
    Write-Host "  [OK] $text" -ForegroundColor Green
}

function Write-Warn($text) {
    Write-Host "  [!] $text" -ForegroundColor Yellow
}

function Write-Fail($text) {
    Write-Host "  [X] $text" -ForegroundColor Red
}

function Write-Info($text) {
    Write-Host "      $text" -ForegroundColor Gray
}

function Update-SessionPath {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
}

# ============================================================
# Prerequisite checks
# ============================================================

function Test-Administrator {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentUser)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-Git {
    try {
        $version = & git --version 2>&1
        if ($version -match "git version") {
            Write-Success "Git found: $version"
            return $true
        }
    }
    catch { }
    Write-Warn "Git not found in PATH"
    return $false
}

function Install-Git {
    Write-Info "Installing Git via winget..."
    & winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements 2>&1 | ForEach-Object { Write-Info $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install Git via winget"
        Write-Info "Please install Git manually from https://git-scm.com"
        exit 1
    }
    Update-SessionPath
    Write-Success "Git installed"
}

function Test-Python {
    try {
        $version = & python --version 2>&1
        if ($version -match "Python (\d+)\.(\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 11) {
                Write-Success "Python $($Matches[0]) found"
                return $true
            }
            else {
                Write-Warn "Python version too old: $($Matches[0]) (need 3.11+)"
                return $false
            }
        }
    }
    catch { }
    Write-Warn "Python not found in PATH"
    return $false
}

function Install-Python {
    Write-Info "Installing Python 3.13 via winget..."
    & winget install --id Python.Python.3.13 -e --source winget --accept-source-agreements --accept-package-agreements 2>&1 | ForEach-Object { Write-Info $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install Python via winget"
        Write-Info "Please install Python manually from https://python.org"
        exit 1
    }
    Update-SessionPath
    Write-Success "Python installed"
}

# WinSW constants
$script:WINSW_VERSION = "v2.12.0"
$script:WINSW_URL = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"

function Install-WinSW {
    $binDir = Join-Path $InstallPath "bin"
    $dest = Join-Path $binDir "winsw.exe"

    if (Test-Path $dest) {
        Write-Success "WinSW already exists at $dest"
        return $true
    }

    Write-Info "Downloading WinSW $script:WINSW_VERSION..."

    try {
        New-Item -ItemType Directory -Path $binDir -Force | Out-Null
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $script:WINSW_URL -OutFile $dest -UseBasicParsing

        if (-not (Test-Path $dest)) {
            Write-Fail "Failed to download WinSW"
            return $false
        }

        Write-Success "WinSW $script:WINSW_VERSION downloaded to $dest"
        return $true
    }
    catch {
        Write-Fail "Failed to download WinSW: $($_.Exception.Message)"
        return $false
    }
}

# ============================================================
# Main
# ============================================================

function Main {
    Write-Host ""
    Write-Host "  _   _             _      _ " -ForegroundColor Magenta
    Write-Host " | | | | __ _ _ __ (_) ___| |" -ForegroundColor Magenta
    Write-Host " | |_| |/ _`` | '_ \| |/ _ \ |" -ForegroundColor Magenta
    Write-Host " |  _  | (_| | | | | |  __/ |" -ForegroundColor Magenta
    Write-Host " |_| |_|\__,_|_| |_|_|\___|_|" -ForegroundColor Magenta
    Write-Host ""
    Write-Host "  Bootstrap Installer" -ForegroundColor Gray
    Write-Host ""

    if (-not (Test-Administrator)) {
        Write-Fail "Administrator privileges required."
        Write-Info "Service registration and PATH modification need elevated permissions."
        Write-Info "Right-click PowerShell -> 'Run as Administrator' and try again."
        exit 1
    }

    # --------------------------------------------------------
    # Step 0: Git
    # --------------------------------------------------------
    Write-Step "0/5" "Git"

    if (-not $SkipGitCheck) {
        if (-not (Test-Git)) {
            $answer = Read-Host "  Install Git via winget? (Y/n)"
            if ($answer -ne "n" -and $answer -ne "N") {
                Install-Git
                if (-not (Test-Git)) {
                    Write-Fail "Git still not available after installation."
                    Write-Info "Please restart your terminal and try again."
                    exit 1
                }
            }
            else {
                Write-Fail "Git is required. Please install it and try again."
                exit 1
            }
        }
    }
    else {
        Write-Info "Skipping Git check"
    }

    # --------------------------------------------------------
    # Step 1: Python
    # --------------------------------------------------------
    Write-Step "1/5" "Python"

    if (-not $SkipPythonCheck) {
        if (-not (Test-Python)) {
            $answer = Read-Host "  Install Python 3.13 via winget? (Y/n)"
            if ($answer -ne "n" -and $answer -ne "N") {
                Install-Python
                if (-not (Test-Python)) {
                    Write-Fail "Python still not available after installation."
                    Write-Info "Please restart your terminal and try again."
                    exit 1
                }
            }
            else {
                Write-Fail "Python 3.11+ is required. Please install it and try again."
                exit 1
            }
        }
    }
    else {
        Write-Info "Skipping Python check"
    }

    # --------------------------------------------------------
    # Step 2: WinSW (Service Wrapper)
    # --------------------------------------------------------
    Write-Step "2/5" "WinSW (Service Wrapper)"

    $ok = Install-WinSW
    if (-not $ok) {
        Write-Fail "WinSW is required for Windows service registration."
        Write-Info "You can download it manually from https://github.com/winsw/winsw/releases"
        exit 1
    }

    # --------------------------------------------------------
    # Step 3: Install path + clone haniel
    # --------------------------------------------------------
    Write-Step "3/5" "Clone Haniel"

    if ([string]::IsNullOrWhiteSpace($InstallPath)) {
        $defaultPath = "C:\Services\Haniel"
        $InstallPath = Read-Host "  Install path [$defaultPath]"
        if ([string]::IsNullOrWhiteSpace($InstallPath)) {
            $InstallPath = $defaultPath
        }
    }

    if (Test-Path (Join-Path $InstallPath ".git")) {
        Write-Info "Haniel repository already exists at $InstallPath"
        Write-Info "Pulling latest changes..."
        & git -C $InstallPath pull --ff-only 2>&1 | ForEach-Object { Write-Info $_ }
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "git pull failed (exit code $LASTEXITCODE). Continuing with existing state."
            Write-Info "You may want to resolve this manually: git -C $InstallPath status"
        }
    }
    else {
        if (Test-Path $InstallPath) {
            # Directory exists but is not a git repo — check if empty
            $contents = Get-ChildItem -Path $InstallPath -Force
            if ($contents.Count -gt 0) {
                Write-Fail "Directory $InstallPath exists and is not empty."
                Write-Info "Please choose a different path or remove the existing directory."
                exit 1
            }
            # Empty directory — remove so git clone can create it
            Remove-Item -Path $InstallPath -Force -Recurse
        }

        Write-Info "Cloning haniel to $InstallPath..."
        & git clone https://github.com/eiaserinnys/Haniel.git $InstallPath 2>&1 | ForEach-Object { Write-Info $_ }
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "Failed to clone haniel repository"
            exit 1
        }
    }

    Write-Success "Haniel repository ready at $InstallPath"

    # Create venv + editable install
    $venvPath = Join-Path $InstallPath ".venv"
    $pipExe = Join-Path $venvPath "Scripts\pip.exe"
    $hanielExe = Join-Path $venvPath "Scripts\haniel.exe"

    if (-not (Test-Path $venvPath)) {
        Write-Info "Creating virtual environment..."
        & python -m venv $venvPath
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "Failed to create virtual environment"
            exit 1
        }
    }

    Write-Info "Installing haniel (editable)..."
    & $pipExe install -e $InstallPath 2>&1 | ForEach-Object { Write-Info $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install haniel"
        exit 1
    }

    Write-Success "haniel installed"

    # --------------------------------------------------------
    # Step 4: Download config file
    # --------------------------------------------------------
    Write-Step "4/5" "Service Configuration"

    if ([string]::IsNullOrWhiteSpace($ConfigUrl)) {
        $ConfigUrl = Read-Host "  Config file URL (e.g. https://raw.githubusercontent.com/.../seosoyoung.yaml)"
        if ([string]::IsNullOrWhiteSpace($ConfigUrl)) {
            Write-Fail "Config URL is required."
            exit 1
        }
    }

    # Validate HTTPS
    if ($ConfigUrl -notmatch '^https://') {
        Write-Fail "Config URL must use HTTPS."
        exit 1
    }

    # Extract service name from URL filename (strip query string first)
    $uri = [System.Uri]$ConfigUrl
    $fileName = [System.IO.Path]::GetFileName($uri.AbsolutePath)
    $serviceName = [System.IO.Path]::GetFileNameWithoutExtension($fileName)

    if ($serviceName -notmatch '^[a-zA-Z0-9_-]+$') {
        Write-Fail "Invalid service name derived from URL: '$serviceName'"
        Write-Info "Service name must contain only letters, numbers, hyphens, and underscores."
        exit 1
    }
    $serviceDir = Join-Path $InstallPath ".services" $serviceName

    Write-Info "Service name : $serviceName"
    Write-Info "Config dir   : $serviceDir"

    New-Item -ItemType Directory -Path $serviceDir -Force | Out-Null

    $configPath = Join-Path $serviceDir $fileName
    Write-Info "Downloading $ConfigUrl..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $ConfigUrl -OutFile $configPath -UseBasicParsing
    }
    catch {
        Write-Fail "Failed to download config: $($_.Exception.Message)"
        exit 1
    }

    if (-not (Test-Path $configPath)) {
        Write-Fail "Config file download failed."
        exit 1
    }

    Write-Success "Config saved to $configPath"

    # --------------------------------------------------------
    # Step 5: haniel install
    # --------------------------------------------------------
    Write-Step "5/5" "Running haniel install"

    Write-Info "Working directory: $serviceDir"
    Write-Info "Command: $hanielExe install $fileName"
    Write-Host ""

    Push-Location $serviceDir
    try {
        & $hanielExe install $fileName
        $installExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($installExitCode -ne 0) {
        Write-Fail "haniel install exited with code $installExitCode"
        Write-Info "Check the output above for details."
        Write-Info "You can re-run: $hanielExe install $fileName"
        Write-Info "  (from directory: $serviceDir)"
        exit 1
    }

    # --------------------------------------------------------
    # Done
    # --------------------------------------------------------
    Write-Header "Installation Complete"

    Write-Host "  Service '$serviceName' has been set up at:" -ForegroundColor Green
    Write-Host "    $serviceDir" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Haniel commands:" -ForegroundColor Yellow
    Write-Host "    Start service : sc start $serviceName" -ForegroundColor White
    Write-Host "    Stop service  : sc stop $serviceName" -ForegroundColor White
    Write-Host "    Run manually  : $hanielExe run $fileName" -ForegroundColor White
    Write-Host "    Validate      : $hanielExe validate $fileName" -ForegroundColor White
    Write-Host ""
    Write-Host "  Config directory: $serviceDir" -ForegroundColor Gray
    Write-Host "  Haniel root     : $InstallPath" -ForegroundColor Gray
    Write-Host ""
}

# Run
Main
