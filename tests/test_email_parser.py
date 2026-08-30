"""Parser tests.

Every subject line below has the structure of a real one from the mailbox.
Guest names and reservation numbers are anonymised -- see
docs/subject-line-analysis.md. Dates and wording are untouched, since the
weekday/date cross-check and the Dutch month table depend on them.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rental_pms.email_parser import (  # noqa: E402
    ParseError,
    extract_guest_name,
    parse_dutch_date,
    parse_email,
    parse_subject,
)
from rental_pms.models import EventType  # noqa: E402

# (subject, expected event type, expected reservation number, expected arrival)
REAL_SUBJECTS = [
    (
        "Booking.com - Nieuwe boeking! (5100000008, vrijdag 14 augustus 2026)",
        EventType.NEW, "5100000008", date(2026, 8, 14),
    ),
    (
        "Booking.com - Gewijzigde boeking! (5100000008, vrijdag 14 augustus 2026)",
        EventType.MODIFIED, "5100000008", date(2026, 8, 14),
    ),
    (
        "Booking.com - Geannuleerde boeking! (6100000003, vrijdag 14 augustus 2026)",
        EventType.CANCELLED, "6100000003", date(2026, 8, 14),
    ),
    (
        "Booking.com - Nieuwe last-minutereservering (6100000006, zondag 16 augustus 2026)",
        EventType.NEW, "6100000006", date(2026, 8, 16),
    ),
    (
        "Booking.com - Nieuwe boeking! (5100000002, dinsdag 18 augustus 2026)",
        EventType.NEW, "5100000002", date(2026, 8, 18),
    ),
    # Arrival almost a year after the send date -- proves it is not the send date.
    (
        "Booking.com - Nieuwe boeking! (5100000004, vrijdag 23 juli 2027)",
        EventType.NEW, "5100000004", date(2027, 7, 23),
    ),
    (
        "Booking.com - Nieuwe boeking! (6100000001, dinsdag 1 september 2026)",
        EventType.NEW, "6100000001", date(2026, 9, 1),
    ),
    (
        "Booking.com - Nieuwe boeking! (5100000001, woensdag 29 april 2026)",
        EventType.NEW, "5100000001", date(2026, 4, 29),
    ),
    (
        "Booking.com - Geannuleerde boeking! (5100000003, zondag 31 mei 2026)",
        EventType.CANCELLED, "5100000003", date(2026, 5, 31),
    ),
    (
        "Booking.com - Nieuwe boeking! (6100000002, vrijdag 26 juni 2026)",
        EventType.NEW, "6100000002", date(2026, 6, 26),
    ),
    (
        "Booking.com - Nieuwe boeking! (5100000009, zaterdag 13 juni 2026)",
        EventType.NEW, "5100000009", date(2026, 6, 13),
    ),
    (
        "Booking.com - Geannuleerde boeking! (5100000007, donderdag 13 augustus 2026)",
        EventType.CANCELLED, "5100000007", date(2026, 8, 13),
    ),
    (
        "Booking.com - Nieuwe boeking! (6100000004, maandag 6 juli 2026)",
        EventType.NEW, "6100000004", date(2026, 7, 6),
    ),
    (
        "Booking.com - Gewijzigde boeking! (5100000006, zondag 12 juli 2026)",
        EventType.MODIFIED, "5100000006", date(2026, 7, 12),
    ),
]


@pytest.mark.parametrize("subject,event_type,reservation,arrival", REAL_SUBJECTS)
def test_parses_real_subjects(subject, event_type, reservation, arrival):
    parsed_type, parsed_res, parsed_arrival, weekday_matches = parse_subject(subject)
    assert parsed_type is event_type
    assert parsed_res == reservation
    assert parsed_arrival == arrival
    # Every real subject's weekday agrees with its date -- so the dates are real
    # calendar dates and our month table is right.
    assert weekday_matches


def test_all_twelve_dutch_months_parse():
    months = [
        ("januari", 1), ("februari", 2), ("maart", 3), ("april", 4),
        ("mei", 5), ("juni", 6), ("juli", 7), ("augustus", 8),
        ("september", 9), ("oktober", 10), ("november", 11), ("december", 12),
    ]
    for name, number in months:
        parsed, _ = parse_dutch_date("15 {} 2026".format(name))
        assert parsed == date(2026, number, 15)


def test_weekday_is_optional():
    parsed, weekday_matches = parse_dutch_date("14 augustus 2026")
    assert parsed == date(2026, 8, 14)
    assert weekday_matches


def test_wrong_weekday_is_flagged_not_raised():
    # 14 August 2026 is a Friday, not a Monday.
    parsed, weekday_matches = parse_dutch_date("maandag 14 augustus 2026")
    assert parsed == date(2026, 8, 14)
    assert not weekday_matches


def test_unrelated_subject_rejected():
    with pytest.raises(ParseError):
        parse_subject("Booking.com Invoice 1100000001")
    with pytest.raises(ParseError):
        parse_subject("Wij hebben dit bericht ontvangen van Hannah Verstraete")
    with pytest.raises(ParseError):
        parse_subject(
            "Reservations with today's or tomorrow's arrival date for "
            "Example Rental - Studio met zeezicht"
        )


def test_unknown_month_rejected():
    with pytest.raises(ParseError):
        parse_dutch_date("vrijdag 14 augustus2 2026")


# Real cancellation body, hard-wrapped exactly as Booking.com sends it.
CANCELLATION_BODY = """
   Cancellation - 6100000003
   IATA/TIDS: PC000000

   Reserveringsnummer 6100000003 voor Élodie Devriendt is geannuleerd. We
   hebben deze annulering voor u verwerkt bij Booking.com en zullen de
   gast terugbetalen in overeenstemming met de voorwaarden.
