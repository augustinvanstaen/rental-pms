"""Notion writer tests. No network: the HTTP layer is stubbed."""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rental_pms.email_parser import parse_email  # noqa: E402
from rental_pms.models import EventType, ReservationEvent  # noqa: E402
from rental_pms.notion_writer import (  # noqa: E402
    HUMAN_OWNED_PROPERTIES,
    NotionWriter,
    fold_events,
)


def event(number, event_type, arrival, sent_at=None, guest=None, departure=None):
    return ReservationEvent(
        event_type=event_type,
        reservation_number=number,
        arrival_date=arrival,
        subject="Booking.com - test ({}, x)".format(number),
        guest_name=guest,
        sent_at=sent_at,
        departure_date=departure,
    )


class FakeWriter(NotionWriter):
    """NotionWriter with the HTTP layer replaced by an in-memory store."""

    def __init__(self, pages=None):
        super().__init__("token", "db", dry_run=False)
        self.pages = pages or {}
        self.requests = []

    def _request(self, path, method="GET", body=None):
        self.requests.append((method, path, body))
        if path.endswith("/query"):
            wanted = body["filter"]["title"]["equals"]
            page = self.pages.get(wanted)
            return {"results": [page] if page else []}
        return {"id": "new-page"}


def existing_page(page_id, number, status=None, arrival=None, departure=None,
                  guest=None, received=None, raw=None):
    def rich(value):
        return {"rich_text": [{"plain_text": value}] if value else []}

    def day(value):
        return {"date": {"start": value} if value else None}

    return {
        "id": page_id,
        "properties": {
            "Reservation #": {"title": [{"plain_text": number}]},
            "Booking status": {"select": {"name": status} if status else None},
            "Arrival": day(arrival),
            "Departure": day(departure),
            "Guest name": rich(guest),
            "Email received": day(received),
            "Raw source": rich(raw),
            # Human-owned; present so we can prove they are never written.
            "Cleaning status": {"select": {"name": "Done"}},
            "Cleaning notes": rich("bring extra towels"),
            "Amount paid": {"number": 420.0},
        },
    }


# --- folding ---------------------------------------------------------------

def test_latest_event_determines_status():
    events = [
        event("111", EventType.NEW, date(2026, 8, 14), sent_at=date(2026, 6, 11)),
        event("111", EventType.MODIFIED, date(2026, 8, 14), sent_at=date(2026, 7, 1)),
        event("111", EventType.CANCELLED, date(2026, 8, 14), sent_at=date(2026, 8, 10)),
    ]
    folded = fold_events(events)
    assert len(folded) == 1
    assert folded[0].status == "Cancelled"


def test_out_of_order_input_still_folds_by_send_date():
    events = [
        event("111", EventType.CANCELLED, date(2026, 8, 14), sent_at=date(2026, 8, 10)),
        event("111", EventType.NEW, date(2026, 8, 14), sent_at=date(2026, 6, 11)),
    ]
    assert fold_events(events)[0].status == "Cancelled"


def test_guest_name_survives_a_later_nameless_event():
    # The name only ever appears on the cancellation; a later modification must
    # not erase it.
    events = [
        event("111", EventType.CANCELLED, date(2026, 8, 14),
              sent_at=date(2026, 7, 1), guest="Elodie Devriendt"),
        event("111", EventType.MODIFIED, date(2026, 8, 14), sent_at=date(2026, 8, 10)),
    ]
    folded = fold_events(events)
    assert folded[0].guest_name == "Elodie Devriendt"
    assert folded[0].status == "Modified"


def test_departure_survives_a_later_event_without_one():
    events = [
        event("111", EventType.NEW, date(2026, 9, 1), sent_at=date(2026, 8, 1),
              departure=date(2026, 9, 15)),
        event("111", EventType.MODIFIED, date(2026, 9, 1), sent_at=date(2026, 8, 20)),
    ]
    assert fold_events(events)[0].departure == date(2026, 9, 15)


def test_separate_reservations_stay_separate():
    events = [
        event("111", EventType.NEW, date(2026, 8, 14), sent_at=date(2026, 6, 11)),
        event("222", EventType.NEW, date(2026, 8, 16), sent_at=date(2026, 6, 12)),
    ]
    assert len(fold_events(events)) == 2


def test_duplicate_identical_emails_fold_to_one():
    # Booking.com sent this modification twice at the same timestamp.
    dup = event("111", EventType.MODIFIED, date(2026, 8, 14), sent_at=date(2026, 8, 13))
    assert len(fold_events([dup, dup])) == 1


# --- writing ---------------------------------------------------------------

def test_creates_page_when_none_exists():
    writer = FakeWriter()
    state = fold_events([
        event("111", EventType.NEW, date(2026, 8, 14), sent_at=date(2026, 6, 11))
    ])[0]
    assert writer.upsert(state) == "created"
    method, path, body = writer.requests[-1]
    assert (method, path) == ("POST", "pages")
    assert body["properties"]["Reservation #"]["title"][0]["text"]["content"] == "111"


