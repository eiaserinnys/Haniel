<#
.SYNOPSIS
    Haniel bootstrapper for Windows.

.DESCRIPTION
    This script bootstraps haniel on a fresh Windows machine:
    1. Checks/installs Python via winget
    2. Prompts for installation directory
    3. Creates venv and installs haniel
    4. Runs haniel install

.EXAMPLE
    .\install-haniel.ps1

.EXAMPLE
    .\install-haniel.ps1 -InstallPath "D:\Services\Haniel"

.NOTES
    Requires PowerShell 5.1 or higher.
    Requires winget for automatic Python installation.
#>

[CmdletBinding()]
param(
    [Parameter(HelpMessage="Installation directory")]
    [string]$InstallPath,

    [Parameter(HelpMessage="Skip Python installation check")]
    [switch]$SkipPythonCheck,

    [Parameter(HelpMessage="Haniel config file (haniel.yaml)")]
    [string]$ConfigFile
)

$ErrorActionPreference = "Stop"

# Colors for output
function Write-Header($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Success($text) {
    Write-Host "[OK] $text" -ForegroundColor Green
}

function Write-Warning($text) {
    Write-Host "[!] $text" -ForegroundColor Yellow
}

function Write-Error($text) {
    Write-Host "[X] $text" -ForegroundColor Red
}

function Write-Info($text) {
    Write-Host "    $text" -ForegroundColor Gray
}

# Check if running as administrator
function Test-Administrator {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentUser)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Check Python installation
function Test-Python {
    try {
        $pythonVersion = & python --version 2>&1
        if ($pythonVersion -match "Python (\d+)\.(\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 11) {
                Write-Success "Python $($Matches[0]) found"
                return $true
            } else {
                Write-Warning "Python found but version too old: $($Matches[0]) (need 3.11+)"
                return $false
            }
        }
    } catch {
        Write-Warning "Python not found in PATH"
        return $false
    }
    return $false
}

# Install Python via winget
function Install-Python {
    Write-Info "Checking for winget..."

    try {
        $wingetVersion = & winget --version 2>&1
        Write-Info "winget found: $wingetVersion"
    } catch {
        Write-Error "winget not found. Please install Python manually from https://python.org"
        exit 1
    }

    Write-Info "Installing Python 3.12 via winget..."

    & winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install Python via winget"
        Write-Info "Please install Python manually from https://python.org"
        exit 1
    }

    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

    Write-Success "Python installed successfully"
    Write-Info "You may need to restart your terminal for PATH changes to take effect"
}

# Check Node.js installation
function Test-Node {
    try {
        $nodeVersion = & node --version 2>&1
        if ($nodeVersion -match "v(\d+)\.") {
            $major = [int]$Matches[1]
            if ($major -ge 18) {
                Write-Success "Node.js $nodeVersion found"
                return $true
            } else {
                Write-Warning "Node.js found but version too old: $nodeVersion (need 18+)"
                return $false
            }
        }
    } catch {
        Write-Warning "Node.js not found in PATH"
        return $false
    }
    return $false
}

# Check NSSM installation
function Test-NSSM {
    try {
        $nssmPath = Get-Command nssm -ErrorAction Stop
        Write-Success "NSSM found at $($nssmPath.Source)"
        return $true
    } catch {
        Write-Warning "NSSM not found in PATH"
        Write-Info "Download from https://nssm.cc/download"
        return $false
    }
}

# Check Claude Code installation
function Test-ClaudeCode {
    try {
        $claudePath = Get-Command claude -ErrorAction Stop
        Write-Success "Claude Code found at $($claudePath.Source)"
        return $true
    } catch {
        Write-Warning "Claude Code not found in PATH"
        Write-Info "Install with: npm install -g @anthropic-ai/claude-code"
        return $false
    }
}

# Create virtual environment
function New-HanielVenv {
    param($Path)

    $venvPath = Join-Path $Path ".venv"

    if (Test-Path $venvPath) {
        Write-Info "Virtual environment already exists at $venvPath"
        return $venvPath
    }

    Write-Info "Creating virtual environment at $venvPath..."
    & python -m venv $venvPath

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to create virtual environment"
        exit 1
    }

    Write-Success "Virtual environment created"
    return $venvPath
}

# Install haniel in venv
function Install-Haniel {
    param($VenvPath)

    $pipPath = Join-Path $VenvPath "Scripts\pip.exe"

    Write-Info "Installing haniel..."

    # Install from PyPI or local
    if (Test-Path ".\pyproject.toml") {
        Write-Info "Installing from local source..."
        & $pipPath install -e ".[dev]"
    } else {
        Write-Info "Installing from PyPI..."
        & $pipPath install haniel
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install haniel"
        exit 1
    }

    Write-Success "haniel installed"
}

