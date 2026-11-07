#!/usr/bin/env bash
# Run tooling/c64u_mode.py (switch a C64 Ultimate between firmware setups). Link it as ~/.local/bin/c64u-mode.
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/c64u_mode.py" "$@"
