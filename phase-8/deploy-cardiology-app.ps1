[CmdletBinding()]
param (
    [Parameter(Mandatory)][string]$ResourceGroupName,
    [string]$Location = "eastus2",
    [hashtable]$Tags = @{},
    [string]$ExpectedTenantId = "8d038e6a-9b7d-4cb8-bbcf-e84dff156478",
    [string]$ExpectedSubscriptionId = "9bbee190-dc61-4c58-ab47-1275cb04018f",
    [string]$CardiologyAppPath = "",
    [string[]]$CardiologyAppUsers = @(),
    [string]$Prefix = "cardioe2e"
)

# Phase 8 — Cardiology App.
#
# Builds the private cardiology app (kfprugger/caldova-cardio-e2e, from a local
# checkout) into one image, deploys it to Azure Container Apps with
# bicep/cardiology-app.bicep, puts Entra sign-in in front of it, and does not
# report success until the live URL answers health and enforces sign-in.
#
# Only accounts assigned to the app registration can sign in: the deploying
# az user always, plus -CardiologyAppUsers. /api/health stays anonymous so the
# orchestrator can probe it.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptRoot
$Template = Join-Path $RepoRoot "bicep/cardiology-app.bicep"
$AppRepo = "kfprugger/caldova-cardio-e2e"
$AppBranch = "feature/cardiology-integration"
$ImageRepo = "cardiology-app"
$DefaultAccessRole = "00000000-0000-0000-0000-000000000000"

function Invoke-Az {
    param ([Parameter(Mandatory)][string[]]$Arguments, [switch]$AllowFailure)
    $lines = & az @Arguments --only-show-errors 2>&1
    $code = $LASTEXITCODE
    $out = ($lines | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
    $err = ($lines | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] }) -join "`n"
    # Only the command words are echoed: later arguments can carry a client secret.
    if ($code -ne 0 -and -not $AllowFailure) { throw "az $($Arguments[0..1] -join ' ') failed (exit $code): $err" }
    return [pscustomobject]@{ Code = $code; Out = $out; Err = $err }
}

function Get-Output {
    param ($Outputs, [string]$Name)
    # Azure CLI re-cases output names, so match case-insensitively.
    $prop = $Outputs.PSObject.Properties | Where-Object { $_.Name -ieq $Name } | Select-Object -First 1
    if (-not $prop -or -not $prop.Value.value) { throw "Deployment output '$Name' is missing." }
    return [string]$prop.Value.value
}

function Deploy-Template {
    param ([string]$Image, [bool]$UseRegistry, [string]$Revision, [string]$PrincipalId, [string]$AuthSecret = "")
    $paramsFile = New-TemporaryFile
    try {
        & chmod 600 $paramsFile  # may hold the sign-in client secret
        $parameters = @{
            prefix = @{ value = $Prefix }
            location = @{ value = $Location }
            principalId = @{ value = $PrincipalId }
            containerImage = @{ value = $Image }
            useRegistryImage = @{ value = $UseRegistry }
            revision = @{ value = $Revision }
            tags = @{ value = $Tags }
        }
        # A container app deployment replaces its whole secret set; pass the
        # sign-in secret back so an authenticated app keeps working.
        if ($AuthSecret) { $parameters.authClientSecret = @{ value = $AuthSecret } }
        @{
            '$schema' = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
            contentVersion = "1.0.0.0"
            parameters = $parameters
        } | ConvertTo-Json -Depth 10 | Set-Content -Path $paramsFile -Encoding utf8
        $name = "cardiology-app-$(Get-Date -Format 'yyyyMMddHHmmss')"
        $result = Invoke-Az @("deployment", "group", "create", "-g", $ResourceGroupName, "-n", $name,
            "--template-file", $Template, "--parameters", "@$paramsFile", "--query", "properties.outputs", "-o", "json")
        return $result.Out | ConvertFrom-Json
    } finally {
        Remove-Item $paramsFile -ErrorAction SilentlyContinue
    }
}

