#!/usr/bin/env bash
# logging.sh — colour setup and all log_* / die helpers

setup_colors() {
    if [ -t 1 ] && [ "${NO_COLOR:-}" != "1" ]; then
        COLOR_RED=$(printf '\033[31m')
        COLOR_GREEN=$(printf '\033[32m')
        COLOR_YELLOW=$(printf '\033[33m')
        COLOR_BLUE=$(printf '\033[34m')
        COLOR_CYAN=$(printf '\033[36m')
        COLOR_BOLD=$(printf '\033[1m')
        COLOR_RESET=$(printf '\033[0m')
    else
        COLOR_RED="" COLOR_GREEN="" COLOR_YELLOW=""
        COLOR_BLUE="" COLOR_CYAN="" COLOR_BOLD="" COLOR_RESET=""
    fi
}

log_info() {
    printf '%sINFO%s %s\n' "$COLOR_BLUE" "$COLOR_RESET" "$*"
}

log_warn() {
    printf '%sWARN%s %s\n' "$COLOR_YELLOW" "$COLOR_RESET" "$*" >&2
}

log_error() {
    printf '%sERROR%s %s\n' "$COLOR_RED" "$COLOR_RESET" "$*" >&2
}

log_success() {
    printf '%sOK%s %s\n' "$COLOR_GREEN" "$COLOR_RESET" "$*"
}

log_section() {
    printf '\n%s==> %s%s\n' "$COLOR_BOLD" "$*" "$COLOR_RESET"
}

# die MESSAGE — report an environment or setup failure and exit 3. Unlike
# die_usage it does not print the usage text: the command line was fine.
die() {
    log_error "$*"
    exit 3
}

# die_usage MESSAGE — report a command-line error, print the usage, exit 3
die_usage() {
    log_error "$*"
    printf '\n' >&2
    usage >&2
    exit 3
}

on_error() {
    local exit_code=$?
    [ "$exit_code" -eq 0 ] && return
    [ -n "$CURRENT_ACTION" ] && log_error "$CURRENT_ACTION failed."
    exit "$exit_code"
}
