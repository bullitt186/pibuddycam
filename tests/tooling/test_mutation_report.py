"""Unit tests for scripts/mutation_report.py, the mutmut result summary (stdlib only)."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'mutation_report.py'
_spec = importlib.util.spec_from_file_location('mutation_report', SCRIPT)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

CAMERA = 'src/camera.py'
STATE = 'src/state.py'


class MutationReportTest(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def write_meta(self, module, exit_codes):
        meta = Path('mutants') / f'{module}.meta'
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({'exit_code_by_key': exit_codes}))

    def main(self, *args):
        argv, sys.argv = sys.argv, ['mutation_report.py', *args]
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                return report.main(), out.getvalue()
        finally:
            sys.argv = argv

    def test_counts_and_score(self):
        # mutmut 3 exit codes: 1 killed, 0 survived, 36 timeout, None not checked.
        self.write_meta(
            CAMERA,
            {
                'camera.x_a__mutmut_1': 1,
                'camera.x_a__mutmut_2': 0,
                'camera.x_a__mutmut_3': 36,
                'camera.x_a__mutmut_4': 1,
            },
        )
        counts = report.collect([CAMERA])['camera.py']
        self.assertEqual((counts['killed'], counts['survived'], counts['timeout']), (2, 1, 1))
        self.assertEqual(report.score(counts), 75.0)

    def test_check_complete_fails_with_unchecked_mutants(self):
        self.write_meta(CAMERA, {'camera.x_a__mutmut_1': 1, 'camera.x_a__mutmut_2': None})
        self.assertEqual(self.main('--check-complete', CAMERA)[0], 1)
        self.write_meta(CAMERA, {'camera.x_a__mutmut_1': 1, 'camera.x_a__mutmut_2': 0})
        self.assertEqual(self.main('--check-complete', CAMERA)[0], 0)

    def test_no_mutants(self):
        self.write_meta(STATE, {})
        self.assertEqual(self.main('--no-mutants', STATE)[0], 0)
        self.write_meta(CAMERA, {'camera.x_a__mutmut_1': None})
        self.assertEqual(self.main('--no-mutants', STATE, CAMERA)[0], 1)
        self.assertEqual(self.main('--no-mutants', 'src/missing.py')[0], 1)


if __name__ == '__main__':
    unittest.main()
