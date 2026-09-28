#!/usr/bin/env python3
"""Masimo FHIR aggregator.

Every run summarizes the Masimo pulse-oximeter stream (Eventhouse table
TelemetryRaw) into one FHIR Observation per device, measure and completed
5-minute window, so the regular FHIR $export carries the telemetry into HDS
(bronze -> silver -> gold) with FHIR provenance.

- Windows are [start, start + WINDOW_MINUTES) on UTC boundaries, by reading time
  (todatetime(timestamp)). A run processes the LOOKBACK_WINDOWS most recent
  completed windows, which absorbs ingestion lag and one missed run.
- Observation ids are deterministic, so rewriting a window replaces it.
- A device without an active FHIR Basic device-assoc gets no Observation: the
  subject is never guessed.

Stdlib only. Tokens come from the Container Apps managed identity endpoint when
AZURE_CLIENT_ID is set, otherwise from the az CLI. SYNTHETIC DATA ONLY.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

SUBSCRIPTION_ID = "9bbee190-dc61-4c58-ab47-1275cb04018f"
TAG = {
    "system": "https://brakekat.com/hls/tags",
    "code": "masimo-telemetry-aggregate",
    "display": "Masimo telemetry 5-minute aggregate",
}
META_SOURCE = "https://brakekat.com/hls/masimo-fhir-aggregator"
UCUM = "http://unitsofmeasure.org"
VITAL_SIGNS = {
    "coding": [{
        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
        "code": "vital-signs",
        "display": "Vital Signs",
    }]
}
# Telemetry field -> the catalog v2 entry whose source is masimo-telemetry
# (cardiology-api/measure-catalog.json). tests/test_aggregator.py holds them equal.
MEASURES = {
    "pr": {"key": "pulseRate", "system": "http://loinc.org", "code": "8889-8",
           "display": "Heart rate by Pulse oximetry", "unit": "/min"},
    "spo2": {"key": "spo2", "system": "http://loinc.org", "code": "59408-5",
             "display": "Oxygen saturation in Arterial blood by Pulse oximetry", "unit": "%"},
}
DEVICE_EXTENSION_SUFFIXES = ("associated-device", "device-association-device")
STATUS_EXTENSION_SUFFIXES = ("association-status", "device-association-status")
BATCH_SIZE = 200
HTTP_TIMEOUT_SECONDS = 60

# One query for every device and window. The ingestion_time() bound only prunes
# extents: a reading cannot be ingested before it was taken, and 10 minutes
# covers device clock skew.
KQL = """declare query_parameters(window_from:datetime, window_to:datetime, window_size:timespan);
TelemetryRaw
| where ingestion_time() >= window_from - 10m
| extend ts = todatetime(timestamp)
| where ts >= window_from and ts < window_to
| extend pr = todouble(telemetry.pr), spo2 = todouble(telemetry.spo2)
| summarize pr_mean = avg(pr), pr_min = min(pr), pr_max = max(pr), pr_n = countif(isnotnull(pr)),
            spo2_mean = avg(spo2), spo2_min = min(spo2), spo2_max = max(spo2), spo2_n = countif(isnotnull(spo2))
    by device_id, window_start = bin(ts, window_size)"""


# ---------------------------------------------------------------- pure logic

def iso_z(moment):
    """UTC timestamp at second precision with a Z suffix."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def completed_windows(now, window_minutes, lookback):
    """Starts of the `lookback` most recent completed windows, oldest first."""
    size = window_minutes * 60
    current_start = int(now.timestamp()) // size * size
    return [datetime.fromtimestamp(current_start - size * k, tz=timezone.utc)
            for k in range(lookback, 0, -1)]


def observation_id(device_id, measure_key, window_start):
    digest = hashlib.sha256(f"{device_id}|{measure_key}|{iso_z(window_start)}".encode()).hexdigest()
    return "masimo-agg-" + digest[:40]


