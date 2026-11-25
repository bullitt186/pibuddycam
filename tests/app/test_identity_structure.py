"""Source-structure checks for identity: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_identity.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))


class MainWiringTests(unittest.TestCase):
    """Guard against re-introducing the live identity regression.

    main() must pass the configured ``[identity] fingerprint`` into
    ``get_network_info`` so a registered token keeps its bound fingerprint. This
    is the second identity regression in a row, so pin the wiring with an AST
    check (main.py cannot be imported here: it needs aiohttp/socketio/gi).
    """

    def test_main_passes_configured_fingerprint(self):
        import ast

        src = (Path(__file__).resolve().parents[2] / 'app' / 'main.py').read_text()
        calls = [
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'get_network_info'
        ]
        self.assertEqual(len(calls), 1, 'expected exactly one get_network_info call')
        self.assertEqual(len(calls[0].args), 1, 'get_network_info must receive the config value')
        arg_src = ast.unparse(calls[0].args[0])
        self.assertIn('identity', arg_src)
        self.assertIn('fingerprint', arg_src)


if __name__ == '__main__':
    unittest.main()
