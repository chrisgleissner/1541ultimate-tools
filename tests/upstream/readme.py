#!/usr/bin/env python3
"""The README's install steps, run as written.

README.md shows how to overlay the tools onto a 1541ultimate checkout and how
to exclude them from git. This module extracts those two shell blocks so that
the tests and CI run exactly what a reader runs, and cannot drift from it.

    python3 tests/upstream/readme.py PARENT

installs into PARENT/1541ultimate from PARENT/1541ultimate-tools, the layout
the README assumes, and excludes the installed files from git.
"""

import os
import re
import subprocess
import sys

TOOLS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def blocks(readme=os.path.join(TOOLS, "README.md")):
    """(install, exclude): the two bash blocks, each from its `cd 1541ultimate` on."""
    with open(readme) as handle:
        text = handle.read()
    section = text[text.index("## Installing into a checkout"):text.index("## Prerequisites")]
    found = re.findall(r"```bash\n(.*?)```", section, re.S)
    install = [b for b in found if "cp -r ../1541ultimate-tools/" in b]
    exclude = [b for b in found if ".git/info/exclude" in b]
    if len(install) != 1 or len(exclude) != 1:
        raise ValueError("README.md no longer has one install and one exclude block")
    # Both blocks change into the checkout first; the install block starts with
    # the clone commands, which the caller has already done its own way.
    return tuple(b[b.index("cd 1541ultimate"):] for b in (install[0], exclude[0]))


def run(script, parent):
    return subprocess.run(["bash", "-euc", script], cwd=parent, capture_output=True, text=True)


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    parent = argv[1]
    for name, script in zip(("install", "exclude"), blocks()):
        result = run(script, parent)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if result.returncode:
            print(f"README {name} block failed with exit status {result.returncode}",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
