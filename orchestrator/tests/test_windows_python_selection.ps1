$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
. (Join-Path $repoRoot "utilities/python-runtime.ps1")

# Load only setup's selection functions, not its installation entry point.
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repoRoot "setup-prereqs.ps1"), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw $parseErrors[0].Message }
foreach ($name in @("Get-PythonCandidates", "Select-SupportedPython")) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    Invoke-Expression $definition.Extent.Text
}

function Assert-Equal($Expected, $Actual, [string]$Message) {
    if ($Expected -ne $Actual) { throw "$Message Expected '$Expected', got '$Actual'." }
}
function New-Runtime([string]$Platform = "win-amd64", [int]$Bits = 64, [int]$Minor = 13, [string]$Executable = "selected-x64.exe") {
    [pscustomobject]@{ version = @(3, $Minor, 1); platform = $Platform; bits = $Bits; executable = $Executable }
}

foreach ($case in @(
    @{ Platform = "win-arm64"; Bits = 64; Minor = 13; Windows = $true; Expected = $false },
    @{ Platform = "win32"; Bits = 32; Minor = 13; Windows = $true; Expected = $false },
    @{ Platform = "win-amd64"; Bits = 32; Minor = 13; Windows = $true; Expected = $false },
    @{ Platform = "win-amd64"; Bits = 64; Minor = 12; Windows = $true; Expected = $false },
    @{ Platform = "win-amd64"; Bits = 64; Minor = 14; Windows = $true; Expected = $false },
    @{ Platform = "win-amd64"; Bits = 64; Minor = 13; Windows = $true; Expected = $true },
    @{ Platform = "macosx-11.0-arm64"; Bits = 64; Minor = 14; Windows = $false; Expected = $true },
    @{ Platform = "linux-x86_64"; Bits = 64; Minor = 13; Windows = $false; Expected = $true },
    @{ Platform = "linux-x86_64"; Bits = 64; Minor = 12; Windows = $false; Expected = $false },
    @{ Platform = "linux-x86_64"; Bits = 64; Minor = 15; Windows = $false; Expected = $false }
)) {
    $runtime = New-Runtime -Platform $case.Platform -Bits $case.Bits -Minor $case.Minor
    Assert-Equal $case.Expected (Test-SupportedPythonRuntime $runtime -Windows $case.Windows) "Runtime support boundary"
}
Assert-Equal $false (Test-SupportedPythonRuntime $null -Windows $true) "Unreadable runtime must fail closed"

# Fake command discovery and launchers keep selection independent of the machine
# running the test, while executing the real probe and selection decisions.
function Get-Command {
    param([string]$Name, [switch]$All, [string]$CommandType, $ErrorAction)
    if ($Name -eq "py" -and $script:launcherAvailable) { return @{ Source = "py" } }
    if ($Name -eq "python" -and $CommandType -eq "Application") {
        foreach ($file in $script:pathCandidates) { @{ Source = $file } }
    } elseif ($Name -in @("registered-x64.exe", "path-arm.exe", "path-x64.exe")) {
        return @{ Source = $Name }
    }
}
function Write-FakeRuntime($Runtime) {
    if ($env:PYTHON_MANAGER_AUTOMATIC_INSTALL -ne "false" -or $env:PYLAUNCHER_ALLOW_INSTALL) {
        throw "Interpreter probing allowed automatic installation"
    }
    $global:LASTEXITCODE = 0
    $Runtime | ConvertTo-Json -Compress
}
function py {
    if ($args[0] -eq "-0p") {
        $global:LASTEXITCODE = 0
        if ($script:launcherMode -eq "registry") { ' -V:3.13-64 * registered-x64.exe' }
        return
    }
    switch ($script:launcherMode) {
        "legacy" {
            if ($args[0] -eq "-3.13-64") { Write-FakeRuntime (New-Runtime); return }
        }
        "manager" {
            if ($args[0] -eq "-V:3.13-64") { Write-FakeRuntime (New-Runtime); return }
        }
    }
    Write-FakeRuntime (New-Runtime -Platform "win-arm64")
}
function registered-x64.exe { Write-FakeRuntime (New-Runtime) }
function path-arm.exe { Write-FakeRuntime (New-Runtime -Platform "win-arm64") }
function path-x64.exe { Write-FakeRuntime (New-Runtime) }
function malformed-python { $global:LASTEXITCODE = 0; '{"version":[3,13,1]}' }
function failed-python { $global:LASTEXITCODE = 1; New-Runtime | ConvertTo-Json -Compress }

