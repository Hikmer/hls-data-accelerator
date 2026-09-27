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

# StrictMode throws on a missing property; control-plane JSON omits unset ones.
function Get-Prop {
    param ($Object, [string]$Name)
    if ($null -ne $Object -and $Object.PSObject.Properties[$Name]) { return $Object.$Name }
    return $null
}

# The exact access policy, read from the control plane. Anything else is weaker:
# another identity provider (its users are not subject to app assignment), an
# extra accepted token audience or another issuer (foreign tokens accepted), an
# extra anonymous path, an open post-login redirect, plain HTTP, CORS, or an
# extra externally exposed port (served around the sign-in sidecar). Settings
# that only narrow access (allowedApplications, jwtClaimChecks) are not checked.
function Get-AccessPolicyProblem {
    param ($Auth, $Ingress, [string]$ClientId = "")
    if (-not $Auth) { return "no sign-in configuration" }
    $validation = Get-Prop $Auth "globalValidation"
    if ((Get-Prop (Get-Prop $Auth "platform") "enabled") -ne $true) { return "sign-in is disabled" }
    if ((Get-Prop $validation "unauthenticatedClientAction") -ne "RedirectToLoginPage") { return "unauthenticated visitors are not redirected to sign-in" }
    if ((Get-Prop $validation "redirectToProvider") -ne "azureactivedirectory") { return "sign-in does not redirect to Entra" }
    if ((@(Get-Prop $validation "excludedPaths") -join ",") -cne "/api/health") { return "unexpected anonymous paths '$(@(Get-Prop $validation 'excludedPaths') -join ',')'" }
    $providers = Get-Prop $Auth "identityProviders"
    foreach ($provider in @(if ($providers) { $providers.PSObject.Properties } else { @() })) {
        if ($provider.Name -eq "azureActiveDirectory" -or $null -eq $provider.Value) { continue }
        $entries = if ($provider.Name -eq "customOpenIdConnectProviders") { @($provider.Value.PSObject.Properties | ForEach-Object { $_.Value }) } else { @($provider.Value) }
        foreach ($entry in $entries) { if ((Get-Prop $entry "enabled") -ne $false) { return "identity provider '$($provider.Name)' is enabled" } }
    }
    $aad = Get-Prop $providers "azureActiveDirectory"
    if (-not $aad -or (Get-Prop $aad "enabled") -eq $false) { return "Entra sign-in is not configured" }
    $registration = Get-Prop $aad "registration"
    if ((Get-Prop $registration "openIdIssuer") -cne "https://login.microsoftonline.com/$ExpectedTenantId/v2.0") { return "unexpected token issuer '$(Get-Prop $registration 'openIdIssuer')'" }
    if ($ClientId -and (Get-Prop $registration "clientId") -ne $ClientId) { return "sign-in uses client $(Get-Prop $registration 'clientId'), expected $ClientId" }
    if (@(Get-Prop (Get-Prop $aad "validation") "allowedAudiences" | Where-Object { $_ }).Count) { return "extra token audiences are accepted" }
    if (@(Get-Prop (Get-Prop $Auth "login") "allowedExternalRedirectUrls" | Where-Object { $_ }).Count) { return "post-login redirects to external URLs are allowed" }
    if ((Get-Prop (Get-Prop $Auth "httpSettings") "requireHttps") -eq $false) { return "sign-in does not require HTTPS" }
    if ((Get-Prop $Ingress "allowInsecure") -eq $true) { return "ingress allows plain HTTP" }
    if (Get-Prop $Ingress "corsPolicy") { return "ingress has a CORS policy" }
    if (@(Get-Prop $Ingress "additionalPortMappings" | Where-Object { $_ -and (Get-Prop $_ "external") -eq $true }).Count) { return "an additional port is exposed externally" }
    return ""
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
            chatModel = @{ value = $chat.Name }
            chatModelVersion = @{ value = $chat.Version }
            chatCapacity = @{ value = $chat.Capacity }
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

# ── Chat model ───────────────────────────────────────────────────────────────
# DataZoneStandard keeps inference in the US data zone. Its quota is counted
# across the whole zone (every US region reports the same usage), so another
# region adds no capacity. An environment keeps the model it already runs; a new
# one gets the first candidate the zone can still hold, sized to fit.
$modelCandidates = @(
    @{ Name = "gpt-5.6-luna"; Version = "2026-07-09" },
    @{ Name = "gpt-5.5"; Version = "2026-04-24" }
)
$wantedCapacity = 100; $minimumCapacity = 50
$chat = $null
$aiName = (Invoke-Az @("cognitiveservices", "account", "list", "-g", $ResourceGroupName, "--query", $acrQuery, "-o", "tsv")).Out.Trim()
if ($aiName) {
    $existingModels = @((Invoke-Az @("cognitiveservices", "account", "deployment", "list", "-g", $ResourceGroupName, "-n", $aiName, "-o", "json")).Out | ConvertFrom-Json | ForEach-Object { $_ })
    foreach ($candidate in $modelCandidates) {
        $found = @($existingModels | Where-Object { $_.name -eq $candidate.Name -and $_.sku.name -eq "DataZoneStandard" }) | Select-Object -First 1
        if ($found) {
            $chat = @{ Name = $found.name; Version = $found.properties.model.version; Capacity = [int]$found.sku.capacity }
            Write-Host "  = Keeping this environment's model $($chat.Name)" -ForegroundColor Gray
            break
        }
    }
}
if (-not $chat) {
    $usage = @((Invoke-Az @("cognitiveservices", "usage", "list", "-l", $Location, "-o", "json")).Out | ConvertFrom-Json | ForEach-Object { $_ })
    $offered = @((Invoke-Az @("cognitiveservices", "model", "list", "-l", $Location, "-o", "json")).Out | ConvertFrom-Json | ForEach-Object { $_ })
    foreach ($candidate in $modelCandidates) {
        $isOffered = @($offered | Where-Object { (Get-Prop $_.model "name") -eq $candidate.Name -and (Get-Prop $_.model "version") -eq $candidate.Version -and
            @(Get-Prop $_.model "skus" | ForEach-Object { Get-Prop $_ "name" }) -contains "DataZoneStandard" }).Count -gt 0
        $quota = @($usage | Where-Object { $_.name.value -eq "OpenAI.DataZoneStandard.$($candidate.Name)" }) | Select-Object -First 1
        if (-not $isOffered -or -not $quota) { Write-Host "  = $($candidate.Name) DataZoneStandard is not offered in $Location" -ForegroundColor Gray; continue }
        $free = [int][Math]::Floor([double]$quota.limit - [double]$quota.currentValue)
        if ([Math]::Min($wantedCapacity, $free) -ge $minimumCapacity) {
            $chat = @{ Name = $candidate.Name; Version = $candidate.Version; Capacity = [Math]::Min($wantedCapacity, $free) }
            break
        }
        Write-Host "  = $($candidate.Name): only $free of its US data-zone quota is free" -ForegroundColor Gray
    }
    if (-not $chat) { throw "No chat model fits the free US data-zone quota (need $minimumCapacity); free capacity or request more quota." }
}
Write-Host "  ✓ Model $($chat.Name) $($chat.Version), DataZoneStandard capacity $($chat.Capacity)" -ForegroundColor Green
if ($hasRegistryImage) {
    Write-Host "  = $appName already runs a registry image; building the new revision directly" -ForegroundColor Gray
} else {
    Write-Host "  Deploying infrastructure (placeholder image)..." -ForegroundColor Gray
    Deploy-Template -Image "mcr.microsoft.com/k8se/quickstart:latest" -UseRegistry $false -Revision "placeholder" -PrincipalId $deployerId | Out-Null
}
$acrName = (Invoke-Az @("acr", "list", "-g", $ResourceGroupName, "--query", $acrQuery, "-o", "tsv")).Out.Trim()
if (-not $acrName) { throw "The cardiology app registry was not found in $ResourceGroupName." }
Write-Host "  ✓ Infrastructure ready (registry $acrName)" -ForegroundColor Green

# An existing cardiology revision must never be served under a weaker policy. If
# the app runs the real image and its access policy is not exactly the intended
# one (Get-AccessPolicyProblem), take it offline before any other work: an
# earlier run may have failed part-way, or someone may have loosened it.
function Get-AppIngress {
    return (Invoke-Az @("containerapp", "show", "-g", $ResourceGroupName, "-n", $appName, "--query", "properties.configuration.ingress", "-o", "json")).Out | ConvertFrom-Json
}
function Stop-AppRevisions {
    $activeRevisions = (Invoke-Az @("containerapp", "revision", "list", "-g", $ResourceGroupName, "-n", $appName,
        "--query", "[?properties.active].name", "-o", "tsv")).Out -split "`n" | Where-Object { $_ }
    foreach ($revisionName in $activeRevisions) {
        Invoke-Az @("containerapp", "revision", "deactivate", "-g", $ResourceGroupName, "-n", $appName, "--revision", $revisionName, "-o", "none") | Out-Null
    }
}
if ($hasRegistryImage) {
    $authShow = Invoke-Az @("containerapp", "auth", "show", "-g", $ResourceGroupName, "-n", $appName, "-o", "json") -AllowFailure
    try { $weakness = Get-AccessPolicyProblem ($authShow.Out | ConvertFrom-Json) (Get-AppIngress) } catch { $weakness = "sign-in configuration unreadable: $($_.Exception.Message)" }
    if ($authShow.Code -ne 0) { $weakness = "sign-in configuration unreadable" }
    if ($weakness) {
        Stop-AppRevisions
        Write-Host "  ! $appName was not enforcing the intended sign-in policy ($weakness); its revisions are offline until it is" -ForegroundColor Yellow
    }
}
# Offline = no active revision (quarantined now, or left offline by an earlier
# failed run). Nothing answers at the edge, so sign-in is verified on the control
# plane only, and the latest revision is reactivated after publishing.
$activeNow = @((Invoke-Az @("containerapp", "revision", "list", "-g", $ResourceGroupName, "-n", $appName,
    "--query", "[?properties.active].name", "-o", "tsv")).Out -split "`n" | Where-Object { $_ })
$offline = $activeNow.Count -eq 0

# ── Entra sign-in, enforced BEFORE the app image is served ───────────────────
# Until sign-in is verified the app serves only what it served before: nothing
# public on a fresh deployment (the placeholder has internal ingress), the
# previous signed-in revision on a rerun, or nothing if it was quarantined above.
# The cardiology app itself is never reachable anonymously, including when any
# step below fails. The external FQDN is <app>.<environment domain>, known
# before ingress turns external, so the callback can be registered first.
$envDomain = (Invoke-Az @("containerapp", "env", "show", "-g", $ResourceGroupName, "-n", "$Prefix-cae",
    "--query", "properties.defaultDomain", "-o", "tsv")).Out.Trim()
if (-not $envDomain) { throw "The Container Apps environment $Prefix-cae has no default domain." }
$fqdn = "$appName.$envDomain"
$appUrl = "https://$fqdn"
$isExternal = (Get-Prop (Get-AppIngress) "external") -eq $true

# The registration is bound to this app by its sign-in callback (the Container
# Apps FQDN is unique) and marked with an owner tag naming this subscription,
# resource group, and app. Lookup is by exact name. It is reused only when its
# sole redirect URI is this app's exact callback (case-sensitive), or rebound
# when it carries the owner tag and its sole redirect is an earlier FQDN of the
# same app (the environment was recreated). It must have no owner other than
# the deploying user. Anything else is refused, never edited.
$displayName = "cardiology-app-$ResourceGroupName"
$redirect = "$appUrl/.auth/login/aad/callback"
$ownerTag = "hls-cardiology-app:$($account.id)/$($ResourceGroupName.ToLowerInvariant())/$appName"
$named = @((Invoke-Az @("ad", "app", "list", "--filter", "displayName eq '$displayName'",
    "--query", "[].{appId:appId,id:id,uris:web.redirectUris,tags:tags}", "-o", "json")).Out | ConvertFrom-Json | ForEach-Object { $_ } | Where-Object { $_ })
$bound = @($named | Where-Object { @($_.uris) -ccontains $redirect })
$earlierCallback = "^https://$([regex]::Escape($appName))\.[a-z0-9-]+\.[a-z0-9-]+\.azurecontainerapps\.io/\.auth/login/aad/callback$"
$owned = @($named | Where-Object { @($_.tags) -ccontains $ownerTag })
function Assert-DeployerOnlyOwner([string]$ObjectId) {
    $owners = @((Invoke-Az @("ad", "app", "owner", "list", "--id", $ObjectId, "--query", "[].id", "-o", "json")).Out | ConvertFrom-Json | ForEach-Object { $_ })
    $others = @($owners | Where-Object { $_ -ne $deployerId })
    if ($others.Count) { throw "App registration $displayName has other owners ($($others -join ', ')) who can change it; refusing to use it." }
}
if ($bound.Count -gt 1) { throw "$($bound.Count) app registrations named $displayName are bound to $redirect; remove the extras." }
if ($bound.Count -eq 1) {
    # Exclusive binding: a registration that also serves other callbacks is shared,
    # and its assignments are reconciled below, so it is refused, never edited.
    if (@($bound[0].uris).Count -ne 1) { throw "App registration $displayName also lists other redirect URIs; refusing to reuse a shared registration." }
    $registration = $bound[0]
    Assert-DeployerOnlyOwner $registration.id
    Write-Host "  = Reusing app registration $displayName" -ForegroundColor Gray
} elseif ($owned.Count -eq 1 -and $named.Count -eq 1 -and @($owned[0].uris).Count -eq 1 -and @($owned[0].uris)[0] -cmatch $earlierCallback) {
    $registration = $owned[0]
    Assert-DeployerOnlyOwner $registration.id
    Invoke-Az @("ad", "app", "update", "--id", $registration.appId, "--web-redirect-uris", $redirect) | Out-Null
    Write-Host "  ✓ Rebound app registration $displayName from $(@($owned[0].uris)[0]) to this app's callback" -ForegroundColor Green
} elseif ($named.Count -gt 0) {
    throw "App registration $displayName exists but is not bound to $redirect; refusing to take it over."
} else {
    $registration = (Invoke-Az @("ad", "app", "create", "--display-name", $displayName, "--sign-in-audience", "AzureADMyOrg",
        "--web-redirect-uris", $redirect, "--enable-id-token-issuance", "true", "--query", "{appId:appId,id:id,tags:tags}", "-o", "json")).Out | ConvertFrom-Json
    Write-Host "  ✓ Created app registration $displayName" -ForegroundColor Green
}
$appId = $registration.appId
# Single-tenant, ID tokens only (no implicit access tokens), and the owner tag.
Invoke-Az @("ad", "app", "update", "--id", $appId, "--sign-in-audience", "AzureADMyOrg",
    "--enable-id-token-issuance", "true", "--enable-access-token-issuance", "false") | Out-Null
if (@($registration.tags) -cnotcontains $ownerTag) {
    $tagFile = New-TemporaryFile
    try {
        @{ tags = @(@($registration.tags | Where-Object { $_ }) + $ownerTag) } | ConvertTo-Json | Set-Content $tagFile -Encoding utf8
        Invoke-Az @("rest", "--method", "PATCH", "--url", "https://graph.microsoft.com/v1.0/applications/$($registration.id)",
            "--headers", "Content-Type=application/json", "--body", "@$tagFile", "-o", "none") | Out-Null
    } finally { Remove-Item $tagFile -ErrorAction SilentlyContinue }
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

# The client secret Container Apps uses to redeem sign-in codes. A secret is
# proven live by a client-credentials token request as this app. Entra
# replicates credential changes gradually and, for minutes after one, refuses a
# valid secret intermittently (AADSTS7000215), so one token proves a secret and
# refusal counts only when it persists until -Until. Each attempt is one
# HttpClient POST cancelled at min(10 s, time left); PostAsync buffers the whole
# body before completing, so the deadline covers connection, headers, and body.
# The installed secret is reused while it authenticates; otherwise a credential
# is ADDED. Credentials are never deleted here, so a failed or overlapping run
# cannot revoke the secret the app is using. Never printed; cleared at script end.
$tokenClient = [System.Net.Http.HttpClient]::new()
$tokenClient.Timeout = [System.Threading.Timeout]::InfiniteTimeSpan  # each attempt carries its own deadline
# Everything that holds the secret or the HTTP clients runs inside this try, so
# every exit (including a rejected credential or a failed auth update) cleans up.
$browser = $null
try {
    function Test-ClientSecret([string]$Value, [datetime]$Until = (Get-Date).AddSeconds(30)) {
        if (-not $Value) { return $false }
        $form = "client_id=$appId&scope=$([uri]::EscapeDataString('https://graph.microsoft.com/.default'))&grant_type=client_credentials&client_secret=$([uri]::EscapeDataString($Value))"
        while (($left = ($Until - (Get-Date)).TotalMilliseconds) -gt 0) {
            $cancel = [System.Threading.CancellationTokenSource]::new([TimeSpan]::FromMilliseconds([Math]::Min(10000, $left)))
            $response = $null; $content = $null
            try {
                $content = [System.Net.Http.StringContent]::new($form, [System.Text.Encoding]::UTF8, "application/x-www-form-urlencoded")
                $response = $tokenClient.PostAsync("https://login.microsoftonline.com/$ExpectedTenantId/oauth2/v2.0/token", $content, $cancel.Token).GetAwaiter().GetResult()
                if ($response.IsSuccessStatusCode -and ($response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json).access_token) { return $true }
            } catch { } finally { if ($response) { $response.Dispose() }; if ($content) { $content.Dispose() }; $cancel.Dispose() }
            $left = ($Until - (Get-Date)).TotalMilliseconds
            if ($left -gt 0) { Start-Sleep -Milliseconds ([int][Math]::Min(5000, $left)) }
        }
        return $false
    }
    function Get-InstalledSecret {
        $auth = Invoke-Az @("containerapp", "auth", "show", "-g", $ResourceGroupName, "-n", $appName, "-o", "json") -AllowFailure
        try { $settingName = ($auth.Out | ConvertFrom-Json).identityProviders.azureActiveDirectory.registration.clientSecretSettingName } catch { $settingName = "" }
        if ($auth.Code -ne 0 -or -not $settingName) { return "" }
        $installed = Invoke-Az @("containerapp", "secret", "show", "-g", $ResourceGroupName, "-n", $appName,
            "--secret-name", $settingName, "--query", "value", "-o", "tsv") -AllowFailure
        if ($installed.Code -eq 0) { return $installed.Out.Trim() } else { return "" }
    }
    $secret = Get-InstalledSecret
    if (Test-ClientSecret $secret) {
        Write-Host "  = Reusing the installed sign-in secret; it authenticates as the app" -ForegroundColor DarkGray
    } else {
        $credentialName = "container-apps-auth-$(Get-Date -Format 'yyyyMMddHHmmss')"
        $secret = (Invoke-Az @("ad", "app", "credential", "reset", "--id", $appId, "--append", "--display-name", $credentialName,
            "--years", "1", "--query", "password", "-o", "tsv")).Out.Trim()
        if (-not (Test-ClientSecret $secret -Until (Get-Date).AddMinutes(3))) {
            throw "The new sign-in credential $credentialName was not accepted within 3 minutes."
        }
        Write-Host "  ✓ Added sign-in credential $credentialName" -ForegroundColor Green
    }
    # az reads the secret from a chmod-600 file (@file expansion), so it never
    # appears in a process argument list (the "=" form also keeps a leading "-"
    # from being read as a flag).
    $secretFile = New-TemporaryFile
    try {
        & chmod 600 $secretFile
        [System.IO.File]::WriteAllText($secretFile.FullName, $secret)
        Invoke-Az @("containerapp", "auth", "microsoft", "update", "-g", $ResourceGroupName, "-n", $appName,
            "--client-id", $appId, "--client-secret=@$($secretFile.FullName)",
            # The v2 issuer names the tenant; the CLI rejects --tenant-id alongside it.
            "--issuer", "https://login.microsoftonline.com/$ExpectedTenantId/v2.0", "--yes", "-o", "none") | Out-Null
    } finally { Remove-Item $secretFile -ErrorAction SilentlyContinue }
    # Then replace the whole auth config with exactly the intended policy, so a
    # loosened setting (another provider, an extra audience, an open redirect,
    # plain HTTP) is removed rather than left beside the fields the CLI updates.
    $authBody = @{ properties = @{
        platform = @{ enabled = $true }
        globalValidation = @{ unauthenticatedClientAction = "RedirectToLoginPage"; redirectToProvider = "azureactivedirectory"; excludedPaths = @("/api/health") }
        identityProviders = @{ azureActiveDirectory = @{
            enabled = $true
            registration = @{ clientId = $appId; clientSecretSettingName = "microsoft-provider-authentication-secret"; openIdIssuer = "https://login.microsoftonline.com/$ExpectedTenantId/v2.0" }
            validation = @{ defaultAuthorizationPolicy = @{ allowedApplications = @() } }
        } }
        login = @{ preserveUrlFragmentsForLogins = $false }
        httpSettings = @{ requireHttps = $true }
    } }
    $authFile = New-TemporaryFile
    try {
        $authBody | ConvertTo-Json -Depth 20 | Set-Content $authFile -Encoding utf8
        Invoke-Az @("rest", "--method", "PUT", "--headers", "Content-Type=application/json", "--body", "@$($authFile.FullName)", "-o", "none",
            "--url", "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/$ResourceGroupName/providers/Microsoft.App/containerApps/$appName/authConfigs/current?api-version=2024-03-01") | Out-Null
    } finally { Remove-Item $authFile -ErrorAction SilentlyContinue }

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
                # The redirect must name exactly this registration and this app's callback.
                # Assigned directly: an `if` expression would unroll the collection into strings.
                $query = $null
                if ($location) { $query = [System.Web.HttpUtility]::ParseQueryString(([uri]$location).Query) }
                if ([int]$response.StatusCode -eq 302 -and $location.StartsWith($signIn) -and
                    ((@($query.GetValues("client_id")) -join ",") -ceq $appId) -and ((@($query.GetValues("redirect_uri")) -join ",") -ceq $redirect)) { return "" }
                return "unauthenticated browser request to / returned $([int]$response.StatusCode) (Location '$location')"
            } finally { $response.Dispose() }
        } catch { return "sign-in probe: $($_.Exception.Message)" }
    }

    # Control plane: the exact access policy for this registration
    # (Get-AccessPolicyProblem), the registration itself (sole callback,
    # single-tenant, no implicit access tokens, owner tag, no other owner), and an
    # installed client secret that authenticates as the app (without it the
    # redirect works but every sign-in callback fails).
    # Before publishing, ingress is not yet ours: the publishing template deploy
    # replaces it (a weak ingress already quarantined the app above), so the
    # pre-publish gate checks everything except ingress (-BeforePublish).
    function Test-AuthConfig([switch]$BeforePublish) {
        $cfg = (Invoke-Az @("containerapp", "auth", "show", "-g", $ResourceGroupName, "-n", $appName, "-o", "json")).Out | ConvertFrom-Json
        $ingress = $null
        if (-not $BeforePublish) { $ingress = Get-AppIngress }
        $weakness = Get-AccessPolicyProblem $cfg $ingress $appId
        if ($weakness) { return $weakness }
        $settingName = Get-Prop (Get-Prop (Get-Prop (Get-Prop $cfg "identityProviders") "azureActiveDirectory") "registration") "clientSecretSettingName"
        $secretNames = @((Invoke-Az @("containerapp", "secret", "list", "-g", $ResourceGroupName, "-n", $appName, "--query", "[].name", "-o", "tsv")).Out -split "`n")
        if (-not $settingName -or $secretNames -notcontains $settingName) { return "client secret '$settingName' is missing from the app" }
        $app = (Invoke-Az @("ad", "app", "show", "--id", $appId, "-o", "json")).Out | ConvertFrom-Json
        $registered = @(Get-Prop (Get-Prop $app "web") "redirectUris" | Where-Object { $_ })
        if ($registered.Count -ne 1 -or $registered -cnotcontains $redirect) { return "registration $appId redirect URIs are not exactly the callback $redirect" }
        if ((Get-Prop $app "signInAudience") -ne "AzureADMyOrg") { return "registration $appId is not single-tenant" }
        if ((Get-Prop (Get-Prop (Get-Prop $app "web") "implicitGrantSettings") "enableAccessTokenIssuance") -eq $true) { return "registration $appId issues implicit access tokens" }
        if (@(Get-Prop $app "tags") -cnotcontains $ownerTag) { return "registration $appId lacks the owner tag $ownerTag" }
        $others = @((Invoke-Az @("ad", "app", "owner", "list", "--id", $app.id, "--query", "[].id", "-o", "json")).Out | ConvertFrom-Json | ForEach-Object { $_ } | Where-Object { $_ -ne $deployerId })
        if ($others.Count) { return "registration $appId has other owners ($($others -join ', '))" }
        if (-not (Test-ClientSecret (Get-InstalledSecret))) { return "the installed client secret does not authenticate as $appId" }
        return ""
    }

    $problem = Test-AuthConfig -BeforePublish
    if ($problem) { throw "Container Apps sign-in is not correctly configured ($problem); the app image was not published." }
    # Then the live edge, when something answers there: not offline (no active
    # revision) and not a fresh app still on internal ingress.
    if (-not $offline -and $isExternal) {
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
    # The template replaced ingress, so the full policy (ingress included) must
    # hold now, before any quarantined revision is brought back. If it does not,
    # take the app offline again rather than serve it.
    $problem = Test-AuthConfig
    if ($problem) {
        Stop-AppRevisions
        throw "The access policy does not hold after publishing ($problem); $appName is offline."
    }
    # An unchanged template creates no new revision, so an offline app would
    # stay offline. The full policy holds; bring the latest revision back.
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
} finally { if ($browser) { $browser.Dispose() }; $tokenClient.Dispose(); $secret = $null }

Write-Host "  ✓ $appUrl healthy on revision $tag; unauthenticated visitors are sent to sign-in" -ForegroundColor Green
Write-Host ""
Write-Host "CARDIOLOGY_APP_URL=$appUrl"
Write-Host "Phase 8 Cardiology App complete." -ForegroundColor Green
