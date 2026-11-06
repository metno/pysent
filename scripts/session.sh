#!/usr/bin/env bash
#
# session.sh — the mechanical parts of a session (PLANNING/WORKFLOW.md §6), done the same way every
# time, so the session spends its words on the work.
#
#   scripts/session.sh start <session> [--branch <name>]
#       git fetch; the worktree ../pysent-<session> on <session>, or on --branch <name>
#       (from origin/main, or on the branch if it already exists; with BRANCH_PREFIX empty
#       --branch is required); the hooks path (MODE=overlay: overlay/link.sh links the worktree), and the
#       commit identity if that module is installed; scripts/memory-repo.sh sync; then prints the session's
#       row, its prompt, and the plan files the prompt names.
#   scripts/session.sh claim <session> [--who <handle>] [--beside --work "<what>"]
#       from the session's worktree: the row to ▶ with the date and Who (⏭ moved on), the checker,
#       commit, push, a PR "[<session>] claim", scripts/ci-wait.sh, merge. The claim of §6.1, merged
#       before any other work. A row already ▶ under another name stops it.
#   scripts/session.sh close <session> --mark done|open-ends --prs "#12, #13" --verified "…"
#                          [--shipped "…"] --note <memory note> [--open-end "<due> | <what> | <closes when>"]…
#                          [--next <session>] [--merge] [--issue <n>]
#       from the session's worktree: the mechanical close (PLANNING/queue.py close: the receipts, ⏭,
#       open ends, the prompt block, order rules), the checker, commit (with whatever is already
#       staged: the plan's banner, docs), push, a PR "[<session>] close", scripts/ci-wait.sh — and
#       there it stops, with the PR open. --merge merges it, and is yours to pass only when epifanio
#       has said to merge this PR, in so many words; with --issue <n>, close that GitHub issue of the
#       project with a comment once the close has landed.
#
# When the planning files live in their own repo (PLANNING_HOME=separate: PLANNING/ is a link into the
# planning clone), claim and close commit there, pull with rebase and push to its main instead of
# opening a PR; start links PLANNING/ into the new worktree.
#   scripts/session.sh next [--slot N]
#       what each slot runs next, and the prompt of the first free slot, to paste.
#   scripts/session.sh pr <session> create|edit [<pr>] --title "<t>" [--body "<b>" | --body-file <f>] [gh args…]
#       from the session's worktree: the title shaped by PR_TITLE ("{title}"), then, where
#       scripts/scan.py is installed (MODE=overlay), the branch, title and body scanned against the
#       deny-list and refused on a hit; then gh pr create (--head the session's branch) or gh pr edit.
#
# MODE=overlay: this script lives in the private repo's overlay/scripts/ and is called by path from a
# checkout of the code repo (the checkout it acts on is the one you are in); the planning files are the
# private repo's PLANNING/, so claim and close commit and push there, like PLANNING_HOME=separate.
#
# No session merges on its own. close leaves its PR open whatever MERGE_POLICY says, docs-only or
# not: --merge is epifanio's go-ahead carried on the command line, and under MERGE_POLICY=owner-code
# even --merge is refused on a PR that reaches outside DOCS_PATHS. The one merge still automatic is
# the claim, because the claim is the slot lock and no work can start without it — and before merging
# it the script asks GitHub what that PR holds and refuses anything but the queue file. (With
# PLANNING_HOME=separate nothing is merged at all: claim and close push to the planning repo's main.)
# Going around this script is blocked rather than discouraged: scripts/hooks/guard-git.py refuses a
# hand-run `gh pr merge` unless it carries OWNER_MERGE=<pr> naming that PR, so a merge off this path
# is a thing a session has to decide to write down.
#
# The queue edits are PLANNING/queue.py (it uses the checker's parser); without it (a project's own PLANNING/,
# kept under MODE=overlay) claim, close and next refuse and say how to do it by hand. The CI wait is
# scripts/ci-wait.sh. What stays the session's own: the State table, later prompts, the next prompt
# when the slot runs dry, the plan's banner and work-package row, the docs check, the memory note.
set -euo pipefail

