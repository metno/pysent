# How planning and sessions work — the rulebook

**Status:** installed 2026-11-06 by the session `setup-session-system` from agent-session-kit.
Where another file says something else about *process*, this file wins and the other gets fixed.

The aim: epifanio opens a session and talks about a topic; the agent writes the plan with its
kickoff prompts and rules, and keeps the session queue current: marks what is done, labels what is
started but incomplete, deferred or dropped. Then epifanio pastes the queued prompts, one session
each, and every session keeps the queue current by itself.

Who reads what:

- **A session epifanio opens to talk a topic through** is a planning session: read §1–§5.
- **A session started by pasting a prompt** from `TODO_session_sequence.md` is a build session:
  read §6–§8. Its prompt already carries the short form.
- **The first session on an account or machine that has been idle for days** pastes the catch-up
  prompt of §2.1: its checkout, and the `CLAUDE.md` in it, are behind.
- **Before any PR that touches `PLANNING/`**: `python3 PLANNING/check_planning.py` (§9).
- **A person who joins** reads `PLANNING/README.md`, the one-page version of this file for humans.
- **The skills** `/plan-session`, `/build-session <session>`, `/catch-up` and `/close-session`
  (`.claude/skills/`) carry the sections for each kind of session; this file stays the truth, and
  the checker (S17) refuses a section they cite that does not exist.
- **The mechanical steps have scripts**: `scripts/session.sh start|claim|close|next` and
  `scripts/ci-wait.sh <pr>` (§6); the queue edits behind them are `PLANNING/queue.py`.
- **Hooks do what prose only asks**: `.claude/settings.json` syncs memory at session start and end
  and prints the queue at start; `scripts/hooks/guard-git.py` blocks, before it runs, a command that
  breaks a git rule of §10; `.githooks/pre-push` runs the checker. Once per clone:
  `git config core.hooksPath .githooks` (`scripts/session.sh start` does it).

## 1. The files, and what each one is the truth for

| File | It is the truth for | Who edits it, when |
|---|---|---|
| `PLANNING/TODO_<topic>.md` — a plan | **what** to build and why: facts, design, epifanio's answers, the work packages and their state | the planning session writes it; every build session updates the status banner and its work package's row when it closes |
| `PLANNING/TODO_session_sequence.md` — the queue | **what runs next** (§1), **what is deployed now** (the State table), what is still owed (Open ends), and the prompts to paste (§2) | a planning session adds rows and prompts; a build session claims its row at the start and closes it at the end |
| `PLANNING/TODO_INDEX.md` | the list of every plan, open and closed. The checker only *requires* a row for an open (`TODO_`-prefixed) plan, so the closed ones are a habit, not something CI catches (I1) | a row when a plan is created, moved to Completed when it closes |
| `PLANNING/DECISIONS.md` and `PLANNING/decisions/` | decisions that bind more than one plan (a store, a library ban, a public contract, a vocabulary, a deploy rule): one record each, superseded, never edited | a planning session writes a record when an answer binds work outside its plan; the index row rides in the same PR |
| `epifanio/pysent-memory` (its own private repo) | lessons, epifanio's preferences, what a session learned that the code does not show | every session, at the end: a dated note and its index line |
| `CLAUDE.md` | how the code base is laid out and the standing rules | rarely |

Three rules follow from the table:

1. **One home per fact.** A work package's state lives in its plan. What runs next lives in the
   queue. The index is *derived*: it points, in a few words, and never carries detail that exists
   nowhere else. A decision that binds more than one plan lives in its record (`PLANNING/decisions/`);
   the plan that made it keeps the reasoning and cites it.
2. **One home per prompt.** A queued session's prompt is in the queue (§2 there). The plan keeps
   the background and a pointer. Work that is not queued keeps its prompt in its plan, and the
   queue's §5 lists it.
3. **`main` and the running system are the truth; files and memory are records of
   them.** Before a plan, a row or a memory line is relied on for something that matters (a
   rollout, "this is done", "this is not merged"), check the thing itself: `gh pr view`,
   `git log origin/main`, the live service. A memory description is never enough:
   read the note's body.

**Where these files live** (§12 Q12). In this repo, under `PLANNING/`: a plan, a claim and a close are PRs here, checked by CI.

## 2. The kinds of session

| Kind | Started by | It may | It may not | It ends with |
|---|---|---|---|---|
| **Planning** | Epifanio opens a session and talks about a topic: an idea, feedback, a bug | read code and live systems, measure, ask, write or extend plans, add rows and prompts to the queue, mark what `main` shows as done | build the feature, deploy anything | one docs-only PR merged, a memory note, a report of what was queued and what is still asked |
| **Build** (queued) | Epifanio pastes a ⏭ prompt | build one work package or one stack of PRs in one lane; deploy with the go of one of epifanio (§7) | change an answered decision of epifanio's, a vocabulary or a tuned constant without asking; build against an accepted record in `DECISIONS.md` (§6.2); touch another lane's files | its row closed with receipts, the State table and the plan updated, a memory note |
| **Side** (beside the queue) | Epifanio starts something urgent or small that has no row: a bug, a follow-up a build session spun off | the same as a build session | run unregistered: its first PR adds a row to *Beside the queue* in the queue's §1 | the same close as a build session |
| **Catch-up** | Epifanio pastes the prompt of §2.1 | read, fetch, fast-forward, sync memory, report | build, deploy, delete, open PRs | a report |

A planning session hands off and stops. A fresh session that has only the plan is the test of the
plan: if it cannot build from it, the plan is fixed, not a memory. The one exception is epifanio
asking for the build in the same session; then the plan is still written first, in its own
docs-only PR.

