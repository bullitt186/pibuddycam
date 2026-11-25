#!/usr/bin/env bash
# Agent stop hook (repo-kit, docs/CI_STANDARD.md): runs the fast check when the working tree
# has changes and sends the agent back to work if it fails. Registered in .claude/settings.json.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 0
[ -z "$(git status --porcelain)" ] && exit 0

# The repository's fast check (lint, format check, type check, unit tests), as in AGENTS.md.
FAST_CHECK="python3 -m unittest discover -s tests -t . && python3 -m compileall -q app tests"

if ! out=$(bash -c "$FAST_CHECK" 2>&1); then
  printf 'Fast check failed. Fix it before stopping:\n%s\n' "$(printf '%s' "$out" | tail -40)" >&2
  exit 2
fi
git diff --check >&2 || { echo "Whitespace errors (git diff --check). Fix them before stopping." >&2; exit 2; }

# Lint (scripts/check_lint.py, ESLint) when the tools are installed (AGENTS.md, Install / set up).
if command -v ruff >/dev/null 2>&1 && command -v shellcheck >/dev/null 2>&1; then
  if ! out=$(python3 scripts/check_lint.py 2>&1); then
    printf 'Lint failed. Fix it before stopping:\n%s\n' "$(printf '%s' "$out" | tail -40)" >&2
    exit 2
  fi
else
  echo "Note: lint skipped (ruff or shellcheck not installed)." >&2
fi
if [ -d node_modules/eslint ]; then
  if ! out=$(npm run --silent lint 2>&1); then
    printf 'ESLint failed. Fix it before stopping:\n%s\n' "$(printf '%s' "$out" | tail -40)" >&2
    exit 2
  fi
fi
exit 0
