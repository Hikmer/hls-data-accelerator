# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "environment": {
# META       "environmentId": "",
# META       "workspaceId": ""
# META     }
# META   }
# META }

# MARKDOWN ********************

# # Cardiology gold projection
# - Projects the synthetic Caldova cardiology cohort from HDS Silver into three Reporting Gold tables read by the cardiology app: `cardiology_subject`, `cardiology_observation`, `cardiology_enrollable_patient`.
# - Each run fully overwrites all three tables and stamps `refreshed_at` with the run time (UTC). SYNTHETIC DATA ONLY.
# - Cohort membership is a Silver Encounter carrying the `synthetic-caldova-cardiology` tag with status `in-progress`.
# - Deploy and run with `cardiology-api/fabric/deploy_gold_projection.py`, which fills the IDs below and refuses to deploy if `MEASURE_CATALOG` differs from `cardiology-api/measure-catalog.json`.

# PARAMETERS CELL ********************

WORKSPACE_ID = ""
SILVER_LAKEHOUSE_ID = ""
GOLD_LAKEHOUSE_ID = ""

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from datetime import datetime, timezone

from pyspark.sql import Window
from pyspark.sql import functions as F

if not (WORKSPACE_ID and SILVER_LAKEHOUSE_ID and GOLD_LAKEHOUSE_ID):
    raise RuntimeError("WORKSPACE_ID, SILVER_LAKEHOUSE_ID and GOLD_LAKEHOUSE_ID must be set (deploy_gold_projection.py fills them).")

spark.conf.set("spark.sql.session.timeZone", "UTC")

TAG_SYSTEM = "https://brakekat.com/hls/tags"
TAG_CODE = "synthetic-caldova-cardiology"
ACT_CODE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
CARE_SETTINGS = {"IMP": "inpatient", "AMB": "outpatient", "HH": "home-monitored"}
OBSERVATION_STATUSES = ["final", "amended", "corrected"]
# Masimo device-assoc extension url suffixes, verbatim from masimo-fhir-aggregator/aggregator.py.
DEVICE_EXTENSION_SUFFIXES = ("associated-device", "device-association-device")
STATUS_EXTENSION_SUFFIXES = ("association-status", "device-association-status")

# Must equal cardiology-api/measure-catalog.json (key, code system, code, unit, in catalog order).
# deploy_gold_projection.py refuses to deploy when it does not.
MEASURE_CATALOG = [
    {"key": "lactate", "system": "http://loinc.org", "code": "2524-7", "unit": "mmol/L"},
    {"key": "map", "system": "http://loinc.org", "code": "8478-0", "unit": "mm[Hg]"},
    {"key": "pulseRate", "system": "http://loinc.org", "code": "8889-8", "unit": "/min"},
    {"key": "spo2", "system": "http://loinc.org", "code": "59408-5", "unit": "%"},
    {"key": "deviceFlow", "system": "https://brakekat.com/hls/cardiology/measures", "code": "device-flow-index", "unit": "%"},
]

# Silver stores complex FHIR elements as JSON strings; references carry the target FHIR id at
# $.identifier.value (HDS also keeps msftSourceReference "Type/<id>" and idOrig).
REFERENCE_SCHEMA = "struct<type:string,identifier:struct<value:string>,idOrig:string,msftSourceReference:string>"
META_TAG_SCHEMA = "struct<tag:array<struct<system:string,code:string>>>"
CODEABLE_SCHEMA = "struct<coding:array<struct<system:string,code:string,display:string>>>"
CODING_SCHEMA = "struct<system:string,code:string>"
PERIOD_SCHEMA = "struct<start:string,end:string>"
QUANTITY_SCHEMA = "struct<value:double,unit:string,code:string>"
NAME_SCHEMA = "array<struct<family:string,given:array<string>>>"
PARTICIPANT_SCHEMA = "array<struct<role:array<struct<coding:array<struct<display:string>>>>>>"
# Basic.extension is kept as the raw FHIR JSON array.
EXTENSION_SCHEMA = "array<struct<url:string,valueCode:string,valueReference:struct<reference:string>>>"

