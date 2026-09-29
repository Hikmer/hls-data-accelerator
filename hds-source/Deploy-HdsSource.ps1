[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$FabricWorkspaceName,

    [string]$WorkspaceId = "",

    [switch]$ValidateOnly,

    [switch]$ContractOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot "utilities/python-runtime.ps1")
$orchestratorRoot = Join-Path $repoRoot "orchestrator"
$windowsHost = $env:OS -eq "Windows_NT" -or $PSVersionTable.OS -match "Windows"
$runtime = Initialize-PythonVenv -Path (Join-Path $orchestratorRoot ".venv") -Windows $windowsHost -CheckOnly
if (-not $runtime) {
    throw "HDS source deployment requires a compatible orchestrator/.venv (Windows: Python 3.13 x64; macOS/Linux: Python 3.13-3.14). Run: pwsh -NoProfile -File ./setup-prereqs.ps1"
}
$python = $runtime.executable

& $python -c "import requests; import azure.identity" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "orchestrator/.venv is incomplete. Run: pwsh -NoProfile -File ./setup-prereqs.ps1"
}

$arguments = @("-m", "activities.deploy_hds_source", "--workspace", $FabricWorkspaceName)
if ($WorkspaceId) {
    $arguments += @("--workspace-id", $WorkspaceId)
}
if ($ValidateOnly) {
    $arguments += "--validate-only"
}
if ($ContractOnly) {
    $arguments += "--contract-only"
}

Push-Location $orchestratorRoot
try {
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
