"""Contract tests for the mint-time work tool schemas (no provider or filesystem I/O)."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import work_schema


class WorkSchemaContractTests(unittest.TestCase):
    def test_schema_helper_shape(self):
        result = work_schema.schema(
            "n", "d", {"q": {"type": "string"}}, ("q",)
        )
        self.assertEqual(
            result,
            {
                "type": "function",
                "name": "n",
                "description": "d",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                    "additionalProperties": False,
                },
            },
        )
        self.assertIsInstance(result["parameters"]["required"], list)

        default_required = work_schema.schema("n", "d", {})["parameters"]["required"]
        self.assertEqual(default_required, [])
        self.assertIsInstance(default_required, list)

    def test_tool_names_exact_and_unique(self):
        names = [tool["name"] for tool in work_schema.TOOLS]
        self.assertEqual(
            names,
            ["work_search", "ask_hermes", "start_task", "task_status", "what_changed"],
        )
        self.assertEqual(len(names), len(set(names)))

    def test_required_fields_exist_in_properties(self):
        for tool in work_schema.TOOLS:
            with self.subTest(tool=tool["name"]):
                parameters = tool["parameters"]
                self.assertIsInstance(parameters["required"], list)
                for field in parameters["required"]:
                    self.assertIn(field, parameters["properties"])
                self.assertIs(parameters["additionalProperties"], False)

    def test_required_sets(self):
        expected = {
            "work_search": ["query"],
            "ask_hermes": ["question"],
            "start_task": ["title", "details"],
            "task_status": [],
            "what_changed": [],
        }
        actual = {
            tool["name"]: tool["parameters"]["required"]
            for tool in work_schema.TOOLS
        }
        self.assertEqual(actual, expected)

    def test_enums(self):
        by_name = {tool["name"]: tool for tool in work_schema.TOOLS}
        for name, field, expected in [
            ("work_search", "scope", ["recent", "all"]),
            ("start_task", "priority", ["low", "normal", "high", "urgent"]),
            ("what_changed", "since", ["today", "yesterday", "week"]),
        ]:
            with self.subTest(tool=name, property=field):
                self.assertEqual(
                    by_name[name]["parameters"]["properties"][field]["enum"],
                    expected,
                )

    def test_descriptions_are_spoken_safe(self):
        for tool in work_schema.TOOLS:
            with self.subTest(tool=tool["name"]):
                description = tool["description"]
                self.assertIsInstance(description, str)
                self.assertTrue(description.strip())
                self.assertLess(len(description), 600)
                properties = tool["parameters"]["properties"]
                if "project" in properties:
                    self.assertIs(properties["project"], work_schema.PROJECT)

    def test_core_reexports_same_tools(self):
        from z0live_desktop import core

        for tool in work_schema.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertIn(tool, core.TOOLS)
                self.assertTrue(any(item is tool for item in core.TOOLS))

        names = [tool["name"] for tool in core.TOOLS]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
