#!/usr/bin/env bash
# targets.sh — target name resolution, capabilities, and artifact paths

# All canonical target names in build order
CANONICAL_TARGETS=(u64 u64ii u2)

validate_target_name() {
    case "$1" in
        u2|u2rv|u2_rv|u2plus|u2pl|u2l|u64|u64ii|ue2|c64u|all) return 0 ;;
        *) return 1 ;;
    esac
}

normalize_target_name() {
    case "$1" in
        u2|u2rv|u2_rv)   printf 'u2' ;;
        u2pl|u2l)        printf 'u2pl' ;;
        # The C64 Ultimate is Ultimate 64 Elite II hardware: same Artix-7
        # FPGA, RISC-V CPU and update.ue2 package.
        u64ii|ue2|c64u)  printf 'u64ii' ;;
        *)             printf '%s' "$1" ;;
    esac
}

expand_targets() {
    local input=("$@")
    local expanded=()
    local item normalized

    if [ "${#input[@]}" -eq 0 ] || contains_value all "${input[@]}"; then
        TARGETS=("${CANONICAL_TARGETS[@]}")
        return
    fi

    for item in "${input[@]}"; do
        normalized=$(normalize_target_name "$item")
        contains_value "$normalized" "${expanded[@]}" || expanded+=("$normalized")
    done
    TARGETS=("${expanded[@]}")
}

enforce_required_targets() {
    # A JTAG request is an explicitly scoped development deployment, so its
    # target list is sufficient without BUILD_TOOL_ALLOW_PARTIAL.
    [ "${#JTAG_TARGETS[@]}" -gt 0 ] && [ "${#DEPLOY_TARGETS[@]}" -eq 0 ] && return 0
    if [ "${BUILD_TOOL_ALLOW_PARTIAL:-${BUILD_DOCKER_ALLOW_PARTIAL:-0}}" = "1" ]; then
        return 0
    fi
    local required missing=()
    for required in "${CANONICAL_TARGETS[@]}"; do
        contains_value "$required" "${TARGETS[@]}" || missing+=("$required")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        die "Missing required target(s): ${missing[*]}. All three targets (u64, u64ii, u2) must be built together unless BUILD_TOOL_ALLOW_PARTIAL=1 is set, for example: BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool ${TARGETS[*]}"
    fi
}

resolve_target_list() {
    local input=("$@")
    local resolved=()
    local item normalized

    if [ "${#input[@]}" -eq 0 ] || contains_value all "${input[@]}"; then
        printf '%s\n' "${CANONICAL_TARGETS[@]}"
        return 0
    fi

    for item in "${input[@]}"; do
        normalized=$(normalize_target_name "$item")
        contains_value "$normalized" "${resolved[@]}" || resolved+=("$normalized")
    done
    printf '%s\n' "${resolved[@]}"
}

# jtag_app_only TARGET — true when TARGET is built only for a JTAG run: the
# application image, without the ESP32 firmware or the update package.
jtag_app_only() {
    contains_value "$1" "${JTAG_APP_ONLY_TARGETS[@]+"${JTAG_APP_ONLY_TARGETS[@]}"}"
}

# target_app_makefile TARGET — the makefile of the application a JTAG run loads
target_app_makefile() {
    case "$1" in
        u64)   printf 'target/u64/nios2/ultimate/Makefile' ;;
        u64ii) printf 'target/u64ii/riscv/ultimate/Makefile' ;;
        *) return 1 ;;
    esac
}

# target_jtag_image TARGET — the file a JTAG run loads into the device
target_jtag_image() {
    case "$1" in
        u64)   printf 'target/u64/nios2/ultimate/result/ultimate.elf' ;;
        u64ii) printf 'target/u64ii/riscv/ultimate/result/ultimate.bin' ;;
        *) return 1 ;;
    esac
}

# target_output_name TARGET — canonical firmware filename
target_output_name() {
    case "$1" in
        u2)     printf 'update.u2r' ;;
        u2plus) printf 'update.u2p' ;;
        u2pl)   printf 'update.u2l' ;;
        u64)    printf 'update.u64' ;;
        u64ii)  printf 'update.ue2' ;;
        *) return 1 ;;
    esac
}

