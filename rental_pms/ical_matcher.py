"""Match parsed arrival dates against the Booking.com iCal feed.

The notification emails give an arrival date but never a departure date, so the
stay length comes from the calendar export.

What the feed actually contains
-------------------------------
Every event looks like this, and they are all identical apart from the dates::

    BEGIN:VEVENT
    DTSTART;VALUE=DATE:20260901
    DTEND;VALUE=DATE:20260915
    UID:aaaa1111bbbb2222cccc3333dddd4444@booking.com
    SUMMARY:CLOSED - Not available
    END:VEVENT

Three consequences drive the design here:

1. **No reservation number and no guest name.** ``SUMMARY`` is the constant
   string "CLOSED - Not available" and ``UID`` is an opaque hash. Matching can
   only be done on ``DTSTART`` versus the arrival date from the email.
2. **Not every event is a booking.** The feed carried a 182-night event
   (2027-08-31 to 2028-02-29) that was a manual closure, alongside two real
   reservations. So the feed must never be enumerated as a list of bookings; it
   is only ever queried for a date we already know is an arrival.
3. **Contiguous ranges may be merged.** Booking.com publishes availability, not
   reservations. If two stays are back to back -- one guest out and the next in
   on the same day, which has happened here -- they could well appear as a
   single event. An arrival landing strictly inside an event is therefore
   reported as ambiguous rather than being given the event's end date, which
   would be the end of the whole run and not that guest's departure.

Because of (3) only an exact ``DTSTART`` match yields a departure date. Anything
else is surfaced for a human rather than guessed at.
"""

import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from .models import ReservationEvent

logger = logging.getLogger(__name__)

# iCal DTEND for DATE values is exclusive: it is the morning the room frees up,
# which is exactly the guest's checkout day. So departure == DTEND.
#
# CONFIRMED against two real reservations by the property owner:
#   6100000001  DTSTART 2026-09-01  DTEND 2026-09-15  -> checkout 15 Sept
#   5100000004  DTSTART 2027-07-23  DTEND 2027-08-06  -> checkout 6 Aug
# Both departures equal DTEND exactly. If this ever stops holding, it is the one
# line to change.
DTEND_IS_DEPARTURE = True

# Events longer than this are almost certainly manual closures rather than stays.
# Used only to annotate an otherwise-exact match, never to filter the feed.
#
# The one such event observed (2027-08-31 to 2028-02-29, 182 nights) turned out
# to be a mistaken closure and has since been removed by the owner, so nothing in
# the feed currently trips this. It is kept because a deliberate seasonal closure
# is plausible, and the failure it prevents -- writing a months-long "stay" into
# Notion -- is worse than the occasional ambiguous report.
LIKELY_BLOCK_NIGHTS = 60

MATCH_EXACT = "exact"
MATCH_AMBIGUOUS = "ambiguous"
MATCH_UNMATCHED = "unmatched"

