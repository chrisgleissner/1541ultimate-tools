#!/usr/bin/env bash
# deploy.sh — FTP deployment to Ultimate devices

normalize_deploy_path() {
    case "$DEPLOY_PATH" in /*) ;; *) DEPLOY_PATH="/$DEPLOY_PATH" ;; esac
    case "$DEPLOY_PATH" in */) ;; *) DEPLOY_PATH="${DEPLOY_PATH}/" ;; esac
}

normalize_remote_directory_path() {
    local remote_path=$1

    case "$remote_path" in /*) ;; *) remote_path="/$remote_path" ;; esac
    case "$remote_path" in */) ;; *) remote_path="${remote_path}/" ;; esac
    printf '%s' "$remote_path"
}

remote_path_exists() {
    local remote_host=$1 remote_path=$2
    run_command curl --fail --show-error --silent \
        --user "${DEPLOY_USER}:${DEPLOY_PASS}" \
        --list-only "ftp://${remote_host}${remote_path}" >/dev/null
}

resolve_deploy_path() {
    local target=$1
    local preferred_path

    if [ "$DEPLOY_PATH_EXPLICIT" -eq 1 ]; then
        printf '%s' "$DEPLOY_PATH"
        return 0
    fi

    preferred_path=$(target_default_deploy_path "$target")
    normalize_remote_directory_path "$preferred_path"
}

ensure_remote_path() {
    local remote_host=$1
    local remote_path current_path segment

    remote_path=$(normalize_remote_directory_path "$2")

    if [ "$DRY_RUN" -eq 1 ]; then
        log_info "Would ensure remote directory ${remote_path} on ${remote_host}"
        return 0
    fi

    if remote_path_exists "$remote_host" "$remote_path"; then
        return 0
    fi

    current_path=""
    while IFS= read -r segment; do
        [ -n "$segment" ] || continue
        current_path="${current_path}/${segment}"
        log_info "$(quote_command curl --silent --show-error --user "${DEPLOY_USER}:${DEPLOY_PASS}" --quote "MKD ${current_path}" "ftp://${remote_host}/")"
        curl --silent --show-error \
            --user "${DEPLOY_USER}:${DEPLOY_PASS}" \
            --quote "MKD ${current_path}" \
            "ftp://${remote_host}/" >/dev/null 2>&1 || true
    done <<EOF
$(printf '%s' "$remote_path" | tr '/' '\n')
EOF

    if remote_path_exists "$remote_host" "$remote_path"; then
        log_info "Remote directory ready: ${remote_path}"
        return 0
    fi

    log_warn "Could not confirm remote directory ${remote_path} on ${remote_host}; continuing with FTP upload attempt."
    return 1
}

# validate_deploy_file — --deploy-file belongs to one --deploy-only upload
validate_deploy_file() {
    [ -n "$DEPLOY_FILE" ] || return 0
    [ "$DEPLOY_ONLY" -eq 1 ] \
        || die_usage "--deploy-file needs --deploy-only."
    [ "${#DEPLOY_TARGETS[@]}" -gt 0 ] \
        || die_usage "--deploy-file needs a --deploy TARGET."
    [ "$(resolve_target_list "${DEPLOY_TARGETS[@]}" | wc -l)" -eq 1 ] \
        || die_usage "--deploy-file can only be used with a single --deploy target."
    [ -s "$DEPLOY_FILE" ] \
        || die "--deploy-file ${DEPLOY_FILE} does not exist or is empty."
    case "$(basename "$DEPLOY_FILE")" in
        *[!A-Za-z0-9._-]*|"")
            die "--deploy-file name '$(basename "$DEPLOY_FILE")' may only contain letters, digits, '.', '_' and '-': it becomes the remote file name."
            ;;
    esac
}

# resolve_deploy_artifact TARGET — print the local file to upload, or explain
# on stderr and return 1. The file is the one built for the commit at HEAD
# (update_<version>.<ext>), or the one named with --deploy-file. A file left
# over from another commit is never picked up on its own.
resolve_deploy_artifact() {
    local target=$1 versioned other suffix

    if [ -n "$DEPLOY_FILE" ]; then
        printf '%s' "$DEPLOY_FILE"
        return 0
    fi

    versioned=$(target_artifact_path "$target")
    if [ -f "$versioned" ]; then
        printf '%s' "$versioned"
        return 0
    fi
    # A dry run that includes the build has not produced the file yet.
    if [ "$DRY_RUN" -eq 1 ] && [ "$DEPLOY_ONLY" -eq 0 ]; then
        printf '%s' "$versioned"
        return 0
    fi

    suffix=${versioned##*.}
    other=$(find . -maxdepth 1 -type f \( -name "update_*.${suffix}" -o -name "update.${suffix}" \) \
                -printf '%P\n' | sort | tail -n 1)
    if [ -n "$other" ]; then
        log_error "No ${versioned} for ${target} (version ${VERSION_STRING}). Found ${other}, which is not from this commit."
        log_error "Build this commit, or upload ${other} deliberately with: --deploy-only -d ${target} --deploy-file ${other}"
    else
        log_error "No local artifact ${versioned} for ${target}. Build it first."
    fi
    return 1
}

deploy_target() {
    local target=$1
    local artifact remote_name remote_path remote_url

    if [ -z "$DEPLOY_HOST" ]; then
        die_usage "--deploy-host is required for FTP deployment."
    fi

    if [ "$DEPLOY_ONLY" -eq 0 ] && ! contains_value "$target" "${BUILT_TARGETS[@]+"${BUILT_TARGETS[@]}"}"; then
        DEPLOY_FAILED=1
        FAILED_DEPLOYS+=("$target")
        log_error "Skipping deploy for ${target}: its build did not succeed in this run."
        return 1
    fi

    if ! artifact=$(resolve_deploy_artifact "$target"); then
        DEPLOY_FAILED=1
        FAILED_DEPLOYS+=("$target")
        return 1
    fi

    # The remote file is named like the local one, so a device never holds a
    # file whose name claims another commit.
    remote_name=$(basename "$artifact")
    remote_path=$(resolve_deploy_path "$target")
    ensure_remote_path "$DEPLOY_HOST" "$remote_path" || true
    remote_url="ftp://${DEPLOY_HOST}${remote_path}${remote_name}"

    CURRENT_ACTION="Deploying ${target} via FTP"
    if ! run_command curl --fail --show-error --silent \
            --ftp-create-dirs \
            --user "${DEPLOY_USER}:${DEPLOY_PASS}" \
            -T "$artifact" "$remote_url"; then
        DEPLOY_FAILED=1
        FAILED_DEPLOYS+=("$target")
        log_error "FTP upload failed for ${target}: ${remote_url}"
        CURRENT_ACTION=""
        return 1
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        log_info "Would upload ${artifact} -> ${remote_url}"
    else
        log_success "Uploaded ${artifact} -> ${remote_url}"
        printf '  Trigger the update on the device from the firmware update menu.\n'
    fi
    CURRENT_ACTION=""
    return 0
}
