#!/usr/bin/env bash
# runtime.sh — command execution and tool availability helpers
#
# Depends on: common.sh (quote_command), logging.sh (log_info, die), $DRY_RUN

# run_command CMD [ARGS...] — log and execute; skip execution in dry-run mode
run_command() {
    log_info "$(quote_command "$@")"
    [ "$DRY_RUN" -eq 1 ] && return 0
    "$@"
}

# require_command NAME — abort if NAME is not on PATH
require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}