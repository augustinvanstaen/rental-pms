"""The default run output must not contain personal data.

This project's CI logs are public, so anything printed at default verbosity is
world-readable. A run once published real guest names and reservation numbers
to the Actions log; these tests exist so that cannot happen again silently.
"""

import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rental_pms.main import report  # noqa: E402
from rental_pms.models import EventType, ReservationEvent  # noqa: E402

GUEST = "Élodie Devriendt"
RESERVATION = "6100000003"

EVENTS = [
    ReservationEvent(
        event_type=EventType.CANCELLED,
        reservation_number=RESERVATION,
        arrival_date=date(2026, 8, 14),
        subject="Booking.com - Geannuleerde boeking! ({}, x)".format(RESERVATION),
        guest_name=GUEST,
        sent_at=date(2026, 8, 10),
    ),
    ReservationEvent(
        event_type=EventType.NEW,
        reservation_number="5100000008",
        arrival_date=date(2026, 9, 1),
        subject="Booking.com - Nieuwe boeking! (5100000008, x)",
        sent_at=date(2026, 8, 1),
        departure_date=date(2026, 9, 15),
    ),
]


def test_default_report_hides_names_and_numbers(capsys):
    report(EVENTS)
    out = capsys.readouterr().out
    assert GUEST not in out
    assert RESERVATION not in out
    assert "5100000008" not in out


def test_default_report_still_gives_useful_totals(capsys):
    report(EVENTS)
    out = capsys.readouterr().out
    assert "2 notification(s)" in out
    assert "1 cancelled" in out and "1 new" in out
    assert "1 with a departure date" in out
    assert "1 with a guest name" in out


def test_details_flag_opts_into_personal_data(capsys):
    report(EVENTS, detailed=True)
    out = capsys.readouterr().out
    assert GUEST in out
    assert RESERVATION in out


def test_empty_run_prints_nothing_sensitive(capsys):
    report([])
    out = capsys.readouterr().out
    assert "No Booking.com notifications" in out


def test_per_item_logs_are_debug_not_info(caplog):
    """Aggregate lines may be INFO; anything naming a reservation must be DEBUG."""
    from rental_pms.ical_matcher import IcalMatcher
    from rental_pms.notion_writer import NotionWriter, fold_events

    matcher = IcalMatcher("https://example.invalid")
    matcher.load("BEGIN:VCALENDAR\nEND:VCALENDAR\n")
    with caplog.at_level(logging.INFO):
        matcher.enrich(EVENTS)
    for record in caplog.records:
        assert RESERVATION not in record.getMessage()
        assert GUEST not in record.getMessage()

    caplog.clear()

    class Offline(NotionWriter):
        def _request(self, path, method="GET", body=None):
            return {"results": []} if path.endswith("/query") else {"id": "x"}

    writer = Offline("token", "db", dry_run=True)
    with caplog.at_level(logging.INFO):
        writer.sync(EVENTS)
    for record in caplog.records:
        assert RESERVATION not in record.getMessage()
        assert GUEST not in record.getMessage()
