#!/usr/bin/env python3
"""The README's development install, run as written.

README.md shows how to install the tools into a 1541ultimate checkout from a
clone of this repository. This module extracts that shell block so that the
tests and CI run exactly what a reader runs, and cannot drift from it.

    python3 tests/upstream/readme.py PARENT

installs into PARENT/1541ultimate from PARENT/1541ultimate-tools, the layout
the README assumes.
"""

import os
import re
import subprocess
import sys

TOOLS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
INSTALL_LINE = "1541ultimate-tools/install.sh 1541ultimate"


def install_block(readme=os.path.join(TOOLS, "README.md")):
    """The development install block, from its install.sh line on.

    The block starts with the two `git clone` commands, which the caller has
    already done its own way.
    """
    with open(readme) as handle:
        text = handle.read()
    found = [b for b in re.findall(r"```bash\n(.*?)```", text, re.S) if INSTALL_LINE in b]
    if len(found) != 1:
        raise ValueError(f"README.md has {len(found)} blocks running {INSTALL_LINE!r}, expected 1")
    return found[0][found[0].index(INSTALL_LINE):]


def run(script, parent):
    return subprocess.run(["bash", "-euc", script], cwd=parent, capture_output=True, text=True)


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    result = run(install_block(), argv[1])
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
