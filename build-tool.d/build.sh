#!/usr/bin/env bash
# build.sh — git/repo setup, cleaning, building, and artifact verification
#
# New features vs. original:
#   - Per-target build timing (shows elapsed time on success/failure)
#   - --parallel flag: build all targets concurrently, then collect results
#   - Artifact checksum printed in summary (sha256, if xxd/sha256sum available)

ensure_repo_root() {
    local root
    if [ -n "$REPO_DIR" ]; then
        git -C "$REPO_DIR" rev-parse --show-toplevel >/dev/null 2>&1 \
            || die "--repo-dir '$REPO_DIR' is not a git repository."
        root=$(cd "$REPO_DIR" && git rev-parse --show-toplevel)
    else
        root=$(git rev-parse --show-toplevel 2>/dev/null) \
            || die "Not inside a git repository. Use --repo-dir to specify the 1541ultimate repo root."
    fi
    REPO_DIR=$(cd "$root" && pwd -P)
    cd "$REPO_DIR"
}

ensure_git_ready() {
    require_command git
    git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || die "Not a git repository: $REPO_DIR"
}

detect_jobs() {
    [ -n "$JOBS" ] && return
    JOBS=$(getconf _NPROCESSORS_ONLN 2>/dev/null \
        || nproc 2>/dev/null \
        || sysctl -n hw.ncpu 2>/dev/null \
        || echo 1)
}

resolve_version() {
    [ -n "$VERSION_STRING" ] && return
    VERSION_STRING=$(git rev-parse --short=8 HEAD)
}

update_submodules() {
    if [ "$SKIP_SUBMODULE_UPDATE" -eq 1 ] || [ "$DEPLOY_ONLY" -eq 1 ]; then
        return 0
    fi
    CURRENT_ACTION="Updating git submodules"
    run_command git submodule update --init --recursive
    CURRENT_ACTION=""
}

run_clean() {
    [ "$CLEAN_BUILD" -eq 0 ] || [ "$DEPLOY_ONLY" -eq 1 ] && return 0

    local clean_target require_nios=0 require_xilinx=0 require_lattice=0 require_riscv=0
    clean_target=$([ "$SW_ONLY" -eq 1 ] && echo sw_clean || echo clean)

    local target
    for target in "${TARGETS[@]}"; do
        target_requires_nios    "$target" && require_nios=1
        target_requires_xilinx  "$target" && require_xilinx=1
        target_requires_lattice "$target" && require_lattice=1
        target_requires_riscv   "$target" && require_riscv=1
    done

    CURRENT_ACTION="Cleaning build outputs"
    docker_run_build_script \
        "${CONTAINER_PRELUDE}
cd /__w
make ${clean_target}" \
        "$require_nios" "$require_xilinx" "$require_lattice" 1 "$require_riscv"
    CURRENT_ACTION=""
}

preseed_make_dirs() {
    # Writes output/ and result/ directories and gitinfo.h into the checkout.
    if [ "$DRY_RUN" -eq 1 ]; then
        log_info "Would pre-seed output/ and result/ directories and gitinfo.h in the checkout."
        return 0
    fi
    CURRENT_ACTION="Pre-seeding build directories"

    local git_tag git_branch git_date git_hash git_build git_host makefile dir
    git_tag=$(git describe --tags 2>/dev/null || echo unknown)
    git_branch=$(git describe --all 2>/dev/null || echo unknown)
    git_date=$(git log -n 1 --format=%ai 2>/dev/null || echo unknown)
    git_hash=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
    # The development JTAG path should be a no-op when neither the source nor
    # HEAD changed.  A wall-clock value makes gitinfo.h newer every run and
    # needlessly recompiles system_info.cc.  Commit time still identifies the
    # firmware revision while keeping the generated header stable.
    local app_only=1 target
    for target in "${TARGETS[@]}"; do
        jtag_app_only "$target" || app_only=0
    done
    if [ "$app_only" -eq 1 ]; then
        git_build=$(git log -n 1 --format=%ai 2>/dev/null || echo unknown)
    else
        git_build=$(date +"%F %R")
    fi
    git_host=$(hostname)

    local -a makefiles=()
    if [ "$app_only" -eq 1 ]; then
        for target in "${TARGETS[@]}"; do
            makefiles+=("$(target_app_makefile "$target")")
        done
    else
        while IFS= read -r -d '' makefile; do
            makefiles+=("$makefile")
        done < <(find target -type f \( -iname 'makefile' -o -iname 'Makefile' \) -print0)
    fi

    for makefile in "${makefiles[@]}"; do
        dir=$(dirname "$makefile")
        mkdir -p "$dir/output" "$dir/result"
        cat >"$dir/output/gitinfo.h" <<EOF
/* Generated file, do not edit. */
#define APP_VERSION_TAG    "${git_tag}"
#define APP_VERSION_BRANCH "${git_branch}"
#define APP_VERSION_DATE   "${git_date}"
#define APP_VERSION_HASH   "${git_hash}"
#define APP_BUILD_DATE     "${git_build}"
#define APP_BUILD_MACHINE  "${git_host}"
EOF
    done

    CURRENT_ACTION=""
}

