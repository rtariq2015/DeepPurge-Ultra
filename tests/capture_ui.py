#!/usr/bin/python3
"""Capture GTK layout fixtures without exposing the user's application inventory."""
from pathlib import Path
import runpy
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
runpy.run_path(str(ROOT / 'src/deep_purge.py'), run_name='runtime_setup')
from deeppurge.app import ASSETS, Dashboard
from deeppurge.history import History
from deeppurge.models import Application, Capability, Inventory, Preview
from gi.repository import Gdk, Gio, GLib, Gtk


def main():
    destination = ROOT / 'docs/screenshots'
    destination.mkdir(parents=True, exist_ok=True)
    fixtures = (
        ('Archive Manager', 'org.example.Archive', 'APT/Deb', 'deb', 'system', 'org.gnome.ArchiveManager'),
        ('Calculator', 'org.example.Calculator', 'Flatpak', 'flatpak', 'user', 'org.gnome.Calculator'),
        ('Calendar', 'org.example.Calendar', 'Flatpak', 'flatpak', 'system', 'org.gnome.Calendar'),
        ('Camera', 'org.example.Camera', 'APT/Deb', 'deb', 'system', 'camera-photo'),
        ('Design Studio', 'DesignStudio.AppImage', 'AppImage', 'appimage', 'user file', 'applications-graphics'),
        ('Disk Usage Analyzer', 'org.example.DiskUsage', 'APT/Deb', 'deb', 'system', 'org.gnome.baobab'),
        ('Document Viewer', 'org.example.Documents', 'Flatpak', 'flatpak', 'user', 'org.gnome.Evince'),
        ('Files', 'org.example.Files', 'APT/Deb', 'deb', 'system', 'org.gnome.Nautilus'),
        ('Local workspace', 'example-workspace', 'Custom/Local', 'local', 'user shortcut', 'folder-development'),
        ('Desktop Platform', 'example-platform', 'Snap', 'snap', 'system', 'applications-system'),
        ('Notes', 'org.example.Notes', 'Flatpak', 'flatpak', 'user', 'accessories-text-editor'),
        ('Text Editor', 'org.example.TextEditor', 'APT/Deb', 'deb', 'system', 'org.gnome.TextEditor'),
    )
    apps = tuple(Application(str(index), backend, name, identifier, kind, scope=scope,
                             arch='x86_64' if backend == 'flatpak' else '',
                             branch='stable' if backend == 'flatpak' else '', icon=icon,
                             protected=identifier == 'example-platform')
                 for index, (name, identifier, kind, backend, scope, icon) in enumerate(fixtures))
    capabilities = {key: Capability(True, True, 'Layout fixture — not a real installation')
                    for key in ('flatpak', 'snap', 'deb', 'appimage', 'local')}
    inventory = Inventory(apps, capabilities)
    settings = Gtk.Settings.get_default()
    settings.set_property('gtk-application-prefer-dark-theme', True)
    css = Gtk.CssProvider(); css.load_from_path(str(ASSETS / 'style.css'))
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    application = Gtk.Application(application_id='io.github.deeppurge.Captures', flags=Gio.ApplicationFlags.NON_UNIQUE)
    application.register(None)
    context = GLib.MainContext.default()

    def settle(seconds=.35):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(.01)

    def capture(widget, name, wait=True):
        print('Capturing', name, flush=True)
        if wait:
            settle()
        pixbuf = Gdk.pixbuf_get_from_window(widget.get_window(), 0, 0,
                                           widget.get_allocated_width(), widget.get_allocated_height())
        pixbuf.savev(str(destination / name), 'png', [], [])

    with tempfile.TemporaryDirectory(prefix='deeppurge-capture-') as directory, patch.dict(
            'os.environ', {'XDG_STATE_HOME': directory}), patch('deeppurge.app.scan', return_value=inventory) as inventory_scan:
        window = Dashboard(application, history=History())
        Gdk.Display.get_default().get_default_seat().get_pointer().warp(Gdk.Screen.get_default(), 0, 0)
        settle(1)
        assert not window.scanning and len(window.cards) == len(apps)
        capture(window, 'applications-wide.png')
        window.resize(480, 850); settle(); window.resize(480, 850)
        capture(window, 'applications-narrow.png')
        window.resize(1120, 840)
        window.search.set_text('no-matching-layout-fixture'); window.filter_changed()
        capture(window, 'empty-search.png')
        window.clear_filters(); settle()
        failed_capabilities = dict(capabilities)
        failed_capabilities['snap'] = Capability(False, False, 'Controlled screenshot fixture: snapd unavailable', True)
        inventory_scan.return_value = Inventory(tuple(app for app in apps if app.backend != 'snap'),
            failed_capabilities, ('snap: Controlled screenshot fixture: snapd unavailable',))
        window.refresh(); settle(1)
        assert window.scan_notice.get_visible()
        capture(window, 'incomplete-scan.png')
        inventory_scan.return_value = inventory
        window.refresh(); settle(1)
        window.activity_tab.set_active(True)
        capture(window, 'activity.png')
        window.applications_tab.set_active(True); settle()
        old_dpi = settings.get_property('gtk-xft-dpi')
        settings.set_property('gtk-xft-dpi', int(96 * 1024 * 1.35))
        window.resize(1280, 1000)
        capture(window, 'large-text.png')
        settings.set_property('gtk-xft-dpi', old_dpi)
        window.resize(1120, 840); settle()
        preview = Preview(apps[1], 'Uninstall application',
            ('user · app/org.example.Calculator/x86_64/stable',),
            'Flatpak removes this exact installation and its managed integration. '
            'Application data stays in your account. Related refs and unused runtimes are retained. '
            'This screenshot uses a layout fixture; no application is removed.')
        def capture_dialog():
            for widget in Gtk.Window.list_toplevels():
                if isinstance(widget, Gtk.Dialog):
                    capture(widget, 'uninstall-details.png', wait=False)
                    widget.response(Gtk.ResponseType.CANCEL)
            return False
        GLib.timeout_add(400, capture_dialog)
        window.details_dialog(preview)
        window.destroy()
    application.quit()
    print('Captured seven GTK states with clearly documented inventory fixtures:', destination)


if __name__ == '__main__':
    main()