Write-Host "Phase 8: Cardiology App" -ForegroundColor Cyan

# ── Context ──────────────────────────────────────────────────────────────────
$account = (Invoke-Az @("account", "show", "-o", "json")).Out | ConvertFrom-Json
if ($account.tenantId -ne $ExpectedTenantId -or $account.id -ne $ExpectedSubscriptionId) {
    throw "Azure CLI is on tenant $($account.tenantId) / subscription $($account.id); expected $ExpectedTenantId / $ExpectedSubscriptionId."
}
Write-Host "  ✓ Azure CLI: $($account.user.name) on $($account.name)" -ForegroundColor Green
$deployerId = (Invoke-Az @("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")).Out.Trim()
if (-not $deployerId) { throw "Could not resolve the signed-in user's object id." }

if ((Invoke-Az @("group", "exists", "-n", $ResourceGroupName)).Out.Trim() -ne "true") {
    $tagArgs = @($Tags.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })
    $createArgs = @("group", "create", "-n", $ResourceGroupName, "-l", $Location, "-o", "none")
    if ($tagArgs.Count) { $createArgs += @("--tags") + $tagArgs }
    Invoke-Az $createArgs | Out-Null
    Write-Host "  ✓ Created resource group $ResourceGroupName" -ForegroundColor Green
}

# ── Source checkout ──────────────────────────────────────────────────────────
if (-not $CardiologyAppPath) { $CardiologyAppPath = Join-Path (Split-Path -Parent $RepoRoot) "caldova-cardio-e2e" }
if (-not (Test-Path (Join-Path $CardiologyAppPath ".git"))) {
    Write-Host "  Cloning $AppRepo to $CardiologyAppPath" -ForegroundColor Gray
    $cloned = $false
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        & gh repo clone $AppRepo $CardiologyAppPath -- --branch $AppBranch
        $cloned = $LASTEXITCODE -eq 0
    }
    if (-not $cloned) {
        & git clone --branch $AppBranch "https://github.com/$AppRepo.git" $CardiologyAppPath
        if ($LASTEXITCODE -ne 0) { throw "Could not clone $AppRepo. Sign in with 'gh auth login' as an account with access." }
    }
}
if (-not (Test-Path (Join-Path $CardiologyAppPath "Dockerfile"))) {
    throw "$CardiologyAppPath has no root Dockerfile; check out $AppBranch of $AppRepo."
}
$sha = (& git -C $CardiologyAppPath rev-parse --short=12 HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $sha) { throw "Could not read the checkout's commit." }
$dirty = & git -C $CardiologyAppPath status --porcelain
$tag = if ($dirty) { "$sha-dirty-$(Get-Date -Format 'yyyyMMddHHmmss')" } else { $sha }
Write-Host "  ✓ Source $CardiologyAppPath @ $tag" -ForegroundColor Green

# ── Infrastructure (placeholder image until the registry holds a build) ──────
$appName = "$Prefix-app"
$current = Invoke-Az @("containerapp", "show", "-g", $ResourceGroupName, "-n", $appName,
    "--query", "properties.template.containers[0].image", "-o", "tsv") -AllowFailure
$hasRegistryImage = $current.Code -eq 0 -and $current.Out -match "\.azurecr\.io/${ImageRepo}:"
$acrQuery = '[?tags."hls-workload"==''cardiology-app''].name | [0]'
if ($hasRegistryImage) {
    Write-Host "  = $appName already runs a registry image; building the new revision directly" -ForegroundColor Gray
} else {
    Write-Host "  Deploying infrastructure (placeholder image)..." -ForegroundColor Gray
    Deploy-Template -Image "mcr.microsoft.com/k8se/quickstart:latest" -UseRegistry $false -Revision "placeholder" -PrincipalId $deployerId | Out-Null
}
$acrName = (Invoke-Az @("acr", "list", "-g", $ResourceGroupName, "--query", $acrQuery, "-o", "tsv")).Out.Trim()
if (-not $acrName) { throw "The cardiology app registry was not found in $ResourceGroupName." }
Write-Host "  ✓ Infrastructure ready (registry $acrName)" -ForegroundColor Green