# BASH_SOURCE, not $0: $0 is the calling shell when this file is sourced (the kit's tests source it).
MODE="project"   # project (the kit's files are in the checkout) or overlay (they are in the private repo)
if [[ "$MODE" == overlay ]]; then
  # Physically: the script is often reached as <checkout>/.claude/../scripts, and .claude is a link.
  SCRIPTS="$(cd -P "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
  PLANNING_DIR="$(dirname "$(dirname "$SCRIPTS")")/PLANNING"
  REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"   # the checkout this is run from
else
  REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
  SCRIPTS="$REPO/scripts"
  PLANNING_DIR="$REPO/PLANNING"
fi
SCAN="$SCRIPTS/scan.py"   # the deny-list scanner: installed only with MODE=overlay
PY="${PYTHON:-python3}"
PROJECT="pysent"
DEFAULT_BRANCH="main"
IDENTITY_CONFIG=""   # the identity module's git config for a new worktree, or empty
PLANNING_CLONE=""   # the planning repo's clone when PLANNING_HOME=separate, else empty
QUEUE_FILE="PLANNING/TODO_session_sequence.md"
MERGE_POLICY="session"   # session, or owner-code: under owner-code, --merge is refused off the docs lane
DOCS_PATHS="PLANNING/ docs/"   # the docs-only lane (§6.3): the only paths --merge may touch under owner-code
BRANCH_PREFIX=""   # a session's branch is <prefix><session>; empty: start needs --branch
PR_TITLE="{title}"   # the shape of a PR title: {session} and {title}

main_checkout() {   # the main checkout, also when run from a linked worktree
  local c
  c="$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || { printf '%s' "$1"; return; }
  if [[ "$(basename "$c")" == .git ]]; then dirname "$c"; else printf '%s' "$1"; fi
}
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d'; }
die() { echo "!! $*" >&2; exit 1; }

marker() {   # $1 a checkout: the per-worktree file (inside its git dir, invisible to git) where start records the session
  printf '%s/kit-session' "$(git -C "$1" rev-parse --path-format=absolute --git-dir 2>/dev/null)"
}

session_branch() {   # the branch of session $1: what start recorded in this checkout, else <BRANCH_PREFIX><session>
  local f; f="$(marker "$REPO")"
  if [[ -f "$f" && "$(sed -n 1p "$f")" == "$1" ]]; then sed -n 2p "$f"; return 0; fi
  [[ -n "$BRANCH_PREFIX" ]] || return 1
  printf '%s%s\n' "$BRANCH_PREFIX" "$1"
}

pr_title() {   # $1 session, $2 what: the PR title in the shape PR_TITLE gives
  "$PY" -c 'import sys; print(sys.argv[1].replace("{session}", sys.argv[2]).replace("{title}", sys.argv[3]))' \
    "$PR_TITLE" "$1" "$2"
}

scan() {   # $1 label, then scan.py arguments: 0 when clean or when no scanner is installed
  [[ -f "$SCAN" ]] || return 0
  local label="$1"; shift
  "$PY" "$SCAN" -q --label "$label" "$@"
}

