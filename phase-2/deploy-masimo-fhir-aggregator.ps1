[CmdletBinding()]
param (
    [string]$ResourceGroupName = "rg-med-0906",
    [ValidatePattern('\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z')][string]$ExpectedTenantId = "8d038e6a-9b7d-4cb8-bbcf-e84dff156478",
    [ValidatePattern('\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z')][string]$ExpectedSubscriptionId = "9bbee190-dc61-4c58-ab47-1275cb04018f",
    [string]$EnvironmentName = "hds-dicom-env",
    [ValidatePattern('\A[a-zA-Z0-9]{5,50}\z')][string]$AcrName = "med0906frzkspw34dzciacr",
    [string]$IdentityName = "id-masimo-fhir-aggregator",
    [string]$JobName = "masimo-fhir-aggregator",
    [ValidatePattern('\A/subscriptions/[0-9a-f-]{36}/resourceGroups/[A-Za-z0-9._()-]+/providers/Microsoft\.HealthcareApis/workspaces/[a-z0-9]+/fhirservices/[a-z0-9]+\z')][string]$FhirServiceId = "/subscriptions/9bbee190-dc61-4c58-ab47-1275cb04018f/resourceGroups/rg-med-0906/providers/Microsoft.HealthcareApis/workspaces/hdwsfrzkspw34dzci/fhirservices/fhirfrzkspw34dzci",
    [ValidatePattern('\Ahttps://[a-z0-9-]+\.fhir\.azurehealthcareapis\.com\z')][string]$FhirUrl = "https://hdwsfrzkspw34dzci-fhirfrzkspw34dzci.fhir.azurehealthcareapis.com",
    [ValidatePattern('\Ahttps://[a-z0-9-]+(\.[a-z0-9-]+)?\.kusto\.fabric\.microsoft\.com\z')][string]$EventhouseQueryUri = "https://trd-0vj4c1a07qab5cxg8f.z0.kusto.fabric.microsoft.com",
    # Kept inert inside the ['...'] name literal of the Kusto management command.
    [ValidatePattern('\A[A-Za-z0-9_.-]{1,260}\z')][string]$EventhouseDatabase = "MasimoEventhouse",
    [hashtable]$Tags = @{},
    [int]$ExecutionTimeoutMinutes = 10
)

# Phase 2 — Masimo FHIR aggregator.
#
# Deploys masimo-fhir-aggregator/ as a scheduled Azure Container Apps Job
# (bicep/masimo-fhir-aggregator.bicep, cron */5) that writes 5-minute Masimo
# telemetry aggregates from the Eventhouse into FHIR as Observations.
#
# Every step is create-only-if-absent, so re-running is safe:
#   1. user-assigned identity -IdentityName
#   2. AcrPull on -AcrName and FHIR Data Contributor on -FhirServiceId
#   3. Kusto database viewer on -EventhouseDatabase (least privilege; not workspace Viewer)
#   4. image masimo-fhir-aggregator:<git sha>[-dirty-<timestamp>] built in ACR
#   5. the job itself, then one manual execution that must succeed

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptRoot
$Template = Join-Path $RepoRoot "bicep/masimo-fhir-aggregator.bicep"
$SourcePath = Join-Path $RepoRoot "masimo-fhir-aggregator"
$ImageRepo = "masimo-fhir-aggregator"
$AcrPull = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
$FhirDataContributor = "5a1fc7df-4bf1-4951-a576-89034ee01acd"

function Invoke-Az {
    param ([Parameter(Mandatory)][string[]]$Arguments, [switch]$AllowFailure)
    $lines = & az @Arguments --only-show-errors 2>&1
    $code = $LASTEXITCODE
    $out = ($lines | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
    $err = ($lines | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] }) -join "`n"
    # Only the command words are echoed: later arguments can carry a token.
    if ($code -ne 0 -and -not $AllowFailure) { throw "az $($Arguments[0..1] -join ' ') failed (exit $code): $err" }
    return [pscustomobject]@{ Code = $code; Out = $out; Err = $err }
}

# Exact-scope assignments only; a new identity's service principal can take a
# minute to replicate, so creation retries on PrincipalNotFound.
function Set-RoleAssignment {
    param ([string]$PrincipalId, [string]$Role, [string]$Scope, [string]$Label)
    $scopes = @((Invoke-Az @("role", "assignment", "list", "--scope", $Scope, "--assignee-object-id", $PrincipalId,
        "--role", $Role, "--query", "[].scope", "-o", "json")).Out | ConvertFrom-Json | ForEach-Object { $_ })
    if (@($scopes | Where-Object { $_ -eq $Scope }).Count) {
        Write-Host "  = $Label already assigned" -ForegroundColor Gray
        return
    }
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        $result = Invoke-Az @("role", "assignment", "create", "--assignee-object-id", $PrincipalId,
            "--assignee-principal-type", "ServicePrincipal", "--role", $Role, "--scope", $Scope, "-o", "none") -AllowFailure
        if ($result.Code -eq 0) { Write-Host "  ✓ $Label assigned" -ForegroundColor Green; return }
        if ($result.Err -notmatch 'PrincipalNotFound|does not exist in the directory' -or $attempt -eq 6) {
            throw "Could not assign ${Label}: $($result.Err)"
        }
        Write-Host "  Waiting 20s for the identity to replicate ($attempt/6)..." -ForegroundColor Yellow
        Start-Sleep -Seconds 20
    }
}

