import unittest

from schema.work_project.assets import WorkProjectAssetType
from schema.work_project.graph import WorkProjectGraphEdgeType
from schema.work_project.osint_manifests import parse_osint_collector_manifest


class OsintManifestParserTests(unittest.TestCase):
    def test_domain_manifest_normalizes_assets_and_dns_relationships(self) -> None:
        plan = parse_osint_collector_manifest({
            "schema": "xuanmu.passive-domain-intel.manifest",
            "schema_version": 1,
            "scope": {"domain": "example.com"},
            "entities": {
                "subdomains": ["www.example.com", "example.com", "outside.test"],
                "ip_addresses": ["192.0.2.10", "2001:0db8::1", "invalid"],
                "dns_records": [
                    {"name": "www.example.com", "type": "A", "value": "192.0.2.10"},
                    {"name": "example.com", "type": "AAAA", "value": "2001:db8::1"},
                    {"name": "outside.test", "type": "A", "value": "192.0.2.10"},
                ],
            },
        })

        identities = {(item.asset.type, item.asset.identifier) for item in plan.assets}
        self.assertIn((WorkProjectAssetType.DOMAIN, "example.com"), identities)
        self.assertIn((WorkProjectAssetType.DOMAIN, "www.example.com"), identities)
        self.assertNotIn((WorkProjectAssetType.DOMAIN, "outside.test"), identities)
        self.assertIn((WorkProjectAssetType.NETWORK, "192.0.2.10/32"), identities)
        self.assertIn((WorkProjectAssetType.NETWORK, "2001:db8::1/128"), identities)
        self.assertEqual(len(plan.relationships), 2)
        self.assertTrue(all(item.type == WorkProjectGraphEdgeType.RESOLVES_TO for item in plan.relationships))
        self.assertTrue(any("invalid IP" in warning for warning in plan.warnings))

    def test_archive_manifest_enforces_exact_prefix_scope(self) -> None:
        plan = parse_osint_collector_manifest({
            "schema": "xuanmu.web-archive-intel.manifest",
            "schema_version": 1,
            "scope": {"url_prefix": "https://example.com/api/"},
            "entities": {
                "urls": [
                    "https://example.com/api/v1?x=1",
                    "http://example.com/api/v1",
                    "https://example.com/admin",
                    "https://example.com/api/../admin",
                    "https://example.com/api/%5C..%5Cadmin",
                    "https://other.example/api/v1",
                ],
            },
        })

        urls = [item.asset.path for item in plan.assets if item.asset.type == WorkProjectAssetType.URL]
        self.assertEqual(urls, ["https://example.com/api/v1?x=1"])
        self.assertEqual(len(plan.relationships), 1)
        self.assertEqual(plan.relationships[0].type, WorkProjectGraphEdgeType.HOSTS)
        self.assertEqual(len(plan.warnings), 5)

    def test_archive_manifest_rejects_dot_segment_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope is not a valid"):
            parse_osint_collector_manifest({
                "schema": "xuanmu.web-archive-intel.manifest",
                "schema_version": 1,
                "scope": {"url_prefix": "https://example.com/api/../admin"},
                "entities": {},
            })

    def test_rejects_unknown_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported OSINT manifest schema"):
            parse_osint_collector_manifest({"schema": "unknown", "schema_version": 1})


if __name__ == "__main__":
    unittest.main()