# Gold column order and types are the app contract; conform() is the only way rows reach a table.
GOLD_SCHEMAS = {
    "cardiology_subject": [
        ("subject_id", "string"), ("initials", "string"), ("age_band", "string"), ("care_setting", "string"),
        ("encounter_id", "string"), ("encounter_start", "timestamp"), ("primary_condition_system", "string"),
        ("primary_condition_code", "string"), ("primary_condition_display", "string"), ("attending_role", "string"),
        ("device_id", "string"), ("device_kind", "string"), ("device_serial", "string"),
        ("device_associated_since", "timestamp"), ("pulse_oximeter_device_id", "string"), ("refreshed_at", "timestamp"),
    ],
    "cardiology_observation": [
        ("observation_id", "string"), ("subject_id", "string"), ("measure", "string"), ("code_system", "string"),
        ("code", "string"), ("value", "double"), ("unit", "string"), ("effective_at", "timestamp"),
        ("device_id", "string"), ("fhir_last_updated", "timestamp"), ("silver_modified_at", "timestamp"),
        ("refreshed_at", "timestamp"),
    ],
    "cardiology_enrollable_patient": [
        ("patient_id", "string"), ("initials", "string"), ("age_band", "string"), ("pulse_oximeter_device_id", "string"),
        ("refreshed_at", "timestamp"),
    ],
}
GOLD_KEYS = {"cardiology_subject": "subject_id", "cardiology_observation": "observation_id", "cardiology_enrollable_patient": "patient_id"}

run_ts = datetime.now(timezone.utc)
refreshed_at = F.lit(run_ts).cast("timestamp")
print(f"Cardiology gold projection run at {run_ts.isoformat()}")


def onelake_table(lakehouse_id, name):
    return f"abfss://{WORKSPACE_ID}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}/Tables/{name}"


def read_silver(name):
    return spark.read.format("delta").load(onelake_table(SILVER_LAKEHOUSE_ID, name))


def write_gold(df, name):
    df.write.mode("overwrite").format("delta").option("overwriteSchema", "true").save(onelake_table(GOLD_LAKEHOUSE_ID, name))
    print(f"{name}: wrote {spark.read.format('delta').load(onelake_table(GOLD_LAKEHOUSE_ID, name)).count()} rows")


def conform(df, name):
    df = df.withColumn("refreshed_at", refreshed_at)
    return df.select(*[F.col(column).cast(data_type).alias(column) for column, data_type in GOLD_SCHEMAS[name]])


def non_empty(expr):
    return F.when(F.length(F.trim(expr)) > 0, expr)


def ref_id(column):
    ref = F.from_json(F.col(column), REFERENCE_SCHEMA)
    return F.coalesce(
        non_empty(ref["identifier"]["value"]),
        non_empty(F.regexp_replace(ref["msftSourceReference"], "^[A-Za-z]+/", "")),
        non_empty(ref["idOrig"]),
    )


def ref_type(column):
    ref = F.from_json(F.col(column), REFERENCE_SCHEMA)
    return F.coalesce(non_empty(ref["type"]), non_empty(F.regexp_extract(ref["msftSourceReference"], "^([A-Za-z]+)/", 1)))


def url_ends_with(url, suffixes):
    matched = F.lit(False)
    for suffix in suffixes:
        matched = matched | url.endswith(suffix)
    return matched


def first_coding(column):
    return F.from_json(F.col(column), CODEABLE_SCHEMA)["coding"].getItem(0)


def tagged(df):
    tags = F.from_json(F.col("meta_string"), META_TAG_SCHEMA)["tag"]
    return df.filter(F.coalesce(F.exists(tags, lambda t: (t["system"] == TAG_SYSTEM) & (t["code"] == TAG_CODE)), F.lit(False)))


