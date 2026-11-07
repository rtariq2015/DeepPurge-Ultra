"""Native adapters. Inventory is bounded; removal is never forcibly timed out."""

from dataclasses import replace
import http.client
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import time

import gi
from gi.repository import Gio, GLib

from .cleanup import candidate, fingerprint, trash_candidate, validate_path, xdg_path
from .models import Application, Capability, OperationResult, Preview

LOG = logging.getLogger(__name__)
COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,199}\Z")
DEB_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?\Z")
SNAP_NAME = re.compile(r"[a-z0-9][a-z0-9-]*(?:_[a-z0-9]+)?\Z")


class BackendError(RuntimeError):
    pass


def command(argv, *, allowed=(0,), timeout=25):
    process = subprocess.run(
        argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"}, check=False,
    )
    if process.returncode not in allowed:
        raise BackendError(process.stderr.strip()[:2000] or
                           f"{argv[0]} exited with status {process.returncode}")
    return process


def native_command(argv, publish):
    """Use the backend's own authentication. Do not terminate a live transaction."""
    lines = []
    with subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    ) as process:
        for line in process.stdout:
            line = line.strip()
            if line:
                lines = (lines + [line])[-100:]
                publish(line[:300], None)
        code = process.wait()
    if code:
        raise BackendError("\n".join(lines)[-4000:] or f"Native backend exited with {code}")


class Backend:
    key = ""

    def discover(self):
        return ()

    def capability(self):
        return Capability(True, True, "Available")

    def uninstall(self, preview, publish):
        """Revalidate preview, execute and independently verify the resulting state."""
        error = None
        try:
            fresh = self.prepare(preview.app)
            if (not fresh.allowed or fresh.token != preview.token or
                    fresh.affected != preview.affected or fresh.file_identities != preview.file_identities):
                raise BackendError("The uninstall preview changed. Refresh and review it again.")
            self.remove(preview, publish)
        except Exception as problem:
            LOG.warning("Native removal did not finish cleanly: %s", problem)
            error = str(problem)
        try:
            remains = self.exists(preview.app)
        except Exception as problem:
            return OperationResult("unverified", "Removal could not be verified.",
                                   tuple(x for x in (error, str(problem)) if x))
        if not remains:
            return OperationResult(
                "partial" if error else "success",
                "Application is no longer installed." if preview.app.backend != "local"
                else "The selected shortcut was removed; its application was not uninstalled.",
                tuple(x for x in (error, self.retained_message(preview.app)) if x), True,
            )
        cancelled = error and any(word in error.lower() for word in
                                  ("cancel", "dismiss", "declin", "not authorized", "not authorised"))
        return OperationResult("cancelled" if cancelled else "failed",
                               "Removal cancelled." if cancelled else "Application remains installed.",
                               (error or "The backend completed but the application is still present.",))

    def retained_message(self, app):
        return "User data was preserved. Other installations and shared components were not removed."