# _build_target_script TARGET — print the make script body for the container
_build_target_script() {
    local target=$1
    local make_target
    make_target=$(target_make_name "$target")
    # A fresh worktree has no output/ or result/ directories, and the library
    # makefiles do not create them.
    local mkdirs="find target -type f \\( -iname 'makefile' -o -iname 'Makefile' \\) -printf '%h\\n' \\
    | sort -u | xargs -I {} mkdir -p '{}/output' '{}/result' 2>/dev/null || true"

    if [ "$target" = "u64" ] && jtag_app_only u64; then
        cat <<SCRIPT
${CONTAINER_PRELUDE}
cd /__w
${mkdirs}
make -j ${JOBS} -C tools
make -j ${JOBS} -C software/nios_solo_bsp
make -j ${JOBS} -C software/nios_appl_bsp
make -j ${JOBS} -C target/libs/nios2/lwip
make -j ${JOBS} -C target/u64/nios2/ultimate result/ultimate.elf
SCRIPT
        return
    fi

    if [ "$target" = "u64ii" ] && jtag_app_only u64ii; then
        cat <<SCRIPT
${CONTAINER_PRELUDE}
cd /__w
${mkdirs}
make -j ${JOBS} -C tools
make -j ${JOBS} -C target/libs/riscv/lwip
make -j ${JOBS} -C target/u64ii/riscv/ultimate
SCRIPT
        return
    fi

    if [ "$target" = "u2" ] && [ "$DRY_RUN" -eq 0 ]; then
        cat <<SCRIPT
${CONTAINER_PRELUDE}
cd /__w
overlay=/tmp/1541u-global_makefiles
rm -rf "\$overlay"
cp -R global_makefiles "\$overlay"
git show HEAD:global_makefiles/shell.inc > "\$overlay/shell.inc"
git show HEAD:global_makefiles/xilinx_ise.inc > "\$overlay/xilinx_ise.inc"
make -j ${JOBS} SHELL=/bin/bash GLOBAL_INCS="\$overlay" ${make_target}
SCRIPT
    else
        # Pre-clean stale idf.py log files to prevent PID-collision errors
        # across Docker runs (idf.py names log files using the process PID).
        local esp_log_clean=""
        case "$target" in
            u64)
                # Also remove project_elf_src_esp32.c: this cmake-generated file can go missing
                # after an interrupted build, causing a fatal compile error at the last ninja step.
                esp_log_clean="rm -f /__w/software/wifi/raw_u64/build/log/idf_py_*
rm -f /__w/software/wifi/raw_u64/build/project_elf_src_esp32.c 2>/dev/null || true"
                ;;
            u64ii)
                esp_log_clean="rm -f /__w/software/u64ctrl/build/log/idf_py_* 2>/dev/null || true
rm -f /__w/software/wifi/raw_u64ii/build/log/idf_py_* 2>/dev/null || true
rm -f /__w/software/u64ctrl/build/project_elf_src_esp32s3.c 2>/dev/null || true"
                ;;
        esac

        # The updater embeds the ESP32-S3 firmware. Trees whose u64ii goal does
        # not depend on esp32_u64ctrl (a C64 Ultimate firmware tree) need it
        # built first, and not in parallel with u64ii.
        local esp_prereq=""
        if [ "$target" = "u64ii" ]; then
            esp_prereq="grep -Eq '^u64ii::?.*esp32_u64ctrl' Makefile || make esp32_u64ctrl"
        fi

        cat <<SCRIPT
${CONTAINER_PRELUDE}
cd /__w
${esp_log_clean}
${esp_prereq}
find target -type f \( -iname 'makefile' -o -iname 'Makefile' \) -printf '%h\n' \
    | sort -u | xargs -I {} mkdir -p '{}/output' '{}/result' 2>/dev/null || true
