<#
.SYNOPSIS
    Cross-platform prerequisite installer for the Medical Device FHIR Integration Platform.

.DESCRIPTION
    Checks and installs all dependencies needed to:
    1. Run the Deployment Orchestrator UI (frontend + backend)
    2. Execute the PowerShell deployment pipeline (Deploy-All.ps1)

    Supports Windows, macOS, and Linux.

.EXAMPLE
    # Check and install everything:
    .\setup-prereqs.ps1

    # Check only (don't install anything):
    .\setup-prereqs.ps1 -CheckOnly
#>

param(
    [switch]$CheckOnly
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir "utilities/python-runtime.ps1")

Write-Host ""
Write-Host "+============================================================+" -ForegroundColor Cyan
Write-Host "|       PREREQUISITE SETUP — Med Device FHIR Platform        |" -ForegroundColor Cyan
Write-Host "+============================================================+" -ForegroundColor Cyan
Write-Host ""

$isWin = $env:OS -eq "Windows_NT" -or $PSVersionTable.OS -match "Windows"
$isMac = $PSVersionTable.OS -match "Darwin"
$isLnx = $PSVersionTable.OS -match "Linux"

$platform = if ($isWin) { "Windows" } elseif ($isMac) { "macOS" } else { "Linux" }
Write-Host "  Platform: $platform" -ForegroundColor DarkGray
Write-Host ""

$pass = 0
$fail = 0
$warn = 0
$installed = 0

function Check-Tool {
    param([string]$Name, [string]$Command, [string]$VersionMatch, [string]$InstallHint)
    try {
        $output = Invoke-Expression $Command 2>&1
        $ver = if ($output -match $VersionMatch) { $Matches[0] } else { "found" }
        Write-Host "  ✓ $Name ($ver)" -ForegroundColor Green
        $script:pass++
        return $true
    } catch {
        Write-Host "  ✗ $Name — not found" -ForegroundColor Red
        Write-Host "    Install: $InstallHint" -ForegroundColor DarkGray
        $script:fail++
        return $false
    }
}

# ── 1. PowerShell 7+ ──────────────────────────────────────────────────
Write-Host "  Checking core tools..." -ForegroundColor White
if ($PSVersionTable.PSVersion.Major -ge 7) {
    Write-Host "  ✓ PowerShell $($PSVersionTable.PSVersion)" -ForegroundColor Green
    $pass++
} else {
    Write-Host "  ✗ PowerShell $($PSVersionTable.PSVersion) — 7+ required" -ForegroundColor Red
    Write-Host "    Install: https://aka.ms/powershell" -ForegroundColor DarkGray
    $fail++
}

# ── 2. Azure CLI ──────────────────────────────────────────────────────
# Direct invocation (don't use Check-Tool/Invoke-Expression; the latter has
# quoting/exit-code interactions that occasionally report `az` as not found
# even when it works at the prompt).
$hasAzCli = $false
$azCmd = Get-Command az -ErrorAction SilentlyContinue
if ($azCmd) {
    try {
        $azVerJson = az version --output json 2>$null | ConvertFrom-Json
        $cliVer = $azVerJson.'azure-cli'
        if ($cliVer) {
            Write-Host "  ✓ Azure CLI $cliVer" -ForegroundColor Green
            $pass++
            $hasAzCli = $true
        } else {
            Write-Host "  ⚠ Azure CLI present but version unreadable" -ForegroundColor Yellow
            $warn++
            $hasAzCli = $true  # binary exists; downstream checks can still try
        }
    } catch {
        Write-Host "  ⚠ Azure CLI present but `az version` failed: $($_.Exception.Message)" -ForegroundColor Yellow
        $warn++
        $hasAzCli = $true
    }
} else {
    Write-Host "  ✗ Azure CLI — not found" -ForegroundColor Red
    Write-Host "    Install: https://aka.ms/installazurecli" -ForegroundColor DarkGray
    $fail++
}

# ── 2b. AzCopy (parallel OneLake bulk transfer) ──────────────────────
$azCopyCmd = Get-Command azcopy -ErrorAction SilentlyContinue
if ($azCopyCmd) {
    $azCopyVersion = azcopy --version 2>$null
    Write-Host "  ✓ $azCopyVersion" -ForegroundColor Green
    $pass++
} else {
    Write-Host "  ✗ AzCopy — required for HDS bulk upload" -ForegroundColor Red
    Write-Host "    Install: https://learn.microsoft.com/azure/storage/common/storage-use-azcopy-v10" -ForegroundColor DarkGray
    $fail++
}

