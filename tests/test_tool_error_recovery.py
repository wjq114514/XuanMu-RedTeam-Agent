import unittest

from config import AgentConfig
from core.agent.model_input import ModelInputAdapter
from core.conversation.context_budget import build_context_run_config
from core.tools.sandbox import read_sandbox_command_output


class ToolErrorRecoveryTests(unittest.TestCase):
    def test_sandbox_reader_uses_canonical_public_name(self) -> None:
        self.assertEqual(read_sandbox_command_output.name, "read_command_output")

    def test_unknown_tools_are_returned_to_model_for_correction(self) -> None:
        config = AgentConfig(
            code="cpe",
            name="CPE",
            description="test",
            base_url="https://example.test/v1",
            api_key="test",
            model="test-model",
            context_window=100_000,
        )
        run_config = build_context_run_config(config)
        self.assertEqual(run_config.tool_not_found_behavior, "return_error_to_model")

    def test_legacy_sandbox_reader_name_is_normalized_in_history(self) -> None:
        adapted = ModelInputAdapter().adapt([
            {
                "type": "function_call",
                "name": "read_sandbox_command_output",
                "arguments": "{}",
                "call_id": "call-1",
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "{}",
            },
        ])
        self.assertIsInstance(adapted, list)
        self.assertEqual(adapted[0]["name"], "read_command_output")


if __name__ == "__main__":
    unittest.main()
