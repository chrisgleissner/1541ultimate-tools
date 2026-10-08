#!/usr/bin/env bash
# Run the built Ultimate 64 Elite II / C64 Ultimate application from RAM over
# JTAG (FT232H or USB-Blaster). The counterpart of build_and_deploy_u64.sh for the Artix-7
# boards, which have no Nios II and no nios2-download.
#
# Like the U64 script it does not build and takes no arguments: it loads
# target/u64ii/riscv/ultimate/result/ultimate.bin, which must already exist.
# Nothing is flashed; a power cycle returns the board to its flashed firmware.
#
# Environment:
#   ULTIMATE_REPO_DIR       tree holding the built image (default: this checkout)
#   U64II_JTAG_FPGA         bitstream configured before the application loads:
#                           auto (default; external/u64e2_*.bit by IDCODE) or a
#                           path. "warm" keeps the running FPGA image and
#                           restarts only the CPU, about 7 s faster.
#   U64II_JTAG_CONSOLE=N    show the application's console for N seconds after
#   U64II_JTAG_URL          pyftdi URL of the FT232H, or blaster

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
export ULTIMATE_REPO_DIR="${ULTIMATE_REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
IMAGE="$ULTIMATE_REPO_DIR/target/u64ii/riscv/ultimate/result/ultimate.bin"

log() { printf '[u64ii-jtag] %s\n' "$*"; }
die() { printf '[u64ii-jtag] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $# -eq 0 ]] || die "This helper does not accept arguments"
[[ -s "$IMAGE" ]] || die "Missing deployable image: $IMAGE"

args=(run --bin "$IMAGE")
case "${U64II_JTAG_FPGA:-auto}" in
    warm) args+=(--warm) ;;
    *)    args+=(--fpga "${U64II_JTAG_FPGA:-auto}") ;;
esac
[[ -n "${U64II_JTAG_CONSOLE:-}" ]] && args+=(--console "$U64II_JTAG_CONSOLE")

log "Running $IMAGE over JTAG"
"$SCRIPT_DIR/u64ii_jtag.sh" "${args[@]}"
log "U64-II JTAG deployment completed"