def round1(value):
    """Half-up to one decimal place (Python's round() is half-to-even)."""
    return float(Decimal(repr(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def build_observation(device_id, patient_ref, field, window_start, window_minutes,
                      mean, low, high, count, issued):
    measure = MEASURES[field]
    window_end = window_start + timedelta(minutes=window_minutes)
    return {
        "resourceType": "Observation",
        "id": observation_id(device_id, measure["key"], window_start),
        "meta": {"tag": [dict(TAG)], "source": META_SOURCE},
        "status": "final",
        "category": [VITAL_SIGNS],
        "code": {"coding": [{"system": measure["system"], "code": measure["code"],
                             "display": measure["display"]}]},
        "subject": {"reference": patient_ref},
        "device": {"reference": f"Device/{device_id}"},
        "effectivePeriod": {"start": iso_z(window_start), "end": iso_z(window_end)},
        "issued": iso_z(issued),
        "valueQuantity": {"value": round1(mean), "unit": measure["unit"],
                          "system": UCUM, "code": measure["unit"]},
        "note": [{"text": f"{window_minutes}-minute mean of {count} readings "
                          f"(min {low:g}, max {high:g}) from Masimo telemetry"}],
    }


def parse_associations(resources):
    """Map device id -> Patient reference from Basic device-assoc resources.

    Inactive associations are ignored. A device linked to more than one patient
    is ambiguous and left out, as is a link without a Patient subject.
    """
    patients_by_device = {}
    for resource in resources:
        extensions = resource.get("extension") or []
        status = next((e.get("valueCode") for e in extensions
                       if str(e.get("url", "")).endswith(STATUS_EXTENSION_SUFFIXES)), None)
        if status is not None and status != "active":
            continue
        subject = str((resource.get("subject") or {}).get("reference", ""))
        if not subject.startswith("Patient/") or len(subject) <= len("Patient/"):
            continue
        for extension in extensions:
            if not str(extension.get("url", "")).endswith(DEVICE_EXTENSION_SUFFIXES):
                continue
            reference = str((extension.get("valueReference") or {}).get("reference", ""))
            if reference.startswith("Device/") and len(reference) > len("Device/"):
                patients_by_device.setdefault(reference[len("Device/"):], set()).add(subject)
    unique = {d: next(iter(p)) for d, p in patients_by_device.items() if len(p) == 1}
    ambiguous = sorted(d for d, p in patients_by_device.items() if len(p) > 1)
    return unique, ambiguous


def parse_kusto_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_observations(rows, associations, window_minutes, issued):
    """Observations for KQL summary rows; returns (observations, unassociated device ids)."""
    observations, unassociated = [], set()
    for row in rows:
        device_id = row["device_id"]
        patient_ref = associations.get(device_id)
        if patient_ref is None:
            unassociated.add(device_id)
            continue
        window_start = parse_kusto_time(row["window_start"])
        for field in MEASURES:
            count = int(row[f"{field}_n"] or 0)
            if count == 0:
                continue
            observations.append(build_observation(
                device_id, patient_ref, field, window_start, window_minutes,
                row[f"{field}_mean"], row[f"{field}_min"], row[f"{field}_max"], count, issued))
    return observations, sorted(unassociated)


# ------------------------------------------------------------------ I/O

def get_token(resource, client_id):
    if client_id:
        endpoint = os.environ.get("IDENTITY_ENDPOINT")
        header = os.environ.get("IDENTITY_HEADER")
        if not endpoint or not header:
            raise RuntimeError("AZURE_CLIENT_ID is set but IDENTITY_ENDPOINT/IDENTITY_HEADER are not; "
                               "no managed identity endpoint is available")
        query = urllib.parse.urlencode({"api-version": "2019-08-01", "resource": resource,
                                        "client_id": client_id})
        request = urllib.request.Request(f"{endpoint}?{query}", headers={"X-IDENTITY-HEADER": header})
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.load(response)["access_token"]
    result = subprocess.run(
        ["az", "account", "get-access-token", "--subscription", SUBSCRIPTION_ID,
         "--resource", resource, "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"az account get-access-token failed for {resource}: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def http_json(method, url, token, body=None, content_type="application/json"):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {error.code}: {detail}") from None


def read_associations(fhir_url, token):
    resources = []
    url = f"{fhir_url}/Basic?code=device-assoc&_count=200"
    while url:
        bundle = http_json("GET", url, token)
        resources.extend(e["resource"] for e in bundle.get("entry", []) if "resource" in e)
        url = next((link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"), None)
    return resources


def query_kusto(query_uri, database, token, window_from, window_to, window_minutes):
    body = {
        "db": database,
        "csl": KQL,
        "properties": {"Parameters": {
            "window_from": f"datetime({iso_z(window_from)})",
            "window_to": f"datetime({iso_z(window_to)})",
            "window_size": f"time({window_minutes}m)",
        }},
    }
    frames = http_json("POST", f"{query_uri}/v2/rest/query", token, body)
    table = None
    for frame in frames:
        if frame.get("FrameType") == "DataSetCompletion" and (frame.get("HasErrors") or frame.get("Cancelled")):
            raise RuntimeError(f"KQL query failed: {json.dumps(frame.get('OneApiErrors'))[:500]}")
        if frame.get("FrameType") == "DataTable" and frame.get("TableKind") == "PrimaryResult":
            table = frame
    if table is None:
        raise RuntimeError("KQL query returned no primary result")
    columns = [c["ColumnName"] for c in table["Columns"]]
    return [dict(zip(columns, row)) for row in table["Rows"]]


def write_observations(fhir_url, token, observations):
    batches = 0
    for offset in range(0, len(observations), BATCH_SIZE):
        chunk = observations[offset:offset + BATCH_SIZE]
        bundle = {
            "resourceType": "Bundle",
            "type": "batch",
            "entry": [{"resource": o, "request": {"method": "PUT", "url": f"Observation/{o['id']}"}}
                      for o in chunk],
        }
        result = http_json("POST", fhir_url, token, bundle, content_type="application/fhir+json")
        entries = result.get("entry", [])
        failed = [(o["id"], (e.get("response") or {}).get("status", "missing"))
                  for o, e in zip(chunk, entries)
                  if not str((e.get("response") or {}).get("status", "")).startswith(("200", "201"))]
        if len(entries) != len(chunk) or failed:
            raise RuntimeError(f"FHIR batch {batches + 1}: {len(failed)} failed of {len(chunk)} "
                               f"({len(entries)} responses); first: {failed[:3]}")
        batches += 1
    return batches


# ------------------------------------------------------------------ entry

def load_settings():
    missing = [n for n in ("FHIR_URL", "KUSTO_QUERY_URI", "KUSTO_DATABASE") if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"missing environment: {', '.join(missing)}")
    window_minutes = int(os.environ.get("WINDOW_MINUTES", "5"))
    lookback = int(os.environ.get("LOOKBACK_WINDOWS", "3"))
    if window_minutes < 1 or 1440 % window_minutes:
        raise RuntimeError("WINDOW_MINUTES must divide a day into whole windows")
    if lookback < 1:
        raise RuntimeError("LOOKBACK_WINDOWS must be at least 1")
    return {
        "fhir_url": os.environ["FHIR_URL"].rstrip("/"),
        "kusto_uri": os.environ["KUSTO_QUERY_URI"].rstrip("/"),
        "kusto_database": os.environ["KUSTO_DATABASE"],
        "client_id": os.environ.get("AZURE_CLIENT_ID") or None,
        "window_minutes": window_minutes,
        "lookback": lookback,
    }


def run(dry_run):
    started = time.monotonic()
    settings = load_settings()
    now = datetime.now(timezone.utc)
    windows = completed_windows(now, settings["window_minutes"], settings["lookback"])
    window_to = windows[-1] + timedelta(minutes=settings["window_minutes"])

    fhir_token = get_token(settings["fhir_url"], settings["client_id"])
    associations, ambiguous = parse_associations(read_associations(settings["fhir_url"], fhir_token))
    kusto_token = get_token(settings["kusto_uri"], settings["client_id"])
    rows = query_kusto(settings["kusto_uri"], settings["kusto_database"], kusto_token,
                       windows[0], window_to, settings["window_minutes"])
    observations, unassociated = build_observations(rows, associations, settings["window_minutes"], now)

    batches = 0 if dry_run else write_observations(settings["fhir_url"], fhir_token, observations)
    if dry_run and observations:
        print(json.dumps({"event": "masimo-fhir-aggregator.sample", "observation": observations[0]}))
    return {
        "event": "masimo-fhir-aggregator.run",
        "status": "succeeded",
        "dry_run": dry_run,
        "windows": [iso_z(w) for w in windows],
        "associations": len(associations),
        "ambiguous_devices": ambiguous,
        "summary_rows": len(rows),
        "devices_with_readings": len({r["device_id"] for r in rows}),
        "devices_without_association": unassociated,
        "observations": len(observations),
        "written": 0 if dry_run else len(observations),
        "batches": batches,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true",
                        help="process the latest completed windows and exit (the default; the job schedule is the loop)")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print a summary and one sample Observation; write nothing")
    args = parser.parse_args(argv)
    try:
        summary = run(args.dry_run)
    except Exception as error:  # one failure line, then a non-zero exit for the job
        print(json.dumps({"event": "masimo-fhir-aggregator.run", "status": "failed",
                          "dry_run": args.dry_run, "error": str(error)[:1000]}))
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