# An existing cardiology revision must never be served without sign-in. If the
# app runs the real image but its auth config is not enforcing exactly the
# intended policy (redirect everything except /api/health), take it offline
# before any other work: an earlier run may have failed part-way.
if ($hasRegistryImage) {
    $authShow = Invoke-Az @("containerapp", "auth", "show", "-g", $ResourceGroupName, "-n", $appName, "-o", "json") -AllowFailure
    $enforcing = $false
    try {
        $cfg = $authShow.Out | ConvertFrom-Json
        $enforcing = $cfg.platform.enabled -eq $true -and $cfg.globalValidation.unauthenticatedClientAction -eq "RedirectToLoginPage" -and
            (@($cfg.globalValidation.excludedPaths) -join ",") -eq "/api/health"
    } catch { $enforcing = $false }
    if (-not $enforcing) {
        $activeRevisions = (Invoke-Az @("containerapp", "revision", "list", "-g", $ResourceGroupName, "-n", $appName,
            "--query", "[?properties.active].name", "-o", "tsv")).Out -split "`n" | Where-Object { $_ }
        foreach ($revisionName in $activeRevisions) {
            Invoke-Az @("containerapp", "revision", "deactivate", "-g", $ResourceGroupName, "-n", $appName, "--revision", $revisionName, "-o", "none") | Out-Null
        }
        Write-Host "  ! $appName was not enforcing the intended sign-in policy; its revisions are offline until it is" -ForegroundColor Yellow
    }
}
# Offline = no active revision (quarantined now, or left offline by an earlier
# failed run). Nothing answers at the edge, so sign-in is verified on the control
# plane only, and the latest revision is reactivated after publishing.
$activeNow = @((Invoke-Az @("containerapp", "revision", "list", "-g", $ResourceGroupName, "-n", $appName,
    "--query", "[?properties.active].name", "-o", "tsv")).Out -split "`n" | Where-Object { $_ })
$offline = $activeNow.Count -eq 0

# ── Entra sign-in, enforced BEFORE the app image is served ───────────────────
# Until sign-in is verified the app serves only what it served before: the
# public placeholder on a fresh deployment, the previous signed-in revision on a
# rerun, or nothing if it was quarantined above. The cardiology app itself is
# never reachable anonymously, including when any step below fails.
$fqdn = (Invoke-Az @("containerapp", "show", "-g", $ResourceGroupName, "-n", $appName,
    "--query", "properties.configuration.ingress.fqdn", "-o", "tsv")).Out.Trim()
if (-not $fqdn) { throw "$appName has no ingress FQDN." }
$appUrl = "https://$fqdn"

$displayName = "cardiology-app-$ResourceGroupName"
$redirect = "$appUrl/.auth/login/aad/callback"
$appId = (Invoke-Az @("ad", "app", "list", "--display-name", $displayName, "--query", "[0].appId", "-o", "tsv")).Out.Trim()
if (-not $appId) {
    $appId = (Invoke-Az @("ad", "app", "create", "--display-name", $displayName, "--sign-in-audience", "AzureADMyOrg",
        "--web-redirect-uris", $redirect, "--enable-id-token-issuance", "true", "--query", "appId", "-o", "tsv")).Out.Trim()
    Write-Host "  ✓ Created app registration $displayName" -ForegroundColor Green
} else {
    Invoke-Az @("ad", "app", "update", "--id", $appId, "--web-redirect-uris", $redirect, "--enable-id-token-issuance", "true") | Out-Null
    Write-Host "  = Reusing app registration $displayName" -ForegroundColor Gray
}
$spId = (Invoke-Az @("ad", "sp", "show", "--id", $appId, "--query", "id", "-o", "tsv") -AllowFailure).Out.Trim()
if (-not $spId) { $spId = (Invoke-Az @("ad", "sp", "create", "--id", $appId, "--query", "id", "-o", "tsv")).Out.Trim() }
# Only assigned users may sign in.
Invoke-Az @("ad", "sp", "update", "--id", $spId, "--set", "appRoleAssignmentRequired=true") | Out-Null