def test_identical_state_is_unchanged():
    page = existing_page(
        "p1", "111", status="New", arrival="2026-08-14",
        received="2026-06-11", raw="Booking.com - test (111, x)",
    )
    writer = FakeWriter({"111": page})
    state = fold_events([
        event("111", EventType.NEW, date(2026, 8, 14), sent_at=date(2026, 6, 11))
    ])[0]
    assert writer.upsert(state) == "unchanged"
    # A no-op run must not issue a write at all.
    assert all(method != "PATCH" for method, _, _ in writer.requests)


def test_status_change_patches_only_that_property():
    page = existing_page(
        "p1", "111", status="New", arrival="2026-08-14",
        received="2026-06-11", raw="Booking.com - test (111, x)",
    )
    writer = FakeWriter({"111": page})
    state = fold_events([
        event("111", EventType.CANCELLED, date(2026, 8, 14), sent_at=date(2026, 6, 11))
    ])[0]
    assert writer.upsert(state) == "updated"
    method, path, body = writer.requests[-1]
    assert method == "PATCH" and path == "pages/p1"
    assert list(body["properties"]) == ["Booking status"]


def test_human_owned_properties_are_never_written():
    page = existing_page("p1", "111", status="New", arrival="2026-08-14")
    writer = FakeWriter({"111": page})
    state = fold_events([
        event("111", EventType.CANCELLED, date(2026, 8, 14),
              sent_at=date(2026, 8, 10), guest="Someone", departure=date(2026, 8, 16))
    ])[0]
    writer.upsert(state)
    for _, _, body in writer.requests:
        if not body or "properties" not in body:
            continue
        for owned in HUMAN_OWNED_PROPERTIES:
            assert owned not in body["properties"]
        assert "Nights" not in body["properties"]


def test_absent_guest_name_does_not_clear_an_existing_one():
    page = existing_page(
        "p1", "111", status="New", arrival="2026-08-14", guest="Margot Vermeulen",
        received="2026-06-11", raw="Booking.com - test (111, x)",
    )
    writer = FakeWriter({"111": page})
    # A modification email carries no guest name at all.
    state = fold_events([
        event("111", EventType.MODIFIED, date(2026, 8, 14), sent_at=date(2026, 6, 11))
    ])[0]
    writer.upsert(state)
    patches = [b for m, _, b in writer.requests if m == "PATCH"]
    assert all("Guest name" not in p["properties"] for p in patches)


def test_absent_departure_does_not_clear_an_existing_one():
    page = existing_page(
        "p1", "111", status="New", arrival="2026-09-01", departure="2026-09-15",
        received="2026-06-11", raw="Booking.com - test (111, x)",
    )
    writer = FakeWriter({"111": page})
    state = fold_events([
        event("111", EventType.MODIFIED, date(2026, 9, 1), sent_at=date(2026, 6, 11))
    ])[0]
    writer.upsert(state)
    patches = [b for m, _, b in writer.requests if m == "PATCH"]
    assert all("Departure" not in p["properties"] for p in patches)


def test_dry_run_issues_no_writes():
    writer = FakeWriter()
    writer._dry_run = True
    state = fold_events([
        event("111", EventType.NEW, date(2026, 8, 14), sent_at=date(2026, 6, 11))
    ])[0]
    assert writer.upsert(state) == "created"
    assert all(m in ("GET", "POST") and p.endswith("/query") for m, p, _ in writer.requests)


def test_sync_counts_outcomes():
    page = existing_page(
        "p1", "111", status="New", arrival="2026-08-14",
        received="2026-06-11", raw="Booking.com - test (111, x)",
    )
    writer = FakeWriter({"111": page})
    counts = writer.sync([
        event("111", EventType.NEW, date(2026, 8, 14), sent_at=date(2026, 6, 11)),
        event("222", EventType.NEW, date(2026, 8, 16), sent_at=date(2026, 6, 12)),
    ])
    assert counts == {"created": 1, "updated": 0, "unchanged": 1}


def test_real_parsed_events_fold_correctly():
    # Reservation 6100000003's actual lifecycle from the mailbox.
    events = [
        parse_email(
            "Booking.com - Nieuwe boeking! (6100000003, vrijdag 14 augustus 2026)",
            "", date(2026, 6, 11)),
        parse_email(
            "Booking.com - Gewijzigde boeking! (6100000003, vrijdag 14 augustus 2026)",
            "", date(2026, 7, 1)),
        parse_email(
            "Booking.com - Geannuleerde boeking! (6100000003, vrijdag 14 augustus 2026)",
            "Reserveringsnummer 6100000003 voor Elodie Devriendt is geannuleerd.",
            date(2026, 8, 10)),
    ]
    folded = fold_events(events)
    assert len(folded) == 1
    assert folded[0].status == "Cancelled"
    assert folded[0].guest_name == "Elodie Devriendt"
    assert folded[0].arrival == date(2026, 8, 14)
