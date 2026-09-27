"""WP-R2 (AC-24): application version resolution (app_version).

Stdlib-only and hermetic: build-info files live in ``tempfile`` and the
environment is injected, so no real image path or process environment is read.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import app_version  # noqa: E402


class ApplicationVersionTests(unittest.TestCase):
    def _write_build_info(self, directory, doc):
        path = os.path.join(directory, 'build-info.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        return path

    def test_build_info_wins_over_env(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_build_info(d, {'version': '1.2.3'})
            self.assertEqual(
                app_version.application_version(path, env={'PIBUDDYCAM_APP_VERSION': '9.9.9'}),
                '1.2.3',
            )

    def test_env_used_when_build_info_has_no_version(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_build_info(d, {'source_commit': 'abc'})
            self.assertEqual(
                app_version.application_version(path, env={'PIBUDDYCAM_APP_VERSION': '2.0.0'}),
                '2.0.0',
            )

    def test_default_when_nothing_available(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'missing.json')
            self.assertEqual(
                app_version.application_version(path, env={}),
                app_version.DEFAULT_VERSION,
            )

    def test_missing_file_falls_through_to_env(self):
        self.assertEqual(
            app_version.application_version(
                '/nonexistent/build-info.json', env={'PIBUDDYCAM_APP_VERSION': '3.1.0'}),
            '3.1.0',
        )

    def test_unreadable_build_info_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'build-info.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{not valid json')
            self.assertEqual(
                app_version.application_version(path, env={}),
                app_version.DEFAULT_VERSION,
            )

    def test_blank_env_falls_through_to_default(self):
        self.assertEqual(
            app_version.application_version('/nonexistent', env={'PIBUDDYCAM_APP_VERSION': '   '}),
            app_version.DEFAULT_VERSION,
        )

    def test_non_string_version_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_build_info(d, {'version': 123})
            self.assertEqual(
                app_version.application_version(path, env={}),
                app_version.DEFAULT_VERSION,
            )

    def test_sanitizes_control_characters_and_bounds_length(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_build_info(d, {'version': '1.0.0\n\x00evil'})
            value = app_version.application_version(path, env={})
        self.assertEqual(value, '1.0.0evil')
        self.assertLessEqual(len(value), app_version.MAX_VERSION_LENGTH)

    def test_env_none_uses_process_environment(self):
        # No crash when env is omitted; the default path is exercised.
        value = app_version.application_version('/nonexistent/build-info.json')
        self.assertIsInstance(value, str)
        self.assertTrue(value)


class BuildIdentityTests(unittest.TestCase):
    """WP-UI2/AC-14: bounded, non-secret build identity for the dashboard."""

    def _write(self, directory, doc):
        path = os.path.join(directory, 'build-info.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        return path

    def test_projects_only_allowlisted_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, {
                'version': '1.1.2',
                'source_commit': 'abc123',
                'os_suite': 'trixie',
                'kernel_package': 'linux-image-rpi',
                'python_lock_sha256': 'f' * 64,
                'package_manifest': '/home/secretuser/manifest.json',
                'unexpected': 'ignored',
            })
            identity = app_version.build_identity(path)
        self.assertEqual(
            set(identity), set(app_version.BUILD_IDENTITY_FIELDS))
        self.assertEqual(identity['version'], '1.1.2')
        self.assertEqual(identity['source_commit'], 'abc123')
        self.assertNotIn('package_manifest', identity)
        self.assertNotIn('unexpected', identity)

    def test_missing_file_yields_empty_fields(self):
        identity = app_version.build_identity('/nonexistent/build-info.json')
        self.assertEqual(set(identity), set(app_version.BUILD_IDENTITY_FIELDS))
        for value in identity.values():
            self.assertEqual(value, '')

    def test_values_are_sanitized_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, {'version': '1.0.0\n\x00evil'})
            identity = app_version.build_identity(path)
        self.assertEqual(identity['version'], '1.0.0evil')
        self.assertLessEqual(
            len(identity['version']), app_version.MAX_IDENTITY_LENGTH)

    def test_non_string_value_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, {'version': 123, 'source_commit': 'ok'})
            identity = app_version.build_identity(path)
        self.assertEqual(identity['version'], '')
        self.assertEqual(identity['source_commit'], 'ok')


class ActiveReleaseMetadataTests(unittest.TestCase):
    """WP-UI3: the active signed release overrides the factory image identity."""

    def _write(self, directory, name, doc):
        path = os.path.join(directory, name)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        return path

    def test_release_metadata_is_projected_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, 'release.json', {
                'version': '1.1.3',
                'source_commit': 'abc123',
                'channel': 'stable',
                'release_date': '2026-09-26',
                'bundle_url': 'https://secret.example/bundle',
                'token': 'SECRET',
            })
            metadata = app_version.active_release_metadata(path)
        self.assertEqual(
            set(metadata), set(app_version.RELEASE_METADATA_FIELDS))
        self.assertEqual(metadata['version'], '1.1.3')
        self.assertEqual(metadata['source_commit'], 'abc123')
        self.assertNotIn('bundle_url', metadata)
        self.assertNotIn('token', metadata)

    def test_missing_or_malformed_metadata_is_empty(self):
        empty = {field: '' for field in app_version.RELEASE_METADATA_FIELDS}
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                app_version.active_release_metadata(os.path.join(d, 'missing.json')),
                empty)
            path = os.path.join(d, 'release.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{not json')
            self.assertEqual(app_version.active_release_metadata(path), empty)

    def test_installed_release_version_reads_update_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, 'update-state.json', {
                'installed_version': '1.1.3', 'latest_version': '1.2.0'})
            self.assertEqual(app_version.installed_release_version(path), '1.1.3')
            self.assertEqual(
                app_version.installed_release_version(os.path.join(d, 'none.json')), '')

    def test_application_version_prefers_the_active_release(self):
        with tempfile.TemporaryDirectory() as d:
            build_info = self._write(d, 'build-info.json', {'version': '0.0.0+local'})
            release = self._write(d, 'release.json', {'version': '1.1.3'})
            state = self._write(d, 'update-state.json', {'installed_version': '1.1.2'})
            self.assertEqual(
                app_version.application_version(
                    build_info, env={}, release_metadata_path=release,
                    release_state_path=state),
                '1.1.3',
            )

    def test_application_version_falls_back_to_installed_version(self):
        with tempfile.TemporaryDirectory() as d:
            build_info = self._write(d, 'build-info.json', {'version': '0.0.0+local'})
            state = self._write(d, 'update-state.json', {'installed_version': '1.1.3'})
            self.assertEqual(
                app_version.application_version(
                    build_info, env={},
                    release_metadata_path=os.path.join(d, 'missing.json'),
                    release_state_path=state),
                '1.1.3',
            )

    def test_application_version_falls_back_to_build_info(self):
        with tempfile.TemporaryDirectory() as d:
            build_info = self._write(d, 'build-info.json', {'version': '1.0.4'})
            self.assertEqual(
                app_version.application_version(
                    build_info, env={},
                    release_metadata_path=os.path.join(d, 'missing.json'),
                    release_state_path=os.path.join(d, 'missing.json')),
                '1.0.4',
            )

    def test_build_identity_prefers_release_version_and_commit(self):
        with tempfile.TemporaryDirectory() as d:
            build_info = self._write(d, 'build-info.json', {
                'version': '0.0.0+local', 'source_commit': 'oldcommit',
                'os_suite': 'trixie'})
            release = self._write(d, 'release.json', {
                'version': '1.1.3', 'source_commit': 'newcommit'})
            identity = app_version.build_identity(
                build_info, release_metadata_path=release,
                release_state_path=os.path.join(d, 'missing.json'))
        self.assertEqual(identity['version'], '1.1.3')
        self.assertEqual(identity['source_commit'], 'newcommit')
        self.assertEqual(identity['os_suite'], 'trixie')

    def test_build_identity_uses_installed_version_when_no_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            build_info = self._write(d, 'build-info.json', {'version': '0.0.0+local'})
            state = self._write(d, 'update-state.json', {'installed_version': '1.1.3'})
            identity = app_version.build_identity(
                build_info,
                release_metadata_path=os.path.join(d, 'missing.json'),
                release_state_path=state)
        self.assertEqual(identity['version'], '1.1.3')


if __name__ == '__main__':
    unittest.main()
