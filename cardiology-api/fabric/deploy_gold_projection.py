#!/usr/bin/env python3
"""Deploy, and optionally run, the cardiology gold projection notebook in Microsoft Fabric.

Creates or updates the Notebook item ``cardiology_gold_projection`` in the workspace from
``cardiology_gold_projection.py`` (Fabric notebook source), fills its workspace/lakehouse IDs,
binds it to the workspace Spark environment, and with ``--run`` starts a RunNotebook job and
waits for it to finish. Re-running is idempotent: an existing item's definition is replaced.

Before any Fabric call the notebook's embedded ``MEASURE_CATALOG`` must equal
``cardiology-api/measure-catalog.json``; otherwise nothing is deployed.

Auth: Azure CLI token pinned to the subscription (never ``--tenant``). Tokens are never printed.

    python3 cardiology-api/fabric/deploy_gold_projection.py          # deploy only
    python3 cardiology-api/fabric/deploy_gold_projection.py --run    # deploy, run, wait

Standard library only. SYNTHETIC DATA ONLY.
"""

from __future__ import annotations

import argparse
import ast
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOTEBOOK_SOURCE = HERE / "cardiology_gold_projection.py"
MEASURE_CATALOG = HERE.parent / "measure-catalog.json"

FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_AUDIENCE = "https://api.fabric.microsoft.com"
NOTEBOOK_NAME = "cardiology_gold_projection"
NOTEBOOK_DESCRIPTION = "Projects the synthetic Caldova cardiology cohort from HDS Silver into Reporting Gold."

DEFAULT_SUBSCRIPTION = "9bbee190-dc61-4c58-ab47-1275cb04018f"
DEFAULT_WORKSPACE_ID = "f8f84d68-cfa1-4460-95d1-943fac43248a"  # med-0906
DEFAULT_SILVER_LAKEHOUSE = "healthcare1_msft_silver"
DEFAULT_GOLD_LAKEHOUSE = "healthcare1_reporting_gold"
DEFAULT_ENVIRONMENT = "healthcare1_msft_environment"

TERMINAL_JOB_STATES = {"Completed", "Failed", "Cancelled", "Deduped"}


# ── Measure catalog parity ─────────────────────────────────────────────────────────


def catalog_measures(catalog_path: Path = MEASURE_CATALOG) -> list[dict[str, str]]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    return [
        {"key": m["key"], "system": m["code"]["system"], "code": m["code"]["code"], "unit": m["unit"]}
        for m in catalog["measures"]
    ]


def notebook_measures(source: str) -> list[dict[str, str]]:
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MEASURE_CATALOG" for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("notebook source has no top-level MEASURE_CATALOG literal")


def assert_measures_match(source: str, catalog_path: Path = MEASURE_CATALOG) -> None:
    expected = catalog_measures(catalog_path)
    actual = notebook_measures(source)
    if actual != expected:
        raise SystemExit(
            "Refusing to deploy: notebook MEASURE_CATALOG differs from measure-catalog.json\n"
            f"  notebook: {json.dumps(actual)}\n  catalog:  {json.dumps(expected)}"
        )


# ── Notebook rendering ─────────────────────────────────────────────────────────────


def render_source(source: str, workspace_id: str, silver_id: str, gold_id: str, environment_id: str) -> str:
    """Fill the empty placeholders shipped in the notebook source; each must occur exactly once."""
    replacements = [
        ('\nWORKSPACE_ID = ""\n', f'\nWORKSPACE_ID = "{workspace_id}"\n'),
        ('\nSILVER_LAKEHOUSE_ID = ""\n', f'\nSILVER_LAKEHOUSE_ID = "{silver_id}"\n'),
        ('\nGOLD_LAKEHOUSE_ID = ""\n', f'\nGOLD_LAKEHOUSE_ID = "{gold_id}"\n'),
        ('# META       "environmentId": "",\n', f'# META       "environmentId": "{environment_id}",\n'),
        ('# META       "workspaceId": ""\n', f'# META       "workspaceId": "{workspace_id}"\n'),
    ]
    for placeholder, value in replacements:
        count = source.count(placeholder)
        if count != 1:
            raise SystemExit(f"Notebook placeholder {placeholder.strip()!r} found {count} times; expected exactly 1")
        source = source.replace(placeholder, value)
    return source


# ── Fabric REST ────────────────────────────────────────────────────────────────────


