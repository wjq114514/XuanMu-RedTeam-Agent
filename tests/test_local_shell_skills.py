import unittest
from pathlib import Path

from core.tools.local_shell import _local_skill_names, _local_skill_root


class LocalShellSkillTests(unittest.TestCase):
    def test_builtin_nmap_skill_is_available_locally(self) -> None:
        self.assertIn("nmap", _local_skill_names())
        skill_file = _local_skill_root("nmap") / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        body = skill_file.read_text(encoding="utf-8")
        self.assertIn("## Stage 1: Host Discovery", body)
        self.assertIn("-oA", body)

    def test_passive_osint_skills_are_available_locally(self) -> None:
        names = _local_skill_names()
        self.assertIn("passive-domain-intel", names)
        self.assertIn("web-archive-intel", names)

    def test_unknown_skill_resolves_under_custom_root(self) -> None:
        self.assertEqual(_local_skill_root("example"), Path(".agents/skills/example"))


if __name__ == "__main__":
    unittest.main()
