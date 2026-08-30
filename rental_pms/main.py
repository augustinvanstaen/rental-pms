"""Entry point.

Today this runs the half of the pipeline that exists: read notification emails
over IMAP and parse them. iCal matching and Notion writing are step 5; until
they land, ``--parse-only`` is the whole useful run.
"""

import argparse
import logging
import sys
from email.utils import parsedate_to_datetime
from typing import List, Optional

from .config import ConfigError, load_config
from .email_parser import ParseError, parse_email, parse_subject
from .ical_matcher import IcalMatcher
from .imap_client import ImapClient
from .notion_writer import NotionWriter
from .models import EventType, ReservationEvent

logger = logging.getLogger("rental_pms")

# Which Gmail label each event type belongs under, and the subject phrase that
# selects it in the Gmail search box.
LABEL_FOR_EVENT = {
    EventType.NEW: "Nieuwe boeking",
    EventType.MODIFIED: "Gewijzigde boeking",
    EventType.CANCELLED: "Geannuleerde boeking",
}
SEARCH_PHRASE_FOR_LABEL = {
    "Nieuwe boeking": "Nieuwe",
    "Gewijzigde boeking": "Gewijzigde boeking",
    "Geannuleerde boeking": "Geannuleerde boeking",
}


def _parse_sent_at(date_header: Optional[str]):
    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header).date()
    except (TypeError, ValueError):
        return None


def collect_events(config) -> List[ReservationEvent]:
    """Read the mailbox and return every notification we could parse."""
    events = []
    skipped = 0

    with ImapClient(
        host=config.imap_host,
        port=config.imap_port,
        address=config.gmail_address,
        app_password=config.gmail_app_password,
        folder=config.imap_folder,
        labels=config.imap_labels,
        selection=config.selection,
    ) as client:
        for subject, body, date_header in client.iter_notifications(config.lookback_days):
            try:
                event = parse_email(subject, body, _parse_sent_at(date_header))
            except ParseError as exc:
                # Booking.com sends plenty of other mail (invoices, guest
                # messages). Those are expected to fail here, so this is a
                # debug-level skip, not a warning.
                logger.debug("skipping message: %s", exc)
                skipped += 1
                continue

            if not event.weekday_matches:
                logger.warning(
                    "weekday in subject disagrees with parsed date: %r", event.subject
                )
            events.append(event)

    logger.info("parsed %d notifications (%d other messages skipped)", len(events), skipped)
    return events


def report(events: List[ReservationEvent]) -> None:
    if not events:
        print("No Booking.com notifications found in the lookback window.")
        return

    header = "{:<12}  {:<10}  {:<12}  {:<12}  {}"
    print(header.format("RESERVATION", "EVENT", "ARRIVAL", "DEPARTURE", "GUEST"))
    for event in sorted(events, key=lambda e: (e.arrival_date, e.reservation_number)):
        print(header.format(
            event.reservation_number,
            event.event_type.value,
            event.arrival_date.isoformat(),
            event.departure_date.isoformat() if event.departure_date else "-",
            event.guest_name or "-",
        ))
    with_departure = sum(1 for e in events if e.departure_date)
    print("\n{} notification(s), {} with a departure date.".format(
        len(events), with_departure))


def audit(config) -> int:
    """Report notifications that the labels miss. Returns 1 if any were found."""
    with ImapClient(
        host=config.imap_host,
        port=config.imap_port,
        address=config.gmail_address,
        app_password=config.gmail_app_password,
        folder=config.imap_folder,
        labels=config.imap_labels,
    ) as client:
        candidates = client.audit_unlabelled(config.lookback_days)

    # Gmail's IMAP SUBJECT search is token-based, not a literal substring test,
    # so the trailing "-" in "Booking.com -" is ignored and invoices, sign-in
    # alerts and verification codes come back too. Keep only what actually
    # parses as a notification.
    missing = []
    for subject, date_header in candidates:
        try:
            parse_subject(subject)
        except ParseError:
            continue
        missing.append((subject, date_header))

    if not missing:
        print("All notification emails in the window carry a label.")
        return 0

    print("{} notification(s) carry NO label and will be missed:\n".format(len(missing)))
    for subject, date_header in missing:
        print("  {}  {}".format((date_header or "?")[:16], subject))

    # Group by event type so the fix can be given as one Gmail search per label.
    by_label = {}
    for subject, _ in missing:
        event_type, _, _, _ = parse_subject(subject)
        by_label.setdefault(LABEL_FOR_EVENT[event_type], []).append(subject)

    print("\nGmail filters only run on incoming mail, never retroactively, so")
    print("these need labelling by hand. In Gmail, run each search below, select")
    print("all results, and apply the matching label:\n")
    for label, subjects in sorted(by_label.items()):
        print('  {:<24} ({} message(s))'.format(label, len(subjects)))
        print('    search:  from:booking.com subject:"{}" -label:"{}"'.format(
            SEARCH_PHRASE_FOR_LABEL[label], label))
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sync Booking.com emails into Notion.")
    parser.add_argument(
        "--parse-only",
        action="store_true",
        help="read and parse emails, then print them. No iCal, no Notion writes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything except write to Notion; report what would change.",
    )
    parser.add_argument(
        "--audit-labels",
        action="store_true",
        help="report notification emails that no Gmail label covers, then exit.",
    )
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config()
    except ConfigError as exc:
        print("Configuration error: {}".format(exc), file=sys.stderr)
        return 2

    if args.lookback_days is not None:
        config = type(config)(**{**config.__dict__, "lookback_days": args.lookback_days})

    logger.info("config: %s", config.redacted())

    if args.audit_labels:
        return audit(config)

    events = collect_events(config)

    if not args.parse_only:
        # Fills in departure dates where the feed says so unambiguously; see
        # ical_matcher for why ambiguous matches are left empty rather than
        # guessed at.
        events = IcalMatcher(config.ical_url).enrich(events)

    report(events)

    if args.parse_only:
        return 0

    writer = NotionWriter(
        api_token=config.notion_api_token,
        database_id=config.notion_database_id,
        dry_run=config.dry_run or args.dry_run,
    )
    counts = writer.sync(events)

    print("\nNotion: {} created, {} updated, {} unchanged{}".format(
        counts["created"], counts["updated"], counts["unchanged"],
        "  (dry run -- nothing written)" if (config.dry_run or args.dry_run) else "",
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
