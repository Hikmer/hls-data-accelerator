from __future__ import annotations

import base64
import json

import unittest

import urllib.error
from unittest.mock import patch

from shared.deployment_validation import (
    _eventhub_consumption_check,
    _quality_report_binding_check,
    cardiology_app_check,
    cardiology_sign_in_check,
    effective_validation_config,
    fabric_runtime_expected,
    feature_presence_checks,
    runtime_feature_checks,
)


class _AzResult:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


_CARDIOLOGY_APP_LIST = json.dumps([
    {"name": "hds-dicom-proxy", "fqdn": "proxy.example.test", "tags": {"hls-workload": "dicom-proxy"}},
    {"name": "cardioe2e-app", "fqdn": "cardio.example.test", "tags": {"hls-workload": "cardiology-app"}},
])


class DeploymentValidationTests(unittest.TestCase):
    def test_legacy_continuation_still_requires_fabric_runtime(self) -> None:
        self.assertTrue(
            fabric_runtime_expected(
                {
                    "skip_fabric": True,
                    "continue_from_instance_id": "prior-run",
                    "skip_activator": False,
                }
            )
        )

    def test_requested_features_fail_when_only_workspace_exists(self) -> None:
        resources = {
            "workspace": {"id": "workspace-id", "name": "med-test"},
            "azure": [],
            "fabric": [],
        }
        config = {
            "skip_fabric": False,
            "skip_hds_pipelines": True,
            "skip_data_agents": True,
            "skip_imaging": True,
            "skip_ontology": True,
            "skip_activator": False,
            "alert_email": "alerts@example.test",
            "skip_quality_measures": True,
            "skip_phase7": True,
        }

        checks = feature_presence_checks(resources, config)
        failed_names = {check["name"] for check in checks if check["status"] == "fail"}

        self.assertIn("Masimo Eventstream", failed_names)
        self.assertIn("Masimo Eventhouse", failed_names)
        self.assertIn("Masimo KQL database", failed_names)
        self.assertIn("Masimo KQL dashboard", failed_names)
        self.assertIn("Clinical alert Activator", failed_names)

    def test_disabled_features_do_not_create_false_failures(self) -> None:
        checks = feature_presence_checks(
            {"workspace": None, "azure": [], "fabric": []},
            {
                "skip_fabric": True,
                "skip_hds_pipelines": True,
                "skip_data_agents": True,
                "skip_imaging": True,
                "skip_ontology": True,
                "skip_activator": True,
                "skip_quality_measures": True,
                "skip_phase7": True,
                "skip_cardiology_app": True,
            },
        )

        self.assertEqual(checks, [])

    def test_scaffolding_validates_definitions_without_runtime_or_emulators(self) -> None:
        config = {
            "scaffolding_only": True,
            "skip_fabric": False,
            "skip_hds_pipelines": True,
            "skip_data_agents": True,
            "skip_imaging": True,
            "skip_ontology": True,
            "skip_activator": True,
            "skip_quality_measures": True,
            "skip_phase7": False,
            "skip_payer_rti": False,
            "skip_ops_agent": True,
            "skip_graph_agent": True,
            "skip_payer_activator": True,
        }

        self.assertFalse(fabric_runtime_expected(config))
        checks = feature_presence_checks(
            {"workspace": {"id": "ws", "name": "med-test"}, "azure": [], "fabric": []},
            config,
        )
        names = {check["name"] for check in checks}
        self.assertIn("Masimo Eventstream", names)
        self.assertIn("HDS Bronze lakehouse", names)
        self.assertIn("Clinical foundation pipeline", names)
        self.assertNotIn("Payer claim emulator", names)


    def test_eventhub_validation_filters_and_requires_each_entity(self) -> None:
        captured: list[str] = []

        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps({
                "value": [
                    {"name": {"value": "IncomingMessages"}, "timeseries": [{"data": [{"total": 10}]}]},
                    {"name": {"value": "OutgoingMessages"}, "timeseries": [{"data": [{"total": 10}]}]},
                ]
            })

        def az_run(args):
            captured.extend(args)
            return Result()

        result = _eventhub_consumption_check(
            {
                "azure": [
                    {
                        "name": "namespace",
                        "fullType": "Microsoft.EventHub/namespaces",
                        "id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.EventHub/namespaces/ns",
                    }
                ]
            },
            {"expected_subscription_id": "sub"},
            az_run,
            "claim-stream",
        )

        self.assertEqual(result["status"], "pass")
        self.assertIn("EntityName eq 'claim-stream'", captured)

    def test_quality_validation_requires_new_phase6_artifacts(self) -> None:
        checks = feature_presence_checks(
            {
                "workspace": {"id": "workspace-id"},
                "azure": [],
                "fabric": [
                    {"id": "legacy-report", "name": "healthcare1_msft_cma_report", "type": "Report"},
                    {"id": "legacy-model", "name": "healthcare1_msft_cma_semantic_model", "type": "SemanticModel"},
                ],
            },
            {
                "skip_fabric": True,
                "skip_hds_pipelines": True,
                "skip_data_agents": True,
                "skip_imaging": True,
                "skip_ontology": True,
                "skip_activator": True,
                "skip_quality_measures": False,
                "skip_phase7": True,
            },
        )

        failed_names = {check["name"] for check in checks if check["status"] == "fail"}
        self.assertIn("Population health quality report", failed_names)
        self.assertIn("Population health quality semantic model", failed_names)
        checks_with_alert = feature_presence_checks(
            {"workspace": {"id": "workspace-id"}, "azure": [], "fabric": []},
            {
                "skip_fabric": True,
                "skip_hds_pipelines": True,
                "skip_data_agents": True,
                "skip_imaging": True,
                "skip_ontology": True,
                "skip_activator": False,
                "alert_email": "alerts@example.test",
                "skip_quality_measures": False,
                "skip_phase7": True,
            },
        )
        failed_with_alert = {check["name"] for check in checks_with_alert if check["status"] == "fail"}
        self.assertIn("Readmission risk Activator", failed_with_alert)

    def test_quality_report_binding_targets_phase6_semantic_model(self) -> None:
        resources = {
            "workspace": {"id": "workspace-id"},
            "fabric": [
                {"id": "report-id", "name": "Population Health & Quality Dashboard", "type": "Report"},
                {"id": "model-id", "name": "Population Health & Quality Semantic Model", "type": "SemanticModel"},
            ],
        }
        pbir = {
            "datasetReference": {
                "byConnection": {"connectionString": "Data Source=powerbi://example;semanticmodelid=model-id"}
            }
        }

        class Client:
            def get_item_definition(self, workspace_id, report_id):
                self.workspace_id = workspace_id
                self.report_id = report_id
                return {
                    "parts": [
                        {
                            "path": "definition.pbir",
                            "payload": base64.b64encode(json.dumps(pbir).encode()).decode(),
                        }
                    ]
                }

        result = _quality_report_binding_check(resources, Client)

        self.assertEqual(result["status"], "pass")
        self.assertIn("semanticModel=model-id", result["detail"])

    def test_phase7_validation_excludes_unrelated_full_deployment_features(self) -> None:
        config = effective_validation_config({
            "phase7_only": True,
            "continue_from_instance_id": "prior-full-run",
            "skip_fabric": False,
            "skip_hds_pipelines": False,
            "skip_data_agents": False,
            "skip_imaging": False,
            "skip_ontology": False,
            "skip_activator": False,
            "skip_quality_measures": False,
            "skip_phase7": False,
            "skip_payer_rti": False,
            "skip_ops_agent": False,
            "skip_graph_agent": False,
            "skip_payer_activator": False,
            "payer_ops_email": "payer@example.test",
        })

        self.assertFalse(fabric_runtime_expected(config))
        checks = feature_presence_checks({"workspace": {"id": "ws"}, "azure": [], "fabric": []}, config)
        names = {check["name"] for check in checks}
        self.assertIn("Payer claim emulator", names)
        self.assertIn("Healthcare Graph Agent", names)
        self.assertNotIn("HDS Bronze lakehouse", names)
        self.assertNotIn("Imaging report", names)
        self.assertNotIn("Population health report", names)
        self.assertNotIn("Cardiology app Container App", names)

    def test_phase8_validation_selects_only_cardiology_checks(self) -> None:
        config = effective_validation_config({
            "phase8_only": True,
            "continue_from_instance_id": "prior-full-run",
            "skip_fabric": False,
            "skip_hds_pipelines": False,
            "skip_data_agents": False,
            "skip_imaging": False,
            "skip_ontology": False,
            "skip_activator": False,
            "skip_quality_measures": False,
            "skip_phase7": False,
            "skip_payer_rti": False,
            "alert_email": "alerts@example.test",
            "payer_ops_email": "payer@example.test",
        })

        self.assertFalse(fabric_runtime_expected(config))
        checks = feature_presence_checks({"workspace": {"id": "ws"}, "azure": [], "fabric": []}, config)

        self.assertEqual([check["name"] for check in checks], ["Cardiology app Container App"])
        self.assertEqual(checks[0]["status"], "fail")

    def test_full_deploy_can_deselect_the_cardiology_app(self) -> None:
        resources = {
            "workspace": {"id": "ws"},
            "fabric": [],
            "azure": [{
                "name": "cardioe2e-app",
                "fullType": "Microsoft.App/containerApps",
                "id": "/subscriptions/sub/resourceGroups/rg-test/providers/Microsoft.App/containerApps/cardioe2e-app",
                "tags": {"hls-workload": "cardiology-app"},
            }],
        }
        base = {
            "skip_fabric": True,
            "skip_hds_pipelines": True,
            "skip_data_agents": True,
            "skip_imaging": True,
            "skip_ontology": True,
            "skip_activator": True,
            "skip_quality_measures": True,
            "skip_phase7": True,
            "resource_group_name": "rg-test",
        }

        def unused_az_run(args):
            raise AssertionError(f"no CLI call expected: {args}")

        skipped = {**base, "skip_cardiology_app": True}
        self.assertEqual(feature_presence_checks(resources, skipped), [])
        self.assertEqual(runtime_feature_checks(resources, skipped, unused_az_run, lambda: None), [])

        selected = {**base, "skip_cardiology_app": False}
        presence = feature_presence_checks(resources, selected)
        self.assertEqual([check["name"] for check in presence], ["Cardiology app Container App"])
        self.assertEqual(presence[0]["status"], "pass")

    def test_a_reused_cardiology_app_is_still_validated(self) -> None:
        """Resume skips redeploying a verified app but must not drop its checks."""
        resources = {"workspace": {"id": "ws"}, "fabric": [], "azure": []}
        base = {"skip_fabric": True, "skip_hds_pipelines": True, "skip_data_agents": True, "skip_imaging": True,
                "skip_ontology": True, "skip_activator": True, "skip_quality_measures": True, "skip_phase7": True}
        reused = effective_validation_config({**base, "skip_cardiology_app": True, "cardiology_app_reused": True})
        self.assertEqual([c["name"] for c in feature_presence_checks(resources, reused)], ["Cardiology app Container App"])

        # A phase-only run cloned from that config does not inherit the reuse flag.
        phase7 = effective_validation_config({**base, "phase7_only": True, "cardiology_app_reused": True})
        self.assertNotIn("Cardiology app Container App", [c["name"] for c in feature_presence_checks(resources, phase7)])

    def test_cardiology_health_passes_on_a_built_live_revision(self) -> None:
        captured: list[list[str]] = []

        def az_run(args):
            captured.append(args)
            return _AzResult(_CARDIOLOGY_APP_LIST)

        with patch(
            "shared.deployment_validation._url_bytes",
            return_value=json.dumps({"status": "ok", "profile": "live", "model": "gpt-5.6-luna", "revision": "9ed0e58ea936"}).encode(),
        ) as url_bytes:
            result = cardiology_app_check(
                {"resource_group_name": "rg-test", "expected_subscription_id": "sub"},
                az_run,
            )

        self.assertEqual(result["status"], "pass")
        url_bytes.assert_called_once_with("https://cardio.example.test/api/health", timeout=30)
        self.assertIn("rg-test", captured[0])
        self.assertIn("--subscription", captured[0])

    def test_cardiology_health_rejects_ok_from_anything_but_a_built_live_app(self) -> None:
        for payload in (
            {"status": "degraded", "profile": "live", "model": "m", "revision": "abc"},
            {"status": "ok", "profile": "local", "model": "m", "revision": "abc"},
            {"status": "ok", "profile": "live", "model": "m", "revision": "placeholder"},
            {"status": "ok", "profile": "live", "revision": "abc"},
        ):
            with self.subTest(payload=payload), patch(
                "shared.deployment_validation._url_bytes", return_value=json.dumps(payload).encode(),
            ):
                result = cardiology_app_check({"resource_group_name": "rg-test"}, lambda args: _AzResult(_CARDIOLOGY_APP_LIST))
                self.assertEqual(result["status"], "fail")

    def test_cardiology_health_fails_when_unreachable(self) -> None:
        with patch(
            "shared.deployment_validation._url_bytes",
            side_effect=urllib.error.URLError("no route to host"),
        ), patch("shared.deployment_validation.time.sleep") as sleep:
            result = cardiology_app_check(
                {"resource_group_name": "rg-test"},
                lambda args: _AzResult(_CARDIOLOGY_APP_LIST),
            )

        self.assertEqual(result["status"], "fail")
        self.assertIn("attempt 3/3", result["detail"])
        self.assertEqual(sleep.call_count, 2)

    def test_cardiology_sign_in_requires_edge_protection_and_exact_access_policy(self) -> None:
        tenant = "8d038e6a-9b7d-4cb8-bbcf-e84dff156478"
        authorize = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
        callback = "https%3A%2F%2Fcardio.example.test%2F.auth%2Flogin%2Faad%2Fcallback"
        sign_in = f"{authorize}?response_type=code+id_token&redirect_uri={callback}&client_id=app&scope=openid"
        protected = {"/": (302, sign_in), "/api/activity": (401, "")}
        good = {
            # The live shape after the deployer's PUT, including the server-set isAutoProvisioned.
            "auth": {"platform": {"enabled": True}, "encryptionSettings": {},
                     "globalValidation": {"unauthenticatedClientAction": "RedirectToLoginPage", "redirectToProvider": "azureactivedirectory",
                                          "excludedPaths": ["/api/health"]},
                     "identityProviders": {"azureActiveDirectory": {
                         "enabled": True, "isAutoProvisioned": False,
                         "registration": {"clientId": "app", "clientSecretSettingName": "microsoft-provider-authentication-secret",
                                          "openIdIssuer": f"https://login.microsoftonline.com/{tenant}/v2.0"},
                         "validation": {"defaultAuthorizationPolicy": {"allowedApplications": []}}}},
                     "login": {"preserveUrlFragmentsForLogins": False, "nonce": {"validateNonce": True}},
                     "httpSettings": {"requireHttps": True}},
            "ingress": {"external": True, "allowInsecure": False, "corsPolicy": None, "additionalPortMappings": None, "transport": "Auto"},
            "secrets": ["microsoft-provider-authentication-secret"],
            "secret_value": "live",
            "application": {"id": "obj", "signInAudience": "AzureADMyOrg", "tags": ["hls-cardiology-app:sub/rg-test/cardioe2e-app"],
                            "isFallbackPublicClient": None, "spa": {"redirectUris": []}, "publicClient": {"redirectUris": []},
                            "web": {"redirectUris": ["https://cardio.example.test/.auth/login/aad/callback"],
                                    "implicitGrantSettings": {"enableAccessTokenIssuance": False, "enableIdTokenIssuance": True}}},
            "owners": [],
            "sp_owners": [],
            "sp": {"id": "sp", "appRoleAssignmentRequired": True},
            "pages": [{"value": [{"principalId": "deployer"}]}],
        }

        def variant(change):
            state = json.loads(json.dumps(good))
            change(state)
            return state

        aad = lambda s: s["auth"]["identityProviders"]["azureActiveDirectory"]  # noqa: E731
        weaker = [
            ("extra anonymous path", lambda s: s["auth"]["globalValidation"].update(excludedPaths=["/api/health", "/api/admit"])),
            ("unauthenticated allowed", lambda s: s["auth"]["globalValidation"].update(unauthenticatedClientAction="AllowAnonymous")),
            ("redirects to another provider", lambda s: s["auth"]["globalValidation"].update(redirectToProvider="github")),
            ("GitHub sign-in enabled", lambda s: s["auth"]["identityProviders"].update(gitHub={"registration": {"clientId": "gh"}})),
            ("custom OIDC provider enabled", lambda s: s["auth"]["identityProviders"].update(customOpenIdConnectProviders={"idp": {"enabled": True}})),
            ("extra token audience", lambda s: aad(s).setdefault("validation", {}).update(allowedAudiences=["api://someone-else"])),
            ("foreign issuer", lambda s: aad(s)["registration"].update(openIdIssuer="https://sts.windows.net/other-tenant/")),
            ("open post-login redirect", lambda s: s["auth"]["login"].update(allowedExternalRedirectUrls=["https://evil.example"])),
            ("HTTPS not required", lambda s: s["auth"].update(httpSettings={"requireHttps": False})),
            ("plain HTTP ingress", lambda s: s["ingress"].update(allowInsecure=True)),
            ("CORS on ingress", lambda s: s["ingress"].update(corsPolicy={"allowedOrigins": ["*"], "allowCredentials": True})),
            ("extra external port", lambda s: s["ingress"].update(additionalPortMappings=[{"external": True, "targetPort": 4317}])),
            ("multi-tenant registration", lambda s: s["application"].update(signInAudience="AzureADMultipleOrgs")),
            ("implicit access tokens", lambda s: s["application"]["web"]["implicitGrantSettings"].update(enableAccessTokenIssuance=True)),
            ("owner tag missing", lambda s: s["application"].update(tags=[])),
            ("owner tag of another group", lambda s: s["application"].update(tags=["hls-cardiology-app:sub/rg-other/cardioe2e-app"])),
            ("another owner", lambda s: s.update(owners=["someone-else"])),
            ("service principal has another owner", lambda s: s.update(sp_owners=["someone-else"])),
            ("SPA redirect beside the web callback", lambda s: s["application"]["spa"].update(redirectUris=["https://attacker.example/cb"])),
            ("public-client redirect", lambda s: s["application"]["publicClient"].update(redirectUris=["http://localhost"])),
            ("public-client flows allowed", lambda s: s["application"].update(isFallbackPublicClient=True)),
            ("nonce validation off", lambda s: s["auth"]["login"]["nonce"].update(validateNonce=False)),
            ("long-lived session cookie", lambda s: s["auth"]["login"].update(cookieExpiration={"convention": "FixedTime", "timeToExpiration": "87600:00:00"})),
            ("known cookie signing secret", lambda s: s["auth"].update(encryptionSettings={"containerAppAuthSigningSecretName": "known"})),
            ("disabled GitHub provider (exact policy)", lambda s: s["auth"]["identityProviders"].update(gitHub={"enabled": False})),
            ("TCP transport", lambda s: s["ingress"].update(transport="Tcp")),
            ("internal extra port", lambda s: s["ingress"].update(additionalPortMappings=[{"external": False, "targetPort": 9000}])),
        ]
        cases = [
            ("protected with exact policy", protected, good, {}, "pass"),
            ("service omits isAutoProvisioned", protected, variant(lambda s: aad(s).pop("isAutoProvisioned")), {}, "pass"),
            ("deployer as sole owner of both", protected, variant(lambda s: s.update(owners=["deployer"], sp_owners=["deployer"])), {}, "pass"),
            ("auth off at the edge", {"/": (200, ""), "/api/activity": (200, "")}, good, {}, "fail"),
            ("another tenant's sign-in", {"/": (302, "https://login.microsoftonline.com/other/oauth2"), "/api/activity": (401, "")}, good, {}, "fail"),
            ("API excluded from auth", {"/": (302, sign_in), "/api/activity": (200, "")}, good, {}, "fail"),
            ("redirect names another client", {"/": (302, f"{authorize}?redirect_uri={callback}&client_id=stale"), "/api/activity": (401, "")}, good, {}, "fail"),
            ("redirect has no callback", {"/": (302, f"{authorize}?client_id=app"), "/api/activity": (401, "")}, good, {}, "fail"),
            ("malformed redirect", {"/": (302, "https://["), "/api/activity": (401, "")}, good, {}, "fail"),
            ("callback secret missing", protected, variant(lambda s: s.update(secrets=[])), {}, "fail"),
            ("installed secret revoked", protected, variant(lambda s: s.update(secret_value="revoked")), {}, "fail"),
            ("empty auth output", protected, variant(lambda s: s.update(auth=None)), {}, "fail"),
            ("callback path case differs", protected, variant(lambda s: s["application"]["web"].update(redirectUris=["https://cardio.example.test/.auth/login/aad/CALLBACK"])), {}, "fail"),
            ("callback of another app", protected, variant(lambda s: s["application"]["web"].update(redirectUris=["https://prod.example.test/.auth/login/aad/callback"])), {}, "fail"),
            ("callback plus another redirect", protected, variant(lambda s: s["application"]["web"]["redirectUris"].append("https://other.example.test/cb")), {}, "fail"),
            ("assignment not required", protected, variant(lambda s: s.update(sp={"id": "sp", "appRoleAssignmentRequired": False})), {}, "fail"),
            ("removed user still assigned", protected, variant(lambda s: s.update(pages=[{"value": [{"principalId": "deployer"}, {"principalId": "old"}]}])), {}, "fail"),
            ("unwanted grant on page two", protected, variant(lambda s: s.update(pages=[{"value": [{"principalId": "deployer"}], "@odata.nextLink": "next"}, {"value": [{"principalId": "old"}]}])), {}, "fail"),
            ("requested user not assigned", protected, good, {"cardiology_app_users": ["new@example.test"]}, "fail"),
            *[(label, protected, variant(change), {}, "fail") for label, change in weaker],
        ]
        for label, responses, state, extra_config, expected in cases:
            pages = iter(state["pages"])

            def az_run(args, state=state, pages=pages):
                if args[:3] == ["az", "containerapp", "list"]:
                    return _AzResult(_CARDIOLOGY_APP_LIST)
                if args[1:4] == ["containerapp", "secret", "show"]:
                    return _AzResult(json.dumps(state["secret_value"]))
                if args[1:4] == ["ad", "app", "owner"]:
                    return _AzResult(json.dumps(state["owners"]))
                if args[1:4] == ["ad", "sp", "owner"]:
                    return _AzResult(json.dumps(state["sp_owners"]))
                payload = {
                    ("containerapp", "auth"): lambda: state["auth"],
                    ("containerapp", "show"): lambda: state["ingress"],
                    ("containerapp", "secret"): lambda: state["secrets"],
                    ("ad", "app"): lambda: state["application"],
                    ("ad", "sp"): lambda: state["sp"],
                    ("rest", "--method"): lambda: next(pages),
                    ("ad", "signed-in-user"): lambda: "deployer",
                    ("ad", "user"): lambda: "new-user",
                }[(args[1], args[2])]()
                return _AzResult("" if payload is None else json.dumps(payload))

            config = {"resource_group_name": "rg-test", "expected_tenant_id": tenant, "expected_subscription_id": "sub", **extra_config}
            with self.subTest(label), patch(
                "shared.deployment_validation._unauthenticated_response",
                side_effect=lambda fqdn, path, browser, r=responses: r[path],
            ), patch(
                "shared.deployment_validation._client_secret_authenticates",
                side_effect=lambda tenant_id, client_id, secret: (tenant_id, client_id, secret) == (tenant, "app", "live"),
            ):
                result = cardiology_sign_in_check(config, az_run)
                self.assertEqual(result["status"], expected, result["detail"])


if __name__ == "__main__":
    unittest.main()
