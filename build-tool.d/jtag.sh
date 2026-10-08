#!/usr/bin/env bash
# jtag.sh — JTAG deployment and JTAG terminal monitoring
#
# The two board families are reached differently. The Ultimate 64 (Cyclone V,
# Nios II) goes through a USB-Blaster and nios2-download. The Ultimate 64
# Elite II and the C64 Ultimate (Artix-7, RISC-V) go through an FT232H (or a
# USB-Blaster) and the FPGA's user JTAG chain. Both deployments are volatile and load the
# application built from $REPO_DIR; the helper scripts live beside build-tool.

# _jtag_helper NAME — path of a tooling/ script, or fail the calling step
_jtag_helper() {
    local path="$TOOLING_DIR/$1"
    [ -x "$path" ] || { log_error "JTAG helper not found or not executable: $path"; return 1; }
    printf '%s' "$path"
}

run_jtag_recovery() {
    local target=$1 helper
    local -a env_args=(ULTIMATE_REPO_DIR="$REPO_DIR")

    case "$target" in
        u64)
            helper=$(_jtag_helper build_and_deploy_u64.sh) || {
                DEPLOY_FAILED=1; FAILED_JTAGS+=("$target"); return 1; }
            ;;
        u64ii)
            helper=$(_jtag_helper build_and_deploy_u64ii.sh) || {
                DEPLOY_FAILED=1; FAILED_JTAGS+=("$target"); return 1; }
            env_args+=(U64II_JTAG_URL="$JTAG_URL")
            [ -n "$JTAG_FPGA" ] && env_args+=(U64II_JTAG_FPGA="$JTAG_FPGA")
            ;;
        *)
            DEPLOY_FAILED=1; FAILED_JTAGS+=("$target")
            log_error "JTAG deployment is supported for u64 and u64ii (c64u) only."
            return 1
            ;;
    esac

    CURRENT_ACTION="Running JTAG deployment for ${target}"
    if ! run_command env "${env_args[@]}" "$helper"; then
        DEPLOY_FAILED=1; FAILED_JTAGS+=("$target")
        log_error "JTAG deployment failed for ${target}."
        CURRENT_ACTION=""; return 1
    fi
    log_success "JTAG deployment completed for ${target}."
    CURRENT_ACTION=""; return 0
}

run_jtag_monitor() {
    local target=$1 helper
    local -a env_args=()

    case "$target" in
        u64)   helper=$(_jtag_helper read_u64_jtag_terminal.sh) || helper="" ;;
        u64ii) helper=$(_jtag_helper read_u64ii_jtag_terminal.sh) || helper=""
               env_args+=(U64II_JTAG_URL="$JTAG_URL") ;;
        *)
            log_error "JTAG monitor is supported for u64 and u64ii (c64u) only."
            helper=""
            ;;
    esac
    if [ -z "$helper" ]; then
        MONITOR_FAILED=1; FAILED_JTAG_MONITORS+=("$target")
        return 1
    fi

    CURRENT_ACTION="Reading JTAG monitor output for ${target}"
    if ! run_command env "${env_args[@]}" "$helper" "$JTAG_MONITOR_SECS"; then
        MONITOR_FAILED=1; FAILED_JTAG_MONITORS+=("$target")
        log_error "JTAG monitor failed for ${target}."
        CURRENT_ACTION=""; return 1
    fi

    log_success "JTAG monitor completed for ${target}."
    CURRENT_ACTION=""; return 0
}
