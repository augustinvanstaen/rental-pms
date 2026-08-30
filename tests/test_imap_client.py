"""Tests for the pure helpers in imap_client. No network."""

import email as emaillib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rental_pms.email_parser import parse_subject  # noqa: E402
from rental_pms.imap_client import (  # noqa: E402
    _decode_subject,
    _parse_list_row,
    _quote_mailbox,
)


def message_with_subject(raw_header: bytes):
    return emaillib.message_from_bytes(raw_header)


def test_folded_subject_is_unfolded_to_one_line():
    # Real header from the mailbox: the subject wraps before the year.
    raw = (
        b"Subject: Booking.com - Nieuwe boeking! (5100000005, woensdag 10 september"
        b"\r\n 2025)\r\n\r\n"
    )
    subject = _decode_subject(message_with_subject(raw))
    assert "\n" not in subject and "\r" not in subject
    assert subject == (
        "Booking.com - Nieuwe boeking! (5100000005, woensdag 10 september 2025)"
    )


def test_unfolded_subject_still_parses():
    raw = (
        b"Subject: Booking.com - Nieuwe last-minutereservering (5100000010, vrijdag 17"
        b"\r\n oktober 2025)\r\n\r\n"
    )
    subject = _decode_subject(message_with_subject(raw))
    _, reservation, arrival, weekday_ok = parse_subject(subject)
    assert reservation == "5100000010"
    assert arrival.isoformat() == "2025-10-17"
    assert weekday_ok


def test_rfc2047_encoded_subject_is_decoded():
    # Two base64 encoded-words, folded across lines, exactly as Gmail delivers
    # a subject containing non-ASCII or exceeding the line limit.
    raw = (
        b"Subject: =?UTF-8?B?Qm9va2luZy5jb20gLSBOaWV1d2UgYm9la2luZyEgKDUxMDAwMDAwMDQsIHZy?=\r\n"
        b" =?UTF-8?B?aWpkYWcgMjMganVsaSAyMDI3KQ==?=\r\n\r\n"
    )
    subject = _decode_subject(message_with_subject(raw))
    assert subject == (
        "Booking.com - Nieuwe boeking! (5100000004, vrijdag 23 juli 2027)"
    )


def test_missing_subject_is_empty_string():
    assert _decode_subject(message_with_subject(b"From: x@example.com\r\n\r\n")) == ""


def test_mailbox_names_are_quoted():
    assert _quote_mailbox("[Gmail]/Alle e-mail") == '"[Gmail]/Alle e-mail"'
    assert _quote_mailbox("INBOX") == '"INBOX"'
    # Already-quoted names are left alone rather than double-quoted.
    assert _quote_mailbox('"x"') == '"x"'


def test_list_row_yields_name_and_flags():
    row = b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle e-mail"'
    name, flags = _parse_list_row(row)
    assert name == "[Gmail]/Alle e-mail"
    assert "\\All" in flags


def test_list_row_for_a_user_label():
    row = b'(\\HasNoChildren) "/" "Geannuleerde boeking"'
    name, flags = _parse_list_row(row)
    assert name == "Geannuleerde boeking"
    assert "\\All" not in flags
