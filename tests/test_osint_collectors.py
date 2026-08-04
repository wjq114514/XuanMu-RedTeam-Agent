import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from schema.work_project.assets import WorkProjectAssetType
from schema.work_project.osint_manifests import parse_osint_collector_manifest


ROOT = Path(__file__).resolve().parents[1]
DOMAIN_COLLECTOR = ROOT / "sandbox/.agents/skills/passive-domain-intel/scripts/collect.py"
ARCHIVE_COLLECTOR = ROOT / "sandbox/.agents/skills/web-archive-intel/scripts/collect.py"


class OsintCollectorTests(unittest.TestCase):
    def test_domain_fixture_output_is_deterministic_and_importable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            for record_type in ("A", "AAAA", "CNAME", "MX", "NS", "SOA", "TXT"):
                content = ""
                if record_type == "A":
                    content = "www.example.com. 60 IN A 192.0.2.10\n"
                (fixtures / f"dns_{record_type}.txt").write_text(content, encoding="utf-8")
            (fixtures / "rdap.json").write_text("{}", encoding="utf-8")
            (fixtures / "crtsh.json").write_text(
                json.dumps([{"name_value": "www.example.com\napi.example.com"}]),
                encoding="utf-8",
            )

            first = root / "first"
            second = root / "second"
            self._run(DOMAIN_COLLECTOR, "example.com", first, fixtures)
            self._run(DOMAIN_COLLECTOR, "example.com", second, fixtures)
            self.assertEqual((first / "manifest.json").read_bytes(), (second / "manifest.json").read_bytes())

            payload = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
            plan = parse_osint_collector_manifest(payload)
            identities = {(item.asset.type, item.asset.identifier) for item in plan.assets}
            self.assertIn((WorkProjectAssetType.DOMAIN, "api.example.com"), identities)
            self.assertIn((WorkProjectAssetType.NETWORK, "192.0.2.10/32"), identities)
            self.assertEqual(len(plan.relationships), 1)

    def test_archive_fixture_output_is_importable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            (fixtures / "wayback.json").write_text(json.dumps([
                ["timestamp", "original", "statuscode", "mimetype", "digest"],
                ["20260101000000", "https://example.com/api/v1", "200", "application/json", "A"],
            ]), encoding="utf-8")
            (fixtures / "commoncrawl-indexes.json").write_text(json.dumps([
                {"cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-30-index"},
            ]), encoding="utf-8")
            (fixtures / "commoncrawl.ndjson").write_text(json.dumps({
                "url": "https://example.com/api/v2?x=1",
                "timestamp": "20260202000000",
                "status": "200",
                "mime": "application/json",
                "digest": "B",
            }) + "\n", encoding="utf-8")

            output = root / "output"
            second = root / "second"
            self._run(ARCHIVE_COLLECTOR, "https://example.com/api/", output, fixtures)
            self._run(ARCHIVE_COLLECTOR, "https://example.com/api/", second, fixtures)
            self.assertEqual((output / "manifest.json").read_bytes(), (second / "manifest.json").read_bytes())
            self.assertEqual((output / "summary.json").read_bytes(), (second / "summary.json").read_bytes())
            payload = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            plan = parse_osint_collector_manifest(payload)
            urls = [item.asset.path for item in plan.assets if item.asset.type == WorkProjectAssetType.URL]
            self.assertEqual(urls, ["https://example.com/api/v1", "https://example.com/api/v2?x=1"])
            self.assertEqual(len(plan.relationships), 2)

    def _run(self, collector: Path, scope: str, output: Path, fixtures: Path) -> None:
        result = subprocess.run(
            [str(collector), scope, "--output", str(output), "--fixtures", str(fixtures)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["manifest"], str(output / "manifest.json"))
        self.assertEqual(response["summary"], str(output / "summary.json"))


if __name__ == "__main__":
    unittest.main()