# Reconcile to exactly the requested accounts: a user dropped from
# -CardiologyAppUsers loses access on the next deploy.
$allowed = @($deployerId)
foreach ($upn in $CardiologyAppUsers | Where-Object { $_ }) {
    $allowed += (Invoke-Az @("ad", "user", "show", "--id", $upn, "--query", "id", "-o", "tsv")).Out.Trim()
}
$allowed = @($allowed | Select-Object -Unique)
$assignmentsUrl = "https://graph.microsoft.com/v1.0/servicePrincipals/$spId/appRoleAssignedTo"
# Graph pages this collection; follow @odata.nextLink so no grant is missed.
function Get-AppAssignments {
    $all = @(); $url = $assignmentsUrl
    while ($url) {
        $page = (Invoke-Az @("rest", "--method", "GET", "--url", $url, "-o", "json")).Out | ConvertFrom-Json
        $all += @($page.value)
        $url = if ($page.PSObject.Properties["@odata.nextLink"]) { $page."@odata.nextLink" } else { $null }
    }
    return ,$all
}
$existingAssignments = Get-AppAssignments
foreach ($assignment in $existingAssignments | Where-Object { $allowed -notcontains $_.principalId }) {
    Invoke-Az @("rest", "--method", "DELETE", "--url", "$assignmentsUrl/$($assignment.id)", "-o", "none") | Out-Null
}
$currentPrincipals = @($existingAssignments | ForEach-Object { $_.principalId })
foreach ($principal in $allowed | Where-Object { $currentPrincipals -notcontains $_ }) {
    $bodyFile = New-TemporaryFile
    try {
        @{ principalId = $principal; resourceId = $spId; appRoleId = $DefaultAccessRole } | ConvertTo-Json | Set-Content $bodyFile -Encoding utf8
        Invoke-Az @("rest", "--method", "POST", "--url", $assignmentsUrl,
            "--headers", "Content-Type=application/json", "--body", "@$bodyFile", "-o", "none") | Out-Null
    } finally { Remove-Item $bodyFile -ErrorAction SilentlyContinue }
}
$verified = @(Get-AppAssignments | ForEach-Object { $_ } | ForEach-Object { $_.principalId } | Sort-Object -Unique)
if (($verified -join ",") -ne (@($allowed | Sort-Object) -join ",")) { throw "App assignments do not match the requested sign-in accounts." }
Write-Host "  ✓ Sign-in limited to $($verified.Count) assigned account(s)" -ForegroundColor Green

# A new credential per deploy, ADDED beside the working one so a failure before
# cutover leaves sign-in intact; superseded credentials are removed only after
# the new one is installed and verified. Never printed; cleared at script end.
$credentialName = "container-apps-auth-$(Get-Date -Format 'yyyyMMddHHmmss')"
$secret = (Invoke-Az @("ad", "app", "credential", "reset", "--id", $appId, "--append", "--display-name", $credentialName,
    "--years", "1", "--query", "password", "-o", "tsv")).Out.Trim()
Invoke-Az @("containerapp", "auth", "microsoft", "update", "-g", $ResourceGroupName, "-n", $appName,
    "--client-id", $appId, "--client-secret", $secret,
    # The v2 issuer names the tenant; the CLI rejects --tenant-id alongside it.
    "--issuer", "https://login.microsoftonline.com/$ExpectedTenantId/v2.0", "--yes", "-o", "none") | Out-Null
Invoke-Az @("containerapp", "auth", "update", "-g", $ResourceGroupName, "-n", $appName, "--enabled", "true",
    "--unauthenticated-client-action", "RedirectToLoginPage", "--redirect-provider", "azureactivedirectory",
    "--excluded-paths", "/api/health", "-o", "none") | Out-Null