function Invoke-KustoMgmtQuery {
    param ([string]$Command, [hashtable]$Headers)
    $body = @{ db = $EventhouseDatabase; csl = $Command } | ConvertTo-Json -Compress
    $response = Invoke-RestMethod -Uri "$EventhouseQueryUri/v1/rest/mgmt" -Method POST -Headers $Headers -Body $body
    $table = $response.Tables[0]
    $columns = @($table.Columns | ForEach-Object { $_.ColumnName })
    return @($table.Rows | ForEach-Object {
        $row = $_; $record = [ordered]@{}
        for ($i = 0; $i -lt $columns.Count; $i++) { $record[$columns[$i]] = $row[$i] }
        [pscustomobject]$record
    })
}

Write-Host "Phase 2: Masimo FHIR aggregator" -ForegroundColor Cyan

# ── Context ──────────────────────────────────────────────────────────────────
$account = (Invoke-Az @("account", "show", "-o", "json")).Out | ConvertFrom-Json
if ($account.tenantId -ne $ExpectedTenantId -or $account.id -ne $ExpectedSubscriptionId) {
    throw "Azure CLI is on tenant $($account.tenantId) / subscription $($account.id); expected $ExpectedTenantId / $ExpectedSubscriptionId."
}
Write-Host "  ✓ Azure CLI: $($account.user.name) on $($account.name)" -ForegroundColor Green
$environment = (Invoke-Az @("containerapp", "env", "show", "-g", $ResourceGroupName, "-n", $EnvironmentName,
    "--query", "{id:id, location:location}", "-o", "json")).Out | ConvertFrom-Json
$acr = (Invoke-Az @("acr", "show", "-g", $ResourceGroupName, "-n", $AcrName,
    "--query", "{id:id, loginServer:loginServer}", "-o", "json")).Out | ConvertFrom-Json
Write-Host "  ✓ Environment $EnvironmentName ($($environment.location)); registry $($acr.loginServer)" -ForegroundColor Green

# ── Identity ─────────────────────────────────────────────────────────────────
$identityShow = Invoke-Az @("identity", "show", "-g", $ResourceGroupName, "-n", $IdentityName, "-o", "json") -AllowFailure
if ($identityShow.Code -eq 0) {
    $identity = $identityShow.Out | ConvertFrom-Json
    Write-Host "  = Identity $IdentityName exists" -ForegroundColor Gray
} elseif ($identityShow.Err -match 'ResourceNotFound') {
    $createArgs = @("identity", "create", "-g", $ResourceGroupName, "-n", $IdentityName, "-l", $environment.location, "-o", "json")
    $tagArgs = @($Tags.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })
    if ($tagArgs.Count) { $createArgs += @("--tags") + $tagArgs }
    $identity = (Invoke-Az $createArgs).Out | ConvertFrom-Json
    Write-Host "  ✓ Identity $IdentityName created" -ForegroundColor Green
} else {
    throw "Could not read identity ${IdentityName}: $($identityShow.Err)"
}

# ── Access ───────────────────────────────────────────────────────────────────
Set-RoleAssignment -PrincipalId $identity.principalId -Role $AcrPull -Scope $acr.id -Label "AcrPull on $AcrName"
Set-RoleAssignment -PrincipalId $identity.principalId -Role $FhirDataContributor -Scope $FhirServiceId -Label "FHIR Data Contributor"

$kustoToken = (Invoke-Az @("account", "get-access-token", "--subscription", $ExpectedSubscriptionId,
    "--resource", $EventhouseQueryUri, "--query", "accessToken", "-o", "tsv")).Out.Trim()
$kustoHeaders = @{ Authorization = "Bearer $kustoToken"; "Content-Type" = "application/json" }
try {
    $fqn = "aadapp=$($identity.clientId)"
    $principals = Invoke-KustoMgmtQuery -Command ".show database ['$EventhouseDatabase'] principals" -Headers $kustoHeaders
    $isViewer = @($principals | Where-Object {
        $_.Role -match '^Database .+ Viewer$' -and ($_.PrincipalFQN -ieq $fqn -or $_.PrincipalFQN -like "$fqn;*")
    }).Count -gt 0
    if ($isViewer) {
        Write-Host "  = Kusto viewer on $EventhouseDatabase already granted" -ForegroundColor Gray
    } else {
        $null = Invoke-KustoMgmtQuery -Headers $kustoHeaders -Command (
            ".add database ['$EventhouseDatabase'] viewers ('$fqn;$ExpectedTenantId') 'masimo-fhir-aggregator'")
        Write-Host "  ✓ Kusto viewer on $EventhouseDatabase granted" -ForegroundColor Green
    }
} finally { $kustoToken = $null; $kustoHeaders = $null }

