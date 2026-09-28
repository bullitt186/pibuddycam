"""Unit tests for ``app/``.

Importing this package redirects every module-level path that points into the real
``/data`` (or that would remount ``/``) to a throw-away directory, so a test that
forgets to inject a path can never read or write the machine's own durable state.
Before this, a run as root left ``/data/pibuddycam/update-state.json`` behind, which
then made ``app_version`` report a different version in later runs.

Tests that exercise those modules still inject their own paths explicitly; this is
only the safety net for the ones that do not.
"""
import atexit
import os
import shutil
import sys
import tempfile

_APP_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'app')
if _APP_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_APP_DIR))

_SANDBOX = tempfile.mkdtemp(prefix='pibuddycam-tests-')
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)


def _isolate():
    import app_version
    import migrations
    import settings_store
    import updater_install

    data = os.path.join(_SANDBOX, 'data')
    os.makedirs(os.path.join(data, 'pibuddycam'), exist_ok=True)

    migrations.MIGRATION_STATE_PATH = os.path.join(data, 'pibuddycam', 'migrations.json')
    app_version.RELEASE_STATE_PATH = os.path.join(data, 'pibuddycam', 'update-state.json')
    app_version.RELEASE_METADATA_PATH = os.path.join(
        data, 'pibuddycam', 'releases', 'current', 'release.json')
    updater_install.DEFAULT_UPDATE_STATE_PATH = os.path.join(
        data, 'pibuddycam', 'update-state.json')
    # ``updater_install.main`` builds its argparse defaults from DATA_ROOT on each
    # call, so a CLI test without ``--data-root`` would write the real state file.
    updater_install.DATA_ROOT = os.path.join(data, 'pibuddycam')
    # Not a mountpoint: settings_store.available() stays False, so nothing persists.
    settings_store.DATA_MOUNT = os.path.join(data, 'not-a-mount')

    # A call that leaves ``root`` at its default would remount the real ``/``
    # read-write. Tests of the migrations themselves pass an explicit tree.
    real_run_pending = migrations.run_pending

    def run_pending(root='/', state_path=migrations._UNSET):
        if root == '/':
            return []
        return real_run_pending(root, state_path)

    run_pending.__wrapped__ = real_run_pending
    migrations.run_pending = run_pending


_isolate()