make -j ${JOBS} ${make_target}
SCRIPT
    fi
}

recover_u64ii_artifact() {
    local freshness_file=$1
    local output_name=$2

    [ "${BUILD_TOOL_ALLOW_PARTIAL:-${BUILD_DOCKER_ALLOW_PARTIAL:-0}}" = "1" ] || return 1
    [ -n "$freshness_file" ] || return 1
    [ -f target/u64ii/riscv/ultimate/result/ultimate.app ] || return 1
    [ -f target/u64ii/riscv/ultimate/result/ultimate.elf ] || return 1
    [ target/u64ii/riscv/ultimate/result/ultimate.app -nt "$freshness_file" ] || return 1
    [ target/u64ii/riscv/ultimate/result/ultimate.elf -nt "$freshness_file" ] || return 1

    if [ ! -f target/u64ii/riscv/update/result/update.app ] \
       || [ ! target/u64ii/riscv/update/result/update.app -nt "$freshness_file" ]; then
        log_warn "u64ii make exited after building ultimate; finishing updater target directly"
        docker_run_build_script "${CONTAINER_PRELUDE}
cd /__w
make -j ${JOBS} -C target/u64ii/riscv/update" 0 0 0 1 1 || return 1
    fi

    [ -f target/u64ii/riscv/update/result/update.app ] || return 1
    [ target/u64ii/riscv/update/result/update.app -nt "$freshness_file" ] || return 1

    log_warn "u64ii make exited after producing fresh artifacts; using generated update.app and ultimate.elf"
    cp target/u64ii/riscv/update/result/update.app "$output_name"
}

build_target() {
    local target=$1
    local output_name versioned_name require_nios=0 require_xilinx=0 require_lattice=0 require_riscv=0
    local t_start t_elapsed freshness_file=""

    output_name=$(target_output_name "$target")
    versioned_name=$(target_artifact_path "$target")

    target_requires_nios    "$target" && require_nios=1
    target_requires_xilinx  "$target" && require_xilinx=1
    target_requires_lattice "$target" && require_lattice=1
    target_requires_riscv   "$target" && require_riscv=1

    t_start=$(date +%s)
    if [ "$DRY_RUN" -eq 0 ]; then
        freshness_file=$(mktemp /tmp/1541u-build.XXXXXX)
    fi
    CURRENT_ACTION="Building ${target}"

    local needs_idf=1
    jtag_app_only "$target" && needs_idf=0

    if ! docker_run_build_script "$(_build_target_script "$target")" \
           "$require_nios" "$require_xilinx" "$require_lattice" "$needs_idf" "$require_riscv"; then
        if [ "$target" = "u64ii" ] && recover_u64ii_artifact "$freshness_file" "$output_name"; then
            :
        else
            [ -n "$freshness_file" ] && rm -f "$freshness_file"
            t_elapsed=$(elapsed_seconds "$t_start")
            BUILD_FAILED=1
            FAILED_TARGETS+=("$target")
            log_error "Build failed for target ${target} ($(format_duration "$t_elapsed"))."
            CURRENT_ACTION=""
            return 1
        fi
    fi

    if jtag_app_only "$target"; then
        local app_image
        app_image=$(target_jtag_image "$target")
        if [ "$DRY_RUN" -eq 0 ] && [ ! -s "$app_image" ]; then
            [ -n "$freshness_file" ] && rm -f "$freshness_file"
            BUILD_FAILED=1
            FAILED_TARGETS+=("$target")
            log_error "Expected ${target} JTAG application ${app_image} was not produced."
            CURRENT_ACTION=""
            return 1
        fi
        [ -n "$freshness_file" ] && rm -f "$freshness_file"
        t_elapsed=$(elapsed_seconds "$t_start")
        BUILT_TARGETS+=("$target")
        if [ "$DRY_RUN" -eq 1 ]; then
            log_info "Would build the ${target} JTAG application (${app_image})"
        else
            log_success "Built ${target} JTAG application ($(format_duration "$t_elapsed"))"
        fi
        CURRENT_ACTION=""
        return 0
    fi

    if [ "$DRY_RUN" -eq 0 ]; then
        if [ ! -f "$output_name" ]; then
            [ -n "$freshness_file" ] && rm -f "$freshness_file"
            BUILD_FAILED=1
            FAILED_TARGETS+=("$target")
            log_error "Expected artifact ${output_name} was not produced for ${target}."
            CURRENT_ACTION=""
            return 1
        fi
        mv -f "$output_name" "$versioned_name"
        [ -n "$freshness_file" ] && rm -f "$freshness_file"
    fi

    t_elapsed=$(elapsed_seconds "$t_start")
    BUILT_TARGETS+=("$target")
    BUILT_ARTIFACTS+=("$versioned_name")
    if [ "$DRY_RUN" -eq 1 ]; then
        log_info "Would build ${target} -> ${versioned_name}"
    else
        log_success "Built ${target} -> ${versioned_name} ($(format_duration "$t_elapsed"))"
    fi
    CURRENT_ACTION=""
    return 0
}

