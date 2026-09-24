#!/usr/bin/env bash
# Commit and push the state checkout (the `state` branch) after a cron run.
# Each workflow writes its own file, so a rebase onto a concurrent push never conflicts.
set -euo pipefail

cd "${STATE_DIR:-state}"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then
  echo "state: no changes"
  exit 0
fi
git commit -q -m "${1:-update state}"
for attempt in 1 2 3; do
  if git push -q origin HEAD:state; then
    exit 0
  fi
  echo "state: push attempt ${attempt} rejected, rebasing" >&2
  git pull -q --rebase origin state
done
echo "state: push failed" >&2
exit 1