# Easy Auth redirects only browser requests (others get 401), so probe as one,
# without following the redirect, and require it to land on this tenant's sign-in.
$handler = [System.Net.Http.HttpClientHandler]::new()
$handler.AllowAutoRedirect = $false
$browser = [System.Net.Http.HttpClient]::new($handler)
$browser.Timeout = [TimeSpan]::FromSeconds(15)
$browser.DefaultRequestHeaders.UserAgent.ParseAdd("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36")
$browser.DefaultRequestHeaders.Accept.ParseAdd("text/html,application/xhtml+xml")
$signIn = "https://login.microsoftonline.com/$ExpectedTenantId/"
function Test-SignInEnforced {
    try {
        $response = $browser.GetAsync("$appUrl/").GetAwaiter().GetResult()
        try {
            $location = if ($response.Headers.Location) { $response.Headers.Location.AbsoluteUri } else { "" }
            if ([int]$response.StatusCode -eq 302 -and $location.StartsWith($signIn) -and $location.Contains("client_id=$appId")) { return "" }
            return "unauthenticated browser request to / returned $([int]$response.StatusCode) (Location '$location')"
        } finally { $response.Dispose() }
    } catch { return "sign-in probe: $($_.Exception.Message)" }
}

# Control plane: auth is enforcing for this registration, only /api/health is
# anonymous, and the client secret it references exists (without it the redirect
# works but every sign-in callback fails).
function Test-AuthConfig {
    $cfg = (Invoke-Az @("containerapp", "auth", "show", "-g", $ResourceGroupName, "-n", $appName, "-o", "json")).Out | ConvertFrom-Json
    $registration = $cfg.identityProviders.azureActiveDirectory.registration
    $excluded = @($cfg.globalValidation.excludedPaths) -join ","
    $secretNames = @((Invoke-Az @("containerapp", "secret", "list", "-g", $ResourceGroupName, "-n", $appName, "--query", "[].name", "-o", "tsv")).Out -split "`n")
    if (-not ($cfg.platform.enabled -eq $true -and $cfg.globalValidation.unauthenticatedClientAction -eq "RedirectToLoginPage")) { return "auth is not redirecting unauthenticated visitors" }
    if ($registration.clientId -ne $appId) { return "auth uses client $($registration.clientId), expected $appId" }
    if ($excluded -ne "/api/health") { return "unexpected anonymous paths '$excluded'" }
    if ($secretNames -notcontains $registration.clientSecretSettingName) { return "client secret '$($registration.clientSecretSettingName)' is missing from the app" }
    return ""
}

