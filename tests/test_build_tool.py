"""Exit codes and side effects of build-tool, ./build and the UART monitor script.

Nothing here touches a device or a real Docker daemon. A fake `docker` and a fake
`curl` sit first on PATH and record their command lines in a log file; the fake
`docker run` creates the package file a real build would leave in the checkout.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BUILD_TOOL = os.path.join(ROOT, "build-tool")
BUILD = os.path.join(ROOT, "build")
UART = os.path.join(ROOT, "tooling", "read_u64_uart_terminal.sh")

FAKE_DOCKER = """#!/bin/bash
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
    info) [ "${FAKE_DOCKER_DOWN:-0}" = 1 ] && exit 1; exit 0 ;;
    run)  if [ -n "${FAKE_ARTIFACT:-}" ]; then
              head -c "$FAKE_ARTIFACT_SIZE" /dev/zero > "$FAKE_ARTIFACT"
          fi
          exit 0 ;;
    *)    exit 0 ;;
esac
"""

FAKE_CURL = """#!/bin/bash
echo "curl $*" >> "$FAKE_LOG"
case " $* " in
    *" -T "*) if [ "${FAKE_CURL_FAIL:-0}" = 1 ]; then
                  echo "curl: (7) Failed to connect (fake)" >&2
                  exit 7
              fi ;;
esac
exit 0
"""

# Stands in for build-tool inside the ./build tests: it leaves the package a
# build would, or fails an FTP upload on request.
FAKE_BUILD_TOOL = """#!/bin/bash
echo "$*" >> "$FAKE_LOG"
case " $* " in
    *" --deploy-only "*)
        if [ "${FAKE_FTP_FAIL:-0}" = 1 ]; then
            echo "ERROR FTP upload failed (fake)"
            exit 2
        fi
        exit 0 ;;
esac
for last in "$@"; do :; done
case "$last" in
    u64)   ext=u64; size=${FAKE_U64_SIZE:-5400000} ;;
    u64ii) ext=ue2; size=${FAKE_UE2_SIZE:-8990000} ;;
    u2)    ext=u2r; size=${FAKE_U2_SIZE:-1750000} ;;
    *)     echo "fake build-tool: unexpected target $last" >&2; exit 9 ;;