### 2.1 Catching up: after a gap, or on another account

A checkout nobody has used for days is behind `main`, and so is its `CLAUDE.md`,
which is what a new session loads first. Only a prompt epifanio pastes can get past that, so the
first session on an idle account or machine is this one. It **changes nothing in the project's
history**: no commit on `main`, no PR, no deletion, nothing built or deployed. It does
create its own worktree, fast-forward this machine's main checkout when that is clean, and push one
memory note — so it is not literally read-only, and a session told "read-only" could not perform
steps 1, 2 and the last line. Whatever the files could not answer, or answered wrongly, is a gap in
them: it reports it, and the fix is a planning session's work.

**`<account>` is the machine's short name**, as `user_<login>.md` → *Machines* lists it in the
memory repo (`mac`, `linux`, `cloud`, …): not the GitHub login. With one login and two machines,
`catchup-<login>-<date>` is the same name twice, which is the incident §10 records — so the session
name, its branch and its worktree all carry the machine, not the login.

**One catch-up per account at a time.** Before starting, look for one already running: a
`*-catchup-*` worktree in `git worktree list`, and a `catchup-` note with today's date in the memory
repo. (If your harness has a session list or `ListAgents`, check that too.) If one is running,
message it instead of starting a second.

```text
Catch this machine up with the pysent project after a gap, as session
`catchup-<account>-<date>`, where <account> is this machine's short name (mac, linux, cloud) from
user_<login>.md in the memory repo. Build nothing, deploy nothing, delete nothing, open no PR,
commit nothing to the project: your only writes are your own worktree, a fast-forward of a clean
main checkout, and one memory note. If another catch-up is already running on this machine (a
*-catchup-* worktree, or a catchup- note with today's date in the memory repo), do not start a
second: message it and stop.
1. The checkout this session started in may be days old, and so may its CLAUDE.md: trust neither.
   Run `git fetch origin`, then work in a fresh worktree:
   `git worktree add ../pysent-catchup-<account>-<date> -b claude/catchup-<account>-<date> origin/main`
   (an app-made session worktree counts: when its branch has no commits of its own,
   `git merge --ff-only origin/main`). Never switch branches in a checkout you did not
   create. Fast-forward this account's main checkout only if it is clean and on main.
2. Memory: `scripts/memory-repo.sh status`, then `scripts/memory-repo.sh sync`; read MEMORY.md: the
   Rules block first, then the newest lines at the bottom, back to this account's last session.
3. From the fresh worktree read CLAUDE.md, PLANNING/WORKFLOW.md and PLANNING/TODO_session_sequence.md §1.
4. Run `python3 PLANNING/check_planning.py --live` and keep its warnings for the report.
5. Answer from the files alone: what is deployed now (the State table, with the command each row
   names as its source), what is running, the next prompt of each slot (a slot with no ⏭ and no
   ⬜ is empty: say so, and name what the queue's §1 says happens instead — a planning session),
   what is owed and when (Open ends, the queue's §3, the owner's list) and **how old each item on
   the owner's list is**, from the date it carries. Then check each answer
   against the thing itself: `gh pr list`, `git log origin/main`, the running system if
   this account can reach it (read nothing into it).
6. This account's leftovers, listed and not touched: `git worktree list` with `git status --short` in
   each, local branches with commits that are not on origin, whether `gh` works from here.
7. Report: (a) the state in ten lines; (b) what this account needs before it can run a session
   (pulls, installs, keys); (c) every question the files could not answer or answered wrongly, with a
   proposed fix and nothing changed; (d) the prompts to paste next.
Write a dated memory note `catchup-<account>` only if you found a gap or a lesson, and sync at the end.
```

## 3. The queue

### 3.1 Slots and lanes

A **lane** is a set of files and machines that only one session may work on at a time. A **slot**
is a queue epifanio pastes from.

| Lane | Touches | Slot |
|---|---|---|
| **L** library | `src/`, `tests/`, `tools/`, `scripts/` | slot 1 |
| **D** docs | `docs/`, `README.md`, `examples/` | slot 2 |

`PLANNING/` belongs to no lane: every session writes its own rows and its own plan there, which is
why the claim and close PRs are docs-only and why the checker gates them.

Each slot runs one session at a time by default. A second one may run beside it only when it is in
another lane, the queue's *Order rules* say the two share no file and no machine, and epifanio
starts it; epifanio's word goes into the order rules as "Slot <k> runs <n> sessions at once
(owner, <date>)", which the checker reads. More is epifanio's call each time, never a session's.

### 3.2 Marks

