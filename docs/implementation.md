# Implementation decisions and plan

Verified workspace: `/home/home/DeepPurge-Ultra`. No applicable `AGENTS.md` was
present. Target: Zorin OS 18.1, Ubuntu noble base; GTK 3.24, Flatpak 1.14.6,
snapd 2.77.1 and PackageKit 1.2.8 using the `apt` backend are installed.

## Implementation sequence

1. Preserve the native dark card design and split inventory, backend, cleanup,
   history and GTK responsibilities into small modules.
2. Build the dashboard with real inventory, search, filters, sort and activity.
3. Remove exact Flatpak installations/references, preserving data by default.
4. Use asynchronous PackageKit removal with simulation and native PolicyKit;
   use snapd's authenticated REST transactions and actual change status.
5. Verify AppImage executables without running them. Trash verified local files
   and optionally offer metadata-backed leftovers after removal.
6. Test adapters, errors, filesystem boundaries, UI states and responsive layouts.

## Research and rationale

- [Gtk.FlowBox](https://docs.gtk.org/gtk3/class.FlowBox.html),
  [GTK3 CSS](https://docs.gtk.org/gtk3/css-overview.html), and
  [accessibility](https://docs.gtk.org/gtk3/class.Accessible.html): use native
  layout, supported transitions, focus outlines and named accessible controls.
  [PyGObject threading](https://pygobject.gnome.org/guide/threading.html): run
  blocking inventory/operations off-thread, update widgets through idle callbacks.
- [Flatpak command reference](https://docs.flatpak.org/en/latest/flatpak-command-reference.html):
  identity includes installation, app ID, architecture and branch. Native removal
  preserves app data; do not pass `--delete-data`. Avoid removing related refs.
- [Snap REST API](https://snapcraft.io/docs/how-to-guides/snap-development/use-the-rest-api/):
  communicate over snapd's Unix socket with `X-Allow-Interaction` for native
  authorization, then watch the returned asynchronous change.
  [Snapshot behavior](https://snapcraft.io/docs/explanation/security/decommissioning/):
  ordinary removal normally creates an automatic snapshot with a default 31-day
  retention. Purge suppresses the new snapshot; existing snapshots remain.
- [Debian ownership queries](https://manpages.debian.org/bookworm/dpkg/dpkg-query.1.en.html):
  match returned absolute paths and installed package records, not app names.
- [PackageKit async removal](https://www.freedesktop.org/software/PackageKit/doc/method.Client.remove_packages_async.html)
  and [client declarations](https://github.com/PackageKit/PackageKit/blob/main/lib/packagekit-glib2/pk-client.h):
  use the installed GI API, actual daemon roles and simulation. The target's
  APT daemon advertises removal and completed a read-only removal simulation.
  Dependency-changing removals are refused in version one; autoremove is disabled.
  Ordinary removal is not advertised as Debian purge. Interactive authorization
  and real package removal still require a sacrificial target to integration-test.
- [Desktop entry specification](https://specifications.freedesktop.org/desktop-entry/latest-single/)
  and [XDG base directories](https://specifications.freedesktop.org/basedir/latest/):
  honor directory precedence, hidden overrides and absolute XDG paths. Use Gio
  metadata without executing launch commands. Unknown entries remain unverified.
- [AppImage portable mode](https://docs.appimage.org/user-guide/portable-mode.html):
  verify the ELF/AppImage magic of the exact direct executable; only adjacent
  `.home` and `.config` folders are automatic leftover candidates. Other names
  require an explicit mapping and are retained.

User-data cleanup is a separate, unchecked-by-default, recoverable Trash action.
No broad matching, shell deletion, privileged project-code helper or forced
package-transaction cancellation is used. Package managers own packaged files.