# target_default_deploy_path TARGET
target_default_deploy_path() {
    case "$1" in
        u64)   printf '%s' "$DEFAULT_DEPLOY_PATH_U64" ;;
        u64ii) printf '%s' "$DEFAULT_DEPLOY_PATH_U64II" ;;
        *)     printf '%s' "$DEFAULT_DEPLOY_PATH" ;;
    esac
}

# target_make_name TARGET — make goal inside the container
target_make_name() {
    local target=$1
    case "$target" in
        u2)
            [ "$SW_ONLY" -eq 1 ] && printf 'u2_rv_swonly' || printf 'u2_rv'
            ;;
        u2plus)
            [ "$SW_ONLY" -eq 1 ] && printf 'u2plus_swonly' || printf 'u2plus'
            ;;
        u2pl)
            [ "$SW_ONLY" -eq 1 ] && printf 'u2pl_swonly' || printf 'u2pl'
            ;;
        u64)    printf 'u64' ;;
        u64ii)  printf 'u64ii' ;;
        *) return 1 ;;
    esac
}

target_requires_nios() {
    case "$1" in u2plus|u64) return 0 ;; *) return 1 ;; esac
}

target_requires_xilinx() {
    [ "$SW_ONLY" -eq 1 ] && return 1
    case "$1" in u2) return 0 ;; *) return 1 ;; esac
}

target_requires_lattice() {
    [ "$SW_ONLY" -eq 1 ] && return 1
    case "$1" in u2pl) return 0 ;; *) return 1 ;; esac
}

target_requires_riscv() {
    case "$1" in u2|u2pl|u64ii) return 0 ;; *) return 1 ;; esac
}

target_needs_build_tools() {
    target_requires_nios "$1" || target_requires_xilinx "$1" \
        || target_requires_lattice "$1"
}

# target_artifact_path TARGET — versioned output filename
target_artifact_path() {
    local plain
    plain=$(target_output_name "$1")
    printf 'update_%s.%s' "$VERSION_STRING" "${plain#update.}"
}

# find_existing_artifact TARGET — print path or return 1
find_existing_artifact() {
    local target=$1
    local versioned plain suffix fallback
    versioned=$(target_artifact_path "$target")
    plain=$(target_output_name "$target")

    [ -f "$versioned" ] && { printf '%s' "$versioned"; return 0; }
    [ -f "$plain"     ] && { printf '%s' "$plain";     return 0; }

    suffix=${plain#update.}
    fallback=$(find . -maxdepth 1 -type f -name "update_*.${suffix}" -printf '%P\n' \
               | sort | tail -n 1 2>/dev/null || true)
    [ -n "$fallback" ] && { printf '%s' "$fallback"; return 0; }
    return 1
}

# list_targets_table — formatted target list for --list-targets
list_targets_table() {
    printf '%sTarget   Output          Toolchain required%s\n' "$COLOR_BOLD" "$COLOR_RESET"
    printf '%s-------  --------------  ------------------------------------------%s\n' \
           "$COLOR_CYAN" "$COLOR_RESET"
    printf '%-8s %-15s %s\n' u2     update.u2r  "RISC-V + Xilinx ISE (sw-only: cached FPGA)"
    printf '%-8s %-15s %s\n' u2plus update.u2p  "Nios2 + Quartus (sw-only: cached FPGA)"
    printf '%-8s %-15s %s\n' u2pl   update.u2l  "RISC-V + Lattice Diamond + ESP32-C3"
    printf '%-8s %-15s %s\n' u64    update.u64  "Nios2 + Quartus + ESP32"
    printf '%-8s %-15s %s\n' u64ii  update.ue2  "RISC-V + ESP32-S3 (alias c64u; FPGA from external/)"
    printf '%-8s %-15s %s\n' all    "(u64 u64ii u2)" "the default set; not u2plus or u2pl"
}
