"""Aggregator logic without network: windows, ids, Observation shape, catalog codes."""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import aggregator  # noqa: E402

CATALOG = HERE.parents[1] / "cardiology-api" / "measure-catalog.json"


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def test_lookback_selects_only_completed_aligned_windows():
    # Mid-window: the running 22:05 window is excluded.
    assert aggregator.completed_windows(utc(2026, 9, 28, 22, 7, 13), 5, 3) == [
        utc(2026, 9, 28, 21, 50), utc(2026, 9, 28, 21, 55), utc(2026, 9, 28, 22, 0)]
    # On a boundary the window that just started is not complete either.
    assert aggregator.completed_windows(utc(2026, 9, 28, 22, 5, 0), 5, 3) == [
        utc(2026, 9, 28, 21, 50), utc(2026, 9, 28, 21, 55), utc(2026, 9, 28, 22, 0)]
    # Just after midnight, with another window size and lookback.
    assert aggregator.completed_windows(utc(2026, 9, 29, 0, 14, 59), 15, 2) == [
        utc(2026, 9, 28, 23, 30), utc(2026, 9, 28, 23, 45)]


def test_observation_id_is_deterministic_and_keyed_by_device_measure_window():
    start = utc(2026, 9, 28, 21, 55)
    oid = aggregator.observation_id("MASIMO-RADIUS7-0001", "spo2", start)
    expected = hashlib.sha256(b"MASIMO-RADIUS7-0001|spo2|2026-09-28T21:55:00Z").hexdigest()[:40]
    assert oid == "masimo-agg-" + expected
    assert oid == aggregator.observation_id("MASIMO-RADIUS7-0001", "spo2", start.astimezone())
    others = {
        aggregator.observation_id("MASIMO-RADIUS7-0002", "spo2", start),
        aggregator.observation_id("MASIMO-RADIUS7-0001", "pulseRate", start),
        aggregator.observation_id("MASIMO-RADIUS7-0001", "spo2", utc(2026, 9, 28, 22, 0)),
    }
    assert oid not in others and len(others) == 3


def test_summary_row_becomes_observations_only_for_associated_devices():
    associations = {"MASIMO-RADIUS7-0001": "Patient/p1"}
    issued = utc(2026, 9, 28, 22, 0, 30)
    rows = [
        {"device_id": "MASIMO-RADIUS7-0001", "window_start": "2026-09-28T21:55:00Z",
         "pr_mean": None, "pr_min": None, "pr_max": None, "pr_n": 0,
         "spo2_mean": 97.25, "spo2_min": 95.4, "spo2_max": 99.0, "spo2_n": 298},
        {"device_id": "MASIMO-RADIUS7-0099", "window_start": "2026-09-28T21:55:00Z",
         "pr_mean": 80.0, "pr_min": 70.0, "pr_max": 90.0, "pr_n": 300,
         "spo2_mean": 96.0, "spo2_min": 94.0, "spo2_max": 98.0, "spo2_n": 300},
    ]
    observations, unassociated = aggregator.build_observations(rows, associations, 5, issued)
    assert unassociated == ["MASIMO-RADIUS7-0099"]
    assert observations == [{
        "resourceType": "Observation",
        "id": aggregator.observation_id("MASIMO-RADIUS7-0001", "spo2", utc(2026, 9, 28, 21, 55)),
        "meta": {"tag": [{"system": "https://brakekat.com/hls/tags", "code": "masimo-telemetry-aggregate",
                          "display": "Masimo telemetry 5-minute aggregate"}],
                 "source": "https://brakekat.com/hls/masimo-fhir-aggregator"},
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                  "code": "vital-signs", "display": "Vital Signs"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "59408-5",
                             "display": "Oxygen saturation in Arterial blood by Pulse oximetry"}]},
        "subject": {"reference": "Patient/p1"},
        "device": {"reference": "Device/MASIMO-RADIUS7-0001"},
        "effectivePeriod": {"start": "2026-09-28T21:55:00Z", "end": "2026-09-28T22:00:00Z"},
        "issued": "2026-09-28T22:00:30Z",
        # Half-up, not Python's half-to-even round().
        "valueQuantity": {"value": 97.3, "unit": "%", "system": "http://unitsofmeasure.org", "code": "%"},
        "note": [{"text": "5-minute mean of 298 readings (min 95.4, max 99) from Masimo telemetry"}],
    }]


def test_associations_never_guess_a_subject():
    def assoc(device, subject, status="active", url="http://example.org/StructureDefinition/associated-device"):
        return {"resourceType": "Basic", "subject": {"reference": subject}, "extension": [
            {"url": url, "valueReference": {"reference": f"Device/{device}"}},
            {"url": "http://example.org/StructureDefinition/association-status", "valueCode": status}]}

    unique, ambiguous = aggregator.parse_associations([
        assoc("D1", "Patient/a"),
        assoc("D2", "Patient/b", url="http://hl7.org/fhir/StructureDefinition/device-association-device"),
        assoc("D3", "Patient/c", status="inactive"),
        assoc("D4", "Patient/d"), assoc("D4", "Patient/e"),
        assoc("D5", "Group/g"),
    ])
    assert unique == {"D1": "Patient/a", "D2": "Patient/b"}
    assert ambiguous == ["D4"]


def test_codes_and_units_equal_catalog_masimo_entries():
    catalog = json.loads(CATALOG.read_text())
    assert catalog["version"].startswith("2.")
    masimo = {m["telemetryField"]: m for m in catalog["measures"] if m.get("source") == "masimo-telemetry"}
    assert set(masimo) == set(aggregator.MEASURES)
    for field, spec in aggregator.MEASURES.items():
        entry = masimo[field]
        assert spec == {"key": entry["key"], "system": entry["code"]["system"], "code": entry["code"]["code"],
                        "display": entry["code"]["display"], "unit": entry["unit"]}
