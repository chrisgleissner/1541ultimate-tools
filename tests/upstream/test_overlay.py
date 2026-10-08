#!/usr/bin/env python3
"""Installs the tools into a 1541ultimate checkout exactly as README.md says.

The README's development install block is run, unchanged, against a fresh
clone of the checkout. The tests then check what the README promises: the
tools in place and executable, a clean `git status`, and a `build-tool` that
starts and knows every target.

    ULTIMATE_REPO_DIR=/path/to/1541ultimate python3 -m unittest tests/upstream/test_overlay.py

Without ULTIMATE_REPO_DIR every test is skipped; with UPSTREAM_REQUIRED=1 a
missing checkout is a failure instead.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import readme  # noqa: E402

TOOLS = readme.TOOLS
REPO = os.environ.get("ULTIMATE_REPO_DIR", "")
REQUIRED = os.environ.get("UPSTREAM_REQUIRED") == "1"
TARGETS = ("u2", "u2plus", "u2pl", "u64", "u64ii")


def setUpModule():
    if not REPO or not os.path.exists(os.path.join(REPO, ".git")):
        if REQUIRED:
            raise RuntimeError(f"ULTIMATE_REPO_DIR={REPO!r} is not a 1541ultimate checkout")
        raise unittest.SkipTest("ULTIMATE_REPO_DIR is not set")


class Overlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="overlay_")
        cls.checkout = os.path.join(cls.tmp, "1541ultimate")
        # The README works from a parent directory holding both checkouts.
        os.symlink(TOOLS, os.path.join(cls.tmp, "1541ultimate-tools"))
        subprocess.run(["git", "clone", "-q", "--shared", REPO, cls.checkout], check=True)
        cls.install = readme.run(readme.install_block(), cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_tool(self, *args, env=None):
        return subprocess.run(["./build-tool", *args], cwd=self.checkout,
                              env={**os.environ, **(env or {})},
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

    def test_git_status_stays_clean(self):
        self.assertEqual(self.install.returncode, 0, self.install.stderr)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.checkout,
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "", "the installer leaves these paths visible to git")

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
        self.assertIn("Unknown target: no-such-target", result.stdout + result.stderr)

    def test_jtag_tool_help_runs_in_the_overlay(self):
        result = subprocess.run(["python3", "tooling/u64ii_jtag.py", "--help"], cwd=self.checkout,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("probe", "fpga", "run", "recover"):
            with self.subTest(command=command):
                self.assertIn(command, result.stdout)


if __name__ == "__main__":
    unittest.main()