class FlatpakBackend(Backend):
    key = "flatpak"

    def capability(self):
        present = bool(shutil.which("flatpak"))
        return Capability(present, present, "Native Flatpak removal; user data retained" if present
                          else "flatpak is not installed")

    def discover(self):
        if not self.capability().available:
            raise BackendError("Flatpak is unavailable; installed applications cannot be verified")
        output = command(["flatpak", "list", "--app", "--columns=ref,name,installation"]).stdout
        apps = []
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 3:
                raise BackendError("Flatpak returned incomplete inventory; no absence can be verified")
            ref, name, scope = fields
            bits = ref.removeprefix("app/").split("/")
            if len(bits) != 3 or not all(COMPONENT.fullmatch(bit) for bit in bits):
                raise BackendError("Flatpak returned an invalid application reference")
            if scope not in {"user", "system"} and not COMPONENT.fullmatch(scope):
                raise BackendError("Flatpak returned an unrecognized installation name")
            identifier, arch, branch = bits
            apps.append(Application(
                f"flatpak:{scope}:{'/'.join(bits)}", self.key, name or identifier,
                identifier, "Flatpak", scope=scope, arch=arch, branch=branch, icon=identifier,
            ))
        return tuple(apps)

    @staticmethod
    def scope_args(app):
        if app.scope in {"user", "system"}:
            return [f"--{app.scope}"]
        if not COMPONENT.fullmatch(app.scope):
            raise BackendError("Invalid Flatpak installation name")
        return [f"--installation={app.scope}"]

    @staticmethod
    def ref(app):
        if not all(COMPONENT.fullmatch(value) for value in (app.identifier, app.arch, app.branch)):
            raise BackendError("Invalid Flatpak application reference")
        return f"app/{app.identifier}/{app.arch}/{app.branch}"

    def exists(self, app):
        return any(item.key == app.key for item in self.discover())

    def prepare(self, app):
        ref = self.ref(app)
        if not self.exists(app):
            raise BackendError("This exact Flatpak installation is no longer present. Refresh the list.")
        return Preview(app, "Uninstall application", (f"{app.scope} · {ref}",),
                       "Flatpak removes this exact installation and its managed integration. "
                       "Application data stays in your account. Related refs and unused runtimes "
                       "are retained. System installations may request desktop authentication.",
                       token=(app.key,))

    def remove(self, preview, publish):
        app = preview.app
        native_command(["flatpak", "uninstall", "--assumeyes", "--no-related",
                        *self.scope_args(app), "--", self.ref(app)], publish)


class SnapConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=20)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/run/snapd.socket")


class SnapBackend(Backend):
    key = "snap"

    def request(self, method, path, body=None):
        connection = SnapConnection()
        try:
            if method == "POST":
                connection.connect()
                # Give the native authorization dialog time to complete. A
                # removal transaction must not be abandoned on a UI timeout.
                connection.sock.settimeout(None)
            connection.request(method, path, None if body is None else json.dumps(body),
                               {"Content-Type": "application/json", "X-Allow-Interaction": "true"})
            response = connection.getresponse()
            data = json.loads(response.read(8_000_000))
            if response.status >= 400 or data.get("type") == "error":
                detail = data.get("result", {})
                raise BackendError(f"snapd: {detail.get('kind', 'error')}: "
                                   f"{detail.get('message', response.reason)}")
            return data
        finally:
            connection.close()

    def capability(self):
        if not Path("/run/snapd.socket").exists() and not shutil.which("snap"):
            return Capability(False, False, "Snap is not installed")
        try:
            self.request("GET", "/v2/system-info")
            return Capability(True, True, "snapd available; native PolicyKit authentication")
        except Exception as error:
            return Capability(False, False, str(error), scan_failed=True)

    def discover(self):
        if not Path("/run/snapd.socket").exists():
            return ()
        result = self.request("GET", "/v2/snaps?select=enabled")["result"]
        return tuple(Application(
            f"snap:{item['name']}", self.key, item.get("title") or item["name"],
            item["name"], "Snap", str(item.get("version", "")), "system",
            branch=str(item.get("revision", "")),
            protected=item["name"].startswith(("mesa-", "nvidia-", "gnome-")) or
                      item["name"] == "gtk-common-themes",
        ) for item in result if item.get("type", "app") == "app"
            and SNAP_NAME.fullmatch(item.get("name", "")))

    def exists(self, app):
        # A failed removal may have disabled a revision without uninstalling it.
        # Query all revisions, not just the enabled inventory shown in the UI.
        return any(item.get("name") == app.identifier for item in
                   self.request("GET", "/v2/snaps?select=all")["result"])

    def prepare(self, app):
        if not SNAP_NAME.fullmatch(app.identifier):
            raise BackendError("Invalid Snap instance name")
        if app.protected:
            return Preview(app, "Shared Snap platform", (),
                           "Shared graphics, theme and desktop platforms are outside version one's removal scope.", False)
        installed = next((item for item in self.discover() if item.identifier == app.identifier), None)
        if not installed or installed.branch != app.branch:
            raise BackendError("Snap installation changed. Refresh before removing it.")
        return Preview(app, "Uninstall application", (f"Snap: {app.identifier} · revision {app.branch}",),
                       "snapd removes the snap and its managed data/integration. Ordinary removal "
                       "normally creates a data snapshot, retained for 31 days by default. "
                       "The optional purge switch skips creating that new snapshot. Existing "
                       "snapshots remain in either mode. Desktop authentication may be requested.",
                       token=(app.identifier, app.branch))

    def remove(self, preview, publish, purge=False):
        app = preview.app
        data = self.request("POST", f"/v2/snaps/{app.identifier}",
                            {"action": "remove", "purge": purge})
        if data.get("type") != "async" or not str(data.get("change", "")).isdigit():
            raise BackendError("snapd did not return a trackable removal transaction")
        change = str(data["change"])
        while True:
            try:
                status = self.request("GET", f"/v2/changes/{change}")["result"]
            except (OSError, http.client.HTTPException) as error:
                publish(f"Waiting for snapd to reconnect · transaction {change}: {error}", None)
                time.sleep(2)
                continue
            tasks = status.get("tasks", [])
            active = next((task for task in tasks if task.get("status") == "Doing"), {})
            publish(active.get("summary") or f"snapd: {status.get('status', 'Working')}", None)
            if status.get("ready"):
                if status.get("err") or status.get("status") != "Done":
                    raise BackendError(status.get("err") or f"snapd finished with {status.get('status')}")
                break
            time.sleep(0.5)

    def uninstall(self, preview, publish, purge=False):
        # The per-operation option does not modify shared adapter state.
        wrapper = SnapBackend()
        wrapper.remove = lambda prepared, callback: self.remove(prepared, callback, purge)
        wrapper.request = self.request
        wrapper.discover = self.discover
        return Backend.uninstall(wrapper, preview, publish)

    def retained_message(self, app):
        return "Existing Snap snapshots were not removed. snapd owns its data-removal and snapshot policy."


