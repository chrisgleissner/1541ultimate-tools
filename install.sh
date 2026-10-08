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
# not the documentation, the logo, the tests, vivado/ or patches/.
#
# Running it again, for example with a newer release, overwrites the installed
# files and adds no duplicate exclude entries. Other files in tooling/ are left
# alone. A tracked file of the checkout is never overwritten: the install stops
# before copying anything if one of the tool paths is tracked there.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

DRY_RUN=0
CHECKOUT=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,18p' "$HERE/install.sh" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option $1" ;;
        *) [[ -z $CHECKOUT ]] || die "only one checkout, please"; CHECKOUT=$1 ;;
    esac
    shift
done
[[ -n $CHECKOUT ]] || die "usage: install.sh [--dry-run] CHECKOUT"
[[ -d $CHECKOUT ]] || die "$CHECKOUT is not a directory"
CHECKOUT="$(cd "$CHECKOUT" && pwd -P)"
[[ $CHECKOUT != "$HERE" ]] || die "CHECKOUT is this repository; give the 1541ultimate checkout"
top="$(git -C "$CHECKOUT" rev-parse --show-toplevel 2>/dev/null)" \
    || die "$CHECKOUT is not a git checkout"
[[ "$(cd "$top" && pwd -P)" == "$CHECKOUT" ]] \
    || die "$CHECKOUT is inside the checkout $top; give its top directory"
[[ -d $CHECKOUT/software && -d $CHECKOUT/target ]] \
    || die "$CHECKOUT does not look like a 1541ultimate checkout (no software/ and target/)"

# A release archive has no .git; a clone of this repository records its commit.
VERSION="$(cat "$HERE/VERSION")"
if commit="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null)"; then
    VERSION="$VERSION+g$commit"
fi

# What is installed, relative to this repository and to CHECKOUT alike.
FILES=(build build.cmd build-tool .build-tool.env.example)
while IFS= read -r path; do
    FILES+=("${path#"$HERE"/}")
done < <(find "$HERE/build-tool.d" -type f -name '*.sh' | sort)
while IFS= read -r path; do
    FILES+=("${path#"$HERE"/}")
done < <(find "$HERE/tooling" -maxdepth 1 -type f \( -name '*.sh' -o -name '*.py' \) \
             ! -name 'test_*' | sort)

# The paths git must ignore in CHECKOUT, anchored at its top so that a
# directory called build or tooling deeper in the tree stays visible.
EXCLUDES=(/build /build.cmd /build-tool /build-tool.d/ /tooling/ /.build-tool.env
          /.build-tool.env.example /.1541ultimate-tools-version)

tracked="$(git -C "$CHECKOUT" ls-files -- "${FILES[@]}" .1541ultimate-tools-version)"
[[ -z $tracked ]] || die "the checkout tracks these tool paths, so they are not overwritten:
$tracked"

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '[install] would run:'; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

log "installing 1541ultimate-tools $VERSION into $CHECKOUT"

# The exclude entries go first, so that a failed copy leaves nothing for git to see.
exclude="$(git -C "$CHECKOUT" rev-parse --git-path info/exclude)"
[[ $exclude = /* ]] || exclude="$CHECKOUT/$exclude"
missing=()
for entry in "${EXCLUDES[@]}"; do
    grep -qxF -- "$entry" "$exclude" 2>/dev/null || missing+=("$entry")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
        log "would add to $exclude: ${missing[*]}"
    else
        mkdir -p "$(dirname "$exclude")"
        # An exclude file without a final newline would glue our first entry
        # onto its last line.
        if [[ -s $exclude && -n "$(tail -c1 "$exclude")" ]]; then
            printf '\n' >> "$exclude"
        fi
        printf '%s\n' "${missing[@]}" >> "$exclude"
    fi
fi

for file in "${FILES[@]}"; do
    run mkdir -p "$CHECKOUT/$(dirname "$file")"
    run cp -f "$HERE/$file" "$CHECKOUT/$file"
    case $file in
        *.sh|*.py|build|build-tool) run chmod +x "$CHECKOUT/$file" ;;
    esac
done

if [[ $DRY_RUN -eq 1 ]]; then
    log "would write $CHECKOUT/.1541ultimate-tools-version: $VERSION"
    exit 0
fi
printf '%s\n' "$VERSION" > "$CHECKOUT/.1541ultimate-tools-version"

log "installed ${#FILES[@]} files; added ${#missing[@]} entries to $exclude"
visible="$(git -C "$CHECKOUT" status --porcelain --untracked-files=all -- \
           "${FILES[@]}" .1541ultimate-tools-version)"
[[ -z $visible ]] || die "git still sees these paths:
$visible"
log "done; next: cd $(printf '%q' "$CHECKOUT") && ./build-tool --check-support"
