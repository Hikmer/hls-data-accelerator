# Microsoft HDS v1.4.0 Source Deployment Guide

HDS is deployed automatically from Microsoft source. Do not create a Healthcare Data Solution item in the Fabric portal and do not add a placeholder item to satisfy downstream checks.

## Source and generated content

- Vendored source and empty schema assets: `vendor/microsoft-hds/1.4.0/HDS.SourceCode` and `DTT.SourceCode`
- Generated stage: `.hds-build/1.4.0`
- Deployment entry point: `hds-source/Deploy-HdsSource.ps1`
- Shared implementation: `orchestrator/activities/deploy_hds_source.py`

The stage contains Microsoft build artifacts, one HDS wheel, one DTT wheel, patched deployment notebooks, and `environment.yml` with `scipy==1.11.4`.

This distribution deliberately excludes Microsoft `SampleData`/`ReferenceData` datasets, nested Patient Outreach Analytics sample tables, sample FHIR/DICOM operation payloads, and saved notebook outputs. Populated claims files distributed as table schemas have been converted to zero-row Parquet with their schemas preserved; their Delta logs contain no sample statistics. Generator code, configurations, deployment artifacts, and schema-only Parquet remain available. Tools and integration scenarios that reference sample files require separately supplied data at their documented local paths. Those paths are ignored by Git and must not be force-added.

The three DTT `configuration_compiler/config_files_models/env` modules are tracked source, including on case-insensitive filesystems. Python virtual-environment ignore rules do not exclude that runtime package.

Staging validates every discovered runtime Python module and wheel `RECORD` entry. It rejects incomplete wheels and rebuilds an invalid cache. The source checksum includes vendored file contents; a source change or explicit force rebuild cannot reuse an old same-named wheel. Wheels retain distribution versions `hds==1.4.0` and `dtt==0.3.1.1271`, with deterministic content build tags in their filenames.

The CMA and POA report definitions and themes are also tracked. Their `Reports/` directories are excluded from the generic coverage-output ignore rule, including when Git folds case. Missing report artifacts still fail Microsoft's original artifact manifest validation.

## Local validation

```powershell
pwsh -NoProfile -File ./setup-prereqs.ps1
pwsh -NoProfile -File ./hds-source/Deploy-HdsSource.ps1 `
  -FabricWorkspaceName local-validation `
  -ValidateOnly
```

`-ValidateOnly` makes no cloud calls. It verifies:

- the Microsoft artifact validator manifest;
- exact HDS and DTT versions, one wheel per package, required runtime modules, `RECORD` integrity, and content build tags;
- nine deployment notebooks and three validation notebooks;
- managed lakehouse, config-notebook, and environment names;
- `%run healthcare1_msft_config_notebook` references;
- absence of legacy config-notebook references and double-prefixed environment names.

## Automated deployment flow

`Deploy-All.ps1` validates the payload before its first cloud mutation. After workspace and RTI foundation creation, it:

1. Creates or reuses `deployment_lakehouse`.
2. Streams `hds-build-artifacts` to OneLake in 4 MiB chunks.
3. Creates `deployment_notebooks` and `validation_notebooks` folders.
4. Creates or updates all Microsoft bootstrap notebook definitions.
5. Attaches `deployment_lakehouse` to `master_deployer`.
6. Starts `RunNotebook` and follows its returned job location.
7. Waits for environment publishing and the master job to complete.
8. Validates the live artifact contract.
9. Runs RTI enrichment and the existing ordered HDS pipelines.

## Live contract check

```powershell
pwsh -NoProfile -File ./hds-source/Deploy-HdsSource.ps1 `
  -FabricWorkspaceName "hls-demo" `
  -ContractOnly
```

The contract requires the published `healthcare1_environment`, expected lakehouses, all source notebooks and pipelines, semantic models and reports, required Clinical/POA/Imaging/OMOP pipelines, and a completed `master_deployer` job.

## Expected core artifacts

| Type | Name |
|---|---|
| Environment | `healthcare1_environment` |
| Lakehouse | `healthcare1_msft_admin` |
| Lakehouse | `healthcare1_msft_bronze` |
| Lakehouse | `healthcare1_msft_silver` |
| Lakehouse | `healthcare1_msft_gold_omop` |
| Lakehouse | `healthcare1_msft_gold_cma` |
| Notebook | `healthcare1_msft_config_notebook` |
| Pipeline | `healthcare1_msft_clinical_data_foundation_ingestion` |
| Pipeline | `healthcare1_msft_imaging_with_clinical_foundation_ingestion` |
| Pipeline | `healthcare1_msft_omop_analytics` |

## Retry and conflict rules

- Exact same-type/name items are reused or updated.
- Existing lakehouse tables and folders are reused.
- A same-name item of another type fails with the conflicting item ID.
- Names are never suffixed because that breaks downstream references.
- 429, 5xx, and Fabric inbound-policy denials are retried.
- Other 4xx responses fail immediately.
- Environment publish fails after 75 minutes; the master job fails after 90 minutes.

## Troubleshooting

### Missing `orchestrator/.venv`

Run:

```powershell
pwsh -NoProfile -File ./setup-prereqs.ps1
```

The deployment wrapper never installs Python packages during a cloud run.

### Payload validation failure

Rerun `-ValidateOnly`; an invalid cached wheel or changed source triggers a rebuild. A missing DTT `env` package is a source-integrity error, not a Spark installation problem. Update to the repaired checkout rather than regenerating an incomplete wheel. Do not copy an entire Microsoft download over `vendor/`, because that can restore excluded datasets. Deployment-specific source patches belong in `.hds-build/1.4.0`.

### Environment publish failure

Open `healthcare1_msft_environment` in Fabric and inspect publish details. Resolve the reported library conflict, then rerun the same deployment. Reconciliation compares the published content-tagged HDS/DTT library names and published dependency YAML, not just version filenames or the last successful publish status. It uploads repaired wheels, removes obsolete managed HDS/DTT staging libraries while preserving unrelated libraries, publishes, and verifies the published payload. After updating an existing customer workspace, start a fresh Spark session before rerunning the failed POA pipeline. Local `-ValidateOnly` does not publish or repair an already deployed environment.

### Missing pipeline placeholders

The host validator reports every missing display name. Confirm source notebooks deployed under their `healthcare1_msft_*` names and rerun the source phase. The deployment never treats a skipped pipeline as success.

### Fabric inbound communication policy

`RequestDeniedByInboundPolicy` is transient in this workflow and receives bounded retries. If it persists, correct the tenant/workspace inbound policy; do not bypass contract validation.

### Downstream row gates fail

First run `-ContractOnly`. If the contract passes, inspect Clinical, POA, Imaging, and OMOP job histories and the Bronze/Silver row counts emitted by `phase-2/storage-access-trusted-workspace.ps1`.

## Teardown

The normal teardown removes all source-created items and then deletes `deployment_notebooks` and `validation_notebooks`. It does not delete or modify the local vendored Microsoft source.