def packagekit():
    try:
        gi.require_version("PackageKitGlib", "1.0")
        from gi.repository import PackageKitGlib
        return PackageKitGlib
    except (ImportError, ValueError) as error:
        raise BackendError("Install packagekit and gir1.2-packagekitglib-1.0 for APT removal") from error


def pk_call(method, arguments, publish=lambda *_args: None, interactive=False):
    """Run PackageKit async transactions on a worker-owned GLib context."""
    pk = packagekit()
    context = GLib.MainContext.new()
    context.push_thread_default()
    source = None
    try:
        loop = GLib.MainLoop.new(context, False)
        client = pk.Client()
        client.set_interactive(interactive)
        state = {}
        cancellable = Gio.Cancellable()

        def progress(value, progress_type, *_unused):
            if progress_type in (pk.ProgressType.STATUS, pk.ProgressType.PERCENTAGE):
                percentage = value.get_percentage()
                publish(value.get_status().value_nick.replace("-", " ").capitalize(),
                        percentage / 100 if 0 <= percentage <= 100 else None)

        def finished(client, result, *_unused):
            try:
                state["result"] = client.generic_finish(result)
            except Exception as error:
                state["error"] = error
            loop.quit()

        # Read-only queries/simulations may be cancelled on timeout. Live removal
        # is never terminated, and the UI remains attached until it finishes.
        if not interactive:
            source = GLib.timeout_source_new_seconds(45)
            source.set_callback(lambda *_args: (cancellable.cancel(), False)[1])
            source.attach(context)
        getattr(client, method)(*arguments, cancellable, progress, (), finished, None)
        loop.run()
        if "error" in state:
            raise BackendError(str(state["error"]))
        result = state["result"]
        if error := result.get_error_code():
            raise BackendError(f"PackageKit {error.get_code().value_nick}: {error.get_details()}")
        if result.get_exit_code() != pk.ExitEnum.SUCCESS:
            raise BackendError(f"PackageKit transaction: {result.get_exit_code().value_nick}")
        return result
    finally:
        if source:
            source.destroy()
        context.pop_thread_default()