| Mark | Means | The row must say |
|---|---|---|
| ⏭ | the next prompt to paste in this slot. At most one per slot | its prompt (§) |
| ▶ | running | its prompt (§), "started" and the date |
| ⬜ | queued, later | its prompt (§) |
| ⏸ | waiting on something that will happen: a session closing, a date, an answer | "**Waits on** …"; a due date, if there is one, right after *Waits on*, *by* or *until* |
| ✅ | closed, nothing owed | date · PRs · how it was verified · what it shipped as · memory note |
| ◐ | closed **with open ends**: the work is merged, but something is still owed (a soak, a device check, a rollout, a part cut short, a code PR left for epifanio to merge) | the same receipts, and **at least one line in *Open ends*** that names it (the checker's S9). A ◐ *row* always has one; a ◐ *plan* may not — §5.3 says which owed thing goes where. A line needs a *Due* cell, but not a date: "no date · Epifanio's review" is a valid Due |
| 💤 | deferred: epifanio said "not now", with no condition that will bring it back by itself | who said so, their words and the date |
| 🚫 | dropped or superseded: it will not run | why, or by which row, and the date |

- ⏭ and ▶ can stand in the same slot: ⏭ then means "next, when the slot is free".
- ⏸ is not 💤. A ⏸ row comes back when its wait is met, and the session that meets it turns it into
  ⬜ or ⏭. A 💤 row comes back only when epifanio says so.
- A ⏸ row's due date is the date right after *Waits on*, *by* or *until* ("Waits on #12, by
  2026-11-01"). The checker's L5 reads only that date, so a date the row mentions for another reason
  (when an upstream issue was filed) does not make it warn forever.
- ◐ becomes ✅ when its last line leaves *Open ends*. Whoever settles the open end makes that edit.
- If a row shows ▶ and nothing is running, the session stopped early. Paste the same prompt again: it
  starts by reading its branch and carries on.

### 3.3 Order

Rows run top to bottom. A planning session adds new rows at the end of the slot: they jump nothing.
Only epifanio (one of epifanio, §7) moves a row up, and the row then carries their name,
their words and the date. When two open rows touch the same files or the same machine, the queue's
*Order rules* say which runs first and why. Rules about closed rows are deleted.

## 4. A planning session, step by step

1. **Start.** Work in a worktree (an app-made session worktree counts). Name the session
   `plan-<topic>`. Run `scripts/memory-repo.sh sync`, read `MEMORY.md`, your `user_<login>.md`, this
   file, `DECISIONS.md` and the queue's §1.
2. **Take the ask down.** One line, in epifanio's words, with the date. Say what kind it is: a new
   feature, feedback on something shipped, a bug, an operation, research. A GitHub issue is an ask
   too: `gh issue view <n>`, and the brief cites `#<n>`; the row's Work cell carries it, and the
   session that finishes the work closes it with the receipts (`scripts/session.sh close … --issue <n>`).
3. **Look before writing.** Search `TODO_INDEX.md`, `PLANNING/` and memory for the topic. If a plan
   covers it, extend that plan with a section and a work package. One feature has one plan.
   Read `DECISIONS.md`: a record that bears on the topic is a constraint the plan states and cites
   (`D-<nnn>`), not a question to ask again.
4. **Establish the facts.** Read the code and, where it is cheap, the live system. Measure what can
   be measured. Write each fact with where it was read and when ("`main` abc1234,
   <date>"). Never plan from a memory description.
5. **Ask.** Decisions that are epifanio's go to them in chat as numbered questions, each with a
   recommendation. A tick means *keep the recommendation*, never "change it". The answers are
   written into the plan verbatim with the name of who answered and the date, and are then frozen: a
   later session asks again rather than changing one. **An answer that binds work outside this
   plan** (a store, a library ban, a public contract, a vocabulary, a deploy rule) is promoted to a
   record `PLANNING/decisions/D-<nnn>_<slug>.md` with its row in `DECISIONS.md`, in the same PR
   (`decisions/README.md` has the shape); the plan cites `D-<nnn>` instead of repeating it. With one
   owner the record is `accepted` when the PR merges. With several maintainers it is `proposed`
   until the approval of §12 Q10, and a row that depends on it is ⏸ "Waits on D-<nnn>". A plan that
   contradicts an accepted record does not change it: it proposes a new record that `supersedes` the
   old one, and its rows wait. A decision with a textual signature (a banned library, a keyword
   that belongs in one directory) names the project's linter rule in `enforced-by`, or carries
   `enforce: <regex> in <scope>` for the checker to run (D4).
6. **Write the plan** (§5), or extend the one that exists.
7. **Cut it into sessions.** One session is one lane and one work package, or one stack of PRs that
   must land together. Name each: `seq-<lane letter><n>-<slug>`, or the plan's own family name.
8. **Place each session**, one of four ways:
   - **queued**: a row at the end of its slot (⬜, or ⏸ with what it waits on), its prompt in the
     queue's §2 (§4.1 below), an order rule if it shares files with another open row, and a line in
     the queue's §4 if it adds a collision surface;
   - **not queued yet**: its prompt stays in the plan, and the queue's §5 names it;
   - **Epifanio's own**: a line in *The owner's list*;
   - **deferred**: a 💤 row with the name of who deferred it, their words and the date.
9. **Register the plan**: a row in `TODO_INDEX.md`.
10. **Bring the queue up to date while you are there.** Run `python3 PLANNING/check_planning.py
    --live` and fix what it reports: a row still ⏭ whose session has open PRs, a ▶ row nobody is
    running, an open end past its date. For the topic you planned, mark what `main`
    already shows as done. Each mark is checked against the PR, not against another note.
11. **Land it.** One docs-only PR titled `[plan-<topic>] …`, the checker clean, merged when CI is
    green. Then the memory note and its index line.
12. **Report and stop**: what was written, where each session sits in the queue, which questions are
    still open, and the next prompt to paste.

### 4.1 The shape of a prompt

A prompt is pasted into a session that knows nothing. It carries, in this order:

```text
Work on <plan's short name> WP-<n> (<what>) of PLANNING/TODO_<plan>.md, as session `<session>`
(slot <k> of PLANNING/TODO_session_sequence.md). First command:
`git fetch origin && git worktree add ../pysent-<session> -b claude/<session> origin/main`
(if that branch already exists, add the worktree on it and carry on from what it holds; an app-made
session worktree counts: name its branch `claude/<session>`), then work only there and name this
session `<session>`. Before your first commit: `git config core.hooksPath .githooks`, then `git config user.name <you> && git config user.email <yours>` with one of the allowed addresses: `massimods@met.no`, `epiesasha@me.com`.
`scripts/session.sh start <session>` does all of this and prints this prompt.
Check the tokens left and ask the owner which model to use.
Claim first: a PR that only sets your row in §1 to ▶ ("started" and the date, your handle in Who) and
moves ⏭ on, merged before any other work (`scripts/session.sh claim <session>`).
Memory: attach `epifanio/pysent-memory` (local: `scripts/memory-repo.sh sync` at start and end); read
`MEMORY.md`, <the notes that matter>.
Read PLANNING/WORKFLOW.md §6–§8; the plan's §<…>; <code, other plans>.
Decisions that bind this work: <D-nnn, … in PLANNING/decisions/, or "none">.
Build, in PRs titled `[<short>] …`: <scope>. Verify on the real thing: <how>.
Touch only <paths>. <the collision rule, if there is one>.
Needs a maintainer's yes (production: epifanio): <decisions; every production deploy>.
Before closing: the docs check (`docker build -q -f docs/Dockerfile .`), <the pages to fix>; update the plan (<row, §>).
Close by PLANNING/TODO_session_sequence.md §6.1: your row ✅ (or ◐, with its open ends) with the date,
PRs and memory note; ⏭ on the next row that can run; the State table if you deployed; your prompt
removed from §2; any later prompt your work made stale; `python3 PLANNING/check_planning.py` clean.
`scripts/session.sh close <session> --mark … --prs … --verified … --note …` does the mechanical part.
Then push the dated memory note `<session>` and put its commit in your final report.
```

The checker refuses a prompt without the worktree command, the claim PR, the memory step, the close
by §6.1 or its own session name.

**The short form.** When every session that will paste it is Claude Code with the skills installed,
the prompt invokes the build-session skill, which carries the invariant part, and says only what is
this session's own:

```text
/build-session <session>
Plan: PLANNING/TODO_<plan>.md WP-<n> (<what>), slot <k>. Scope: <…>. Verify on the real thing: <how>.
Touch only <paths>. <the collision rule, if there is one>. Needs a yes: <decisions; every deploy>.
Decisions that bind this work: <D-nnn, …>. Memory notes to read: <…>. Docs check: docker build -q -f docs/Dockerfile ..
```

The checker accepts either form (S11): the short one only when its `/build-session` names the
prompt's own session. The long form stays for an agent that is not Claude Code.

## 5. A plan file

### 5.1 Anatomy

1. **Title and epifanio's brief**: the ask in one line, dated.
2. **Status banner**: dated; what is settled, what is built, what still blocks. Every session that
   touches the plan updates it. A banner that says "done" names what it landed in (a commit on
   `main`, a release); "on branch X" is not done.
3. **Session conventions**: the one-line pointer
   `Conventions: PLANNING/WORKFLOW.md §5.2 (rules 0–6)`, plus the plan's own rule 3 if it has one.
   Not a copy of the block.
4. **What exists today**, verified in code or on the live system, dated.
5. **Design.**
6. **Questions for epifanio**: asked, answered, frozen. An answer that binds other plans is a record
   in `decisions/`, cited here as `D-<nnn>`.
7. **Work packages**: `<plan> WP-n` · scope · depends on · size · state.
8. **Lane split**: which packages are sequential, which may run side by side, the files two sessions
   would collide on, what a session may decide for itself (branch, PR split, test shape) and what it
   must ask (answered questions, vocabularies, tuned constants).
9. **Verification**: the real case, end to end, on the real thing.
10. **Kickoff**: the prompt, or "queued: `TODO_session_sequence.md` §2.<n>".

### 5.2 The conventions (every plan cites them by number)

A plan carries the one-line pointer `Conventions: PLANNING/WORKFLOW.md §5.2 (rules 0–6)`, never a
copy: a copy costs every reader the same tokens again and drifts. The numbers are stable: plans and
prompts cite "rule 4" and "rule 5"; the build-session skill reads this section.

> **Session conventions** (`PLANNING/WORKFLOW.md` §5.2)
> 0. **Worktree first (MANDATORY).** `git fetch origin && git worktree add ../pysent-<session>
>    -b claude/<session> origin/main`, then work only there (an app-made session worktree
>    counts). Never switch branches in a checkout you did not create. Commit and push after every
>    finished step. The worktree and its branches stay until epifanio declares this session done.
> 1. **Name everything after the session**: its title, branch, PR prefix (`[seq-A3]`) and memory note.
>    Write work-package numbers with the plan's name ("search WP-2").
> 2. **Measured, not guessed.** Facts are read from the code or the live system and dated. Decisions
>    of epifanio's are asked, written down with the date, then frozen. A record in
>    `PLANNING/DECISIONS.md` binds every plan: cite it, never re-decide it.
> 3. *(the project's or the plan's own domain rule, if it has one)*
> 4. **Docs ride with the work.** Before closing a session that landed something substantive, run the
>    docs check (`docker build -q -f docs/Dockerfile .`) and fix any page whose claims the session changed.
> 5. **Memory goes through the memory repo.** Attach `epifanio/pysent-memory`, read `MEMORY.md`, your
>    `user_<login>.md` and the notes the plan names. At the end write a dated note, add its index
>    line, pull --rebase, push, and put the commit in the final report. On a local machine run
>    `scripts/memory-repo.sh sync` at the start and the end. No secrets in notes.
> 6. **Keep the planning files current** (`PLANNING/WORKFLOW.md` §6): claim your row first, close it
>    with receipts, record what you deployed, list what you leave open, and run
>    `python3 PLANNING/check_planning.py` before each PR that touches `PLANNING/`.

### 5.3 Closing a plan

When its last work package is merged and nothing is owed, the session that merged it closes the
plan **in one commit**: `git mv PLANNING/TODO_x.md PLANNING/x.md`, a ✅ banner, the `TODO_INDEX.md`
row moved to Completed with its PRs, and every reference to the old name fixed (the checker finds
the ones in the queue's §1 and the index).

**Closing with open ends (◐).** A plan whose work is all merged and deployed, and which owes only a
check by epifanio, a soak or an upstream step, closes the same way with a ◐ banner instead of ✅, *if*
each owed item is first moved to where epifanio will see it. Which home, by the kind of item:

| The owed thing | Where it goes |
|---|---|
| anything with a date by which it must happen | the queue's *Open ends*, with that date |
| a code PR left for epifanio to review and merge (§6.3) | the queue's *Open ends*, Due "no date", *Closes when* "Epifanio merges it" |
| a check only epifanio can perform (a device, a GUI, a running system) | the queue's §3 |
| an open question of epifanio's with no date | *The owner's list*, with the date it was raised |

The banner names what was moved and where. **A ◐ plan whose items all went to §3 or to the owner's
list leaves *Open ends* empty, and that is correct, not a miss** — the checker's S9 is about a ◐
*row* in §1, which always names its open end, not about the plan's banner. A plan that still owes a
work package, a build or a decision keeps its prefix, and its banner says what is owed.

## 6. A build or side session

### 6.1 Start

1. The worktree, from the first command.
2. Memory: sync, read `MEMORY.md` and the notes the prompt names.
3. **Claim.** A PR that only sets the session's row to ▶ ("started", the date, and in *Who* the
   handle of who pasted the prompt: the GitHub login of the account running the session, `gh api
   user -q .login`, or the name they give) and moves ⏭ to the next row that can run, merged before
   any other work. A side session adds its row to *Beside the queue* instead. Until that PR is on
   `main`, the queue tells epifanio and the other sessions that the slot is free.
   **Two people, one prompt:** if two sessions paste the same ⏭, the second claim PR conflicts on
   the queue, takes `main`'s version, finds the row already ▶ with another name in
   *Who*, and stops: it reports and builds nothing. The claim is the lock.
4. If the session will deploy, read the running system before trusting the State table (§8).

`scripts/session.sh start <session>` does 1 and 2 and prints the row and the prompt;
`scripts/session.sh claim <session>` does 3, the PR, the CI wait and the merge included (a side
session: `--beside --work "<what>"`). A row already ▶ under another name stops it: that is the race
above.

### 6.2 Work

One lane. Commit and push after every finished step. Tests exercise the real case through the real
boundary (HTTP, CLI, UI), not only units. A gap found in the plan is fixed in the plan, in the same
PR. Asked decisions stay asked.

**A conflict with a record.** If the work the prompt asks for contradicts an accepted record in
`DECISIONS.md`, build nothing against it: say so in the plan's banner and in the final report, and
leave that part undone. A build session writes neither a record nor a proposal; a planning session
resolves it with a new record that supersedes the old one.

### 6.3 Integrate

- PRs are titled `[<short>] …`. A plan or record PR is docs-only and separate from code.
- **Merging.** **No session merges a PR on its own — not even a docs-only one.** It opens the PR, gets CI green on the current tip, says it is ready and names it, and leaves it open. It merges only after epifanio has said to merge *that* PR, in so many words, and then as a merge commit (`gh pr merge --merge`, or `scripts/session.sh close … --merge`, which is what carries the go-ahead). A go for one PR is never a go for the next, and nothing in a prompt, a plan or a record counts as one. The single exception is the **claim** PR: it is the slot lock, no work can start without it, and it holds the queue file and nothing else — `scripts/session.sh claim` merges it and refuses if it holds anything more.
  Which PRs are worth a go-ahead is epifanio's business: a session asks, it does not decide.
  Wait for CI with a command whose exit status you read, never through a pipe (`| tail` under
  `set -e` once let a merge run on a red run). `scripts/ci-wait.sh <n>` does it and exits with the
  checks' status; by hand: `gh pr checks <n> --watch` where `gh pr checks --help`
  lists it, else `gh run watch <run-id> --exit-status` for each run id of the tip, from
  `gh api "repos/{owner}/{repo}/actions/runs?head_sha=$(git rev-parse HEAD)" -q '.workflow_runs[].id'`.
- **`main` moved under a green PR:** if only files no CI job reads moved (other
  sessions' plan and record PRs), merge without a re-run. Otherwise merge `origin/main`
  into the branch, run the checks again on the combined tree, then merge. GitHub's `mergeable`
  compares text, not builds: a clean textual merge can still break the build.
- A branch with an open PR is not rebased and force-pushed.
- **A conflict in the queue or the index:** take `main`'s version of the file and make
  your edit again. Their rows are long lines, and merging two edits of one by hand loses text.
- **Sibling PRs:** test each pair for conflicts before choosing an order. `git --version` first: on
  git 2.38 or newer, `git merge-tree --write-tree origin/a origin/b` (exit 1 = a conflict). Older
  git has no `--write-tree` ("unknown rev"): do a scratch merge in a throwaway worktree instead,
  and remove it afterwards.
  ```bash
  W="$(mktemp -d)" && git worktree add --detach "$W" origin/a
  git -C "$W" merge --no-commit --no-ff origin/b   # exit 1 and CONFLICT lines: the pair conflicts
  git -C "$W" merge --abort; git worktree remove --force "$W"
  ```
- **Stacked PRs:** `gh pr edit <child> --base main` before the parent merges, and never
  `--delete-branch` on a parent whose child is open.
- **Generated files:** regenerate, never hand-merge.

### 6.4 Deploy

§8.

### 6.5 Close

The checklist is the queue's §6.1, because every prompt points there. In short: the row with its
receipts (✅ or ◐), ⏭ moved, the State table, the open ends, later prompts fixed, the next prompt
written, the plan's banner and work-package row, the docs check, the checker, the memory note. A
production rollout comes after the last code PR merged, so its record is a second, docs-only PR.

`scripts/session.sh close <session> --mark done|open-ends --prs "…" --verified "…" [--shipped "…"]
--note <file> [--open-end "<due> | <what> | <closes when>"]… [--next <session>]` does the mechanical
steps (the row and its receipts, ⏭, the open ends, the prompt block, the order rules, the checker)
and lands the PR; the State table, later prompts, the next prompt, the plan, the docs check and the
memory note stay yours.

## 7. Who does what, when and where

| Action | Who | When | Where and how | Epifanio's go |
|---|---|---|---|---|
| Worktree and branch | every session | first command | `../pysent-<session>` on `claude/<session>` | no |
| Commit and push | the session | after every finished step | its own branch | no |
| Claim a row | build and side sessions | before any other work | a one-line PR on the queue: ▶, the date, their handle in *Who* | no |
| Open a PR | the session | when a step can stand alone | `[<short>] …` | no |
| Merge the claim PR | the session | before any other work, CI green | `scripts/session.sh claim` does it; it holds the queue file and nothing else | no — it is the slot lock |
| Merge any other PR | epifanio, or the session once epifanio has said to merge that PR | CI green on the current tip, **and** the go-ahead in hand for that PR | `scripts/session.sh close … --merge`, or `OWNER_MERGE=<pr> gh pr merge <pr> --merge` — without that token the hook blocks it (§6.3, §10) | **yes, every PR** |
| Update a branch | the session | whenever `main` moved before a merge | merge it in; no force-push | no |
| Add, move, defer or drop a row | a planning session, or the closing session | on epifanio's word | the queue's §1, with their words and the date | **yes** |
| Write or extend a plan | a planning session | after the facts and the answers | `PLANNING/TODO_<topic>.md` and the index | their answers |
| Write a decision record | a planning session | when an answer binds work outside its plan | `PLANNING/decisions/` and its row in `DECISIONS.md`, in the plan's PR | their answer; with several maintainers, the approval of §12 Q10 |
| Deploy to staging | the session holding the unit | to verify on the real thing | "held by <session>" in the State row | see §12 |
| Deploy to production | the session that built it | after the merge and the staging check | §8 | **yes, each time** |
| Close a plan | the session that merges its last package | one commit | §5.3 | no |
| Write memory | every session | at the end, and when a lesson is learned | `epifanio/pysent-memory` | no |
| Remove a worktree, delete branches | nobody until the person who started that session (its *Who*) declares it done | — | only the branches you name; never from `git branch` output; the command prefixed `SESSION_DONE=<session>`, which the hook requires | **yes, per session** |

**Who decides** (from `kit.config`; the answer to §12 Q11):

| Decision | Who |
|---|---|
| a plan's questions (then frozen) | any maintainer: epifanio; the plan records their name and the date |
| accepting a decision record | §12 Q10 |
| moving a row up, deferring (💤), dropping (🚫) | any maintainer, with their name, their words and the date in the row |
| the production go | one of epifanio, each time, never reused |
| "this session is done" (its worktree and branches may go) | the person who started it: the row's *Who* |
| changing this file | any maintainer, in a PR |

Where this file says epifanio, it means the person this table names for that decision.

## 8. Deploying

**Nothing deploys; releases are epifanio's.** pysent is a library, it is not on PyPI, and it ships
only as a git pin: `metno/mapping-api`'s `requirements.txt` names a pysent commit. A merge to `main`
publishes nothing and changes nothing that is running. Bumping that pin is a PR **in that
repository**, not here, and it is epifanio's to time — see the memory note on the consuming service
for the conventions that apply to anything written there.

So there is no go to ask for and no staging to hold. The State table keeps two rows:

| Unit | Now | Read with |
|---|---|---|
| Released version | not released; consumers pin a commit | `gh api repos/metno/pysent/commits/main --jq .sha` |
| `main` — working state | *(filled by the session that last ran it)* | `docker run --rm -v "$PWD:/src:ro" pysent-ci:noble bash /run-tests.sh` |

The second is the suite's last green run. It is not a deployment and nothing infers it: a session
that runs the suite writes what it saw, and a session that did not leaves the cell alone.

**The pin is far behind.** `0bc5732` is what the service pins today; `main` has moved a long way
past it, including a fix that changed every true-colour picture. A bump will re-render whatever the
consuming service built from the old pin, whether or not that is intended.

## 9. The checker

`python3 PLANNING/check_planning.py` (standard library only) holds the rules a machine can check. CI
runs it, `PLANNING/test_check_planning.py` and `PLANNING/test_queue.py` (the queue editor's tests) in
a job that runs on **every** PR, including PRs that
change only Markdown: those are the PRs it is for. `.githooks/pre-push` runs it before every push.
The mirror image holds for the project's own jobs: a claim or a close changes only `PLANNING/`, so
those jobs should skip it (`paths-ignore: ["PLANNING/**"]`) rather than pay a full run for a row.

| Rule | It refuses |
|---|---|
| S0 | a queue without a State table or a slot table |
| S1, S2 | an unknown mark; a row without a session; a session with two rows |
| S3 | two ⏭ in one slot |
| S4 | more ▶ in a slot than allowed (§3.1) |
| S5 | a ⏭, ▶ or ⬜ row without its prompt |
| S6 | a ⏸ row that does not say what it waits on |
| S7 | a closed or deferred row without a date |
| S8 | a closed session whose prompt is still in the file |
| S9 | a ◐ row that no line of *Open ends* names |
| S10, S11 | a prompt with no row; a prompt without the worktree, the claim PR, the memory step, the close by §6.1 or its own name, unless it is the short form `/build-session <its own session>` |
| S12 | an open end without Due, What, From, Closes when (all four must be filled; *Due* need not be a **date** — "no date · Epifanio's review" is valid, and only a date makes L6 warn) |
| S13 | a line of §1 over 1,200 characters |
| S14 | a `TODO_x.md` named in §1 that is not on disk |
| S15 | a "TODO_session_sequence.md §N" with no such heading |
| S16 | in a slot table with a *Who* column, a ▶, ✅ or ◐ row whose *Who* cell is empty |
| S17 | a `WORKFLOW.md §N` cited in `PLANNING/`, `CLAUDE.md` or a skill, or a bare §N in this file, with no such heading |
| I1–I4 | an open plan with no index row; an index link to a missing file; `TODO_x.md` and `x.md` both on disk; a `TODO_` plan whose banner says SUPERSEDED, RETIRED or ABSORBED |
| D1 | a record in `decisions/` with no row in `DECISIONS.md`; a row with no record; two records or two rows with one id; a record whose id is not its file name |
| D2 | a record without `id`, `date`, `status` or `decided-by`; an unknown status; a `supersedes` or `superseded by` that names no record; an index row whose status differs from its record |
| D3 | a ⏭, ▶ or ⬜ row, or the prompt of such a session, that cites a `D-<nnn>` that is not `accepted` |
| D4 | a project file in the scope of an accepted record's `enforce: <regex> in <scope>` line that matches the regex (a lint, not a review; `decisions/README.md`) |

`--live` adds warnings from GitHub's open PRs: a queued row whose session has open PRs (L1), a closed
row with open PRs (L2), a ▶ row with none (L3), a ⏸ row whose PR merged (L4), a ⏸ row whose due date
(after *Waits on*, *by* or *until*) has come (L5), an open end that is due (L6), and **an open PR that
no row and no line of §1 accounts for** (L7: neither a row's session nor its number or branch named
anywhere in §1 — the owner's list included). A planning session runs it in step 10 of §4.

L7 is why `--live` on an empty queue is not silence: with no rows at all, L1–L6 have nothing to say,
and L7 is the only rule that still speaks. Every PR it names is either work that belongs in the
queue, or work epifanio has parked — and parked work goes on *The owner's list* with its date, which
makes the warning stop.

What it cannot check stays a person's job: that a ✅ is true, that a prompt is still right, that the
State table matches the running system (`--live` reads GitHub, never the running system).

## 10. Lessons that became rules

Each new incident adds a row, with its date and the memory note that tells the story. The rows
below came with the kit (its `docs/lessons.md` has the full list).

| Rule | What happened |
|---|---|
| One worktree per session; never switch branches in a shared checkout | a commit landed on another session's branch; another was orphaned on a detached HEAD |
| Commit and push after every finished step | a 900-line feature sat uncommitted for five days |
| No clean-up until epifanio declares the session done | worktrees are live workspaces; a merged PR is not "done" |
| Never delete branches from `git branch` output | one loop deleted 815 branches of every session; 722 were restored |
| Claim the row before working | a session built and opened three PRs while the queue still said ⏭ |
| A session never merges by hand; the merge goes through `session.sh close --merge` | two sessions ran `gh pr merge` themselves against a brief that forbade it — the merge policy lives inside the script, and a hand-run merge walks straight past it |
| Side sessions register | eight sessions ran beside the queue with no row; seven reached production |
| One fact per row in the State table | one state line grew to 6,513 characters, rewritten by 29 commits in three days |
| A check runs in a job its PRs do not skip | the planning check sat in a job planning-only PRs skipped |
| Take `main`'s side of a long-row file and redo your edit | merging long rows by hand loses text |
| Read the note's body; "done" names what it landed in | a description said shipped, the body said not merged |
| `mergeable` is not proof; test the combined tree | two clean merges broke the type check together |
| `git --version` before `git merge-tree --write-tree`; a scratch merge on older git | the sibling-PR check failed with "unknown rev --write-tree" on git 2.34 |
| Read a CI wait's own exit status; never pipe it | a wait piped to `tail` under `set -e` showed `tail`'s status; a merge ran on a red run |
| A planning session hands off | its context holds discarded options that are not in the file |
| A wait loop must not match itself | `until ! pgrep -f x` loops waited on their own command line for days |
| One catch-up per account at a time, named after the **machine** | two ran at once and wrote memory notes of the same name; with one login, the login cannot tell two machines apart |
| A State row names the command that reads it | a row was filled from the repo's version file: the published thing was a major version and three years older |
| An empty queue is a state, not a fault | a freshly installed project has no rows; "the first ⬜ row" has no answer, and a clean `--live` on no rows read like agreement while six PRs sat open |
| Say the rule where it is used | the queue's §6.1 said "merged" and another file patched it afterwards; a tired session follows the one in front of it |
| A decision that binds more than one plan is a record | — (prevention: two planners can freeze contradicting answers in two plans and both PRs merge green; kit research review, 2026-10-22) |
| A row says who runs it | — (prevention: with several people pasting, two can paste the same ⏭; the claim resolves it and *Who* shows whose the row is; kit research review, 2026-10-22) |

Five of the git rows are enforced before the command runs by `scripts/hooks/guard-git.py`
(`.claude/settings.json`): a branch switch in the main checkout, a branch deletion fed by
`git branch` output, a force push onto an open PR, a worktree removal without
`SESSION_DONE=<session>` in front of it, which is how epifanio's "this session is done" is
written down, and a hand-run `gh pr merge` (or the same call through `gh api …/pulls/<n>/merge`)
without `OWNER_MERGE=<pr>` in front of it naming that same PR, which is how their go-ahead for one
PR is written down. Neither token makes the act safe; they make it deliberate, named, and visible in
the transcript. The sanctioned merge, `scripts/session.sh close … --merge`, needs no token: the
script's own merge runs inside the script, where the hook does not reach.

## 11. Keeping it current

- **Every session, at its close**: its own row, its plan, the State table, its open ends.
- **Every planning session**: step 10 of §4, the queue against GitHub.
- **After a gap, or on another account**: the catch-up prompt (§2.1), before anything else.
- **The index** is edited by the session that owns the plan, one row at a time. A session never
  edits another plan's row from an open branch; it says so in its report and the next planning
  session picks it up.
- **A record** changes only by a new record that supersedes it; setting the old one's status line and
  index row to `superseded by D-<nnn>` is the one edit it gets.

## 12. Epifanio's answers at setup

Asked by `setup-session-system` on 2026-11-06; frozen. A later session asks before changing one.

Epifanio's answer, 2026-11-06: *"i accept the suggested default and yes I want pysent to follow the
mapping-api rule"*. Each recommendation as put to them, and the one override that answer carries:

- **Q1 lanes and slots:** two lanes — **L** library (`src/`, `tests/`, `tools/`, `scripts/`) in slot
  1, **D** docs (`docs/`, `README.md`, `examples/`) in slot 2. One session per slot.
- **Q2 memory repo, machines, cloud sessions:** `epifanio/pysent-memory`, private, cloned at
  `~/dev/pysent-memory`. **This machine is the only one that has run sessions**; cloud sessions were
  not named, so the inbox stands as the fallback until they are.
- **Q3 merge style and who merges:** **rebase, overriding the kit's recommended merge commits** —
  pysent's history is strictly linear and every PR to date was rebase-merged. `MERGE_POLICY=session`:
  a go-ahead covers any PR of that session's, and no session merges a PR unasked.
- **Q4 deploys:** nothing deploys. Not on PyPI; consumers pin a commit. Two State rows (§8).
- **Q5 commit identity:** yes. `Massimo Di Stefano <massimods@met.no>`, also `epiesasha@me.com`. No
  attribution trailers — this writes down a rule the project already followed.
- **Q6 model per session:** yes; every build prompt asks which model.
- **Q7 docs check:** `docker build -q -f docs/Dockerfile .`, before closing any session that touched
  `src/`.
- **Q8 existing conventions:** the five completed plans moved to [`legacy/`](legacy/) with superseded
  banners and backfilled rows in [`TODO_INDEX.md`](TODO_INDEX.md). **The five `*_audit/` evidence
  directories stayed where they are** — commit messages cite them by path, and they are how any
  number in those plans is re-checked. `atmospheric_correction_expert_survey.md` also stayed: it is a
  live deliverable for external reviewers, not a superseded plan. `TODO_PLANNING_composites.md` is
  still open and became this system's first plan rather than legacy.
- **Q9 domain rule:** *no user-visible default changes without epifanio's yes, and every number
  reported is reproducible from a committed script.* Both have caught real errors.
- **Q10 who accepts a decision record:** one owner; merging the plan's PR accepts it.
- **Q11 people:** maintainers `epifanio`; production approvers `epifanio`.
- **Q12 where the planning files live:** in this repo, `PLANNING_HOME=project`. Epifanio was asked
  explicitly whether a public `PLANNING/` was a problem and said it was not.
- **Q13 existing knowledge:** all of it counts and is current. The kit's `adopt-inventory.sh`
  **misses `PLANNING/`** (it excludes `$REPO/PLANNING/*` by design, which is backwards for a project
  being adopted) and does not look for a local-only knowledge branch; both were inventoried by hand
  — 7 plans, 5 evidence directories, and `AGENT_MEMORY/` (9 files, 38 commits on the local-only
  `agent-memory` branch). Reported back to the kit, per its SETUP step 9.3.

**The override, and why.** The kit ships `BRANCH_PREFIX=claude/` and `PR_TITLE=[{session}] {title}`,
which name the agent and the session. Epifanio's answer applies the rule already adopted on
`metno/mapping-api`: **nothing names an AI tool or model, an agent, a session id, a plan file, a
queue or a memory repo** — not in a commit, a branch, a file, a PR title or body, an issue or a
comment. So `BRANCH_PREFIX` is empty and branches are `<kind>/<slug>` (`fix/`, `feat/`, `docs/`,
`ops/`, `chore/`), and `PR_TITLE={title}` — a plain sentence. This session's own branch is
`chore/setup-session-system`. The two conventions are mutually exclusive out of the box, which is
the second thing to report back to the kit.

## 13. History

- 2026-11-06: this file, the queue, the index, the decisions registry, the checker, the session
  scripts, the hooks, the skills, the memory repo (`setup-session-system`, from agent-session-kit).
