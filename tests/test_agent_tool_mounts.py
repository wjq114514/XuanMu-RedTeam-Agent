import unittest

from core.agent.registry import AgentToolSnapshot, _tool_mount_available
from core.agent.specs import AGENT_SPECS


class AgentToolMountTests(unittest.TestCase):
    def test_specialist_prefers_sandbox_tools_when_container_is_bound(self) -> None:
        cie = next(spec for spec in AGENT_SPECS if spec.code == "cie")
        local_names = {
            mount.tool.name
            for mount in cie.tools
            if _tool_mount_available(mount, AgentToolSnapshot())
        }
        sandbox_names = {
            mount.tool.name
            for mount in cie.tools
            if _tool_mount_available(mount, AgentToolSnapshot(sandbox_container_id=7))
        }

        self.assertIn("execute_command", local_names)
        self.assertNotIn("execute_sync_command", local_names)
        self.assertNotIn("execute_command", sandbox_names)
        self.assertIn("execute_sync_command", sandbox_names)
        self.assertIn("execute_async_command", sandbox_names)
        self.assertIn("load_skill", sandbox_names)
        self.assertIn("read_command_output", local_names)
        self.assertIn("read_command_output", sandbox_names)
        self.assertNotIn("read_sandbox_command_output", sandbox_names)


if __name__ == "__main__":
    unittest.main()