def latest_per(df, key, *order):
    # Deterministic pick of one row per key: newest first, FHIR id as the tie-breaker.
    window = Window.partitionBy(key).orderBy(*[F.col(c).desc_nulls_last() for c in order], F.col("idOrig").asc())
    return df.withColumn("_rank", F.row_number().over(window)).filter(F.col("_rank") == 1).drop("_rank")


def report_exclusions(label, df):
    rows = df.filter(F.col("exclusion").isNotNull()).groupBy("exclusion").count().orderBy("exclusion").collect()
    total = sum(r["count"] for r in rows)
    print(f"{label}: excluded {total}" + "".join(f"\n  {r['exclusion']}: {r['count']}" for r in rows))
    return df.filter(F.col("exclusion").isNull()).drop("exclusion")


# ── Patients: initials, age band, alive ─────────────────────────────────────────────
patient_src = latest_per(read_silver("Patient").filter(F.col("idOrig").isNotNull()), "idOrig", "meta_lastUpdated", "msftModifiedDatetime")
name0 = F.from_json(F.col("name_string"), NAME_SCHEMA).getItem(0)
given0 = F.trim(name0["given"].getItem(0))
family = F.trim(name0["family"])
birth = F.to_date(F.col("birthDate"))
run_date = F.lit(run_ts.date())
age_years = (
    F.year(run_date) - F.year(birth)
    - F.when((F.month(run_date) < F.month(birth)) | ((F.month(run_date) == F.month(birth)) & (F.dayofmonth(run_date) < F.dayofmonth(birth))), 1).otherwise(0)
)
decade = (F.floor(age_years / 10) * 10).cast("int")
patients = patient_src.select(
    F.col("idOrig").alias("patient_id"),
    F.when(
        (F.length(given0) > 0) & (F.length(family) > 0),
        F.concat(F.upper(F.substring(given0, 1, 1)), F.lit("."), F.upper(F.substring(family, 1, 1)), F.lit(".")),
    ).alias("initials"),
    F.when(age_years >= 0, F.concat(decade.cast("string"), F.lit("-"), (decade + 9).cast("string"))).alias("age_band"),
    (~F.coalesce(F.col("deceasedBoolean"), F.lit(False)) & F.col("deceasedDateTime").isNull()).alias("alive"),
)

# ── Enrollment: tagged in-progress Encounters ───────────────────────────────────────
encounter_class = F.from_json(F.col("class_string"), CODING_SCHEMA)
care_setting_map = F.create_map(*[F.lit(v) for kv in CARE_SETTINGS.items() for v in kv])
encounters = tagged(read_silver("Encounter")).filter(F.col("status") == "in-progress").select(
    F.col("idOrig").alias("encounter_id"),
    ref_id("subject_string").alias("subject_id"),
    F.when(encounter_class["system"] == ACT_CODE_SYSTEM, F.element_at(care_setting_map, encounter_class["code"])).alias("care_setting"),
    F.from_json(F.col("period_string"), PERIOD_SCHEMA)["start"].cast("timestamp").alias("encounter_start"),
).filter(F.col("subject_id").isNotNull())
enrolled_ids = encounters.select("subject_id").distinct()
encounter_counts = encounters.groupBy("subject_id").agg(F.count("*").alias("encounter_count"))

# ── Devices: active tagged Device linked by active tagged DeviceUseStatement or Device.patient ──
device_type = first_coding("type_string")
devices = tagged(read_silver("Device")).filter(F.col("status") == "active").select(
    F.col("idOrig").alias("device_id"),
    ref_id("patient_string").alias("device_patient_id"),
    device_type["display"].alias("device_kind"),
    F.col("serialNumber").alias("device_serial"),
)
device_uses = tagged(read_silver("DeviceUseStatement")).filter(F.col("status") == "active").select(
    ref_id("subject_string").alias("subject_id"),
    ref_id("device_string").alias("device_id"),
    F.from_json(F.col("timingPeriod_string"), PERIOD_SCHEMA)["start"].cast("timestamp").alias("associated_since"),
)
device_links = (
    device_uses.join(devices.select("device_id"), "device_id")
    .unionByName(devices.select(F.col("device_patient_id").alias("subject_id"), "device_id", F.lit(None).cast("timestamp").alias("associated_since")))
    .filter(F.col("subject_id").isNotNull())
)
device_per_subject = device_links.groupBy("subject_id").agg(
    F.countDistinct("device_id").alias("device_count"),
    F.first("device_id", ignorenulls=True).alias("device_id"),
)
subject_device = (
    device_per_subject.filter(F.col("device_count") == 1)
    .join(device_links.groupBy("subject_id", "device_id").agg(F.max("associated_since").alias("device_associated_since")), ["subject_id", "device_id"])
    .join(devices.drop("device_patient_id"), "device_id")
    .select("subject_id", "device_id", "device_kind", "device_serial", "device_associated_since")
)

