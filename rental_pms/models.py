"""Core data types shared across the pipeline."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional


class EventType(str, Enum):
    """The kind of change a Booking.com notification email announces."""

    NEW = "new"
    MODIFIED = "modified"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ReservationEvent:
    """One Booking.com notification, parsed.

    ``arrival_date`` comes from the subject line and is verified to be the
    guest's arrival date, not the send date -- see docs/subject-line-analysis.md.
    ``departure_date`` is left empty here; it is filled in later by the iCal
    matcher, which looks up the stay by arrival date.
    """

    event_type: EventType
    reservation_number: str
    arrival_date: date
    subject: str
    # Only cancellation emails carry the guest name in the body.
    guest_name: Optional[str] = None
    # Message timestamp, used for ordering when several events touch one booking.
    sent_at: Optional[date] = None
    # False when the weekday named in the subject disagrees with the parsed date.
    weekday_matches: bool = True
    departure_date: Optional[date] = None
