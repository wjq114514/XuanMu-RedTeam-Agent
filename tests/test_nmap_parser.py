import unittest

from service.work_project.nmap_parser import NmapXmlError, parse_nmap_xml


class NmapParserTests(unittest.TestCase):
    def test_parses_up_host_assets_and_relationships(self) -> None:
        report = parse_nmap_xml(b"""<?xml version="1.0"?>
<nmaprun scanner="nmap">
  <host>
    <status state="up"/>
    <address addr="192.0.2.10" addrtype="ipv4"/>
    <hostnames><hostname name="Web.Example.test." type="PTR"/></hostnames>
    <ports>
      <port protocol="tcp" portid="443">
        <state state="open"/>
        <service name="https" product="nginx" version="1.25" extrainfo="Ubuntu" tunnel="ssl"/>
      </port>
    </ports>
  </host>
</nmaprun>""")

        assets = {asset.key: asset for asset in report.assets}
        self.assertEqual(report.counts.hosts, 1)
        self.assertIn("network:192.0.2.10/32", assets)
        self.assertIn("domain:web.example.test", assets)
        self.assertIn("service:192.0.2.10:443", assets)
        service = assets["service:192.0.2.10:443"]
        self.assertEqual(service.extra.service_name, "https")
        self.assertEqual(service.extra.banner, "ssl https nginx 1.25 Ubuntu")
        relationship_types = {relationship.type.value for relationship in report.relationships}
        self.assertEqual(relationship_types, {"resolves_to", "hosts"})

    def test_ignores_non_open_ports(self) -> None:
        report = parse_nmap_xml(b"""<nmaprun><host><status state="up"/>
<address addr="198.51.100.7" addrtype="ipv4"/><ports>
<port protocol="tcp" portid="22"><state state="closed"/><service name="ssh"/></port>
<port protocol="tcp" portid="80"><state state="filtered"/></port>
</ports></host></nmaprun>""")

        self.assertEqual(report.counts.networks, 1)
        self.assertEqual(report.counts.services, 0)
        self.assertEqual(report.counts.relationships, 0)

    def test_accepts_standard_nmap_doctype(self) -> None:
        report = parse_nmap_xml(b"""<?xml version="1.0"?>
<!DOCTYPE nmaprun>
<nmaprun><host><status state="up"/>
<address addr="192.0.2.20" addrtype="ipv4"/></host></nmaprun>""")

        self.assertEqual(report.counts.hosts, 1)
        self.assertEqual(report.counts.networks, 1)

    def test_rejects_custom_doctype_and_entity(self) -> None:
        xml = b"""<!DOCTYPE nmaprun [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<nmaprun><host><status state="up"/></host></nmaprun>"""
        with self.assertRaisesRegex(NmapXmlError, "ENTITY"):
            parse_nmap_xml(xml)

        with self.assertRaisesRegex(NmapXmlError, "ENTITY"):
            parse_nmap_xml(xml.decode().encode("utf-16"))

        custom_dtd = b"""<!DOCTYPE nmaprun SYSTEM "file:///tmp/custom.dtd">
<nmaprun/>"""
        with self.assertRaisesRegex(NmapXmlError, "standard"):
            parse_nmap_xml(custom_dtd)

    def test_tcp_and_udp_same_port_have_distinct_identity(self) -> None:
        report = parse_nmap_xml(b"""<nmaprun><host><status state="up"/>
<address addr="203.0.113.4" addrtype="ipv4"/><ports>
<port protocol="tcp" portid="53"><state state="open"/><service name="domain"/></port>
<port protocol="udp" portid="53"><state state="open"/><service name="domain"/></port>
</ports></host></nmaprun>""")

        service_keys = {asset.key for asset in report.assets if asset.type.value == "service"}
        self.assertEqual(
            service_keys,
            {"service:203.0.113.4:53", "service:203.0.113.4:53/udp"},
        )

    def test_reports_incomplete_xml(self) -> None:
        with self.assertRaisesRegex(NmapXmlError, "incomplete"):
            parse_nmap_xml(b"<nmaprun><host>")


if __name__ == "__main__":
    unittest.main()
