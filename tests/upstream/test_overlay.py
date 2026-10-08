#!/usr/bin/env python3
"""Installs the tools into a 1541ultimate checkout exactly as README.md says.

The README's own shell blocks are run, unchanged, against a fresh clone of the
checkout: the `cp` block that overlays the tools and the block that excludes
them from git. The tests then check what the README promises: a clean
`git status`, and a `build-tool` that starts and knows every target.

    ULTIMATE_REPO_DIR=/path/to/1541ultimate python3 -m unittest tests/upstream/test_overlay.py

Without ULTIMATE_REPO_DIR every test is skipped; with UPSTREAM_REQUIRED=1 a
missing checkout is a failure instead.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.environ.get("ULTIMATE_REPO_DIR", "")
REQUIRED = os.environ.get("UPSTREAM_REQUIRED") == "1"
TARGETS = ("u2", "u2plus", "u2pl", "u64", "u64ii")


def setUpModule():
    if not REPO or not os.path.exists(os.path.join(REPO, ".git")):
        if REQUIRED:
            raise RuntimeError(f"ULTIMATE_REPO_DIR={REPO!r} is not a 1541ultimate checkout")
        raise unittest.SkipTest("ULTIMATE_REPO_DIR is not set")


def readme_blocks():
    """The bash blocks of the README's install and exclude sections."""
    with open(os.path.join(TOOLS, "README.md")) as handle:
        text = handle.read()
    section = text[text.index("## Installing into a checkout"):text.index("## Prerequisites")]
    return re.findall(r"```bash\n(.*?)```", section, re.S)


class Overlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="overlay_")
        cls.checkout = os.path.join(cls.tmp, "1541ultimate")
        # The README works from a parent directory holding both checkouts.
        os.symlink(TOOLS, os.path.join(cls.tmp, "1541ultimate-tools"))
        subprocess.run(["git", "clone", "-q", "--shared", REPO, cls.checkout], check=True)
        blocks = readme_blocks()
        install = [b for b in blocks if "cp -r ../1541ultimate-tools/" in b]
        exclude = [b for b in blocks if ".git/info/exclude" in b]
        if len(install) != 1 or len(exclude) != 1:
            raise AssertionError("README.md no longer has one install and one exclude block")
        # The install block starts with the clone commands and `cd 1541ultimate`;
        # run it from the line that changes into the checkout.
        script = install[0]
        script = script[script.index("cd 1541ultimate"):]
        cls.install = subprocess.run(["bash", "-euc", script], cwd=cls.tmp,
                                     capture_output=True, text=True)
        # Like the install block, it starts with `cd 1541ultimate`.
        cls.exclude = subprocess.run(["bash", "-euc", exclude[0]], cwd=cls.tmp,
                                     capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_tool(self, *args):
        return subprocess.run(["./build-tool", *args], cwd=self.checkout,
                              capture_output=True, text=True, timeout=120)

    def test_readme_install_block_runs(self):
        self.assertEqual(self.install.returncode, 0, self.install.stderr)
        for path in ("build", "build-tool", "build-tool.d/common.sh",
                     "tooling/u64ii_jtag.py", "tooling/build_and_deploy_u64.sh"):
            with self.subTest(path=path):
                self.assertTrue(os.path.exists(os.path.join(self.checkout, path)))
        for path in ("build", "build-tool", "tooling/u64ii_jtag.sh", "tooling/u64ii_jtag.py"):
            with self.subTest(executable=path):
                self.assertTrue(os.access(os.path.join(self.checkout, path), os.X_OK))

    def test_readme_exclude_block_leaves_git_status_clean(self):
        self.assertEqual(self.exclude.returncode, 0, self.exclude.stderr)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.checkout,
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "", "the README's exclude list misses these files")

    def test_build_tool_lists_every_target(self):
        result = self.run_tool("--list-targets")
        self.assertEqual(result.returncode, 0, result.stderr)
        for target in TARGETS:
            with self.subTest(target=target):
                self.assertRegex(result.stdout, rf"(?m)^\s*{target}\b")

    def test_build_tool_help(self):
        result = self.run_tool("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--jtag", result.stdout)

    def test_build_tool_refuses_an_unknown_target(self):
        result = self.run_tool("no-such-target")
        self.assertNotEqual(result.returncode, 0)

    def test_jtag_tool_help_runs_in_the_overlay(self):
        result = subprocess.run(["python3", "tooling/u64ii_jtag.py", "--help"], cwd=self.checkout,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("probe", "fpga", "run", "recover"):
            with self.subTest(command=command):
                self.assertIn(command, result.stdout)


if __name__ == "__main__":
    unittest.main()
