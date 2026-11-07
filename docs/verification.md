# Verification evidence

Verified on November 7, 2026 in `/home/home/DeepPurge-Ultra` on Zorin OS 18.1
(Ubuntu noble base), Python 3.12.3, GTK 3.24, Flatpak 1.14.6, snapd 2.77.1
and PackageKit 1.2.8 with its APT backend.
Current reviewed version: **0.2.1**. The findings, reproductions and selected
fix batch are in [the engineering review](review-2026-11-07.md).

## Completed checks

- **53 tests passed**, including all opt-in native and GTK checks, with
  `G_DEBUG=fatal-criticals` enabled for the final run.
- Python compilation and `desktop-file-validate` completed successfully.
- Live inventory returned 100 application records across APT/Deb, Flatpak, Snap,
  AppImage and local entries, with no discovery warnings at verification time.
- PackageKit advertised APT removal and resolve roles. Its asynchronous native
  removal simulation for the installed `baobab` package completed successfully;
  that package remained installed. PolicyKit authorization capability was queried
  without initiating a privileged removal.
- A disposable Flatpak app was exported, installed into an isolated temporary
  HOME/XDG installation, previewed, uninstalled by its exact user reference, and
  re-queried. Its data survived uninstall and moved to Trash only in the separate
  explicitly selected cleanup step. No host installation was targeted.
- Temporary AppImage and user-shortcut fixtures moved through the real desktop
  Trash implementation in isolated HOME/XDG directories. AppImage portable data
  survived executable removal and was separately selected for cleanup.
- Unit checks cover distinct Flatpak scopes/architectures/branches, native-command
  arguments, missing tools, desktop precedence, stale Gio metadata, package
  ownership, unsupported local entries, changed previews, cancellation/failure
  reporting, disabled Snap revisions, partially installed Debian packages,
  filesystem boundaries, foreign ownership, symlink parents and leaf links,
  changed file identities, Trash refusal and operation-lock behavior.
- Review regressions cover unavailable/malformed Flatpak inventory blocking
  absence verification and shared-data selection, corrupt UTF-8 Activity,
  atomic history writes without truncating an unrelated hardlink, rejection
  of FIFO/shared state files, empty XDG search paths, unreadable desktop roots,
  failed capability probes, preservation of backend results when history fails,
  and correct distinction between operation-lock contention and backend I/O.
- GTK checks cover stylesheet loading, missing icons, long names, search, format
  filtering, reverse sorting, empty results, keyboard focus shortcuts, refresh
  generation handling, busy-window closure, cancelled uninstall previews,
  unchecked personal-data cleanup and growth under larger accessibility fonts.
- Additional GTK checks cover failed/partial scan states, retry after failed
  refresh, and actual card/notice allocations after resizing and replacing data.
- Seven real GTK screenshots were captured and visually inspected, covering wide
  and narrow grids, increased text size, uninstall details, empty search and
  Activity, plus an incomplete-scan notice. Public captures use documented fixture inventory to protect the user's
  application list. The final capture pass also completed with
  `G_DEBUG=fatal-criticals`, without GTK critical errors.
- The actual application was separately launched against live package backends.
  Its 100 identity keys were unique, with no discovery warnings. A real Flatpak
  details view was prepared and cancelled before any uninstall. Live captures
  are kept under ignored `artifacts/local/` rather than published to GitHub.

## Commands

```bash
G_DEBUG=fatal-criticals DEEPPURGE_NATIVE_TESTS=1 DEEPPURGE_UI_TESTS=1 GDK_BACKEND=x11 xvfb-run -a /usr/bin/python3 tests/run.py
/usr/bin/python3 -m compileall -q src tests
desktop-file-validate assets/deep-purge-ultra.desktop
G_DEBUG=fatal-criticals GDK_BACKEND=x11 xvfb-run -a -s '-screen 0 1600x1200x24' /usr/bin/python3 tests/capture_ui.py
```

A separate `dbus-run-session` test run also passed. Its desktop portal services
reported host Snap/FUSE environment problems; the application tests and native
fixture operations still completed. The final run used the existing session bus
and produced no GTK criticals.

## Explicit integration limits

No real host Debian or Snap package was removed. Interactive authentication
acceptance/cancellation for real privileged APT, system Flatpak and Snap removals
has not been integration-tested. A sacrificial VM or separately authorized target
is still needed for those paths. Snap status/error behavior and cancellation
classification are covered by adapter tests; this is not a substitute for a full
snapd removal test. Named Flatpak system installations and external AppImage
integration have not been exercised on this machine.
Cross-filesystem Trash behavior on removable/network storage also remains
unverified; unsupported Trash operations continue to retain paths and report errors.

User-data associations are deliberately narrow. Unknown application data,
Debian configuration files, existing Snap snapshots, shared vendor directories
and unrelated files remain. The filesystem checks protect normal desktop use;
they do not claim an atomic security boundary against a hostile same-user process
concurrently rewriting the filesystem during a Trash operation.

These checks establish the working features described in the README. They do
not certify every backend, authentication agent or supported desktop as production
ready. No personal application or its data was uninstalled during verification.
