#!/usr/bin/env bash

set -euo pipefail

PORT=""
BAUD=115200
SECONDS_TO_CAPTURE=5

log() { printf '[u64-uart-monitor] %s\n' "$*"; }
die() { printf '[u64-uart-monitor] ERROR: %s\n' "$*" >&2; exit 1; }

# USB-serial adapters this script may pick on its own, as VID:PID. An FTDI
# FT232H, FT2232H or FT4232H (0403:6014, 6010, 6011) is usually the JTAG
# adapter of the same bench, and a USB-Blaster is 09fb:6001. Reading from one
# of those would block on the wrong device, so they are never auto-selected.
# Pass --port to use any other device.
KNOWN_USB_TTL="0403:6001 0403:6015 10c4:ea60 1a86:7523 1a86:55d4 067b:2303"

# Where the device nodes and their sysfs entries are looked up; the tests point
# these at a scratch tree.
TTY_DEV_DIR="${U64_UART_DEV_DIR:-/dev}"
TTY_SYS_DIR="${U64_UART_SYS_TTY_DIR:-/sys/class/tty}"

usage() {
    cat <<'EOF'
Usage: read_u64_uart_terminal.sh [--port DEVICE] [--baud N] [--secs N]

Reads the U64 debug UART using a USB-TTL serial adapter.

If --port is omitted, the script uses the only /dev/ttyUSB* or /dev/ttyACM*
device whose USB vendor:product ID is a known USB-TTL adapter (FT232R, FT231X,
CP210x, CH340/CH9102, PL2303). Other devices, such as an FTDI JTAG adapter,
are listed and skipped. Pass --port to use one of those.
EOF
}

# usb_id TTY_NAME — "vid:pid" of the USB device behind a tty, or nothing
usb_id() {
    local dir vid pid
    dir=$(readlink -f "$TTY_SYS_DIR/$1/device" 2>/dev/null) || return 0
    while [ -n "$dir" ] && [ "$dir" != "/" ]; do
        if [ -r "$dir/idVendor" ] && [ -r "$dir/idProduct" ]; then
            vid=$(tr 'A-F' 'a-f' <"$dir/idVendor")
            pid=$(tr 'A-F' 'a-f' <"$dir/idProduct")
            printf '%s:%s' "$vid" "$pid"
            return 0
        fi
        dir=$(dirname "$dir")
    done
}

detect_port() {
    local ports=() skipped=()
    local candidate id

    for candidate in "$TTY_DEV_DIR"/ttyUSB* "$TTY_DEV_DIR"/ttyACM*; do
        [ -e "$candidate" ] || continue
        id=$(usb_id "$(basename "$candidate")")
        if [ -n "$id" ] && [[ " $KNOWN_USB_TTL " == *" $id "* ]]; then
            ports+=("$candidate")
        else
            skipped+=("$candidate (${id:-unknown USB ID})")
        fi
    done

    if [ "${#skipped[@]}" -gt 0 ]; then
        printf '[u64-uart-monitor] Not a known USB-TTL adapter, skipped:\n' >&2
        printf '  %s\n' "${skipped[@]}" >&2
    fi
    if [ "${#ports[@]}" -eq 0 ]; then
        die "No known USB-TTL adapter found. Connect a 3.3V USB-TTL adapter or pass --port."
    fi
    if [ "${#ports[@]}" -gt 1 ]; then
        printf '[u64-uart-monitor] Multiple USB-TTL adapters detected:\n' >&2
        printf '  %s\n' "${ports[@]}" >&2
        die "Pass --port to choose the correct device."
    fi

    printf '%s\n' "${ports[0]}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --port)
            [ "$#" -ge 2 ] || die "Missing value for $1"
            PORT=$2
            shift
            ;;
        --baud)
            [ "$#" -ge 2 ] || die "Missing value for $1"
            BAUD=$2
            shift
            ;;
        --secs)
            [ "$#" -ge 2 ] || die "Missing value for $1"
            SECONDS_TO_CAPTURE=$2
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "Unknown option: $1"
            ;;
    esac
    shift
done

case "$BAUD" in
    ''|*[!0-9]*)
        die "Baud rate must be a positive integer"
        ;;
esac
case "$SECONDS_TO_CAPTURE" in
    ''|*[!0-9]*)
        die "Capture duration must be a non-negative integer number of seconds"
        ;;
esac

if [ -z "$PORT" ]; then
    PORT=$(detect_port)
fi

[ -e "$PORT" ] || die "Serial device not found: $PORT"
[ -r "$PORT" ] || die "Serial device is not readable: $PORT"
[ -w "$PORT" ] || die "Serial device is not writable: $PORT"

ORIGINAL_STTY=$(stty -F "$PORT" -g) || die "Failed to read serial settings from $PORT"
cleanup() {
    stty -F "$PORT" "$ORIGINAL_STTY" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

stty -F "$PORT" "$BAUD" cs8 -cstopb -parenb -ixon -ixoff -crtscts -echo raw min 0 time 1

log "Listening on $PORT at ${BAUD} baud"
if [ "$SECONDS_TO_CAPTURE" -eq 0 ]; then
    log "Streaming until interrupted"
    exec cat "$PORT"
fi

log "Capturing UART output for ${SECONDS_TO_CAPTURE}s"
set +e
timeout "$SECONDS_TO_CAPTURE" cat "$PORT"
status=$?
set -e

if [ "$status" -ne 0 ] && [ "$status" -ne 124 ]; then
    die "UART capture failed with status $status"
fi