QUEUE_PY_MISSING="the planning files at $PLANNING_DIR have no queue.py, the kit's queue editor (a kept PLANNING/ of the
!! project's own, MODE=overlay: bin/install.py lists what it lacks). Claim or close by hand instead: edit your row in
!! $QUEUE_FILE (▶ \"started <date>\" for a claim; the receipts for a close), run the checker, commit and push
!! (WORKFLOW.md §6.1). Or copy templates/project/base/PLANNING/queue.py and test_queue.py from the kit into it."
need_queue() {   # $1 the command: refuse it, saying what is missing and what works instead, without PLANNING/queue.py
  [[ -f "$PLANNING_DIR/queue.py" ]] || die "$1: $QUEUE_PY_MISSING"
}

in_session_worktree() {   # the current checkout must be the session's own worktree
  local want have
  want="$(session_branch "$1")" || die "no branch recorded for \`$1\` in this checkout: run this from the worktree that scripts/session.sh start $1 made"
  have="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [[ "$have" == "$want" ]] || die "run this from the session's worktree (branch $want; this is ${have:-no branch})"
}

outside_lane() {   # $1 the lane: space-separated path prefixes and exact paths. Paths on stdin;
                   # prints those outside it, one per line.
  local lane="$1" path prefix inside
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    inside=no
    for prefix in $lane; do
      prefix="${prefix%/}"
      if [[ "$path" == "$prefix" || "$path" == "$prefix"/* ]]; then inside=yes; break; fi
    done
    [[ "$inside" == yes ]] || printf '%s\n' "$path"
  done
}

outside_docs_lane() { outside_lane "$DOCS_PATHS"; }

pr_files() { (cd "$REPO" && gh pr view "$1" --json files -q '.files[].path'); }

pr_outside_lane() {   # $1 PR number, $2 the lane: 0 if every file the PR touches is inside it, else 1
                      # and the stray paths on stdout; 2 if GitHub could not be asked.
  local files stray
  files="$(pr_files "$1")" || return 2
  stray="$(printf '%s\n' "$files" | outside_lane "$2" | sort -u | tr '\n' ' ')"
  stray="${stray% }"
  [[ -z "$stray" ]] && return 0
  printf '%s\n' "$stray"; return 1
}

docs_only_pr() { pr_outside_lane "$1" "$DOCS_PATHS"; }   # §6.3: the lane --merge may touch under owner-code

land_planning_repo() {   # PLANNING/ is a link into the planning clone: commit there, pull --rebase, push to its main
  local root="$1" title="$2"
  git -C "$root" add "$QUEUE_FILE"
  if git -C "$root" diff --cached --quiet; then echo ">> nothing to commit in the planning repo"; return 0; fi
  git -C "$root" commit -q -m "$title"
  git -C "$root" pull -q --rebase --autostash \
    || die "the planning repo moved under you and $QUEUE_FILE conflicts: take its main's version of the file, look at your row (▶ under another name: the slot is taken, stop), redo the edit, git -C $root rebase --continue, then push (§6.3)"
  git -C "$root" push -q
  echo ">> pushed to the planning repo: $title ($(git -C "$root" rev-parse --short HEAD))"
}

land() {   # $1 session, $2 what (claim, close), $3 yes|no (merge), $4 the lane the PR must stay inside for
           # that merge (empty: no lane check), $5 what to print when it does not: commit the queue and
           # what is staged, push, PR, CI, merge.
  local s="$1" title="[$1] $2" merge="$3" lane="${4:-}" hint="${5:-}" n url qroot br prt
  qroot="$(cd "$(dirname "$(realpath "$PLANNING_DIR/${QUEUE_FILE#PLANNING/}")")/.." && pwd)"
  if [[ "$qroot" != "$REPO" ]]; then land_planning_repo "$qroot" "$title"; return; fi
  br="$(session_branch "$s")" || die "no branch for \`$s\` in this checkout"
  prt="$(pr_title "$s" "$2")"
  git -C "$REPO" add "$QUEUE_FILE"
  if git -C "$REPO" diff --cached --quiet; then echo ">> nothing to commit"; return 0; fi
  git -C "$REPO" commit -q -m "$title"
  git -C "$REPO" push -q -u origin "$br"
  n="$(cd "$REPO" && gh pr list --head "$br" --state open --json number -q '.[0].number' 2>/dev/null || true)"
  if [[ -z "$n" ]]; then
    scan "PR title" --text "$prt" || die "the PR title breaks the deny-list (scripts/scan.py)"
    url="$(cd "$REPO" && gh pr create --base "$DEFAULT_BRANCH" --head "$br" --title "$prt" \
           --body "$prt (PLANNING/WORKFLOW.md §6.1), by scripts/session.sh." | tail -1)"
    n="${url##*/}"
    echo ">> PR #$n: $url"
  else
    echo ">> PR #$n (already open) updated"
  fi
  "$SCRIPTS/ci-wait.sh" "$n" || die "CI is red on PR #$n: fix it, push, then come back here — a merge by hand is blocked (scripts/hooks/guard-git.py)"
  if [[ "$merge" == yes && -n "$lane" ]]; then
    # The commit rides whatever branch the session used, so a reused branch puts its code in this PR
    # too — ask GitHub what is in it, never assume, and never merge past the lane.
    local stray rc=0
    stray="$(pr_outside_lane "$n" "$lane")" || rc=$?
    [[ $rc != 2 ]] || die "could not list PR #$n's files: gh pr view $n --json files"
    if [[ $rc == 1 ]]; then
      merge=no
      echo "!! PR #$n reaches outside the lane this merge may touch ($lane) — it changes: $stray" >&2
      [[ -z "$hint" ]] || printf '%s\n' "$hint" >&2
    fi
  fi
  if [[ "$merge" == yes ]]; then
    (cd "$REPO" && gh pr merge "$n" --merge) || die "merge of PR #$n failed. If $QUEUE_FILE conflicted, another session edited it: git fetch origin && git merge origin/$DEFAULT_BRANCH, take its side of the file, redo the edit (§6.3)"
    echo ">> PR #$n merged"
  else
    echo ">> PR #$n left open. Merging it is epifanio's call, not yours (§6.3): say it is ready, and"
    echo ">> merge it only once they have said to — then:  OWNER_MERGE=$n gh pr merge $n --merge"
    echo ">> (without that token the hook blocks the merge: scripts/hooks/guard-git.py)"
  fi
}

# sourced (by the kit's tests) rather than run: define the functions and stop.
if [[ "${BASH_SOURCE[0]:-}" != "${0}" ]]; then return 0; fi

cmd="${1:-}"; shift || true
case "$cmd" in
  start)
    s="${1:?usage: session.sh start <session> [--branch <name>]}"; shift
    br=""
    while [[ $# -gt 0 ]]; do
      case "$1" in --branch) br="${2:?--branch <name>}"; shift 2 ;; --branch=*) br="${1#*=}"; shift ;;
                   *) die "start: unknown argument $1" ;; esac
    done
    if [[ -z "$br" ]]; then
      [[ -n "$BRANCH_PREFIX" ]] || die "start: BRANCH_PREFIX is empty here, so name the branch: --branch <kind>/<slug> (fix/, feat/, docs/, ops/, chore/)"
      br="$BRANCH_PREFIX$s"
    fi
    git check-ref-format --branch "$br" >/dev/null 2>&1 || die "start: $br is not a valid branch name"
    scan "branch name" --text "$br" || die "start: the branch name $br breaks the deny-list (scripts/scan.py): pick a plain <kind>/<slug>"
    main="$(main_checkout "$REPO")"
    wt="$(dirname "$main")/$PROJECT-$s"
    git -C "$main" fetch -q origin
    if [[ -d "$wt" ]]; then
      echo ">> worktree exists: $wt (branch $(git -C "$wt" rev-parse --abbrev-ref HEAD))"
    elif git -C "$main" show-ref --verify -q "refs/heads/$br"; then
      git -C "$main" worktree add -q "$wt" "$br"
      echo ">> worktree $wt on existing branch $br: carry on from what it holds"
    elif git -C "$main" show-ref --verify -q "refs/remotes/origin/$br"; then
      git -C "$main" worktree add -q "$wt" -b "$br" "origin/$br"
      echo ">> worktree $wt on $br from origin: carry on from what it holds"
    else
      git -C "$main" worktree add -q "$wt" -b "$br" "origin/$DEFAULT_BRANCH"
      echo ">> worktree $wt on new branch $br from origin/$DEFAULT_BRANCH"
    fi
    printf '%s\n%s\n' "$s" "$(git -C "$wt" rev-parse --abbrev-ref HEAD)" > "$(marker "$wt")"
    if [[ "$MODE" == overlay ]]; then
      "$(dirname "$SCRIPTS")/link.sh" "$wt" || die "start: overlay/link.sh could not link $wt"
    elif [[ -d "$wt/.githooks" ]]; then git -C "$wt" config core.hooksPath .githooks; fi
    if [[ -n "$PLANNING_CLONE" && ! -e "$wt/PLANNING" ]]; then
      [[ -d "$PLANNING_CLONE/PLANNING" ]] || die "no planning clone at $PLANNING_CLONE: scripts/planning-repo.sh init <url>"
      git -C "$PLANNING_CLONE" pull -q --rebase --autostash 2>/dev/null || echo "!! planning repo: pull failed" >&2
      ln -s "$PLANNING_CLONE/PLANNING" "$wt/PLANNING"; echo ">> PLANNING linked to $PLANNING_CLONE/PLANNING"
    fi
    if [[ -n "$IDENTITY_CONFIG" ]]; then (cd "$wt" && eval "$IDENTITY_CONFIG"); fi
    [[ -n "$(git -C "$wt" config user.email 2>/dev/null || true)" ]] \
      || echo "!! no git identity here yet: git -C $wt config user.name <you> && git -C $wt config user.email <yours>" >&2
    wplan="$wt/PLANNING"; wscripts="$wt/scripts"
    if [[ "$MODE" == overlay ]]; then wplan="$PLANNING_DIR"; wscripts="$SCRIPTS"; fi
    "$wscripts/memory-repo.sh" sync || echo "!! memory sync failed: scripts/memory-repo.sh status" >&2
    if [[ ! -f "$wplan/queue.py" ]]; then
      echo "!! no row printed: $QUEUE_PY_MISSING" >&2
    else
      "$PY" "$wplan/queue.py" row "$s" \
        || echo "!! no row for \`$s\`: a side session claims with: scripts/session.sh claim $s --beside --work \"<what>\"" >&2
    fi
    tmp="$(mktemp)"
    if [[ -f "$wplan/queue.py" ]] && "$PY" "$wplan/queue.py" prompt "$s" > "$tmp" 2>/dev/null; then
      echo "---- the prompt"; cat "$tmp"
      echo "---- read first"; grep -oE 'PLANNING/TODO_[A-Za-z0-9_.-]+\.md' "$tmp" | sort -u || true
    fi
    rm -f "$tmp"
    if [[ "$MODE" == overlay ]]; then echo ">> next: cd $wt && $SCRIPTS/session.sh claim $s"
    else echo ">> next: cd $wt && scripts/session.sh claim $s"; fi
    ;;
  claim)
    s="${1:?usage: session.sh claim <session> [--who <handle>] [--beside --work \"<what>\"]}"; shift
    need_queue claim
    in_session_worktree "$s"
    "$PY" "$PLANNING_DIR/queue.py" claim "$s" "$@"
    # The claim is the only merge left automatic (it is the slot lock), and only ever the queue file.
    land "$s" claim yes "$QUEUE_FILE" \
      "!! a claim PR holds $QUEUE_FILE and nothing else. Put the rest on your own branch and claim again."
    ;;
  close)
    s="${1:?usage: session.sh close <session> <queue.py close arguments> [--merge]}"; shift
    need_queue close
    in_session_worktree "$s"
    # Not merging is the default: a close PR is merged only on epifanio's word, carried here as --merge.
    merge=no; issue=""; args=(); skip=0
    for a in "$@"; do
      if [[ $skip == 1 ]]; then issue="$a"; skip=0
      elif [[ "$a" == --merge ]]; then merge=yes
      elif [[ "$a" == --no-merge ]]; then merge=no   # now the default; still accepted
      elif [[ "$a" == --issue ]]; then skip=1
      else args+=("$a"); fi
    done
    lane=""
    if [[ "$MERGE_POLICY" == owner-code ]]; then lane="$DOCS_PATHS"; fi
    "$PY" "$PLANNING_DIR/queue.py" close "$s" ${args[@]+"${args[@]}"}
    land "$s" close "$merge" "$lane" \
      "!! a PR that changes code is epifanio's to review and merge (§6.3, §12 Q3), go-ahead or not.
