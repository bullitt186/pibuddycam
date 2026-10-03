"""A camera set up with "Later" has no Prusa token: no Prusa cloud traffic.

``main.main`` is an application-lifetime coroutine that cannot run on the host,
so this pins the guard structurally: every Prusa Connect call is skipped while
``cloud_enabled`` is false, and the local services are not.
"""
import re
import unittest
from pathlib import Path

MAIN = (Path(__file__).resolve().parents[2] / 'app' / 'main.py').read_text(encoding='utf-8')


class CloudGuardTests(unittest.TestCase):
    def test_empty_token_disables_the_cloud(self):
        self.assertIn("cloud_enabled = bool(token and token.strip())", MAIN)

    def test_prusa_loops_are_only_started_with_a_token(self):
        for name in ('snapshot_loop', 'info_service_loop', 'ota_loop'):
            pattern = (
                r"if cloud_enabled:\n(?:[^\n]*\n){0,3}?\s+asyncio\.create_task\(" + name
            )
            self.assertRegex(MAIN, pattern, name)

    def test_initial_upload_and_signaling_need_a_token(self):
        self.assertRegex(MAIN, r"if cloud_enabled:\n\s+status, result_class, body = await upload_info")
        self.assertRegex(MAIN, r"if cloud_enabled:\n\s+await sig\.connect\(\)")

    def test_local_services_do_not_depend_on_the_token(self):
        for needle in ('timelapse_loop', 'start_local_http', 'detect_timezone'):
            index = MAIN.index(needle)
            window = MAIN[max(0, index - 60):index]
            self.assertNotIn('cloud_enabled', window, needle)


if __name__ == '__main__':
    unittest.main()
