"""Single source of truth for the application version."""

from pathlib import Path

VERSION_FILE = Path(__file__).parent.parent / "VERSION"


def get_version() -> str:
    """Read version from the VERSION file, falling back to 'unknown'."""
    try:
        return VERSION_FILE.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return "unknown"
