#!/usr/bin/env python3
"""Host tests for install.sh against throwaway git repositories; no network.

Each test builds a minimal checkout with the 1541ultimate layout (a commit,
software/ and target/), runs install.sh on it, and checks what a user relies
on: the tools arrive and run, nothing else from this repository does (no
documentation, logo or tests), git sees none of it, a second run is harmless,
and a worktree's checkout works like a plain one.

    python3 -m unittest discover -s tests -p 'test_install.py'
"""

import hashlib
import os
import re
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
                     "tooling/build_and_deploy_u64.sh", "tooling/flash_u64.py", "tooling/u2pl_jtag.py",
                     "tooling/u2pl_jtag_sample.py", "tooling/jtag_verify.py",
                     "tooling/jtag_verify.sh"):
            with self.subTest(path=path):
                self.assertTrue(os.path.isfile(self.path(path)))
        for path in ("build", "build-tool", "tooling/u64ii_jtag.sh", "tooling/flash_u64.py",
                     "tooling/u2pl_jtag.py", "tooling/jtag_verify.py", "tooling/jtag_verify.sh"):
            with self.subTest(executable=path):
                self.assertTrue(os.access(self.path(path), os.X_OK))
        tool = subprocess.run(["./build-tool", "--list-targets"], cwd=self.checkout,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(tool.returncode, 0, tool.stderr)
        self.assertIn("u64ii", tool.stdout)
        jtag = subprocess.run(["tooling/u2pl_jtag.py", "--help"], cwd=self.checkout,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(jtag.returncode, 0, jtag.stderr)
        self.assertIn("--sram", jtag.stdout)
        verifier = subprocess.run(["tooling/jtag_verify.py", "--help"], cwd=self.checkout,
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(verifier.returncode, 0, verifier.stderr)
        self.assertIn("--allow-disruptive", verifier.stdout)
        sample = subprocess.run(["tooling/u2pl_jtag_sample.py", "--help"], cwd=self.checkout,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(sample.returncode, 0, sample.stderr)
        self.assertIn("--refresh", sample.stdout)

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

    # The agent guide: AGENTS.md and CLAUDE.md at the top of the checkout.

    def read(self, *parts):
        with open(self.path(*parts)) as handle:
            return handle.read()

    def write(self, name, text):
        with open(self.path(name), "w") as handle:
            handle.write(text)

    def guide_backups(self, name="AGENTS.md"):
        return sorted(n for n in os.listdir(self.checkout) if n.startswith(name + ".local-"))

    def test_guide_is_installed_excluded_and_recorded(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        agents = self.read("AGENTS.md")
        self.assertIn("1541 Ultimate: agent guide", agents)
        self.assertNotIn("{{TOOLS_REF}}", agents)
        self.assertRegex(agents, r"1541ultimate-tools/blob/(v[0-9.]+|[0-9a-f]+)/docs/")
        self.assertIn("@AGENTS.md", self.read("CLAUDE.md"))
        for name in ("AGENTS.md", "CLAUDE.md"):
            self.assertIn("/" + name, self.excludes())
            self.assertIn("/" + name + ".local-*", self.excludes())
        self.assertIn("/.1541ultimate-tools-guide", self.excludes())
        record = self.read(".1541ultimate-tools-guide")
        digest = hashlib.sha256(agents.encode()).hexdigest()
        self.assertIn(f"{digest}  AGENTS.md", record)
        for name in os.listdir(os.path.join(TOOLS, "docs", "agents")):
            with self.subTest(doc=name):
                self.assertTrue(os.path.isfile(self.path("tooling", "docs", "agents", name)))
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout), "")
        self.assertEqual(self.guide_backups(), [])

    def test_guide_absent_is_installed_and_second_run_is_silent(self):
        self.assertEqual(self.install().returncode, 0)
        before = self.read("AGENTS.md")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("AGENTS.md"), before)
        self.assertEqual(self.guide_backups(), [])
        self.assertEqual(self.guide_backups("CLAUDE.md"), [])

    def test_unchanged_copy_of_an_earlier_install_is_replaced_silently(self):
        self.assertEqual(self.install().returncode, 0)
        # An earlier release wrote this text and recorded its digest.
        old = "older shipped guide\n"
        self.write("AGENTS.md", old)
        with open(self.path(".1541ultimate-tools-guide"), "w") as handle:
            handle.write(f"{hashlib.sha256(old.encode()).hexdigest()}  AGENTS.md\n")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1541 Ultimate: agent guide", self.read("AGENTS.md"))
        self.assertEqual(self.guide_backups(), [])

    def test_edited_or_handwritten_guide_is_moved_aside_never_lost(self):
        self.write("AGENTS.md", "my own guide\n")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = self.guide_backups()
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0], r"^AGENTS\.md\.local-\d{8}-\d{6}$")
        self.assertEqual(self.read(backups[0]), "my own guide\n")
        self.assertIn("1541 Ultimate: agent guide", self.read("AGENTS.md"))
        self.assertIn(backups[0], result.stdout)
        self.assertIn(self.path("AGENTS.md"), result.stdout)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout), "")
        # A local edit of an installed copy is kept the same way on the next install.
        self.write("AGENTS.md", self.read("AGENTS.md") + "local addition\n")
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(len(self.guide_backups()), 2)

    def test_tracked_guide_is_left_alone_with_a_warning(self):
        self.write("AGENTS.md", "tracked upstream\n")
        git("add", "AGENTS.md", cwd=self.checkout)
        git("commit", "-qm", "track AGENTS.md", cwd=self.checkout)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("AGENTS.md"), "tracked upstream\n")
        self.assertIn("tracks AGENTS.md", result.stderr)
        self.assertIn(os.path.join(TOOLS, "checkout", "AGENTS.md"), result.stderr)
        self.assertNotIn("/AGENTS.md", self.excludes())
        self.assertEqual(self.guide_backups(), [])
        self.assertIn("@AGENTS.md", self.read("CLAUDE.md"))      # the other file is unaffected
        self.assertNotIn("  AGENTS.md", self.read(".1541ultimate-tools-guide"))

    def test_dry_run_reports_each_guide_case_and_changes_nothing(self):
        result = self.install("--dry-run")
        self.assertIn("AGENTS.md: absent", result.stdout)
        self.assertFalse(os.path.exists(self.path("AGENTS.md")))
        self.write("AGENTS.md", "mine\n")
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AGENTS.md: differs from the shipped copy; would move it to", result.stdout)
        self.assertEqual(self.read("AGENTS.md"), "mine\n")
        self.assertEqual(self.guide_backups(), [])
        self.assertIn("AGENTS.local.md does not exist", result.stdout)
        self.write("AGENTS.local.md", "notes\n")
        self.assertIn("AGENTS.local.md exists; left as it is", self.install("--dry-run").stdout)
        self.assertNotIn("/AGENTS.local.md", self.excludes())
        self.assertFalse(os.path.exists(self.path(".1541ultimate-tools-guide")))

    def test_local_overlay_is_excluded_and_never_touched(self):
        self.write("AGENTS.local.md", "private notes\n")
        self.assertEqual(self.install().returncode, 0)
        self.assertIn("/AGENTS.local.md", self.excludes())
        self.assertEqual(self.read("AGENTS.local.md"), "private notes\n")
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout), "")
        self.assertEqual(self.guide_backups(), [])

    def test_local_overlay_symlink_is_left_as_it_is(self):
        target = os.path.join(self.tmp, "elsewhere.md")
        with open(target, "w") as handle:
            handle.write("outside the checkout\n")
        os.symlink(target, self.path("AGENTS.local.md"))
        self.assertEqual(self.install().returncode, 0)
        self.assertTrue(os.path.islink(self.path("AGENTS.local.md")))
        self.assertEqual(os.readlink(self.path("AGENTS.local.md")), target)
        with open(target) as handle:
            self.assertEqual(handle.read(), "outside the checkout\n")

    def test_local_overlay_is_never_created(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertFalse(os.path.lexists(self.path("AGENTS.local.md")))

    def test_example_template_has_headings_only(self):
        with open(os.path.join(TOOLS, "docs", "agents", "AGENTS.local.example.md")) as handle:
            text = handle.read()
        self.assertNotRegex(text, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotIn("/home/", text)
        for heading in ("Devices and addresses", "Power control and safety",
                        "Other trees and repositories", "Local build quirks"):
            self.assertIn("## " + heading, text)
        body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        prose = [l for l in body.splitlines()[1:] if l.strip() and not l.startswith("## ")]
        self.assertEqual(len(prose), 1, prose)       # the one introductory paragraph

    # The agent skills: .claude/skills/NAME/ and .agents/skills/NAME/.

    SKILLS = sorted(name for name in os.listdir(os.path.join(TOOLS, "skills"))
                    if os.path.isfile(os.path.join(TOOLS, "skills", name, "SKILL.md")))
    SKILL_ROOTS = (".claude/skills", ".agents/skills")

    def skill_path(self, root, name):
        return self.path(root, name, "SKILL.md")

    def skill_backups(self, root, name):
        return sorted(n for n in os.listdir(self.path(root, name)) if n.startswith("SKILL.md.local-"))

    def test_skills_are_installed_rendered_and_excluded_one_by_one(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(len(self.SKILLS), 6)
        excludes = self.excludes()
        for root in self.SKILL_ROOTS:
            self.assertNotIn("/" + root.split("/")[0] + "/", excludes)      # not all of /.claude/
            for name in self.SKILLS:
                with self.subTest(root=root, skill=name):
                    text = self.read(root, name, "SKILL.md")
                    self.assertTrue(text.startswith(f"---\nname: {name}\n"))
                    self.assertNotIn("{{TOOLS_REF}}", text)
                    self.assertEqual(os.listdir(self.path(root, name)), ["SKILL.md"])
                    self.assertEqual(excludes.count(f"/{root}/{name}/"), 1)
        self.assertRegex(self.read(".claude/skills", "c64u-jtag-run", "SKILL.md"),
                         r"1541ultimate-tools/blob/(v[0-9.]+|[0-9a-f]+)/docs/c64u-jtag\.md")
        self.assertEqual(self.read(".claude/skills", "u2pl-recover", "SKILL.md"),
                         self.read(".agents/skills", "u2pl-recover", "SKILL.md"))
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout), "")
        record = self.read(".1541ultimate-tools-guide")
        text = self.read(".claude/skills", "pr-minimal-diff", "SKILL.md")
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.assertIn(f"{digest}  .claude/skills/pr-minimal-diff/SKILL.md", record)

    def test_other_skills_in_the_same_directories_stay_visible_to_git(self):
        for root in self.SKILL_ROOTS:
            os.makedirs(self.path(root, "mine"))
            with open(self.path(root, "mine", "SKILL.md"), "w") as handle:
                handle.write("my skill\n")
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout),
                         "?? .agents/skills/mine/SKILL.md\n?? .claude/skills/mine/SKILL.md\n")
        self.assertEqual(self.read(".claude/skills", "mine", "SKILL.md"), "my skill\n")

    def test_second_run_skills_are_idempotent(self):
        self.assertEqual(self.install().returncode, 0)
        before = {(root, name): self.read(root, name, "SKILL.md")
                  for root in self.SKILL_ROOTS for name in self.SKILLS}
        excludes = self.excludes()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("added 0 entries", result.stdout)
        self.assertIn("already the shipped copy", result.stdout)
        self.assertEqual(self.excludes(), excludes)
        for (root, name), text in before.items():
            with self.subTest(root=root, skill=name):
                self.assertEqual(self.read(root, name, "SKILL.md"), text)
                self.assertEqual(self.skill_backups(root, name), [])

    def test_unchanged_skill_of_an_earlier_install_is_replaced_silently(self):
        self.assertEqual(self.install().returncode, 0)
        old = "---\nname: u2pl-recover\n---\nolder shipped skill\n"
        target = self.skill_path(".claude/skills", "u2pl-recover")
        with open(target, "w") as handle:
            handle.write(old)
        record = self.path(".1541ultimate-tools-guide")
        kept = [line for line in self.read(".1541ultimate-tools-guide").splitlines()
                if not line.endswith("  .claude/skills/u2pl-recover/SKILL.md")]
        with open(record, "w") as handle:
            handle.write("".join(line + "\n" for line in kept))
            handle.write(f"{hashlib.sha256(old.encode()).hexdigest()}  .claude/skills/u2pl-recover/SKILL.md\n")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("older shipped skill", self.read(".claude/skills", "u2pl-recover", "SKILL.md"))
        self.assertEqual(self.skill_backups(".claude/skills", "u2pl-recover"), [])

    def test_edited_skill_is_moved_aside_never_lost(self):
        self.assertEqual(self.install().returncode, 0)
        target = self.skill_path(".agents/skills", "vhdl-analysis")
        shipped = self.read(".agents/skills", "vhdl-analysis", "SKILL.md")
        with open(target, "w") as handle:
            handle.write(shipped + "my local rule\n")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = self.skill_backups(".agents/skills", "vhdl-analysis")
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0], r"^SKILL\.md\.local-\d{8}-\d{6}$")
        self.assertEqual(self.read(".agents/skills", "vhdl-analysis", backups[0]), shipped + "my local rule\n")
        self.assertEqual(self.read(".agents/skills", "vhdl-analysis", "SKILL.md"), shipped)
        self.assertIn(backups[0], result.stdout)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout), "")

    def test_a_tracked_skill_path_stops_the_install_before_anything_is_copied(self):
        os.makedirs(self.path(".claude", "skills", "pr-minimal-diff"))
        with open(self.skill_path(".claude/skills", "pr-minimal-diff"), "w") as handle:
            handle.write("tracked upstream\n")
        git("add", ".claude", cwd=self.checkout)
        git("commit", "-qm", "track a skill", cwd=self.checkout)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".claude/skills/pr-minimal-diff/SKILL.md", result.stderr)
        self.assertFalse(os.path.exists(self.path("build-tool")))
        self.assertFalse(os.path.exists(self.path(".agents")))
        self.assertEqual(self.read(".claude/skills", "pr-minimal-diff", "SKILL.md"), "tracked upstream\n")
        self.assertNotIn("/.claude/skills/pr-minimal-diff/", self.excludes())

    def test_tracked_files_elsewhere_in_the_skill_directories_do_not_block_the_install(self):
        os.makedirs(self.path(".claude", "skills", "upstream-skill"))
        with open(self.path(".claude", "skills", "upstream-skill", "SKILL.md"), "w") as handle:
            handle.write("tracked upstream\n")
        with open(self.path(".claude", "settings.json"), "w") as handle:
            handle.write("{}\n")
        git("add", ".claude", cwd=self.checkout)
        git("commit", "-qm", "track other claude files", cwd=self.checkout)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout), "")
        self.assertEqual(self.read(".claude", "skills", "upstream-skill", "SKILL.md"), "tracked upstream\n")

    def test_dry_run_reports_each_skill_case_and_writes_nothing(self):
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".claude/skills/pr-minimal-diff/SKILL.md: absent, installing it", result.stdout)
        self.assertIn(".agents/skills/pr-minimal-diff/SKILL.md: absent, installing it", result.stdout)
        self.assertIn("/.claude/skills/pr-minimal-diff/", result.stdout)          # the entries it would add
        self.assertFalse(os.path.exists(self.path(".claude")))
        self.assertFalse(os.path.exists(self.path(".agents")))
        os.makedirs(self.path(".claude", "skills", "pr-minimal-diff"))
        with open(self.skill_path(".claude/skills", "pr-minimal-diff"), "w") as handle:
            handle.write("mine\n")
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".claude/skills/pr-minimal-diff/SKILL.md: differs from the shipped copy; would move it to",
                      result.stdout)
        self.assertEqual(self.read(".claude/skills", "pr-minimal-diff", "SKILL.md"), "mine\n")
        self.assertEqual(self.skill_backups(".claude/skills", "pr-minimal-diff"), [])
        self.assertFalse(os.path.exists(self.path(".1541ultimate-tools-guide")))

    def test_worktree_gets_the_skills_through_the_shared_exclude_file(self):
        worktree = os.path.join(self.tmp, "wt")
        git("worktree", "add", "-q", "--detach", worktree, cwd=self.checkout)
        result = self.install(checkout=worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(worktree, ".claude", "skills", "c64u-jtag-run", "SKILL.md")))
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=worktree), "")

    def test_dry_run_changes_nothing(self):
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would run: cp -f", result.stdout)
        self.assertFalse(os.path.exists(self.path("build-tool")))
        self.assertFalse(os.path.exists(self.path(".1541ultimate-tools-version")))
        self.assertNotIn("/tooling/", self.excludes())

    # tooling/read_u64_jtag_terminal.sh as releases 0.1.0 to 0.2.4 shipped it.
    RETIRED = "tooling/read_u64_jtag_terminal.sh"
    RETIRED_COPY = os.path.join(TOOLS, "tests", "fixtures", "read_u64_jtag_terminal.sh")

    def place_retired(self, content=None):
        os.makedirs(self.path("tooling"), exist_ok=True)
        if content is None:
            shutil.copy(self.RETIRED_COPY, self.path(self.RETIRED))
        else:
            with open(self.path(self.RETIRED), "w") as handle:
                handle.write(content)

    def test_retired_list_names_no_file_that_is_still_shipped(self):
        with open(INSTALL) as handle:
            text = handle.read()
        listed = re.findall(r'^    "(\S+) [0-9a-f]{64}', text, re.M)
        self.assertIn(self.RETIRED, listed)
        for name in listed:
            self.assertFalse(os.path.exists(os.path.join(TOOLS, name)), name)

    def test_untracked_retired_tool_of_a_known_version_is_removed_and_reported(self):
        self.place_retired()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"removed retired tool {self.RETIRED}", result.stdout)
        self.assertFalse(os.path.exists(self.path(self.RETIRED)))
        self.assertTrue(os.path.exists(self.path("tooling", "read_u64ii_jtag_terminal.sh")))
        again = self.install()
        self.assertNotIn("retired", again.stdout)

    def test_dry_run_reports_the_retired_tool_and_keeps_it(self):
        self.place_retired()
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"would remove retired tool {self.RETIRED}", result.stdout)
        self.assertTrue(os.path.exists(self.path(self.RETIRED)))

    def test_retired_tool_with_other_content_is_left_alone(self):
        self.place_retired("echo mine\n")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"retired tool {self.RETIRED} differs from every version", result.stdout)
        with open(self.path(self.RETIRED)) as handle:
            self.assertEqual(handle.read(), "echo mine\n")

    def test_tracked_retired_tool_is_left_alone(self):
        self.place_retired()
        git("add", "-f", self.RETIRED, cwd=self.checkout)
        git("commit", "-qm", "track it", cwd=self.checkout)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"retired tool {self.RETIRED} is tracked", result.stdout)
        self.assertTrue(os.path.exists(self.path(self.RETIRED)))

    def test_retired_tool_that_is_a_symlink_or_directory_is_left_alone(self):
        os.makedirs(self.path("tooling"))
        os.symlink(self.RETIRED_COPY, self.path(self.RETIRED))
        result = self.install()
        self.assertIn(f"retired tool {self.RETIRED} is not a regular file", result.stdout)
        self.assertTrue(os.path.islink(self.path(self.RETIRED)))
        os.remove(self.path(self.RETIRED))
        os.makedirs(self.path(self.RETIRED))
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is not a regular file", result.stdout)
        self.assertTrue(os.path.isdir(self.path(self.RETIRED)))

    def test_retired_tool_is_not_reported_when_absent(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("retired", result.stdout)

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
