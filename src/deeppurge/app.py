"""GTK3 presentation; widget mutations are confined to the main thread."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
import os
from pathlib import Path
import sys

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from . import __version__
from .backends import backend_registry
from .cleanup import clean_selected, leftover_candidates
from .discovery import scan
from .history import History, OperationBusyError, configure_logging
from .models import FORMATS, Inventory, OperationResult

LOG = logging.getLogger(__name__)
ASSETS = Path(__file__).resolve().parents[2] / 'assets'


def styled(widget, *classes):
    for name in classes:
        widget.get_style_context().add_class(name)
    return widget


def label(text, *classes, wrap=False):
    widget = styled(Gtk.Label(label=text, xalign=0), *classes)
    widget.set_line_wrap(wrap)
    if wrap:
        widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        widget.set_max_width_chars(64)
    return widget


def column(spacing=12, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def app_icon(app, size=36):
    image = Gtk.Image.new_from_icon_name('application-x-executable', Gtk.IconSize.DIALOG)
    image.set_pixel_size(size)
    try:
        icon = Gio.Icon.new_for_string(app.icon)
        if isinstance(icon, Gio.FileIcon) and not icon.get_file().query_exists(None):
            return image
        if isinstance(icon, Gio.ThemedIcon) and not any(
                Gtk.IconTheme.get_default().has_icon(name) for name in icon.get_names()):
            return image
        image.set_from_gicon(icon, Gtk.IconSize.DIALOG)
    except GLib.Error:
        pass
    return image


def artwork(width=190, height=104):
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(ASSETS / 'illustrations/welcome.png'), width, height, True)
        image = Gtk.Image.new_from_pixbuf(pixbuf)
    except GLib.Error:
        image = Gtk.Image.new_from_icon_name('view-grid-symbolic', Gtk.IconSize.DIALOG)
    image.get_accessible().set_name('Decorative floating software cubes')
    return image


def scroller(child, height=0):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    if height:
        scroll.set_min_content_height(height)
    scroll.add(child)
    return scroll


def text_area(text):
    view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_left_margin(12)
    view.set_right_margin(12)
    view.set_top_margin(10)
    view.set_bottom_margin(10)
    view.get_buffer().set_text(text)
    return scroller(view, 100)


class AppCard(Gtk.Button):
    def __init__(self, app, callback):
        super().__init__()
        self.app = app
        self.search_text = f'{app.name} {app.identifier} {app.format} {app.scope}'.casefold()
        self.set_size_request(240, 110)
        self.set_hexpand(True)
        styled(self, 'app-card')
        caption = ('Protected' if app.protected else
                   'Unverified' if app.backend in {'local', 'appimage'} and not app.identity else
                   'Shortcut' if app.backend == 'local' else app.scope)
        self.set_tooltip_text(f'{app.name}\n{app.identifier}\n{app.format} · {app.scope}'
                              f'\n{app.arch} {app.branch}\n{caption}: open details to review supported actions')
        self.get_accessible().set_name(f'{app.name}, {app.format}, {app.scope}, {caption}. Review details')
        box = column(8)
        top = Gtk.Box(spacing=11)
        top.pack_start(app_icon(app), False, False, 0)
        name = label(app.name, 'app-name')
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(22)
        top.pack_start(name, True, True, 0)
        box.pack_start(top, True, True, 0)
        identifier = label(app.identifier, 'app-id')
        identifier.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        identifier.set_max_width_chars(27)
        box.pack_start(identifier, False, False, 0)
        bottom = Gtk.Box(spacing=8)
        badge = styled(Gtk.Label(label=app.format), 'badge', f'badge-{FORMATS[app.format][0]}')
        bottom.pack_start(badge, False, False, 0)
        scope = label(caption, 'warning' if caption in {'Protected', 'Unverified'} else 'muted', 'small')
        scope.set_ellipsize(Pango.EllipsizeMode.END)
        scope.set_max_width_chars(13)
        bottom.pack_end(scope, False, False, 0)
        box.pack_start(bottom, False, False, 0)
        self.add(box)
        self.connect('clicked', lambda *_args: callback(app))


class Dashboard(Gtk.ApplicationWindow):
    def __init__(self, application, registry=None, history=None):
        super().__init__(application=application, title='DeepPurge Ultra')
        self.registry = registry if registry is not None else backend_registry()
        self.history = history if history is not None else History()
        self.jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix='controller')
        self.inventory = Inventory((), {})
        self.entries, self.cards = [], []
        self.generation = 0
        self.closed = self.scanning = self.operation_active = self.preparing = False
        self.scan_error = self.history_warning = None
        self.notice_messages = ()
        self.narrow_layout = None
        self.layout_source = None
        self.set_default_size(1120, 840)
        self.set_size_request(360, 420)
        self.set_icon_name('system-software-install')
        self.connect('delete-event', self.on_close)
        self.connect('destroy', self.on_destroy)
        self.connect('key-press-event', self.on_key)
        header = Gtk.HeaderBar(title='DeepPurge Ultra', subtitle='APPLICATION MANAGER', show_close_button=True)
        try:
            mark = Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(ASSETS / 'icons/deep-purge-ultra.svg'), 30, 30, True))
        except GLib.Error:
            mark = Gtk.Image.new_from_icon_name('system-software-install', Gtk.IconSize.BUTTON)
        header.pack_start(mark)
        self.refresh_button = Gtk.Button.new_from_icon_name('view-refresh-symbolic', Gtk.IconSize.BUTTON)
        self.refresh_button.set_tooltip_text('Refresh applications (Ctrl+R)')
        self.refresh_button.get_accessible().set_name('Refresh installed applications')
        self.refresh_button.connect('clicked', lambda *_args: self.refresh())
        header.pack_end(self.refresh_button)
        self.set_titlebar(header)
        self.layout = column(16, margin=24)
        self.add(self.layout)
        navigation = styled(Gtk.Box(halign=Gtk.Align.START), 'navigation')
        self.applications_tab = Gtk.RadioButton.new_with_label_from_widget(None, 'Applications')
        self.activity_tab = Gtk.RadioButton.new_with_label_from_widget(self.applications_tab, 'Activity')
        for button in (self.applications_tab, self.activity_tab):
            button.set_mode(False)
            navigation.pack_start(button, False, False, 0)
        self.layout.pack_start(navigation, False, False, 0)
        self.pages = Gtk.Stack()
        self.layout.pack_start(self.pages, True, True, 0)
        self.applications_tab.connect('toggled', lambda w: self.pages.set_visible_child_name('applications') if w.get_active() else None)
        self.activity_tab.connect('toggled', lambda w: self.pages.set_visible_child_name('activity') if w.get_active() else None)
        self.build_applications()
        self.build_activity()
        self.build_status()
        self.connect('size-allocate', self.on_size)
        self.show_all()
        self.progress.hide()
        self.pages.set_visible_child_name('applications')
        self.refresh()

    def build_applications(self):
        page = column(16)
        self.pages.add_named(page, 'applications')
        title_row = Gtk.Box(spacing=12)
        title_row.pack_start(label('Applications', 'page-title'), True, True, 0)
        self.count = label('Scanning…', 'muted', 'small')
        self.count.set_ellipsize(Pango.EllipsizeMode.END)
        self.count.set_max_width_chars(30)
        title_row.pack_end(self.count, False, False, 0)
        page.pack_start(title_row, False, False, 0)
        hero = styled(Gtk.Box(spacing=16), 'hero')
        copy = column(8)
        copy.pack_start(label('A LITTLE SPACE. A FRESH START.', 'eyebrow'), False, False, 0)
        copy.pack_start(label('Keep the apps you love.', 'hero-title'), False, False, 0)
        copy.pack_start(label('Review every removal. Keep personal data until you decide.', 'muted', 'small', wrap=True), False, False, 0)
        hero.pack_start(copy, True, True, 0)
        self.hero_art = artwork()
        hero.pack_end(self.hero_art, False, False, 0)
        page.pack_start(hero, False, False, 0)
        self.search = styled(Gtk.SearchEntry(placeholder_text='Search applications, packages or formats…'), 'search')
        self.search.get_accessible().set_name('Search installed applications')
        self.search.connect('search-changed', lambda *_args: self.filter_changed())
        page.pack_start(self.search, False, False, 0)
        controls = Gtk.Box(spacing=10)
        self.format_filter = Gtk.ComboBoxText()
        for name in ('All formats', *FORMATS):
            self.format_filter.append_text(name)
        self.format_filter.set_active(0)
        self.format_filter.get_accessible().set_name('Filter application format')
        self.format_filter.connect('changed', lambda *_args: self.filter_changed())
        self.sort = Gtk.ComboBoxText()
        for option in ('Name: A–Z', 'Name: Z–A'):
            self.sort.append_text(option)
        self.sort.set_active(0)
        self.sort.get_accessible().set_name('Sort application names')
        self.sort.connect('changed', lambda *_args: self.flow.invalidate_sort())
        controls.pack_start(self.format_filter, True, True, 0)
        controls.pack_start(self.sort, False, False, 0)
        page.pack_start(controls, False, False, 0)
        self.backend_status = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
            min_children_per_line=1, max_children_per_line=5, column_spacing=6, row_spacing=6, homogeneous=True)
        page.pack_start(self.backend_status, False, False, 0)
        self.scan_notice = column(0)
        notice = styled(Gtk.Box(spacing=10), 'scan-notice')
        notice.pack_start(Gtk.Image.new_from_icon_name('dialog-warning-symbolic', Gtk.IconSize.BUTTON),
                          False, False, 0)
        notice_text = column(5)
        self.notice_title = label('', 'app-name', wrap=True)
        self.notice_hint = label('', 'muted', 'small', wrap=True)
        notice_text.pack_start(self.notice_title, False, False, 0)
        notice_text.pack_start(self.notice_hint, False, False, 0)
        notice.pack_start(notice_text, True, True, 0)
        details = Gtk.Button(label='Details', valign=Gtk.Align.CENTER)
        details.connect('clicked', lambda *_args: self.show_message(
            self.notice_title.get_text(), '\n\n'.join(self.notice_messages), Gtk.MessageType.WARNING))
        retry = Gtk.Button(label='Retry', valign=Gtk.Align.CENTER)
        retry.connect('clicked', lambda *_args: self.refresh())
        notice.pack_end(retry, False, False, 0)
        notice.pack_end(details, False, False, 0)
        self.scan_notice.pack_start(notice, False, False, 0)
        page.pack_start(self.scan_notice, False, False, 0)
        # GTK3 crossfade snapshots can draw unallocated flow children when a
        # resize and a filter update arrive together. Cards keep CSS transitions.
        self.grid_stack = Gtk.Stack()
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1,
            max_children_per_line=8, row_spacing=14, column_spacing=14, homogeneous=True, valign=Gtk.Align.START)
        self.flow.set_filter_func(self.card_visible)
        self.flow.set_sort_func(self.compare_cards)
        self.grid_stack.add_named(scroller(self.flow), 'grid')
        empty = column(12, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        empty.pack_start(artwork(250, 140), False, False, 0)
        self.empty_title = label('Finding your applications…', 'hero-title', wrap=True)
        self.empty_hint = label('Reading installed packages and desktop metadata.', 'muted', wrap=True)
        empty.pack_start(self.empty_title, False, False, 0)
        empty.pack_start(self.empty_hint, False, False, 0)
        clear = Gtk.Button(label='Clear search and filters', halign=Gtk.Align.START)
        clear.connect('clicked', lambda *_args: self.clear_filters())
        empty.pack_start(clear, False, False, 0)
        self.grid_stack.add_named(empty, 'empty')
        self.grid_stack.set_visible_child_name('empty')
        page.pack_start(self.grid_stack, True, True, 0)

    def build_activity(self):
        page = column(14)
        page.pack_start(label('Activity', 'page-title'), False, False, 0)
        page.pack_start(label('Real results, including anything that could not be verified.', 'muted', wrap=True), False, False, 0)
        self.activity_list = column(10)
        page.pack_start(scroller(self.activity_list), True, True, 0)
        self.pages.add_named(page, 'activity')

    def build_status(self):
        panel = styled(column(7), 'status-panel')
        row = Gtk.Box(spacing=10)
        self.spinner = Gtk.Spinner()
        self.status = label('Discovering installed applications…', 'muted', 'small')
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        row.pack_start(self.spinner, False, False, 0)
        row.pack_start(self.status, True, True, 0)
        panel.pack_start(row, False, False, 0)
        self.progress = Gtk.ProgressBar()
        panel.pack_start(self.progress, False, False, 0)
        self.layout.pack_end(panel, False, False, 0)

    def on_size(self, _widget, allocation):
        narrow = allocation.width < 650
        if narrow == self.narrow_layout:
            return
        self.narrow_layout = narrow
        if self.layout_source is not None:
            return
        def apply_layout():
            self.layout_source = None
            if not self.closed:
                self.hero_art.set_visible(not self.narrow_layout)
                margin = 14 if self.narrow_layout else 24
                self.layout.set_margin_start(margin)
                self.layout.set_margin_end(margin)
                self.queue_resize()
            return GLib.SOURCE_REMOVE
        # Changing visibility inside size-allocate can leave GTK3 descendants
        # with stale allocations. Apply the latest breakpoint after that pass.
        self.layout_source = GLib.idle_add(apply_layout)

    def on_key(self, _widget, event):
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            if event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
                self.applications_tab.set_active(True)
                self.search.grab_focus()
                return True
            if event.keyval in (Gdk.KEY_r, Gdk.KEY_R):
                self.refresh()
                return True
        if event.keyval == Gdk.KEY_Escape and self.search.has_focus():
            self.search.set_text('')
            return True
        return False

    def on_close(self, *_args):
        if self.operation_active:
            self.status.set_text('A removal or cleanup is running. Keep this window open for its result.')
            return True
        return False

    def on_destroy(self, *_args):
        self.closed = True
        if self.layout_source is not None:
            GLib.source_remove(self.layout_source)
            self.layout_source = None
        self.generation += 1
        self.jobs.shutdown(wait=False, cancel_futures=True)

    def submit(self, function, callback):
        future = self.jobs.submit(function)
        def completed(job):
            if job.cancelled() or self.closed:
                return
            try:
                value, error = job.result(), None
            except Exception as problem:
                LOG.exception('Background task failed')
                value, error = None, str(problem)
            def deliver():
                if not self.closed:
                    callback(value, error)
                return GLib.SOURCE_REMOVE
            if not self.closed:
                GLib.idle_add(deliver)
        future.add_done_callback(completed)

    def controls_enabled(self):
        enabled = not (self.scanning or self.operation_active or self.preparing)
        self.refresh_button.set_sensitive(enabled)
        self.flow.set_sensitive(enabled and not self.scan_error)
        self.scan_notice.set_sensitive(enabled)
        self.spinner.stop() if enabled else self.spinner.start()

    def refresh(self):
        if self.scanning or self.operation_active or self.preparing or self.closed:
            return
        self.scanning = True
        self.scan_error = None
        self.scan_notice.hide()
        self.generation += 1
        generation = self.generation
        self.controls_enabled()
        self.status.set_text('Scanning native package backends and desktop entries…')
        previous_entries = list(self.entries)
        def work():
            inventory = scan(self.registry)
            history_error = None
            try:
                entries = self.history.read()
            except Exception as problem:
                LOG.warning('Activity read failed: %s', problem)
                entries, history_error = previous_entries, str(problem)
            return inventory, entries, history_error
        def complete(value, error):
            if generation != self.generation:
                return
            if error:
                self.scanning = False
                self.scan_error = error
                self.controls_enabled()
                self.status.set_text(f'Scan failed: {error}')
                self.status.set_tooltip_text(error)
                self.update_notice()
                self.filter_changed()
                return
            self.inventory, self.entries, self.history_warning = value
            self.render_activity()
            self.render_capabilities()
            for child in self.flow.get_children():
                self.flow.remove(child)
            self.cards = []
            self.populate(0, generation)
        self.submit(work, complete)

    def populate(self, offset, generation):
        if self.closed or generation != self.generation:
            return GLib.SOURCE_REMOVE
        for app in self.inventory.apps[offset:offset + 20]:
            card = AppCard(app, self.open_details)
            self.cards.append(card)
            self.flow.add(card)
            card.get_parent().show_all()
        if offset + 20 < len(self.inventory.apps):
            GLib.idle_add(self.populate, offset + 20, generation)
        else:
            self.scanning = False
            self.controls_enabled()
            self.filter_changed()
            self.update_notice()
            warnings = self.inventory.warnings
            self.status.set_text(f'Inventory is incomplete: {len(warnings)} source warning(s). See Details above.' if warnings
                                 else 'Select an application to review removal.')
            self.status.set_tooltip_text('\n'.join(warnings) or 'All discovered sources were processed.')
        return GLib.SOURCE_REMOVE

    def render_capabilities(self):
        for child in self.backend_status.get_children():
            self.backend_status.remove(child)
        names = {'flatpak': 'Flatpak', 'snap': 'Snap', 'deb': 'APT/Deb', 'appimage': 'AppImage', 'local': 'Local'}
        for key in names:
            capability = self.inventory.capabilities.get(key)
            if capability is None:
                continue
            state = ('Scan failed' if capability.scan_failed else 'Ready' if capability.can_remove
                     else 'Read only' if capability.available else 'Unavailable')
            pill = styled(Gtk.Label(label=f'{names[key]} · {state}'), 'backend-pill')
            if not capability.can_remove:
                styled(pill, 'backend-unavailable')
            pill.set_tooltip_text(capability.detail)
            self.backend_status.add(pill)
        self.backend_status.show_all()

    def update_notice(self):
        messages = list(self.inventory.warnings)
        if self.history_warning:
            messages.append(f'Activity history: {self.history_warning}')
        if self.scan_error:
            messages.insert(0, self.scan_error)
            title = 'Refresh failed'
            hint = 'Previous results are retained. Retry before selecting a removal action.'
        elif self.inventory.warnings:
            title = 'Inventory is incomplete'
            hint = 'Some applications could not be checked. View details or retry the scan.'
        else:
            title = 'Activity history is unavailable'
            hint = 'The application inventory is available. Operation results will still be reported.'
        self.notice_messages = tuple(messages)
        self.notice_title.set_text(title)
        self.notice_hint.set_text(hint)
        self.scan_notice.set_visible(bool(messages))
        # Recompute the top-level layout after replacing flow children or
        # changing the notice height; newly inserted cards must be allocated.
        self.queue_resize()
        self.queue_draw()

    def card_visible(self, child):
        card = child.get_child()
        return (self.search.get_text().casefold() in card.search_text and
                (self.format_filter.get_active() == 0 or self.format_filter.get_active_text() == card.app.format))

    def compare_cards(self, first, second):
        a, b = first.get_child().app, second.get_child().app
        left, right = (a.name.casefold(), a.key), (b.name.casefold(), b.key)
        result = (left > right) - (left < right)
        return -result if self.sort.get_active() == 1 else result

    def filter_changed(self):
        self.flow.invalidate_filter()
        matches = sum(self.card_visible(card.get_parent()) for card in self.cards)
        total = len(self.inventory.apps)
        count = f'{matches} of {total} applications'
        if self.scan_error:
            count = f'{matches} previous results'
        elif self.inventory.warnings:
            count = f'{matches} of {total} available · partial scan'
        self.count.set_text(count)
        self.count.set_tooltip_text(count)
        self.grid_stack.set_visible_child_name('grid' if matches else 'empty')
        if not matches and not self.scanning:
            if self.scan_error or (not total and self.inventory.warnings):
                self.empty_title.set_text('Applications could not be checked')
                self.empty_hint.set_text('Application sources could not be read. View Details and retry.')
            else:
                self.empty_title.set_text('No matching applications' if total else 'No applications found')
                self.empty_hint.set_text('Try another search or clear the format filter.' if total else
                                         'No applications were found in the available sources. Try refreshing.')

    def clear_filters(self):
        self.search.set_text('')
        self.format_filter.set_active(0)
        self.filter_changed()

    def render_activity(self):
        for child in self.activity_list.get_children():
            self.activity_list.remove(child)
        if not self.entries:
            self.activity_list.pack_start(label('Nothing removed yet. Completed actions will appear here.', 'muted', wrap=True), False, False, 18)
        for entry in reversed(self.entries):
            card = styled(column(8), 'activity-card')
            status = str(entry.get('status', 'unknown'))
            card.pack_start(label(f"{entry.get('app', 'Application')} · {entry.get('action', '')}", 'app-name', wrap=True), False, False, 0)
            tone = {'success': 'success', 'failed': 'error', 'cancelled': 'muted'}.get(status, 'warning')
            card.pack_start(label(str(entry.get('summary', '')), tone, wrap=True), False, False, 0)
            card.pack_start(label(f"{entry.get('time', '')} · {status}", 'muted', 'small', wrap=True), False, False, 0)
            details = entry.get('details', [])
            if isinstance(details, list) and details:
                expander = Gtk.Expander(label='Result details')
                expander.add(label('\n'.join(map(str, details)), 'muted', 'small', wrap=True))
                card.pack_start(expander, False, False, 0)
            self.activity_list.pack_start(card, False, False, 0)
        self.activity_list.show_all()

    def show_message(self, title, message, kind=Gtk.MessageType.INFO):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                                   buttons=Gtk.ButtonsType.CLOSE, text=title)
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def open_details(self, app):
        if self.scanning or self.operation_active or self.preparing:
            return
        self.preparing = True
        self.controls_enabled()
        self.status.set_text(f'Preparing a native removal preview for {app.name}…')
        def complete(preview, error):
            self.preparing = False
            self.controls_enabled()
            self.status.set_text('Review the uninstall details before continuing.')
            if error:
                self.show_message('Unable to prepare removal', error, Gtk.MessageType.ERROR)
            else:
                self.details_dialog(preview)
        self.submit(lambda: self.registry[app.backend].prepare(app), complete)

    def details_dialog(self, preview):
        app = preview.app
        dialog = Gtk.Dialog(title='Application details', transient_for=self, modal=True)
        dialog.set_default_size(610, 570)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        action_text = 'Remove shortcut' if app.backend == 'local' else 'Move AppImage to Trash' if app.backend == 'appimage' else 'Uninstall'
        action = styled(dialog.add_button(action_text, Gtk.ResponseType.ACCEPT), 'destructive-action')
        action.set_sensitive(preview.allowed)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        body = column(16, margin=22)
        dialog.get_content_area().pack_start(scroller(body), True, True, 0)
        heading = Gtk.Box(spacing=14)
        heading.pack_start(app_icon(app, 48), False, False, 0)
        heading.pack_start(label(app.name, 'hero-title', wrap=True), True, True, 0)
        body.pack_start(heading, False, False, 0)
        meta = styled(column(6), 'details')
        for text in (app.identifier, f'{app.format} · {app.scope}',
                     ' · '.join(x for x in (app.version, app.arch, app.branch) if x)):
            if text:
                row = label(text, 'muted', 'small', wrap=True)
                row.set_selectable(True)
                meta.pack_start(row, False, False, 0)
        body.pack_start(meta, False, False, 0)
        body.pack_start(label(preview.title, 'app-name', wrap=True), False, False, 0)
        body.pack_start(label(preview.explanation, 'muted', wrap=True), False, False, 0)
        if preview.affected:
            body.pack_start(label('THIS ACTION AFFECTS', 'eyebrow'), False, False, 0)
            body.pack_start(text_area('\n'.join(preview.affected)), True, True, 0)
        skip_snapshot = None
        if app.backend == 'snap':
            skip_snapshot = Gtk.CheckButton(label='Skip creating a new data snapshot')
            skip_snapshot.set_tooltip_text('Unchecked by default. Existing snapshots remain in either mode.')
            body.pack_start(skip_snapshot, False, False, 0)
        body.pack_start(label('Close the application and save your work before removing it.', 'warning', 'small', wrap=True), False, False, 0)
        dialog.show_all()
        response = dialog.run()
        purge_snap = bool(skip_snapshot and skip_snapshot.get_active())
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT and preview.allowed:
            self.run_removal(preview, purge_snap)

    def update_progress(self, message, fraction):
        def update():
            if not self.closed and self.operation_active:
                self.status.set_text(message)
                self.status.set_tooltip_text(message)
                if fraction is None:
                    self.progress.hide()
                else:
                    self.progress.set_fraction(max(0, min(1, fraction)))
                    self.progress.show()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(update)

    def run_operation(self, app, action, function, callback):
        if self.operation_active:
            return
        self.operation_active = True
        self.controls_enabled()
        self.status.set_text(f'{action}: {app.name}…')
        previous_entries = list(self.entries)
        def work():
            try:
                with self.history.operation_lock():
                    result = function()
                    issues = []
                    try:
                        self.history.append(app.name, action, result)
                    except Exception as problem:
                        LOG.warning('Activity save failed: %s', problem)
                        issues.append(f'Activity could not be saved: {problem}')
                    try:
                        entries = self.history.read()
                    except Exception as problem:
                        LOG.warning('Activity read failed: %s', problem)
                        entries = previous_entries
                        issues.append(f'Activity could not be read: {problem}')
                    if issues:
                        result = replace(result, details=result.details + tuple(issues))
                    return result, entries, '\n'.join(issues) or None
            except OperationBusyError:
                return (OperationResult('failed', 'Another DeepPurge operation is already running.',
                    ('Wait for the other operation to finish before retrying.',)), previous_entries, None)
        def complete(value, error):
            self.operation_active = False
            self.controls_enabled()
            self.progress.hide()
            if error:
                result = OperationResult('unverified', 'Operation did not produce a verified result.', (error,))
            else:
                result, self.entries, self.history_warning = value
                self.render_activity()
                self.update_notice()
            self.status.set_text(result.summary)
            self.status.set_tooltip_text('\n'.join(result.details) or result.summary)
            callback(result)
        self.submit(work, complete)

    def run_removal(self, preview, purge_snap=False):
        backend = self.registry[preview.app.backend]
        def remove():
            if preview.app.backend == 'snap':
                return backend.uninstall(preview, self.update_progress, purge_snap)
            return backend.uninstall(preview, self.update_progress)
        self.run_operation(preview.app, preview.title, remove,
                           lambda result: self.removal_finished(preview.app, result))

    def removal_finished(self, app, result):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
            message_type=Gtk.MessageType.INFO if result.status == 'success' else Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE, text=result.summary)
        dialog.format_secondary_text('\n\n'.join(result.details) or 'See Activity for this result.')
        dialog.add_button('Close', Gtk.ResponseType.CLOSE)
        if result.removed and app.backend in {'flatpak', 'appimage'}:
            dialog.add_button('Review leftover data', Gtk.ResponseType.APPLY)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.APPLY:
            self.inspect_leftovers(app)
        else:
            self.refresh()

    def inspect_leftovers(self, app):
        self.preparing = True
        self.controls_enabled()
        self.status.set_text('Inspecting verified leftover data…')
        def inspect():
            if app.backend == 'flatpak' and any(item.identifier == app.identifier for item in self.registry['flatpak'].discover()):
                return (), ('Another Flatpak installation uses this data namespace. Data was retained.',)
            return leftover_candidates(app)
        def complete(value, error):
            self.preparing = False
            self.controls_enabled()
            if error:
                self.show_message('Unable to inspect leftovers', error, Gtk.MessageType.WARNING)
                self.refresh()
            else:
                self.cleanup_dialog(app, *value)
        self.submit(inspect, complete)

    def cleanup_dialog(self, app, candidates, notes):
        if not candidates:
            self.show_message('No verified leftovers to clean', '\n\n'.join(notes))
            self.refresh()
            return
        dialog = Gtk.Dialog(title='Optional leftover cleanup', transient_for=self, modal=True)
        dialog.set_default_size(610, 480)
        dialog.add_button('Keep all data', Gtk.ResponseType.CANCEL)
        clean = styled(dialog.add_button('Move selected data to Trash', Gtk.ResponseType.ACCEPT), 'destructive-action')
        clean.set_sensitive(False)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        body = column(16, margin=22)
        body.pack_start(label('Your data. Your choice.', 'hero-title'), False, False, 0)
        body.pack_start(label('Nothing is selected. These locations can contain personal data. Selected paths move to the desktop Trash; no permanent-delete fallback is used.', 'muted', wrap=True), False, False, 0)
        choices = []
        for item in candidates:
            check = Gtk.CheckButton()
            content = column(6)
            content.pack_start(label(str(item.path), 'app-name', wrap=True), False, False, 0)
            content.pack_start(label(item.reason, 'muted', 'small', wrap=True), False, False, 0)
            check.add(content)
            choices.append((check, item))
            check.connect('toggled', lambda *_args: clean.set_sensitive(any(c.get_active() for c, _ in choices)))
            body.pack_start(check, False, False, 0)
        body.pack_start(label('\n'.join(notes), 'muted', 'small', wrap=True), False, False, 0)
        dialog.get_content_area().pack_start(scroller(body), True, True, 0)
        dialog.show_all()
        response = dialog.run()
        selected = tuple(item for check, item in choices if check.get_active())
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT or not selected:
            self.refresh()
            return
        def cleanup():
            if app.backend == 'flatpak' and any(item.identifier == app.identifier for item in self.registry['flatpak'].discover()):
                return OperationResult('failed', 'Data retained: another Flatpak installation uses this namespace.')
            return clean_selected(selected)
        def complete(result):
            self.show_message(result.summary, '\n'.join(result.details),
                              Gtk.MessageType.INFO if result.status == 'success' else Gtk.MessageType.WARNING)
            self.refresh()
        self.run_operation(app, 'Optional data cleanup', cleanup, complete)


class DeepPurgeApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='io.github.deeppurge.Ultra')
        self.startup_error = None

    def do_activate(self):
        if window := self.get_active_window():
            window.present()
            return
        Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', True)
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(str(ASSETS / 'style.css'))
        except GLib.Error as error:
            LOG.warning('Stylesheet unavailable: %s', error)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        try:
            Dashboard(self).present()
        except OSError as error:
            LOG.error('Unable to initialize user state: %s', error)
            self.startup_error = str(error)
            dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.CLOSE,
                                       text='Unable to open the local activity directory')
            dialog.format_secondary_text(str(error))
            dialog.run()
            dialog.destroy()
            self.quit()


def main():
    if os.getuid() == 0:
        print('Run DeepPurge Ultra as your normal desktop user, without sudo.', file=sys.stderr)
        return 1
    configure_logging()
    LOG.info('Starting DeepPurge Ultra %s', __version__)
    application = DeepPurgeApplication()
    result = application.run(sys.argv)
    return 1 if application.startup_error else result
