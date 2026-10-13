"""Execute the release workflow's Bash against a mocked GitHub CLI; no network."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"
TEXT = WORKFLOW.read_text()
SCRIPT = textwrap.dedent(TEXT.split("        run: |\n", 1)[1])


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "calls.jsonl"
        gh = self.root / "gh"
        gh.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
            import json, os, sys
            args = sys.argv[1:]
            with open(os.environ["GH_CALL_LOG"], "a") as handle:
                handle.write(json.dumps(args) + "\\n")
            if args[:2] == ["release", "view"]:
                sys.exit(0 if os.environ["RELEASE_EXISTS"] == "1" else 1)
            if args[:2] == ["release", "create"]:
                sys.exit(int(os.environ["CREATE_STATUS"]))
            sys.exit(95)
        '''))
        gh.chmod(0o755)

    def run_release(self, tag="v0.2.2", *, exists=False, create_status=0):
        env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}",
                   GH_CALL_LOG=str(self.log), RELEASE_EXISTS=str(int(exists)),
                   CREATE_STATUS=str(create_status), RELEASE_TAG=tag,
                   GITHUB_REPOSITORY="owner/repo", GH_TOKEN="mock-token")
        result = subprocess.run(["bash", "-c", SCRIPT], env=env, text=True,
                                capture_output=True, timeout=10)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_stable_tag_publishes_verified_tag_with_generated_notes(self):
        result, calls = self.run_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            ["release", "view", "v0.2.2", "--repo", "owner/repo"],
            ["release", "create", "v0.2.2", "--repo", "owner/repo", "--verify-tag",
             "--title", "1541ultimate-tools 0.2.2", "--generate-notes"],
        ])

    def test_prerelease_is_not_marked_latest(self):
        result, calls = self.run_release("v0.3.0-rc.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-1][-2:], ["--prerelease", "--latest=false"])

    def test_existing_release_is_not_modified(self):
        result, calls = self.run_release("v0.2.1", exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("leaving it unchanged", result.stdout)

    def test_publication_errors_fail_the_job(self):
        result, _ = self.run_release(create_status=5)
        self.assertEqual(result.returncode, 5)

    def test_invalid_tags_never_reach_github(self):
        for tag in ("", "main", "0.2.2", "v0.2", "v0.2.2; echo bad", "v0.2.2$(echo bad)"):
            with self.subTest(tag=tag):
                result, calls = self.run_release(tag)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(calls, [])

    def test_workflow_wiring(self):
        self.assertIn("push:\n    tags: ['v*']", TEXT)
        self.assertIn("workflow_dispatch:", TEXT)
        self.assertIn("contents: write", TEXT)
        self.assertIn("cancel-in-progress: false", TEXT)
        self.assertIn("GH_TOKEN: ${{ github.token }}", TEXT)
        self.assertIn("RELEASE_TAG: ${{ inputs.tag || github.ref_name }}", TEXT)
        self.assertNotIn("${{", SCRIPT)  # tag input is data, never interpolated shell code


if __name__ == "__main__":
    unittest.main()
