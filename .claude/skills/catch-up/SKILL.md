---
name: catch-up
description: Catch this machine up after days away - a fresh worktree from origin, memory synced, the queue read and checked against GitHub, a report of what is deployed, running, next and owed. It changes nothing in the project's history (no commit on the default branch, no PR, no deletion), but it does create its worktree, fast-forward a clean main checkout and push one memory note. Use when the owner pastes the catch-up prompt, or when this checkout's last commit is days old and nobody pasted a prompt.
---
# Catch-up (`PLANNING/WORKFLOW.md` §2.1)

Follow the prompt in `PLANNING/WORKFLOW.md` §2.1 exactly, as session `catchup-<account>-<date>`,
where `<account>` is **this machine's** short name from `user_<login>.md` → *Machines* in the memory
repo (`mac`, `linux`, `cloud`), not the GitHub login: with one login, two machines would take the
same session name, branch and worktree. Build nothing, deploy nothing, delete nothing, open no PR,
commit nothing to the project — your only writes are your own worktree, a fast-forward of a clean
main checkout, and one memory note. One catch-up per machine at a time: if one is running (a
`*-catchup-*` worktree, or a `catchup-` note with today's date in the memory repo), message it and
stop. The checkout and its `CLAUDE.md` may be days old, so trust the fresh worktree,
`origin/<default branch>`, `gh pr list` and the running system, not the files alone. An empty slot,
or an empty queue, is an answer: say so and name what §1 of the queue says happens instead.
Report in the four parts §2.1 names. A question the files could not answer, or answered wrongly, is
a gap in them: say so, change nothing; the fix is a planning session's work.
