#!/usr/bin/env python3
"""The agent guide that install.sh ships: size, references and no private details.

    python3 -m unittest discover -s tests -p 'test_guide.py'
"""

import glob
import os
import re
import unittest

TOOLS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SHIPPED = sorted(glob.glob(os.path.join(TOOLS, "checkout", "*.md"))
                 + glob.glob(os.path.join(TOOLS, "docs", "agents", "*.md")))
MAIN = os.path.join(TOOLS, "checkout", "AGENTS.md")
WORD_BUDGET = 2000

# Things that identify one machine or one person. More patterns, one regular
# expression per line, can go in the git-ignored tests/forbidden-terms.local.
FORBIDDEN = {
    "IPv4 address": (r"\b(?!192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)"
                     r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "home path": r"/home/|/Users/|~/dev",
    "e-mail address": r"[\w.+-]+@[\w-]+\.[\w.-]+",
}
LOCAL_TERMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forbidden-terms.local")
if os.path.isfile(LOCAL_TERMS):
    with open(LOCAL_TERMS) as _handle:
        for _number, _line in enumerate(_handle, 1):
            if _line.strip() and not _line.startswith("#"):
                FORBIDDEN[f"local term, line {_number}"] = _line.strip()


def read(path):
    with open(path) as handle:
        return handle.read()


class GuideTest(unittest.TestCase):
    def test_main_guide_stays_within_its_word_budget(self):
        self.assertLessEqual(len(read(MAIN).split()), WORD_BUDGET)

    def test_nothing_shipped_names_a_machine_or_a_private_tree(self):
        for path in SHIPPED:
            text = read(path)
            for what, pattern in FORBIDDEN.items():
                with self.subTest(file=os.path.relpath(path, TOOLS), what=what):
                    found = re.search(pattern, text, re.I if what.startswith("local") else 0)
                    self.assertIsNone(found, found and f"matched at offset {found.start()}")

    def test_tools_named_in_the_guide_exist(self):
        text = read(MAIN)
        names = set(re.findall(r"`tooling/([A-Za-z0-9_]+\.(?:sh|py))`", text))
        self.assertGreaterEqual(len(names), 8)
        for name in names:
            with self.subTest(tool=name):
                self.assertTrue(os.path.isfile(os.path.join(TOOLS, "tooling", name)))
        for name in ("build", "build-tool"):
            self.assertTrue(os.path.isfile(os.path.join(TOOLS, name)))

    def test_procedures_named_in_the_guide_exist(self):
        text = "".join(read(path) for path in SHIPPED)
        for name in set(re.findall(r"tooling/docs/agents/([A-Za-z0-9_.-]+\.md)", text)):
            with self.subTest(procedure=name):
                self.assertTrue(os.path.isfile(os.path.join(TOOLS, "docs", "agents", name)))

    def test_tool_documents_listed_in_the_guide_exist(self):
        text = read(MAIN)
        listed = re.search(r"/docs/<name>`\): (.*?)\.\n", text).group(1)
        names = re.findall(r"`([a-z0-9-]+\.md)`", listed)
        self.assertGreaterEqual(len(names), 4)
        for name in names:
            with self.subTest(document=name):
                self.assertTrue(os.path.isfile(os.path.join(TOOLS, "docs", name)))

    def test_every_procedure_is_linked_from_the_main_guide(self):
        text = read(MAIN)
        for path in glob.glob(os.path.join(TOOLS, "docs", "agents", "*.md")):
            name = os.path.basename(path)
            if name.startswith("AGENTS.local"):
                self.assertIn(name, text)
            else:
                with self.subTest(procedure=name):
                    self.assertIn(f"tooling/docs/agents/{name}", text)


if __name__ == "__main__":
    unittest.main()
