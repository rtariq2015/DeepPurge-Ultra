from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deeppurge.backends import AppImageBackend, LocalBackend, appimage_verified
from deeppurge.cleanup import candidate, clean_selected, leftover_candidates, trash_candidate, validate_path
from deeppurge.models import Application


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / 'home'
        self.home.mkdir()

    def test_boundaries_root_traversal_and_foreign_ownership(self):
        root = self.home / 'data'; root.mkdir()
        path = root / 'editor'; path.mkdir()
        self.assertTrue(validate_path(path, root))
        for forbidden in (root, self.home, root / '..' / 'data/editor'):
            with self.assertRaises(ValueError):
                validate_path(forbidden, root)
        with patch('deeppurge.cleanup.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                validate_path(path, root)

    def test_symlink_parent_refused_and_leaf_not_resolved(self):
        outside = Path(self.temporary.name) / 'outside'; outside.mkdir()
        (outside / 'keep').write_text('safe')
        link = self.home / 'link'; link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            validate_path(link / 'keep', self.home)
        leaf = self.home / 'leaf'; leaf.symlink_to(outside, target_is_directory=True)
        item = candidate(leaf, self.home, 'test')
        with patch('deeppurge.cleanup.Gio.File.new_for_path') as gio:
            gio.return_value.trash.side_effect = lambda _: leaf.unlink() or True
            trash_candidate(item)
        self.assertEqual((outside / 'keep').read_text(), 'safe')

    def test_replaced_path_rejected_before_trash(self):
        path = self.home / 'data'; path.mkdir()
        item = candidate(path, self.home, 'test')
        path.rename(self.home / 'original'); path.mkdir()
        with patch('deeppurge.cleanup.Gio.File.new_for_path') as gio:
            with self.assertRaises(ValueError):
                trash_candidate(item)
            gio.assert_not_called()

    def test_only_verified_namespaces_not_similar_names(self):
        (self.home / '.var/app/org.example.Editor').mkdir(parents=True)
        (self.home / '.config/editor').mkdir(parents=True)
        app = Application('flat', 'flatpak', 'Editor', 'org.example.Editor', 'Flatpak')
        with patch('deeppurge.cleanup.Path.home', return_value=self.home):
            items, _ = leftover_candidates(app)
            self.assertEqual([item.path for item in items], [self.home / '.var/app/org.example.Editor'])
            local = replace(app, backend='local', format='Custom/Local')
            self.assertEqual(leftover_candidates(local)[0], ())

    def test_trash_failure_never_falls_back_to_permanent_delete(self):
        path = self.home / 'data'; path.mkdir()
        item = candidate(path, self.home, 'test')
        with patch('deeppurge.cleanup.Gio.File.new_for_path') as gio:
            gio.return_value.trash.return_value = False
            result = clean_selected((item,))
        self.assertEqual(result.status, 'failed')
        self.assertTrue(path.exists())

    def test_in_place_content_change_invalidates_preview(self):
        path = self.home / 'data'; path.write_text('original')
        item = candidate(path, self.home, 'test')
        path.write_text('changed contents in the same inode')
        with patch('deeppurge.cleanup.Gio.File.new_for_path') as gio:
            with self.assertRaises(ValueError):
                trash_candidate(item)
            gio.assert_not_called()

    def test_package_managed_appimage_launcher_is_retained(self):
        image = self.home / 'Editor.AppImage'
        image.write_bytes(b'\x7fELF\x00\x00\x00\x00AI\x02fixture'); image.chmod(0o700)
        root = self.home / '.local/share/applications'; root.mkdir(parents=True)
        launcher = root / 'editor.desktop'; launcher.write_text('package-managed integration')
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.home / '.local/share')}), patch(
                'deeppurge.backends.Path.home', return_value=self.home), patch(
                'deeppurge.backends.debian_owned', side_effect=lambda path: path == launcher):
            app = Application('image', 'appimage', 'Editor', image.name, 'AppImage',
                executable=image, identity=appimage_verified(image), launchers=(launcher,))
            preview = AppImageBackend().prepare(app)
        self.assertTrue(preview.allowed)
        self.assertEqual(preview.affected, (str(image),))

    def test_appimage_header_and_symlink_validation(self):
        image = self.home / 'Editor.AppImage'
        image.write_bytes(b'\x7fELF\x00\x00\x00\x00AI\x02fixture')
        image.chmod(0o700)
        with patch('deeppurge.backends.Path.home', return_value=self.home):
            self.assertTrue(appimage_verified(image))
            link = self.home / 'Link.AppImage'; link.symlink_to(image)
            with self.assertRaises(RuntimeError):
                appimage_verified(link)
            app = Application('image', 'appimage', 'Editor', image.name, 'AppImage',
                              executable=image, identity=appimage_verified(image))
            self.assertTrue(AppImageBackend().prepare(app).allowed)
        image.write_text('not an appimage')
        with patch('deeppurge.backends.Path.home', return_value=self.home):
            with self.assertRaises(RuntimeError):
                appimage_verified(image)

    def test_unknown_system_launcher_is_unsupported(self):
        app = Application('local', 'local', 'Unknown', 'unknown', 'Custom/Local',
                          launchers=(Path('/usr/share/applications/unknown.desktop'),))
        self.assertFalse(LocalBackend().prepare(app).allowed)