# Main script
function Main {
    Write-Host ""
    Write-Host "  _   _             _      _ " -ForegroundColor Magenta
    Write-Host " | | | | __ _ _ __ (_) ___| |" -ForegroundColor Magenta
    Write-Host " | |_| |/ _`` | '_ \| |/ _ \ |" -ForegroundColor Magenta
    Write-Host " |  _  | (_| | | | | |  __/ |" -ForegroundColor Magenta
    Write-Host " |_| |_|\__,_|_| |_|_|\___|_|" -ForegroundColor Magenta
    Write-Host ""
    Write-Host "  Configuration-based service runner" -ForegroundColor Gray
    Write-Host ""

    # Check administrator privileges (recommended for NSSM)
    if (-not (Test-Administrator)) {
        Write-Warning "Not running as Administrator. Some features may not work."
        Write-Info "Consider running PowerShell as Administrator for full functionality."
        Write-Host ""
    }

    # Step 1: Check requirements
    Write-Header "Step 1: Checking Requirements"

    $pythonOk = $false
    if (-not $SkipPythonCheck) {
        $pythonOk = Test-Python
        if (-not $pythonOk) {
            $install = Read-Host "Install Python via winget? (Y/n)"
            if ($install -ne "n" -and $install -ne "N") {
                Install-Python
                $pythonOk = Test-Python
            }
        }
    } else {
        Write-Info "Skipping Python check"
        $pythonOk = $true
    }

    $nodeOk = Test-Node
    $nssmOk = Test-NSSM
    $claudeOk = Test-ClaudeCode

    if (-not $pythonOk) {
        Write-Error "Python 3.11+ is required. Please install it and try again."
        exit 1
    }

    # Warnings for optional dependencies
    if (-not $nodeOk) {
        Write-Warning "Node.js is recommended for npm-based MCP servers"
    }
    if (-not $nssmOk) {
        Write-Warning "NSSM is required for Windows service registration"
    }
    if (-not $claudeOk) {
        Write-Warning "Claude Code is required for interactive setup"
    }

    # Step 2: Choose installation directory
    Write-Header "Step 2: Installation Directory"

    if (-not $InstallPath) {
        $defaultPath = "C:\Services\Haniel"
        $InstallPath = Read-Host "Installation directory [$defaultPath]"
        if ([string]::IsNullOrWhiteSpace($InstallPath)) {
            $InstallPath = $defaultPath
        }
    }

    # Create directory if it doesn't exist
    if (-not (Test-Path $InstallPath)) {
        Write-Info "Creating directory: $InstallPath"
        New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
    }

    Write-Success "Installation directory: $InstallPath"

    # Step 3: Create venv and install haniel
    Write-Header "Step 3: Installing Haniel"

    $venvPath = New-HanielVenv -Path $InstallPath
    Install-Haniel -VenvPath $venvPath

    # Step 4: Create or find config file
    Write-Header "Step 4: Configuration"

    $hanielPath = Join-Path $venvPath "Scripts\haniel.exe"
    $configPath = if ($ConfigFile) { $ConfigFile } else { Join-Path $InstallPath "haniel.yaml" }

    if (-not (Test-Path $configPath)) {
        Write-Warning "No haniel.yaml found at $configPath"
        Write-Info "You need to create a haniel.yaml configuration file."
        Write-Info "See: https://github.com/eiaserinnys/Haniel for examples"
        Write-Host ""
        $createSample = Read-Host "Create a sample config? (Y/n)"
        if ($createSample -ne "n" -and $createSample -ne "N") {
            $sampleConfig = @"
# Haniel Configuration
# See https://github.com/eiaserinnys/Haniel for full documentation

poll_interval: 60

repos: {}

services: {}

install:
  requirements:
    python: ">=3.11"
    nssm: true
    claude-code: true

  directories:
    - ./runtime
    - ./runtime/logs
    - ./workspace

  configs:
    workspace-env:
      path: ./workspace/.env
      keys:
        - key: DEBUG
          default: "false"

  service:
    name: haniel
    display: "Haniel Service Runner"
    working_directory: "{root}"
"@
            Set-Content -Path $configPath -Value $sampleConfig
            Write-Success "Sample config created at $configPath"
            Write-Info "Edit the config file to add your services and repositories"
        }
    } else {
        Write-Success "Config found: $configPath"
    }

    # Step 5: Run haniel install
    Write-Header "Step 5: Running Haniel Install"

    if (Test-Path $configPath) {
        $runInstall = Read-Host "Run 'haniel install' now? (Y/n)"
        if ($runInstall -ne "n" -and $runInstall -ne "N") {
            Push-Location $InstallPath
            try {
                Write-Info "Running: $hanielPath install $configPath --skip-interactive"
                & $hanielPath install (Split-Path $configPath -Leaf) --skip-interactive
            } finally {
                Pop-Location
            }
        }
    }

    # Done
    Write-Header "Installation Complete"

    Write-Host "Haniel has been installed to: $InstallPath" -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Yellow
    Write-Host "  1. Edit $configPath to configure your services" -ForegroundColor White
    Write-Host "  2. Run: $hanielPath install haniel.yaml" -ForegroundColor White
    Write-Host "  3. Start manually: $hanielPath run haniel.yaml" -ForegroundColor White
    Write-Host "  4. Or start as service: nssm start haniel" -ForegroundColor White
    Write-Host ""
}

# Run main
Main
