import unittest

from model.work_project.assets import WorkProjectAsset
from schema.work_project.assets import WorkProjectAssetOrigin, WorkProjectAssetType
from schema.work_project.osint_manifests import parse_osint_collector_manifest
from service.work_project.osint_manifests import _plan_matches_project_scope


class OsintImportScopeTests(unittest.TestCase):
    def test_domain_scope_requires_authoritative_parent_domain(self) -> None:
        plan = parse_osint_collector_manifest({
            "schema": "xuanmu.passive-domain-intel.manifest",
            "schema_version": 1,
            "scope": {"domain": "api.example.com"},
            "entities": {},
        })
        scope = WorkProjectAsset(
            type=WorkProjectAssetType.DOMAIN,
            origin=WorkProjectAssetOrigin.SCOPE,
            host="example.com",
            identifier="example.com",
        )
        unrelated = WorkProjectAsset(
            type=WorkProjectAssetType.DOMAIN,
            origin=WorkProjectAssetOrigin.SCOPE,
            host="example.net",
            identifier="example.net",
        )
        self.assertTrue(_plan_matches_project_scope(plan, [scope]))
        self.assertFalse(_plan_matches_project_scope(plan, [unrelated]))

    def test_archive_scope_accepts_domain_or_narrower_url_scope(self) -> None:
        plan = parse_osint_collector_manifest({
            "schema": "xuanmu.web-archive-intel.manifest",
            "schema_version": 1,
            "scope": {"url_prefix": "https://api.example.com/v1/"},
            "entities": {},
        })
        domain_scope = WorkProjectAsset(
            type=WorkProjectAssetType.DOMAIN,
            origin=WorkProjectAssetOrigin.SCOPE,
            host="example.com",
            identifier="example.com",
        )
        url_scope = WorkProjectAsset(
            type=WorkProjectAssetType.URL,
            origin=WorkProjectAssetOrigin.SCOPE,
            path="https://api.example.com/",
            identifier="https://api.example.com/",
        )
        wrong_url = WorkProjectAsset(
            type=WorkProjectAssetType.URL,
            origin=WorkProjectAssetOrigin.SCOPE,
            path="https://api.example.com/admin/",
            identifier="https://api.example.com/admin/",
        )
        self.assertTrue(_plan_matches_project_scope(plan, [domain_scope]))
        self.assertTrue(_plan_matches_project_scope(plan, [url_scope]))
        self.assertFalse(_plan_matches_project_scope(plan, [wrong_url]))

    def test_archive_scope_does_not_inherit_from_service_asset(self) -> None:
        plan = parse_osint_collector_manifest({
            "schema": "xuanmu.web-archive-intel.manifest",
            "schema_version": 1,
            "scope": {"url_prefix": "https://api.example.com/"},
            "entities": {},
        })
        service_scope = WorkProjectAsset(
            type=WorkProjectAssetType.SERVICE,
            origin=WorkProjectAssetOrigin.SCOPE,
            host="api.example.com",
            port=8443,
            identifier="api.example.com:8443",
        )
        self.assertFalse(_plan_matches_project_scope(plan, [service_scope]))


if __name__ == "__main__":
    unittest.main()
