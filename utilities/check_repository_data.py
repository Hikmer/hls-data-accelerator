#!/usr/bin/env python3
"""Reject bundled datasets in Git's index or commits, not merely the working tree."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any

import pyarrow as pa
import pyarrow.parquet as parquet


RECORD_FORMATS = {".csv", ".tsv", ".ndjson", ".jsonl", ".dcm", ".avro", ".orc", ".feather", ".arrow"}
ARCHIVE_FORMATS = {".zip", ".tar", ".gz", ".tgz", ".7z", ".whl", ".jar", ".pbix"}
JSON_FORMATS = {".json", ".avsc", ".bim", ".pbir", ".pbism"}
FHIR_REFERENCE_TYPES = {
    "Organization", "CodeSystem", "ValueSet", "ConceptMap", "StructureDefinition",
    "SearchParameter", "CapabilityStatement", "OperationDefinition", "NamingSystem",
    "CompartmentDefinition", "ImplementationGuide",
}
FHIR_TYPE = re.compile(r"[A-Z][A-Za-z0-9]*\Z")
OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True,
    ).stdout


def _entries(root: Path, revision: str | None) -> list[tuple[str, str, str]]:
    arguments = ("ls-tree", "-rz", "--full-tree", revision) if revision else ("ls-files", "--stage", "-z")
    entries = []
    for record in _git(root, *arguments).split(b"\0"):
        if not record:
            continue
        attributes, filename = record.split(b"\t", 1)
        mode, middle, last = attributes.decode("ascii").split()
        if revision:
            if middle != "blob":
                raise ValueError("Repository data inspection does not support nested Git repositories")
            object_id = last
        else:
            if last != "0":
                raise ValueError("Resolve the unmerged index before checking repository data")
            object_id = middle
        entries.append((filename.decode("utf-8"), object_id, mode))
    return entries


def _blocked_path(path: PurePosixPath) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    return (
        path.name.casefold() == "canonical-fixture-manifest.json"
        or any(part in {"prepackaged", ".generated", ".hds-build"} for part in parts)
        or parts[:2] in {("synthea", "output"), ("synthea", "prepackaged")}
        or parts[:1] == ("output",)
        or "patientoutreachanalytics" in parts
        or (parts[:2] == ("vendor", "microsoft-hds") and any(part in {"sampledata", "referencedata"} for part in parts[3:5]))
    )


def _contains_clinical_fhir(value: Any) -> bool:
    if isinstance(value, dict):
        resource_type = value.get("resourceType")
        if isinstance(resource_type, str) and FHIR_TYPE.fullmatch(resource_type):
            if resource_type != "Bundle" and resource_type not in FHIR_REFERENCE_TYPES:
                return True
        return any(_contains_clinical_fhir(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_clinical_fhir(child) for child in value)
    return False


def _content_violation(path: PurePosixPath, payload: bytes) -> str | None:
    suffix = path.suffix.casefold()
    if suffix == ".parquet":
        return "populated-parquet" if parquet.read_metadata(pa.BufferReader(payload)).num_rows else None
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        return "database-payload" if payload else None
    if suffix == ".ipynb":
        notebook = json.loads(payload.decode("utf-8-sig"))
        if any(cell.get("outputs") for cell in notebook.get("cells", [])):
            return "notebook-output"
        return None
    if "_delta_log" in path.parts:
        for line in payload.decode("utf-8-sig").splitlines():
            if not line.strip():
                continue
            action = json.loads(line)
            addition = action.get("add")
            if addition:
                statistics = json.loads(addition.get("stats") or "{}")
                if statistics.get("numRecords", 0) or statistics.get("minValues") or statistics.get("maxValues"):
                    return "sample-delta-statistics"
        return None
    if suffix in JSON_FORMATS:
        try:
            document = json.loads(payload.decode("utf-8-sig"))
        except json.JSONDecodeError:
            # TypeScript configuration permits comments and trailing commas.
            if path.name == "tsconfig.json" or path.name.startswith("tsconfig."):
                return None
            raise
        if _contains_clinical_fhir(document):
            return "clinical-fhir-record"
    return None


def check_repository(root: Path, revision: str | None = None) -> list[tuple[str, str]]:
    violations: list[tuple[str, str]] = []
    entries = _entries(root, revision)
    inspected: dict[tuple[str, str, bool, bool], str | None] = {}
    with subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as objects:
        assert objects.stdin is not None and objects.stdout is not None
        for filename, object_id, mode in entries:
            path = PurePosixPath(filename)
            suffix = path.suffix.casefold()
            if _blocked_path(path) or suffix in RECORD_FORMATS | ARCHIVE_FORMATS:
                violations.append((filename, "bundled-data-path"))
                continue
            if suffix not in JSON_FORMATS | {".parquet", ".ipynb", ".db", ".sqlite", ".sqlite3"}:
                continue
            if mode == "120000":
                violations.append((filename, "uninspectable-data-symlink"))
                continue
            cache_key = (object_id, suffix, "_delta_log" in path.parts, path.name.startswith("tsconfig."))
            if cache_key not in inspected:
                objects.stdin.write(object_id.encode("ascii") + b"\n")
                objects.stdin.flush()
                header = objects.stdout.readline().split()
                if len(header) != 3 or header[1] != b"blob":
                    raise ValueError(f"Could not read indexed blob for {filename}")
                size = int(header[2])
                payload = objects.stdout.read(size)
                if len(payload) != size or objects.stdout.read(1) != b"\n":
                    raise ValueError(f"Incomplete indexed blob for {filename}")
                try:
                    inspected[cache_key] = _content_violation(path, payload)
                except (ValueError, UnicodeError, pa.ArrowException):
                    inspected[cache_key] = "unreadable-data-format"
            if inspected[cache_key]:
                violations.append((filename, str(inspected[cache_key])))
        objects.stdin.close()
        if objects.wait() != 0:
            raise RuntimeError("Git object inspection failed")
    return violations


def push_revisions(root: Path, updates: str) -> list[str]:
    revisions: set[str] = set()
    for line in updates.splitlines():
        _local_ref, local_id, _remote_ref, remote_id = line.split()
        if not OBJECT_ID.fullmatch(local_id) or not OBJECT_ID.fullmatch(remote_id):
            raise ValueError("Invalid object ID received from Git pre-push")
        if set(local_id) == {"0"}:
            continue
        revisions.add(local_id)
        arguments = ("rev-list", local_id, "--not", "--remotes") if set(remote_id) == {"0"} else ("rev-list", f"{remote_id}..{local_id}")
        revisions.update(_git(root, *arguments).decode("ascii").splitlines())
    return sorted(revisions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--revision", help="Inspect a commit instead of the index")
    target.add_argument("--pre-push", action="store_true", help="Inspect every outgoing commit from Git pre-push input")
    args = parser.parse_args()
    try:
        revisions = push_revisions(args.root, sys.stdin.read()) if args.pre_push else [args.revision]
        problems = []
        for revision in revisions:
            problems.extend((revision or "index", path, reason) for path, reason in check_repository(args.root, revision))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Repository data check could not complete: {exc}", file=sys.stderr)
        return 2
    for revision, path, reason in problems:
        print(f"{revision}: {path}: {reason}", file=sys.stderr)
    if problems:
        print("Keep generated/downloaded datasets outside Git; retain only empty schemas and source/configuration.", file=sys.stderr)
        return 1
    print(f"Repository data check passed ({len(revisions)} Git snapshot(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