# ── 3. Bicep ──────────────────────────────────────────────────────────
if ($hasAzCli) {
    $bicepOut = az bicep version 2>&1 | Out-String
    if ($bicepOut -match "(\d+\.\d+\.\d+)") {
        Write-Host "  ✓ Bicep $($Matches[1])" -ForegroundColor Green
        $pass++
    } else {
        if (-not $CheckOnly) {
            Write-Host "  ⚙ Installing Bicep..." -ForegroundColor Yellow
            az bicep install 2>$null
            $installed++
            Write-Host "  ✓ Bicep installed" -ForegroundColor Green
            $pass++
        } else {
            Write-Host "  ✗ Bicep — not installed (run: az bicep install)" -ForegroundColor Red
            $fail++
        }
    }

    # ── 3b. Azure CLI extension dynamic-install ──────────────────────
    # The orchestrator runs pwsh with -NonInteractive. If az needs to install an
    # extension (e.g. `healthcareapis`) and the default `yes_prompt` setting is in
    # effect, the deployment hangs forever waiting on stdin.
    $dynInstall = az config get extension.use_dynamic_install --query value -o tsv 2>$null
    if ($dynInstall -eq "yes_without_prompt") {
        Write-Host "  ✓ Az CLI extension auto-install (yes_without_prompt)" -ForegroundColor Green
        $pass++
    } else {
        if (-not $CheckOnly) {
            Write-Host "  ⚙ Configuring az CLI to auto-install extensions without prompt..." -ForegroundColor Yellow
            $null = az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors 2>$null
            $installed++
            Write-Host "  ✓ Az CLI extension auto-install set to yes_without_prompt" -ForegroundColor Green
            $pass++
        } else {
            Write-Host "  ⚠ Az CLI extension auto-install is '$dynInstall' (will hang under -NonInteractive)" -ForegroundColor Yellow
            Write-Host "    Fix: az config set extension.use_dynamic_install=yes_without_prompt" -ForegroundColor DarkGray
            $warn++
        }
    }
}

# ── 4. Az PowerShell Module ───────────────────────────────────────────
$azMod = Get-Module -ListAvailable -Name Az.Accounts | Select-Object -First 1
if ($azMod) {
    Write-Host "  ✓ Az PowerShell module $($azMod.Version)" -ForegroundColor Green
    $pass++
} else {
    if (-not $CheckOnly) {
        Write-Host "  ⚙ Installing Az PowerShell module (this may take a few minutes)..." -ForegroundColor Yellow
        Install-Module Az -Scope CurrentUser -Force -AllowClobber -SkipPublisherCheck 2>$null
        $installed++
        Write-Host "  ✓ Az module installed" -ForegroundColor Green
        $pass++
    } else {
        Write-Host "  ✗ Az PowerShell module — not installed" -ForegroundColor Red
        Write-Host "    Install: Install-Module Az -Scope CurrentUser" -ForegroundColor DarkGray
        $fail++
    }
}

# ── 5. Python 3.13 x64 on Windows, 3.13-3.14 elsewhere ───────────────
Write-Host ""
Write-Host "  Checking Python + Node.js..." -ForegroundColor White
$hasPython = $false
$pythonFile = $null
$pythonSupport = if ($isWin) { "Python 3.13 x64 (AMD64)" } else { "Python 3.13-3.14" }
function Get-PythonCandidates {
    if ($isWin) {
        # Explicit x64 selectors cover the legacy launcher and the install manager.
        foreach ($selector in @("-V:3.13-64", "-3.13-64", "-V:3.13")) {
            @{ File = "py"; Args = @($selector) }
        }
        # Both launchers support -0p. Probe every registered path, including x64
        # installs hidden by a same-version ARM64 default. Listing never installs.
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $registered = & py -0p 2>$null
            if ($LASTEXITCODE -eq 0) {
                foreach ($line in $registered) {
                    if ($line -match '^\s*-\S+\s+(?:\*\s+)?(.+?\.exe)\s*$') {
                        @{ File = $Matches[1].Trim('"'); Args = @() }
                    }
                }
            }
        }
    }
    $names = if ($isWin) {
        @("python3.13", "python", "python3")
    } else {
        @("python3.14", "python3.13", "python3", "python")
    }
    foreach ($name in $names) {
        # Do not let an earlier PATH entry hide a compatible later executable.
        foreach ($command in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            @{ File = $command.Source; Args = @() }
        }
    }
}

