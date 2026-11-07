#!/usr/bin/env python3
"""The agent skills that install.sh ships: format, size, references and no private details.

    python3 -m unittest discover -s tests -p 'test_skills.py'

With ULTIMATE_REPO_DIR set to a 1541ultimate checkout, the firmware files the
skills name are checked against it as well.
"""

import glob
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_guide import FORBIDDEN  # noqa: E402  (the generic and the local patterns)

TOOLS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SKILLS = sorted(glob.glob(os.path.join(TOOLS, "skills", "*", "SKILL.md")))
EXPECTED = {"c64u-jtag-run", "u2pl-recover", "u64-flash-unattended", "pr-minimal-diff",
            "vhdl-analysis", "device-failure-triage"}
BODY_WORD_BUDGET = 500
REPO = os.environ.get("ULTIMATE_REPO_DIR")
URL = re.compile(r"https://github\.com/chrisgleissner/1541ultimate-tools/blob/\{\{TOOLS_REF\}\}/docs/([A-Za-z0-9_.-]+\.md)")

# Skills are read by agents on every machine, so they also carry no dated
# incident, no path under a home directory and no personal tool name.
EXTRA_FORBIDDEN = {
    "home-relative path": r"(^|[\s`(])~/",
    "date": r"\b20\d\d-\d\d-\d\d\b",
    "month and year": r"\b(January|February|March|April|May|June|July|August|September|October|"
                      r"November|December) 20\d\d\b",
}


def read(path):
    with open(path) as handle:
        return handle.read()


def split(path):
    """The frontmatter fields and the body of one skill."""
    text = read(path)
    match = re.match(r"---\n(.*?)\n---\n(.*)\Z", text, re.S)
    assert match, f"{path} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, match.group(2)


def name_of(path):
    return os.path.basename(os.path.dirname(path))


class SkillsTest(unittest.TestCase):
    def test_the_expected_skills_are_present_and_nothing_else_is_in_their_directories(self):
        self.assertEqual({name_of(path) for path in SKILLS}, EXPECTED)
        for path in SKILLS:
            with self.subTest(skill=name_of(path)):
                self.assertEqual(os.listdir(os.path.dirname(path)), ["SKILL.md"])

    def test_frontmatter_has_a_name_and_a_description_with_triggers(self):
        for path in SKILLS:
            fields, _ = split(path)
            with self.subTest(skill=name_of(path)):
                self.assertEqual(set(fields), {"name", "description"})
                self.assertEqual(fields["name"], name_of(path))
                self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertLessEqual(len(fields["name"]), 64)
                self.assertTrue(0 < len(fields["description"]) <= 1024)
                self.assertRegex(fields["description"], r"\bUse (for|when)\b")
                self.assertIn('"', fields["description"])        # quoted trigger phrases

    def test_body_stays_within_its_word_budget(self):
        for path in SKILLS:
            _, body = split(path)
            with self.subTest(skill=name_of(path)):
                self.assertLessEqual(len(body.split()), BODY_WORD_BUDGET)

    def test_nothing_shipped_names_a_machine_a_private_tree_or_a_date(self):
        for path in SKILLS:
            text = read(path)
            for what, pattern in {**FORBIDDEN, **EXTRA_FORBIDDEN}.items():
                with self.subTest(skill=name_of(path), what=what):
                    found = re.search(pattern, text, re.I if what.startswith("local") else re.M)
                    self.assertIsNone(found, found and f"matched at offset {found.start()}")

    def test_tools_and_procedures_named_in_the_skills_exist(self):
        for path in SKILLS:
            text = read(path)
            with self.subTest(skill=name_of(path)):
                for name in re.findall(r"`tooling/([A-Za-z0-9_]+\.(?:sh|py))", text):
                    self.assertTrue(os.path.isfile(os.path.join(TOOLS, "tooling", name)), name)
                for name in re.findall(r"tooling/docs/agents/([A-Za-z0-9_.-]+\.md)", text):
                    self.assertTrue(os.path.isfile(os.path.join(TOOLS, "docs", "agents", name)), name)
                for name in URL.findall(text):
                    self.assertTrue(os.path.isfile(os.path.join(TOOLS, "docs", name)), name)

    def test_documents_are_linked_only_in_the_pinned_form(self):
        # Installed skills cannot rely on docs/ in a checkout, so a link names
        # the tools repository at {{TOOLS_REF}}, which install.sh fills in.
        for path in SKILLS:
            text = read(path)
            with self.subTest(skill=name_of(path)):
                self.assertNotRegex(text, r"(?<![/\w])docs/[A-Za-z0-9_.-]+\.md")
                self.assertEqual(text.count("{{"), len(URL.findall(text)))
                self.assertNotIn("github.com/chrisgleissner/1541ultimate-tools/blob/main", text)

    def test_each_skill_with_hardware_states_its_authorisation_rule(self):
        for path in SKILLS:
            name = name_of(path)
            if name in ("c64u-jtag-run", "u2pl-recover", "u64-flash-unattended"):
                text = read(path)
                with self.subTest(skill=name):
                    self.assertIn("device lock", text)
                    self.assertRegex(text, r"(?i)only when the task asks|needs the current task to ask")

    def test_firmware_files_named_in_the_skills_exist(self):
        if not REPO:
            self.skipTest("ULTIMATE_REPO_DIR is not set")
        pattern = r"`((?:fpga|software|target|tests)/[A-Za-z0-9_./-]+\.[a-z]+)`"
        checked = 0
        for path in SKILLS:
            for name in re.findall(pattern, read(path)):
                if "/result/" in name:
                    continue                      # build output
                with self.subTest(skill=name_of(path), file=name):
                    self.assertTrue(os.path.isfile(os.path.join(REPO, name)), name)
                checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_readme_lists_every_skill(self):
        readme = read(os.path.join(TOOLS, "README.md"))
        for name in EXPECTED:
            with self.subTest(skill=name):
                self.assertIn(f"`{name}`", readme)


if __name__ == "__main__":
    unittest.main()
