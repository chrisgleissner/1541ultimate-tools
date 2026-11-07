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
# never offers them for a commit. It also installs the agent guide as
# CHECKOUT/AGENTS.md and CHECKOUT/CLAUDE.md, and the guide's procedures in
# CHECKOUT/tooling/docs/agents/. Nothing else from this repository is copied:
# not the other documentation, the logo, the tests or vivado/.
#
# Running it again, for example with a newer release, overwrites the installed
# files and adds no duplicate exclude entries. Other files in tooling/ are left
# alone. A tracked file of the checkout is never overwritten: the install stops
# before copying anything if one of the tool paths is tracked there.
#
# The guide files get more care, because people keep their own. A tracked
# AGENTS.md or CLAUDE.md is left alone with a warning. An untracked one that
# equals a shipped copy, or the copy of the previous install, is replaced. Any
# other untracked one (local edits, a hand-written guide) is first moved to
# NAME.local-<YYYYmmdd-HHMMSS>, which git ignores too. AGENTS.local.md, the
# place for machine-specific notes, is only added to the excludes; it is never
# created, changed, moved or deleted.
#
# The agent skills in skills/ are installed as CHECKOUT/.claude/skills/NAME/ and
# CHECKOUT/.agents/skills/NAME/, with each skill's own anchored exclude entries,
# so other skills in those directories stay visible to git. The install stops
# before copying anything if the checkout tracks one of the skill paths. An
# untracked SKILL.md is handled like the guide files: replaced when it equals
# the shipped copy or the previous install's copy, and otherwise moved to
# SKILL.md.local-<YYYYmmdd-HHMMSS> in the same directory first.
#
# Files that an earlier release installed and this repository no longer ships
# are listed in RETIRED, with the SHA-256 of every version that was shipped. An
# install removes such a file from CHECKOUT and reports it, but only when the
# checkout does not track it and its content equals one of those versions. A
# file that the checkout tracks, or that differs (a local edit, a file of the
# same name from elsewhere), stays and is reported. --dry-run only reports.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

DRY_RUN=0
CHECKOUT=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,42p' "$HERE/install.sh" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

# The guide's procedures: docs/agents/ here, tooling/docs/agents/ in CHECKOUT
# (inside the excluded tooling/ directory).
AGENT_DOCS=tooling/docs/agents
while IFS= read -r path; do
    FILES+=("$AGENT_DOCS/$(basename "$path")")
done < <(find "$HERE/docs/agents" -maxdepth 1 -type f -name '*.md' | sort)

