"""Read Booking.com notification emails from Gmail over IMAP.

Two selection strategies, chosen with ``selection``:

``subject`` (default)
    Search All Mail for subjects beginning "Booking.com -". Depends only on what
    Booking.com puts in the subject line, which is verified across the mailbox
    in docs/subject-line-analysis.md. Covers history automatically.

``labels``
    Read the Gmail labels the mailbox's filters apply, one IMAP folder each.
    Useful when you want to curate scope by hand -- unlabel a test booking and
    it disappears from the run.

Subject is the default because a Gmail filter is mutable configuration that
lives outside this repo, cannot be tested in CI, and fails silently: a filter
that stops matching produces a smaller run, not an error. Filters also never
apply retroactively, so mail that predates a filter change is invisible to
``labels`` until someone labels it by hand. ``audit_unlabelled()`` measures that
gap; over 120 days it was 11 notifications, 8 of them cancellations.
"""
import email
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from typing import Iterator, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

SELECTION_SUBJECT = "subject"
SELECTION_LABELS = "labels"
SELECTION_MODES = (SELECTION_SUBJECT, SELECTION_LABELS)

# Every notification subject begins with this, whatever the event type:
#   "Booking.com - Nieuwe boeking! (...)"
#   "Booking.com - Nieuwe last-minutereservering (...)"
SUBJECT_PREFIX = "Booking.com -"

# The Gmail labels holding the notifications, one IMAP folder each.
DEFAULT_LABELS = ("Nieuwe boeking", "Gewijzigde boeking", "Geannuleerde boeking")

# Sender of the notification emails, kept for reference. Not used for
# selection -- the subject is the criterion.
NOTIFICATION_SENDER = "noreply@booking.com"


