"""Activity: deploy the Phase 8 cardiology Container App."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from activities.invoke_powershell import (
    _ps_array_literal,
    _ps_hashtable_literal,
    _ps_single_quoted,
    _run_powershell,
)

SCRIPT = Path(__file__).resolve().parents[2] / "phase-8" / "deploy-cardiology-app.ps1"


def run(config: dict[str, Any], resources: dict[str, Any]) -> dict[str, Any]:
    """Invoke the Phase 8 script with the orchestrator config."""
    start = time.time()
    params = [
        f"-ResourceGroupName {_ps_single_quoted(config.get('resource_group_name', 'rg-medtech-rti-fhir'))}",
        f"-Location {_ps_single_quoted(config.get('location', 'eastus'))}",
        f"-ExpectedTenantId {_ps_single_quoted(config.get('expected_tenant_id', '8d038e6a-9b7d-4cb8-bbcf-e84dff156478'))}",
        f"-ExpectedSubscriptionId {_ps_single_quoted(config.get('expected_subscription_id', '9bbee190-dc61-4c58-ab47-1275cb04018f'))}",
        f"-Tags {_ps_hashtable_literal(config.get('tags', {}))}",
    ]
    if config.get("cardiology_app_path"):
        params.append(f"-CardiologyAppPath {_ps_single_quoted(config['cardiology_app_path'])}")
    if config.get("cardiology_app_users"):
        params.append(f"-CardiologyAppUsers {_ps_array_literal(config['cardiology_app_users'])}")

    command = f"& {_ps_single_quoted(str(SCRIPT))} {' '.join(params)}"
    exit_code = _run_powershell(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command]
    )
    if exit_code != 0:
        raise RuntimeError(f"deploy-cardiology-app.ps1 exited with code {exit_code}")

    return {
        "phase": "Phase 8: Cardiology App",
        "duration_seconds": time.time() - start,
        "exit_code": exit_code,
        "resources": {"cardiology_app": "deployed"},
    }
