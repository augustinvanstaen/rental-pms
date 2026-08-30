"""iCal matcher tests, built on the real feed content."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rental_pms.ical_matcher import (  # noqa: E402
    MATCH_AMBIGUOUS,
    MATCH_EXACT,
    MATCH_UNMATCHED,
    IcalMatcher,
    parse_ics,
)
from rental_pms.email_parser import parse_email  # noqa: E402

# Verbatim copy of the live feed on 2026-08-30. Kept as a fixture rather than
# refreshed: the 182-night event was a mistaken closure that the owner has since
# removed, and it is the only sample of a manual block available to test the
# block heuristic against. The two bookings are real, and both their departure
# dates have been confirmed by the owner.
REAL_FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//admin.booking.com\\\\\\, b.v.//NONSGML v1.0//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
DTSTAMP:20260830T123812Z
DTSTART;VALUE=DATE:20260901
DTEND;VALUE=DATE:20260915
UID:aaaa1111bbbb2222cccc3333dddd4444@booking.com
SUMMARY:CLOSED - Not available
ORGANIZER:mailto:noreply@booking.com
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20260830T123812Z
DTSTART;VALUE=DATE:20270723
DTEND;VALUE=DATE:20270806
UID:eeee5555ffff6666aaaa7777bbbb8888@booking.com
SUMMARY:CLOSED - Not available
ORGANIZER:mailto:noreply@booking.com
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20260830T123812Z
DTSTART;VALUE=DATE:20270831
DTEND;VALUE=DATE:20280229
UID:cccc9999dddd0000eeee1111ffff2222@booking.com
SUMMARY:CLOSED - Not available
ORGANIZER:mailto:noreply@booking.com
END:VEVENT
END:VCALENDAR
"""


def _matcher():
    matcher = IcalMatcher("https://example.invalid/ical")
    matcher.load(REAL_FEED)
    return matcher


def test_parses_all_three_events():
    events = parse_ics(REAL_FEED)
    assert len(events) == 3
    assert events[0].start == date(2026, 9, 1)
    assert events[0].end == date(2026, 9, 15)
    assert events[0].uid == "aaaa1111bbbb2222cccc3333dddd4444@booking.com"


def test_line_folding_is_undone():
    folded = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        "DTSTART;VALUE=DA\r\n TE:20260901\r\n"
        "DTEND;VALUE=DATE:20260915\r\nUID:x@booking.com\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events = parse_ics(folded)
    assert len(events) == 1
    assert events[0].start == date(2026, 9, 1)


def test_event_missing_dates_is_skipped_not_fatal():
    broken = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x@booking.com\nEND:VEVENT\n"
        "BEGIN:VEVENT\nDTSTART;VALUE=DATE:20260901\nDTEND;VALUE=DATE:20260915\n"
        "UID:y@booking.com\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    events = parse_ics(broken)
    assert len(events) == 1
    assert events[0].uid == "y@booking.com"


def test_exact_match_yields_departure():
    # Reservation 6100000001: subject says arrival 1 September 2026, and the
    # owner confirmed the guest checks out on 15 September. So DTEND is the
    # departure date, not the last night.
    result = _matcher().match(date(2026, 9, 1))
    assert result.status == MATCH_EXACT
    assert result.departure == date(2026, 9, 15)


def test_second_real_booking_matches():
    # Reservation 5100000004: owner confirmed arrival 23 July, departure 6 Aug
    # 2027. Second independent confirmation that departure == DTEND.
    result = _matcher().match(date(2027, 7, 23))
    assert result.status == MATCH_EXACT
    assert result.departure == date(2027, 8, 6)


def test_long_closure_is_not_treated_as_a_stay():
    # 2027-08-31 to 2028-02-29 is 182 nights: a manual block, not a booking.
    result = _matcher().match(date(2027, 8, 31))
    assert result.status == MATCH_AMBIGUOUS
    assert result.departure is None
    assert "manual block" in result.note


def test_arrival_inside_an_event_is_ambiguous_not_guessed():
    # If contiguous stays were merged, the event's end is the end of the whole
    # run, not this guest's departure. Must not be reported as a departure.
    result = _matcher().match(date(2026, 9, 5))
    assert result.status == MATCH_AMBIGUOUS
    assert result.departure is None


def test_unknown_date_is_unmatched():
    result = _matcher().match(date(2026, 8, 14))  # a past stay, not in the feed
    assert result.status == MATCH_UNMATCHED
    assert result.departure is None


def test_dtend_is_exclusive_so_covers_stops_before_it():
    matcher = _matcher()
    # 14 Sept is the last night; 15 Sept is checkout and the room is free.
    assert matcher.match(date(2026, 9, 14)).status == MATCH_AMBIGUOUS
    assert matcher.match(date(2026, 9, 15)).status == MATCH_UNMATCHED


def test_enrich_fills_only_confident_departures():
    matched = parse_email(
        "Booking.com - Nieuwe boeking! (6100000001, dinsdag 1 september 2026)"
    )
    unmatched = parse_email(
        "Booking.com - Nieuwe boeking! (5100000008, vrijdag 14 augustus 2026)"
    )

    result = _matcher().enrich([matched, unmatched])
    assert result[0].departure_date == date(2026, 9, 15)
    assert result[1].departure_date is None
    # Enrichment must not disturb anything else on the event.
    assert result[1].reservation_number == "5100000008"
    assert result[1].arrival_date == date(2026, 8, 14)


def test_enrich_handles_empty_input():
    assert _matcher().enrich([]) == []
