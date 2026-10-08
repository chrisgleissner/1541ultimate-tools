#!/usr/bin/env python3
"""Host tests for auth_token.py against a fake xsetup. Nothing contacts AMD.

Run: python3 vivado/test_auth_token.py
"""
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "auth_token.py")
EMAIL = "tester@example.invalid"
PASSWORD = "s3cret#pass word"

# Behaves like the AMD client: prompts on the terminal, reads the password
# without echo, and writes a mode 0400 token. BEHAVIOUR selects a rejection
# (asks again) or a silent failure (writes nothing).
FAKE_XSETUP = textwrap.dedent("""\
    #!/usr/bin/env python3
    import getpass, os, sys
    behaviour = os.environ.get("BEHAVIOUR", "ok")
    token = os.path.expanduser("~/.Xilinx/wi_authentication_key")
    if behaviour == "hang":
        import time
        time.sleep(60)
    while True:
        email = input("E-mail Address:")
        password = getpass.getpass("Password:")
        if behaviour == "repass":
            password = getpass.getpass("Password:")
        if behaviour == "reject":
            print("ERROR - Invalid credentials")
            continue
        break
    if behaviour == "nowrite":
        sys.exit(1)
    with open(os.path.expanduser("~/fake-received"), "w") as f:
        f.write(email + "\\n" + password + "\\n")
    os.makedirs(os.path.dirname(token), exist_ok=True)
    # Rewrites the file in place, the case a leftover mode 0400 would break.
    with open(token, "w") as f:
        f.write("TOKEN")
    os.chmod(token, 0o400)
    print("INFO  - Saved authentication token file successfully")
    """)


class AuthTokenTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.xsetup = os.path.join(self.home, "xsetup")
        with open(self.xsetup, "w") as f:
            f.write(FAKE_XSETUP)
        os.chmod(self.xsetup, 0o755)
        self.token = os.path.join(self.home, ".Xilinx", "wi_authentication_key")
        self.env_file = os.path.join(self.home, ".env")

    def write_env(self, text):
        with open(self.env_file, "w") as f:
            f.write(text)

    def write_token(self, age_s):
        os.makedirs(os.path.dirname(self.token), exist_ok=True)
        with open(self.token, "w") as f:
            f.write("OLD")
        os.chmod(self.token, 0o400)
        t = time.time() - age_s
        os.utime(self.token, (t, t))

    def run_tool(self, *args, extra_env=None):
        env = {"HOME": self.home, "PATH": os.environ["PATH"]}
        # Lets coverage follow the tool into its subprocess; unset otherwise.
        env.update({k: v for k, v in os.environ.items() if k.startswith("COVERAGE_")})
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, SCRIPT, "--xsetup", self.xsetup,
             "--env-file", self.env_file, "--timeout", "20", *args],
            env=env, capture_output=True, text=True, timeout=60)

    def received(self):
        with open(os.path.join(self.home, "fake-received")) as f:
            return f.read().splitlines()

    def test_fresh_token_is_left_alone(self):
        self.write_token(age_s=3600)
        r = self.run_tool("--ensure")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("more hours", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, "fake-received")))

    def test_stale_read_only_token_is_renewed(self):
        self.write_env(f"AMD_EMAIL={EMAIL}\nAMD_PASSWORD='{PASSWORD}'\n")
        self.write_token(age_s=6.5 * 24 * 3600)
        r = self.run_tool("--ensure")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.received(), [EMAIL, PASSWORD])
        with open(self.token) as f:
            self.assertEqual(f.read(), "TOKEN")

    def test_missing_token_is_created(self):
        self.write_env(f"export AMD_EMAIL={EMAIL}\nexport AMD_PASSWORD=\"{PASSWORD}\"\n")
        r = self.run_tool("--ensure")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.token))

    def test_credentials_never_reach_the_output(self):
        self.write_env(f"AMD_EMAIL={EMAIL}\nAMD_PASSWORD='{PASSWORD}'\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for secret in (EMAIL, PASSWORD, "s3cret"):
            self.assertNotIn(secret, r.stdout + r.stderr)

    def test_environment_overrides_the_file(self):
        self.write_env("AMD_EMAIL=file@example.invalid\nAMD_PASSWORD=filepass\n")
        r = self.run_tool(extra_env={"AMD_PASSWORD": "envpass"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.received(), ["file@example.invalid", "envpass"])

    def test_inline_comment_is_not_part_of_an_unquoted_value(self):
        self.write_env(f"AMD_EMAIL={EMAIL}  # work account\nAMD_PASSWORD=pw#1\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.received(), [EMAIL, "pw#1"])

    def test_missing_credentials_are_named(self):
        self.write_env("AMD_PASSWORD=x\n")
        r = self.run_tool()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("AMD_EMAIL not set", r.stderr)

    def test_rejected_login_fails_fast(self):
        self.write_env(f"AMD_EMAIL={EMAIL}\nAMD_PASSWORD=wrong\n")
        start = time.time()
        r = self.run_tool(extra_env={"BEHAVIOUR": "reject"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rejected", r.stderr)
        self.assertLess(time.time() - start, 10)

    def test_no_new_token_is_an_error(self):
        self.write_env(f"AMD_EMAIL={EMAIL}\nAMD_PASSWORD=x\n")
        self.write_token(age_s=8 * 24 * 3600)
        r = self.run_tool("--ensure", extra_env={"BEHAVIOUR": "nowrite"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no new token", r.stderr)

    def test_password_asked_again_is_a_rejection(self):
        self.write_env(f"AMD_EMAIL={EMAIL}\nAMD_PASSWORD=wrong\n")
        r = self.run_tool(extra_env={"BEHAVIOUR": "repass"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rejected", r.stderr)
        self.assertNotIn("wrong", r.stdout + r.stderr)

    def test_silent_installer_times_out(self):
        self.write_env(f"AMD_EMAIL={EMAIL}\nAMD_PASSWORD=x\n")
        start = time.time()
        env = {"HOME": self.home, "PATH": os.environ["PATH"], "BEHAVIOUR": "hang"}
        env.update({k: v for k, v in os.environ.items() if k.startswith("COVERAGE_")})
        r = subprocess.run(
            [sys.executable, SCRIPT, "--xsetup", self.xsetup, "--env-file", self.env_file,
             "--timeout", "2"], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("timed out after 2 s", r.stderr)
        self.assertLess(time.time() - start, 30)

    def test_missing_installer_is_named(self):
        self.write_env(f"AMD_EMAIL={EMAIL}\nAMD_PASSWORD=x\n")
        os.remove(self.xsetup)
        r = self.run_tool()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not found; extract the installer first", r.stderr)

    def test_empty_token_counts_as_missing(self):
        self.write_env(f"AMD_EMAIL={EMAIL}\nAMD_PASSWORD=x\n")
        self.write_token(age_s=60)
        os.chmod(self.token, 0o600)
        open(self.token, "w").close()
        r = self.run_tool("--ensure")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.received(), [EMAIL, "x"])

    def test_comments_and_blank_lines_in_the_env_file(self):
        self.write_env(f"# AMD account\n\n  \nnot an assignment\nAMD_EMAIL={EMAIL}\nAMD_PASSWORD=x\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.received(), [EMAIL, "x"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
