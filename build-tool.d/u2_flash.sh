#!/usr/bin/env bash
# u2_flash.sh — thin wrapper around tooling/u2_flash.py for U2+L
# REST-based firmware installs (upload + REST-driven menu flash + safe
# power-cycle). All real logic lives in the Python script; this wrapper
# only resolves the build-tool-native defaults (local artifact, branch,
# git id) and passes everything else through.

run_u2_flash() {
    require_command python3

    local script="$TOOLING_DIR/u2_flash.py"
    [ -f "$script" ] || die_usage "U2+L flash script not found: $script"

    local artifact
    artifact=$(find_existing_artifact u2pl 2>/dev/null || true)

    local branch git_id
    branch=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
    git_id=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)

    local cmd=(python3 "$script" --repo-root "$REPO_DIR" --branch "$branch" --git-id "$git_id")
    [ -n "$artifact" ] && cmd+=(--artifact "$REPO_DIR/$artifact")
    [ "$DRY_RUN" -eq 1 ] && cmd+=(--dry-run)

    cmd+=("$@")

    log_info "$(quote_command "${cmd[@]}")"
    "${cmd[@]}"
}
