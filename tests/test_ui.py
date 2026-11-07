import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('DEEPPURGE_UI_TESTS') == '1', 'GTK display tests are opt-in')
class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gi.repository import Gio, Gtk
        cls.shared_application = Gtk.Application(application_id='io.github.deeppurge.TestHarness',
                                                 flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.shared_application.register(None)

    @classmethod
    def tearDownClass(cls):
        cls.shared_application.quit()

    def setUp(self):
        from deeppurge.app import Dashboard, ASSETS
        from deeppurge.history import History
        from deeppurge.models import Application, Capability, Inventory
        from gi.repository import Gdk, GLib, Gtk
        self.Gdk, self.GLib, self.Gtk = Gdk, GLib, Gtk
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = patch.dict(os.environ, {'XDG_STATE_HOME': self.temporary.name})
        environment.start(); self.addCleanup(environment.stop)
        self.application = self.shared_application
        provider = Gtk.CssProvider()
        provider.load_from_path(str(ASSETS / 'style.css'))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.addCleanup(lambda: Gtk.StyleContext.remove_provider_for_screen(Gdk.Screen.get_default(), provider))
        apps = tuple(Application(str(index), 'flatpak', name, f'org.example.App{index}', kind,
                     scope='user', icon='missing-icon-fixture') for index, (name, kind) in enumerate((
            ('Alpha', 'Flatpak'), ('Zeta', 'Snap'), ('An unusually long application name for accessibility verification', 'APT/Deb'))))
        inventory = Inventory(apps, {'flatpak': Capability(True, True, 'Fixture backend')})
        scan = patch('deeppurge.app.scan', return_value=inventory)
        scan.start(); self.addCleanup(scan.stop)
        self.window = Dashboard(self.application, registry={}, history=History())
        self.addCleanup(self.window.destroy)
        self.pump(lambda: not self.window.scanning)

    def pump(self, condition, timeout=5):
        context = self.GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            if condition():
                return
            time.sleep(.01)
        self.fail('GTK callback timed out')

    def test_search_filter_sort_empty_and_keyboard(self):
        window = self.window
        self.assertEqual(len(window.cards), 3)
        window.search.set_text('org.example.App0'); window.filter_changed()
        self.assertEqual(window.count.get_text(), '1 of 3 applications')
        window.search.set_text('no-matches'); window.filter_changed()
        self.assertEqual(window.grid_stack.get_visible_child_name(), 'empty')
        window.clear_filters(); window.format_filter.set_active(2)
        self.assertEqual(window.count.get_text(), '1 of 3 applications')
        window.clear_filters(); window.sort.set_active(1)
        self.assertEqual(window.flow.get_children()[0].get_child().app.name, 'Zeta')
        self.assertTrue(window.on_key(None, SimpleNamespace(keyval=self.Gdk.KEY_f,
                                  state=self.Gdk.ModifierType.CONTROL_MASK)))
        self.assertTrue(window.applications_tab.get_active())

    def test_failed_inventory_is_not_presented_as_no_installed_apps(self):
        from deeppurge.models import Capability, Inventory
        window = self.window
        window.inventory = Inventory((), {'snap': Capability(False, False, 'Fixture outage', True)},
                                     ('snap: Fixture outage',))
        for child in window.flow.get_children():
            window.flow.remove(child)
        window.cards = []
        window.render_capabilities(); window.filter_changed(); window.update_notice()
        self.assertEqual(window.empty_title.get_text(), 'Applications could not be checked')
        self.pump(lambda: window.scan_notice.get_mapped() and window.scan_notice.get_allocated_height() > 5)
        self.assertIn('partial scan', window.count.get_text())
        self.assertEqual(window.backend_status.get_children()[0].get_child().get_text(), 'Snap · Scan failed')

    def test_failed_refresh_preserves_previous_results_and_requires_successful_retry(self):
        window = self.window
        original = window.inventory
        with patch('deeppurge.app.scan', side_effect=RuntimeError('Fixture inventory failure')):
            window.refresh()
            self.pump(lambda: not window.scanning)
        self.assertEqual(tuple(card.app.key for card in window.cards), tuple(app.key for app in original.apps))
        self.assertFalse(window.flow.get_sensitive())
        self.assertTrue(window.refresh_button.get_sensitive())
        self.assertTrue(window.scan_notice.get_visible())
        self.assertEqual(window.notice_title.get_text(), 'Refresh failed')
        with patch('deeppurge.app.scan', return_value=original):
            window.refresh()
            self.pump(lambda: not window.scanning)
        self.assertIsNone(window.scan_error)
        self.assertTrue(window.flow.get_sensitive())
        self.assertFalse(window.scan_notice.get_visible())

    def test_resize_then_refresh_allocates_new_cards_and_the_failure_notice(self):
        from dataclasses import replace
        window = self.window
        window.resize(480, 850)
        self.pump(lambda: window.get_allocated_width() < 650 and not window.hero_art.get_visible())
        window.resize(1120, 840)
        partial = replace(window.inventory, warnings=('Fixture source unavailable',))
        with patch('deeppurge.app.scan', return_value=partial):
            window.refresh()
            self.pump(lambda: not window.scanning and window.hero_art.get_visible() and
                window.scan_notice.get_allocated_height() > 5 and
                all(card.get_allocated_width() >= 240 and card.get_allocated_height() >= 110
                    for card in window.cards))
        self.assertTrue(window.scan_notice.get_mapped())
        self.assertTrue(all(card.get_mapped() for card in window.cards))

    def test_history_failure_cannot_replace_a_verified_operation_result(self):
        from deeppurge.models import OperationResult
        result = OperationResult('success', 'Fixture operation independently verified', removed=True)
        received = []
        with patch.object(self.window.history, 'append', side_effect=ValueError('Fixture history failure')), patch.object(
                self.window.history, 'read', side_effect=UnicodeError('Fixture read failure')):
            self.window.run_operation(self.window.cards[0].app, 'Fixture action', lambda: result, received.append)
            self.pump(lambda: bool(received))
        self.assertEqual(received[0].status, 'success')
        self.assertTrue(received[0].removed)
        self.assertIn('Activity could not be saved', '\n'.join(received[0].details))
        self.assertIn('Activity could not be read', '\n'.join(received[0].details))
        self.assertEqual(self.window.notice_title.get_text(), 'Activity history is unavailable')

    def test_locked_operation_does_not_call_the_backend(self):
        from unittest.mock import Mock
        backend_action, received = Mock(), []
        with self.window.history.operation_lock():
            self.window.run_operation(self.window.cards[0].app, 'Fixture action', backend_action, received.append)
            self.pump(lambda: bool(received))
        backend_action.assert_not_called()
        self.assertEqual(received[0].status, 'failed')
        self.assertIn('already running', received[0].summary)

    def test_backend_io_error_is_not_mislabelled_as_a_busy_operation_lock(self):
        from unittest.mock import Mock
        received = []
        backend_action = Mock(side_effect=BlockingIOError('Fixture backend I/O failure'))
        self.window.run_operation(self.window.cards[0].app, 'Fixture action', backend_action, received.append)
        self.pump(lambda: bool(received))
        self.assertEqual(received[0].status, 'unverified')
        self.assertNotIn('already running', received[0].summary)
        self.assertIn('Fixture backend I/O failure', '\n'.join(received[0].details))

    def test_unavailable_flatpak_blocks_leftover_selection(self):
        from deeppurge.backends import FlatpakBackend
        self.window.registry['flatpak'] = FlatpakBackend()
        with patch('deeppurge.backends.shutil.which', return_value=None), patch.object(
                self.window, 'show_message') as message, patch.object(self.window, 'cleanup_dialog') as cleanup, patch.object(
                self.window, 'refresh'):
            self.window.inspect_leftovers(self.window.cards[0].app)
            self.pump(lambda: not self.window.preparing)
        message.assert_called_once()
        cleanup.assert_not_called()

    def test_refresh_generation_shutdown_and_busy_close(self):
        window = self.window
        generation = window.generation
        window.refresh(); window.refresh()
        self.assertEqual(window.generation, generation + 1)
        self.pump(lambda: not window.scanning)
        count = len(window.cards)
        window.populate(0, window.generation - 1)
        self.assertEqual(len(window.cards), count)
        window.operation_active = True
        self.assertTrue(window.on_close())
        window.operation_active = False
        self.assertFalse(window.on_close())

    def test_details_cancel_does_not_start_removal(self):
        from deeppurge.models import Preview
        preview = Preview(self.window.cards[0].app, 'Remove fixture', ('fixture',), 'Test preview')
        def cancel():
            for widget in self.Gtk.Window.list_toplevels():
                if isinstance(widget, self.Gtk.Dialog):
                    widget.response(self.Gtk.ResponseType.CANCEL)
            return False
        self.GLib.timeout_add(50, cancel)
        with patch.object(self.window, 'run_removal') as removal:
            self.window.details_dialog(preview)
        removal.assert_not_called()

    def test_cleanup_defaults_to_no_selected_personal_data(self):
        from deeppurge.cleanup import candidate
        path = Path(self.temporary.name) / 'data'; path.mkdir()
        item = candidate(path, Path(self.temporary.name), 'Explicit test association')
        inspected = []
        def cancel():
            for widget in self.Gtk.Window.list_toplevels():
                if isinstance(widget, self.Gtk.Dialog):
                    clean = widget.get_widget_for_response(self.Gtk.ResponseType.ACCEPT)
                    self.assertFalse(clean.get_sensitive())
                    inspected.append(True)
                    widget.response(self.Gtk.ResponseType.CANCEL)
            return False
        self.GLib.timeout_add(50, cancel)
        with patch.object(self.window, 'run_operation') as operation, patch.object(self.window, 'refresh'):
            self.window.cleanup_dialog(self.window.cards[0].app, (item,), ())
        self.assertTrue(inspected)
        operation.assert_not_called()
        self.assertTrue(path.exists())

    def test_cards_grow_with_accessibility_font_size(self):
        settings = self.Gtk.Settings.get_default()
        original = settings.get_property('gtk-font-name')
        self.addCleanup(lambda: settings.set_property('gtk-font-name', original))
        before = self.window.cards[0].get_preferred_height()[0]
        settings.set_property('gtk-font-name', 'Sans 18')
        self.pump(lambda: self.window.cards[0].get_preferred_height()[0] > before)
