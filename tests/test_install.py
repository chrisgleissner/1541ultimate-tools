#!/usr/bin/env python3
"""Host tests for install.sh against throwaway git repositories; no network.

Each test builds a minimal checkout with the 1541ultimate layout (a commit,
software/ and target/), runs install.sh on it, and checks what a user relies
on: the tools arrive and run, nothing else from this repository does (no
documentation, logo or tests), git sees none of it, a second run is harmless,
and a worktree's checkout works like a plain one.

    python3 -m unittest discover -s tests -p 'test_install.py'
"""

import os
import shutil
import subprocess
import tempfile
import unittest

TOOLS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
INSTALL = os.path.join(TOOLS, "install.sh")
with open(os.path.join(TOOLS, "VERSION")) as _handle:
    VERSION = _handle.read().strip()


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                           "-c", "init.defaultBranch=master", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="install_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.checkout = os.path.join(self.tmp, "1541ultimate")
        os.makedirs(os.path.join(self.checkout, "software"))
        os.makedirs(os.path.join(self.checkout, "target"))
        for name in ("software/main.c", "target/Makefile", "Makefile"):
            with open(os.path.join(self.checkout, name), "w") as handle:
                handle.write("x\n")
        git("init", "-q", cwd=self.checkout)
        git("add", ".", cwd=self.checkout)
        git("commit", "-qm", "base", cwd=self.checkout)

    def install(self, *args, checkout=None):
        return subprocess.run(["bash", INSTALL, *args, checkout or self.checkout],
                              capture_output=True, text=True, timeout=60)

    def path(self, *parts):
        return os.path.join(self.checkout, *parts)

    def excludes(self, checkout=None):
        path = git("rev-parse", "--path-format=absolute", "--git-path", "info/exclude",
                   cwd=checkout or self.checkout).strip()
        with open(path) as handle:
            return handle.read().splitlines()

    def test_installs_the_tools_and_they_run(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in ("build", "build.cmd", "build-tool", ".build-tool.env.example",
                     "build-tool.d/common.sh", "tooling/u64ii_jtag.py", "tooling/u64ii_jtag.sh",
                     "tooling/build_and_deploy_u64.sh", "tooling/flash_u64.py"):
            with self.subTest(path=path):
                self.assertTrue(os.path.isfile(self.path(path)))
        for path in ("build", "build-tool", "tooling/u64ii_jtag.sh", "tooling/flash_u64.py"):
            with self.subTest(executable=path):
                self.assertTrue(os.access(self.path(path), os.X_OK))
        tool = subprocess.run(["./build-tool", "--list-targets"], cwd=self.checkout,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(tool.returncode, 0, tool.stderr)
        self.assertIn("u64ii", tool.stdout)

    def test_copies_only_the_tools(self):
        self.assertEqual(self.install().returncode, 0)
        for path in ("docs", "tests", "vivado", "patches", "README.md", "install.sh", "VERSION"):
            with self.subTest(path=path):
                self.assertFalse(os.path.exists(self.path(path)))
        installed = os.listdir(self.path("tooling"))
        self.assertFalse([name for name in installed if name.startswith("test_")])
        self.assertFalse([name for name in installed if name.endswith(".png")])

    def test_records_the_version(self):
        self.assertEqual(self.install().returncode, 0)
        with open(self.path(".1541ultimate-tools-version")) as handle:
            self.assertEqual(handle.read().strip(), VERSION)

    def test_git_sees_nothing_and_tracked_files_are_untouched(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout), "")
        self.assertEqual(git("diff", "HEAD", cwd=self.checkout), "")

    def test_second_run_adds_no_duplicate_excludes(self):
        self.assertEqual(self.install().returncode, 0)
        first = self.excludes()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("added 0 entries", result.stdout)
        self.assertEqual(self.excludes(), first)
        for entry in ("build", "build-tool", "build-tool.d/", "tooling/",
                      ".1541ultimate-tools-version"):
            with self.subTest(entry=entry):
                self.assertEqual(first.count(entry), 1)

    def test_keeps_local_files_in_tooling(self):
        os.makedirs(self.path("tooling"))
        with open(self.path("tooling", "my_script.sh"), "w") as handle:
            handle.write("echo mine\n")
        self.assertEqual(self.install().returncode, 0)
        with open(self.path("tooling", "my_script.sh")) as handle:
            self.assertEqual(handle.read(), "echo mine\n")

    def test_overwrites_an_older_install(self):
        self.assertEqual(self.install().returncode, 0)
        with open(self.path("tooling", "u64ii_jtag.py"), "w") as handle:
            handle.write("old\n")
        with open(self.path(".1541ultimate-tools-version"), "w") as handle:
            handle.write("0.0.1\n")
        self.assertEqual(self.install().returncode, 0)
        with open(self.path("tooling", "u64ii_jtag.py")) as new, \
                open(os.path.join(TOOLS, "tooling", "u64ii_jtag.py")) as source:
            self.assertEqual(new.read(), source.read())
        with open(self.path(".1541ultimate-tools-version")) as handle:
            self.assertEqual(handle.read().strip(), VERSION)

    def test_worktree_uses_the_shared_exclude_file(self):
        worktree = os.path.join(self.tmp, "wt")
        git("worktree", "add", "-q", "--detach", worktree, cwd=self.checkout)
        result = self.install(checkout=worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=worktree), "")
        self.assertIn("tooling/", self.excludes(worktree))

    def test_dry_run_changes_nothing(self):
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would run: cp", result.stdout)
        self.assertFalse(os.path.exists(self.path("build-tool")))
        self.assertFalse(os.path.exists(self.path(".1541ultimate-tools-version")))
        self.assertNotIn("tooling/", self.excludes())

    def test_refuses_what_is_not_a_1541ultimate_checkout(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        git("init", "-q", cwd=plain)
        cases = {
            "missing": (os.path.join(self.tmp, "nowhere"), "is not a directory"),
            "no git": (self.tmp, "is not a git checkout"),
            "wrong layout": (plain, "does not look like a 1541ultimate checkout"),
            "itself": (TOOLS, "CHECKOUT is this repository"),
        }
        for name, (checkout, message) in cases.items():
            with self.subTest(case=name):
                result = self.install(checkout=checkout)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_usage_errors(self):
        result = subprocess.run(["bash", INSTALL], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage: install.sh", result.stderr)
        result = subprocess.run(["bash", INSTALL, "--bogus", self.checkout],
                                capture_output=True, text=True)
        self.assertIn("unknown option --bogus", result.stderr)
        result = subprocess.run(["bash", INSTALL, self.checkout, self.checkout],
                                capture_output=True, text=True)
        self.assertIn("only one checkout", result.stderr)
        result = subprocess.run(["bash", INSTALL, "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
