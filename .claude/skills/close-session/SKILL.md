---
name: close-session
description: Close a build or side session by the queue's §6.1 - the receipts on its row, ⏭ moved, open ends, the State table, later prompts fixed, the plan's banner, the docs check, the checker, the memory note. Use when a session's work is merged, or when it must stop early.
argument-hint: <session>
---
# Close `$0` (the queue's §6.1; `PLANNING/WORKFLOW.md` §6.5)

1. **By hand, before the script**: the plan's status banner and your work-package row; the State
   table if you deployed (your units' rows only); any later prompt your work made wrong; the next
   prompt of your slot if it is missing (when no row is left, ask the owner what comes next); the
   docs check your prompt names, its pages fixed. Stage those changes.
2. **The script**: `scripts/session.sh close $0 --mark done|open-ends --prs "#…" --verified "…"
   [--shipped "…"] --note <YYYY-MM-DD>_$0.md [--open-end "<due> | <what> | <closes when>"]…
   [--next <session>] [--issue <n>]` writes the receipts, moves ⏭, adds the open ends, retires your prompt, deletes
   the order rules about you, runs the checker, commits with what you staged, pushes, opens the PR and
   waits for CI — and stops there, with the PR open: merging it is the owner's call, and `--merge` is
   yours to pass only once they have said to merge that PR (§6.3). Something still owed (a soak, a device check, a rollout, a part cut short,
   or a code PR left for someone else to review and merge — §6.3) means `--mark open-ends` with one
   `--open-end` per item. *Due* must be filled but need not be a date: `"no date | \`PR #<n>\` awaits
   review and merge | it is merged"` is a valid open end, and only a date makes the checker's L6 warn. `--issue <n>` closes the GitHub issue the
   plan's brief cites, with a comment, once the close has landed.
3. **A production rollout** after the last code PR is recorded in a second, docs-only PR.
4. **Memory**: the dated note `<YYYY-MM-DD>_$0.md` (what the code does not show: lessons, the owner's
   words, where things live), its index line at the bottom of `MEMORY.md`,
   `scripts/memory-repo.sh sync` (the Stop hook does it too); put the commit in your final report.
5. **Report**: the PRs, what was verified and how, what is open, the next prompt to paste. Leave the
   worktree and the branch: the person who started the session declares it done.
