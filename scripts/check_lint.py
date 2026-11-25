#!/usr/bin/env python3
"""Lint and format check for Python and shell (repo-kit CI standard, category 2).

- ruff check over the repository (rules and the per-module baseline in ruff.toml);
- ruff format on the Python lines changed against a base, by default the merge base with
  origin/main: new and changed code follows the formatter, existing code is not reformatted
  in bulk (formatting it would rewrite almost every runtime module);
- shellcheck on the scripts CI also checks with ``bash -n`` (SHELL_SCRIPTS).

The web console is linted separately with ESLint (``npx eslint app/web``).

Usage: python3 scripts/check_lint.py [BASE]
Exit code 0 = ok, 1 = problems found. Needs ruff (requirements-dev.txt) and shellcheck.
"""

from __future__ import annotations

import glob
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL_SCRIPTS = [
    'image/scripts/*.sh',
    'app/*.sh',
    'image/assets/*.sh',
    'image/assets/pibuddycam-priv',
    'image/assets/networkmanager/dispatcher.d/*',
]
# Per-script shellcheck exclusions, each with its reason.
SHELL_EXCLUDES = {
    # SC2115 (rm -rf "$root/home/$SERVICE_USER"): $root is set with ${1:?...} under set -u
    # and SERVICE_USER is a constant, so the path can never collapse to /home.
    # SC2016: the dpkg-query format string must reach dpkg-query unexpanded.
    'image/assets/install-factory-app.sh': ['SC2115', 'SC2016'],
    # SC2329: cleanup() runs from `trap cleanup EXIT`, which shellcheck does not follow.
    'image/scripts/make-app-release.sh': ['SC2329'],
    # SC2086: the unquoted find results are split on purpose, one path per printf argument.
    'image/scripts/validate-image.sh': ['SC2086'],
}
PYTHON_EXCLUDED = ('_archive/', 'research/')
HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', re.MULTILINE)


def run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=check)


def shell_scripts(root: Path = ROOT) -> list[str]:
    """The scripts CI checks with bash -n, relative to the repository root."""
    found: set[str] = set()
    for pattern in SHELL_SCRIPTS:
        found.update(str(Path(p).relative_to(root)) for p in glob.glob(str(root / pattern)))
    return sorted(p for p in found if (root / p).is_file())


def changed_ranges(diff: str) -> list[tuple[int, int]]:
    """Line ranges (first, last) of the new side of a ``git diff -U0`` for one file."""
    ranges = []
    for match in HUNK.finditer(diff):
        start, count = int(match.group(1)), int(match.group(2) or '1')
        if count > 0:
            ranges.append((start, start + count - 1))
    return ranges


def changed_python_files(merge_base: str) -> list[str]:
    out = run(
        'git', 'diff', '--name-only', '--diff-filter=AMR', merge_base, '--', '*.py', check=True
    ).stdout
    return [
        p for p in out.splitlines() if not p.startswith(PYTHON_EXCLUDED) and (ROOT / p).is_file()
    ]


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else 'origin/main'
    ok = True

    print('ruff check')
    result = run('ruff', 'check', '.')
    if result.returncode != 0:
        print(result.stdout + result.stderr)
        ok = False

    merge_base = run('git', 'merge-base', base, 'HEAD', check=True).stdout.strip()
    files = changed_python_files(merge_base)
    print(f'ruff format: lines changed against {base} in {len(files)} files')
    for path in files:
        diff = run('git', 'diff', '-U0', merge_base, '--', path, check=True).stdout
        for first, last in changed_ranges(diff):
            result = run('ruff', 'format', '--check', f'--range={first}-{last}', path)
            if result.returncode != 0:
                print(result.stdout + result.stderr)
                print(f'Format it: ruff format --range={first}-{last} {path}', file=sys.stderr)
                ok = False

    scripts = shell_scripts()
    print(f'shellcheck: {len(scripts)} scripts')
    for path in scripts:
        excludes = SHELL_EXCLUDES.get(path, [])
        args = ['shellcheck', *(['--exclude=' + ','.join(excludes)] if excludes else []), path]
        result = run(*args)
        if result.returncode != 0:
            print(result.stdout + result.stderr)
            ok = False

    print('ok' if ok else 'problems found')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
