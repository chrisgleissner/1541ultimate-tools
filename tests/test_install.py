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

    def test_records_the_version_and_the_commit_of_a_clone(self):
        self.assertEqual(self.install().returncode, 0)
        commit = subprocess.run(["git", "-C", TOOLS, "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        with open(self.path(".1541ultimate-tools-version")) as handle:
            self.assertEqual(handle.read().strip(), f"{VERSION}+g{commit}")

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
        for entry in ("/build", "/build-tool", "/build-tool.d/", "/tooling/",
                      "/.1541ultimate-tools-version"):
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
            self.assertTrue(handle.read().startswith(VERSION + "+g"))

    def test_worktree_uses_the_shared_exclude_file(self):
        worktree = os.path.join(self.tmp, "wt")
        git("worktree", "add", "-q", "--detach", worktree, cwd=self.checkout)
        result = self.install(checkout=worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=worktree), "")
        self.assertIn("/tooling/", self.excludes(worktree))

    def test_dry_run_changes_nothing(self):
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would run: cp -f", result.stdout)
        self.assertFalse(os.path.exists(self.path("build-tool")))
        self.assertFalse(os.path.exists(self.path(".1541ultimate-tools-version")))
        self.assertNotIn("/tooling/", self.excludes())

    def test_refuses_what_is_not_a_1541ultimate_checkout(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        git("init", "-q", cwd=plain)
        nested = self.path("software")
        cases = {
            "nested": (nested, "is inside the checkout"),
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

    def test_exclude_file_without_a_final_newline_keeps_its_last_entry(self):
        exclude = git("rev-parse", "--path-format=absolute", "--git-path", "info/exclude",
                      cwd=self.checkout).strip()
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "w") as handle:
            handle.write("# mine\nlocal-notes")                # no newline at the end
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.excludes()
        self.assertEqual(lines[:3], ["# mine", "local-notes", "/build"])

    def test_entries_are_anchored_at_the_top(self):
        os.makedirs(self.path("software", "build"))
        with open(self.path("software", "build", "keep.c"), "w") as handle:
            handle.write("x\n")
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout),
                         "?? software/build/keep.c\n")

    def test_refuses_to_overwrite_a_tracked_tool_path(self):
        os.makedirs(self.path("tooling"))
        with open(self.path("tooling", "apply_pr.sh"), "w") as handle:
            handle.write("tracked upstream\n")
        git("add", "tooling/apply_pr.sh", cwd=self.checkout)
        git("commit", "-qm", "track a tool path", cwd=self.checkout)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tooling/apply_pr.sh", result.stderr)
        self.assertFalse(os.path.exists(self.path("build-tool")))   # nothing copied
        with open(self.path("tooling", "apply_pr.sh")) as handle:
            self.assertEqual(handle.read(), "tracked upstream\n")

    def test_read_only_files_from_an_earlier_install_are_replaced(self):
        self.assertEqual(self.install().returncode, 0)
        target = self.path("tooling", "u64ii_jtag.py")
        os.chmod(target, 0o444)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runs_through_a_symlink(self):
        link = os.path.join(self.tmp, "install-link.sh")
        os.symlink(INSTALL, link)
        result = subprocess.run(["bash", link, self.checkout], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_archive_installs_without_the_logo(self):
        # What GitHub serves for a tag: `git archive` of the commit, with the
        # export-ignore paths left out.
        release = os.path.join(self.tmp, "release")
        os.makedirs(release)
        archive = subprocess.run(["git", "-C", TOOLS, "archive",
                                  f"--prefix=1541ultimate-tools-{VERSION}/", "HEAD"],
                                 capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", release], input=archive, check=True)
        extracted = os.path.join(release, f"1541ultimate-tools-{VERSION}")
        self.assertFalse(os.path.exists(os.path.join(extracted, "docs",
                                                     "1541ultimate-tools-logo.png")))
        result = subprocess.run([os.path.join(extracted, "install.sh"), self.checkout],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.path(".1541ultimate-tools-version")) as handle:
            self.assertEqual(handle.read().strip(), VERSION)        # no commit: a release
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout), "")


if __name__ == "__main__":
    unittest.main()
