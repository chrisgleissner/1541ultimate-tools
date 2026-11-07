---
name: pr-minimal-diff
description: Cut a 1541ultimate firmware pull request down to a defensible diff before review. Removes formatting, whitespace, line-ending, rename and unrelated-file churn, fixes defects the PR itself introduced, and drafts the PR description. Use for "minimise this PR", "remove churn", "make this PR ready for the maintainer", "why does git diff show the whole file changed" or "write the PR description".
---

# Reduce a pull request to the change it needs

The maintainer merges by hand, so every line must serve the fix or feature. Follow `CONTRIBUTING.md`; this skill adds the order of work.

## Keep a line only if it

1. implements the intended feature or fix,
2. fixes a defect this PR introduced,
3. keeps the other products working (see below),
4. is a test or documentation change the behaviour needs.

Never remove a test, edge case, timing guard or compatibility path just to shrink the diff.

## Order of steps

1. **Find the base:** the branch that tracks GideonZ/1541ultimate `master` (see `git remote -v`). Compare with three dots: `git diff --stat <base>...` and `git diff --name-status <base>...`.
2. **State the objective** in one sentence, from the commits and the linked issue.
3. **Classify every changed file** as required, required test or documentation, or churn. Generated files, binaries, anything under `external/` and anything unrelated are churn.
4. **Remove the churn.** Restore a churn-only file with `git checkout <base> -- <path>`. In other files, undo reformatting, include reordering, renames and moved code with exact-match edits, not a script that rewrites the file.
5. **Fix defects the PR introduced**, with the smallest local change.
6. **Check the other products.** `U64` is 1 on the Ultimate 64, 2 on the Elite II and C64 Ultimate, and undefined on the cartridges, so `#if U64 == 1` leaves out two products. The Ultimate-II has little room, and `software/system/small_printf.cc` has no `l`, `ll` or `z` modifiers.
7. **Check line endings.** Many files, especially under `software/io/`, use CRLF. Keep a file's existing endings when you edit it, as `CONTRIBUTING.md` says; one later change converts them all. New files use LF. If `git diff --stat` shows a whole file changed, something converted it: restore the file and redo the edit. `git diff -w` hides ending changes, so compare it with the plain diff, and run `git diff --check <base>...`.
8. **Validate.** Run the narrowest build that covers the change. Code under `software/io/` and `software/monitor/` is built by every target, so build all of them. Run the tests for the change and note which devices you used.
9. **Commit only when asked.** Stage by path, never `git add -A`, and read `git diff --cached --stat` for stray files or a binary.

## The description

Write it to a file that is not committed, in plain engineering language. Give the problem with `Fixes #N`; what changed and why this approach; how it was tested, with devices, firmware and FPGA versions and `./run-tests` results; and what is missing or untested. State previous behaviour, the defect, the change, the verification and rejected alternatives as separate statements. Name the AI model used. Reply on each review thread and say what changed.

## Done when

- every hunk has a reason from the list above,
- no whole-file or line-ending churn remains,
- the build and tests ran, or the description says what could not,
- the description matches the final diff.