# ── Masimo pulse oximeter: active FHIR Basic device-assoc links ─────────────────────
# The same rule as masimo-fhir-aggregator/aggregator.py parse_associations, so gold and the
# aggregator agree on every device's patient: the latest version of each Basic with
# code.coding[0].code = device-assoc; status and devices are found by extension url suffix
# (an association whose status is present and not "active" is ignored); the subject must be
# a Patient; a device linked to more than one patient is ambiguous and dropped for all of
# them. The Device id equals the Eventhouse TelemetryRaw device_id. Not cardiology-tagged.
association_extensions = F.from_json(F.col("extension"), EXTENSION_SCHEMA)
association_status = F.filter(association_extensions, lambda e: url_ends_with(e["url"], STATUS_EXTENSION_SUFFIXES)).getItem(0)["valueCode"]
associated_devices = F.transform(
    F.filter(association_extensions, lambda e: url_ends_with(e["url"], DEVICE_EXTENSION_SUFFIXES)),
    lambda e: e["valueReference"]["reference"],
)
device_assoc_links = (
    latest_per(read_silver("Basic").filter(F.col("idOrig").isNotNull()), "idOrig", "meta_lastUpdated", "msftModifiedDatetime")
    .filter(first_coding("code_string")["code"] == "device-assoc")
    .filter(association_status.isNull() | (association_status == "active"))
    .filter(ref_type("subject_string") == "Patient")
    .select(ref_id("subject_string").alias("patient_id"), F.explode(associated_devices).alias("device_reference"))
    .filter(F.col("patient_id").isNotNull() & F.col("device_reference").startswith("Device/") & (F.length("device_reference") > len("Device/")))
    .select("patient_id", F.regexp_replace("device_reference", "^Device/", "").alias("pulse_oximeter_device_id"))
    .distinct()
)
ambiguous_devices = (
    device_assoc_links.groupBy("pulse_oximeter_device_id").agg(F.countDistinct("patient_id").alias("patient_count"))
    .filter(F.col("patient_count") > 1).select("pulse_oximeter_device_id")
)
unambiguous_links = device_assoc_links.join(ambiguous_devices, "pulse_oximeter_device_id", "left_anti")
patient_pulse_oximeter = unambiguous_links.groupBy("patient_id").agg(
    F.countDistinct("pulse_oximeter_device_id").alias("pulse_oximeter_count"),
    F.min("pulse_oximeter_device_id").alias("pulse_oximeter_device_id"),
)
print(
    f"Masimo device-assoc: {device_assoc_links.count()} active Patient links; "
    f"{ambiguous_devices.count()} ambiguous devices (linked to more than one patient) dropped for "
    f"{device_assoc_links.join(ambiguous_devices, 'pulse_oximeter_device_id', 'left_semi').select('patient_id').distinct().count()} patients; "
    f"{patient_pulse_oximeter.filter(F.col('pulse_oximeter_count') > 1).count()} patients linked to more than one device"
)