# build_targets_parallel — build all TARGETS concurrently, collect results back
# into the shared BUILT_TARGETS / BUILT_ARTIFACTS / FAILED_TARGETS arrays.
build_targets_parallel() {
    local target pids=() result_files=()
    local tmp_base
    tmp_base=$(mktemp -d /tmp/1541u-par-build.XXXXXX)

    log_section "Building ${TARGETS[*]} in parallel"

    for target in "${TARGETS[@]}"; do
        local rf="${tmp_base}/${target}.result"
        result_files+=("$rf")
        (
            # Redirect all output to the result file so lines stay together
            exec >"$rf" 2>&1
            if build_target "$target"; then
                printf '__STATUS__ok %s\n' "$target"
            else
                printf '__STATUS__fail %s\n' "$target"
            fi
        ) &
        pids+=("$!")
    done

    # Wait for all jobs; stream their output in order
    local i status_line verdict tgt
    for i in "${!pids[@]}"; do
        wait "${pids[$i]}" || true
        local rf="${result_files[$i]}"
        # Print non-status lines (build log)
        grep -v '^__STATUS__' "$rf" || true
        # Parse the status line
        status_line=$(grep '^__STATUS__' "$rf" | tail -n 1 || true)
        verdict=${status_line#__STATUS__}; verdict=${verdict%% *}
        tgt=${status_line#*__STATUS__?? }  # strip "ok " or "fail "
        if [ "$verdict" = "ok" ]; then
            # build_target already appended to BUILT_TARGETS/BUILT_ARTIFACTS inside the subshell,
            # so we must rebuild those arrays from what actually exists on disk.
            local art
            if jtag_app_only "$tgt"; then
                BUILT_TARGETS+=("$tgt")
            else
                art=$(target_artifact_path "$tgt")
                if [ -f "$art" ]; then
                    BUILT_TARGETS+=("$tgt"); BUILT_ARTIFACTS+=("$art")
                else
                    BUILD_FAILED=1; FAILED_TARGETS+=("$tgt")
                fi
            fi
        else
            BUILD_FAILED=1
            FAILED_TARGETS+=("$tgt")
        fi
        rm -f "$rf"
    done
    rm -rf "$tmp_base"

    return "$BUILD_FAILED"
}

verify_build_prereqs() {
    if [ "$DEPLOY_ONLY" -eq 1 ] || [ "$CHECK_SUPPORT" -eq 1 ]; then
        return 0
    fi
    local target
    for target in "${TARGETS[@]}"; do
        if target_needs_build_tools "$target" \
           && [ -z "$BUILD_TOOLS_DIR" ] \
           && [ "$CUSTOM_IMAGE" -eq 0 ]; then
            die "Target ${target} requires external build tools. Pass --build-tools-dir DIR (or set BUILD_TOOL_TOOLS_DIR), or use a custom --image that already contains them."
        fi
    done
}

# _artifact_checksum FILE — sha256 hex string, or empty if unavailable
_artifact_checksum() {
    local file=$1
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$file" | awk '{print $1}'
    else
        printf ''
    fi
}

print_summary() {
    local artifact size cksum
    [ "${#BUILT_ARTIFACTS[@]}" -eq 0 ] && return 0

    if [ "$DRY_RUN" -eq 1 ]; then
        log_section "Artifacts a real run would build"
    else
        log_section "Built artifacts"
    fi
    for artifact in "${BUILT_ARTIFACTS[@]}"; do
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '  %s\n' "$artifact"
        elif [ -f "$artifact" ]; then
            size=$(wc -c <"$artifact" | tr -d ' ')
            cksum=$(_artifact_checksum "$artifact")
            if [ -n "$cksum" ]; then
                printf '  %-40s  %10s bytes  sha256:%s\n' "$artifact" "$size" "$cksum"
            else
                printf '  %-40s  %10s bytes\n' "$artifact" "$size"
            fi
        fi
    done
}
