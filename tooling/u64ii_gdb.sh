#!/usr/bin/env bash
# Start the JTAG gdb server for an Ultimate 64 Elite II / C64 Ultimate and
# attach gdb to it with the application's ELF. Read-only: memory and each
# FreeRTOS task's saved registers; the CPU is never stopped or modified.
#
# Usage: u64ii_gdb.sh [ELF] [-- extra gdb arguments]
#   ELF defaults to target/u64ii/riscv/ultimate/result/ultimate.elf of
#   $ULTIMATE_REPO_DIR (default: this checkout). It must be the ELF of the
#   application that is running. Set U64II_GDB_WRITES=1 to allow memory writes.
#   Inside gdb: `info threads`, `thread N`, `bt`, `p some_global`, `x/16x ADDR`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="${ULTIMATE_REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
ELF="$REPO/target/u64ii/riscv/ultimate/result/ultimate.elf"
if [[ $# -gt 0 && "$1" != "--" ]]; then ELF=$1; shift; fi
[[ "${1:-}" == "--" ]] && shift
PORT="${U64II_GDB_PORT:-3333}"

die() { printf '[u64ii-gdb] ERROR: %s\n' "$*" >&2; exit 1; }
[[ -s "$ELF" ]] || die "no ELF at $ELF"

# A RISC-V gdb with Python is needed for the unwinder (u64ii_gdb_unwind.py):
# without it backtraces stop after the first frame. The xPack builds ship one
# as *-gdb-py3; gdb-multiarch also has Python.
GDB="${GDB:-}"
for candidate in riscv-none-embed-gdb-py3 riscv32-unknown-elf-gdb-py3 \
        "$HOME"/riscv-xpack-*/bin/riscv-none-embed-gdb-py3 gdb-multiarch \
        riscv32-unknown-elf-gdb "$HOME/riscv/bin/riscv32-unknown-elf-gdb"; do
    [[ -n "$GDB" ]] && break
    [[ -x "$candidate" ]] && GDB=$candidate && continue
    command -v "$candidate" >/dev/null 2>&1 && GDB=$(command -v "$candidate")
done
[[ -n "$GDB" ]] || die "no RISC-V gdb found; set GDB=/path/to/riscv gdb with Python"
"$GDB" -q -batch -ex "python print(1)" >/dev/null 2>&1 \
    || printf '[u64ii-gdb] warning: %s has no Python; backtraces stop after frame 0\n' "$GDB" >&2

stub_args=(--elf "$ELF" --port "$PORT")
[[ "${U64II_GDB_WRITES:-0}" == "1" ]] && stub_args+=(--allow-writes)

# The JTAG tool's wrapper provides pyftdi; reuse its interpreter.
PY="${U64II_JTAG_PYTHON:-${XDG_CACHE_HOME:-$HOME/.cache}/1541ultimate-tools/jtag-venv/bin/python}"
[[ -x "$PY" ]] || { "$SCRIPT_DIR/u64ii_jtag.sh" --help >/dev/null; }
"$PY" "$SCRIPT_DIR/u64ii_gdbstub.py" "${stub_args[@]}" &
STUB=$!
trap 'rc=$?; kill $STUB 2>/dev/null; wait $STUB 2>/dev/null; exit $rc' EXIT
for _ in $(seq 1 100); do
    kill -0 $STUB 2>/dev/null || die "the gdb server did not start"
    (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null && break
    sleep 0.2
done

unwind=()
"$GDB" -q -batch -ex "python print(1)" >/dev/null 2>&1 && unwind=(-ex "source $SCRIPT_DIR/u64ii_gdb_unwind.py")
"$GDB" -q "$ELF" -ex "set pagination off" "${unwind[@]}" -ex "target extended-remote :$PORT" "$@"
