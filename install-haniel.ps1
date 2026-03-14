<#
.SYNOPSIS
    Haniel bootstrapper for Windows.

.DESCRIPTION
    Bootstraps haniel on a fresh Windows machine with a single command:

        irm https://raw.githubusercontent.com/eiaserinnys/haniel/main/install-haniel.ps1 | iex

    The script handles prerequisite installation (Git, Python, Node.js, WinSW),
    clones the haniel repository into .self/, downloads a haniel.yaml
    config, and delegates all environment setup to `haniel install`.

    See docs/adr/0003-directory-structure.md for the directory layout.

.EXAMPLE
    # One-liner (downloads and runs)
    irm https://raw.githubusercontent.com/eiaserinnys/haniel/main/install-haniel.ps1 | iex

.EXAMPLE
    # Direct execution with parameters
    .\install-haniel.ps1 -InstallPath "D:\Services\Haniel" -ConfigUrl "https://raw.githubusercontent.com/.../haniel.yaml"

.NOTES
    Requires PowerShell 5.1+.
    Requires winget for automatic Git/Python/Node.js installation.
    Administrator privileges required for service registration and PATH modification.
#>

[CmdletBinding()]
param(
    [Parameter(HelpMessage = "Haniel root directory")]
    [string]$InstallPath,

    [Parameter(HelpMessage = "haniel.yaml config file raw URL")]
    [string]$ConfigUrl,

    [Parameter(HelpMessage = "Skip Git installation check")]
    [switch]$SkipGitCheck,

    [Parameter(HelpMessage = "Skip Python installation check")]
    [switch]$SkipPythonCheck,

    [Parameter(HelpMessage = "Skip Node.js installation check")]
    [switch]$SkipNodeCheck
)

$ErrorActionPreference = "Stop"

# ============================================================
# Logging — all output goes to console AND log file
# ============================================================
$script:LogFile = Join-Path ([System.IO.Path]::GetTempPath()) "haniel-install.log"
"" | Out-File -FilePath $script:LogFile -Encoding utf8

function Write-Log($text) {
    $text | Out-File -FilePath $script:LogFile -Append -Encoding utf8
}

# ============================================================
# Output helpers
# ============================================================

function Write-Header($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Log "=== $text ==="
}

function Write-Step($step, $text) {
    Write-Host ""
    Write-Host "[$step] $text" -ForegroundColor Cyan
    Write-Host ""
    Write-Log "[$step] $text"
}

function Write-Success($text) {
    Write-Host "  [OK] $text" -ForegroundColor Green
    Write-Log "  [OK] $text"
}

function Write-Warn($text) {
    Write-Host "  [!] $text" -ForegroundColor Yellow
    Write-Log "  [!] $text"
}

function Write-Fail($text) {
    Write-Host "  [X] $text" -ForegroundColor Red
    Write-Log "  [X] $text"
}

function Invoke-NativeCommand {
    <#
    .SYNOPSIS
        Run an external command, streaming combined stdout/stderr through Write-Info.
        $ErrorActionPreference = "Stop" makes PowerShell throw on stderr ErrorRecords
        from native commands piped through 2>&1. We temporarily switch to "Continue"
        so that stderr lines are logged instead of aborting the script.
    #>
    param([scriptblock]$Command)
    $savedPref = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Command 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                Write-Info $_.Exception.Message
            } else {
                Write-Info $_
            }
        }
    } finally {
        $ErrorActionPreference = $savedPref
    }
}

function Write-Info($text) {
    Write-Host "      $text" -ForegroundColor Gray
    Write-Log "      $text"
}

function Exit-WithLog($code) {
    Write-Host ""
    Write-Host "  Log saved to: $script:LogFile" -ForegroundColor Yellow
    Write-Host "  Press any key to exit..." -ForegroundColor Gray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit $code
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
    Invoke-NativeCommand { winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install Git via winget"
        Write-Info "Please install Git manually from https://git-scm.com"
        Exit-WithLog 1
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
    Invoke-NativeCommand { winget install --id Python.Python.3.13 -e --source winget --accept-source-agreements --accept-package-agreements }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install Python via winget"
        Write-Info "Please install Python manually from https://python.org"
        Exit-WithLog 1
    }
    Update-SessionPath
    Write-Success "Python installed"
}