class DebianBackend(Backend):
    key = "deb"

    def capability(self):
        if not shutil.which("dpkg-query"):
            return Capability(False, False, "Debian package database unavailable")
        try:
            pk = packagekit()
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
                "org.freedesktop.PackageKit", "/org/freedesktop/PackageKit",
                "org.freedesktop.PackageKit", None,
            )
            roles = proxy.get_cached_property("Roles").unpack()
            backend = proxy.get_cached_property("BackendName").unpack()
            supported = backend == "apt" and all(roles & (1 << int(role)) for role in
                                                (pk.RoleEnum.REMOVE_PACKAGES, pk.RoleEnum.RESOLVE))
            return Capability(True, bool(supported), f"PackageKit {backend}: " +
                              ("native removal; configuration files retained" if supported
                               else "required APT removal capabilities unavailable"))
        except Exception as error:
            return Capability(True, False, f"Inventory only: {error}")

    def discover(self):
        if not shutil.which("dpkg-query"):
            return ()
        output = command(["dpkg-query", "--show", "--showformat="
                          "${binary:Package}\t${Version}\t${Architecture}\t"
                          "${db:Status-Status}\t${Essential}\t${Protected}\n"]).stdout
        apps = []
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) != 6 or fields[3] != "installed" or not DEB_NAME.fullmatch(fields[0]):
                continue
            name, version, arch, _status, essential, protected = fields
            unsafe = essential == "yes" or protected == "yes" or name.startswith((
                "linux-", "nvidia-", "xserver-xorg", "ubuntu-desktop", "zorin-os-desktop"))
            apps.append(Application(f"deb:{name}:{arch}", self.key, name, name,
                                    "APT/Deb", version, "system", arch, protected=unsafe))
        return tuple(apps)

    def exists(self, app):
        if not DEB_NAME.fullmatch(app.identifier):
            raise BackendError("Invalid Debian package name")
        query = command(["dpkg-query", "--show", "--showformat=${db:Status-Status}",
                         "--", app.identifier], allowed=(0, 1))
        if query.returncode == 1:
            return False
        state = query.stdout.strip()
        if state in {"not-installed", "config-files"}:
            return False
        if state in {"installed", "half-installed", "unpacked", "half-configured",
                     "triggers-awaited", "triggers-pending"}:
            return True
        raise BackendError(f"Unable to verify Debian package state: {state!r}")

    def prepare(self, app):
        if app.protected:
            return Preview(app, "Protected system component", (),
                           "Essential, protected, desktop-core and driver packages cannot be removed here.", False)
        capability = self.capability()
        if not capability.can_remove:
            return Preview(app, "Removal unavailable", (), capability.detail, False)
        if not DEB_NAME.fullmatch(app.identifier):
            raise BackendError("Invalid Debian package name")
        pk = packagekit()
        packages = pk_call("resolve_async", (1 << int(pk.FilterEnum.INSTALLED),
                                             [app.identifier])).get_package_array()
        matches = [package.get_id() for package in packages
                   if package.get_id().split(";")[0] == app.identifier.split(":")[0]
                   and package.get_id().split(";")[1:3] == [app.version, app.arch]]
        if len(matches) != 1:
            raise BackendError("The installed package version or architecture changed. Refresh first.")
        result = pk_call("remove_packages_async", (
            1 << int(pk.TransactionFlagEnum.SIMULATE), matches, True, False,
        ))
        effects = tuple(sorted(package.get_id() for package in result.get_package_array()
                               if package.get_info() in (pk.InfoEnum.REMOVING, pk.InfoEnum.OBSOLETING)))
        allowed = len(effects) == 1 and effects[0] == matches[0]
        explanation = ("PackageKit will request PolicyKit authorization and perform ordinary package "
                       "removal. Debian configuration files and personal data remain. Automatic "
                       "dependency cleanup is disabled.")
        if not allowed:
            explanation += " This transaction affects dependencies or could not be fully previewed; use your system package manager."
        return Preview(app, "Remove Debian package", effects, explanation, allowed, tuple(matches))

    def remove(self, preview, publish):
        pk_call("remove_packages_async", (0, list(preview.token), False, False), publish, True)

    def retained_message(self, app):
        return "Debian configuration files and personal data remain. This was removal, not Debian purge."