"""


def test_extracts_guest_name_from_cancellation():
    assert extract_guest_name(CANCELLATION_BODY, "6100000003") == "Élodie Devriendt"


def test_guest_name_survives_line_wrap_mid_name():
    body = "Reserveringsnummer 6100000003 voor Élodie\n   Devriendt is geannuleerd."
    assert extract_guest_name(body, "6100000003") == "Élodie Devriendt"


def test_guest_name_ignored_for_other_reservation():
    assert extract_guest_name(CANCELLATION_BODY, "9999999999") is None


def test_guest_name_absent_returns_none():
    assert extract_guest_name("U heeft zojuist een nieuwe boeking ontvangen.", "5100000008") is None


def test_parse_email_end_to_end_cancellation():
    event = parse_email(
        "Booking.com - Geannuleerde boeking! (6100000003, vrijdag 14 augustus 2026)",
        CANCELLATION_BODY,
    )
    assert event.event_type is EventType.CANCELLED
    assert event.reservation_number == "6100000003"
    assert event.arrival_date == date(2026, 8, 14)
    assert event.guest_name == "Élodie Devriendt"


def test_parse_email_new_booking_has_no_guest_name():
    # New-booking bodies genuinely contain no name; it is backfilled later.
    event = parse_email(
        "Booking.com - Nieuwe boeking! (5100000008, vrijdag 14 augustus 2026)",
        "U heeft zojuist een nieuwe boeking ontvangen van een gast van Booking.com.",
    )
    assert event.event_type is EventType.NEW
    assert event.guest_name is None


# The second cancellation shape: cancelled inside the 24-hour grace period.
# This one names no guest at all -- verified against two real emails
# (reservations 6100000009 and 6100000007).
GRACE_PERIOD_BODY = """
   Cancellation - 6100000009
   IATA/TIDS: PC000000

   In reactie op de annulering van reservering 6100000009 bevestigen wij
   hierbij dat de annuleringskosten voor de gast nu EUR 0 bedragen.
   Deze reservering is geannuleerd binnen de Bedenkperiode van 24 uur. Er
   zijn geen annuleringskosten in rekening gebracht.
"""


def test_grace_period_cancellation_has_no_name():
    assert extract_guest_name(GRACE_PERIOD_BODY, "6100000009") is None


def test_grace_period_cancellation_is_recognised():
    from rental_pms.email_parser import is_grace_period_cancellation

    assert is_grace_period_cancellation(GRACE_PERIOD_BODY)
    # The standard shape must not be misfiled as a grace-period one.
    assert not is_grace_period_cancellation(CANCELLATION_BODY)


def test_grace_period_cancellation_still_parses_subject():
    event = parse_email(
        "Booking.com - Geannuleerde boeking! (6100000009, zondag 12 juli 2026)",
        GRACE_PERIOD_BODY,
    )
    assert event.event_type is EventType.CANCELLED
    assert event.arrival_date == date(2026, 7, 12)
    assert event.guest_name is None
