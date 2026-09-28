# Masimo FHIR aggregator

Writes the simulated Masimo Radius-7 pulse-oximeter stream into FHIR as 5-minute
aggregate Observations. Each FHIR `$export` then carries the telemetry into HDS
(bronze → silver → gold) with FHIR provenance, next to every other clinical
observation. SYNTHETIC DATA ONLY.

## What a run does

1. Reads every FHIR `Basic` resource with code `device-assoc` and maps each Masimo
   device (`Device/<device_id>`) to its `Patient`. Inactive links are ignored; a
   device linked to more than one patient, or to none, gets no Observation.
2. Runs one parameterized KQL query against the Eventhouse table `TelemetryRaw`:
   mean, min, max and count of `pr` and `spo2` per device per UTC-aligned
   `[start, start + 5m)` window, by reading time, over the 3 most recent
   completed windows. The overlap absorbs ingestion lag and one missed run.
3. PUTs one Observation per device, measure and window in FHIR batch bundles of
   at most 200 entries. Any failed entry fails the run.

| Telemetry | Code (LOINC) | Unit | Catalog key |
| --- | --- | --- | --- |
| `pr` | 8889-8 Heart rate by Pulse oximetry | `/min` | `pulseRate` |
| `spo2` | 59408-5 Oxygen saturation in Arterial blood by Pulse oximetry | `%` | `spo2` |

These equal the `masimo-telemetry` entries of `cardiology-api/measure-catalog.json`
(the test enforces it). Each Observation carries the tag
`https://brakekat.com/hls/tags|masimo-telemetry-aggregate`, `meta.source`
`https://brakekat.com/hls/masimo-fhir-aggregator`, `effectivePeriod` = the
window, `valueQuantity` = the mean rounded half-up to one decimal, and a note
with the reading count, min and max. The id is `masimo-agg-` + the first 40 hex
of sha256(`<device_id>|<catalog key>|<window start ISO Z>`), so rewriting a
window replaces it instead of duplicating it.

Each run prints one JSON summary line (`"event": "masimo-fhir-aggregator.run"`)
and exits non-zero on any failure.

## Schedule and hosting

Azure Container Apps Job `masimo-fhir-aggregator` in environment `hds-dicom-env`
(`rg-med-0906`), cron `*/5 * * * *`, parallelism 1, replica timeout 240 s, one
retry. Template: `bicep/masimo-fhir-aggregator.bicep`. Deployment (idempotent):

```powershell
./phase-2/deploy-masimo-fhir-aggregator.ps1
```

## Identity and access

User-assigned identity `id-masimo-fhir-aggregator`, granted by the deploy script:

- AcrPull on the registry (the job pulls its image as this identity)
- FHIR Data Contributor on the FHIR service
- Kusto database viewer on `MasimoEventhouse` only (not workspace Viewer)

## Environment

| Variable | Meaning |
| --- | --- |
| `FHIR_URL` | FHIR service base URL (also the token audience) |
| `KUSTO_QUERY_URI` | Eventhouse query URI (also the token audience) |
| `KUSTO_DATABASE` | KQL database holding `TelemetryRaw` |
| `AZURE_CLIENT_ID` | Managed identity client id. Unset: tokens come from the az CLI |
| `WINDOW_MINUTES` | Window size, default `5` (must divide a day) |
| `LOOKBACK_WINDOWS` | Completed windows per run, default `3` |

## Run once locally

Standard library only; signed in with `az login` on the deployment subscription.

```bash
export FHIR_URL=https://<workspace>-<fhir>.fhir.azurehealthcareapis.com
export KUSTO_QUERY_URI=https://<eventhouse>.kusto.fabric.microsoft.com
export KUSTO_DATABASE=MasimoEventhouse
python3 masimo-fhir-aggregator/aggregator.py --dry-run   # summary + one sample Observation, writes nothing
python3 masimo-fhir-aggregator/aggregator.py --once      # writes (same as no flag)
python3 -m pytest masimo-fhir-aggregator/tests/test_aggregator.py
```
