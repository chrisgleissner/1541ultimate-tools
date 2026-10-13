#!/usr/bin/env bash
# docker.sh — Docker interaction: run scripts inside the build container

# Container bootstrap script injected into every docker run call.
# Sets up PATH for FPGA/Nios/Xilinx toolchains found under BUILD_TOOLS_ROOT.
CONTAINER_PRELUDE=$(cat <<'EOF'
set -euo pipefail

container_fail() {
    echo "ERROR: $*" >&2
    exit 1
}

append_path_if_dir() {
    if [ -d "$1" ]; then
        PATH="$1:$PATH"
        export PATH
    fi
}

detect_xilinx_license_file() {
    local root="${BUILD_TOOLS_ROOT:-}"
    local candidate found=""

    if [ -n "${XILINXD_LICENSE_FILE:-}" ] || [ -n "${LM_LICENSE_FILE:-}" ]; then
        return 0
    fi
    [ -n "$root" ] || return 0

    for candidate in \
        "$root/dev/c64/1541ultimate-workspace/tooling/VivaldoISALicense/Xilinx.lic" \
        "$root/.Xilinx/Xilinx.lic" \
        "$root/.Xilinx/license.lic" \
        "$root/Xilinx/Xilinx.lic" \
        "$root/xilinx/Xilinx.lic" \
        "$root/Downloads/Xilinx.lic"
    do
        if [ -f "$candidate" ]; then
            found="$candidate"
            break
        fi
    done

    if [ -z "$found" ] && [ -d "$root/.Xilinx" ]; then
        found=$(find "$root/.Xilinx" -maxdepth 3 -type f \
                    \( -name '*.lic' -o -name 'Xilinx.lic' \) | head -n 1 || true)
    fi

    if [ -z "$found" ] && [ -d "$root/dev/c64/1541ultimate-workspace/tooling" ]; then
        found=$(find "$root/dev/c64/1541ultimate-workspace/tooling" -maxdepth 4 -type f \
                    \( -name '*.lic' -o -name 'Xilinx.lic' \) | head -n 1 || true)
    fi

    if [ -n "$found" ]; then
        export XILINXD_LICENSE_FILE="$found"
        export LM_LICENSE_FILE="$found"
    fi
}

configure_build_tools() {
    local root="${BUILD_TOOLS_ROOT:-}"
    local candidate quartus_base="" lattice_base="" xilinx_root="" xilinx_platform=""
    local riscv_base="" riscv_version="" riscv_target="" riscv_banner="" riscv_sha=""

    [ -n "$root" ] || return 0

    riscv_base="$root/riscv"
    # A RISC-V toolchain under <build tools>/riscv takes precedence over the
    # image's own, and is then required to be the build upstream CI uses. With
    # none mounted, the image's toolchain builds the RISC-V targets.
    if [ "${REQUIRE_RISCV:-0}" = "1" ] && [ -x "$riscv_base/bin/riscv32-unknown-elf-g++" ]; then
        riscv_version=$("$riscv_base/bin/riscv32-unknown-elf-g++" \
            -dumpfullversion -dumpversion)
        [ "$riscv_version" = "10.2.0" ] \
            || container_fail "RISC-V GCC ${riscv_version} does not match CI GCC 10.2.0."
        riscv_target=$("$riscv_base/bin/riscv32-unknown-elf-g++" -dumpmachine)
        [ "$riscv_target" = "riscv32-unknown-elf" ] \
            || container_fail "RISC-V target ${riscv_target} does not match the pinned toolchain."
        riscv_banner=$("$riscv_base/bin/riscv32-unknown-elf-g++" --version | head -n 1)
        case "$riscv_banner" in
            "riscv32-unknown-elf-g++ (GCC) 10.2.0") ;;
            *) container_fail "RISC-V GCC is not the pinned CI-compatible build." ;;
        esac
        riscv_sha=$(sha256sum "$riscv_base/bin/riscv32-unknown-elf-g++" | cut -d' ' -f1)
        [ "$riscv_sha" = "85cb6aba16e943239047a41ca19af24001c12ea33f4c9fae1c7bf4c4e31add80" ] \
            || container_fail "RISC-V GCC binary does not match the pinned CI toolchain."
        append_path_if_dir "$riscv_base/bin"
    fi

    for candidate in \
        "$root" \
        "$root/altera_lite/18.1" \
        "$root/intelFPGA_lite/18.1" \
        "$root/intelFPGA/18.1" \
        "$root/Quartus/18.1" \
        "$root/quartus/18.1"
    do
        if [ -d "$candidate/quartus" ]; then
            quartus_base="$candidate"
            break
        fi
    done

    if [ -n "$quartus_base" ]; then
        export QUARTUS_ROOTDIR="$quartus_base/quartus"
        export QSYS_ROOTDIR="$QUARTUS_ROOTDIR/sopc_builder/bin"
        [ -d "$quartus_base/hld" ] && export ALTERAOCLSDKROOT="$quartus_base/hld"
        append_path_if_dir "$QUARTUS_ROOTDIR"
        append_path_if_dir "$QUARTUS_ROOTDIR/bin"
        append_path_if_dir "$quartus_base/nios2eds/bin"
        append_path_if_dir "$quartus_base/nios2eds/sdk2/bin"
        append_path_if_dir "$quartus_base/nios2eds/bin/gnu/H-x86_64-pc-linux-gnu/bin"
        append_path_if_dir "$quartus_base/nios2eds/bin/gnu/H-i686-pc-linux-gnu/bin"
        append_path_if_dir "$QSYS_ROOTDIR"
    fi

    for candidate in \
        "$root/lscc/diamond/3.13" \
        "$root/Lattice/Diamond/3.13" \
        "$root/lattice/diamond/3.13" \
        "$root/Diamond/3.13" \
        "$root/diamond/3.13" \
        "$root/diamond"
    do
        if [ -d "$candidate/bin/lin64" ] || [ -d "$candidate/bin/linux" ]; then
            lattice_base="$candidate"
            break
        fi
    done

    if [ -n "$lattice_base" ]; then
        append_path_if_dir "$lattice_base/bin/lin64"
        append_path_if_dir "$lattice_base/bin/linux"
        # A strategy naming this node list runs its three PAR seeds concurrently.
        printf '[%s]\nSYSTEM = linux\nCORENUM = 3\n' "$(hostname)" > /tmp/par_nodelist.txt
    fi

    for candidate in \
        "$root/Xilinx/13.2/ISE_DS/ISE" \
        "$root/Xilinx/14.7/ISE_DS/ISE" \
        "$root/xilinx/13.2/ISE_DS/ISE" \
        "$root/xilinx/14.7/ISE_DS/ISE"
    do
        if [ -d "$candidate/bin/lin64" ] || [ -d "$candidate/bin/lin" ]; then
            xilinx_root="$candidate"
            break
        fi
    done

    if [ -n "$xilinx_root" ]; then
        if [ -d "$xilinx_root/bin/lin64" ]; then
            xilinx_platform="lin64"
        else
            xilinx_platform="lin"
        fi
        cat >/tmp/1541u-ise-locations.inc <<EOF2
