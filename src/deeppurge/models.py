"""Backend-neutral, immutable records. No GTK dependency."""

from dataclasses import dataclass
from pathlib import Path


FORMATS = {
    "Flatpak": ("flatpak", "#3a60c1"),
    "Snap": ("snap", "#ff5c2b"),
    "AppImage": ("appimage", "#5c3091"),
    "APT/Deb": ("deb", "#0d8253"),
    "Custom/Local": ("local", "#c42944"),
}


@dataclass(frozen=True)
class Application:
    key: str
    backend: str
    name: str
    identifier: str
    format: str
    version: str = ""
    scope: str = ""
    arch: str = ""
    branch: str = ""
    icon: str = "application-x-executable"
    launchers: tuple[Path, ...] = ()
    executable: Path | None = None
    identity: tuple[int, ...] = ()
    protected: bool = False


@dataclass(frozen=True)
class Capability:
    available: bool
    can_remove: bool
    detail: str
    scan_failed: bool = False


@dataclass(frozen=True)
class Preview:
    app: Application
    title: str
    affected: tuple[str, ...]
    explanation: str
    allowed: bool = True
    token: tuple[str, ...] = ()
    file_identities: tuple[tuple[int, ...], ...] = ()


@dataclass(frozen=True)
class OperationResult:
    status: str
    summary: str
    details: tuple[str, ...] = ()
    removed: bool = False


@dataclass(frozen=True)
class Inventory:
    apps: tuple[Application, ...]
    capabilities: dict[str, Capability]
    warnings: tuple[str, ...] = ()
