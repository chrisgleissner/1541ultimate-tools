#!/usr/bin/env bash

set -euo pipefail

# Same lookup as build_and_deploy_u64.sh; set INTEL_FPGA_ROOT to override.
if [ -z "${INTEL_FPGA_ROOT:-}" ]; then
    for candidate in \
        "$HOME"/intelFPGA_lite/*  "$HOME"/intelFPGA/*  "$HOME"/altera_lite/* \
        /opt/intelFPGA_lite/*     /opt/intelFPGA/*     /opt/altera_lite/*; do
        if [ -x "$candidate/quartus/bin/jtagconfig" ]; then
            INTEL_FPGA_ROOT="$candidate"
            break
        fi
    done
fi
[ -n "${INTEL_FPGA_ROOT:-}" ] || { echo "Could not locate the Intel FPGA tools; set INTEL_FPGA_ROOT" >&2; exit 1; }
SECONDS_TO_CAPTURE="${1:-5}"
JTAGCONFIG="$INTEL_FPGA_ROOT/quartus/bin/jtagconfig"
NIOS2_TERMINAL="$INTEL_FPGA_ROOT/quartus/bin/nios2-terminal"

log() { printf '[u64-jtag-monitor] %s\n' "$*"; }
die() { printf '[u64-jtag-monitor] ERROR: %s\n' "$*" >&2; exit 1; }
add_path() { [ -d "$1" ] && PATH="$1:$PATH"; }

[ "$#" -le 1 ] || die "Usage: $(basename "$0") [seconds]"
case "$SECONDS_TO_CAPTURE" in
    ''|*[!0-9]*)
        die "Capture duration must be a non-negative integer number of seconds"
        ;;
esac

[ -x "$JTAGCONFIG" ] || die "Missing jtagconfig under $INTEL_FPGA_ROOT"
[ -x "$NIOS2_TERMINAL" ] || die "Missing nios2-terminal under $INTEL_FPGA_ROOT"

export QUARTUS_ROOTDIR="$INTEL_FPGA_ROOT/quartus"
export QSYS_ROOTDIR="$QUARTUS_ROOTDIR/sopc_builder/bin"
add_path "$INTEL_FPGA_ROOT/quartus/bin"
add_path "$QSYS_ROOTDIR"
export PATH

log "Checking JTAG hardware"
JTAG_OUTPUT="$("$JTAGCONFIG" 2>/dev/null || true)"
printf '%s\n' "$JTAG_OUTPUT"
grep -Eq '[0-9]+\)' <<<"$JTAG_OUTPUT" || die "No JTAG hardware available"

log "Capturing JTAG terminal output for ${SECONDS_TO_CAPTURE}s"
set +e
TERMINAL_OUTPUT="$("$NIOS2_TERMINAL" --flush -o "$SECONDS_TO_CAPTURE" -q 2>&1)"
TERMINAL_STATUS=$?
set -e

if [ -n "$TERMINAL_OUTPUT" ]; then
    printf '%s\n' "$TERMINAL_OUTPUT"
fi

if [ "$TERMINAL_STATUS" -eq 0 ]; then
    exit 0
fi

if printf '%s\n' "$TERMINAL_OUTPUT" | grep -q "There are no JTAG UARTs available"; then
    die "The current U64 image exposes Nios II JTAG debug/download, but no JTAG UART console. There are no JTAG-readable runtime messages to show from this image."
fi

die "nios2-terminal failed with status $TERMINAL_STATUS"
