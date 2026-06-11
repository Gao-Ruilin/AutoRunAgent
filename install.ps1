#Requires -Version 2.0
$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  AutoRUN v1 - Installation Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ============================================================
# Step 1: Find or install Python 3.8+
# ============================================================
Write-Host "[1/6] Ensuring Python 3.8+ is available..." -ForegroundColor Yellow

function Find-Python {
    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    $searchDirs = @(
        (Join-Path $localAppData "Programs\Python")
        "C:\"
    )
    foreach ($baseDir in $searchDirs) {
        if (Test-Path $baseDir) {
            # Filter for directories
            $dirs = Get-ChildItem -Path $baseDir -Filter "Python3*" -ErrorAction SilentlyContinue |
                Where-Object { $_.PSIsContainer } |
                Sort-Object Name -Descending
            foreach ($dir in $dirs) {
                $exe = Join-Path $dir.FullName "python.exe"
                if (Test-Path $exe) { return $exe }
            }
        }
    }
    $cmd = Get-Command python3 -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Install-PythonWinget {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    Write-Host "        Installing Python 3.12 via winget..." -ForegroundColor Gray
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "        Python installed. Please RESTART your terminal and re-run this script." -ForegroundColor Yellow
        Read-Host "Press Enter to exit"
        exit 0
    }
    return $false
}

$pythonExe = Find-Python

if (-not $pythonExe) {
    Write-Host "        Python not found. Attempting auto-install..." -ForegroundColor Gray
    if (-not (Install-PythonWinget)) {
        Write-Host ""
        Write-Host "[ERROR] Could not auto-install Python." -ForegroundColor Red
        Write-Host "        Please install Python 3.8+ manually:"
        Write-Host "        1. Download from: https://www.python.org/downloads/"
        Write-Host "        2. IMPORTANT: Check 'Add Python to PATH' during installation"
        Write-Host "        3. Re-run this script after installation"
        Write-Host ""
        Read-Host "Press Enter to exit"
        exit 1
    }
}

$pyVersion = & $pythonExe --version 2>&1
Write-Host "        Found $pyVersion" -ForegroundColor Green

$versionMatch = [regex]::Match($pyVersion, '(\d+)\.(\d+)')
$major = [int]$versionMatch.Groups[1].Value
$minor = [int]$versionMatch.Groups[2].Value
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 8)) {
    Write-Host "[ERROR] Python 3.8+ required, found $pyVersion" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# ============================================================
# Step 2: Ensure pip is available
# ============================================================
Write-Host ""
Write-Host "[2/6] Ensuring pip is available..." -ForegroundColor Yellow

& $pythonExe -m pip --version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "        pip not found, bootstrapping..." -ForegroundColor Gray
    & $pythonExe -m ensurepip --upgrade 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Could not bootstrap pip. Please reinstall Python." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
}
Write-Host "        pip OK" -ForegroundColor Green

# ============================================================
# Step 3: Create and activate virtual environment
# ============================================================
Write-Host ""
Write-Host "[3/6] Setting up virtual environment..." -ForegroundColor Yellow

function Test-VenvHealthy {
    param([string]$venvPython)
    # Check python works
    $ver = & $venvPython --version 2>&1
    if ($LASTEXITCODE -ne 0) { return $false }
    # Check pip works
    & $venvPython -m pip --version 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    # Check a key dependency is installed (fastapi)
    $check = & $venvPython -c "import fastapi" 2>&1
    if ($LASTEXITCODE -ne 0) { return $false }
    return $true
}

$needCreate = $true
if (Test-Path ".venv") {
    $venvPython = ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        if (Test-VenvHealthy -venvPython $venvPython) {
            Write-Host "        Using existing .venv\ (healthy)" -ForegroundColor Gray
            $needCreate = $false
        } else {
            Write-Host "        Existing .venv\ is broken (missing dependencies), recreating..." -ForegroundColor Yellow
            Remove-Item -Recurse -Force ".venv" -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host "        Existing .venv\ is incomplete, recreating..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force ".venv" -ErrorAction SilentlyContinue
    }
}

