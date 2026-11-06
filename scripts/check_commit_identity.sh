#!/usr/bin/env bash
# Commit identity and attribution policy (CLAUDE.md -> "Commit identity").
#
#   scripts/check_commit_identity.sh <rev-range>     e.g. origin/main..HEAD
#
# Fails (exit 1) and lists every commit in the range that
#   * has an author or committer outside the allowlist, or
#   * carries AI attribution in its message (Claude/Copilot Co-authored-by, Claude-Session,
#     "Generated with Claude Code", a claude.ai session link).
# Used by .githooks/pre-push and the commit-identity CI workflow.
set -euo pipefail

RANGE=${1:?usage: $0 <rev-range>}

# Who may author or commit. noreply@github.com is allowed as COMMITTER only:
# GitHub itself commits PR merges and "Update branch" with that address.
ALLOWED_RE='^(massimods@met\.no|epiesasha@me\.com)$'
COMMITTER_EXTRA_RE='^noreply@github\.com$'
AI_RE='^[[:space:]]*(co-authored-by:.*(anthropic|claude|copilot)|claude-session:)|generated with \[?claude code|claude\.ai/code/session_'

# A bad range must fail, not check nothing: rev-list runs here, under set -e, not in a <( ).
shas=$(git rev-list "$RANGE")
bad=0
while IFS= read -r sha; do
  [ -n "$sha" ] || continue
  ae=$(git log -1 --format=%ae "$sha"); ce=$(git log -1 --format=%ce "$sha")
  why=()
  [[ $ae =~ $ALLOWED_RE ]] || why+=("author <$ae>")
  [[ $ce =~ $ALLOWED_RE || $ce =~ $COMMITTER_EXTRA_RE ]] || why+=("committer <$ce>")
  if git log -1 --format=%B "$sha" | grep -qiE "$AI_RE"; then why+=("AI attribution in message"); fi
  if [ ${#why[@]} -gt 0 ]; then
    bad=$((bad+1))
    printf '  %s %s\n      -> %s\n' "${sha:0:10}" "$(git log -1 --format=%s "$sha" | cut -c1-70)" "$(IFS=';'; echo "${why[*]}")"
  fi
done <<< "$shas"

if [ "$bad" -gt 0 ]; then
  cat <<MSG

$bad commit(s) break the commit identity rule (CLAUDE.md -> "Commit identity").
Allowed: massimods@met.no, epiesasha@me.com
Fix: git config user.name <you> && git config user.email <one of them>
     then rewrite them on your own branch, e.g.
       git rebase -x 'git commit --amend --no-edit --reset-author' <base>
     and remove the AI trailer lines from the messages (reword).
MSG
  exit 1
fi
echo "commit identity: $(git rev-list --count "$RANGE") commit(s) OK"
