import subprocess
import unittest
from unittest.mock import patch

from deeppurge.backends import Backend, BackendError, DebianBackend, FlatpakBackend, SnapBackend
from deeppurge.models import Application, Capability, Preview


class BackendTests(unittest.TestCase):
    def test_flatpak_distinct_scopes_architectures_and_branches(self):
        output = "org.example.App/x86_64/stable\tApp\tuser\norg.example.App/x86_64/stable\tApp\tsystem\norg.example.App/aarch64/beta\tApp Beta\tlab\n--bad/x86_64/stable\tBad\tuser\n"
        with patch('deeppurge.backends.shutil.which', return_value='/usr/bin/flatpak'), patch(
                'deeppurge.backends.command', return_value=subprocess.CompletedProcess([], 0, output)):
            apps = FlatpakBackend().discover()
        self.assertEqual(len(apps), 3)
        self.assertEqual(len({app.key for app in apps}), 3)
        self.assertEqual(FlatpakBackend.scope_args(apps[2]), ['--installation=lab'])

    def test_missing_flatpak(self):
        with patch('deeppurge.backends.shutil.which', return_value=None):
            self.assertFalse(FlatpakBackend().capability().can_remove)
            self.assertEqual(FlatpakBackend().discover(), ())

    def test_flatpak_exact_removal_arguments_preserve_data(self):
        app = Application('key', 'flatpak', 'App', 'org.example.App', 'Flatpak',
                          scope='user', arch='x86_64', branch='beta')
        with patch('deeppurge.backends.native_command') as run:
            FlatpakBackend().remove(Preview(app, '', (), ''), lambda *_: None)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], 'app/org.example.App/x86_64/beta')
        self.assertIn('--user', argv)
        self.assertIn('--no-related', argv)
        self.assertNotIn('--delete-data', argv)

    def test_identifier_option_injection_rejected(self):
        app = Application('bad', 'flatpak', 'Bad', '--delete-data', 'Flatpak',
                          scope='user', arch='x86_64', branch='stable')
        with self.assertRaises(BackendError):
            FlatpakBackend.ref(app)
        app = Application('bad', 'snap', 'Bad', '--purge', 'Snap')
        with self.assertRaises(BackendError):
            SnapBackend().prepare(app)

    def test_snap_tracks_actual_change_and_purge_choice(self):
        app = Application('snap:editor', 'snap', 'Editor', 'editor', 'Snap', branch='5')
        backend = SnapBackend()
        responses = [{'type': 'async', 'change': '123'}, {'result': {
            'ready': True, 'status': 'Done', 'tasks': []}}]
        with patch.object(backend, 'request', side_effect=responses) as request:
            backend.remove(Preview(app, '', (), ''), lambda *_: None, purge=True)
        self.assertEqual(request.call_args_list[0].args,
                         ('POST', '/v2/snaps/editor', {'action': 'remove', 'purge': True}))
        self.assertEqual(request.call_args_list[1].args, ('GET', '/v2/changes/123'))

    def test_snap_failure_does_not_claim_success(self):
        app = Application('snap:editor', 'snap', 'Editor', 'editor', 'Snap')
        backend = SnapBackend()
        with patch.object(backend, 'request', side_effect=[{'type': 'async', 'change': '1'},
              {'result': {'ready': True, 'status': 'Error', 'err': 'locked'}}]):
            with self.assertRaisesRegex(BackendError, 'locked'):
                backend.remove(Preview(app, '', (), ''), lambda *_: None)

    def test_disabled_snap_revision_still_counts_as_installed(self):
        backend = SnapBackend()
        app = Application('snap:editor', 'snap', 'Editor', 'editor', 'Snap')
        with patch.object(backend, 'request', return_value={'result': [
                {'name': 'editor', 'status': 'installed', 'revision': '4'}]}) as request:
            self.assertTrue(backend.exists(app))
            request.assert_called_once_with('GET', '/v2/snaps?select=all')

    def test_protected_debian_package_has_no_action(self):
        app = Application('deb:core', 'deb', 'Core', 'core', 'APT/Deb', protected=True)
        self.assertFalse(DebianBackend().prepare(app).allowed)

    def test_debian_parsing_requires_installed_status(self):
        output = 'editor\t1.2\tamd64\tinstalled\tno\tno\nold\t1\tamd64\tconfig-files\tno\tno\ncore\t1\tamd64\tinstalled\tyes\tno\n'
        with patch('deeppurge.backends.shutil.which', return_value='dpkg-query'), patch(
                'deeppurge.backends.command', return_value=subprocess.CompletedProcess([], 0, output)):
            apps = DebianBackend().discover()
        self.assertEqual([app.identifier for app in apps], ['editor', 'core'])
        self.assertTrue(apps[1].protected)

    def test_partial_debian_state_is_not_reported_removed(self):
        app = Application('deb:editor', 'deb', 'Editor', 'editor', 'APT/Deb')
        for state in ('half-installed', 'unpacked', 'half-configured', 'triggers-pending'):
            with self.subTest(state=state), patch('deeppurge.backends.command',
                    return_value=subprocess.CompletedProcess([], 0, state)):
                self.assertTrue(DebianBackend().exists(app))

    def test_actual_removal_result_requires_requery(self):
        class FixtureBackend(Backend):
            def prepare(self, app):
                return Preview(app, 'Remove', ('package',), '', token=('version',))
            def remove(self, preview, publish):
                pass
            def exists(self, app):
                return True
        app = Application('key', 'fixture', 'App', 'app', 'Custom/Local')
        backend = FixtureBackend()
        result = backend.uninstall(backend.prepare(app), lambda *_: None)
        self.assertEqual(result.status, 'failed')
        self.assertFalse(result.removed)
        with patch.object(backend, 'exists', return_value=False):
            self.assertTrue(backend.uninstall(backend.prepare(app), lambda *_: None).removed)
        with patch.object(backend, 'exists', side_effect=BackendError('database unavailable')):
            self.assertEqual(backend.uninstall(backend.prepare(app), lambda *_: None).status, 'unverified')
        with patch.object(backend, 'remove', side_effect=BackendError('authentication cancelled')):
            self.assertEqual(backend.uninstall(backend.prepare(app), lambda *_: None).status, 'cancelled')

    def test_changed_preview_prevents_removal(self):
        backend = Backend()
        app = Application('key', 'fixture', 'App', 'app', 'Custom/Local')
        original = Preview(app, 'Remove', ('one',), '', token=('one',))
        with patch.object(backend, 'prepare', create=True, return_value=Preview(
                app, 'Remove', ('two',), '', token=('two',))), patch.object(
                backend, 'remove', create=True) as remove, patch.object(
                backend, 'exists', create=True, return_value=True):
            result = backend.uninstall(original, lambda *_: None)
        remove.assert_not_called()
        self.assertFalse(result.removed)
