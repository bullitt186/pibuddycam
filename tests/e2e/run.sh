#!/usr/bin/env bash
# One-command runner for the local Web-console Playwright E2E suite (WP-UI8).
#
# It uses the already-installed local Playwright/Chromium; it installs nothing
# and needs no network. If the browser binary is unavailable it reports SKIP and
# exits 0 unless E2E_REQUIRED=1 is set.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "node is required" >&2; exit 1; }

exec node "$REPO_ROOT/tests/e2e/run.mjs" "$@"
