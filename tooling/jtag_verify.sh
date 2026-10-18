#!/usr/bin/env bash
# Use the same pinned PyFtdi environment as the repository's JTAG tools.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VENV="${U64II_JTAG_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/1541ultimate-tools/jtag-venv}"
PYTHON="${U64II_JTAG_PYTHON:-$VENV/bin/python}"
if ! "$PYTHON" -c 'import pyftdi' >/dev/null 2>&1; then
    printf 'Install the shared JTAG environment with tooling/u64ii_jtag.sh --help, or set U64II_JTAG_PYTHON.\n' >&2
    exit 2
fi
exec "$PYTHON" "$SCRIPT_DIR/jtag_verify.py" "$@"