function Select-SupportedPython {
    $script:hasPython = $false
    $script:pythonFile = $null
    foreach ($candidate in @(Get-PythonCandidates)) {
        if (-not (Get-Command $candidate.File -ErrorAction SilentlyContinue)) { continue }
        $candidateLabel = ($candidate.File + " " + ($candidate.Args -join " ")).Trim()
        $runtime = Get-PythonRuntimeInfo -File $candidate.File -Arguments $candidate.Args
        if (Test-SupportedPythonRuntime -Runtime $runtime -Windows $isWin) {
            Write-Host "  ✓ Python $($runtime.version -join '.') ($($runtime.platform), $($runtime.bits)-bit) via $candidateLabel" -ForegroundColor Green
            $script:pass++
            $script:hasPython = $true
            # Use the verified executable directly, not a launcher default that can change.
            $script:pythonFile = $runtime.executable
            return $true
        }
        if ($runtime) {
            Write-Host "  ⚠ Rejecting Python $($runtime.version -join '.') ($($runtime.platform), $($runtime.bits)-bit) via $candidateLabel; requires $pythonSupport" -ForegroundColor Yellow
            $script:warn++
        }
    }
    return $false
}

Select-SupportedPython | Out-Null

if (-not $hasPython -and $isWin -and -not $CheckOnly) {
    $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
    if ($wingetCmd) {
        Write-Host "  ⚙ Installing Python 3.13 x64 with winget (Windows 11 ARM64 supports x64 emulation)..." -ForegroundColor Yellow
        winget install --id Python.Python.3.13 -e --architecture x64 --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            $installed++
            # Refresh only this process's PATH from registered environment values.
            $env:PATH = @($env:PATH, [Environment]::GetEnvironmentVariable("Path", "Machine"), [Environment]::GetEnvironmentVariable("Path", "User")) -join [IO.Path]::PathSeparator
            Select-SupportedPython | Out-Null
        } else {
            Write-Host "  ✗ Python 3.13 x64 winget install failed; x64 Python requires an x64-capable Windows host (Windows 11 on ARM)." -ForegroundColor Red
            $fail++
        }
    } else {
        Write-Host "  ✗ $pythonSupport — not found and winget is unavailable" -ForegroundColor Red
        Write-Host "    Install: https://www.python.org/downloads/windows/" -ForegroundColor DarkGray
        $fail++
    }
}

if (-not $hasPython) {
    Write-Host "  ✗ $pythonSupport — not found" -ForegroundColor Red
    if ($isWin) {
        Write-Host "    ARM64 and x86 Python are not supported. cryptography is binary-only; C++ build tools cannot supply the required x64 wheel." -ForegroundColor DarkGray
        Write-Host "    Install: winget install --id Python.Python.3.13 -e --architecture x64 --accept-package-agreements --accept-source-agreements" -ForegroundColor DarkGray
    }
    $fail++
}

# ── 6. Node.js 18+ (for the Orchestrator UI) ─────────────────────────
$hasNode = $false
try {
    $nodeVer = node --version 2>&1
    if ($nodeVer -match "v(\d+)\.(\d+)") {
        $nodeMajor = [int]$Matches[1]
        if ($nodeMajor -ge 18) {
            Write-Host "  ✓ Node.js $nodeVer" -ForegroundColor Green
            $pass++
            $hasNode = $true
        } else {
            Write-Host "  ✗ Node.js $nodeVer — 18+ required" -ForegroundColor Red
            Write-Host "    Install: https://nodejs.org" -ForegroundColor DarkGray
            $fail++
        }
    }
} catch {
    Write-Host "  ✗ Node.js — not found (required for Orchestrator UI)" -ForegroundColor Red
    Write-Host "    Install: https://nodejs.org" -ForegroundColor DarkGray
    $fail++
}

# ── 7. npm ────────────────────────────────────────────────────────────
if ($hasNode) {
    try {
        $npmVer = npm --version 2>&1
        Write-Host "  ✓ npm $npmVer" -ForegroundColor Green
        $pass++
    } catch {
        Write-Host "  ⚠ npm — not found (usually bundled with Node.js)" -ForegroundColor Yellow
        $warn++
    }
}

