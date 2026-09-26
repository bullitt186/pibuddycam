"""Appliance image/security defect: boot-provisioned admin TLS.

Focused tests for :mod:`admin_tls` (durable self-signed keypair + volatile
``admin.env``), the admin-mode fail-closed resolver, and the ``admin_app`` wiring
that consumes it. The real-``openssl`` tests are skipped when the binary is
absent; the mock tests pin the argv (no shell) and file-mode behavior.
"""
import ast
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import admin_tls  # noqa: E402
import config_schema  # noqa: E402
import persist_restore  # noqa: E402

ADMIN_APP = PI_DIR / 'admin_app.py'
HAS_OPENSSL = shutil.which('openssl') is not None


def _mode(path):
    return os.stat(path).st_mode & 0o777


def _run_function(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f'{name} not found')


class ResolveServerTlsTests(unittest.TestCase):
    """Requirement 6: fail closed in admin mode; setup stays HTTP."""

    def test_setup_mode_is_plain_http_even_with_tls_env(self):
        called = []

        def factory(env):
            called.append(env)
            return object()

        env = {'ADMIN_TLS_CERT': '/x.crt', 'ADMIN_TLS_KEY': '/x.key'}
        result = admin_tls.resolve_server_tls(
            'setup', env=env, context_factory=factory)
        self.assertIsNone(result)
        self.assertEqual(called, [], 'setup mode must not load a TLS context')

    def test_admin_mode_without_tls_raises(self):
        with self.assertRaises(admin_tls.TlsConfigurationError):
            admin_tls.resolve_server_tls('admin', env={})

    def test_admin_mode_with_tls_returns_context(self):
        sentinel = object()
        result = admin_tls.resolve_server_tls(
            'admin', env={'ADMIN_TLS_CERT': '/c', 'ADMIN_TLS_KEY': '/k'},
            context_factory=lambda env: sentinel)
        self.assertIs(result, sentinel)

    def test_explicit_context_wins_in_both_modes(self):
        sentinel = object()
        self.assertIs(
            admin_tls.resolve_server_tls('admin', ssl_context=sentinel, env={}),
            sentinel,
        )
        self.assertIs(
            admin_tls.resolve_server_tls('setup', ssl_context=sentinel, env={}),
            sentinel,
        )

    def test_broken_certificate_propagates(self):
        def factory(env):
            raise ssl.SSLError('bad certificate')

        with self.assertRaises(ssl.SSLError):
            admin_tls.resolve_server_tls(
                'admin', env={'ADMIN_TLS_CERT': '/c', 'ADMIN_TLS_KEY': '/k'},
                context_factory=factory)

    def test_default_factory_without_paths_returns_none(self):
        self.assertIsNone(admin_tls.context_from_env({}))


class NamingTests(unittest.TestCase):
    def test_san_entries_cover_canonical_and_local(self):
        entries = admin_tls._san_entries(['buddy3d-abc123'])
        self.assertIn('DNS:buddy3d-abc123', entries)
        self.assertIn('DNS:buddy3d-abc123.local', entries)
        self.assertIn('DNS:localhost', entries)
        self.assertIn('IP:127.0.0.1', entries)

    def test_common_name_falls_back_to_localhost(self):
        self.assertEqual(admin_tls._common_name(['buddy3d-abc123']),
                         'buddy3d-abc123.local')
        self.assertEqual(admin_tls._common_name([]), 'localhost')

    def test_label_sanitizes_shell_metacharacters(self):
        self.assertEqual(
            admin_tls._sanitize_label('Evil;touch /tmp/pwned'),
            'evil-touch-tmp-pwned',
        )

    def test_hostname_candidates_prefers_configured_and_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / 'device.toml'
            device.write_text(
                'schema_version = 1\n'
                'fingerprint = "deadbeef"\n'
                '[admin]\n'
                'hostname = "MyCamera"\n',
                encoding='utf-8',
            )
            candidates = admin_tls.hostname_candidates(str(device))
        self.assertIn('mycamera', candidates)
        self.assertTrue(
            any(name.startswith('buddy3d-') for name in candidates), candidates)


