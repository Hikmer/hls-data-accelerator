"""Behavioral deployment validation keyed to the requested feature set."""

from __future__ import annotations

import base64
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from shared.models import (
    CARDIOLOGY_EVENTHOUSE_DATABASE,
    CARDIOLOGY_EVENTHOUSE_QUERY_URI,
    CARDIOLOGY_FABRIC_GOLD_DATABASE,
    CARDIOLOGY_FABRIC_SQL_HOST,
    CARDIOLOGY_FABRIC_WORKSPACE_ID,
    CARDIOLOGY_FHIR_SERVICE_ID,
    CARDIOLOGY_FHIR_URL,
)


def _check(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "pass" if passed else "fail", "detail": detail}


def _items(resources: dict[str, Any], item_type: str, *name_parts: str) -> list[dict[str, Any]]:
    wanted_type = item_type.lower()
    parts = tuple(part.lower() for part in name_parts)
    return [
        item
        for item in resources.get("fabric") or []
        if str(item.get("type") or "").lower() == wanted_type
        and (not parts or all(part in str(item.get("name") or "").lower() for part in parts))
    ]


def _azure(resources: dict[str, Any], full_type: str, *name_parts: str) -> list[dict[str, Any]]:
    wanted_type = full_type.lower()
    parts = tuple(part.lower() for part in name_parts)
    return [
        resource
        for resource in resources.get("azure") or []
        if str(resource.get("fullType") or resource.get("type") or "").lower() == wanted_type
        and (not parts or all(part in str(resource.get("name") or "").lower() for part in parts))
    ]


CARDIOLOGY_WORKLOAD_TAG = "hls-workload"
CARDIOLOGY_WORKLOAD_VALUE = "cardiology-app"