# ── 8. Git ────────────────────────────────────────────────────────────
Check-Tool "Git" "git --version" "\d+\.\d+\.\d+" "https://git-scm.com" | Out-Null

# ── 9. Azure Login Check ──────────────────────────────────────────────
Write-Host ""
Write-Host "  Checking Azure login..." -ForegroundColor White
if ($hasAzCli) {
    try {
        $acct = az account show --output json 2>$null | ConvertFrom-Json
        if ($acct.id) {
            Write-Host "  ✓ Logged in: $($acct.name) ($($acct.user.name))" -ForegroundColor Green
            $pass++
        } else {
            Write-Host "  ✗ Not logged in to Azure" -ForegroundColor Red
            Write-Host "    Run: az login" -ForegroundColor DarkGray
            $fail++
        }
    } catch {
        Write-Host "  ✗ Not logged in to Azure" -ForegroundColor Red
        Write-Host "    Run: az login" -ForegroundColor DarkGray
        $fail++
    }
}

# ── 10. Setup Orchestrator Backend (Python venv) ──────────────────────
Write-Host ""
Write-Host "  Setting up Orchestrator backend..." -ForegroundColor White
$venvPath = Join-Path $ScriptDir "orchestrator/.venv"
$requirementsPath = Join-Path $ScriptDir "orchestrator/requirements.lock"
$dataGuardRequirementsPath = Join-Path $ScriptDir "utilities/repository-data-requirements.lock"

$venvRuntime = Initialize-PythonVenv -Path $venvPath -PythonFile $pythonFile -Windows $isWin -CheckOnly:$CheckOnly
if (-not $venvRuntime) {
    Write-Host "  ✗ Backend venv is missing or incompatible; requires $pythonSupport" -ForegroundColor Red
    Write-Host "    Fix: .\setup-prereqs.ps1" -ForegroundColor DarkGray
    $fail++
} else {
    $venvPython = $venvRuntime.executable
    Write-Host "  ✓ Python venv (Python $($venvRuntime.version -join '.'), $($venvRuntime.platform), $($venvRuntime.bits)-bit)" -ForegroundColor Green
    $pass++
    # Only a successfully probed, compatible venv may reach pip or import checks.
    if (-not $CheckOnly) {
        Write-Host "  ⚙ Installing Python dependencies..." -ForegroundColor Yellow
        & $venvPython -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ✗ pip upgrade failed" -ForegroundColor Red
            $fail++
        } else {
            & $venvPython -m pip install --no-cache-dir --require-hashes --only-binary "cryptography,pyarrow" -r $requirementsPath -r $dataGuardRequirementsPath
            if ($LASTEXITCODE -ne 0) {
                Write-Host "  ✗ Python dependency install failed" -ForegroundColor Red
                Write-Host "    Retry: $venvPython -m pip install --no-cache-dir --require-hashes --only-binary cryptography,pyarrow -r $requirementsPath -r $dataGuardRequirementsPath" -ForegroundColor DarkGray
                Write-Host "    cryptography and pyarrow require wheels; on Windows use Python 3.13 x64, including x64 emulation on Windows 11 ARM64." -ForegroundColor DarkGray
                $fail++
            } else {
                & $venvPython -c "import fastapi, uvicorn, pydantic, pyarrow"
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "  Python dependency verification failed (fastapi/uvicorn/pydantic/pyarrow import)" -ForegroundColor Red
                    $fail++
                } else {
                    $installed++
                    Write-Host "  ✓ Python dependencies installed and verified" -ForegroundColor Green
                    $pass++
                }
            }
        }
    } else {
        & $venvPython -B -c "import fastapi, uvicorn, pydantic, pyarrow" 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  ✓ Python dependencies present" -ForegroundColor Green
            $pass++
        } else {
            Write-Host "  ✗ Python dependencies missing from orchestrator/.venv" -ForegroundColor Red
            Write-Host "    Fix: $venvPython -m pip install --no-cache-dir --require-hashes --only-binary cryptography,pyarrow -r $requirementsPath -r $dataGuardRequirementsPath" -ForegroundColor DarkGray
            $fail++
        }
    }
}