export ISE_LOCATIONS_FILE_INCLUDED=yes
export ISE_LOCATIONS_FILE_USER=yes
PLATFORM=${xilinx_platform}
export CYGXILINX=${xilinx_root}
    export XILINX=${xilinx_root}
export XST=${xilinx_root}/bin/\${PLATFORM}/xst
export NGDBUILD=${xilinx_root}/bin/\${PLATFORM}/ngdbuild
export MAP=${xilinx_root}/bin/\${PLATFORM}/map
export PAR=${xilinx_root}/bin/\${PLATFORM}/par
export TRCE=${xilinx_root}/bin/\${PLATFORM}/trce
export BITGEN=${xilinx_root}/bin/\${PLATFORM}/bitgen
export CPLDFIT=${xilinx_root}/bin/\${PLATFORM}/cpldfit
export HPREP6=${xilinx_root}/bin/\${PLATFORM}/hprep6
export PROMGEN=${xilinx_root}/bin/\${PLATFORM}/promgen
export FPGA_EDITOR=${xilinx_root}/bin/\${PLATFORM}/fpga_editor
export TIMINGAN=${xilinx_root}/bin/\${PLATFORM}/timingan
export TAENGINE=${xilinx_root}/bin/\${PLATFORM}/taengine
export TSIM=${xilinx_root}/bin/\${PLATFORM}/tsim
export NETGEN=${xilinx_root}/bin/\${PLATFORM}/netgen
export CPS_INS=${xilinx_root}/bin/\${PLATFORM}/cps
EOF2
        export ISE_LOCATIONS_FILE_PATH=/tmp/1541u-ise-locations.inc
    fi

    detect_xilinx_license_file

    if [ "${REQUIRE_NIOS:-0}" = "1" ] && [ -z "${QUARTUS_ROOTDIR:-}" ]; then
        container_fail "Quartus/Nios tools were not detected under ${root}."
    fi
    if [ "${REQUIRE_XILINX:-0}" = "1" ] && [ -z "${ISE_LOCATIONS_FILE_PATH:-}" ]; then
        container_fail "Xilinx ISE tools were not detected under ${root}."
    fi
    if [ "${REQUIRE_XILINX:-0}" = "1" ] && [ -z "${XILINXD_LICENSE_FILE:-}" ] && \
            [ -z "${LM_LICENSE_FILE:-}" ]; then
        container_fail "Xilinx ISE license was not detected. Set XILINXD_LICENSE_FILE or place Xilinx.lic in tooling/VivaldoISALicense."
    fi
    if [ "${REQUIRE_LATTICE:-0}" = "1" ] && [ -z "$lattice_base" ]; then
        container_fail "Lattice Diamond tools were not detected under ${root}."
    fi
}

