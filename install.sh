#!/usr/bin/env bash
# Install the tools into a 1541ultimate checkout.
#
# Usage:
#   install.sh [--dry-run] CHECKOUT
#
# Copies build, build.cmd, build-tool, build-tool.d/, .build-tool.env.example
# and the tools in tooling/ (not their tests) into CHECKOUT, makes the scripts
# executable, records the installed version in CHECKOUT/.1541ultimate-tools-version
# and lists every installed path in the checkout's .git/info/exclude, so git
# never offers them for a commit. Nothing else from this repository is copied:
# not the documentation, the logo, the tests or vivado/.
#
# Running it again, for example with a newer release, overwrites the installed
# files and adds no duplicate exclude entries. Other files in tooling/ are left
# alone. The checkout's tracked files are never changed.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
log() { printf '%s\n' "$*"; }

DRY_RUN=0
CHECKOUT=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option $1" ;;
        *) [[ -z $CHECKOUT ]] || die "only one checkout, please"; CHECKOUT=$1 ;;
    esac
    shift
done
[[ -n $CHECKOUT ]] || die "usage: install.sh [--dry-run] CHECKOUT"
[[ -d $CHECKOUT ]] || die "$CHECKOUT is not a directory"
CHECKOUT="$(cd "$CHECKOUT" && pwd -P)"
[[ $CHECKOUT != "$HERE" ]] || die "CHECKOUT is this repository; give the 1541ultimate checkout"
git -C "$CHECKOUT" rev-parse --git-dir >/dev/null 2>&1 \
    || die "$CHECKOUT is not a git checkout"
[[ -d $CHECKOUT/software && -d $CHECKOUT/target ]] \
    || die "$CHECKOUT does not look like a 1541ultimate checkout (no software/ and target/)"

VERSION="$(cat "$HERE/VERSION")"

# What is installed, relative to this repository and to CHECKOUT alike.
FILES=(build build.cmd build-tool .build-tool.env.example)
while IFS= read -r path; do
    FILES+=("${path#"$HERE"/}")
done < <(find "$HERE/build-tool.d" -type f -name '*.sh' | sort)
while IFS= read -r path; do
    FILES+=("${path#"$HERE"/}")
done < <(find "$HERE/tooling" -maxdepth 1 -type f \( -name '*.sh' -o -name '*.py' \) \
             ! -name 'test_*' | sort)

# The paths git must ignore in CHECKOUT; tooling/ holds local files as well.
EXCLUDES=(build build.cmd build-tool build-tool.d/ tooling/ .build-tool.env
          .build-tool.env.example .1541ultimate-tools-version)

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf 'would run:'; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

log "Installing 1541ultimate-tools $VERSION into $CHECKOUT"
for file in "${FILES[@]}"; do
    run mkdir -p "$CHECKOUT/$(dirname "$file")"
    run cp "$HERE/$file" "$CHECKOUT/$file"
    case $file in
        *.sh|*.py|build|build-tool) run chmod +x "$CHECKOUT/$file" ;;
    esac
done

exclude="$(git -C "$CHECKOUT" rev-parse --git-path info/exclude)"
[[ $exclude = /* ]] || exclude="$CHECKOUT/$exclude"
added=0
for entry in "${EXCLUDES[@]}"; do
    if ! grep -qxF -- "$entry" "$exclude" 2>/dev/null; then
        if [[ $DRY_RUN -eq 1 ]]; then
            log "would add to $exclude: $entry"
        else
            mkdir -p "$(dirname "$exclude")"
            printf '%s\n' "$entry" >> "$exclude"
        fi
        added=$((added + 1))
    fi
done

if [[ $DRY_RUN -eq 1 ]]; then
    log "would write $CHECKOUT/.1541ultimate-tools-version: $VERSION"
    exit 0
fi
printf '%s\n' "$VERSION" > "$CHECKOUT/.1541ultimate-tools-version"

log "Installed ${#FILES[@]} files; added $added entries to $exclude"
# The overlay must not show up in git; anything listed here was there before.
untracked="$(git -C "$CHECKOUT" status --porcelain --untracked-files=all -- \
             "${EXCLUDES[@]%/}" 2>/dev/null || true)"
if [[ -n $untracked ]]; then
    printf 'install.sh: git still sees these paths:\n%s\n' "$untracked" >&2
    exit 1
fi
log "Done. Try: cd $CHECKOUT && ./build-tool --list-targets"
