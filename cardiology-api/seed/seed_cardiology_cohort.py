#!/usr/bin/env python3
"""Seed synthetic Caldova cardiology foundation data onto existing FHIR Patients.

Enriches 24 existing Synthea patients in the HDS FHIR service with the resources the cardiology
contract defines (tagged in-progress Encounter, Condition, CareTeam, optional Device and
DeviceUseStatement, and Observation series). No Patient resources are created. SYNTHETIC DATA ONLY.

Modes (every mode is a dry run unless --apply is given):
  (default)          plan the cohort and print counts plus one sample per resource type
  --apply            PUT every planned resource in FHIR batch Bundles, and retract retired measures
  --remove           retract every resource carrying the cardiology tag (PUT, entered-in-error)
  --refresh          write one new observation slot at --as-of per enrolled subject

Pulse rate and SpO2 come from the Masimo pulse-oximeter stream (measure catalog v2), so this script
no longer generates them. --apply retracts every tagged Observation still carrying a retired code
(LOINC 8867-4 heart rate, 2708-6 SpO2) by PUTting it back with status entered-in-error; the
incremental FHIR export propagates that, where a DELETE would not. Already-retracted ones are skipped.
--remove retracts the same export-visible way and never DELETEs: Encounter, CareTeam, Device,
DeviceUseStatement and Observation get status entered-in-error; a Condition loses clinicalStatus and
gets verificationStatus entered-in-error (condition-ver-status). The tag stays.

Resource ids are deterministic (cal- + sha256(patientId|kind|slot)[:40]) and observation slots
are keyed by their effective time, so rerunning --apply with the same --as-of changes nothing.
A later --as-of adds a new observation window beside the earlier one.

Python 3 standard library only. Authentication uses the az CLI pinned to the HDS subscription.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FHIR_URL = "https://hdwsfrzkspw34dzci-fhirfrzkspw34dzci.fhir.azurehealthcareapis.com"
SUBSCRIPTION = "9bbee190-dc61-4c58-ab47-1275cb04018f"
SEED = 20260901
COHORT_SIZE = 24
MIN_AGE, MAX_AGE = 45, 89
BATCH_LIMIT = 200
MAX_ATTEMPTS = 6

TAG_SYSTEM = "https://brakekat.com/hls/tags"
TAG_CODE = "synthetic-caldova-cardiology"
TAG = {"system": TAG_SYSTEM, "code": TAG_CODE, "display": "Synthetic Caldova cardiology demo data"}
TAG_PARAM = f"{TAG_SYSTEM}|{TAG_CODE}"

CATALOG_PATH = Path(__file__).resolve().parent.parent / "measure-catalog.json"

SNOMED = "http://snomed.info/sct"
UCUM = "http://unitsofmeasure.org"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
CONDITION_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
CONDITION_VERIFICATION = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
CONDITION_CATEGORY = "http://terminology.hl7.org/CodeSystem/condition-category"
OBSERVATION_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
DEVICE_TELEMETRY_CATEGORY = "https://brakekat.com/hls/cardiology/observation-category"

ENCOUNTER_CLASS = {
    "inpatient": ("IMP", "inpatient encounter"),
    "outpatient": ("AMB", "ambulatory"),
    "home-monitored": ("HH", "home health"),
}
# (code, display, inpatient only)
CONDITIONS = [
    ("84114007", "Heart failure", False),
    ("739024006", "Transplanted heart present", False),
    ("85898001", "Cardiomyopathy", False),
    ("282825002", "Paroxysmal atrial fibrillation", False),
    ("89138009", "Cardiogenic shock", True),
]
ROLES = [
    ("17561000", "Cardiologist", False),
    ("309339007", "Adult intensive care specialist", True),
]
DEVICE_TYPES = [
    ("360064003", "Ventricular assist device", False),
    ("711337005", "Implantable pulmonary artery pressure monitoring system", False),
    ("470646006", "Implantable cardiac monitor", False),
    ("129113006", "Intra-aortic balloon pump", True),
]

# Observation category per measure: (system, code, display).
MEASURE_CATEGORY = {
    "lactate": (OBSERVATION_CATEGORY, "laboratory", "Laboratory"),
    "map": (OBSERVATION_CATEGORY, "vital-signs", "Vital Signs"),
    "deviceFlow": (DEVICE_TELEMETRY_CATEGORY, "device-telemetry", "Device telemetry"),
}
# Series cadence and window per measure: slots fall in (as_of - window, as_of].
SERIES = {
    "map": (timedelta(minutes=15), timedelta(hours=6)),
    "lactate": (timedelta(hours=4), timedelta(hours=12)),
    "deviceFlow": (timedelta(minutes=5), timedelta(minutes=60)),
}
# Baseline value from per-subject stress in [0, 0.75).
BASELINE = {
    "lactate": lambda s: 1.0 + 3.2 * s,
    "map": lambda s: 85 - 24 * s,
    "deviceFlow": lambda s: 100 - 36 * s,
}
# Half-width of the uniform per-slot variation (seed) and per-refresh drift.
VARIATION = {"lactate": 0.15, "map": 2.0, "deviceFlow": 2.0}
DRIFT = {"lactate": 0.1, "map": 1.5, "deviceFlow": 1.5}
BOUNDS = {"lactate": (0.3, 20.0), "map": (30.0, 140.0), "deviceFlow": (0.0, 120.0)}
DECIMALS = {"lactate": 2, "map": 1, "deviceFlow": 1}

# Codes the seed generated before measure catalog v2 (ECG heart rate, arterial blood-gas SpO2).
RETIRED_CODES = [("http://loinc.org", "8867-4"), ("http://loinc.org", "2708-6")]
RETRACTED_STATUS = "entered-in-error"
CONDITION_VER_STATUS = "http://terminology.hl7.org/CodeSystem/condition-ver-status"

# PUT order keeps referenced resources ahead of referrers.
RESOURCE_TYPES = ["Encounter", "Condition", "CareTeam", "Device", "DeviceUseStatement", "Observation"]


# ---------------------------------------------------------------------------------------------
# Deterministic helpers


def fhir_id(patient_id: str, kind: str, slot: str) -> str:
    return "cal-" + hashlib.sha256(f"{patient_id}|{kind}|{slot}".encode()).hexdigest()[:40]


def seeded(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, (SEED, *parts))).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def fhir_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime:
    moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def truncate_minute(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(second=0, microsecond=0)


def age_on(birth_date: str, today: date) -> int:
    born = date.fromisoformat(birth_date[:10])
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def shaped(measure: str, value: float) -> float | int:
    low, high = BOUNDS[measure]
    value = round(min(max(value, low), high), DECIMALS[measure])
    return int(value) if DECIMALS[measure] == 0 else value


def slots(measure: str, as_of: datetime) -> list[datetime]:
    cadence, window = SERIES[measure]
    return [as_of - cadence * k for k in range(window // cadence)][::-1]


def reference_id(reference: dict | None) -> str | None:
    text = (reference or {}).get("reference", "")
    return text.rsplit("/", 1)[-1] if text else None


def load_catalog() -> dict[str, dict]:
    catalog = json.loads(CATALOG_PATH.read_text())
    measures = {m["key"]: m for m in catalog["measures"]}
    missing = set(BASELINE) - set(measures)
    if missing:
        raise SystemExit(f"measure-catalog.json is missing measures: {sorted(missing)}")
    return measures


# ---------------------------------------------------------------------------------------------
# Resource builders


def tagged(resource: dict) -> dict:
    return {"resourceType": resource.pop("resourceType"), "id": resource.pop("id"),
            "meta": {"tag": [dict(TAG)]}, **resource}


def concept(system: str, code: str, display: str) -> dict:
    return {"coding": [{"system": system, "code": code, "display": display}], "text": display}


def observation(catalog: dict, patient_id: str, encounter_id: str, measure: str, when: datetime,
                value: float | int, device_id: str | None) -> dict:
    spec = catalog[measure]
    category = MEASURE_CATEGORY[measure]
    resource = {
        "resourceType": "Observation",
        "id": fhir_id(patient_id, measure, fhir_time(when)),
        "status": "final",
        "category": [concept(*category)],
        "code": concept(spec["code"]["system"], spec["code"]["code"], spec["code"]["display"]),
        "subject": {"reference": f"Patient/{patient_id}"},
        "encounter": {"reference": f"Encounter/{encounter_id}"},
        "effectiveDateTime": fhir_time(when),
        "valueQuantity": {"value": value, "unit": spec["unit"], "system": UCUM, "code": spec["unit"]},
    }
    if device_id:
        resource["device"] = {"reference": f"Device/{device_id}"}
    return tagged(resource)


def plan_subject(catalog: dict, patient: dict, practitioner_ids: list[str], as_of: datetime,
                 serials: set[str]) -> dict:
    pid = patient["id"]
    rand = seeded(pid, "subject")
    setting = "home-monitored" if rand.random() < 0.45 else ("outpatient" if rand.random() < 0.6 else "inpatient")
    has_device = setting != "outpatient" or rand.random() < 0.5
    stress = rand.random() * 0.35 if rand.random() < 0.72 else 0.35 + rand.random() * 0.4
    allowed = lambda options: [o for o in options if setting == "inpatient" or not o[2]]
    condition = rand.choice(allowed(CONDITIONS))
    role = rand.choice(allowed(ROLES))
    practitioner_id = rand.choice(practitioner_ids)
    device_type = rand.choice(allowed(DEVICE_TYPES))
    enrolled_at = as_of - timedelta(minutes=rand.randint(1 * 1440, 10 * 1440))
    associated_at = as_of - timedelta(minutes=rand.randint(1 * 1440, 10 * 1440))
    serial_number = rand.randint(0, 9999)
    while f"DEV-{serial_number:04d}" in serials:
        serial_number = (serial_number + 1) % 10000
    serial = f"DEV-{serial_number:04d}"

    patient_ref = {"reference": f"Patient/{pid}"}
    encounter_id = fhir_id(pid, "encounter", "enrollment")
    encounter_ref = {"reference": f"Encounter/{encounter_id}"}
    class_code, class_display = ENCOUNTER_CLASS[setting]
    resources = [
        tagged({
            "resourceType": "Encounter", "id": encounter_id, "status": "in-progress",
            "class": {"system": ACT_CODE, "code": class_code, "display": class_display},
            "subject": patient_ref, "period": {"start": fhir_time(enrolled_at)},
        }),
        tagged({
            "resourceType": "Condition", "id": fhir_id(pid, "condition", "primary"),
            "clinicalStatus": concept(CONDITION_CLINICAL, "active", "Active"),
            "verificationStatus": concept(CONDITION_VERIFICATION, "confirmed", "Confirmed"),
            "category": [concept(CONDITION_CATEGORY, "encounter-diagnosis", "Encounter Diagnosis")],
            "code": concept(SNOMED, condition[0], condition[1]),
            "subject": patient_ref, "encounter": encounter_ref, "recordedDate": fhir_time(enrolled_at),
        }),
        tagged({
            "resourceType": "CareTeam", "id": fhir_id(pid, "careteam", "primary"), "status": "active",
            "name": "Caldova cardiology care team", "subject": patient_ref, "encounter": encounter_ref,
            "participant": [{"role": [concept(SNOMED, role[0], role[1])],
                             "member": {"reference": f"Practitioner/{practitioner_id}"}}],
        }),
    ]
    device_id = None
    if has_device:
        serials.add(serial)
        device_id = fhir_id(pid, "device", "primary")
        resources += [
            tagged({
                "resourceType": "Device", "id": device_id, "status": "active", "serialNumber": serial,
                "type": concept(SNOMED, device_type[0], device_type[1]), "patient": patient_ref,
            }),
            tagged({
                "resourceType": "DeviceUseStatement", "id": fhir_id(pid, "device-use", "primary"),
                "status": "active", "subject": patient_ref, "device": {"reference": f"Device/{device_id}"},
                "timingPeriod": {"start": fhir_time(associated_at)},
            }),
        ]
    for measure, baseline in BASELINE.items():
        if measure == "deviceFlow" and not device_id:
            continue
        for when in slots(measure, as_of):
            jitter = seeded(pid, measure, fhir_time(when)).uniform(-VARIATION[measure], VARIATION[measure])
            value = shaped(measure, baseline(stress) + jitter)
            resources.append(observation(catalog, pid, encounter_id, measure, when, value,
                                         device_id if measure == "deviceFlow" else None))
    return {
        "patient_id": pid, "care_setting": setting, "stress": round(stress, 3),
        "age": None, "condition": condition[1], "device": device_type[1] if device_id else None,
        "resources": resources,
    }


def select_enrollees(patients: list[dict], today: date) -> list[tuple[dict, int]]:
    selected = []
    for patient in sorted(patients, key=lambda p: p["id"]):
        if any(key.startswith("deceased") for key in patient) or not patient.get("birthDate"):
            continue
        age = age_on(patient["birthDate"], today)
        if MIN_AGE <= age <= MAX_AGE:
            selected.append((patient, age))
        if len(selected) == COHORT_SIZE:
            return selected
    raise SystemExit(f"only {len(selected)} alive patients aged {MIN_AGE}-{MAX_AGE}; need {COHORT_SIZE}")


# ---------------------------------------------------------------------------------------------
# FHIR client


class Fhir:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.token = self._token()

    def _token(self) -> str:
        done = subprocess.run(
            ["az", "account", "get-access-token", "--subscription", SUBSCRIPTION, "--resource", self.base,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True)
        if done.returncode != 0 or not done.stdout.strip():
            raise SystemExit(f"az token acquisition failed: {done.stderr.strip()}")
        return done.stdout.strip()

    def request(self, method: str, target: str, body: dict | None = None) -> dict:
        url = target if target.startswith("http") else f"{self.base}/{target}"
        if not url.startswith(self.base + "/"):
            raise SystemExit(f"refusing to send credentials outside {self.base}: {url}")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/fhir+json",
                "Content-Type": "application/fhir+json"})
            try:
                with urllib.request.urlopen(req, timeout=180) as response:
                    payload = response.read()
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")[:2000]
                if error.code == 401 and attempt == 1:
                    self.token = self._token()
                    continue
                if (error.code == 429 or error.code >= 500) and attempt < MAX_ATTEMPTS:
                    time.sleep(backoff(attempt, error.headers.get("Retry-After")))
                    continue
                raise SystemExit(f"{method} {url} -> HTTP {error.code}: {detail}")
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(backoff(attempt, None))
                    continue
                raise SystemExit(f"{method} {url} failed: {error}")
        raise AssertionError("unreachable")

    def search(self, query: str) -> list[dict]:
        found, target = [], query
        while target:
            bundle = self.request("GET", target)
            found += [entry["resource"] for entry in bundle.get("entry", []) if "resource" in entry]
            target = next((link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"), None)
        return found

    def count(self, query: str) -> int:
        return int(self.request("GET", f"{query}&_summary=count")["total"])

    def batch(self, entries: list[dict]) -> None:
        """Send entries in batch Bundles of at most BATCH_LIMIT; retry 429/5xx entries; fail on the rest."""
        for offset in range(0, len(entries), BATCH_LIMIT):
            pending = entries[offset:offset + BATCH_LIMIT]
            for attempt in range(1, MAX_ATTEMPTS + 1):
                reply = self.request("POST", f"{self.base}/", {"resourceType": "Bundle", "type": "batch",
                                                                "entry": pending})
                results = reply.get("entry", [])
                if len(results) != len(pending):
                    raise SystemExit(f"batch returned {len(results)} entries for {len(pending)} requests")
                retry, failures = [], []
                for sent, result in zip(pending, results):
                    status_text = result.get("response", {}).get("status", "")
                    status = int(status_text.split()[0]) if status_text[:3].isdigit() else 0
                    if 200 <= status < 300:
                        continue
                    if status == 429 or status >= 500:
                        retry.append(sent)
                    else:
                        outcome = json.dumps(result.get("response", {}).get("outcome", {}))[:500]
                        failures.append(f"{sent['request']['method']} {sent['request']['url']} -> {status_text} {outcome}")
                if failures:
                    raise SystemExit("FHIR batch entries failed:\n  " + "\n  ".join(failures))
                if not retry:
                    break
                if attempt == MAX_ATTEMPTS:
                    raise SystemExit(f"{len(retry)} batch entries still throttled/failing after {MAX_ATTEMPTS} attempts")
                print(f"  retrying {len(retry)} throttled entries (attempt {attempt + 1})")
                time.sleep(backoff(attempt, None))
                pending = retry
            print(f"  batch {offset // BATCH_LIMIT + 1}: {min(offset + BATCH_LIMIT, len(entries))}/{len(entries)} done")


def backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after and retry_after.strip().isdigit():
        return min(float(retry_after), 60.0)
    return min(2.0 ** attempt, 30.0)


def put_entries(resources: list[dict]) -> list[dict]:
    return [{"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}}
            for r in resources]


def tagged_counts(fhir: Fhir) -> dict[str, int]:
    counts = {t: fhir.count(f"{t}?_tag={TAG_PARAM}") for t in RESOURCE_TYPES}
    counts["Encounter(in-progress)"] = fhir.count(f"Encounter?_tag={TAG_PARAM}&status=in-progress")
    return counts


def type_counts(resources: list[dict]) -> dict[str, int]:
    counts = {t: 0 for t in RESOURCE_TYPES}
    for resource in resources:
        counts[resource["resourceType"]] += 1
    return counts


def print_samples(resources: list[dict]) -> None:
    shown = set()
    for resource in resources:
        if resource["resourceType"] not in shown:
            shown.add(resource["resourceType"])
            print(f"--- sample {resource['resourceType']} ---")
            print(json.dumps(resource, indent=2))


def plan_retractions(fhir: Fhir) -> list[dict]:
    """Tagged Observations with a retired code that are not yet entered-in-error, marked for PUT."""
    codes = urllib.parse.quote(",".join(f"{system}|{code}" for system, code in RETIRED_CODES), safe="")
    retractions = []
    for obs in fhir.search(f"Observation?_tag={TAG_PARAM}&code={codes}&_count=1000"):
        if obs.get("resourceType") != "Observation" or obs.get("status") == RETRACTED_STATUS:
            continue
        retractions.append({**obs, "meta": unversioned(obs.get("meta", {})), "status": RETRACTED_STATUS})
    return retractions


def unversioned(meta: dict) -> dict:
    """meta for a PUT back: the server assigns versionId and lastUpdated; the tag is kept."""
    return {k: v for k, v in meta.items() if k not in ("versionId", "lastUpdated")}


def retracted(resource: dict) -> dict | None:
    """The export-visible retraction of a tagged resource, or None when it is already retracted."""
    meta = unversioned(resource.get("meta", {}))
    if resource["resourceType"] == "Condition":
        verification = resource.get("verificationStatus", {}).get("coding", [])
        if "clinicalStatus" not in resource and any(
                c.get("system") == CONDITION_VER_STATUS and c.get("code") == RETRACTED_STATUS for c in verification):
            return None
        # FHIR R4 con-5: clinicalStatus SHALL NOT be present when verificationStatus is entered-in-error.
        condition = {k: v for k, v in resource.items() if k != "clinicalStatus"}
        return {**condition, "meta": meta,
                "verificationStatus": concept(CONDITION_VER_STATUS, RETRACTED_STATUS, "Entered in Error")}
    if resource.get("status") == RETRACTED_STATUS:
        return None
    return {**resource, "meta": meta, "status": RETRACTED_STATUS}


def retired_code_counts(resources: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for resource in resources:
        for coding in resource.get("code", {}).get("coding", []):
            if (coding.get("system"), coding.get("code")) in RETIRED_CODES:
                counts[coding["code"]] = counts.get(coding["code"], 0) + 1
    return counts



# ---------------------------------------------------------------------------------------------
# Modes


def run_seed(fhir: Fhir, catalog: dict, as_of: datetime, apply: bool) -> None:
    patients = fhir.search("Patient?_count=1000")
    practitioner_ids = sorted(p["id"] for p in fhir.search("Practitioner?_count=1000&_elements=id"))
    if not practitioner_ids:
        raise SystemExit("no Practitioners found for CareTeam members")
    enrollees = select_enrollees(patients, as_of.date())
    serials: set[str] = set()
    plans = []
    for patient, age in enrollees:
        plan = plan_subject(catalog, patient, practitioner_ids, as_of, serials)
        plan["age"] = age
        plans.append(plan)

    print(f"as-of {fhir_time(as_of)}; {len(patients)} patients read; {len(practitioner_ids)} practitioners")
    print(f"{'patient_id':38} {'age':>3} {'care_setting':15} {'stress':>6}  condition / device")
    for plan in plans:
        print(f"{plan['patient_id']:38} {plan['age']:>3} {plan['care_setting']:15} {plan['stress']:>6}  "
              f"{plan['condition']} / {plan['device'] or '-'}")
    resources = [r for plan in plans for r in plan["resources"]]
    settings = {s: sum(p["care_setting"] == s for p in plans) for s in ENCOUNTER_CLASS}
    print("care settings:", json.dumps(settings))
    print("planned resources:", json.dumps(type_counts(resources)), "total", len(resources))
    retractions = plan_retractions(fhir)
    print(f"planned retractions (status -> {RETRACTED_STATUS}):", json.dumps(retired_code_counts(retractions)),
          "total", len(retractions))
    if not apply:
        print_samples(resources)
        print("dry run: nothing written (pass --apply to write)")
        return
    fhir.batch(put_entries(resources))
    fhir.batch(put_entries(retractions))
    print("tagged counts now:", json.dumps(tagged_counts(fhir)))


def run_remove(fhir: Fhir, apply: bool) -> None:
    targets = []
    for resource_type in RESOURCE_TYPES:
        found = [r for r in fhir.search(f"{resource_type}?_tag={TAG_PARAM}&_count=1000")
                 if r.get("resourceType") == resource_type]
        pending = [r for r in map(retracted, found) if r is not None]
        print(f"{resource_type}: {len(found)} tagged, {len(found) - len(pending)} already retracted, "
              f"{len(pending)} to retract")
        targets += pending
    entries = put_entries(targets)
    methods = sorted({e["request"]["method"] for e in entries})
    print(f"planned retractions: {len(entries)} requests, methods {methods}")
    if not apply:
        print_samples(targets)
        print(f"dry run: would PUT {len(entries)} retractions (status -> {RETRACTED_STATUS}); "
              "nothing is deleted (pass --apply to write)")
        return
    fhir.batch(entries)
    print("tagged counts now:", json.dumps(tagged_counts(fhir)))


def run_refresh(fhir: Fhir, catalog: dict, as_of: datetime, apply: bool) -> None:
    encounters: dict[str, list[str]] = {}
    for encounter in fhir.search(f"Encounter?_tag={TAG_PARAM}&status=in-progress&_count=1000"):
        encounters.setdefault(reference_id(encounter.get("subject")), []).append(encounter["id"])
    devices: dict[str, list[str]] = {}
    for statement in fhir.search(f"DeviceUseStatement?_tag={TAG_PARAM}&status=active&_count=1000"):
        devices.setdefault(reference_id(statement.get("subject")), []).append(reference_id(statement.get("device")))
    by_code = {(m["code"]["system"], m["code"]["code"]): key for key, m in catalog.items()}
    latest: dict[tuple[str, str], tuple[datetime, float]] = {}
    for obs in fhir.search(f"Observation?_tag={TAG_PARAM}&_count=1000"):
        coding = next((c for c in obs.get("code", {}).get("coding", [])
                       if (c.get("system"), c.get("code")) in by_code), None)
        value = obs.get("valueQuantity", {}).get("value")
        if not coding or value is None or "effectiveDateTime" not in obs:
            continue
        key = (reference_id(obs.get("subject")), by_code[(coding["system"], coding["code"])])
        when = parse_time(obs["effectiveDateTime"])
        if key not in latest or when > latest[key][0]:
            latest[key] = (when, float(value))

    resources, skipped = [], []
    slot = fhir_time(as_of)
    for pid in sorted(encounters):
        if len(encounters[pid]) != 1 or len(devices.get(pid, [])) > 1:
            skipped.append(f"{pid}: {len(encounters[pid])} in-progress encounters, "
                           f"{len(devices.get(pid, []))} active devices")
            continue
        device_id = devices.get(pid, [None])[0]
        for measure in BASELINE:
            if measure == "deviceFlow" and not device_id:
                continue
            previous = latest.get((pid, measure))
            if previous is None:
                skipped.append(f"{pid} {measure}: no tagged value to continue from")
                continue
            when, value = previous
            if when >= as_of or (measure == "lactate" and as_of - when < SERIES["lactate"][0]):
                continue
            drift = seeded(pid, measure, slot, "drift").uniform(-DRIFT[measure], DRIFT[measure])
            resources.append(observation(catalog, pid, encounters[pid][0], measure, as_of,
                                         shaped(measure, value + drift),
                                         device_id if measure == "deviceFlow" else None))
    print(f"as-of {slot}; {len(encounters)} enrolled subjects")
    for line in skipped:
        print("  skipped", line)
    counts = {}
    for resource in resources:
        measure = by_code[(resource["code"]["coding"][0]["system"], resource["code"]["coding"][0]["code"])]
        counts[measure] = counts.get(measure, 0) + 1
    print("planned observations:", json.dumps(counts), "total", len(resources))
    if not apply:
        print_samples(resources)
        print("dry run: nothing written (pass --apply to write)")
        return
    fhir.batch(put_entries(resources))
    print("tagged counts now:", json.dumps(tagged_counts(fhir)))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write to FHIR (default is a dry run)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--remove", action="store_true",
                      help="retract every tagged cardiology resource (PUT entered-in-error; never DELETE)")
    mode.add_argument("--refresh", action="store_true", help="write one new observation slot at --as-of")
    parser.add_argument("--as-of", help="series end time (ISO 8601, UTC if no offset); default now")
    parser.add_argument("--fhir-url", default=FHIR_URL, help="FHIR base URL (also the token audience)")
    args = parser.parse_args(argv)

    as_of = truncate_minute(parse_time(args.as_of) if args.as_of else datetime.now(timezone.utc))
    fhir = Fhir(args.fhir_url)
    if args.remove:
        run_remove(fhir, args.apply)
    elif args.refresh:
        run_refresh(fhir, load_catalog(), as_of, args.apply)
    else:
        run_seed(fhir, load_catalog(), as_of, args.apply)


if __name__ == "__main__":
    main(sys.argv[1:])
