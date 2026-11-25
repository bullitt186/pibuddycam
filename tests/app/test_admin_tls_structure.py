"""Source-structure checks for admin_tls: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_admin_tls.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))
ADMIN_APP = PI_DIR / 'admin_app.py'


def _run_function(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f'{name} not found')


class AdminAppFailClosedWiringTests(unittest.TestCase):
    """admin_app must route through the fail-closed resolver, never HTTP."""

    def setUp(self):
        self.source = ADMIN_APP.read_text(encoding='utf-8')

    def test_run_uses_the_fail_closed_resolver(self):
        run = _run_function(self.source, 'run')
        self.assertIn('admin_tls.resolve_server_tls', run)
        self.assertIn('TlsConfigurationError', run)
        self.assertIn('SystemExit', run)

    def test_no_plaintext_admin_fallback_remains(self):
        self.assertNotIn('serving WITHOUT TLS', self.source)
        self.assertNotIn('_ssl_context_from_env', self.source)

    def test_admin_module_has_no_optional_tls_wording(self):
        # The unit comment must not claim TLS is optional in admin mode.
        unit = (PI_DIR / 'systemd' / 'pibuddycam-admin.service').read_text(encoding='utf-8')
        self.assertIn('TLS is mandatory', unit)
        self.assertNotIn('TLS is optional', unit)


if __name__ == '__main__':
    unittest.main()