def debian_owned(path: Path):
    if not shutil.which("dpkg-query"):
        return False
    output = command(["dpkg-query", "--search", "--", str(path)], allowed=(0, 1)).stdout
    return any(line.rpartition(": ")[2] == str(path) for line in output.splitlines())


def appimage_verified(path: Path):
    identity = validate_path(path, Path.home())
    if not stat.S_ISREG(identity[2]) or not os.access(path, os.X_OK):
        raise BackendError("AppImage must be a regular user-owned executable, without symlink parents")
    with path.open("rb") as stream:
        header = stream.read(11)
    if header[:4] != b"\x7fELF" or header[8:11] not in (b"AI\x01", b"AI\x02"):
        raise BackendError("Executable does not contain a recognized AppImage header")
    if debian_owned(path):
        raise BackendError("The executable belongs to a Debian package; use its package backend")
    return identity


class LocalBackend(Backend):
    key = "local"

    def capability(self):
        return Capability(True, True, "Verified user shortcuts only; arbitrary uninstall scripts disabled")

    def exists(self, app):
        return any(os.path.lexists(path) for path in app.launchers)

    def prepare(self, app):
        root = xdg_path("XDG_DATA_HOME", ".local/share") / "applications"
        try:
            if len(app.launchers) != 1 or not app.identity:
                raise BackendError("No supported, verified removal action is available")
            path = app.launchers[0]
            validate_path(path, root, app.identity)
            if debian_owned(path):
                raise BackendError("This launcher is package-managed")
            return Preview(app, "Remove shortcut", (str(path),),
                           "Only this verified user shortcut will move to Trash. The application, "
                           "its executable and all data remain installed.", token=tuple(map(str, app.identity)),
                           file_identities=(app.identity,))
        except (OSError, ValueError, BackendError) as error:
            return Preview(app, "Unsupported local entry", (), str(error), False)

    def remove(self, preview, publish):
        path = preview.app.launchers[0]
        root = xdg_path("XDG_DATA_HOME", ".local/share") / "applications"
        publish("Moving the selected shortcut to Trash…", None)
        item = candidate(path, root, "Verified user shortcut")
        trash_candidate(replace(item, identity=preview.file_identities[0]))


class AppImageBackend(Backend):
    key = "appimage"

    def capability(self):
        return Capability(True, True, "Verified AppImages in your home; recoverable Trash removal")

    def exists(self, app):
        return bool(app.executable and os.path.lexists(app.executable))

    def prepare(self, app):
        try:
            if app.executable is None:
                raise BackendError("No exact AppImage executable is associated with this launcher")
            identity = appimage_verified(app.executable)
            if identity != app.identity:
                raise BackendError("AppImage changed after discovery; refresh first")
            targets = [str(app.executable)]
            identities = [identity]
            root = xdg_path("XDG_DATA_HOME", ".local/share") / "applications"
            for launcher in app.launchers:
                try:
                    validate_path(launcher, root)
                    if debian_owned(launcher):
                        continue
                    targets.append(str(launcher))
                    identities.append(fingerprint(launcher))
                except (OSError, ValueError):
                    continue
            return Preview(app, "Remove AppImage", tuple(targets),
                           "Move this exact AppImage and the listed user launchers to Trash. "
                           "Portable data stays in place and can be reviewed separately. "
                           "System and package-managed integration is retained.",
                           token=tuple(map(str, identity)), file_identities=tuple(identities))
        except (OSError, ValueError, BackendError) as error:
            return Preview(app, "AppImage removal unavailable", (), str(error), False)

    def remove(self, preview, publish):
        for index, value in enumerate(preview.affected):
            path = Path(value)
            root = Path.home() if path == preview.app.executable else (
                xdg_path("XDG_DATA_HOME", ".local/share") / "applications")
            publish(f"Moving to Trash: {path.name}", None)
            item = candidate(path, root, "Verified AppImage integration")
            trash_candidate(replace(item, identity=preview.file_identities[index]))


def backend_registry():
    return {backend.key: backend for backend in
            (FlatpakBackend(), SnapBackend(), DebianBackend(), AppImageBackend(), LocalBackend())}
