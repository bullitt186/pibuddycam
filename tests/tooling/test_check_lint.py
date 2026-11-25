"""Unit tests for scripts/check_lint.py (stdlib only)."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'check_lint.py'
_spec = importlib.util.spec_from_file_location('check_lint', SCRIPT)
check_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_lint)


class ChangedRangesTest(unittest.TestCase):
    def test_added_and_changed_hunks(self):
        diff = (
            'diff --git a/app/x.py b/app/x.py\n'
            '@@ -3 +3 @@ def a():\n-    x=1\n+    x = 1\n'
            '@@ -10,0 +11,3 @@\n+a\n+b\n+c\n'
        )
        self.assertEqual(check_lint.changed_ranges(diff), [(3, 3), (11, 13)])

    def test_pure_deletion_has_no_range(self):
        self.assertEqual(check_lint.changed_ranges('@@ -5,2 +4,0 @@\n-a\n-b\n'), [])


class ShellScriptsTest(unittest.TestCase):
    def test_globs_match_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'app').mkdir()
            (root / 'app' / 'bootlog.sh').write_text('#!/bin/sh\n')
            (root / 'image' / 'assets' / 'networkmanager' / 'dispatcher.d' / 'sub').mkdir(
                parents=True
            )
            (root / 'image' / 'assets' / 'networkmanager' / 'dispatcher.d' / '90-hook').write_text(
                ''
            )
            self.assertEqual(
                check_lint.shell_scripts(root),
                ['app/bootlog.sh', 'image/assets/networkmanager/dispatcher.d/90-hook'],
            )

    def test_the_repository_scripts_are_found(self):
        scripts = check_lint.shell_scripts()
        self.assertIn('image/assets/pibuddycam-priv', scripts)
        for path in check_lint.SHELL_EXCLUDES:
            self.assertIn(path, scripts)


if __name__ == '__main__':
    unittest.main()
