from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SYNTHEA_DIR = Path(__file__).resolve().parents[1]


class CanonicalFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "synthea"
        self.source.mkdir()
        for filename in ("generate_cached_bundles.py", "validate_canonical_fixture.py"):
            shutil.copyfile(SYNTHEA_DIR / filename, self.source / filename)

    def run_cli(self, script: str, *args: object, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(self.source / script), *(str(arg) for arg in args)],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_source_only_checkout_generates_and_validates_canonical100(self) -> None:
        self.run_cli("generate_cached_bundles.py")
        summary = json.loads(self.run_cli("validate_canonical_fixture.py").stdout)
        generated = self.source / ".generated"
        manifest = json.loads((generated / "canonical-fixture-manifest.json").read_text())
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["patientCount"], 100)
        self.assertEqual(summary["resourceCounts"], manifest["resourceCounts"])
        self.assertEqual(len(manifest["deviceAssignments"]), 100)
        self.assertEqual(
            {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (generated / "prepackaged").glob("*.json")},
            manifest["files"],
        )
        self.assertFalse((self.source / "prepackaged").exists())
        self.assertFalse((self.source / "canonical-fixture-manifest.json").exists())

    def test_explicit_output_regenerates_deterministically_and_rejects_tampering(self) -> None:
        generated = self.root / "separate generated output"
        self.run_cli("generate_cached_bundles.py", "--output-root", generated)
        manifest_path = generated / "canonical-fixture-manifest.json"
        original_manifest = manifest_path.read_bytes()
        original_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (generated / "prepackaged").glob("*.json")
        }
        stale_bundle = generated / "prepackaged" / "stale-patient.json"
        stale_bundle.write_text("{}")
        self.run_cli("generate_cached_bundles.py", "--output-root", generated)
        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertEqual(
            {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (generated / "prepackaged").glob("*.json")},
            original_hashes,
        )
        self.assertFalse(stale_bundle.exists())
        summary = json.loads(self.run_cli("validate_canonical_fixture.py", "--root", generated).stdout)
        self.assertEqual(summary["patientCount"], 100)
        self.assertFalse((self.source / ".generated").exists())

        bundle_path = generated / "prepackaged" / next(iter(original_hashes))
        bundle_path.write_bytes(bundle_path.read_bytes() + b"\n")
        rejected = self.run_cli("validate_canonical_fixture.py", "--root", generated, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("checksum mismatch", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