class ImapClient:
    """Minimal Gmail IMAP reader, usable as a context manager."""

    def __init__(self, host: str, port: int, address: str, app_password: str,
                 folder: Optional[str] = None,
                 labels: Sequence[str] = DEFAULT_LABELS,
                 selection: str = SELECTION_SUBJECT) -> None:
        if selection not in SELECTION_MODES:
            raise ValueError(
                "selection must be one of {}, got {!r}".format(SELECTION_MODES, selection)
            )
        self._host = host
        self._port = port
        self._address = address
        self._app_password = app_password
        self._folder = folder
        self._labels = tuple(labels)
        self._selection = selection
        self._conn = None  # type: Optional[imaplib.IMAP4_SSL]

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        self._conn = imaplib.IMAP4_SSL(self._host, self._port)
        self._conn.login(self._address, self._app_password)

        logger.info("connected to %s as %s", self._host, self._address)

    def select_folder(self, folder: str) -> None:
        """Open one folder read-only.

        Read-only matters: this script must never mark mail as read or move it.
        imaplib does not quote mailbox names and Gmail's contain spaces and
        brackets, so the name is quoted here or the server replies
        BAD "Could not parse command".
        """
        if self._conn is None:
            raise RuntimeError("not connected")
        status, _ = self._conn.select(_quote_mailbox(folder), readonly=True)
        if status != "OK":
            raise RuntimeError(
                "could not select IMAP folder {!r}. Available folders: {}".format(
                    folder, ", ".join(self.list_folders())
                )
            )

    def list_folders(self) -> List[str]:
        """Return the names of every folder on the account."""
        if self._conn is None:
            raise RuntimeError("not connected")
        status, rows = self._conn.list()
        if status != "OK" or not rows:
            return []
        return [name for name, _ in (_parse_list_row(row) for row in rows) if name]

    def _discover_all_mail(self) -> str:
        r"""Locate the All Mail folder by its \All special-use flag.

        Falls back to INBOX, which loses archived mail but is better than
        failing outright.
        """
        if self._conn is None:
            raise RuntimeError("not connected")

        status, rows = self._conn.list()
        if status == "OK" and rows:
            for row in rows:
                name, flags = _parse_list_row(row)
                if name and "\\All" in flags:
                    logger.info("discovered All Mail folder: %s", name)
                    return name

        logger.warning("could not find an All Mail folder; falling back to INBOX")
        return "INBOX"

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
            self._conn.logout()
        except (imaplib.IMAP4.error, OSError):
            # Nothing useful to do if teardown fails on a read-only session.
            pass
        finally:
            self._conn = None

    def _search_since(self, lookback_days: int) -> List[bytes]:
        """Message ids in the currently selected folder within the date window."""
        if self._conn is None:
            raise RuntimeError("not connected")
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        status, data = self._conn.search(None, "SINCE", since)
        if status != "OK":
            raise RuntimeError("IMAP search failed: {}".format(status))
        return data[0].split() if data and data[0] else []

    def fetch(self, message_id: bytes) -> Tuple[str, str, Optional[str]]:
        """Return (subject, plain-text body, Date header) for one message."""
        if self._conn is None:
            raise RuntimeError("not connected")

        status, data = self._conn.fetch(message_id, "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError("could not fetch message {!r}".format(message_id))

        message = email.message_from_bytes(data[0][1])
        return _decode_subject(message), _plain_text_body(message), message.get("Date")

    def _fetch_with_id(self, message_id: bytes):
        """Like fetch(), plus the RFC 822 Message-ID used for de-duplication."""
        if self._conn is None:
            raise RuntimeError("not connected")
        status, data = self._conn.fetch(message_id, "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError("could not fetch message {!r}".format(message_id))
        message = email.message_from_bytes(data[0][1])
        return (
            _decode_subject(message),
            _plain_text_body(message),
            message.get("Date"),
            message.get("Message-ID"),
        )

    def iter_notifications(self, lookback_days: int = 30) -> Iterator[Tuple[str, str, Optional[str]]]:
        """Yield (subject, body, date) for each notification, per the selection mode."""
        if self._selection == SELECTION_LABELS:
            return self._iter_by_labels(lookback_days)
        return self._iter_by_subject(lookback_days)

    def _iter_by_subject(self, lookback_days: int) -> Iterator[Tuple[str, str, Optional[str]]]:
        """Search All Mail for notification-shaped subjects.

        Gmail's IMAP SUBJECT search matches tokens rather than literal
        substrings, so the trailing "-" is ignored and invoices and account
        emails come back too. parse_subject() rejects them downstream.
        """
        if self._conn is None:
            raise RuntimeError("not connected")

        folder = self._folder or self._discover_all_mail()
        self.select_folder(folder)

        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        # imaplib passes criteria through verbatim, so quote the phrase here.
        status, data = self._conn.search(
            None, "SINCE", since, "SUBJECT", '"{}"'.format(SUBJECT_PREFIX)
        )
        if status != "OK":
            raise RuntimeError("IMAP search failed: {}".format(status))

        message_ids = data[0].split() if data and data[0] else []
        logger.info(
            "subject selection: %d candidate message(s) in %s since %s",
            len(message_ids), folder, since,
        )
        for message_id in message_ids:
            yield self.fetch(message_id)

    def _iter_by_labels(self, lookback_days: int) -> Iterator[Tuple[str, str, Optional[str]]]:
        """Walk each label folder in turn.

        A message carrying two labels would otherwise be yielded twice, so
        results are de-duplicated on Message-ID. The duplicate modification
        emails Booking.com sends are *not* collapsed by this -- they are
        distinct messages with distinct Message-IDs.
        """
        seen = set()  # type: Set[str]

        for label in self._labels:
            try:
                self.select_folder(label)
            except RuntimeError:
                # A label that does not exist is a configuration problem worth
                # surfacing, but not a reason to abandon the other labels.
                logger.warning("label folder %r not found, skipping", label)
                continue

            message_ids = self._search_since(lookback_days)
            logger.info("label %r: %d message(s) in window", label, len(message_ids))

            for message_id in message_ids:
                subject, body, date_header, rfc_id = self._fetch_with_id(message_id)
                if rfc_id and rfc_id in seen:
                    continue
                if rfc_id:
                    seen.add(rfc_id)
                yield subject, body, date_header

    def audit_unlabelled(self, lookback_days: int = 30) -> List[Tuple[str, Optional[str]]]:
        """Find notification emails that no label covers.

        Sweeps All Mail by subject and subtracts everything the label folders
        return. Anything left arrived before its Gmail filter existed, or the
        filter failed to match -- either way the scheduled run will not see it.
        """
        labelled = set()  # type: Set[str]
        for label in self._labels:
            try:
                self.select_folder(label)
            except RuntimeError:
                logger.warning("label folder %r not found, skipping", label)
                continue
            for message_id in self._search_since(lookback_days):
                _, _, _, rfc_id = self._fetch_with_id(message_id)
                if rfc_id:
                    labelled.add(rfc_id)

        # NB: Gmail's IMAP SUBJECT search matches on tokens, not literal
        # substrings, so this also returns invoices and account emails that
        # merely contain "Booking.com". Callers filter with parse_subject().
        all_mail = self._folder or self._discover_all_mail()
        self.select_folder(all_mail)
        if self._conn is None:
            raise RuntimeError("not connected")
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        status, data = self._conn.search(
            None, "SINCE", since, "SUBJECT", '"{}"'.format(SUBJECT_PREFIX)
        )
        if status != "OK":
            raise RuntimeError("IMAP search failed: {}".format(status))

        missing = []
        for message_id in (data[0].split() if data and data[0] else []):
            subject, _, date_header, rfc_id = self._fetch_with_id(message_id)
            if rfc_id and rfc_id not in labelled:
                missing.append((subject, date_header))
        return missing


def _parse_list_row(row):
    """Split one IMAP LIST response row into (folder name, flags).

    Rows look like: (\\All \\HasNoChildren) "/" "[Gmail]/Alle e-mail"
    """
    if isinstance(row, bytes):
        text = row.decode("utf-8", "replace")
    elif isinstance(row, tuple):
        text = b" ".join(part for part in row if isinstance(part, bytes)).decode(
            "utf-8", "replace"
        )
    else:
        return None, ""

    flags = text[text.find("(") + 1:text.find(")")] if "(" in text else ""

    # The folder name is the last double-quoted run on the line; unquoted names
    # (rare, but legal) are the last whitespace-separated token.
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    if quoted:
        return quoted[-1].replace('\\"', '"').replace("\\\\", "\\"), flags
    parts = text.rsplit(" ", 1)
    return (parts[-1].strip() if len(parts) > 1 else None), flags


def _quote_mailbox(name: str) -> str:
    """Wrap a mailbox name in IMAP quotes unless it is already quoted."""
    if name.startswith('"') and name.endswith('"'):
        return name
    return '"{}"'.format(name.replace("\\", "\\\\").replace('"', '\\"'))


def _decode_subject(message: Message) -> str:
    """Decode a possibly RFC 2047-encoded subject into a single plain-text line.

    Booking.com subjects contain no non-ASCII today, but the guest names in
    other Booking.com mail do, so decoding is not optional.

    Long subjects are *folded* across lines in the raw header, e.g.::

        Subject: Booking.com - Nieuwe boeking! (5100000005, woensdag 10 september\r\n 2025)

    Folding whitespace carries no meaning (RFC 5322), so every run of whitespace
    is collapsed to a single space. Without this the CRLF survives into the
    parsed subject, and storing it somewhere that normalises CRLF to LF -- as
    Notion does -- makes the value differ on every read-back and the sync never
    converges.
    """
    raw = message.get("Subject", "")
    if not raw:
        return ""
    try:
        decoded = str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        logger.warning("could not decode subject header, using raw value")
        decoded = raw
    return " ".join(decoded.split())


def _plain_text_body(message: Message) -> str:
    """Extract the text/plain part, falling back to text/html stripped of tags."""
    plain_parts = []
    html_parts = []

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if "attachment" in (part.get("Content-Disposition") or ""):
            continue

        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")

        if part.get_content_type() == "text/plain":
            plain_parts.append(text)
        elif part.get_content_type() == "text/html":
            html_parts.append(text)

    if plain_parts:
        return "\n".join(plain_parts)
    return _strip_html("\n".join(html_parts))


def _strip_html(html: str) -> str:
    without_blocks = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", without_blocks)
    return " ".join(text.split())