try {
    $problem = Test-AuthConfig
    if ($problem) { throw "Container Apps sign-in is not correctly configured ($problem); the app image was not published." }
    # Then the live edge, unless offline (no active revision answers).
    if (-not $offline) {
        $deadline = (Get-Date).AddMinutes(5)
        $problem = Test-SignInEnforced
        while ($problem -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 10; $problem = Test-SignInEnforced }
        if ($problem) { throw "Sign-in was not enforced within 5 minutes; the app image was not published: $problem" }
    }
    Write-Host "  ✓ Entra sign-in enforced" -ForegroundColor Green


# ── Image ────────────────────────────────────────────────────────────────────
# $builtTags, not $tags: PowerShell names are case-insensitive and $Tags is the resource-tag parameter.
$builtTags = Invoke-Az @("acr", "repository", "show-tags", "-n", $acrName, "--repository", $ImageRepo, "-o", "tsv") -AllowFailure
if ($builtTags.Code -eq 0 -and ($builtTags.Out -split "`n") -contains $tag) {
    Write-Host "  = Image ${ImageRepo}:$tag already built" -ForegroundColor Gray
} else {
    Write-Host "  Building ${ImageRepo}:$tag in $acrName (a few minutes)..." -ForegroundColor Gray
    & az acr build --registry $acrName --image "${ImageRepo}:$tag" --file (Join-Path $CardiologyAppPath "Dockerfile") $CardiologyAppPath --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az acr build failed for ${ImageRepo}:$tag." }
    Write-Host "  ✓ Built ${ImageRepo}:$tag" -ForegroundColor Green
}
    # ── App revision (role assignments can take a minute to reach the pull) ──
    $loginServer = (Invoke-Az @("acr", "show", "-n", $acrName, "--query", "loginServer", "-o", "tsv")).Out.Trim()
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Deploy-Template -Image "$loginServer/${ImageRepo}:$tag" -UseRegistry $true -Revision $tag -PrincipalId $deployerId -AuthSecret $secret | Out-Null
            break
        } catch {
            if ($attempt -eq 3) { throw }
            Write-Host "  Revision deploy failed (attempt $attempt/3); waiting 30s for role propagation..." -ForegroundColor Yellow
            Start-Sleep -Seconds 30
        }
    }
    # An unchanged template creates no new revision, so an offline app would
    # stay offline. Sign-in is verified above; bring the latest revision back.
    if ($offline) {
        $latest = (Invoke-Az @("containerapp", "show", "-g", $ResourceGroupName, "-n", $appName,
            "--query", "properties.latestRevisionName", "-o", "tsv")).Out.Trim()
        $isActive = (Invoke-Az @("containerapp", "revision", "show", "-g", $ResourceGroupName, "-n", $appName,
            "--revision", $latest, "--query", "properties.active", "-o", "tsv")).Out.Trim()
        if ($isActive -ne "true") {
            Invoke-Az @("containerapp", "revision", "activate", "-g", $ResourceGroupName, "-n", $appName, "--revision", $latest, "-o", "none") | Out-Null
        }
        Write-Host "  ✓ Revision $latest back online behind sign-in" -ForegroundColor Green
    }
    Write-Host "  ✓ Revision $tag deployed to $appUrl" -ForegroundColor Green

    # ── Readiness gate: the new revision is live AND still behind sign-in ────
    $deadline = (Get-Date).AddMinutes(5)
    $healthOk = $false; $problem = "not checked"
    while ((Get-Date) -lt $deadline -and -not ($healthOk -and -not $problem)) {
        try {
            $health = Invoke-RestMethod -Uri "$appUrl/api/health" -TimeoutSec 15
            $healthOk = $health.status -eq "ok" -and $health.profile -eq "live" -and $health.revision -eq $tag
            $problem = if ($healthOk) { Test-SignInEnforced } else { "health reported status=$($health.status) profile=$($health.profile) revision=$($health.revision)" }
        } catch { $healthOk = $false; $problem = "health: $($_.Exception.Message)" }
        if (-not ($healthOk -and -not $problem)) { Start-Sleep -Seconds 10 }
    }
    if (-not $healthOk -or $problem) { throw "Cardiology app did not become ready at $appUrl within 5 minutes: $problem" }
    $problem = Test-AuthConfig
    if ($problem) { throw "Sign-in configuration broke during the revision deploy: $problem" }

    # Cut over: the new credential is live, so retire the ones it replaced.
    $superseded = @(((Invoke-Az @("ad", "app", "credential", "list", "--id", $appId, "-o", "json")).Out | ConvertFrom-Json) |
        Where-Object { "$($_.displayName)".StartsWith("container-apps-auth") -and $_.displayName -ne $credentialName })
    foreach ($old in $superseded) {
        Invoke-Az @("ad", "app", "credential", "delete", "--id", $appId, "--key-id", $old.keyId, "-o", "none") | Out-Null
    }
} finally { $browser.Dispose(); $secret = $null }

Write-Host "  ✓ $appUrl healthy on revision $tag; unauthenticated visitors are sent to sign-in" -ForegroundColor Green
Write-Host ""
Write-Host "CARDIOLOGY_APP_URL=$appUrl"
Write-Host "Phase 8 Cardiology App complete." -ForegroundColor Green