configure_build_tools
EOF
)

# _docker_base_cmd — build the common docker run prefix into an array
_docker_base_cmd() {
    local cmd=(docker run --rm --entrypoint /bin/bash -v "${REPO_DIR}":/__w)
    local host_uid host_gid common_dir

    # In a git worktree, .git points at the main checkout's git directory,
    # which the container cannot see; git-derived version strings then come
    # out empty. Mount that directory at the same path, read-only.
    common_dir=$(git -C "$REPO_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
    case "$common_dir" in
        ""|"$REPO_DIR"/*) ;;
        *) cmd+=(-v "$common_dir":"$common_dir":ro) ;;
    esac

    if [ "$DOCKER_IS_ROOTLESS" -eq 0 ]; then
        host_uid=$(id -u)
        host_gid=$(id -g)
        cmd+=(--user "${host_uid}:${host_gid}")
    fi
    cmd+=(-e HOME=/tmp/1541u-home)

    if [ -n "${LATTICE_LICENSE_FILE:-}" ]; then
        # Preserve the physical host ID used by the node-locked license.
        cmd+=(--network host
              -v "$LATTICE_LICENSE_FILE":/opt/lattice-license/license.lic:ro
              -e LATTICE_LICENSE_FILE=/opt/lattice-license/license.lic
              -e LM_LICENSE_FILE=/opt/lattice-license/license.lic)
    fi

    if [ -n "$BUILD_TOOLS_DIR" ]; then
        cmd+=(-v "$BUILD_TOOLS_DIR":/mnt/build-tools:ro -e BUILD_TOOLS_ROOT=/mnt/build-tools)
    fi
    printf '%s\0' "${cmd[@]}"
}

# docker_run_build_script SCRIPT_BODY REQUIRE_NIOS REQUIRE_XILINX REQUIRE_LATTICE [NEEDS_IDF] [REQUIRE_RISCV]
docker_run_build_script() {
    local script_body=$1 require_nios=$2 require_xilinx=$3 require_lattice=$4
    local needs_idf=${5:-1} require_riscv=${6:-0} idf_setup=""
    local cmd=()

    if [ "$needs_idf" -eq 1 ]; then
        idf_setup='source "$IDF_PATH/export.sh"'
    fi

    while IFS= read -r -d '' token; do
        cmd+=("$token")
    done < <(_docker_base_cmd)

    cmd+=(-e REQUIRE_NIOS="$require_nios"
          -e REQUIRE_XILINX="$require_xilinx"
          -e REQUIRE_LATTICE="$require_lattice"
          -e REQUIRE_RISCV="$require_riscv")
    cmd+=("$DOCKER_IMAGE" -lc "set -euo pipefail
mkdir -p \"\$HOME\"
${idf_setup}
${script_body}")
    run_command "${cmd[@]}"
}

# capture_docker_script_output SCRIPT_BODY — runs quietly and captures stdout
capture_docker_script_output() {
    local script_body=$1
    local cmd=()

    while IFS= read -r -d '' token; do
        cmd+=("$token")
    done < <(_docker_base_cmd)

    cmd+=(-e REQUIRE_NIOS=0 -e REQUIRE_XILINX=0 -e REQUIRE_LATTICE=0 -e REQUIRE_RISCV=1)
    cmd+=("$DOCKER_IMAGE" -lc "set -euo pipefail
mkdir -p \"\$HOME\"
if [ -n \"\${IDF_PATH:-}\" ] && [ -f \"\$IDF_PATH/export.sh\" ]; then
    source \"\$IDF_PATH/export.sh\" >/dev/null 2>&1 || true
fi
${CONTAINER_PRELUDE}
cd /__w
${script_body}")
    "${cmd[@]}"
}

ensure_docker_ready() {
    require_command docker
    CURRENT_ACTION="Checking Docker daemon"
    if [ "$DRY_RUN" -eq 0 ]; then
        docker info >/dev/null 2>&1
        if docker info --format '{{json .SecurityOptions}}' 2>/dev/null | grep -qi rootless; then
            DOCKER_IS_ROOTLESS=1
        fi
    fi
    CURRENT_ACTION=""
}