if ($needCreate) {
    Write-Host "        Creating .venv ..." -ForegroundColor Gray
    & $pythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Could not create virtual environment." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "        Virtual environment created." -ForegroundColor Green
}

$venvActivate = ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    . $venvActivate
    Write-Host "        Virtual environment activated." -ForegroundColor Green
} else {
    Write-Host "[ERROR] Could not activate virtual environment." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# ============================================================
# Step 4: Install dependencies
# ============================================================
Write-Host ""
Write-Host "[4/6] Installing dependencies..." -ForegroundColor Yellow

python -m pip install --upgrade pip 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "        Warning: pip upgrade failed, continuing with current version..." -ForegroundColor Yellow
}
# Ensure pip is functional before proceeding
python -m pip --version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] pip is not functional in virtual environment." -ForegroundColor Red
    Write-Host "        Try deleting .venv and re-running this script."
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "        Installing packages from requirements.txt..." -ForegroundColor Gray
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Failed to install dependencies." -ForegroundColor Red
    Write-Host "        Check your network connection and try again."
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "        Dependencies installed." -ForegroundColor Green

# ============================================================
# Step 5: Ensure pyproject.toml entry point is correct
# ============================================================
Write-Host ""
Write-Host "[5/6] Checking project configuration..." -ForegroundColor Yellow

$pyproject = "pyproject.toml"
if (Test-Path $pyproject) {
    # Read pyproject.toml content
    $content = Get-Content $pyproject | Out-String
    # Fix old-style entry points to use root-level main:cli_main
    if ($content -match 'autorun\s*=\s*"AutoRUN_v1\.') {
        $content = $content -replace 'autorun\s*=\s*"[^"]*"', 'autorun = "main:cli_main"'
        Set-Content $pyproject -Value $content
        Write-Host "        Fixed autorun entry point." -ForegroundColor Green
    }
}
Write-Host "        Configuration OK." -ForegroundColor Green

# ============================================================
# Step 6: Install project in editable mode
# ============================================================
Write-Host ""
Write-Host "[6/6] Installing AutoRUN (pip install -e .)..." -ForegroundColor Yellow
Write-Host ""

python -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[WARNING] pip install -e . failed." -ForegroundColor Yellow
    Write-Host "         autorun command will not be available."
    Write-Host "         You can still run: python main.py"
} else {
    Write-Host ""
    Write-Host "        autorun command installed successfully." -ForegroundColor Green

    # Add .venv\Scripts to user PATH so autorun works from anywhere
    $venvScripts = Join-Path $scriptDir ".venv\Scripts"
    $currentPath = [Environment]::GetEnvironmentVariable("PATH", "User") -split ";"
    if (-not ($currentPath -contains $venvScripts)) {
        [Environment]::SetEnvironmentVariable(
            "PATH",
            "$venvScripts;$([Environment]::GetEnvironmentVariable('PATH', 'User'))",
            "User"
        )
        Write-Host "        Added to PATH: $venvScripts" -ForegroundColor Green
        Write-Host "        (Restart terminal or re-login to take effect)" -ForegroundColor Gray
    }
}

# Create autorun.bat wrapper for convenience
@"
@echo off
call "%~dp0.venv\Scripts\activate.bat" >nul 2>nul
autorun %*
"@ | Set-Content -Path "autorun.bat"
Write-Host "        Created autorun.bat (double-click to launch)." -ForegroundColor Green

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Installation complete!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Quick start:" -ForegroundColor Gray
Write-Host "    autorun                       Start Web UI" -ForegroundColor White
Write-Host "    autorun --web                 Start Web UI" -ForegroundColor White
Write-Host "    autorun --setup               Configure API" -ForegroundColor White
Write-Host ""
Write-Host "  If autorun not found, activate venv first:" -ForegroundColor Gray
Write-Host "    .venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "    autorun" -ForegroundColor White
Write-Host ""
Write-Host "  Or run directly:" -ForegroundColor Gray
Write-Host "    python main.py" -ForegroundColor White
Write-Host "    python main.py --web" -ForegroundColor White
Write-Host ""

Read-Host "Press Enter to exit"