# Register the checked-in pre-push guard without replacing a custom hook directory.
if (Get-Command git -ErrorAction SilentlyContinue) {
    $hooksPath = (& git -C $ScriptDir config --get core.hooksPath 2>$null) -join ""
    if (-not $hooksPath -and -not $CheckOnly) {
        & git -C $ScriptDir config core.hooksPath .githooks
        if ($LASTEXITCODE -eq 0) { $hooksPath = ".githooks" }
    }
    if ($hooksPath -eq ".githooks") {
        Write-Host "  Repository data pre-push guard enabled" -ForegroundColor Green
        $pass++
    } elseif ($hooksPath) {
        Write-Host "  Existing Git hooksPath '$hooksPath' was preserved. Integrate .githooks/pre-push before pushing." -ForegroundColor Yellow
        $warn++
    } else {
        Write-Host "  Repository data pre-push guard is not enabled. Run: git config core.hooksPath .githooks" -ForegroundColor Red
        $fail++
    }
}

# ── 11. Setup Orchestrator UI (npm install) ───────────────────────────
Write-Host ""
Write-Host "  Setting up Orchestrator UI..." -ForegroundColor White
$uiPath = Join-Path $ScriptDir "orchestrator-ui"
$nodeModules = Join-Path $uiPath "node_modules"

if ($hasNode) {
    if (-not (Test-Path $nodeModules)) {
        if (-not $CheckOnly) {
            Write-Host "  ⚙ Installing UI dependencies from package-lock.json (npm ci)..." -ForegroundColor Yellow
            Push-Location $uiPath
            npm ci --silent
            $npmExit = $LASTEXITCODE
            Pop-Location
            if ($npmExit -ne 0) {
                Write-Host "  ✗ UI dependency install failed" -ForegroundColor Red
                $fail++
            } else {
                $installed++
                Write-Host "  ✓ UI dependencies installed from lockfile" -ForegroundColor Green
                $pass++
            }
        } else {
            Write-Host "  ✗ UI deps not installed (run: cd orchestrator-ui && npm ci)" -ForegroundColor Yellow
            $warn++
        }
    } else {
        Write-Host "  ✓ UI dependencies present (orchestrator-ui/node_modules)" -ForegroundColor Green
        $pass++
    }
}

# ── Summary ───────────────────────────────────────────────────────────
Write-Host ""
Write-Host "+============================================================+" -ForegroundColor Cyan
Write-Host "|                      SUMMARY                              |" -ForegroundColor Cyan
Write-Host "+============================================================+" -ForegroundColor Cyan
Write-Host ""
Write-Host "  ✓ Passed:    $pass" -ForegroundColor Green
if ($fail -gt 0) {
    Write-Host "  ✗ Failed:    $fail" -ForegroundColor Red
}
if ($warn -gt 0) {
    Write-Host "  ⚠ Warnings:  $warn" -ForegroundColor Yellow
}
if ($installed -gt 0) {
    Write-Host "  ⚙ Installed:  $installed" -ForegroundColor Cyan
}

if ($fail -gt 0) {
    Write-Host ""
    Write-Host "  Fix the failures above before running the platform." -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "  All prerequisites satisfied!" -ForegroundColor Green
Write-Host ""
Write-Host "  ┌─────────────────────────────────────────────────────────┐" -ForegroundColor DarkCyan
Write-Host "  │  TO START THE ORCHESTRATOR UI:                         │" -ForegroundColor DarkCyan
Write-Host "  │                                                        │" -ForegroundColor DarkCyan
Write-Host "  │    .\Start-WebUI.ps1          # start both servers     │" -ForegroundColor DarkCyan
Write-Host "  │    .\Start-WebUI.ps1 -Stop    # stop both servers      │" -ForegroundColor DarkCyan
Write-Host "  │                                                        │" -ForegroundColor DarkCyan
Write-Host "  │  Then open: http://localhost:5173                      │" -ForegroundColor DarkCyan
Write-Host "  └─────────────────────────────────────────────────────────┘" -ForegroundColor DarkCyan

if (-not $CheckOnly) {
    Write-Host ""
    $startAnswer = Read-Host "  Start the Orchestrator UI now? [Y/n]"
    if (-not $startAnswer -or $startAnswer -match '^[Yy]') {
        & "$PSScriptRoot\Start-WebUI.ps1" -Force
    }
}
Write-Host ""
