"""Conservative, recoverable cleanup from known namespaces, never name similarity."""

import os
from pathlib import Path
import stat
from dataclasses import dataclass

from gi.repository import Gio

from .models import Application, OperationResult


def xdg_path(name: str, default: str) -> Path:
    value = os.environ.get(name, "")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / default


def fingerprint(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid,
            info.st_mtime_ns, info.st_size, info.st_ctime_ns)


def validate_path(path: Path, root: Path, identity=()) -> tuple[int, ...]:
    """Require an exact descendant of an owned real root and real owned parents."""
    if (not path.is_absolute() or not root.is_absolute() or ".." in path.parts
            or path == root or not path.is_relative_to(root)):
        raise ValueError(f"Path is outside its verified root: {path}")
    # Check all existing ancestors for links, including ancestors of XDG roots.
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"A parent is not a real directory: {parent}")
        if (parent == root or parent.is_relative_to(root)) and info.st_uid != os.getuid():
            raise ValueError(f"A cleanup parent is owned by another user: {parent}")
    current = fingerprint(path)
    if current[3] != os.getuid():
        raise ValueError(f"Path is owned by another user: {path}")
    if identity and current != identity:
        raise ValueError(f"Path changed after the preview: {path}")
    return current


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    root: Path
    reason: str
    identity: tuple[int, ...]
    parent_identity: tuple[int, ...]


def candidate(path: Path, root: Path, reason: str) -> CleanupCandidate:
    return CleanupCandidate(path, root, reason, validate_path(path, root),
                            fingerprint(path.parent)[:4])


def trash_candidate(item: CleanupCandidate) -> None:
    validate_path(item.path, item.root, item.identity)
    if fingerprint(item.path.parent)[:4] != item.parent_identity:
        raise ValueError(f"Parent changed after the preview: {item.path.parent}")
    if not Gio.File.new_for_path(str(item.path)).trash(None):
        raise OSError(f"The desktop trash service refused {item.path}")
    if os.path.lexists(item.path):
        raise OSError(f"The path still exists after the trash operation: {item.path}")


def leftover_candidates(app: Application) -> tuple[tuple[CleanupCandidate, ...], tuple[str, ...]]:
    """Only Flatpak namespaces and AppImage's documented portable directories."""
    paths = []
    notes = []
    if app.backend == "flatpak":
        paths.append((Path.home() / ".var/app" / app.identifier,
                      Path.home() / ".var/app",
                      "Flatpak's sandbox data, configuration and cache. Shared by all refs "
                      "with this app ID; cleanup is blocked while any installation remains."))
    elif app.backend == "appimage" and app.executable:
        for suffix, explanation in ((".home", "AppImage portable home and personal data"),
                                    (".config", "AppImage portable configuration")):
            paths.append((Path(str(app.executable) + suffix), Path.home(), explanation))
    else:
        notes.append("No verified user-data mapping is available. No folders were guessed from the app name.")
    found = []
    for path, root, reason in paths:
        if not os.path.lexists(path):
            continue
        try:
            item = candidate(path, root, reason)
            if stat.S_ISLNK(item.identity[2]):
                item = CleanupCandidate(item.path, item.root, item.reason +
                    " Symbolic link: only the link moves to Trash; its destination is retained.",
                    item.identity, item.parent_identity)
            found.append(item)
        except (OSError, ValueError) as error:
            notes.append(str(error))
    notes.append("Other configuration, cache, data and state directories are retained unless their ownership is verified.")
    return tuple(found), tuple(notes)


def clean_selected(items: tuple[CleanupCandidate, ...]) -> OperationResult:
    completed, errors = [], []
    for item in items:
        try:
            trash_candidate(item)
            completed.append(f"Moved to Trash: {item.path}")
        except Exception as error:
            errors.append(f"Retained {item.path}: {error}")
    return OperationResult(
        "partial" if errors and completed else "failed" if errors else "success",
        f"Moved {len(completed)} selected path(s) to Trash; {len(errors)} could not be moved.",
        tuple(completed + errors),
    )
