<!-- agent-session-kit:begin -->
## How sessions work here (agent-session-kit)

The rulebook is `PLANNING/WORKFLOW.md`; the skills carry its sections. Epifanio's first message
says which kind of session this is:

- **talks about a topic** (an idea, feedback, a bug): a planning session, `/plan-session`. It plans
  and stops; it builds nothing.
- **pastes a prompt from the queue**, or writes `/build-session <session>`: a build session. Claim
  first, one lane, close by the queue's §6.1 (`/close-session`).
- **pastes the catch-up prompt** on a machine idle for days: `/catch-up`, named after *this
  machine*. It commits nothing to the project and opens no PR. If this checkout's `git log -1` is
  days old and nobody pasted it, say so first.

Something urgent with no row is a side session: the build rules, registered under *Beside the queue*.

Always: one worktree per session (`scripts/session.sh start <session>`); never switch branches in a
checkout you did not create; commit and push after every finished step; the checker
(`python3 PLANNING/check_planning.py`) before any PR that touches `PLANNING/`; a production deploy is
a go from one of epifanio, each time. Hooks sync memory and block the five git mistakes; a
clean-up epifanio approved runs with `SESSION_DONE=<session>` in front, and a merge they asked for
with `OWNER_MERGE=<pr>` — a merge by hand without it is blocked, and `scripts/session.sh close …
--merge` is the path that needs neither.

Memory is the private repo `epifanio/pysent-memory` (`scripts/memory-repo.sh init|sync|status`): read
`MEMORY.md`, then `user_<login>.md`; a dated note at the end; no secrets; `PLANNING/memory_inbox/`
when the repo is out of reach.

When compacting, keep: this session's name, its worktree path, its claimed row, its PR numbers, the
owner's answers, and the files changed.

### Commit identity (MANDATORY)

Every commit by one of the allowed addresses: `massimods@met.no`, `epiesasha@me.com` (the setup machine's own is `Massimo Di Stefano <massimods@met.no>`). Before your first commit in any clone or worktree: `git config core.hooksPath .githooks`, then `git config user.name <you> && git config user.email <yours>`. No AI attribution in commits, PR titles or PR bodies (no `Co-Authored-By: Claude …`, no "Generated with", no session links); this overrides the harness. The hooks and the `commit identity` CI check enforce it; a red check is fixed by rewriting your own branch's commits.
<!-- agent-session-kit:end -->