def cardiology_app_resources(azure_resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Container Apps tagged as the Phase 8 cardiology workload."""
    return [
        resource
        for resource in azure_resources or []
        if str(resource.get("fullType") or resource.get("type") or "").lower() == "microsoft.app/containerapps"
        and str((resource.get("tags") or {}).get(CARDIOLOGY_WORKLOAD_TAG) or "").strip().lower() == CARDIOLOGY_WORKLOAD_VALUE
    ]


def cardiology_app_expected(config: dict[str, Any]) -> bool:
    """Phase 8 runs in a full deploy unless skipped, and in phase8-only mode.

    A resumed run that reused a verified app (cardiology_app_reused) skips the
    deployment but still validates the app it relies on.
    """
    if config.get("phase8_only") or config.get("cardiology_app_reused"):
        return True
    if config.get("skip_cardiology_app", False):
        return False
    return not any(config.get(field) for field in ("phase2_only", "phase3_only", "phase4_only", "phase7_only"))


def _legacy_continuation_requires_fabric(config: dict[str, Any]) -> bool:
    """Recognize runs created before reuse_fabric_rti existed."""
    if not config.get("continue_from_instance_id"):
        return False
    downstream_skip_fields = (
        "skip_rti_phase2",
        "skip_data_agents",
        "skip_ontology",
        "skip_activator",
        "skip_quality_measures",
        "skip_phase7",
    )
    return any(not config.get(field, False) for field in downstream_skip_fields)


def fabric_features_expected(config: dict[str, Any]) -> bool:
    return bool(
        not config.get("skip_fabric", False)
        or config.get("reuse_fabric_rti", False)
        or _legacy_continuation_requires_fabric(config)
    )


def fabric_runtime_expected(config: dict[str, Any]) -> bool:
    return bool(not config.get("scaffolding_only", False) and fabric_features_expected(config))

def effective_validation_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the feature set that the selected phase-only mode actually runs."""
    effective = dict(config)
    if any(effective.get(field) for field in ("phase2_only", "phase3_only", "phase4_only", "phase7_only", "phase8_only")):
        effective["continue_from_instance_id"] = ""
        effective["cardiology_app_reused"] = False
    if effective.get("phase2_only"):
        effective.update(skip_data_agents=True, skip_imaging=True, skip_ontology=True, skip_activator=True, skip_quality_measures=True, skip_phase7=True, skip_cardiology_app=True)
    elif effective.get("phase3_only"):
        effective.update(skip_fabric=True, reuse_fabric_rti=False, skip_hds_pipelines=True, skip_data_agents=True, skip_ontology=True, skip_activator=True, skip_quality_measures=True, skip_phase7=True, skip_cardiology_app=True)
    elif effective.get("phase4_only"):
        effective.update(skip_fabric=True, reuse_fabric_rti=False, skip_hds_pipelines=True, skip_imaging=True, skip_quality_measures=True, skip_phase7=True, skip_cardiology_app=True)
    elif effective.get("phase7_only"):
        effective.update(skip_fabric=True, reuse_fabric_rti=False, skip_hds_pipelines=True, skip_data_agents=True, skip_imaging=True, skip_ontology=True, skip_activator=True, skip_quality_measures=True, skip_phase7=False, skip_cardiology_app=True)
    elif effective.get("phase8_only"):
        effective.update(skip_fabric=True, reuse_fabric_rti=False, skip_hds_pipelines=True, skip_data_agents=True, skip_imaging=True, skip_ontology=True, skip_activator=True, skip_quality_measures=True, skip_phase7=True, skip_cardiology_app=False)
    return effective


def feature_presence_checks(resources: dict[str, Any], config: dict[str, Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    workspace = resources.get("workspace") or {}
    workspace_exists = bool(workspace.get("id"))

    if fabric_features_expected(config):
        rti_requirements = (
            ("Eventstream", ("masimo", "telemetry"), "Masimo Eventstream"),
            ("Eventhouse", ("masimo",), "Masimo Eventhouse"),
            ("KQLDatabase", ("masimo",), "Masimo KQL database"),
            ("KQLDashboard", ("masimo",), "Masimo KQL dashboard"),
        )
        checks.append(_check("Fabric workspace exists", workspace_exists, workspace.get("name", "Workspace missing")))
        for item_type, parts, label in rti_requirements:
            found = _items(resources, item_type, *parts)
            checks.append(_check(label, bool(found), found[0].get("name", "Missing") if found else "Missing"))

    if config.get("scaffolding_only", False) or not config.get("skip_hds_pipelines", False):
        for item_type, parts, label in (
            ("Lakehouse", ("healthcare1_msft_admin",), "HDS admin lakehouse"),
            ("Lakehouse", ("healthcare1_msft_bronze",), "HDS Bronze lakehouse"),
            ("Lakehouse", ("healthcare1_msft_silver",), "HDS Silver lakehouse"),
            ("Lakehouse", ("healthcare1_msft_gold_omop",), "HDS Gold OMOP lakehouse"),
            ("DataPipeline", ("healthcare1_msft_clinical_data_foundation_ingestion",), "Clinical foundation pipeline"),
            ("DataPipeline", ("healthcare1_msft_imaging_with_clinical_foundation_ingestion",), "Imaging pipeline"),
            ("DataPipeline", ("healthcare1_msft_omop_analytics",), "OMOP pipeline"),
        ):
            found = _items(resources, item_type, *parts)
            checks.append(_check(label, bool(found), found[0].get("name", "Missing") if found else "Missing"))

    if not config.get("skip_data_agents", False):
        for name in ("Patient 360", "Clinical Triage"):
            found = _items(resources, "DataAgent", name.lower())
            checks.append(_check(f"Data Agent: {name}", bool(found), found[0].get("name", "Missing") if found else "Missing"))

    if not config.get("skip_imaging", False):
        for item_type, parts, label in (
            ("Report", ("imagingreport",), "Imaging report"),
            ("SemanticModel", ("imagingreport",), "Imaging semantic model"),
            ("Lakehouse", ("reporting", "gold"), "Imaging reporting lakehouse"),
        ):
            found = _items(resources, item_type, *parts)
            checks.append(_check(label, bool(found), found[0].get("name", "Missing") if found else "Missing"))
        checks.append(_check("DICOM proxy Container App", bool(_azure(resources, "Microsoft.App/containerApps", "dicom", "proxy")), "Required by OHIF"))
        checks.append(_check("OHIF Static Web App", bool(_azure(resources, "Microsoft.Web/staticSites", "dicom", "ohif")), "Required by imaging report links"))

    if not config.get("skip_ontology", False):
        found = _items(resources, "Ontology", "clinicaldeviceontology")
        checks.append(_check("Clinical device ontology", bool(found), found[0].get("name", "Missing") if found else "Missing"))

    if not config.get("skip_activator", False) and config.get("alert_email"):
        found = _items(resources, "Reflex")
        checks.append(_check("Clinical alert Activator", bool(found), found[0].get("name", "Missing") if found else "Missing"))

    if not config.get("skip_quality_measures", False):
        report = _items(resources, "Report", "population health", "quality dashboard")
        model = _items(resources, "SemanticModel", "population health", "quality semantic model")
        checks.append(_check("Population health quality report", bool(report), report[0].get("name", "Missing") if report else "Missing"))
        checks.append(_check("Population health quality semantic model", bool(model), model[0].get("name", "Missing") if model else "Missing"))
        if not config.get("skip_activator", False) and config.get("alert_email"):
            readmission = _items(resources, "Reflex", "readmissionriskalert")
            checks.append(_check("Readmission risk Activator", bool(readmission), readmission[0].get("name", "Missing") if readmission else "Missing"))

    if not config.get("skip_phase7", False):
        if not config.get("skip_payer_rti", False) and not config.get("scaffolding_only", False):
            claim_emulator = _azure(resources, "Microsoft.ContainerInstance/containerGroups", "claim", "emulator")
            checks.append(_check("Payer claim emulator", bool(claim_emulator), claim_emulator[0].get("name", "Missing") if claim_emulator else "Missing"))
        if not config.get("skip_ops_agent", False):
            for name in ("HealthcareOpsAgent", "Payer Ops Triage"):
                found = _items(resources, "DataAgent", name.lower()) + _items(resources, "OperationsAgent", name.lower())
                checks.append(_check(f"Payer agent: {name}", bool(found), found[0].get("name", "Missing") if found else "Missing"))
        if not config.get("skip_graph_agent", False):
            found = _items(resources, "DataAgent", "healthcare graph agent")
            checks.append(_check("Healthcare Graph Agent", bool(found), found[0].get("name", "Missing") if found else "Missing"))
        if not config.get("skip_payer_activator", False) and config.get("payer_ops_email"):
            found = _items(resources, "Reflex", "payer")
            checks.append(_check("Payer operations Activator", bool(found), found[0].get("name", "Missing") if found else "Missing"))

    if cardiology_app_expected(config):
        apps = cardiology_app_resources(resources.get("azure") or [])
        checks.append(_check(
            "Cardiology app Container App",
            bool(apps),
            apps[0].get("name", "Missing") if apps else f"No Microsoft.App/containerApps resource tagged {CARDIOLOGY_WORKLOAD_TAG}={CARDIOLOGY_WORKLOAD_VALUE}",
        ))

    return checks


def _resource_group(resource: dict[str, Any]) -> str:
    match = re.search(r"/resourceGroups/([^/]+)", str(resource.get("id") or ""), flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _url_bytes(url: str, *, timeout: int = 20) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "med-device-deployment-validator/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return response.read()


def _swa_check(resources: dict[str, Any], config: dict[str, Any], az_run: Callable[..., Any]) -> dict[str, str]:
    sites = _azure(resources, "Microsoft.Web/staticSites", "dicom", "ohif")
    if not sites:
        return _check("OHIF HTTP availability", False, "Static Web App missing")
    site = sites[0]
    args = ["az", "staticwebapp", "show", "--name", site["name"], "--resource-group", _resource_group(site), "--query", "defaultHostname", "-o", "tsv"]
    subscription = config.get("expected_subscription_id")
    if subscription:
        args.extend(["--subscription", subscription])
    proc = az_run(args)
    hostname = proc.stdout.strip() if proc.returncode == 0 else ""
    if not hostname:
        return _check("OHIF HTTP availability", False, "Could not resolve Static Web App hostname")
    try:
        index = _url_bytes(f"https://{hostname}/").decode("utf-8", errors="replace")
        bundle_match = re.search(r'<script[^>]+src=["\']([^"\']+\.js)["\']', index, flags=re.IGNORECASE)
        if not bundle_match:
            return _check("OHIF HTTP availability", False, "index.html has no JavaScript entry bundle")
        bundle_url = urllib.request.urljoin(f"https://{hostname}/", bundle_match.group(1))
        bundle = _url_bytes(bundle_url)
        return _check("OHIF HTTP availability", len(bundle) > 1024, f"HTTP 200; entry bundle {len(bundle)} bytes")
    except Exception as exc:
        return _check("OHIF HTTP availability", False, f"{type(exc).__name__}: {exc}")


def _proxy_check(resources: dict[str, Any], config: dict[str, Any], az_run: Callable[..., Any]) -> dict[str, str]:
    apps = _azure(resources, "Microsoft.App/containerApps", "dicom", "proxy")
    if not apps:
        return _check("DICOM proxy health", False, "Container App missing")
    app = apps[0]
    args = ["az", "containerapp", "show", "--name", app["name"], "--resource-group", _resource_group(app), "--query", "properties.configuration.ingress.fqdn", "-o", "tsv"]
    subscription = config.get("expected_subscription_id")
    if subscription:
        args.extend(["--subscription", subscription])
    proc = az_run(args)
    hostname = proc.stdout.strip() if proc.returncode == 0 else ""
    if not hostname:
        return _check("DICOM proxy health", False, "Could not resolve Container App hostname")
    last_error = "Proxy health did not respond"
    for attempt in range(1, 4):
        try:
            payload = json.loads(_url_bytes(f"https://{hostname}/health", timeout=30))
            healthy = payload.get("status") == "ok" and int(payload.get("studies") or 0) > 0
            return _check("DICOM proxy health", healthy, f"status={payload.get('status')}, studies={payload.get('studies', 0)}")
        except Exception as exc:
            last_error = f"attempt {attempt}/3 {type(exc).__name__}: {exc}"
            if attempt < 3:
                time.sleep(10)
    return _check("DICOM proxy health", False, last_error)


def _proxy_viewer_check(resources: dict[str, Any], config: dict[str, Any], az_run: Callable[..., Any]) -> dict[str, str]:
    apps = _azure(resources, "Microsoft.App/containerApps", "dicom", "proxy")
    if not apps:
        return _check("OHIF HTTP availability", False, "DICOM proxy Container App missing")
    app = apps[0]
    args = ["az", "containerapp", "show", "--name", app["name"], "--resource-group", _resource_group(app), "--query", "properties.configuration.ingress.fqdn", "-o", "tsv"]
    subscription = config.get("expected_subscription_id")
    if subscription:
        args.extend(["--subscription", subscription])
    proc = az_run(args)
    hostname = proc.stdout.strip() if proc.returncode == 0 else ""
    if not hostname:
        return _check("OHIF HTTP availability", False, "Could not resolve proxy-hosted viewer hostname")
    try:
        index = _url_bytes(f"https://{hostname}/").decode("utf-8", errors="replace")
        bundle_match = re.search(r'<script[^>]+src=["\']([^"\']+\.js)["\']', index, flags=re.IGNORECASE)
        if not bundle_match:
            return _check("OHIF HTTP availability", False, "Proxy-hosted index has no JavaScript entry bundle")
        bundle_url = urllib.request.urljoin(f"https://{hostname}/", bundle_match.group(1))
        bundle = _url_bytes(bundle_url)
        return _check("OHIF HTTP availability", len(bundle) > 1024, f"proxy-hosted HTTP 200; entry bundle {len(bundle)} bytes")
    except Exception as exc:
        return _check("OHIF HTTP availability", False, f"{type(exc).__name__}: {exc}")


CARDIOLOGY_HEALTH_CHECK_NAME = "Cardiology app health"


def _cardiology_app_fqdn(config: dict[str, Any], az_run: Callable[..., Any]) -> tuple[str, str]:
    """Resolve the tagged cardiology Container App ingress FQDN, or an error detail."""
    fqdn, _name, error = _cardiology_app(config, az_run)
    return fqdn, error


def _cardiology_app(config: dict[str, Any], az_run: Callable[..., Any]) -> tuple[str, str, str]:
    """(fqdn, name, error) for the tagged cardiology Container App."""
    resource_group = str(config.get("resource_group_name") or "")
    if not resource_group:
        return "", "", "Resource group name is not configured"
    args = [
        "az", "containerapp", "list",
        "-g", resource_group,
        "--query", "[].{name:name, fqdn:properties.configuration.ingress.fqdn, tags:tags}",
        "-o", "json",
    ]
    subscription = config.get("expected_subscription_id")
    if subscription:
        args.extend(["--subscription", subscription])
    proc = az_run(args)
    if proc.returncode != 0:
        return "", "", f"Could not list Container Apps: {str(proc.stderr or '').strip()[:200]}"
    try:
        apps = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return "", "", f"Container App list response invalid: {exc}"
    for app in apps:
        tags = app.get("tags") or {}
        if str(tags.get(CARDIOLOGY_WORKLOAD_TAG) or "").strip().lower() != CARDIOLOGY_WORKLOAD_VALUE:
            continue
        fqdn = str(app.get("fqdn") or "")
        if not fqdn:
            return "", "", f"Container App '{app.get('name')}' has no ingress FQDN"
        return fqdn, str(app.get("name") or ""), ""
    return "", "", f"No Container App tagged {CARDIOLOGY_WORKLOAD_TAG}={CARDIOLOGY_WORKLOAD_VALUE} in {resource_group}"


def cardiology_app_check(config: dict[str, Any], az_run: Callable[..., Any]) -> dict[str, str]:
    """The tagged cardiology app serves a built revision on the live profile.

    status alone is not enough: the pre-build placeholder and a local-profile
    image also answer, and neither is the deployed app.
    """
    fqdn, error = _cardiology_app_fqdn(config, az_run)
    if not fqdn:
        return _check(CARDIOLOGY_HEALTH_CHECK_NAME, False, error)
    last_error = "Cardiology app health did not respond"
    for attempt in range(1, 4):
        try:
            payload = json.loads(_url_bytes(f"https://{fqdn}/api/health", timeout=30))
            status, profile = payload.get("status"), payload.get("profile")
            model, revision = payload.get("model"), payload.get("revision")
            ok = status == "ok" and profile == "live" and bool(model) and revision not in (None, "", "placeholder", "local-working-tree")
            return _check(CARDIOLOGY_HEALTH_CHECK_NAME, ok, f"status={status}, profile={profile}, model={model}, revision={revision}, fqdn={fqdn}")
        except Exception as exc:
            last_error = f"attempt {attempt}/3 {type(exc).__name__}: {exc}"
            if attempt < 3:
                time.sleep(10)
    return _check(CARDIOLOGY_HEALTH_CHECK_NAME, False, last_error)


CARDIOLOGY_SIGN_IN_CHECK_NAME = "Cardiology app sign-in enforced"
_BROWSER_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def _unauthenticated_response(fqdn: str, path: str, browser: bool) -> tuple[int, str]:
    """(status, Location) for an anonymous request, without following redirects."""
    connection = http.client.HTTPSConnection(fqdn, timeout=30)
    headers = {"User-Agent": _BROWSER_USER_AGENT, "Accept": "text/html"} if browser else {}
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status, response.getheader("Location") or ""
    finally:
        connection.close()


def _az_json(az_run: Callable[..., Any], args: list[str], expected: type) -> Any:
    proc = az_run([*args, "-o", "json"])
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed")
    value = json.loads(proc.stdout or "null")
    if not isinstance(value, expected):
        raise RuntimeError(f"{' '.join(args[:3])} returned no {expected.__name__}")
    return value


def _token_issued(tenant: str, body: bytes, timeout: float) -> bool:
    request = urllib.request.Request(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=body)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200 and "access_token" in json.load(response)
    except (OSError, ValueError):  # URLError/HTTPError/timeouts are OSError; bad JSON is ValueError
        return False


def _client_secret_authenticates(tenant: str, client_id: str, secret: str, window: float = 30.0) -> bool:
    """Only a live secret for this app gets a client-credentials token.

    Entra replicates credential changes gradually and, for minutes after one,
    refuses a valid secret intermittently (AADSTS7000215): one token proves the
    secret; refusal counts only when it persists for `window` seconds. Each probe
    runs on a daemon thread joined for at most the time left, so resolution,
    connection, and a slow body cannot overrun the window; a late answer is ignored.
    """
    body = urllib.parse.urlencode({"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials",
                                   "scope": "https://graph.microsoft.com/.default"}).encode()
    deadline = time.monotonic() + window
    while (left := deadline - time.monotonic()) > 0:
        budget = min(10.0, left)
        attempt_deadline = min(deadline, time.monotonic() + budget)
        issued: list[tuple[bool, float]] = []  # (token issued, completion time)
        probe = threading.Thread(target=lambda out=issued, t=budget: out.append((_token_issued(tenant, body, t), time.monotonic())), daemon=True)
        probe.start()
        probe.join(budget)
        if issued and issued[0][0] and issued[0][1] <= attempt_deadline:
            return True
        time.sleep(max(0.0, min(5.0, deadline - time.monotonic())))
    return False


def _intended_auth_config(client_id: str, tenant: str) -> dict[str, Any]:
    """The one auth config the deployer writes and accepts (mirrors its
    Get-IntendedAuthConfig)."""
    return {
        "platform": {"enabled": True},
        "globalValidation": {"unauthenticatedClientAction": "RedirectToLoginPage", "redirectToProvider": "azureactivedirectory",
                             "excludedPaths": ["/api/health"]},
        "identityProviders": {"azureActiveDirectory": {
            "enabled": True,
            "registration": {"clientId": client_id, "clientSecretSettingName": "microsoft-provider-authentication-secret",
                             "openIdIssuer": f"https://login.microsoftonline.com/{tenant}/v2.0"},
            "validation": {"defaultAuthorizationPolicy": {"allowedApplications": []}},
        }},
        "login": {"preserveUrlFragmentsForLogins": False, "nonce": {"validateNonce": True}},
        "httpSettings": {"requireHttps": True},
    }


def _canonical(value: Any) -> Any:
    """Keys sorted; None, empty objects, and empty lists dropped (the service
    adds and omits those freely). Booleans are kept."""
    if isinstance(value, dict):
        items = {k: c for k, v in sorted(value.items()) if (c := _canonical(v)) is not None}
        return items or None
    if isinstance(value, list):
        items = [c for v in value if (c := _canonical(v)) is not None]
        return items or None
    return value


def _first_difference(want: Any, got: Any, path: str = "") -> str:
    if isinstance(want, dict) and isinstance(got, dict):
        for key in sorted(set(want) | set(got)):
            if key not in want:
                return f"{path}{key} (unexpected)"
            if key not in got:
                return f"{path}{key} (missing)"
            if difference := _first_difference(want[key], got[key], f"{path}{key}."):
                return difference
        return ""
    return "" if want == got and type(want) is type(got) else (path.rstrip(".") or "(root)")


def _access_policy_problem(auth: dict[str, Any], ingress: dict[str, Any], tenant: str, client_id: str) -> str:
    """The exact access policy (mirrors the deployer's Get-AccessPolicyProblem).

    The auth config must be exactly _intended_auth_config for this client; only
    the service-set isAutoProvisioned flag is ignored, so a setting this code
    never names is still a deviation. Ingress must be HTTPS-only with no CORS
    policy and no additional ports, on the default HTTP transport.
    """
    actual = _canonical(json.loads(json.dumps(auth))) or {}
    ((actual.get("identityProviders") or {}).get("azureActiveDirectory") or {}).pop("isAutoProvisioned", None)
    if difference := _first_difference(_canonical(_intended_auth_config(client_id, tenant)), actual):
        return f"sign-in configuration differs from the intended policy at {difference}"
    if ingress.get("allowInsecure") is not False:
        return "ingress allows plain HTTP"
    if ingress.get("corsPolicy"):
        return "ingress has a CORS policy"
    if [m for m in ingress.get("additionalPortMappings") or [] if m]:
        return "ingress has additional ports"
    if ingress.get("transport") != "Auto":
        return f"ingress transport is {ingress.get('transport')}, not Auto"
    return ""


def _cardiology_auth_config_problem(config: dict[str, Any], app_name: str, fqdn: str, sign_in_location: str,
                                    az_run: Callable[..., Any]) -> str:
    """The control-plane access policy the deployment script establishes.

    The exact auth and ingress policy (_access_policy_problem); a client secret
    that authenticates as the app (the callback redeems codes with it); a
    registration whose sole redirect URI is this app's callback (case-sensitive),
    single-tenant, without implicit access tokens, carrying this deployment's
    owner tag and owned by no one but the deploying user; anonymous browsers sent
    to sign-in for that registration and callback; app assignment required; and
    exactly the deploying user plus cardiology_app_users assigned.
    """
    resource_group = str(config.get("resource_group_name") or "")
    tenant = str(config.get("expected_tenant_id") or "")
    scope = ["-g", resource_group, "-n", app_name]
    if config.get("expected_subscription_id"):
        scope += ["--subscription", str(config["expected_subscription_id"])]
    try:
        auth = _az_json(az_run, ["az", "containerapp", "auth", "show", *scope], dict)
        ingress = _az_json(az_run, ["az", "containerapp", "show", *scope, "--query", "properties.configuration.ingress"], dict)
        registration = auth["identityProviders"]["azureActiveDirectory"]["registration"]
        client_id = str(registration.get("clientId"))
        if weakness := _access_policy_problem(auth, ingress, tenant, client_id):
            return weakness
        secrets = _az_json(az_run, ["az", "containerapp", "secret", "list", *scope, "--query", "[].name"], list)
        setting = registration.get("clientSecretSettingName")
        if setting not in secrets:
            return f"client secret '{setting}' is missing from the app"
        secret = _az_json(az_run, ["az", "containerapp", "secret", "show", *scope, "--secret-name", str(setting), "--query", "value"], str)
        if not _client_secret_authenticates(tenant, client_id, secret):
            return "the installed client secret does not authenticate as the app"

        callback = f"https://{fqdn}/.auth/login/aad/callback"
        application = _az_json(az_run, ["az", "ad", "app", "show", "--id", client_id], dict)
        web = application.get("web") or {}
        if web.get("redirectUris") != [callback]:
            return f"registration redirect URIs are not exactly the callback {callback}"
        if application.get("signInAudience") != "AzureADMyOrg":
            return "registration is not single-tenant"
        if (web.get("implicitGrantSettings") or {}).get("enableAccessTokenIssuance") is True:
            return "registration issues implicit access tokens"
        subscription = str(config.get("expected_subscription_id") or "") or _az_json(az_run, ["az", "account", "show", "--query", "id"], str)
        owner_tag = f"hls-cardiology-app:{subscription}/{resource_group.lower()}/{app_name}"
        if owner_tag not in (application.get("tags") or []):
            return f"registration lacks the owner tag {owner_tag}"
        if [u for u in ((application.get("spa") or {}).get("redirectUris") or []) + ((application.get("publicClient") or {}).get("redirectUris") or []) if u]:
            return "registration has SPA or public-client redirect URIs"
        if application.get("isFallbackPublicClient") is True:
            return "registration allows public-client flows"
        deployer = _az_json(az_run, ["az", "ad", "signed-in-user", "show", "--query", "id"], str)
        sp = _az_json(az_run, ["az", "ad", "sp", "show", "--id", client_id], dict)
        owners = _az_json(az_run, ["az", "ad", "app", "owner", "list", "--id", str(application.get("id")), "--query", "[].id"], list)
        owners += _az_json(az_run, ["az", "ad", "sp", "owner", "list", "--id", str(sp.get("id")), "--query", "[].id"], list)
        if others := sorted({o for o in owners if o != deployer}):
            return f"registration or its service principal has other owners {others}"
        query = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_location).query)
        if query.get("client_id") != [client_id] or query.get("redirect_uri") != [callback]:
            return "the sign-in redirect does not name this app's registration and callback"
        if sp.get("appRoleAssignmentRequired") is not True:
            return "app assignment is not required"
        assigned: set[str] = set()
        url = f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp['id']}/appRoleAssignedTo"
        while url:  # Graph pages this collection
            page = _az_json(az_run, ["az", "rest", "--method", "GET", "--url", url], dict)
            assigned |= {a["principalId"] for a in page.get("value", [])}
            url = page.get("@odata.nextLink")
        wanted = {deployer}
        wanted |= {_az_json(az_run, ["az", "ad", "user", "show", "--id", upn, "--query", "id"], str)
                   for upn in config.get("cardiology_app_users") or [] if upn}
        if assigned != wanted:
            return f"sign-in assignments differ from the requested accounts ({len(assigned)} assigned, {len(wanted)} requested)"
    except (KeyError, TypeError, AttributeError, RuntimeError, ValueError) as exc:  # ValueError: bad JSON or URL
        return f"could not verify the sign-in configuration: {exc}"
    return ""


def cardiology_sign_in_check(config: dict[str, Any], az_run: Callable[..., Any]) -> dict[str, str]:
    """At the edge, anonymous browsers go to Entra sign-in and anonymous API reads
    are refused; on the control plane, the full access policy holds."""
    fqdn, app_name, error = _cardiology_app(config, az_run)
    if not fqdn:
        return _check(CARDIOLOGY_SIGN_IN_CHECK_NAME, False, error)
    tenant = str(config.get("expected_tenant_id") or "")
    try:
        page_status, location = _unauthenticated_response(fqdn, "/", browser=True)
        api_status, _ = _unauthenticated_response(fqdn, "/api/activity", browser=False)
    except Exception as exc:
        return _check(CARDIOLOGY_SIGN_IN_CHECK_NAME, False, f"{type(exc).__name__}: {exc}")
    redirected = page_status == 302 and location.startswith(f"https://login.microsoftonline.com/{tenant}/")
    policy_problem = _cardiology_auth_config_problem(config, app_name, fqdn, location, az_run)
    return _check(CARDIOLOGY_SIGN_IN_CHECK_NAME, redirected and api_status == 401 and not policy_problem,
                  f"browser / -> {page_status} {location[:80]}; anonymous /api/activity -> {api_status}"
                  + (f"; {policy_problem}" if policy_problem else ""))


CARDIOLOGY_HDS_ACCESS_CHECK_NAME = "Cardiology app HDS access"
FHIR_DATA_CONTRIBUTOR_ROLE = "5a1fc7df-4bf1-4951-a576-89034ee01acd"
FABRIC_API = "https://api.fabric.microsoft.com"


def _cardiology_hds_access_problem(config: dict[str, Any], app_name: str, az_run: Callable[..., Any]) -> str:
    """The HDS access the deployment script establishes (mirrors its grants):
    the app runs as one user-assigned identity, is configured for the requested
    Fabric gold endpoint, FHIR service and Masimo Eventhouse (AZURE_CLIENT_ID
    naming that identity), and the identity holds FHIR Data Contributor on the
    FHIR service and exactly Viewer on the Fabric workspace (which also covers
    its KQL reads of the Eventhouse)."""
    fhir_service_id = str(config.get("cardiology_fhir_service_id") or CARDIOLOGY_FHIR_SERVICE_ID)
    workspace_id = str(config.get("cardiology_fabric_workspace_id") or CARDIOLOGY_FABRIC_WORKSPACE_ID)
    scope = ["-g", str(config.get("resource_group_name") or ""), "-n", app_name]
    if config.get("expected_subscription_id"):
        scope += ["--subscription", str(config["expected_subscription_id"])]
    try:
        app = _az_json(az_run, ["az", "containerapp", "show", *scope, "--query",
                                "{identities: identity.userAssignedIdentities, env: properties.template.containers[0].env}"], dict)
        identities = list((app.get("identities") or {}).values())
        if len(identities) != 1:
            return f"the app runs as {len(identities)} user-assigned identities, not exactly one"
        principal, client = str(identities[0]["principalId"]), str(identities[0]["clientId"])
        env = {item.get("name"): item.get("value") for item in app.get("env") or []}
        wanted_env = {
            "CALDOVA_FABRIC_SQL_HOST": str(config.get("cardiology_fabric_sql_host") or CARDIOLOGY_FABRIC_SQL_HOST),
            "CALDOVA_FABRIC_GOLD_DATABASE": str(config.get("cardiology_fabric_gold_database") or CARDIOLOGY_FABRIC_GOLD_DATABASE),
            "CALDOVA_FHIR_URL": str(config.get("cardiology_fhir_url") or CARDIOLOGY_FHIR_URL),
            "CALDOVA_EVENTHOUSE_QUERY_URI": str(config.get("cardiology_eventhouse_query_uri") or CARDIOLOGY_EVENTHOUSE_QUERY_URI),
            "CALDOVA_EVENTHOUSE_DATABASE": str(config.get("cardiology_eventhouse_database") or CARDIOLOGY_EVENTHOUSE_DATABASE),
            "AZURE_CLIENT_ID": client,
        }
        if wrong := sorted(name for name, value in wanted_env.items() if env.get(name) != value):
            return f"app settings differ from the requested HDS access: {', '.join(wrong)}"

        scopes = _az_json(az_run, ["az", "role", "assignment", "list", "--scope", fhir_service_id, "--assignee-object-id", principal,
                                   "--role", FHIR_DATA_CONTRIBUTOR_ROLE, "--query", "[].scope"], list)
        if fhir_service_id.lower() not in {str(s).lower() for s in scopes}:
            return "the app identity lacks FHIR Data Contributor on the FHIR service"

        roles: list[str] = []
        url = f"{FABRIC_API}/v1/workspaces/{workspace_id}/roleAssignments"
        while url:  # Fabric pages this collection
            page = _az_json(az_run, ["az", "rest", "--method", "GET", "--url", url, "--resource", FABRIC_API], dict)
            roles += [str(a.get("role")) for a in page.get("value") or [] if (a.get("principal") or {}).get("id") == principal]
            url = page.get("continuationUri")
        if roles != ["Viewer"]:
            return f"the app identity's Fabric workspace roles are {roles or 'none'}, not exactly Viewer"
    except (KeyError, TypeError, AttributeError, RuntimeError, ValueError) as exc:  # ValueError: bad JSON
        return f"could not verify HDS access: {exc}"
    return ""


def cardiology_hds_access_check(config: dict[str, Any], az_run: Callable[..., Any]) -> dict[str, str]:
    """The deployed app can reach the HDS data its live profile requires."""
    _fqdn, app_name, error = _cardiology_app(config, az_run)
    if not app_name:
        return _check(CARDIOLOGY_HDS_ACCESS_CHECK_NAME, False, error)
    problem = _cardiology_hds_access_problem(config, app_name, az_run)
    return _check(CARDIOLOGY_HDS_ACCESS_CHECK_NAME, not problem,
                  problem or "FHIR Data Contributor on the FHIR service; Viewer on the Fabric workspace; app settings match")


def _eventhub_consumption_check(resources: dict[str, Any], config: dict[str, Any], az_run: Callable[..., Any], entity_name: str) -> dict[str, str]:
    namespaces = _azure(resources, "Microsoft.EventHub/namespaces")
    check_name = f"Eventstream consumption: {entity_name}"
    if not namespaces:
        return _check(check_name, False, "Event Hub namespace missing")
    namespace = namespaces[0]
    args = [
        "az", "monitor", "metrics", "list",
        "--resource", namespace.get("id", ""),
        "--metric", "IncomingMessages", "OutgoingMessages",
        "--filter", f"EntityName eq '{entity_name}'",
        "--interval", "PT1M", "--aggregation", "Total", "--offset", "15m", "-o", "json",
    ]
    subscription = config.get("expected_subscription_id")
    if subscription:
        args.extend(["--subscription", subscription])
    proc = az_run(args)
    if proc.returncode != 0:
        return _check(check_name, False, proc.stderr.strip() or "Metric query failed")
    try:
        values = json.loads(proc.stdout).get("value") or []
        totals: dict[str, float] = {}
        for metric in values:
            points = [point for series in (metric.get("timeseries") or []) for point in (series.get("data") or [])]
            totals[str((metric.get("name") or {}).get("value"))] = sum(float(point.get("total") or 0) for point in points[-10:])
        incoming = totals.get("IncomingMessages", 0)
        outgoing = totals.get("OutgoingMessages", 0)
        return _check(check_name, incoming > 0 and outgoing > 0, f"15m incoming={incoming:.0f}, outgoing={outgoing:.0f}")
    except Exception as exc:
        return _check(check_name, False, f"Metric response invalid: {exc}")


def _pipeline_checks(resources: dict[str, Any], fabric_client_factory: Callable[[], Any]) -> list[dict[str, str]]:
    workspace_id = str((resources.get("workspace") or {}).get("id") or "")
    if not workspace_id:
        return [_check("HDS pipeline outcomes", False, "Workspace ID missing")]
    required = [
        item
        for item in resources.get("fabric") or []
        if str(item.get("type") or "").lower() == "datapipeline"
        and any(part in str(item.get("name") or "").lower() for part in ("clinical_data_foundation", "imaging_with_clinical", "omop_analytics", "msft_cma", "poa_ingestion", "claims_data_ingestion", "sdoh_ingestion"))
    ]
    checks: list[dict[str, str]] = []
    client = fabric_client_factory()
    for item in required:
        name = str(item.get("name") or item.get("id"))
        try:
            result = client.call("GET", f"/workspaces/{workspace_id}/items/{item['id']}/jobs/instances?limit=1", max_retries=2)
            latest = (result.get("value") or [{}])[0]
            status = str(latest.get("status") or "Missing")
            checks.append(_check(f"Pipeline completed: {name}", status == "Completed", status))
        except Exception as exc:
            checks.append(_check(f"Pipeline completed: {name}", False, f"{type(exc).__name__}: {exc}"))
    return checks or [_check("HDS pipeline outcomes", False, "Required pipelines missing")]


def _powerbi_query_check(resources: dict[str, Any], config: dict[str, Any], az_run: Callable[..., Any], model_name: str, dax: str) -> dict[str, str]:
    workspace_id = str((resources.get("workspace") or {}).get("id") or "")
    models = _items(resources, "SemanticModel", model_name.lower())
    if not workspace_id or not models:
        return _check(f"Power BI query: {model_name}", False, "Workspace or semantic model missing")
    args = ["az", "account", "get-access-token", "--resource", "https://analysis.windows.net/powerbi/api", "--query", "accessToken", "-o", "tsv"]
    subscription = config.get("expected_subscription_id")
    if subscription:
        args.extend(["--subscription", subscription])
    proc = az_run(args)
    token = proc.stdout.strip() if proc.returncode == 0 else ""
    if not token:
        return _check(f"Power BI query: {model_name}", False, proc.stderr.strip() or "Power BI token unavailable")
    dataset_id = models[0]["id"]
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
    body = json.dumps({"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
        rows = (((payload.get("results") or [{}])[0].get("tables") or [{}])[0].get("rows") or [])
        return _check(f"Power BI query: {model_name}", bool(rows), f"dataset={dataset_id}, rows={len(rows)}")
    except Exception as exc:
        return _check(f"Power BI query: {model_name}", False, f"{type(exc).__name__}: {exc}")


def _quality_report_binding_check(resources: dict[str, Any], fabric_client_factory: Callable[[], Any]) -> dict[str, str]:
    check_name = "Quality report semantic model binding"
    workspace_id = str((resources.get("workspace") or {}).get("id") or "")
    reports = _items(resources, "Report", "population health", "quality dashboard")
    models = _items(resources, "SemanticModel", "population health", "quality semantic model")
    if not workspace_id or not reports or not models:
        return _check(check_name, False, "Workspace, quality report, or quality semantic model missing")
    try:
        definition = fabric_client_factory().get_item_definition(workspace_id, reports[0]["id"])
        pbir_part = next(part for part in definition.get("parts") or [] if part.get("path") == "definition.pbir")
        pbir = json.loads(base64.b64decode(pbir_part["payload"]).decode("utf-8"))
        connection = str((((pbir.get("datasetReference") or {}).get("byConnection") or {}).get("connectionString") or ""))
        model_id = str(models[0]["id"])
        bound = f"semanticmodelid={model_id}".lower() in connection.lower()
        return _check(check_name, bound, f"report={reports[0]['id']}, semanticModel={model_id}")
    except Exception as exc:
        return _check(check_name, False, f"{type(exc).__name__}: {exc}")


def runtime_feature_checks(
    resources: dict[str, Any],
    config: dict[str, Any],
    az_run: Callable[..., Any],
    fabric_client_factory: Callable[[], Any],
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if fabric_runtime_expected(config):
        checks.append(_eventhub_consumption_check(resources, config, az_run, "telemetry-stream"))
    if not config.get("scaffolding_only", False) and not config.get("skip_phase7", False) and not config.get("skip_payer_rti", False):
        checks.append(_eventhub_consumption_check(resources, config, az_run, "claim-stream"))
    if not config.get("skip_hds_pipelines", False):
        checks.extend(_pipeline_checks(resources, fabric_client_factory))
    if not config.get("skip_imaging", False):
        checks.append(_proxy_check(resources, config, az_run))
        viewer_check = _swa_check(resources, config, az_run)
        if viewer_check["status"] == "fail":
            viewer_check = _proxy_viewer_check(resources, config, az_run)
        checks.append(viewer_check)
        checks.append(_powerbi_query_check(resources, config, az_run, "ImagingReport", "EVALUATE ROW(\"Rows\", COUNTROWS('DicomFile'))"))
    if not config.get("skip_quality_measures", False):
        checks.append(_quality_report_binding_check(resources, fabric_client_factory))
        checks.append(_powerbi_query_check(resources, config, az_run, "Population Health & Quality Semantic Model", "EVALUATE ROW(\"Rows\", COUNTROWS('agg_quality_measures'))"))
    if cardiology_app_expected(config):
        checks.append(cardiology_app_check(config, az_run))
        checks.append(cardiology_sign_in_check(config, az_run))
        checks.append(cardiology_hds_access_check(config, az_run))
    return checks