$isWin = $true
$pythonSupport = "Python 3.13 x64"
$pass = 0
$warn = 0
$launcherAvailable = $true
$pathCandidates = @()
$savedManager = $env:PYTHON_MANAGER_AUTOMATIC_INSTALL
$savedLegacy = $env:PYLAUNCHER_ALLOW_INSTALL
try {
    $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = "true"
    $env:PYLAUNCHER_ALLOW_INSTALL = "1"
    foreach ($mode in @("legacy", "manager", "registry")) {
        $launcherMode = $mode
        Assert-Equal $true (Select-SupportedPython) "$mode launcher must discover x64"
        Assert-Equal "selected-x64.exe" $pythonFile "Selection must bind the probed executable"
    }
    $launcherAvailable = $false
    $pathCandidates = @("path-arm.exe", "path-x64.exe")
    Assert-Equal $true (Select-SupportedPython) "Later compatible PATH entry must not be hidden by ARM64"
    $pathCandidates = @("path-arm.exe")
    Assert-Equal $false (Select-SupportedPython) "Native ARM64 candidate must be rejected"
    Assert-Equal $null $pythonFile "A failed search must not retain the last good interpreter"
    Assert-Equal $null (Get-PythonRuntimeInfo -File malformed-python) "Malformed probe must be rejected"
    Assert-Equal $null (Get-PythonRuntimeInfo -File failed-python) "Failed probe must be rejected even with valid JSON"
    Assert-Equal "true" $env:PYTHON_MANAGER_AUTOMATIC_INSTALL "Manager setting must be restored"
    Assert-Equal "1" $env:PYLAUNCHER_ALLOW_INSTALL "Legacy launcher setting must be restored"
} finally {
    $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = $savedManager
    $env:PYLAUNCHER_ALLOW_INSTALL = $savedLegacy
}

# Venv lifecycle tests use a real temporary directory and a deterministic runtime
# boundary. A rejected result is what keeps setup from entering dependency install.
$ProductionProbe = ${function:Get-PythonRuntimeInfo}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ("hls-python-selection-" + [guid]::NewGuid().ToString("N"))
$venvPath = Join-Path $temporary ".venv"
$venvExecutable = Join-Path $venvPath "Scripts/python.exe"
$creationCalls = 0
$creationExit = 0
$createdPlatform = "win-amd64"
$existingPlatform = "win-arm64"
function Get-PythonRuntimeInfo {
    param([string]$File, [string[]]$Arguments = @())
    if ($File -eq "base-python") { return New-Runtime -Executable $File }
    if ($File -eq $script:venvExecutable -and (Test-Path $File)) {
        return New-Runtime -Platform $script:existingPlatform -Executable $File
    }
    return $null
}
function base-python {
    $script:creationCalls++
    New-Item -ItemType Directory -Path (Split-Path $script:venvExecutable) -Force | Out-Null
    Set-Content -Path $script:venvExecutable -Value "created"
    $script:existingPlatform = $script:createdPlatform
    $global:LASTEXITCODE = $script:creationExit
    if ($script:creationExit) { "creation failed" }
}
try {
    New-Item -ItemType Directory -Path (Split-Path $venvExecutable) -Force | Out-Null
    Set-Content -Path $venvExecutable -Value "original"
    foreach ($platform in @("win-arm64", "win32")) {
        $existingPlatform = $platform
        Assert-Equal $null (Initialize-PythonVenv -Path $venvPath -PythonFile base-python -Windows $true -CheckOnly) "CheckOnly must reject incompatible venv"
        Assert-Equal "original" (Get-Content $venvExecutable -Raw).Trim() "CheckOnly must preserve existing venv bytes"
        Assert-Equal 0 $creationCalls "CheckOnly must not create an environment"
    }
    $result = Initialize-PythonVenv -Path $venvPath -PythonFile base-python -Windows $true
    Assert-Equal "win-amd64" $result.platform "Incompatible environment must be recreated as x64"
    Assert-Equal 1 $creationCalls "Only the incompatible environment should be recreated"
    $result = Initialize-PythonVenv -Path $venvPath -PythonFile base-python -Windows $true -CheckOnly
    Assert-Equal $venvExecutable $result.executable "Compatible venv should be reusable"
    Assert-Equal 1 $creationCalls "Compatible CheckOnly must not recreate the environment"

    $existingPlatform = "win-arm64"
    $creationExit = 1
    Assert-Equal $null (Initialize-PythonVenv -Path $venvPath -PythonFile base-python -Windows $true) "Failed creation stdout must not become a usable runtime"
    $creationExit = 0
    $existingPlatform = "win-arm64"
    $createdPlatform = "win-arm64"
    Assert-Equal $null (Initialize-PythonVenv -Path $venvPath -PythonFile base-python -Windows $true) "A newly created incompatible venv must still fail closed"

    $missingPath = Join-Path $temporary "missing"
    Assert-Equal $null (Initialize-PythonVenv -Path $missingPath -PythonFile base-python -Windows $true -CheckOnly) "Missing venv must remain missing in CheckOnly"
    Assert-Equal $false (Test-Path $missingPath) "CheckOnly must not create any venv directories"
} finally {
    ${function:Get-PythonRuntimeInfo} = $ProductionProbe
    Remove-Item -Recurse -Force $temporary -ErrorAction SilentlyContinue
}
Write-Host "Windows Python selection tests passed."
