"""Parse the daily "arriving today/tomorrow" digest to backfill guest names.

NOT IMPLEMENTED YET -- step 5.

Only cancellation emails name the guest; new and modified bookings do not. The
daily digest from noreply-email@booking.com does, in a table like::

    Reservering Naam gast Aankomst Vertrek
    5100000008 Margot Vermeulen 14 aug 2026 16 aug 2026

Note two things when building this. The subject is English while the body is
Dutch, so select on sender, not subject language. And the digest only covers
arrivals within a day, so a name only lands in Notion right before the stay --
if names are wanted earlier, the guest-message emails from
<reservation>-<token>@guest.booking.com carry both the reservation number and
the guest name, and arrive much sooner.

The abbreviated month form here ("14 aug 2026") differs from the subject line's
full form ("14 augustus 2026") and needs its own lookup table.
"""

from typing import Dict


class DigestParser:
    def parse(self, body: str) -> Dict[str, dict]:
        """Map reservation number -> {guest_name, arrival, departure}."""
        raise NotImplementedError("step 5")
