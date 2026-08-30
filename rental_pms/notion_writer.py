"""Create and update reservation pages in the Notion database.

Keyed on the reservation number, which is the database's title property and the
only stable identifier Booking.com gives us.

What this writes, and what it must never touch
----------------------------------------------
The database is not just a mirror of the mailbox -- it drives a cleaning
workflow, and a human maintains part of it. The "Action items" view filters on
``Cleaning status`` and ``Departure``, so those columns matter operationally.

Owned by this script (safe to overwrite):
    Reservation #, Booking status, Arrival, Departure, Guest name,
    Email received, Raw source

Owned by the human (never written, never cleared):
    Cleaning status, Cleaning notes, Amount paid

``Nights`` is a formula and is read-only by definition.

Two rules beyond that, both about not destroying information:

* A field is never cleared by an absent value. New and modified booking emails
  carry no guest name, so writing ``None`` over a name the digest already filled
  in would lose it. Same for departure dates, which only exist for stays still
  in the iCal feed.
* A cancellation updates ``Booking status`` rather than deleting the page, so
  the history stays visible and the human's cleaning notes survive.

Idempotency
-----------
Booking.com re-sends notifications -- an identical modification email arrived
twice in this mailbox at the same timestamp. Runs are therefore fully
idempotent: the desired state is folded per reservation before writing, and a
page is only patched when a field actually differs.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence

from .models import EventType, ReservationEvent

logger = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1/"
NOTION_VERSION = "2022-06-28"

# Notion's published rate limit is roughly three requests per second.
WRITE_DELAY_SECONDS = 0.35

TITLE_PROPERTY = "Reservation #"
STATUS_PROPERTY = "Booking status"
ARRIVAL_PROPERTY = "Arrival"
DEPARTURE_PROPERTY = "Departure"
GUEST_PROPERTY = "Guest name"
RECEIVED_PROPERTY = "Email received"
RAW_PROPERTY = "Raw source"

# Properties a human maintains. Listed so the intent is greppable, and asserted
# against in the tests -- nothing here may ever appear in a write payload.
HUMAN_OWNED_PROPERTIES = ("Cleaning status", "Cleaning notes", "Amount paid")

STATUS_FOR_EVENT = {
    EventType.NEW: "New",
    EventType.MODIFIED: "Modified",
    EventType.CANCELLED: "Cancelled",
}


class NotionError(RuntimeError):
    """Raised when the Notion API rejects a request."""


@dataclass
class DesiredState:
    """The state one reservation should end up in after a run."""

    reservation_number: str
    status: str
    arrival: date
    subject: str
    departure: Optional[date] = None
    guest_name: Optional[str] = None
    received: Optional[date] = None


def fold_events(events: Sequence[ReservationEvent]) -> List[DesiredState]:
    """Collapse many notifications per reservation into one desired state each.

    A reservation typically produces several emails -- booked, modified,
    cancelled. Only the latest one determines the status, so events are applied
    in send order. Fields are merged rather than replaced: a guest name that
    appeared on the cancellation is kept even though the later email is not the
    one that carried it.
    """
    ordered = sorted(
        events,
        # Events without a send date sort first; they are the least authoritative.
        key=lambda e: (e.sent_at is not None, e.sent_at or date.min),
    )

    folded = {}  # type: Dict[str, DesiredState]
    for event in ordered:
        current = folded.get(event.reservation_number)
        if current is None:
            folded[event.reservation_number] = DesiredState(
                reservation_number=event.reservation_number,
                status=STATUS_FOR_EVENT[event.event_type],
                arrival=event.arrival_date,
                subject=event.subject,
                departure=event.departure_date,
                guest_name=event.guest_name,
                received=event.sent_at,
            )
            continue

        # Later email wins on status, arrival and provenance...
        current.status = STATUS_FOR_EVENT[event.event_type]
        current.arrival = event.arrival_date
        current.subject = event.subject
        if event.sent_at:
            current.received = event.sent_at
        # ...but never blanks a value we already have.
        if event.departure_date:
            current.departure = event.departure_date
        if event.guest_name:
            current.guest_name = event.guest_name

    return [folded[key] for key in sorted(folded)]


def _text(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value}}]}


def _date(value: date) -> dict:
    return {"date": {"start": value.isoformat()}}


class NotionWriter:
    def __init__(self, api_token: str, database_id: str, dry_run: bool = False,
                 timeout: int = 30) -> None:
        self._api_token = api_token
        self._database_id = database_id
        self._dry_run = dry_run
        self._timeout = timeout

    # -- HTTP ---------------------------------------------------------------

    def _request(self, path: str, method: str = "GET", body: Optional[dict] = None) -> dict:
        request = urllib.request.Request(
            NOTION_API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self._api_token,
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                detail = "{}: {}".format(parsed.get("code"), parsed.get("message"))
            except ValueError:
                pass
            raise NotionError("{} {} -> {} ({})".format(method, path, exc.code, detail))

    # -- Reads --------------------------------------------------------------

    def find_page(self, reservation_number: str) -> Optional[dict]:
        """Return the existing page for a reservation, or None."""
        result = self._request(
            "databases/{}/query".format(self._database_id),
            method="POST",
            body={
                "filter": {
                    "property": TITLE_PROPERTY,
                    "title": {"equals": reservation_number},
                },
                "page_size": 2,
            },
        )
        pages = result.get("results", [])
        if len(pages) > 1:
            logger.warning(
                "%d pages share reservation number %s; updating the first",
                len(pages), reservation_number,
            )
        return pages[0] if pages else None

    # -- Diffing ------------------------------------------------------------

    @staticmethod
    def _current_values(page: dict) -> dict:
        """Read back only the properties this script owns."""
        props = page.get("properties", {})

        def plain(name):
            parts = props.get(name, {}).get("rich_text") or []
            return "".join(p.get("plain_text", "") for p in parts) or None

        def day(name):
            value = props.get(name, {}).get("date")
            return value.get("start") if value else None

        select = props.get(STATUS_PROPERTY, {}).get("select")
        title_parts = props.get(TITLE_PROPERTY, {}).get("title") or []

        return {
            TITLE_PROPERTY: "".join(p.get("plain_text", "") for p in title_parts) or None,
            STATUS_PROPERTY: select.get("name") if select else None,
            ARRIVAL_PROPERTY: day(ARRIVAL_PROPERTY),
            DEPARTURE_PROPERTY: day(DEPARTURE_PROPERTY),
            GUEST_PROPERTY: plain(GUEST_PROPERTY),
            RECEIVED_PROPERTY: day(RECEIVED_PROPERTY),
            RAW_PROPERTY: plain(RAW_PROPERTY),
        }

    def _changed_properties(self, state: DesiredState, page: Optional[dict]) -> dict:
        """Build a payload containing only properties that actually differ.

        Returning an empty dict means the page is already correct, which is what
        makes repeat runs no-ops.
        """
        current = self._current_values(page) if page else {}
        payload = {}

        def maybe(name, new_value, formatted):
            # An absent new value never clears what is already there.
            if new_value is None:
                return
            if current.get(name) == new_value:
                return
            payload[name] = formatted

        if page is None:
            payload[TITLE_PROPERTY] = {
                "title": [{"type": "text", "text": {"content": state.reservation_number}}]
            }

        maybe(STATUS_PROPERTY, state.status, {"select": {"name": state.status}})
        maybe(ARRIVAL_PROPERTY, state.arrival.isoformat(), _date(state.arrival))
        if state.departure:
            maybe(DEPARTURE_PROPERTY, state.departure.isoformat(), _date(state.departure))
        if state.guest_name:
            maybe(GUEST_PROPERTY, state.guest_name, _text(state.guest_name))
        if state.received:
            maybe(RECEIVED_PROPERTY, state.received.isoformat(), _date(state.received))
        maybe(RAW_PROPERTY, state.subject, _text(state.subject))

        return payload

    # -- Writes -------------------------------------------------------------

    def upsert(self, state: DesiredState) -> str:
        """Create or patch the page for one reservation.

        Returns "created", "updated" or "unchanged".
        """
        page = self.find_page(state.reservation_number)
        changes = self._changed_properties(state, page)

        # Belt and braces: the human's columns must never reach a payload.
        for owned in HUMAN_OWNED_PROPERTIES:
            assert owned not in changes, "refusing to write human-owned {}".format(owned)

        if page is not None and not changes:
            return "unchanged"

        action = "updated" if page else "created"
        logger.debug(
            "%s %s: %s", action, state.reservation_number, ", ".join(sorted(changes))
        )
        if self._dry_run:
            logger.info(
                "[dry-run] would have %s %s: %s",
                action, state.reservation_number, ", ".join(sorted(changes)),
            )
            return action

        if page:
            self._request(
                "pages/" + page["id"], method="PATCH", body={"properties": changes}
            )
        else:
            self._request(
                "pages",
                method="POST",
                body={
                    "parent": {"database_id": self._database_id},
                    "properties": changes,
                },
            )
        time.sleep(WRITE_DELAY_SECONDS)
        return action

    def sync(self, events: Sequence[ReservationEvent]) -> Dict[str, int]:
        """Fold the events and bring every reservation up to date."""
        counts = {"created": 0, "updated": 0, "unchanged": 0}
        states = fold_events(events)

        for state in states:
            outcome = self.upsert(state)
            counts[outcome] += 1
            logger.info(
                "%s %s (%s, arrival %s)",
                outcome, state.reservation_number, state.status, state.arrival,
            )

        logger.info(
            "Notion sync: %d created, %d updated, %d unchanged%s",
            counts["created"], counts["updated"], counts["unchanged"],
            " (dry run, nothing written)" if self._dry_run else "",
        )
        return counts