esac
head -c "$size" /dev/zero > "update_deadbeef.$ext"
"""

FAKE_JTAG = """#!/bin/bash
echo "jtag deploy" >> "$FAKE_LOG"
exit "${FAKE_JTAG_STATUS:-0}"
"""


def write_executable(path, text):
    with open(path, "w") as handle:
        handle.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_repo(path):
    os.makedirs(os.path.join(path, "target"))
    with open(os.path.join(path, "README"), "w") as handle:
        handle.write("firmware\n")
    git(path, "init", "-q")
    git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
    return git(path, "rev-parse", "--short=8", "HEAD")


def snapshot(path):
    """Every file and directory below path except .git, with size and mtime."""
    state = {}
    for base, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in dirs + files:
            full = os.path.join(base, name)
            info = os.lstat(full)
            state[os.path.relpath(full, path)] = (info.st_size, info.st_mtime_ns)
    return state


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="build_tool_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        write_executable(os.path.join(self.bin, "docker"), FAKE_DOCKER)
        write_executable(os.path.join(self.bin, "curl"), FAKE_CURL)
        self.log = os.path.join(self.tmp, "fake.log")
        open(self.log, "w").close()
        self.repo = os.path.join(self.tmp, "firmware")
        self.head = make_repo(self.repo)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("BUILD_TOOL", "FAKE_", "U64", "U2_FLASH", "DEPLOY_", "DOCKER", "DEVICE_LOCK"))}
        env.update(PATH=self.bin + os.pathsep + os.environ["PATH"], HOME=self.tmp,
                   NO_COLOR="1", FAKE_LOG=self.log)
        env.update(extra)
        return env

    def fake_log(self):
        with open(self.log) as handle:
            return handle.read()

    def put(self, name, size=1000):
        path = os.path.join(self.repo, name)
        with open(path, "wb") as handle:
            handle.write(b"\0" * size)
        return path


class BuildToolDeploy(Sandbox):
    def run_tool(self, *args, **env):
        return subprocess.run([BUILD_TOOL, "--repo-dir", self.repo, *args], cwd=self.tmp,
                              env=self.env(**env), capture_output=True, text=True, timeout=60)

    def test_ftp_upload_failure_exits_nonzero(self):
        self.put(f"update_{self.head}.u64")
        result = self.run_tool("--deploy-only", "-d", "u64", "--deploy-host", "127.0.0.1",
                               FAKE_CURL_FAIL="1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FTP upload failed", result.stderr)
        self.assertNotIn("Completed successfully", result.stdout)

    def test_upload_keeps_the_local_name_and_needs_no_docker(self):
        self.put(f"update_{self.head}.u64")
        result = self.run_tool("--deploy-only", "-d", "u64", "--deploy-host", "127.0.0.1",
                               FAKE_DOCKER_DOWN="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.fake_log()
        self.assertIn(f"-T update_{self.head}.u64 ftp://127.0.0.1/Usb1/firmware/u64/custom/"
                      f"update_{self.head}.u64", log)
        self.assertNotIn("docker", log)

    def test_artifact_of_another_commit_is_refused(self):
        self.put("update_deadbeef.u64")
        result = self.run_tool("--deploy-only", "-d", "u64", "--deploy-host", "127.0.0.1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("update_deadbeef.u64", result.stderr)
        self.assertIn("--deploy-file", result.stderr)
        self.assertNotIn(" -T ", self.fake_log())

    def test_explicit_file_is_uploaded_under_its_own_name(self):
        path = self.put("update_deadbeef.u64")
        result = self.run_tool("--deploy-only", "-d", "u64", "--deploy-file", path,
                               "--deploy-host", "127.0.0.1", "--deploy-path", "/Temp/")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"-T {path} ftp://127.0.0.1/Temp/update_deadbeef.u64", self.fake_log())

    def test_deploy_file_needs_deploy_only(self):
        path = self.put("update_deadbeef.u64")
        result = self.run_tool("--deploy-file", path, "-d", "u64", "--deploy-host", "127.0.0.1")
        self.assertEqual(result.returncode, 3)
        self.assertIn("--deploy-only", result.stderr)


class BuildToolU2Flash(Sandbox):
    """--u2-flash hands the rest of the command line to tooling/u2_flash.py; nothing here reaches a device."""

    def run_tool(self, *args, **env):
        env.setdefault("DEVICE_LOCK_DIR", os.path.join(self.tmp, "locks"))
        return subprocess.run([BUILD_TOOL, "--repo-dir", self.repo, *args], cwd=self.tmp,
                              env=self.env(**env), capture_output=True, text=True, timeout=60)

    def test_help_describes_the_options_the_script_has(self):
        result = self.run_tool("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("tooling/u2_flash.py", result.stdout)
        self.assertNotIn("tools/api", result.stdout)
        self.assertNotIn("--power-host", result.stdout)
        script_help = self.run_tool("--u2-flash", "--help").stdout
        text = result.stdout
        section = text[text.index("U2+L REST FLASH"):text.index("NOTES")]
        examples = [line for line in text.splitlines() if "build-tool --u2-flash" in line or " --u2-flash " in line]
        options = set(re.findall(r"--[a-z0-9-]+", section + " ".join(examples))) - {"--u2-flash", "--help"}
        self.assertGreaterEqual(len(options), 6)
        for option in options:
            self.assertIn(option, script_help, option)

    def test_the_script_is_found_and_its_own_help_is_shown(self):
        result = self.run_tool("--u2-flash", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: u2_flash.py", result.stdout)
        self.assertNotIn("--power-host", result.stdout)

    def test_a_dry_run_names_the_artifact_branch_and_commit_of_the_checkout(self):
        artifact = self.put(f"update_{self.head}.u2l", 300 * 1024)
        branch = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        head = git(self.repo, "rev-parse", "--short", "HEAD")
        result = self.run_tool("--u2-flash", "--dry-run", "--date", "251107")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Local artifact OK: {artifact} ", result.stdout)
        self.assertIn(f"/USB1/Firmware/latest/{branch}/251107/{head}/update.u2l", result.stdout)

    def test_options_after_the_flag_override_the_defaults_and_a_global_dry_run_is_passed_on(self):
        self.put(f"update_{self.head}.u2l", 300 * 1024)
        result = self.run_tool("--dry-run", "--u2-flash", "--branch", "topic", "--git-id", "id9",
                               "--date", "251107", "--dest-root", "/USB0/fw")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Remote destination: /USB0/fw/topic/251107/id9/update.u2l", result.stdout)
        self.assertIn("DRY_RUN_OK", result.stdout)

    def test_a_missing_host_is_a_usage_error_and_the_environment_supplies_one(self):
        self.put(f"update_{self.head}.u2l", 300 * 1024)
        result = self.run_tool("--u2-flash", "--upload-only")
        self.assertEqual(result.returncode, 3)
        self.assertIn("--host (or U2_FLASH_HOST) is required", result.stderr)
        result = self.run_tool("--u2-flash", "--upload-only", U2_FLASH_HOST="127.0.0.1:1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("upload failed: FTP error", result.stdout)
        with open(os.path.join(self.tmp, "locks", "c64u.lock")) as handle:
            self.assertEqual(handle.read(), "")


class BuildToolBuild(Sandbox):
    def run_tool(self, *args, **env):
        return subprocess.run([BUILD_TOOL, "--repo-dir", self.repo, "-i", "fake-image",
                               "--no-submodule-update", *args], cwd=self.tmp,
                              env=self.env(BUILD_TOOL_ALLOW_PARTIAL="1", **env),
                              capture_output=True, text=True, timeout=60)

    def test_failed_artifact_check_stops_the_deploy(self):
        result = self.run_tool("u64ii", "-d", "u64ii", "--deploy-host", "127.0.0.1",
                               FAKE_ARTIFACT="update.ue2", FAKE_ARTIFACT_SIZE="100")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("too small", result.stderr)
        self.assertNotIn(" -T ", self.fake_log())
        self.assertNotIn("Completed successfully", result.stdout)

    def test_good_artifact_is_deployed_under_the_commit_name(self):
        result = self.run_tool("u64ii", "-d", "u64ii", "--deploy-host", "127.0.0.1",
                               FAKE_ARTIFACT="update.ue2", FAKE_ARTIFACT_SIZE=str(600 * 1024))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"-T update_{self.head}.ue2 ftp://127.0.0.1/USB1/Firmware/custom/"
                      f"update_{self.head}.ue2", self.fake_log())

    def test_failed_upload_after_a_build_exits_nonzero(self):
        result = self.run_tool("u64ii", "-d", "u64ii", "--deploy-host", "127.0.0.1",
                               FAKE_ARTIFACT="update.ue2", FAKE_ARTIFACT_SIZE=str(600 * 1024),
                               FAKE_CURL_FAIL="1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_docker_daemon_down_is_reported(self):
        result = self.run_tool("u64ii", FAKE_DOCKER_DOWN="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("daemon is not reachable", result.stderr)
        self.assertNotIn("USAGE", result.stdout + result.stderr)

    def test_partial_target_error_names_the_override(self):
        env = self.env()
        result = subprocess.run([BUILD_TOOL, "--repo-dir", self.repo, "-i", "fake-image", "u64ii"],
                                cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BUILD_TOOL_ALLOW_PARTIAL=1", result.stderr)
        self.assertNotIn("USAGE", result.stdout + result.stderr)

    def test_dry_run_writes_nothing_and_says_would_build(self):
        before = snapshot(self.tmp)
        status = git(self.repo, "status", "--porcelain")
        result = self.run_tool("--dry-run", "u64ii", "-d", "u64ii", "--deploy-host", "127.0.0.1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), status)
        self.assertIn("Would build u64ii", result.stdout)
        self.assertNotIn("Built ", result.stdout)
        self.assertNotIn("Completed successfully", result.stdout)
        self.assertNotIn("ERROR", result.stderr)
        self.assertEqual(self.fake_log(), "")

    def test_dry_run_with_the_default_image_does_not_touch_docker(self):
        before = snapshot(self.repo)
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools)
        result = subprocess.run([BUILD_TOOL, "--repo-dir", self.repo, "--dry-run",
                                 "--no-submodule-update", "--parallel",
                                 "--build-tools-dir", tools], cwd=self.tmp,
                                env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.fake_log(), "")
        self.assertIn("Would build u64 ", result.stdout)

    def test_dry_run_jtag_is_not_reported_as_a_failed_check(self):
        before = snapshot(self.repo)
        result = self.run_tool("--dry-run", "--jtag", "u64ii")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(snapshot(self.repo), before)
        self.assertNotIn("ERROR", result.stderr)
        self.assertIn("Would build the u64ii JTAG application", result.stdout)

    def test_jtag_monitor_of_the_ultimate_64_is_rejected(self):
        result = self.run_tool("--jtag-monitor", "u64")
        self.assertEqual(result.returncode, 3)
        self.assertIn("no JTAG UART", result.stderr)

    def test_removed_options_are_unknown(self):
        for option in ("--install-helpers", "--u64-core"):
            with self.subTest(option=option):
                result = self.run_tool(option)
                self.assertEqual(result.returncode, 3)
                self.assertIn(f"Unknown option: {option}", result.stderr)

    def test_nothing_is_mounted_without_a_tools_directory(self):
        result = self.run_tool("u64ii", FAKE_ARTIFACT="update.ue2",
                               FAKE_ARTIFACT_SIZE=str(600 * 1024))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("/mnt/build-tools", self.fake_log())
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools)
        result = self.run_tool("u64ii", FAKE_ARTIFACT="update.ue2",
                               FAKE_ARTIFACT_SIZE=str(600 * 1024), BUILD_TOOL_TOOLS_DIR=tools)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"-v {tools}:/mnt/build-tools:ro", self.fake_log())
        self.assertNotIn(f"-v {self.tmp}:", self.fake_log())


class BuildScript(Sandbox):
    """./build against a fake build-tool in a scratch directory."""

    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(self.work, "target"))
        shutil.copy(BUILD, os.path.join(self.work, "build"))
        write_executable(os.path.join(self.work, "build-tool"), FAKE_BUILD_TOOL)
        os.makedirs(os.path.join(self.work, "tooling"))
        write_executable(os.path.join(self.work, "tooling", "build_and_deploy_u64.sh"), FAKE_JTAG)
        subprocess.run(["git", "init", "-q", self.work], check=True)

    def run_build(self, *args, **env):
        return subprocess.run(["bash", os.path.join(self.work, "build"), *args], cwd=self.work,
                              env=self.env(**env), capture_output=True, text=True, timeout=120)

    def test_a_9_mb_ue2_package_passes(self):
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STATUS: SUCCESS", result.stdout)

    def test_an_oversize_package_fails(self):
        result = self.run_build(FAKE_UE2_SIZE=str(40 * 1048576))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sanity limit", result.stderr)

    def test_an_empty_package_fails(self):
        result = self.run_build(FAKE_UE2_SIZE="0")
        self.assertNotEqual(result.returncode, 0)

    def test_a_failed_jtag_deploy_is_not_converged_and_exits_nonzero(self):
        result = self.run_build(FAKE_JTAG_STATUS="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("NON-CONVERGED", result.stderr)

    def test_a_failed_ftp_upload_is_not_reported_as_success(self):
        result = self.run_build(DEPLOY_HOST="127.0.0.1", FAKE_FTP_FAIL="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("FTP deploy succeeded", result.stdout)
        self.assertIn("NON-CONVERGED", result.stderr)

    def test_the_deploy_path_is_passed_only_when_set(self):
        self.assertEqual(self.run_build(DEPLOY_HOST="127.0.0.1").returncode, 0)
        deploy = [line for line in self.fake_log().splitlines() if "--deploy-only" in line]
        self.assertEqual(len(deploy), 1)
        self.assertNotIn("--deploy-path", deploy[0])
        open(self.log, "w").close()
        self.assertEqual(self.run_build(DEPLOY_HOST="127.0.0.1", DEPLOY_PATH="/X/").returncode, 0)
        deploy = [line for line in self.fake_log().splitlines() if "--deploy-only" in line]
        self.assertIn("--deploy-path /X/", deploy[0])


class UartAutoDetect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uart_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dev = os.path.join(self.tmp, "dev")
        self.sys = os.path.join(self.tmp, "sys")
        os.makedirs(self.dev)
        os.makedirs(self.sys)

    def add_tty(self, name, vid, pid):
        open(os.path.join(self.dev, name), "w").close()
        usb = os.path.join(self.sys, name, "device")
        os.makedirs(usb)
        for field, value in (("idVendor", vid), ("idProduct", pid)):
            with open(os.path.join(usb, field), "w") as handle:
                handle.write(value + "\n")

    def run_script(self, *args):
        env = dict(os.environ, U64_UART_DEV_DIR=self.dev, U64_UART_SYS_TTY_DIR=self.sys)
        return subprocess.run(["bash", UART, "--secs", "1", *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_an_ftdi_jtag_adapter_is_not_picked(self):
        self.add_tty("ttyUSB0", "0403", "6014")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No known USB-TTL adapter", result.stderr)
        self.assertIn("ttyUSB0 (0403:6014)", result.stderr)

    def test_a_usb_blaster_and_a_dual_channel_ftdi_are_not_picked(self):
        self.add_tty("ttyUSB0", "09fb", "6001")
        self.add_tty("ttyUSB1", "0403", "6010")
        result = self.run_script()
        self.assertIn("No known USB-TTL adapter", result.stderr)

    def test_the_usb_ttl_adapter_is_chosen_next_to_a_jtag_adapter(self):
        self.add_tty("ttyUSB0", "0403", "6014")
        self.add_tty("ttyUSB1", "10C4", "EA60")
        result = self.run_script()
        # The scratch file is not a serial port, so the run stops at stty, but
        # only after it picked the CP210x.
        self.assertIn(f"Failed to read serial settings from {self.dev}/ttyUSB1", result.stderr)
        self.assertIn("ttyUSB0 (0403:6014)", result.stderr)

    def test_two_usb_ttl_adapters_need_a_port(self):
        self.add_tty("ttyUSB0", "10c4", "ea60")
        self.add_tty("ttyUSB1", "1a86", "7523")
        result = self.run_script()
        self.assertIn("Pass --port", result.stderr)

    def test_an_unidentifiable_device_is_not_picked(self):
        open(os.path.join(self.dev, "ttyUSB0"), "w").close()
        result = self.run_script()
        self.assertIn("unknown USB ID", result.stderr)
        self.assertIn("No known USB-TTL adapter", result.stderr)

    def test_an_explicit_port_is_used_as_given(self):
        self.add_tty("ttyUSB0", "0403", "6014")
        port = os.path.join(self.dev, "ttyUSB0")
        result = self.run_script("--port", port)
        self.assertIn(f"Failed to read serial settings from {port}", result.stderr)


if __name__ == "__main__":
    unittest.main()