# ── Primary condition and attending role, both scoped to the enrollment Encounter ────
condition_code = first_coding("code_string")
conditions = latest_per(
    tagged(read_silver("Condition"))
    .filter(first_coding("clinicalStatus_string")["code"] == "active")
    .select(
        "idOrig",
        "meta_lastUpdated",
        ref_id("subject_string").alias("subject_id"),
        ref_id("encounter_string").alias("encounter_id"),
        condition_code["system"].alias("primary_condition_system"),
        condition_code["code"].alias("primary_condition_code"),
        condition_code["display"].alias("primary_condition_display"),
    )
    .filter(F.col("primary_condition_code").isNotNull())
    .withColumn("subject_encounter", F.concat_ws("|", "subject_id", "encounter_id")),
    "subject_encounter",
    "meta_lastUpdated",
).drop("idOrig", "meta_lastUpdated", "subject_encounter")

participants = F.from_json(F.col("participant_string"), PARTICIPANT_SCHEMA)
care_teams = latest_per(
    tagged(read_silver("CareTeam"))
    .filter(F.col("status") == "active")
    .select(
        "idOrig",
        "meta_lastUpdated",
        ref_id("subject_string").alias("subject_id"),
        ref_id("encounter_string").alias("encounter_id"),
        participants.getItem(0)["role"].getItem(0)["coding"].getItem(0)["display"].alias("attending_role"),
    )
    .withColumn("subject_encounter", F.concat_ws("|", "subject_id", "encounter_id")),
    "subject_encounter",
    "meta_lastUpdated",
).drop("idOrig", "meta_lastUpdated", "subject_encounter")

# ── cardiology_subject with row gates ──────────────────────────────────────────────
# Gates are evaluated per subject: Encounter details join only for subjects with exactly one
# in-progress tagged Encounter, so every candidate subject is exactly one row.
single_encounters = encounter_counts.filter(F.col("encounter_count") == 1).join(encounters, "subject_id").drop("encounter_count")
subject_candidates = (
    encounter_counts.join(single_encounters, "subject_id", "left")
    .join(device_per_subject.select("subject_id", "device_count"), "subject_id", "left")
    .join(patients, F.col("subject_id") == F.col("patient_id"), "left")
    .join(conditions, ["subject_id", "encounter_id"], "left")
    .join(care_teams, ["subject_id", "encounter_id"], "left")
    .join(subject_device, "subject_id", "left")
    .join(patient_pulse_oximeter.withColumnRenamed("patient_id", "subject_id"), "subject_id", "left")
    .withColumn(
        "exclusion",
        F.when(F.col("encounter_count") > 1, "more than one in-progress tagged Encounter")
        .when(F.coalesce(F.col("device_count"), F.lit(0)) > 1, "more than one active tagged device")
        .when(F.coalesce(F.col("pulse_oximeter_count"), F.lit(0)) > 1, "more than one Masimo device (active, unambiguous device-assoc)")
        .when(F.col("patient_id").isNull(), "Patient not in Silver")
        .when(F.col("care_setting").isNull(), "Encounter class not IMP/AMB/HH (v3-ActCode)")
        .when(F.col("encounter_start").isNull(), "Encounter period.start missing")
        .when(F.col("initials").isNull() | F.col("age_band").isNull(), "Patient name or birthDate missing")
        .when(F.col("primary_condition_code").isNull(), "no active tagged Condition on the enrollment Encounter"),
    )
)
cardiology_subject = conform(report_exclusions("cardiology_subject", subject_candidates), "cardiology_subject")

