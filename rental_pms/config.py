"""Configuration loading.

Locally, values come from a ``.env`` file (git-ignored). In GitHub Actions they
come from repository secrets injected as environment variables. Same names in
both places, so nothing in the code needs to know which it is.
"""

import os
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Config:
    gmail_address: str
    gmail_app_password: str
    notion_api_token: str
    notion_database_id: str
    ical_url: str
    # How to find the notification emails: "subject" (default) or "labels".
    # See the imap_client module docstring for why subject is the default.
    selection: str = "subject"
    # Gmail labels to read when selection is "labels", one IMAP folder each.
    # These are applied by the mailbox's own Gmail filters.
    imap_labels: Tuple[str, ...] = (
        "Nieuwe boeking",
        "Gewijzigde boeking",
        "Geannuleerde boeking",
    )
    # Folder used only by the label audit. Left unset, All Mail is discovered
    # via its IMAP special-use flag, which survives Gmail's localised names.
    imap_folder: Optional[str] = None
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    # Only look at mail newer than this many days, to keep runs cheap.
    lookback_days: int = 30
    dry_run: bool = False

    def redacted(self) -> dict:
        """Config summary safe to print in CI logs."""
        return {
            "gmail_address": _mask_email(self.gmail_address),
            "gmail_app_password": "***" if self.gmail_app_password else "(unset)",
            "notion_api_token": "***" if self.notion_api_token else "(unset)",
            "notion_database_id": _mask_tail(self.notion_database_id),
            "ical_url": _mask_tail(self.ical_url),
            "selection": self.selection,
            "imap_labels": ", ".join(self.imap_labels),
            "imap_folder": self.imap_folder or "(auto-discover All Mail)",
            "lookback_days": self.lookback_days,
            "dry_run": self.dry_run,
        }


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def _mask_email(value: str) -> str:
    if "@" not in value:
        return "***"
    local, _, domain = value.partition("@")
    return "{}***@{}".format(local[:2], domain)


def _mask_tail(value: str) -> str:
    return "***{}".format(value[-4:]) if len(value) > 4 else "***"


def _load_dotenv(path: str = ".env") -> None:
    """Populate os.environ from a .env file without overriding real env vars.

    Deliberately hand-rolled so the package has no import-time dependency on
    python-dotenv; the file format we need is just KEY=VALUE.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            # Real environment (i.e. GitHub Actions secrets) always wins.
            os.environ.setdefault(key, value)


DEFAULT_LABELS = ("Nieuwe boeking", "Gewijzigde boeking", "Geannuleerde boeking")


def _split_labels(value: Optional[str]) -> Tuple[str, ...]:
    """Parse a comma-separated IMAP_LABELS override, or fall back to defaults."""
    if not value:
        return DEFAULT_LABELS
    labels = tuple(part.strip() for part in value.split(",") if part.strip())
    return labels or DEFAULT_LABELS


SELECTION_MODES = ("subject", "labels")


def _selection(value: Optional[str]) -> str:
    """Validate IMAP_SELECTION, defaulting to subject matching."""
    if not value:
        return "subject"
    normalized = value.strip().lower()
    if normalized not in SELECTION_MODES:
        raise ConfigError(
            "IMAP_SELECTION must be one of {}, got {!r}".format(SELECTION_MODES, value)
        )
    return normalized


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            "missing required setting {}. Set it in .env locally, or as a "
            "GitHub Actions repository secret.".format(name)
        )
    return value


def load_config(dotenv_path: Optional[str] = ".env") -> Config:
    """Read configuration from .env plus the environment."""
    if dotenv_path:
        _load_dotenv(dotenv_path)

    return Config(
        gmail_address=_required("GMAIL_ADDRESS"),
        gmail_app_password=_required("GMAIL_APP_PASSWORD"),
        notion_api_token=_required("NOTION_API_TOKEN"),
        notion_database_id=_required("NOTION_DATABASE_ID"),
        ical_url=_required("ICAL_URL"),
        selection=_selection(os.environ.get("IMAP_SELECTION")),
        imap_labels=_split_labels(os.environ.get("IMAP_LABELS")),
        imap_folder=os.environ.get("IMAP_FOLDER") or None,
        imap_host=os.environ.get("IMAP_HOST", "imap.gmail.com"),
        imap_port=int(os.environ.get("IMAP_PORT", "993")),
        lookback_days=int(os.environ.get("LOOKBACK_DAYS", "30")),
        dry_run=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
    )