# ── Image ────────────────────────────────────────────────────────────────────
$sha = (& git -C $RepoRoot rev-parse --short=12 HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $sha) { throw "Could not read the repository's commit." }
$dirty = & git -C $RepoRoot status --porcelain
$tag = if ($dirty) { "$sha-dirty-$(Get-Date -Format 'yyyyMMddHHmmss')" } else { $sha }
$builtTags = Invoke-Az @("acr", "repository", "show-tags", "-n", $AcrName, "--repository", $ImageRepo, "-o", "tsv") -AllowFailure
if ($builtTags.Code -eq 0 -and ($builtTags.Out -split "`n") -contains $tag) {
    Write-Host "  = Image ${ImageRepo}:$tag already built" -ForegroundColor Gray
} else {
    Write-Host "  Building ${ImageRepo}:$tag in $AcrName..." -ForegroundColor Gray
    & az acr build --registry $AcrName --image "${ImageRepo}:$tag" $SourcePath --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az acr build failed for ${ImageRepo}:$tag." }
    Write-Host "  ✓ Built ${ImageRepo}:$tag" -ForegroundColor Green
}

# ── Job (registry pull can wait on AcrPull propagation) ──────────────────────
$paramsFile = New-TemporaryFile
try {
    @{
        '$schema' = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
        contentVersion = "1.0.0.0"
        parameters = @{
            location = @{ value = $environment.location }
            environmentName = @{ value = $EnvironmentName }
            acrName = @{ value = $AcrName }
            imageName = @{ value = "$($acr.loginServer)/${ImageRepo}:$tag" }
            identityId = @{ value = $identity.id }
            identityClientId = @{ value = $identity.clientId }
            fhirServiceUrl = @{ value = $FhirUrl }
            kustoQueryUri = @{ value = $EventhouseQueryUri }
            kustoDatabase = @{ value = $EventhouseDatabase }
            jobName = @{ value = $JobName }
            resourceTags = @{ value = $Tags }
        }
    } | ConvertTo-Json -Depth 10 | Set-Content -Path $paramsFile -Encoding utf8
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-Az @("deployment", "group", "create", "-g", $ResourceGroupName, "-n", "masimo-fhir-aggregator-$(Get-Date -Format 'yyyyMMddHHmmss')",
                "--template-file", $Template, "--parameters", "@$paramsFile", "-o", "none") | Out-Null
            break
        } catch {
            if ($attempt -eq 3) { throw }
            Write-Host "  Job deploy failed (attempt $attempt/3); waiting 30s for role propagation..." -ForegroundColor Yellow
            Start-Sleep -Seconds 30
        }
    }
} finally {
    Remove-Item $paramsFile -ErrorAction SilentlyContinue
}
Write-Host "  ✓ Job $JobName on ${ImageRepo}:$tag (cron */5 * * * *)" -ForegroundColor Green

# ── One manual execution must succeed ────────────────────────────────────────
$execution = ((Invoke-Az @("containerapp", "job", "start", "-g", $ResourceGroupName, "-n", $JobName, "-o", "json")).Out | ConvertFrom-Json).name
if (-not $execution) { throw "Starting $JobName returned no execution name." }
Write-Host "  Started execution $execution" -ForegroundColor Gray
$deadline = (Get-Date).AddMinutes($ExecutionTimeoutMinutes)
$status = ""
while ((Get-Date) -lt $deadline) {
    $status = (Invoke-Az @("containerapp", "job", "execution", "show", "-g", $ResourceGroupName, "-n", $JobName,
        "--job-execution-name", $execution, "--query", "properties.status", "-o", "tsv")).Out.Trim()
    if ($status -in @("Succeeded", "Failed", "Stopped", "Degraded")) { break }
    Start-Sleep -Seconds 15
}
if ($status -ne "Succeeded") { throw "Execution $execution of $JobName ended as '$status' (see the job's console logs)." }
Write-Host "  ✓ Execution $execution succeeded" -ForegroundColor Green

Write-Host ""
Write-Host "MASIMO_FHIR_AGGREGATOR_JOB=$JobName"
Write-Host "MASIMO_FHIR_AGGREGATOR_IMAGE=$($acr.loginServer)/${ImageRepo}:$tag"
Write-Host "Phase 2 Masimo FHIR aggregator complete." -ForegroundColor Green
