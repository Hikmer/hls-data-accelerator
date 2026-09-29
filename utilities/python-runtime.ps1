# Probe the interpreter itself: native ARM64 PowerShell can run AMD64 Python.
function Get-PythonRuntimeInfo {
    param([string]$File, [string[]]$Arguments = @())

    $savedAutomaticInstall = $env:PYTHON_MANAGER_AUTOMATIC_INSTALL
    $savedLauncherInstall = $env:PYLAUNCHER_ALLOW_INSTALL
    try {
        # Both the install manager and legacy launcher can otherwise install while probing.
        $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = "false"
        $env:PYLAUNCHER_ALLOW_INSTALL = $null
        $output = & $File @Arguments -I -B -c "import json, struct, sys, sysconfig; print(json.dumps(dict(version=list(sys.version_info[:3]), platform=sysconfig.get_platform(), bits=struct.calcsize('P') * 8, executable=sys.executable)))" 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $info = ($output -join "`n") | ConvertFrom-Json -ErrorAction Stop
        if ($info.version.Count -ne 3 -or -not $info.executable -or -not $info.platform -or $info.bits -notin @(32, 64)) { return $null }
        return $info
    } catch {
        return $null
    } finally {
        $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = $savedAutomaticInstall
        $env:PYLAUNCHER_ALLOW_INSTALL = $savedLauncherInstall
    }
}

function Test-SupportedPythonRuntime {
    param($Runtime, [bool]$Windows)

    if (-not $Runtime) { return $false }
    $maxMinor = if ($Windows) { 13 } else { 14 }
    if ($Runtime.version[0] -ne 3 -or $Runtime.version[1] -lt 13 -or $Runtime.version[1] -gt $maxMinor) { return $false }
    return (-not $Windows -or ($Runtime.platform -eq "win-amd64" -and $Runtime.bits -eq 64))
}

function Initialize-PythonVenv {
    param([string]$Path, [string]$PythonFile, [bool]$Windows, [switch]$CheckOnly)

    $interpreter = Join-Path $Path $(if ($Windows) { "Scripts/python.exe" } else { "bin/python" })
    if (Test-Path $Path) {
        $runtime = Get-PythonRuntimeInfo -File $interpreter
        if (Test-SupportedPythonRuntime -Runtime $runtime -Windows $Windows) { return $runtime }
        Write-Host "  Existing orchestrator venv is incompatible or unreadable; run .\setup-prereqs.ps1 to recreate it." -ForegroundColor Yellow
        if ($CheckOnly -or -not $PythonFile) { return $null }
        try {
            Remove-Item -Recurse -Force $Path -ErrorAction Stop
        } catch {
            Write-Host "  Cannot remove incompatible venv: $_" -ForegroundColor Red
            return $null
        }
    }
    if ($CheckOnly -or -not $PythonFile) { return $null }
    # Recheck the base executable before allowing it to create the environment.
    $baseRuntime = Get-PythonRuntimeInfo -File $PythonFile
    if (-not (Test-SupportedPythonRuntime -Runtime $baseRuntime -Windows $Windows)) { return $null }
    & $PythonFile -m venv $Path | Out-Host
    if ($LASTEXITCODE -ne 0) { return $null }
    $runtime = Get-PythonRuntimeInfo -File $interpreter
    if (-not (Test-SupportedPythonRuntime -Runtime $runtime -Windows $Windows)) { return $null }
    return $runtime
}
