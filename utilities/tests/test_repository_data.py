from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import pyarrow as pa
import pyarrow.parquet as parquet


spec = importlib.util.spec_from_file_location(
    "repository_data_guard", Path(__file__).resolve().parents[1] / "check_repository_data.py",
)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class RepositoryDataTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Repository data test")
        self.git("config", "user.email", "repository-data-test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments], check=True, capture_output=True, text=True,
        ).stdout.strip()

    def stage(self, name: str, content: object) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")
        self.git("add", "-f", "--", name)
        return path

    def commit(self) -> str:
        self.git("commit", "-qm", "Data guard boundary")
        return self.git("rev-parse", "HEAD")

    def test_empty_schemas_and_reference_definitions_remain_supported(self) -> None:
        schema = self.root / "schema.parquet"
        parquet.write_table(pa.table({"patientId": pa.array([], type=pa.string())}), schema)
        self.git("add", "--", schema.name)
        self.stage("providers.json", {"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Organization", "id": "clinic"}}]})
        self.stage("mapping.json", {"resourceType": "lit('ImagingStudy')", "id": {"tag": "StudyInstanceUID", "calc": "sha1(col('StudyInstanceUID'))"}})
        self.stage("table/_delta_log/000.json", {"add": {"stats": json.dumps({"numRecords": 0, "nullCount": {"patientId": 0}})}})
        self.stage("source.ipynb", {"cells": [{"cell_type": "code", "source": ["print('generate data at runtime')"], "outputs": []}]})
        self.assertEqual(guard.check_repository(self.root), [])

    def test_staged_patient_data_is_rejected_even_when_worktree_is_cleaned(self) -> None:
        patient = self.stage("renamed.json", {"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Patient", "id": "synthetic"}}]})
        patient.write_text("{}", encoding="utf-8")
        self.assertEqual(guard.check_repository(self.root), [("renamed.json", "clinical-fhir-record")])

    def test_populated_parquet_is_not_confused_with_a_schema(self) -> None:
        dataset = self.root / "schema.parquet"
        parquet.write_table(pa.table({"patientId": ["synthetic"]}), dataset)
        self.git("add", "--", dataset.name)
        self.assertEqual(guard.check_repository(self.root), [("schema.parquet", "populated-parquet")])

    def test_zero_rows_do_not_allow_retained_sample_statistics(self) -> None:
        self.stage("table/_delta_log/000.json", {"add": {"stats": json.dumps({"numRecords": 0, "minValues": {"patientId": "synthetic"}})}})
        self.assertEqual(guard.check_repository(self.root), [("table/_delta_log/000.json", "sample-delta-statistics")])

    def test_forced_adds_and_notebook_output_are_rejected(self) -> None:
        self.stage("synthea/.generated/patient.json", {})
        self.stage("records.ndjson", {"id": "synthetic"})
        self.stage("source.ipynb", {"cells": [{"cell_type": "code", "source": [], "outputs": [{"output_type": "stream", "text": "synthetic patient"}]}]})
        self.assertEqual(guard.check_repository(self.root), [
            ("records.ndjson", "bundled-data-path"),
            ("source.ipynb", "notebook-output"),
            ("synthea/.generated/patient.json", "bundled-data-path"),
        ])

    def test_outgoing_intermediate_commit_cannot_hide_data_with_later_deletion(self) -> None:
        self.stage("config.json", {})
        base = self.commit()
        self.stage("patient.json", {"resourceType": "Patient", "id": "synthetic"})
        data_commit = self.commit()
        self.git("rm", "--", "patient.json")
        clean_commit = self.commit()
        self.assertEqual(guard.check_repository(self.root, clean_commit), [])
        updates = f"refs/heads/main {clean_commit} refs/heads/main {base}\n"
        revisions = guard.push_revisions(self.root, updates)
        self.assertEqual(set(revisions), {data_commit, clean_commit})
        violations = {revision: guard.check_repository(self.root, revision) for revision in revisions}
        self.assertEqual(violations[data_commit], [("patient.json", "clinical-fhir-record")])
        self.assertEqual(guard.push_revisions(self.root, f"refs/heads/main {'0' * 40} refs/heads/main {base}\n"), [])


if __name__ == "__main__":
    unittest.main()
