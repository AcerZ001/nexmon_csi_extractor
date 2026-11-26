"""Resolve a user's run name without changing daily capture defaults."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryFile


def run_database_path(
    name: str, daily_directory: Path, *, started_at: datetime | None = None
) -> Path:
    """Return <name>-YYYY-MM-DD-HH_MM_SS.db using the Pi's local start time."""
    if not name.strip() or any(ord(char) < 32 for char in name):
        raise ValueError("Database name cannot be empty or contain control characters")
    if name.endswith(("/", "\\")):
        raise ValueError("Supply a database name, not a directory ending with a slash")
    supplied = Path(name).expanduser()
    if supplied.name in ("", ".", ".."):
        raise ValueError("Supply a database name or an absolute path ending in a name")
    if not supplied.is_absolute():
        if supplied.name != name:
            raise ValueError(
                "Use a bare database name or an absolute path, not a relative path"
            )
        directory = Path(daily_directory).expanduser()
        # Keep the old daily subfolder for unnamed captures, but put named runs
        # directly in the data folder for existing CSI_DAILY_DIR deployments.
        if directory.name == "csi_daily":
            directory = directory.parent
        supplied = directory / supplied
    stem = (
        supplied.name[:-3] if supplied.name.lower().endswith(".db") else supplied.name
    )
    if not stem.strip() or stem in (".", ".."):
        raise ValueError("Database name must have a nonempty prefix")
    stamp = (started_at or datetime.now()).strftime("%Y-%m-%d-%H_%M_%S")
    # Resolve only the parent; never follow an existing destination-file symlink.
    return supplied.parent.resolve() / f"{stem}-{stamp}.db"


def preflight_database_path(path: Path) -> None:
    """Check destination before the launcher stops an existing capture."""
    if path.exists() or path.is_symlink():
        raise FileExistsError(
            f"Database already exists: {path}; use another name or retry next second"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exercise permissions without leaving a test file or opening a database.
    with TemporaryFile(dir=path.parent):
        pass
