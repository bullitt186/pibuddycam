"""WP-UI2 (AC-4/AC-14): static checks for main.py's runtime bridge wiring.

``main.py`` imports aiohttp/socketio/gi, so it is parsed as source with
:mod:`ast` rather than imported. These checks pin the contract: the dashboard
payload is built from the live coordinator and off the event loop, the runtime
server is started and stopped with the process, and the snapshot paths record
the observations the dashboard reports.
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PI_DIR = REPO / 'app'
MAIN_PY = PI_DIR / 'main.py'
sys.path.insert(0, str(PI_DIR))


def _tree():
    return ast.parse(MAIN_PY.read_text(encoding='utf-8'))


def _functions(tree, name):
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]


def _calls(tree):
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _attr_chains(node):
    chains = []
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            parts = []
            current = child
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
                chains.append(list(reversed(parts)))
    return chains


def _imports(tree):
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or '')
    return imported


class MainDashboardWiringTests(unittest.TestCase):
    def setUp(self):
        self.tree = _tree()
        self.chains = _attr_chains(self.tree)

    def test_imports_the_runtime_bridge(self):
        imported = _imports(self.tree)
        self.assertIn('dashboard', imported)
        self.assertIn('runtime_ipc', imported)

    def test_builds_the_dashboard_from_live_sources(self):
        functions = _functions(self.tree, 'dashboard_payload')
        self.assertEqual(len(functions), 1, 'expected one dashboard_payload')
        text = ast.unparse(functions[0])
        for marker in (
            'dashboard.build_dashboard',
            'app_metrics.metrics_provider',
            'updater_install.read_update_state',
            'coordinator.authoritative',
            'app_version.application_version',
            'app_version.build_identity',
            'secrets=',
        ):
            self.assertIn(marker, text, marker)

    def test_dashboard_payload_is_not_a_second_coordinator(self):
        # AC-4: main.py must not construct another CameraState/SettingsCoordinator.
        source = MAIN_PY.read_text(encoding='utf-8')
        self.assertEqual(source.count('SettingsCoordinator('), 1)
        self.assertEqual(source.count('CameraState('), 1)

    def test_runtime_server_is_started_and_stopped(self):
        self.assertIn(['runtime_ipc', 'RuntimeServer'], self.chains)
        self.assertIn(['runtime_server', 'start'], self.chains)
        self.assertIn(['runtime_server', 'stop'], self.chains)

    def test_runtime_server_registers_the_dashboard_handler(self):
        server_calls = [
            call for call in _calls(self.tree)
            if any(chain == ['runtime_ipc', 'RuntimeServer']
                   for chain in _attr_chains(call))
        ]
        self.assertTrue(server_calls)
        handlers = [
            keyword for call in server_calls for keyword in call.keywords
            if keyword.arg == 'handlers'
        ]
        self.assertTrue(handlers, 'RuntimeServer must receive a handlers table')
        handler_text = ast.unparse(handlers[0].value)
        self.assertIn('dashboard', handler_text)

    def test_runtime_server_start_failure_is_non_fatal(self):
        start_calls = [
            call for call in _calls(self.tree)
            if any(chain == ['runtime_server', 'start'] for chain in _attr_chains(call))
        ]
        self.assertTrue(start_calls)
        # The start is wrapped in a try whose handler logs and continues.
        text = MAIN_PY.read_text(encoding='utf-8')
        self.assertIn('Runtime control socket unavailable', text)

    def test_snapshot_paths_record_observations(self):
        for name in ('snapshot_loop', 'dispatch_trigger_action'):
            functions = _functions(self.tree, name)
            self.assertEqual(len(functions), 1, name)
            text = ast.unparse(functions[0])
            for marker in (
                'state.last_capture_at', 'state.last_capture_ok',
                'state.last_snapshot_at', 'state.last_snapshot_ok',
            ):
                self.assertIn(marker, text, f'{name}: {marker}')

    def test_mqtt_secrets_are_passed_to_the_dashboard(self):
        text = MAIN_PY.read_text(encoding='utf-8')
        self.assertIn('mqtt_secrets', text)
        self.assertIn('(token, fingerprint) + mqtt_secrets', text)

    def test_no_systemctl_in_the_dashboard_handler(self):
        text = ast.unparse(_functions(self.tree, 'dashboard_payload')[0])
        self.assertNotIn('systemctl', text)
        self.assertNotIn('subprocess', text)


class MainSettingsDispatchWiringTests(unittest.TestCase):
    """WP-UI3: main.py marshals IPC mutations onto the owning event loop."""

    def setUp(self):
        self.tree = _tree()
        self.source = MAIN_PY.read_text(encoding='utf-8')

    def test_imports_the_settings_dispatcher(self):
        self.assertIn('settings_dispatch', _imports(self.tree))

    def test_builds_an_event_loop_dispatcher(self):
        chains = _attr_chains(self.tree)
        self.assertIn(['settings_dispatch', 'EventLoopMutationDispatcher'], chains)
        calls = [
            call for call in _calls(self.tree)
            if any(chain == ['settings_dispatch', 'EventLoopMutationDispatcher']
                   for chain in _attr_chains(call))
        ]
        self.assertTrue(calls)
        text = ast.unparse(calls[0])
        self.assertIn('coordinator', text)
        self.assertIn('loop', text)

    def test_settings_handler_dispatches_through_the_marshaller(self):
        functions = _functions(self.tree, 'apply_setting')
        self.assertEqual(len(functions), 1)
        text = ast.unparse(functions[0])
        self.assertIn('settings_dispatcher.apply', text)
        # The IPC handler must not call the coordinator directly: the marshaller
        # is the single owner boundary.
        self.assertNotIn('coordinator.apply_mutation', text)

    def test_runtime_server_registers_the_settings_handler(self):
        server_calls = [
            call for call in _calls(self.tree)
            if any(chain == ['runtime_ipc', 'RuntimeServer']
                   for chain in _attr_chains(call))
        ]
        handlers = [
            keyword for call in server_calls for keyword in call.keywords
            if keyword.arg == 'handlers'
        ]
        handler_text = ast.unparse(handlers[0].value)
        self.assertIn('settings.set', handler_text)

    def test_only_one_coordinator_apply_mutation_call_site(self):
        # AC-4: the coordinator is mutated through the marshaller; main.py must
        # not add a second direct apply_mutation path.
        self.assertEqual(self.source.count('apply_mutation('), 0)



if __name__ == '__main__':
    unittest.main()