!! close this row ◐, with that review and merge as its open end.
!! to land the close on its own docs-only PR instead, put it on a fresh branch:
!!   git switch -c $(session_branch "$s")-close origin/$DEFAULT_BRANCH && git cherry-pick $(session_branch "$s")"
    if [[ -n "$issue" ]]; then
      (cd "$REPO" && gh issue close "$issue" --comment "Closed by session \`$s\` (PLANNING/WORKFLOW.md §6.1); the receipts are on its row in PLANNING/TODO_session_sequence.md.") \
        && echo ">> issue #$issue closed" || echo "!! issue #$issue not closed: gh issue close $issue --comment '…'" >&2
    fi
    ;;
  next)
    need_queue next
    "$PY" "$PLANNING_DIR/queue.py" next "$@"
    if s="$("$PY" "$PLANNING_DIR/queue.py" next --paste "$@" 2>/dev/null)"; then
      echo "---- paste into a fresh session: \`$s\`"
      "$PY" "$PLANNING_DIR/queue.py" prompt "$s"
    fi
    ;;
  pr)
    s="${1:?usage: session.sh pr <session> create|edit [<pr>] --title <t> [--body <b> | --body-file <f>] [gh args…]}"; shift
    sub="${1:-}"; shift || true
    [[ "$sub" == create || "$sub" == edit ]] || die "pr: create or edit, not ${sub:-nothing}"
    in_session_worktree "$s"
    title=""; body=""; bodyfile=""; have_body=no; rest=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -t|--title) title="${2?--title <t>}"; shift 2 ;;  --title=*) title="${1#*=}"; shift ;;
        -b|--body) body="${2?--body <b>}"; have_body=yes; shift 2 ;;  --body=*) body="${1#*=}"; have_body=yes; shift ;;
        -F|--body-file) bodyfile="${2:?--body-file <f>}"; shift 2 ;;  --body-file=*) bodyfile="${1#*=}"; shift ;;
        *) rest+=("$1"); shift ;;
      esac
    done
    [[ "$sub" == edit || -n "$title" ]] || die "pr create: --title is required"
    [[ -z "$title" ]] || title="$(pr_title "$s" "$title")"
    br="$(session_branch "$s")"
    bad=0
    scan "branch name" --text "$br" || bad=1
    if [[ -n "$title" ]]; then scan "PR title" --text "$title" || bad=1; fi
    if [[ $have_body == yes ]]; then scan "PR body" --text "$body" || bad=1; fi
    if [[ -n "$bodyfile" ]]; then scan "PR body ($bodyfile)" --file "$bodyfile" || bad=1; fi
    [[ $bad == 0 ]] || die "pr $sub: refused, the lines above break the deny-list (scripts/scan.py); reword them"
    ghargs=(pr "$sub")
    if [[ "$sub" == create ]]; then ghargs+=(--base "$DEFAULT_BRANCH" --head "$br"); fi
    if [[ -n "$title" ]]; then ghargs+=(--title "$title"); fi
    if [[ $have_body == yes ]]; then ghargs+=(--body "$body"); fi
    if [[ -n "$bodyfile" ]]; then ghargs+=(--body-file "$bodyfile"); fi
    (cd "$REPO" && gh "${ghargs[@]}" ${rest[@]+"${rest[@]}"})
    ;;
  -h|--help|"") usage ;;
  *) echo "unknown command: $cmd" >&2; usage; exit 2 ;;
esac
