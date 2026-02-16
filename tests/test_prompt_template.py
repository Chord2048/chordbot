"""Tests for chordcode.prompts.template — prompt template rendering."""

from __future__ import annotations

import re
import unittest

from chordcode.prompts.template import render_prompt


class TestRenderPrompt(unittest.TestCase):
    """Core render_prompt behaviour."""

    # -- built-in variables --------------------------------------------------

    def test_builtin_date(self):
        result = render_prompt("Today is {{date}}.")
        self.assertRegex(result, r"Today is \d{4}-\d{2}-\d{2}\.")

    def test_builtin_time(self):
        result = render_prompt("Now {{time}}.")
        self.assertRegex(result, r"Now \d{2}:\d{2}:\d{2}\.")

    def test_builtin_datetime(self):
        result = render_prompt("{{datetime}}")
        self.assertRegex(result, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_builtin_utc_variants(self):
        result = render_prompt("{{date_utc}} {{time_utc}} {{datetime_utc}}")
        parts = result.split()
        self.assertEqual(len(parts), 4)  # date time date+time(2 parts)

    def test_builtin_timezone(self):
        result = render_prompt("tz={{timezone}}")
        self.assertNotIn("{{timezone}}", result)

    def test_builtin_unix_timestamp(self):
        result = render_prompt("ts={{unix_timestamp}}")
        match = re.search(r"ts=(\d+)", result)
        self.assertIsNotNone(match)
        self.assertGreater(int(match.group(1)), 1_000_000_000)

    def test_builtin_os(self):
        result = render_prompt("os={{os}}")
        self.assertNotIn("{{os}}", result)
        self.assertIn("=", result)

    def test_builtin_hostname(self):
        result = render_prompt("host={{hostname}}")
        self.assertNotIn("{{hostname}}", result)

    def test_builtin_cwd(self):
        result = render_prompt("dir={{cwd}}")
        self.assertNotIn("{{cwd}}", result)

    # -- session_context ------------------------------------------------------

    def test_session_context_overrides_builtin(self):
        result = render_prompt(
            "cwd={{cwd}}",
            session_context={"cwd": "/custom/path"},
        )
        self.assertEqual(result, "cwd=/custom/path")

    def test_session_context_custom_keys(self):
        result = render_prompt(
            "model={{model}} agent={{agent}} sid={{session_id}}",
            session_context={
                "model": "gpt-4o",
                "agent": "primary",
                "session_id": "abc-123",
            },
        )
        self.assertEqual(result, "model=gpt-4o agent=primary sid=abc-123")

    def test_session_context_none_values_ignored(self):
        result = render_prompt(
            "wt={{worktree}}",
            session_context={"worktree": None},
        )
        # worktree is not a builtin, so should stay as-is
        self.assertEqual(result, "wt={{worktree}}")

    # -- template_variables (replaces CHORDCODE_TPL_* env vars) ----------------

    def test_template_variable(self):
        result = render_prompt("team={{team}}", template_variables={"team": "backend"})
        self.assertEqual(result, "team=backend")

    def test_template_variable_case_sensitive(self):
        result = render_prompt("p={{project}}", template_variables={"project": "myproj"})
        self.assertEqual(result, "p=myproj")

    def test_empty_braces_not_matched(self):
        # {{}} should not be matched by regex (requires \w+)
        result = render_prompt("x={{}}.")
        self.assertEqual(result, "x={{}}.")

    # -- unknown variables preserved ------------------------------------------

    def test_unknown_variable_preserved(self):
        result = render_prompt("Hello {{unknown_var}}!")
        self.assertEqual(result, "Hello {{unknown_var}}!")

    def test_mixed_known_unknown(self):
        result = render_prompt(
            "{{date}} and {{nonexistent}}",
        )
        self.assertNotIn("{{date}}", result)
        self.assertIn("{{nonexistent}}", result)

    # -- no template variables ------------------------------------------------

    def test_plain_text_passthrough(self):
        text = "No variables here, just plain text."
        self.assertEqual(render_prompt(text), text)

    # -- extra_variables override priority ------------------------------------

    def test_extra_variables_override_session_context(self):
        result = render_prompt(
            "m={{model}}",
            session_context={"model": "gpt-4o"},
            extra_variables={"model": "claude-3"},
        )
        self.assertEqual(result, "m=claude-3")

    def test_extra_variables_override_template_variables(self):
        result = render_prompt(
            "f={{foo}}",
            template_variables={"foo": "from_config"},
            extra_variables={"foo": "from_caller"},
        )
        self.assertEqual(result, "f=from_caller")

    def test_extra_variables_override_builtin(self):
        result = render_prompt(
            "d={{date}}",
            extra_variables={"date": "2000-01-01"},
        )
        self.assertEqual(result, "d=2000-01-01")

    # -- empty / edge cases ---------------------------------------------------

    def test_empty_string(self):
        self.assertEqual(render_prompt(""), "")

    def test_only_variable(self):
        result = render_prompt("{{os}}")
        self.assertNotEqual(result, "{{os}}")

    def test_adjacent_variables(self):
        result = render_prompt("{{date}}{{time}}")
        self.assertNotIn("{{", result)

    def test_nested_braces_not_matched(self):
        result = render_prompt("{{{date}}}")
        # outer brace is literal, inner {{date}} gets replaced
        self.assertRegex(result, r"^\{\d{4}-\d{2}-\d{2}\}$")


if __name__ == "__main__":
    unittest.main()