# Where an installed path comes from, relative to this repository.
source_of() {
    case $1 in
        "$AGENT_DOCS"/*) printf 'docs/agents/%s\n' "${1#"$AGENT_DOCS"/}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# The agent skills: skills/NAME/SKILL.md here, installed under both roots in
# CHECKOUT, which Claude Code and other agents read respectively.
SKILL_ROOTS=(.claude/skills .agents/skills)
SKILLS=()
while IFS= read -r path; do
    SKILLS+=("$(basename "$(dirname "$path")")")
done < <(find "$HERE/skills" -mindepth 2 -maxdepth 2 -type f -name SKILL.md | sort)
SKILL_FILES=()
for root in "${SKILL_ROOTS[@]}"; do
    for name in "${SKILLS[@]}"; do SKILL_FILES+=("$root/$name/SKILL.md"); done
done

# The agent guide files, installed from checkout/ at the top of CHECKOUT, and
# the record of what an install wrote (the guide files and the skills), so the
# next install can tell its own unchanged copy from a local edit.
GUIDE=(AGENTS.md CLAUDE.md)
GUIDE_RECORD=.1541ultimate-tools-guide
LOCAL_GUIDE=AGENTS.local.md

# Installed by an earlier release, no longer shipped: "PATH SHA256 [SHA256...]".
# tooling/recover_ultimate.py is not listed: this repository never shipped it.
RETIRED=(
    # removed in #24; the U64 has no JTAG UART, use --uart-monitor u64
    "tooling/read_u64_jtag_terminal.sh 1745a66339db53a5fb6020f35df788b9b09dd68f00cc0776904f405820605394"
)

# The paths git must ignore in CHECKOUT, anchored at its top so that a
# directory called build or tooling deeper in the tree stays visible.
EXCLUDES=(/build /build.cmd /build-tool /build-tool.d/ /tooling/ /.build-tool.env
          /.build-tool.env.example /.1541ultimate-tools-version /$GUIDE_RECORD
          /$LOCAL_GUIDE)
for file in "${SKILL_FILES[@]}"; do EXCLUDES+=("/$(dirname "$file")/"); done

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# The guide links the long tool documents of this repository. The link names
# this release's tag, or the commit of a clone, so it shows what was installed.
case $VERSION in
    *+g*) REF="${VERSION##*+g}" ;;
    *) REF="v$VERSION" ;;
esac
RENDERED="$(mktemp -d)"
trap 'rm -rf "$RENDERED"' EXIT
for name in "${GUIDE[@]}"; do
    sed "s|{{TOOLS_REF}}|$REF|g" "$HERE/checkout/$name" > "$RENDERED/$name"
done
for name in "${SKILLS[@]}"; do
    sed "s|{{TOOLS_REF}}|$REF|g" "$HERE/skills/$name/SKILL.md" > "$RENDERED/skill-$name.md"
done

# What happens to an installed text file REL (a guide file or a skill), given
# its rendered copy: one of
#   install    absent: installed
#   same       present and equal to the shipped copy: nothing to write
#   replace    present and equal to the copy of the previous install
#   backup     present and different: moved aside, then installed
file_action() {
    local rel=$1 rendered=$2 shipped current recorded
    if [[ ! -e $CHECKOUT/$rel && ! -L $CHECKOUT/$rel ]]; then
        echo install
        return
    fi
    shipped="$(sha256 "$rendered")"
    current="$(sha256 "$CHECKOUT/$rel" 2>/dev/null || true)"
    recorded="$(awk -v n="$rel" '$2 == n { print $1; exit }' "$CHECKOUT/$GUIDE_RECORD" 2>/dev/null || true)"
    if [[ $current == "$shipped" ]]; then echo same
    elif [[ -n $recorded && $current == "$recorded" ]]; then echo replace
    else echo backup
    fi
}

# What happens to each guide file; GUIDE_ACTION[i] for GUIDE[i] is one of the
# above or
#   tracked    the checkout tracks it: left alone, a warning
GUIDE_ACTION=()
for i in "${!GUIDE[@]}"; do
    name=${GUIDE[$i]}
    if git -C "$CHECKOUT" ls-files --error-unmatch -- "$name" >/dev/null 2>&1; then
        action=tracked
    else
        action="$(file_action "$name" "$RENDERED/$name")"
    fi
    GUIDE_ACTION+=("$action")
    [[ $action == tracked ]] || EXCLUDES+=("/$name" "/$name.local-*")
done

# The same for each skill file; SKILL_ACTION[i] is for SKILL_FILES[i].
SKILL_ACTION=()
for file in "${SKILL_FILES[@]}"; do
    SKILL_ACTION+=("$(file_action "$file" "$RENDERED/skill-$(basename "$(dirname "$file")").md")")
done

SKILL_DIRS=()
for file in "${SKILL_FILES[@]}"; do SKILL_DIRS+=("$(dirname "$file")"); done
tracked="$(git -C "$CHECKOUT" ls-files -- "${FILES[@]}" .1541ultimate-tools-version "${SKILL_DIRS[@]}")"
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
    run cp -f "$HERE/$(source_of "$file")" "$CHECKOUT/$file"
    case $file in
        *.sh|*.py|build|build-tool) run chmod +x "$CHECKOUT/$file" ;;
    esac
done

installed_guide=()
for i in "${!GUIDE[@]}"; do
    name=${GUIDE[$i]}
    case ${GUIDE_ACTION[$i]} in
        tracked)
            printf '[install] WARNING: the checkout tracks %s, so the guide was not installed (shipped copy: %s)\n' \
                "$name" "$HERE/checkout/$name" >&2
            continue ;;
        install) log "$name: absent, installing it" ;;
        same) log "$name: already the shipped copy" ;;
        replace) log "$name: unchanged copy of an earlier install, replacing it" ;;
        backup)
            stamp="$(date +%Y%m%d-%H%M%S)"
            aside="$CHECKOUT/$name.local-$stamp"
            n=1
            while [[ -e $aside || -L $aside ]]; do aside="$CHECKOUT/$name.local-$stamp-$n"; n=$((n + 1)); done
            if [[ $DRY_RUN -eq 1 ]]; then
                log "$name: differs from the shipped copy; would move it to $aside and install the shipped copy"
            else
                mv -- "$CHECKOUT/$name" "$aside"
                log "$name: differs from the shipped copy; kept as $aside, installed the shipped copy as $CHECKOUT/$name"
            fi ;;
    esac
    installed_guide+=("$name")
    [[ ${GUIDE_ACTION[$i]} == same ]] || run cp -f "$RENDERED/$name" "$CHECKOUT/$name"
done

installed_skills=()
for i in "${!SKILL_FILES[@]}"; do
    file=${SKILL_FILES[$i]}
    case ${SKILL_ACTION[$i]} in
        install) log "$file: absent, installing it" ;;
        same) log "$file: already the shipped copy" ;;
        replace) log "$file: unchanged copy of an earlier install, replacing it" ;;
        backup)
            stamp="$(date +%Y%m%d-%H%M%S)"
            aside="$CHECKOUT/$file.local-$stamp"
            n=1
            while [[ -e $aside || -L $aside ]]; do aside="$CHECKOUT/$file.local-$stamp-$n"; n=$((n + 1)); done
            if [[ $DRY_RUN -eq 1 ]]; then
                log "$file: differs from the shipped copy; would move it to $aside and install the shipped copy"
            else
                mv -- "$CHECKOUT/$file" "$aside"
                log "$file: differs from the shipped copy; kept as $aside, installed the shipped copy as $CHECKOUT/$file"
            fi ;;
    esac
    installed_skills+=("$file")
    if [[ ${SKILL_ACTION[$i]} != same ]]; then
        run mkdir -p "$CHECKOUT/$(dirname "$file")"
        run cp -f "$RENDERED/skill-$(basename "$(dirname "$file")").md" "$CHECKOUT/$file"
    fi
done

for entry in "${RETIRED[@]}"; do
    read -r -a fields <<< "$entry"
    file=${fields[0]}
    [[ -e $CHECKOUT/$file || -L $CHECKOUT/$file ]] || continue
    if git -C "$CHECKOUT" ls-files --error-unmatch -- "$file" >/dev/null 2>&1; then
        log "retired tool $file is tracked by the checkout; left alone"
        continue
    fi
    if [[ -L $CHECKOUT/$file || ! -f $CHECKOUT/$file ]]; then
        log "retired tool $file is not a regular file; left alone"
        continue
    fi
    current="$(sha256 "$CHECKOUT/$file")"
    match=0
    for known in "${fields[@]:1}"; do [[ $current == "$known" ]] && match=1; done
    if [[ $match -eq 0 ]]; then
        log "retired tool $file differs from every version this repository shipped; left alone"
    elif [[ $DRY_RUN -eq 1 ]]; then
        log "would remove retired tool $file"
    else
        rm -f -- "$CHECKOUT/$file"
        log "removed retired tool $file"
    fi
done

if [[ -e $CHECKOUT/$LOCAL_GUIDE || -L $CHECKOUT/$LOCAL_GUIDE ]]; then
    log "$LOCAL_GUIDE exists; left as it is"
else
    log "$LOCAL_GUIDE does not exist; the installer never creates it (template: $AGENT_DOCS/AGENTS.local.example.md)"
fi

if [[ $DRY_RUN -eq 1 ]]; then
    log "would write $CHECKOUT/.1541ultimate-tools-version: $VERSION"
    exit 0
fi
printf '%s\n' "$VERSION" > "$CHECKOUT/.1541ultimate-tools-version"
: > "$CHECKOUT/$GUIDE_RECORD"
for name in ${installed_guide[@]+"${installed_guide[@]}"}; do
    printf '%s  %s\n' "$(sha256 "$RENDERED/$name")" "$name" >> "$CHECKOUT/$GUIDE_RECORD"
done
for file in "${installed_skills[@]}"; do
    printf '%s  %s\n' "$(sha256 "$RENDERED/skill-$(basename "$(dirname "$file")").md")" "$file" \
        >> "$CHECKOUT/$GUIDE_RECORD"
done

log "installed ${#FILES[@]} files and ${#SKILLS[@]} skills; added ${#missing[@]} entries to $exclude"
visible="$(git -C "$CHECKOUT" status --porcelain --untracked-files=all -- \
           "${FILES[@]}" .1541ultimate-tools-version "$GUIDE_RECORD" "$LOCAL_GUIDE" \
           ${installed_guide[@]+"${installed_guide[@]}"} 'AGENTS.md.local-*' 'CLAUDE.md.local-*' \
           "${SKILL_DIRS[@]}")"
[[ -z $visible ]] || die "git still sees these paths:
$visible"
log "done; next: cd $(printf '%q' "$CHECKOUT") && ./build-tool --check-support"