# ── cardiology_observation: every catalog-coded Observation for projected subjects ──
# A row reaches gold only with a finite value, an effective time and the catalog unit
# (UCUM code, falling back to unit); rejected rows are counted, never kept.
measure_map = F.create_map(*[F.lit(v) for m in MEASURE_CATALOG for v in (f"{m['system']}|{m['code']}", m["key"])])
catalog_unit_map = F.create_map(*[F.lit(v) for m in MEASURE_CATALOG for v in (m["key"], m["unit"])])
measure_keys = [f"{m['system']}|{m['code']}" for m in MEASURE_CATALOG]
codings = F.from_json(F.col("code_string"), CODEABLE_SCHEMA)["coding"]
catalog_coding = F.filter(codings, lambda c: F.concat(c["system"], F.lit("|"), c["code"]).isin(measure_keys)).getItem(0)
quantity = F.from_json(F.col("valueQuantity_string"), QUANTITY_SCHEMA)
observation_candidates = (
    latest_per(read_silver("Observation").filter(F.col("idOrig").isNotNull()), "idOrig", "meta_lastUpdated", "msftModifiedDatetime")
    .filter(F.col("status").isin(OBSERVATION_STATUSES))
    .withColumn("_coding", catalog_coding)
    .filter(F.col("_coding").isNotNull())
    .withColumn("subject_id", ref_id("subject_string"))
    .join(cardiology_subject.select("subject_id"), "subject_id", "left_semi")
    .select(
        F.col("idOrig").alias("observation_id"),
        "subject_id",
        F.element_at(measure_map, F.concat(F.col("_coding.system"), F.lit("|"), F.col("_coding.code"))).alias("measure"),
        F.col("_coding.system").alias("code_system"),
        F.col("_coding.code").alias("code"),
        quantity["value"].alias("value"),
        F.coalesce(quantity["code"], quantity["unit"]).alias("unit"),
        # Point-in-time readings carry effectiveDateTime/Instant; the Masimo 5-minute aggregates carry
        # effectivePeriod and are placed at the end of their window.
        F.coalesce(F.col("effectiveDateTime"), F.col("effectiveInstant"),
                   F.from_json(F.col("effectivePeriod_string"), PERIOD_SCHEMA)["end"].cast("timestamp")).alias("effective_at"),
        ref_id("device_string").alias("device_id"),
        F.col("meta_lastUpdated").alias("fhir_last_updated"),
        F.col("msftModifiedDatetime").alias("silver_modified_at"),
    )
    .withColumn(
        "exclusion",
        F.when(F.col("value").isNull() | F.isnan("value") | (F.abs("value") == float("inf")), "valueQuantity.value missing or not finite")
        .when(F.col("effective_at").isNull(), "no effectiveDateTime, effectiveInstant or effectivePeriod.end")
        .when(F.col("unit").isNull() | (F.col("unit") != F.element_at(catalog_unit_map, F.col("measure"))), "unit differs from the catalog unit"),
    )
)
observations = conform(report_exclusions("cardiology_observation", observation_candidates), "cardiology_observation")

# ── cardiology_enrollable_patient: alive and not enrolled ────────────────────────────
enrollable_candidates = (
    patients.filter(F.col("alive"))
    .join(enrolled_ids.withColumnRenamed("subject_id", "patient_id"), "patient_id", "left_anti")
    .join(patient_pulse_oximeter, "patient_id", "left")
    # Only a patient's unique, unambiguous active Masimo device; otherwise NULL (not admissible).
    .withColumn("pulse_oximeter_device_id", F.when(F.col("pulse_oximeter_count") == 1, F.col("pulse_oximeter_device_id")))
    .withColumn("exclusion", F.when(F.col("initials").isNull() | F.col("age_band").isNull(), "Patient name or birthDate missing"))
)
cardiology_enrollable_patient = conform(report_exclusions("cardiology_enrollable_patient", enrollable_candidates), "cardiology_enrollable_patient")
print(f"enrolled subjects (tagged in-progress Encounter): {enrolled_ids.count()}; "
      f"deceased patients: {patients.filter(~F.col('alive')).count()}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

outputs = {
    "cardiology_subject": cardiology_subject,
    "cardiology_observation": observations,
    "cardiology_enrollable_patient": cardiology_enrollable_patient,
}
for name, df in outputs.items():
    duplicates = df.groupBy(GOLD_KEYS[name]).count().filter(F.col("count") > 1).count()
    if duplicates:
        raise RuntimeError(f"{name} has {duplicates} duplicate {GOLD_KEYS[name]} values")

for name, df in outputs.items():
    write_gold(df, name)
print("Cardiology gold projection complete.")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