function Test-Node {
    try {
        $version = & node --version 2>&1
        if ($version -match "v(\d+)\.(\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            if ($major -ge 18) {
                Write-Success "Node.js $version found"
                return $true
            } else {
                Write-Warn "Node.js version too old: $version (need v18+)"
                return $false
            }
        }
    } catch { }
    Write-Warn "Node.js not found in PATH"
    return $false
}

function Install-Node {
    Write-Info "Installing Node.js LTS via winget..."
    & winget install --id OpenJS.NodeJS.LTS -e --source winget --accept-source-agreements --accept-package-agreements 2>&1 | ForEach-Object { Write-Info $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install Node.js via winget"
        Write-Info "Please install Node.js manually from https://nodejs.org"
        exit 1
    }
    Update-SessionPath
    Write-Success "Node.js installed"
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
        Exit-WithLog 1
    }

    # --------------------------------------------------------
    # Step 0: Git
    # --------------------------------------------------------
    Write-Step "0/8" "Git"

    if (-not $SkipGitCheck) {
        if (-not (Test-Git)) {
            $answer = Read-Host "  Install Git via winget? (Y/n)"
            if ($answer -ne "n" -and $answer -ne "N") {
                Install-Git
                if (-not (Test-Git)) {
                    Write-Fail "Git still not available after installation."
                    Write-Info "Please restart your terminal and try again."
                    Exit-WithLog 1
                }
            }
            else {
                Write-Fail "Git is required. Please install it and try again."
                Exit-WithLog 1
            }
        }
    }
    else {
        Write-Info "Skipping Git check"
    }

    # --------------------------------------------------------
    # Step 1: Python
    # --------------------------------------------------------
    Write-Step "1/8" "Python"

    if (-not $SkipPythonCheck) {
        if (-not (Test-Python)) {
            $answer = Read-Host "  Install Python 3.13 via winget? (Y/n)"
            if ($answer -ne "n" -and $answer -ne "N") {
                Install-Python
                if (-not (Test-Python)) {
                    Write-Fail "Python still not available after installation."
                    Write-Info "Please restart your terminal and try again."
                    Exit-WithLog 1
                }
            }
            else {
                Write-Fail "Python 3.11+ is required. Please install it and try again."
                Exit-WithLog 1
            }
        }
    }
    else {
        Write-Info "Skipping Python check"
    }

    # --------------------------------------------------------
    # Step 2: Node.js
    # --------------------------------------------------------
    Write-Step "2/8" "Node.js"

    if (-not $SkipNodeCheck) {
        if (-not (Test-Node)) {
            $answer = Read-Host "  Install Node.js LTS via winget? (Y/n)"
            if ($answer -ne "n" -and $answer -ne "N") {
                Install-Node
                if (-not (Test-Node)) {
                    Write-Fail "Node.js still not available after installation."
                    Write-Info "Please restart your terminal and try again."
                    exit 1
                }
            }
            else {
                Write-Fail "Node.js v18+ is required. Please install it and try again."
                exit 1
            }
        }
    }
    else {
        Write-Info "Skipping Node.js check"
    }

    # --------------------------------------------------------
    # Step 3: Claude Code auth
    # --------------------------------------------------------
    Write-Step "3/8" "Claude Code Auth"

    $claudePath = Get-Command claude -ErrorAction SilentlyContinue
    if ($claudePath) {
        try {
            $authJson = & claude auth status 2>&1 | ConvertFrom-Json
            if ($authJson.loggedIn) {
                Write-Success "Claude Code authenticated: $($authJson.email)"
            }
            else {
                Write-Warn "Claude Code is not authenticated."
                Write-Info "Running 'claude auth login'..."
                & claude auth login
                $authJson = & claude auth status 2>&1 | ConvertFrom-Json
                if ($authJson.loggedIn) {
                    Write-Success "Claude Code authenticated: $($authJson.email)"
                }
                else {
                    Write-Fail "Claude Code authentication failed."
                    Write-Info "Please run 'claude auth login' manually and try again."
                    Exit-WithLog 1
                }
            }
        }
        catch {
            Write-Warn "Could not check Claude Code auth status: $($_.Exception.Message)"
            Write-Info "You can authenticate later with 'claude auth login'"
        }
    }
    else {
        Write-Warn "Claude Code not found in PATH. Interactive setup will be skipped."
        Write-Info "Install Claude Code and run 'claude auth login' before using 'haniel install'"
    }

    # --------------------------------------------------------
    # Step 4: Install path + create root
    # --------------------------------------------------------
    Write-Step "4/8" "Install Directory"

    if ([string]::IsNullOrWhiteSpace($InstallPath)) {
        $defaultPath = "C:\Services\Haniel"
        $InstallPath = Read-Host "  Install path [$defaultPath]"
        if ([string]::IsNullOrWhiteSpace($InstallPath)) {
            $InstallPath = $defaultPath
        }
    }

    # Create root directory
    New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
    Write-Success "Root directory: $InstallPath"

    # --------------------------------------------------------
    # Step 4: Clone haniel into .self/ + install
    # --------------------------------------------------------
    Write-Step "5/8" "Clone Haniel"

    # WinSW (needs InstallPath set first)
    $binDir = Join-Path $InstallPath "bin"
    $winsw = Install-WinSW
    if (-not $winsw) {
        Write-Fail "WinSW is required for Windows service registration."
        Write-Info "You can download it manually from https://github.com/winsw/winsw/releases"
        Exit-WithLog 1
    }

    $selfDir = Join-Path $InstallPath ".self"

    if (Test-Path (Join-Path $selfDir ".git")) {
        Write-Info "Haniel repository already exists at $selfDir"
        Write-Info "Pulling latest changes..."
        Invoke-NativeCommand { git -C $selfDir pull --ff-only }
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "git pull failed (exit code $LASTEXITCODE). Continuing with existing state."
        }
    }
    else {
        if (Test-Path $selfDir) {
            $contents = Get-ChildItem -Path $selfDir -Force
            if ($contents.Count -gt 0) {
                Write-Fail "Directory $selfDir exists and is not empty."
                Write-Info "Please remove it or choose a different install path."
                Exit-WithLog 1
            }
            Remove-Item -Path $selfDir -Force -Recurse
        }

        Write-Info "Cloning haniel to $selfDir..."
        Invoke-NativeCommand { git clone https://github.com/eiaserinnys/haniel.git $selfDir }
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "Failed to clone haniel repository"
            Exit-WithLog 1
        }
    }

    Write-Success "Haniel repository ready at $selfDir"

    # Create venv inside .self/ + editable install
    $venvPath = Join-Path $selfDir ".venv"
    $pipExe = Join-Path $venvPath "Scripts\pip.exe"
    $hanielExe = Join-Path $venvPath "Scripts\haniel.exe"

    if (-not (Test-Path $venvPath)) {
        Write-Info "Creating virtual environment..."
        & python -m venv $venvPath
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "Failed to create virtual environment"
            Exit-WithLog 1
        }
    }

    Write-Info "Installing haniel (editable)..."
    Invoke-NativeCommand { & $pipExe install -e $selfDir }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install haniel"
        Exit-WithLog 1
    }

    Write-Success "haniel installed"

    # --------------------------------------------------------
    # Step 5: Download haniel.yaml to root
    # --------------------------------------------------------
    Write-Step "6/8" "Configuration"

    $configPath = Join-Path $InstallPath "haniel.yaml"

    if (Test-Path $configPath) {
        Write-Info "haniel.yaml already exists at $configPath"
        $answer = Read-Host "  Overwrite with new config? (y/N)"
        if ($answer -ne "y" -and $answer -ne "Y") {
            Write-Info "Keeping existing config"
        }
        else {
            $downloadConfig = $true
        }
    }
    else {
        $downloadConfig = $true
    }

    if ($downloadConfig) {
        if ([string]::IsNullOrWhiteSpace($ConfigUrl)) {
            $ConfigUrl = Read-Host "  Config source — HTTPS URL or local file path"
            if ([string]::IsNullOrWhiteSpace($ConfigUrl)) {
                Write-Fail "Config source is required."
                Exit-WithLog 1
            }
        }

        if ($ConfigUrl -match '^https://') {
            # Remote URL — download
            Write-Info "Downloading $ConfigUrl..."
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -Uri $ConfigUrl -OutFile $configPath -UseBasicParsing
            }
            catch {
                Write-Fail "Failed to download config: $($_.Exception.Message)"
                Exit-WithLog 1
            }
        }
        elseif (Test-Path $ConfigUrl) {
            # Local file — copy
            Write-Info "Copying from $ConfigUrl..."
            try {
                Copy-Item -Path $ConfigUrl -Destination $configPath -Force
            }
            catch {
                Write-Fail "Failed to copy config: $($_.Exception.Message)"
                Exit-WithLog 1
            }
        }
        else {
            Write-Fail "Config source not found: $ConfigUrl"
            Write-Info "Provide an HTTPS URL or a valid local file path."
            Exit-WithLog 1
        }

        if (-not (Test-Path $configPath)) {
            Write-Fail "Config file not available at $configPath"
            Exit-WithLog 1
        }
    }

    Write-Success "Config ready at $configPath"

    # --------------------------------------------------------
    # Step 6: haniel install
    # --------------------------------------------------------
    Write-Step "7/8" "Running haniel install"

    Write-Info "Working directory: $InstallPath"
    Write-Info "Command: $hanielExe install haniel.yaml"
    Write-Host ""

    Push-Location $InstallPath
    try {
        & $hanielExe install haniel.yaml
        $installExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($installExitCode -ne 0) {
        Write-Fail "haniel install exited with code $installExitCode"
        Write-Info "Check the output above for details."
        Write-Info "You can re-run: $hanielExe install haniel.yaml"
        Write-Info "  (from directory: $InstallPath)"
        Exit-WithLog 1
    }

    # --------------------------------------------------------
    # Step 7: Start service
    # --------------------------------------------------------
    Write-Step "8/8" "Starting Service"

    # Read service name from install.service.name in YAML
    # Fall back to "haniel" if we can't parse it
    $serviceName = "haniel"
    try {
        $yamlContent = Get-Content $configPath -Raw
        if ($yamlContent -match '(?m)^\s+name:\s+(\S+)') {
            $serviceName = $Matches[1]
        }
    }
    catch {
        Write-Info "Could not parse service name from YAML, using default: haniel"
    }

    Write-Info "Starting service '$serviceName'..."
    try {
        Invoke-NativeCommand { sc.exe start $serviceName }
        if ($LASTEXITCODE -eq 0) {
            Write-Success "Service '$serviceName' started"
        }
        else {
            Write-Warn "sc start exited with code $LASTEXITCODE"
            Write-Info "You can start the service manually: sc start $serviceName"
        }
    }
    catch {
        Write-Warn "Failed to start service: $($_.Exception.Message)"
        Write-Info "You can start the service manually: sc start $serviceName"
    }

    # --------------------------------------------------------
    # Done
    # --------------------------------------------------------
    Write-Header "Installation Complete"

    Write-Host "  Service '$serviceName' is running at:" -ForegroundColor Green
    Write-Host "    $InstallPath" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Directory layout:" -ForegroundColor Yellow
    Write-Host "    haniel.yaml    : $configPath" -ForegroundColor White
    Write-Host "    haniel repo    : $selfDir" -ForegroundColor White
    Write-Host "    haniel venv    : $venvPath" -ForegroundColor White
    Write-Host ""
    Write-Host "  Commands:" -ForegroundColor Yellow
    Write-Host "    Stop service   : sc stop $serviceName" -ForegroundColor White
    Write-Host "    Restart        : sc stop $serviceName && sc start $serviceName" -ForegroundColor White
    Write-Host "    Run manually   : $hanielExe run haniel.yaml" -ForegroundColor White
    Write-Host "    Validate       : $hanielExe validate haniel.yaml" -ForegroundColor White
    Write-Host ""
}

# Run
Write-Log "Install started at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Log "Log file: $script:LogFile"

try {
    Main
}
catch {
    Write-Host ""
    Write-Fail "Unexpected error: $($_.Exception.Message)"
    Write-Log "EXCEPTION: $($_.Exception.Message)"
    Write-Log "STACK: $($_.ScriptStackTrace)"
    Write-Host ""
    Write-Host "  Full log saved to: $script:LogFile" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Press any key to exit..." -ForegroundColor Gray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}