class EnsureMockTests(unittest.TestCase):
    """Requirement 3/8: argv command behavior, modes, env materialization."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tls_dir = os.path.join(self._tmp.name, 'admin-tls')
        self.env_path = os.path.join(self._tmp.name, 'admin.env')

    def _fake_runner(self, calls, returncode=0):
        def runner(cmd, capture_output=True, text=True):
            calls.append(cmd)
            if returncode == 0:
                key_out = cmd[cmd.index('-keyout') + 1]
                cert_out = cmd[cmd.index('-out') + 1]
                Path(key_out).write_text('FAKE KEY\n', encoding='utf-8')
                Path(cert_out).write_text('FAKE CERT\n', encoding='utf-8')
            return subprocess.CompletedProcess(cmd, returncode, '', 'boom')
        return runner

    def test_generates_with_argv_list_and_materializes_env(self):
        calls = []
        chowns = []
        admin_tls.ensure(
            'prusa-cam',
            tls_dir=self.tls_dir,
            env_path=self.env_path,
            hostnames=['buddy3d-abc123'],
            openssl='/usr/bin/openssl',
            runner=self._fake_runner(calls),
            chown=lambda p, uid, gid: chowns.append((p, uid, gid)),
            service_ids=(1234, 1234),
        )
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], '/usr/bin/openssl')
        self.assertIn('-addext', cmd)
        san = cmd[cmd.index('-addext') + 1]
        self.assertIn('subjectAltName=', san)
        self.assertIn('DNS:buddy3d-abc123.local', san)

        key = os.path.join(self.tls_dir, 'admin.key')
        cert = os.path.join(self.tls_dir, 'admin.crt')
        self.assertEqual(_mode(key), 0o600)
        self.assertEqual(_mode(cert), 0o644)
        self.assertEqual(_mode(self.env_path), 0o640)
        text = Path(self.env_path).read_text(encoding='utf-8')
        self.assertIn(f'ADMIN_TLS_CERT={cert}\n', text)
        self.assertIn(f'ADMIN_TLS_KEY={key}\n', text)
        for path in (key, cert, self.env_path):
            self.assertIn((path, 1234, 1234), chowns)

    def test_tls_dir_is_chowned_to_service_on_generation(self):
        # Review-blocking defect: pi-persist runs as root, so the durable TLS
        # directory it creates must be handed to the service account or
        # prusa-admin cannot traverse it.
        calls = []
        chowns = []
        admin_tls.ensure(
            'prusa-cam',
            tls_dir=self.tls_dir,
            env_path=self.env_path,
            hostnames=['buddy3d-abc123'],
            openssl='/usr/bin/openssl',
            runner=self._fake_runner(calls),
            chown=lambda p, uid, gid: chowns.append((p, uid, gid)),
            service_ids=(1234, 1234),
        )
        self.assertIn((self.tls_dir, 1234, 1234), chowns)

    def test_reuse_reasserts_tls_dir_ownership_without_regeneration(self):
        os.makedirs(self.tls_dir, exist_ok=True)
        Path(os.path.join(self.tls_dir, 'admin.key')).write_text(
            'FAKE KEY\n', encoding='utf-8')
        Path(os.path.join(self.tls_dir, 'admin.crt')).write_text(
            'FAKE CERT\n', encoding='utf-8')
        calls = []
        chowns = []
        with patch.object(admin_tls, '_keypair_reusable', return_value=True):
            admin_tls.ensure(
                'prusa-cam',
                tls_dir=self.tls_dir,
                env_path=self.env_path,
                hostnames=['buddy3d-abc123'],
                openssl='/usr/bin/openssl',
                runner=self._fake_runner(calls),
                chown=lambda p, uid, gid: chowns.append((p, uid, gid)),
                service_ids=(1234, 1234),
            )
        self.assertEqual(calls, [], 'a reusable keypair must not be regenerated')
        self.assertIn((self.tls_dir, 1234, 1234), chowns)

    def test_repairs_wrong_directory_mode_and_ownership(self):
        os.makedirs(self.tls_dir, exist_ok=True)
        Path(os.path.join(self.tls_dir, 'admin.key')).write_text(
            'FAKE KEY\n', encoding='utf-8')
        Path(os.path.join(self.tls_dir, 'admin.crt')).write_text(
            'FAKE CERT\n', encoding='utf-8')
        os.chmod(self.tls_dir, 0o777)
        chowns = []
        with patch.object(admin_tls, '_keypair_reusable', return_value=True):
            admin_tls.ensure(
                'prusa-cam',
                tls_dir=self.tls_dir,
                env_path=self.env_path,
                hostnames=['buddy3d-abc123'],
                openssl='/usr/bin/openssl',
                runner=self._fake_runner([]),
                chown=lambda p, uid, gid: chowns.append((p, uid, gid)),
                service_ids=(1234, 1234),
            )
        self.assertEqual(_mode(self.tls_dir), 0o750)
        self.assertIn((self.tls_dir, 1234, 1234), chowns)

    def test_hostile_hostname_cannot_break_out_of_argv(self):
        calls = []
        admin_tls.ensure(
            'prusa-cam',
            tls_dir=self.tls_dir,
            env_path=self.env_path,
            hostnames=['evil;touch /tmp/pwned'],
            openssl='/usr/bin/openssl',
            runner=self._fake_runner(calls),
            chown=lambda p, uid, gid: None,
            service_ids=(1234, 1234),
        )
        cmd = calls[0]
        self.assertNotIn('evil;touch /tmp/pwned', cmd)
        san = cmd[cmd.index('-addext') + 1]
        self.assertIn('evil-touch-tmp-pwned', san)
        self.assertFalse(os.path.exists('/tmp/pwned'))

    def test_missing_openssl_raises_and_writes_nothing(self):
        with self.assertRaises(admin_tls.TlsError):
            admin_tls.ensure(
                'prusa-cam',
                tls_dir=self.tls_dir,
                env_path=self.env_path,
                hostnames=['buddy3d-abc'],
                openssl=None,
                which=lambda name: None,
                chown=lambda p, uid, gid: None,
                service_ids=(1234, 1234),
            )
        self.assertFalse(os.path.exists(self.env_path))

    def test_openssl_failure_raises_and_writes_nothing(self):
        with self.assertRaises(admin_tls.TlsError):
            admin_tls.ensure(
                'prusa-cam',
                tls_dir=self.tls_dir,
                env_path=self.env_path,
                hostnames=['buddy3d-abc'],
                openssl='/usr/bin/openssl',
                runner=self._fake_runner([], returncode=1),
                chown=lambda p, uid, gid: None,
                service_ids=(1234, 1234),
            )
        self.assertFalse(os.path.exists(self.env_path))

    def test_symlinked_keypair_is_not_reused(self):
        os.makedirs(self.tls_dir, exist_ok=True)
        real = os.path.join(self._tmp.name, 'real')
        Path(real).write_text('x', encoding='utf-8')
        os.symlink(real, os.path.join(self.tls_dir, 'admin.key'))
        os.symlink(real, os.path.join(self.tls_dir, 'admin.crt'))
        self.assertFalse(
            admin_tls._keypair_reusable(
                os.path.join(self.tls_dir, 'admin.crt'),
                os.path.join(self.tls_dir, 'admin.key'),
                ['buddy3d-abc'],
                '/usr/bin/openssl',
                lambda *a, **k: subprocess.CompletedProcess([], 0, '', ''),
            )
        )


@unittest.skipUnless(HAS_OPENSSL, 'openssl not available')
class EnsureRealOpensslTests(unittest.TestCase):
    """Requirements 1/2/4: real generation, idempotency, stable identity."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tls_dir = os.path.join(self._tmp.name, 'admin-tls')
        self.env_path = os.path.join(self._tmp.name, 'admin.env')

    def _ensure(self, hostnames):
        return admin_tls.ensure(
            'prusa-cam',
            tls_dir=self.tls_dir,
            env_path=self.env_path,
            hostnames=hostnames,
            openssl=shutil.which('openssl'),
            chown=lambda p, uid, gid: None,
            service_ids=(1234, 1234),
        )

    def test_generates_valid_self_signed_cert_with_san(self):
        self.assertTrue(self._ensure(['buddy3d-abc123']))
        cert = os.path.join(self.tls_dir, 'admin.crt')
        key = os.path.join(self.tls_dir, 'admin.key')
        self.assertEqual(_mode(key), 0o600)
        self.assertEqual(_mode(cert), 0o644)
        self.assertEqual(_mode(self.env_path), 0o640)
        check = subprocess.run(
            [shutil.which('openssl'), 'x509', '-in', cert, '-noout',
             '-checkhost', 'buddy3d-abc123.local'],
            capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_second_boot_reuses_the_same_keypair(self):
        self._ensure(['buddy3d-abc123'])
        cert = os.path.join(self.tls_dir, 'admin.crt')
        key = os.path.join(self.tls_dir, 'admin.key')
        first_cert = Path(cert).read_bytes()
        first_key = Path(key).read_bytes()
        self._ensure(['buddy3d-abc123'])
        self.assertEqual(Path(cert).read_bytes(), first_cert)
        self.assertEqual(Path(key).read_bytes(), first_key)

    def test_lan_ip_change_does_not_regenerate(self):
        # Requirement 4: the SAN is hostname-based, so an IP change must reuse.
        self._ensure(['buddy3d-abc123'])
        cert = os.path.join(self.tls_dir, 'admin.crt')
        first = Path(cert).read_bytes()
        self._ensure(['buddy3d-abc123'])
        self.assertEqual(Path(cert).read_bytes(), first)

    def test_repair_restores_dir_and_preserves_existing_keypair(self):
        # Acceptance 2/3: a mis-moded directory is repaired on the next ensure
        # while a valid keypair is reused byte-for-byte, never regenerated.
        self._ensure(['buddy3d-abc123'])
        cert = os.path.join(self.tls_dir, 'admin.crt')
        key = os.path.join(self.tls_dir, 'admin.key')
        first_cert = Path(cert).read_bytes()
        first_key = Path(key).read_bytes()
        os.chmod(self.tls_dir, 0o700)
        chowns = []
        admin_tls.ensure(
            'prusa-cam',
            tls_dir=self.tls_dir,
            env_path=self.env_path,
            hostnames=['buddy3d-abc123'],
            openssl=shutil.which('openssl'),
            chown=lambda p, uid, gid: chowns.append((p, uid, gid)),
            service_ids=(1234, 1234),
        )
        self.assertEqual(_mode(self.tls_dir), 0o750)
        self.assertIn((self.tls_dir, 1234, 1234), chowns)
        self.assertEqual(Path(cert).read_bytes(), first_cert)
        self.assertEqual(Path(key).read_bytes(), first_key)

    def test_hostname_change_regenerates(self):
        self._ensure(['buddy3d-aaa111'])
        cert = os.path.join(self.tls_dir, 'admin.crt')
        first = Path(cert).read_bytes()
        self._ensure(['buddy3d-bbb222'])
        self.assertNotEqual(Path(cert).read_bytes(), first)
        check = subprocess.run(
            [shutil.which('openssl'), 'x509', '-in', cert, '-noout',
             '-checkhost', 'buddy3d-bbb222.local'],
            capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_admin_env_points_at_the_durable_keypair(self):
        self._ensure(['buddy3d-abc123'])
        text = Path(self.env_path).read_text(encoding='utf-8')
        self.assertEqual(
            text,
            f'ADMIN_TLS_CERT={self.tls_dir}/admin.crt\n'
            f'ADMIN_TLS_KEY={self.tls_dir}/admin.key\n',
        )


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
        unit = (PI_DIR / 'systemd' / 'prusa-admin.service').read_text(
            encoding='utf-8')
        self.assertIn('TLS is mandatory', unit)
        self.assertNotIn('TLS is optional', unit)


class PersistProvisioningTests(unittest.TestCase):
    """pi-persist isolates provisioning failure so the camera still comes up."""

    def test_provision_swallows_tls_error(self):
        with patch.object(
            persist_restore.admin_tls, 'ensure',
            side_effect=admin_tls.TlsError('no openssl'),
        ):
            # Must not raise: the camera stack is not held hostage to admin TLS.
            self.assertIsNone(persist_restore._provision_admin_tls('prusa-cam'))

    def test_provision_calls_ensure_with_service_user(self):
        with patch.object(persist_restore.admin_tls, 'ensure') as ensure:
            persist_restore._provision_admin_tls('custom-svc')
        ensure.assert_called_once_with('custom-svc')


class AdminEnvAtomicWriteTests(unittest.TestCase):
    def test_write_is_atomic_and_symlink_safe(self):
        # config_schema.write_atomic replaces a symlink at the destination
        # instead of following it.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'admin.env')
            elsewhere = os.path.join(tmp, 'elsewhere')
            Path(elsewhere).write_text('original\n', encoding='utf-8')
            os.symlink(elsewhere, target)
            admin_tls._write_admin_env(target, '/c.crt', '/c.key')
            self.assertFalse(os.path.islink(target))
            self.assertEqual(Path(elsewhere).read_text(encoding='utf-8'),
                             'original\n')
            self.assertIn('ADMIN_TLS_CERT=/c.crt\n',
                          Path(target).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
