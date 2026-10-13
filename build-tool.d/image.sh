#!/usr/bin/env bash
# image.sh — local Docker image preparation and FPGA build-tools detection

detect_default_build_tools_dir() {
    [ -n "$BUILD_TOOLS_DIR" ] && return 0

    local candidate
    for candidate in \
        "$HOME/altera_lite" "$HOME/intelFPGA_lite" \
        "$HOME/xilinx" "$HOME/Xilinx" \
        "$HOME/Lattice" "$HOME/lattice" "$HOME/lscc"
    do
        if [ -d "$candidate" ]; then
            BUILD_TOOLS_DIR="$HOME"
            log_info "Auto-detected build tools directory: $BUILD_TOOLS_DIR"
            return 0
        fi
    done

    for candidate in \
        "$HOME/altera_lite/18.1" \
        "$HOME/intelFPGA_lite/18.1" \
        "$HOME/intelFPGA_lite/18.1-cyclone" \
        "/opt/build-tools" \
        "/opt/altera_lite/18.1" \
        "/opt/intelFPGA_lite/18.1"
    do
        if [ -d "$candidate/quartus" ] \
           || [ -d "$candidate/altera_lite/18.1/quartus" ] \
           || [ -d "$candidate/intelFPGA_lite/18.1/quartus" ]; then
            BUILD_TOOLS_DIR="$candidate"
            log_info "Auto-detected build tools directory: $BUILD_TOOLS_DIR"
            return 0
        fi
    done
}

ensure_build_tools_dir() {
    [ -z "$BUILD_TOOLS_DIR" ] && return 0
    [ -d "$BUILD_TOOLS_DIR" ] || die_usage "Build tools directory does not exist: $BUILD_TOOLS_DIR"
}

ensure_lattice_license_file() {
    [ -z "${LATTICE_LICENSE_FILE:-}" ] && return 0
    [ -f "$LATTICE_LICENSE_FILE" ] && [ -r "$LATTICE_LICENSE_FILE" ] \
        || die_usage "LATTICE_LICENSE_FILE must name a readable license file."
    LATTICE_LICENSE_FILE=$(realpath -- "$LATTICE_LICENSE_FILE")
    export LATTICE_LICENSE_FILE
}

prepare_image() {
    [ "$CUSTOM_IMAGE" -eq 1 ] && return 0

    local target needs_lattice=0
    for target in "${TARGETS[@]}"; do
        target_requires_lattice "$target" && needs_lattice=1
    done

    # The legacy image lacks the runtime libraries Diamond loads, so an ECP5
    # build always uses the prepared image.
    if [ "$needs_lattice" -eq 0 ] \
       && docker image inspect my_docker_image >/dev/null 2>&1; then
        DOCKER_IMAGE="my_docker_image"
        log_info "Using local legacy build image: $DOCKER_IMAGE"
        return 0
    fi

    CURRENT_ACTION="Preparing Docker image"
    if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
        run_command docker pull "$BASE_IMAGE"
    fi

    local base_image_id local_label local_revision_label
    base_image_id=$(docker image inspect --format '{{.Id}}' "$BASE_IMAGE")
    local_label=$(docker image inspect \
        --format '{{ index .Config.Labels "org.1541ultimate.base-image-id" }}' \
        "$DOCKER_IMAGE" 2>/dev/null || true)
    local_revision_label=$(docker image inspect \
        --format '{{ index .Config.Labels "org.1541ultimate.prepared-image-revision" }}' \
        "$DOCKER_IMAGE" 2>/dev/null || true)

    if [ "$local_label" = "$base_image_id" ] \
       && [ "$local_revision_label" = "$PREPARED_IMAGE_REVISION" ]; then
        CURRENT_ACTION=""
        return 0
    fi

    if docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
        log_info "Rebuilding $DOCKER_IMAGE because its base image changed."
    else
        log_info "Creating $DOCKER_IMAGE from $BASE_IMAGE."
    fi

    local container_name="1541u-build-$RANDOM-$$"
    run_command docker create \
        --name "$container_name" --entrypoint /bin/bash \
        "$BASE_IMAGE" -lc '
set -e
apt-get update
apt-get install -y locales libglib2.0-0 libice6 libsm6 libx11-6 libxext6 libxrender1 \
    libgl1 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
    libxcomposite1 libgraphite2-3
locale-gen en_US.UTF-8
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
$IDF_PATH/install.sh esp32 esp32c3 esp32s3
rm -rf "$IDF_TOOLS_PATH"/dist/*
rm -rf /var/lib/apt/lists/*
'
    if [ "$DRY_RUN" -eq 0 ]; then
        docker start -a "$container_name"
        docker commit \
            -c "ENV LANG=en_US.UTF-8" \
            -c "ENV LC_ALL=en_US.UTF-8" \
            -c "LABEL org.1541ultimate.base-image-id=$base_image_id" \
            -c "LABEL org.1541ultimate.prepared-image-revision=$PREPARED_IMAGE_REVISION" \
            "$container_name" "$DOCKER_IMAGE" >/dev/null
        docker rm -f "$container_name" >/dev/null
    fi
    CURRENT_ACTION=""
}
