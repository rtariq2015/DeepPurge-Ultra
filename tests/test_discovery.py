from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deeppurge.discovery import DesktopEntry, application_directories, reconcile, scan_desktops
from deeppurge.models import Application


class DiscoveryTests(unittest.TestCase):
    def test_hidden_user_override_masks_system_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            user, system = Path(directory) / 'user', Path(directory) / 'system'
            user.mkdir(); system.mkdir()
            (user / 'editor.desktop').write_text('[Desktop Entry]\nType=Application\nHidden=true\n')
            (system / 'editor.desktop').write_text('[Desktop Entry]\nType=Application\nName=Editor\nExec=/bin/true\n')
            self.assertEqual(scan_desktops((user, system)), ())

    def test_invalid_gio_metadata_does_not_abort_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'stale.desktop').write_text('[Desktop Entry]\nType=Application\nName=Stale\nExec=/missing/editor\n')
            records = scan_desktops((root,))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].name, 'Stale')

    def test_xdg_directories_and_invalid_relative_override(self):
        with patch.dict(os.environ, {'XDG_DATA_HOME': 'relative', 'XDG_DATA_DIRS': '/a:/b:relative'}):
            roots = application_directories()
        self.assertEqual(roots[0], Path.home() / '.local/share/applications')
        self.assertIn(Path('/a/applications'), roots)
        self.assertNotIn(Path('relative/applications'), roots)

    def test_duplicate_launchers_do_not_merge_flatpak_installations(self):
        app = Application('user', 'flatpak', 'Editor', 'org.example.Editor', 'Flatpak', scope='user')
        system = replace(app, key='system', scope='system')
        entry = DesktopEntry(Path('/usr/share/applications/editor.desktop'), 'editor.desktop',
                             'Friendly editor', 'editor', None, flatpak_id=app.identifier)
        records = reconcile((app, system), (entry, entry), {})
        self.assertEqual(len(records), 2)
        self.assertEqual({record.scope for record in records}, {'user', 'system'})
        self.assertTrue(all(len(record.launchers) == 1 for record in records))

    def test_debian_owner_identity_and_unknown_system_entry(self):
        app = Application('deb:editor', 'deb', 'editor', 'editor', 'APT/Deb', arch='amd64')
        entry = DesktopEntry(Path('/usr/share/applications/editor.desktop'), 'editor.desktop',
                             'Editor', 'editor', None)
        unknown = replace(entry, path=Path('/usr/share/applications/unknown.desktop'), desktop_id='unknown.desktop')
        records = reconcile((app,), (entry, unknown), {entry.path: 'editor'})
        self.assertEqual(len(records), 2)
        self.assertEqual(next(item for item in records if item.backend == 'deb').identifier, 'editor')
        self.assertFalse(next(item for item in records if item.backend == 'local').identity)