DATE_PROP_RE = re.compile(r"^(DTSTART|DTEND)(?:;[^:]*)?:(\d{8})$", re.IGNORECASE)
UID_RE = re.compile(r"^UID:(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class IcalEvent:
    start: date
    end: date
    uid: str

    @property
    def nights(self) -> int:
        return (self.end - self.start).days

    @property
    def looks_like_manual_block(self) -> bool:
        return self.nights > LIKELY_BLOCK_NIGHTS

    def covers(self, day: date) -> bool:
        """True if ``day`` falls within the blocked range (end exclusive)."""
        return self.start <= day < self.end


@dataclass(frozen=True)
class IcalMatch:
    arrival: date
    status: str
    departure: Optional[date] = None
    event: Optional[IcalEvent] = None
    note: str = ""


def _unfold(text: str) -> List[str]:
    """Undo RFC 5545 line folding: a leading space continues the previous line."""
    lines = []  # type: List[str]
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_ics(text: str) -> List[IcalEvent]:
    """Parse the VEVENTs out of an .ics document.

    Hand-rolled rather than pulling in a calendar library: the feed uses only
    DATE-valued DTSTART/DTEND and a UID, and line unfolding is the only part of
    the format that is not trivial.
    """
    events = []  # type: List[IcalEvent]
    start = end = uid = None
    inside = False

    for line in _unfold(text):
        stripped = line.strip()
        if stripped.upper() == "BEGIN:VEVENT":
            inside, start, end, uid = True, None, None, None
            continue
        if stripped.upper() == "END:VEVENT":
            if start and end:
                events.append(IcalEvent(start=start, end=end, uid=uid or ""))
            else:
                logger.warning("skipping VEVENT with missing DTSTART/DTEND")
            inside = False
            continue
        if not inside:
            continue

        match = DATE_PROP_RE.match(stripped)
        if match:
            parsed = datetime.strptime(match.group(2), "%Y%m%d").date()
            if match.group(1).upper() == "DTSTART":
                start = parsed
            else:
                end = parsed
            continue

        uid_match = UID_RE.match(stripped)
        if uid_match:
            uid = uid_match.group(1).strip()

    events.sort(key=lambda e: e.start)
    return events


class IcalMatcher:
    """Looks up departure dates for known arrival dates."""

    def __init__(self, ical_url: str, timeout: int = 60) -> None:
        self._ical_url = ical_url
        self._timeout = timeout
        self._events = None  # type: Optional[List[IcalEvent]]

    def fetch(self) -> str:
        request = urllib.request.Request(
            self._ical_url, headers={"User-Agent": "rental-pms/0.1"}
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return response.read().decode("utf-8", "replace")

    def load(self, text: Optional[str] = None) -> List[IcalEvent]:
        """Fetch and parse the feed once, caching the result for this run."""
        if self._events is None:
            self._events = parse_ics(text if text is not None else self.fetch())
            logger.info("loaded %d event(s) from the iCal feed", len(self._events))
        return self._events

    def match(self, arrival: date) -> IcalMatch:
        """Look up one arrival date.

        Only an exact DTSTART match produces a departure date. See the module
        docstring for why a containing event is not good enough.
        """
        events = self.load()

        for event in events:
            if event.start == arrival:
                if event.looks_like_manual_block:
                    # An arrival that coincides with a long closure is more
                    # likely a coincidence than a genuine months-long stay.
                    return IcalMatch(
                        arrival=arrival,
                        status=MATCH_AMBIGUOUS,
                        event=event,
                        note="event starts on the arrival date but runs {} nights, "
                             "which looks like a manual block".format(event.nights),
                    )
                departure = event.end if DTEND_IS_DEPARTURE else None
                return IcalMatch(
                    arrival=arrival,
                    status=MATCH_EXACT,
                    departure=departure,
                    event=event,
                )

        for event in events:
            if event.covers(arrival):
                return IcalMatch(
                    arrival=arrival,
                    status=MATCH_AMBIGUOUS,
                    event=event,
                    note="arrival falls inside {}..{}; contiguous stays may be "
                         "merged in the feed, so the end date is not necessarily "
                         "this guest's departure".format(event.start, event.end),
                )

        return IcalMatch(
            arrival=arrival,
            status=MATCH_UNMATCHED,
            note="no event covers this date. Expected for cancelled bookings and "
                 "for stays already in the past -- the feed only carries current "
                 "and future dates.",
        )

    def departure_for_arrival(self, arrival: date) -> Optional[date]:
        """Departure date for a stay starting on ``arrival``, or None."""
        return self.match(arrival).departure

    def enrich(self, events: Sequence[ReservationEvent]) -> List[ReservationEvent]:
        """Return ``events`` with departure_date filled in where it is certain.

        Ambiguous and unmatched arrivals are logged and left with no departure
        date rather than being given a guessed one.
        """
        from dataclasses import replace

        enriched = []  # type: List[ReservationEvent]
        counts = {MATCH_EXACT: 0, MATCH_AMBIGUOUS: 0, MATCH_UNMATCHED: 0}

        # One lookup per distinct arrival date; several events can share one.
        cache = {}  # type: Dict[date, IcalMatch]

        for event in events:
            if event.arrival_date not in cache:
                cache[event.arrival_date] = self.match(event.arrival_date)
            result = cache[event.arrival_date]
            counts[result.status] += 1

            if result.status == MATCH_EXACT:
                enriched.append(replace(event, departure_date=result.departure))
            else:
                logger.info(
                    "no departure date for %s (arrival %s): %s",
                    event.reservation_number, event.arrival_date, result.note,
                )
                enriched.append(event)

        logger.info(
            "iCal matching: %d exact, %d ambiguous, %d unmatched",
            counts[MATCH_EXACT], counts[MATCH_AMBIGUOUS], counts[MATCH_UNMATCHED],
        )
        return enriched