class Fabric:
    def __init__(self, subscription: str) -> None:
        self.subscription = subscription
        self._token = ""
        self._expires_at = 0.0

    def _bearer(self) -> str:
        if time.time() > self._expires_at - 300:
            raw = subprocess.run(
                ["az", "account", "get-access-token", "--subscription", self.subscription,
                 "--resource", FABRIC_AUDIENCE, "-o", "json"],
                check=True, capture_output=True, text=True,
            ).stdout
            token = json.loads(raw)
            self._token = token["accessToken"]
            self._expires_at = float(token.get("expires_on") or time.time() + 1800)
        return self._token

    def request(self, method: str, url: str, body: dict | None = None, attempts: int = 6):
        """Return (status, headers, parsed JSON or None); retries throttling and transient failures."""
        if not url.startswith("https://"):
            url = FABRIC_API + url
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self._bearer()}",
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    raw = response.read()
                    return response.status, response.headers, json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                text = error.read().decode("utf-8", "replace")
                retryable = error.code == 429 or error.code >= 500 or (error.code == 403 and "RequestDeniedByInboundPolicy" in text)
                if retryable and attempt < attempts:
                    delay = int(error.headers.get("Retry-After") or 0) or min(60, 10 * attempt)
                    print(f"  HTTP {error.code} on {method} {url}; retrying in {delay}s ({attempt}/{attempts})")
                    time.sleep(delay)
                    continue
                raise SystemExit(f"Fabric {method} {url} failed: HTTP {error.code}: {text[:2000]}") from None
        raise AssertionError("unreachable")

    def wait_operation(self, headers, timeout_seconds: int = 600):
        """Follow a 202 long-running operation; return its result payload, if any."""
        location = headers.get("Location")
        operation_id = headers.get("x-ms-operation-id")
        url = location or f"{FABRIC_API}/operations/{operation_id}"
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            time.sleep(int(headers.get("Retry-After") or 5))
            _, headers, state = self.request("GET", url)
            status = (state or {}).get("status")
            if status == "Succeeded":
                try:
                    return self.request("GET", url.rstrip("/") + "/result", attempts=2)[2]
                except SystemExit:
                    return None  # Operations without a result body (e.g. updateDefinition).
            if status in {"Failed", "Cancelled"}:
                raise SystemExit(f"Fabric operation {status}: {json.dumps((state or {}).get('error'))}")
        raise SystemExit(f"Fabric operation timed out after {timeout_seconds}s: {url}")

    def find_item(self, workspace_id: str, item_type: str, name: str) -> dict | None:
        url = f"/workspaces/{workspace_id}/items?type={item_type}"
        while url:
            _, _, page = self.request("GET", url)
            for item in page.get("value", []):
                if item.get("displayName") == name:
                    return item
            url = page.get("continuationUri")
        return None

    def require_item(self, workspace_id: str, item_type: str, name: str) -> str:
        item = self.find_item(workspace_id, item_type, name)
        if not item:
            raise SystemExit(f"{item_type} {name!r} not found in workspace {workspace_id}")
        return item["id"]


def deploy(fabric: Fabric, workspace_id: str, source: str) -> str:
    definition = {"parts": [{
        "path": "notebook-content.py",
        "payload": base64.b64encode(source.encode("utf-8")).decode("ascii"),
        "payloadType": "InlineBase64",
    }]}
    existing = fabric.find_item(workspace_id, "Notebook", NOTEBOOK_NAME)
    if existing:
        status, headers, _ = fabric.request(
            "POST", f"/workspaces/{workspace_id}/items/{existing['id']}/updateDefinition", {"definition": definition})
        if status == 202:
            fabric.wait_operation(headers)
        print(f"Updated notebook {NOTEBOOK_NAME} ({existing['id']})")
        return existing["id"]

    status, headers, created = fabric.request("POST", f"/workspaces/{workspace_id}/items", {
        "displayName": NOTEBOOK_NAME,
        "type": "Notebook",
        "description": NOTEBOOK_DESCRIPTION,
        "definition": definition,
    })
    if status == 202:
        created = fabric.wait_operation(headers)
    item_id = (created or {}).get("id") or fabric.require_item(workspace_id, "Notebook", NOTEBOOK_NAME)
    print(f"Created notebook {NOTEBOOK_NAME} ({item_id})")
    return item_id


def run_notebook(fabric: Fabric, workspace_id: str, item_id: str, timeout_minutes: int) -> dict:
    _, headers, _ = fabric.request("POST", f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances?jobType=RunNotebook", {})
    job_url = headers.get("Location")
    if not job_url:
        raise SystemExit("RunNotebook response carried no Location header")
    print(f"Started RunNotebook job instance {job_url.rstrip('/').rsplit('/', 1)[-1]}")
    deadline = time.time() + timeout_minutes * 60
    last_status = None
    while time.time() < deadline:
        _, response_headers, job = fabric.request("GET", job_url)
        status = (job or {}).get("status")
        if status != last_status:
            print(f"  [{time.strftime('%H:%M:%S')}] {status}")
            last_status = status
        if status in TERMINAL_JOB_STATES:
            return job
        time.sleep(int(response_headers.get("Retry-After") or 0) or 20)
    raise SystemExit(f"Notebook job still {last_status} after {timeout_minutes} minutes: {job_url}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", action="store_true", help="run the notebook after deploying and wait for completion")
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--subscription", default=DEFAULT_SUBSCRIPTION)
    parser.add_argument("--silver-lakehouse", default=DEFAULT_SILVER_LAKEHOUSE)
    parser.add_argument("--gold-lakehouse", default=DEFAULT_GOLD_LAKEHOUSE)
    parser.add_argument("--environment", default=DEFAULT_ENVIRONMENT)
    parser.add_argument("--timeout-minutes", type=int, default=60)
    args = parser.parse_args(argv)

    source = NOTEBOOK_SOURCE.read_text(encoding="utf-8")
    assert_measures_match(source)
    print("MEASURE_CATALOG matches measure-catalog.json")

    fabric = Fabric(args.subscription)
    workspace = args.workspace_id
    silver_id = fabric.require_item(workspace, "Lakehouse", args.silver_lakehouse)
    gold_id = fabric.require_item(workspace, "Lakehouse", args.gold_lakehouse)
    environment_id = fabric.require_item(workspace, "Environment", args.environment)
    print(f"Silver {args.silver_lakehouse} = {silver_id}; Gold {args.gold_lakehouse} = {gold_id}; "
          f"Environment {args.environment} = {environment_id}")

    item_id = deploy(fabric, workspace, render_source(source, workspace, silver_id, gold_id, environment_id))
    if not args.run:
        return 0

    job = run_notebook(fabric, workspace, item_id, args.timeout_minutes)
    print(f"Notebook {item_id} job {job.get('id')} finished: {job.get('status')} "
          f"({job.get('startTimeUtc')} -> {job.get('endTimeUtc')})")
    if job.get("status") != "Completed":
        print(f"Failure detail: {json.dumps(job.get('failureReason'), indent=1)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
