"""XDG desktop precedence and package-backed application reconciliation."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import logging
import os
from pathlib import Path
import shlex

from gi.repository import Gio, GLib

from .backends import appimage_verified, command
from .cleanup import validate_path, xdg_path
from .models import Application, Capability, Inventory

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DesktopEntry:
    path: Path
    desktop_id: str
    name: str
    icon: str
    executable: Path | None
    flatpak_id: str = ""
    snap_id: str = ""


def application_directories():
    roots = [xdg_path("XDG_DATA_HOME", ".local/share")]
    roots.extend(Path(value) for value in os.environ.get(
        "XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
                 if value and Path(value).is_absolute())
    # Export fallbacks cover desktops that have not yet updated XDG_DATA_DIRS.
    roots.extend((xdg_path("XDG_DATA_HOME", ".local/share") / "flatpak/exports/share",
                  Path("/var/lib/flatpak/exports/share"), Path("/var/lib/snapd/desktop")))
    return tuple(dict.fromkeys(root / "applications" for root in roots))


def desktop_files(directory):
    """Traverse application directories only, with depth/count bounds, no links."""
    count = 0
    for root, directories, files in os.walk(directory, followlinks=False):
        if len(Path(root).relative_to(directory).parts) >= 6:
            directories[:] = []
        else:
            directories.sort()
        for name in sorted(files):
            if name.endswith(".desktop"):
                yield Path(root) / name
                count += 1
                if count >= 10_000:
                    LOG.warning("Desktop scan limit reached for %s", directory)
                    return


def scan_desktops(directories=None):
    seen, entries = set(), []
    for directory in directories if directories is not None else application_directories():
        for path in desktop_files(directory):
            desktop_id = str(path.relative_to(directory)).replace("/", "-")
            if desktop_id in seen:
                continue
            keyfile = GLib.KeyFile()
            try:
                keyfile.load_from_file(str(path), GLib.KeyFileFlags.NONE)
                seen.add(desktop_id)
                def value(key, default=""):
                    try:
                        return keyfile.get_string("Desktop Entry", key)
                    except GLib.Error:
                        return default
                if value("Type") != "Application" or any(
                    value(key).lower() == "true" for key in ("Hidden", "NoDisplay")
                ):
                    continue
                try:
                    metadata = Gio.DesktopAppInfo.new_from_filename(str(path))
                except TypeError:
                    metadata = None  # Stale Exec/TryExec can make Gio return NULL.
                name = metadata.get_display_name() if metadata else value("Name", path.stem)
                icon = metadata.get_icon().to_string() if metadata and metadata.get_icon() else value(
                    "Icon", "application-x-executable")
                executable = None
                try:
                    tokens = shlex.split(value("Exec"))
                    if tokens and Path(tokens[0]).is_absolute() and "%" not in tokens[0]:
                        executable = Path(tokens[0])
                except ValueError:
                    pass
                entries.append(DesktopEntry(path, desktop_id, name, icon, executable,
                                            value("X-Flatpak"), value("X-SnapInstanceName")))
            except (OSError, GLib.Error, UnicodeError) as error:
                LOG.warning("Skipping desktop file %s: %s", path, error)
    return tuple(entries)


def package_owners(entries):
    """Accept only exact returned paths with a single declared Debian owner."""
    import shutil
    if not shutil.which("dpkg-query"):
        return {}
    paths = [str(entry.path) for entry in entries]
    owners = {}
    for offset in range(0, len(paths), 128):
        requested = set(paths[offset:offset + 128])
        result = command(["dpkg-query", "--search", "--", *sorted(requested)], allowed=(0, 1))
        for line in result.stdout.splitlines():
            owner, separator, path = line.rpartition(": ")
            if separator and path in requested and ", " not in owner:
                owners[Path(path)] = owner
    return owners


def reconcile(backend_apps, entries, owners):
    result = {}
    by_flatpak, by_snap, by_deb = {}, {}, {}
    for app in backend_apps:
        if app.backend == "flatpak":
            by_flatpak.setdefault(app.identifier, []).append(app)
            result[app.key] = app
        elif app.backend == "snap":
            by_snap[app.identifier] = app
            result[app.key] = app
        elif app.backend == "deb":
            by_deb[app.identifier] = app
            by_deb.setdefault(app.identifier.split(":")[0], app)
    for entry in entries:
        matched = []
        if entry.flatpak_id:
            matched = by_flatpak.get(entry.flatpak_id, [])
        elif entry.snap_id:
            if app := by_snap.get(entry.snap_id):
                matched = [app]
        elif str(entry.path).startswith("/var/lib/snapd/desktop/applications/"):
            if app := by_snap.get(entry.path.stem.split("_", 1)[0]):
                matched = [app]
        elif owner := owners.get(entry.path):
            if app := by_deb.get(owner):
                matched = [app]
        if matched:
            for app in matched:
                current = result.get(app.key, app)
                launchers = tuple(dict.fromkeys(current.launchers + (entry.path,)))
                result[app.key] = replace(current,
                    name=current.name if current.launchers else entry.name,
                    icon=current.icon if current.launchers else entry.icon,
                    launchers=launchers)
            continue
        # Exported entries are not evidence of a separate local installation.
        if entry.flatpak_id or entry.snap_id or entry.path in owners:
            continue
        executable = entry.executable
        is_image = executable and executable.suffix.lower() == ".appimage"
        if is_image:
            key = f"appimage:{executable}"
            identity = ()
            try:
                identity = appimage_verified(executable)
            except (OSError, ValueError, RuntimeError):
                pass
            app = Application(key, "appimage", entry.name, executable.name, "AppImage",
                              scope="user file", icon=entry.icon, launchers=(entry.path,),
                              executable=executable, identity=identity)
            if old := result.get(key):
                app = replace(old, launchers=tuple(dict.fromkeys(old.launchers + app.launchers)))
        else:
            identity = ()
            try:
                root = xdg_path("XDG_DATA_HOME", ".local/share") / "applications"
                identity = validate_path(entry.path, root)
            except (OSError, ValueError):
                pass
            key = f"local:{entry.path}"
            app = Application(key, "local", entry.name, entry.desktop_id.removesuffix(".desktop"),
                              "Custom/Local", scope="user shortcut" if identity else "unverified source",
                              icon=entry.icon, launchers=(entry.path,), identity=identity)
        result[key] = app
    return tuple(sorted(result.values(), key=lambda app: (app.name.casefold(), app.key)))


def scan(registry):
    """Small bounded pool; all widget creation belongs to the caller's main loop."""
    apps, entries, warnings, capabilities = [], (), [], {}
    def backend_job(backend):
        capability = backend.capability()
        return capability, backend.discover() if capability.available else ()
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="inventory") as executor:
        jobs = {executor.submit(backend_job, backend): key for key, backend in registry.items()
                if key in {"flatpak", "snap", "deb"}}
        jobs[executor.submit(scan_desktops)] = "desktop"
        for future in as_completed(jobs):
            key = jobs[future]
            try:
                if key == "desktop":
                    entries = future.result()
                else:
                    capability, records = future.result()
                    capabilities[key] = capability
                    apps.extend(records)
            except Exception as error:
                LOG.exception("%s discovery failed", key)
                warnings.append(f"{key}: {error}")
                if key != "desktop":
                    capabilities[key] = Capability(False, False, str(error))
    for key in ("appimage", "local"):
        capabilities[key] = registry[key].capability()
    try:
        owners = package_owners(entries)
    except Exception as error:
        warnings.append(f"Package ownership unavailable: {error}")
        # Fail closed: unknown system entries cannot acquire removal capability.
        owners = {}
    return Inventory(reconcile(apps, entries, owners), capabilities, tuple(warnings))
