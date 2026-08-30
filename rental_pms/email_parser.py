"""Parse Booking.com notification emails (Dutch locale).

Two things are extracted:

1. From the *subject line*: the event type, the reservation number and the
   guest's **arrival date**. The bodies of new/modified booking emails contain
   no dates at all, so the subject is the only source for the arrival date.
2. From the *body*, cancellations only: the guest name.

Subject formats seen in the wild::

    Booking.com - Nieuwe boeking! (5100000008, vrijdag 14 augustus 2026)
    Booking.com - Gewijzigde boeking! (5100000008, vrijdag 14 augustus 2026)
    Booking.com - Geannuleerde boeking! (6100000003, vrijdag 14 augustus 2026)
    Booking.com - Nieuwe last-minutereservering (6100000006, zondag 16 augustus 2026)

Note the last-minute variant has no exclamation mark and does not use the word
"boeking", so the parser keys off the leading verb rather than the full phrase.
"""

import re
import unicodedata
from datetime import date
from typing import Optional

from .models import EventType, ReservationEvent

# Dutch month names, lowercase, as they appear in subject lines.
DUTCH_MONTHS = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}

# Dutch weekday names -> Python's Monday=0 convention. Used only to sanity-check
# the parsed date; a mismatch means we misread the subject.
DUTCH_WEEKDAYS = {
    "maandag": 0,
    "dinsdag": 1,
    "woensdag": 2,
    "donderdag": 3,
    "vrijdag": 4,
    "zaterdag": 5,
    "zondag": 6,
}

# "Booking.com - <something> (<digits>, <dutch date>)"
# The date group is deliberately loose; DATE_RE below does the real validation.
SUBJECT_RE = re.compile(
    r"^\s*Booking\.com\s*[-–—]\s*"
    r"(?P<kind>.+?)\s*"
    r"\(\s*(?P<reservation>\d{6,15})\s*,\s*(?P<date>[^)]+?)\s*\)\s*$",
    re.IGNORECASE,
)

DATE_RE = re.compile(
    r"^(?:(?P<weekday>[a-z]+)\s+)?"
    r"(?P<day>\d{1,2})\s+"
    r"(?P<month>[a-z]+)\s+"
    r"(?P<year>\d{4})$",
    re.IGNORECASE,
)

# Cancellation bodies come in two shapes.
#
# Standard -- names the guest:
#   "Reserveringsnummer 6100000003 voor Elodie Devriendt is geannuleerd."
#
# Grace period (cancelled inside Booking.com's 24-hour "Bedenkperiode") --
# names no one:
#   "In reactie op de annulering van reservering 6100000009 bevestigen wij
#    hierbij dat de annuleringskosten voor de gast nu EUR 0 bedragen."
#
# For the second shape the guest name is simply not in the email, so it has to
# be backfilled from another source. GRACE_PERIOD_CANCELLATION_RE exists so
# callers can tell "no name in this email" from "the regex failed".
#
# Body text is hard-wrapped, so whitespace is collapsed before matching.
CANCELLATION_NAME_RE = re.compile(
    r"Reserveringsnummer\s+(?P<reservation>\d{6,15})\s+voor\s+"
    r"(?P<name>.+?)\s+is\s+geannuleerd",
    re.IGNORECASE,
)

GRACE_PERIOD_CANCELLATION_RE = re.compile(
    r"annulering\s+van\s+reservering\s+(?P<reservation>\d{6,15})",
    re.IGNORECASE,
)


class ParseError(ValueError):
    """Raised when a subject line does not look like a Booking.com notification."""


def _classify(kind: str) -> EventType:
    """Map the Dutch phrase between "Booking.com -" and "(" to an event type.

    Matched on a stripped-accent, lowercased form so that e.g. a future
    "Geannuleerde reservering" still classifies correctly.
    """
    normalized = unicodedata.normalize("NFKD", kind).encode("ascii", "ignore").decode()
    normalized = normalized.lower()

    # Order matters: check cancel/modify before "nieuw", since a phrase could in
    # principle contain both (e.g. "Gewijzigde nieuwe boeking").
    if "geannuleerd" in normalized or "annulering" in normalized:
        return EventType.CANCELLED
    if "gewijzigd" in normalized or "wijziging" in normalized:
        return EventType.MODIFIED
    if "nieuw" in normalized:
        return EventType.NEW
    raise ParseError("unrecognised Booking.com event phrase: {!r}".format(kind))


def parse_dutch_date(text: str):
    """Parse "vrijdag 14 augustus 2026" -> (date(2026, 8, 14), weekday_matches).

    The weekday is optional. When present it is checked against the calendar;
    a mismatch is reported rather than raised, so a single odd email does not
    stop the run.
    """
    match = DATE_RE.match(text.strip())
    if not match:
        raise ParseError("unparseable Dutch date: {!r}".format(text))

    month_name = match.group("month").lower()
    if month_name not in DUTCH_MONTHS:
        raise ParseError("unknown Dutch month: {!r}".format(match.group("month")))

    parsed = date(
        int(match.group("year")),
        DUTCH_MONTHS[month_name],
        int(match.group("day")),
    )

    weekday_matches = True
    weekday_name = match.group("weekday")
    if weekday_name:
        expected = DUTCH_WEEKDAYS.get(weekday_name.lower())
        if expected is None:
            raise ParseError("unknown Dutch weekday: {!r}".format(weekday_name))
        weekday_matches = parsed.weekday() == expected

    return parsed, weekday_matches


def parse_subject(subject: str):
    """Extract (event_type, reservation_number, arrival_date, weekday_matches)."""
    match = SUBJECT_RE.match(subject)
    if not match:
        raise ParseError("not a Booking.com notification subject: {!r}".format(subject))

    event_type = _classify(match.group("kind"))
    arrival_date, weekday_matches = parse_dutch_date(match.group("date"))
    return event_type, match.group("reservation"), arrival_date, weekday_matches


def extract_guest_name(body: str, reservation_number: Optional[str] = None) -> Optional[str]:
    """Pull the guest name out of a cancellation body, or None if absent.

    When ``reservation_number`` is given, the name is only returned if the body
    names that same reservation -- guarding against a forwarded or quoted email
    that mentions a different booking.
    """
    if not body:
        return None

    collapsed = " ".join(body.split())
    for match in CANCELLATION_NAME_RE.finditer(collapsed):
        if reservation_number and match.group("reservation") != reservation_number:
            continue
        name = match.group("name").strip(" .,")
        if name:
            return name
    return None


def is_grace_period_cancellation(body: str) -> bool:
    """True for the 24-hour-grace-period cancellation, which omits the guest name.

    Lets step 5 distinguish a genuinely name-less email from a parse failure,
    so it can go looking elsewhere for the name instead of logging a warning.
    """
    if not body:
        return False
    return bool(GRACE_PERIOD_CANCELLATION_RE.search(" ".join(body.split())))


def parse_email(subject: str, body: str = "", sent_at: Optional[date] = None) -> ReservationEvent:
    """Parse one notification email into a ReservationEvent."""
    event_type, reservation_number, arrival_date, weekday_matches = parse_subject(subject)

    guest_name = None
    if event_type is EventType.CANCELLED:
        guest_name = extract_guest_name(body, reservation_number)

    return ReservationEvent(
        event_type=event_type,
        reservation_number=reservation_number,
        arrival_date=arrival_date,
        subject=subject,
        guest_name=guest_name,
        sent_at=sent_at,
        weekday_matches=weekday_matches,
    )
