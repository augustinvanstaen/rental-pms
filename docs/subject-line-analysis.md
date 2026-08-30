# Is the date in the subject line the arrival date?

> **Anonymised.** This repository is public, so guest names, reservation
> numbers, iCal UIDs and the Booking.com partner ID have all been replaced with
> fictional stand-ins throughout the docs and tests. The replacement is
> consistent, so every cross-reference below still lines up. Dates, subject
> wording and message structure are unchanged — those are what the findings rest
> on. **Do not paste real reservation data into this repository.**

**Yes. It is the guest's arrival date, not the send date.** Verified against the
live mailbox on 2026-08-30. Three independent lines of evidence agree.

## 1. Cross-check against the daily digest

The "Nieuwe boeking" email for reservation `5100000008` was **sent 10 Aug 2026**
with the subject:

> Booking.com - Nieuwe boeking! (5100000008, vrijdag 14 augustus 2026)

The daily arrivals digest **sent 13 Aug 2026** lists that same reservation:

> Komt morgen aan: 14 aug 2026
> 5100000008 | Margot Vermeulen | Aankomst 14 aug 2026 | Vertrek 16 aug 2026

The subject's date matches the digest's *Aankomst* (arrival), and is four days
after the send date. Conclusive on its own.

## 2. Subject dates run far ahead of send dates

An email sent 17 Aug 2026 carries the subject date `vrijdag 23 juli 2027` —
eleven months in the future. A send date cannot be in the future.

## 3. The date moves when a booking is modified

Reservation `5100000006`:

| Sent | Subject date |
|---|---|
| 23 May 2026 | `maandag 13 juli 2026` |
| 25 Jun 2026 | `zondag 12 juli 2026` |

The date changed by one day when the guest modified the booking, while the send
date advanced by a month. It tracks the stay, not the message.

As a fourth, weaker check: every weekday name in all 14 sampled subjects agrees
with the calendar date it precedes, so these are genuine calendar dates and the
Dutch month table is correct. The test suite asserts this.

## The bodies contain no dates at all

Worth stating plainly, because it constrains the design: the body of a **new
booking** email is boilerplate plus a reservation number and an extranet link.
No arrival date, no departure date, no guest name. Same for **modification**
emails. So the subject line is the *only* source of the arrival date — subject
parsing is not a shortcut here, it is the only option.

Cancellation bodies are the exception — but only some of them. See below.

## Cancellation emails come in two shapes, and one has no guest name

**Standard cancellation** — names the guest:

> Reserveringsnummer 6100000003 voor Élodie Devriendt is geannuleerd.

**Grace-period cancellation** — cancelled inside Booking.com's 24-hour
*Bedenkperiode*, and names nobody:

> In reactie op de annulering van reservering 6100000009 bevestigen wij hierbij
> dat de annuleringskosten voor de gast nu € 0 bedragen.

Two of the ten cancellations in the last 120 days are this second shape
(reservations `6100000009` and `6100000007`). No regex can recover a name from
them — it is not in the email. `is_grace_period_cancellation()` identifies them
so step 5 can go looking elsewhere rather than logging a false warning.

## Four subject formats, not three

The last-minute variant breaks the pattern the other three share — no
exclamation mark, and it does not use the word "boeking":

```
Booking.com - Nieuwe boeking! (5100000008, vrijdag 14 augustus 2026)
Booking.com - Gewijzigde boeking! (5100000008, vrijdag 14 augustus 2026)
Booking.com - Geannuleerde boeking! (6100000003, vrijdag 14 augustus 2026)
Booking.com - Nieuwe last-minutereservering (6100000006, zondag 16 augustus 2026)
```

The parser therefore classifies on the stem (`nieuw` / `gewijzigd` /
`geannuleerd`) rather than on the full phrase.

## How notifications are selected

Two modes, set with `IMAP_SELECTION`.

**`subject` (default)** — searches All Mail for subjects beginning
`Booking.com -`. Every notification subject starts with that, whatever the event
type.

**`labels`** — reads the three Gmail labels the mailbox's filters apply, one
IMAP folder each. Useful for curating scope by hand: unlabel a test booking and
it drops out of the run.

### Why subject is the default

A Gmail filter is mutable configuration that lives outside this repo. It cannot
be reviewed in a diff or tested in CI, and when it breaks it fails *silently* —
the run reports fewer bookings rather than an error. Two of the three filters
here were correct all along; the cancellation one was not, and nothing surfaced
that until the mailbox was measured.

Filters also never apply retroactively, which is not a bug to fix but a
permanent property. Measured over 120 days:

| Selection | Notifications found |
|---|---|
| `subject` | 37 |
| `labels` | 26 |

The 11-message gap is mail that arrived before its filter existed or matched;
eight are cancellations. Fixing the filter does nothing for them. Subject
matching has no backfill concept at all — widen the lookback and history simply
works.

The asymmetry decides it: a missed cancellation leaves a booking in Notion that
no longer exists, so the calendar shows the studio occupied when it is free.

### Auditing label coverage

`--audit-labels` reports notifications that no label covers, and prints the
Gmail searches that close the gap:

```
11 notification(s) carry NO label and will be missed:
  Sat, 2 May 2026   Booking.com - Nieuwe boeking! (6100000005, donderdag 14 mei 2026)
  Mon, 4 May 2026   Booking.com - Geannuleerde boeking! (6100000008, vrijdag 22 mei 2026)
  ...
```

It exits non-zero when it finds anything. Only relevant if you switch to
`labels`; under the default it is a diagnostic for the Gmail filters themselves.

### A gotcha in the audit

Gmail's IMAP `SUBJECT` search matches **tokens, not literal substrings**, so
`"Booking.com -"` also returns invoices, sign-in alerts and verification codes —
anything containing "Booking.com". The trailing `-` is ignored. Both the audit
and subject selection filter results through `parse_subject()` for this reason;
without it the audit reports 18 messages instead of 11.

## Booking.com sends duplicates

The modification for `5100000008` arrived **twice**, as two separate messages
with the same timestamp and identical subjects. Notion writes must be
idempotent, keyed on the reservation number.

## Other things in the mailbox that are not notifications

The parser rejects these by design; they are noise from the same sender or a
sibling one:

- `Booking.com Invoice 1100000001` — monthly invoices
- `Wij hebben dit bericht ontvangen van <name>` — guest messages, from
  `<reservation>-<token>@guest.booking.com`. These carry both the reservation
  number ("Bevestigingsnummer: 5100000008") and the guest name, and arrive
  earlier than the digest, but they only cover 61% of reservations. Measured and
  rejected as a backfill source below.
- `Reservations with today's or tomorrow's arrival date for ...` — the daily
  digest. English subject, Dutch body.


## Gmail's folder names are localised

This account's interface language is Dutch, so IMAP exposes All Mail as
`[Gmail]/Alle e-mail`, not `[Gmail]/All Mail`. Selecting the English name fails
with `BAD Could not parse command`. `imap_client.py` therefore finds the folder
by its `\\All` special-use flag, which is language-independent, rather than
hardcoding either spelling.

Two related IMAP gotchas, both hit while building this:

- `imaplib` does not quote mailbox names. Gmail's contain spaces and brackets,
  so they must be quoted explicitly.
- `imaplib` does not quote search criteria either. `SUBJECT "Booking.com -"`
  contains a space, so it must be quoted explicitly or the server replies BAD.

Selection uses standard IMAP `SEARCH SINCE <date> SUBJECT "Booking.com -"` —
every notification subject starts with that prefix, whatever the event type.
Cross-checked against selecting by sender instead: both return the same 37
notifications over 120 days.

## Live run, 120-day lookback

37 notifications parsed: 24 new, 4 modified, 9 cancelled. Guest names recovered
for 5 of the 9 cancellations — the other 4 being grace-period shapes or
otherwise nameless. Reservation `5100000008` produced two identical
`modified` events, the duplicate noted above.


## Guest-name sources: measured coverage

The guest name is missing from every new and modified booking email, so it has
to be backfilled. Three candidate sources, measured over the same 120 days:

**Guest-message emails** (`<reservation>-<token>@guest.booking.com`) — rejected.
They carry the reservation number and the guest name and arrive early, but only
**17 of 28 reservations (61%)** ever sent a message. Eleven guests never wrote
at all. Not a backfill source; at best an opportunistic supplement.

**The daily digest** — reliable, but late and incomplete in one specific way.
It lists reservation, guest name, arrival and departure, so it is the primary
source. But it only covers arrivals *today or tomorrow*, which means **a booking
cancelled before its arrival date never appears in any digest.**

**The iCal feed** — carries no names at all. See below.

### The residual gap

A reservation that is cancelled well before arrival, via the grace-period
cancellation shape, has no guest name in *any* email:

| Reservation | Booked | Arrival | Cancelled | Messaged us? | Name available |
|---|---|---|---|---|---|
| `6100000009` | 13 May | 12 Jul | 13 May | yes | only via guest message |
| `6100000007` | 10 May | 9 Jun | 10 May | no | **nowhere** |

Both were cancelled a month or more before arrival, so neither ever reached a
digest. `6100000007`'s guest name is unrecoverable from email.

Whether that matters is a product decision: a booking cancelled six weeks out
may not need a guest name in Notion at all.

## The iCal feed does not identify reservations

Fetched and inspected on 2026-08-30. The whole feed is three VEVENTs, all
structurally identical:

| DTSTART | DTEND | Nights | What it is |
|---|---|---|---|
| 2026-09-01 | 2026-09-15 | 14 | reservation `6100000001` |
| 2027-07-23 | 2027-08-06 | 14 | reservation `5100000004` |
| 2027-08-31 | 2028-02-29 | **182** | a manual closure — since removed, see below |

```
BEGIN:VEVENT
DTSTAMP:20260830T123812Z
DTSTART;VALUE=DATE:20260901
DTEND;VALUE=DATE:20260915
UID:aaaa1111bbbb2222cccc3333dddd4444@booking.com
SUMMARY:CLOSED - Not available
ORGANIZER:mailto:noreply@booking.com
END:VEVENT
```

Four consequences, all of which shape `ical_matcher.py`:

1. **No guest name, no reservation number.** `SUMMARY` is the constant string
   `CLOSED - Not available` for every event and `UID` is an opaque hash. Matching
   can only be on `DTSTART` versus the arrival date from the email.
2. **The feed is availability, not reservations.** That 182-night event was a
   manual closure — and, per the owner, a mistaken one, since corrected in the
   Booking.com admin portal. Re-fetching afterwards returned two events, both
   real bookings, confirming the feed reflects admin changes promptly. The
   matcher still never enumerates events as bookings: it only queries a date
   already known to be an arrival, and flags an exact match longer than 60
   nights as a probable block rather than a stay. Nothing currently trips that,
   but a deliberate seasonal closure is plausible and the check is cheap.
3. **Contiguous stays may be merged.** Booking.com publishes blocked ranges, and
   same-day turnover has happened here (`5100000008` departed 16 Aug,
   `6100000006` arrived 16 Aug). If such stays merge into one event, the event's
   end is the end of the whole run, not the first guest's departure. So only an
   *exact* `DTSTART` match yields a departure date; an arrival landing strictly
   inside an event is reported ambiguous and left empty. Unverified — no two
   future bookings are currently adjacent, so there is nothing to test it against.
4. **Only current and future dates are present.** Over a 400-day lookback, 84
   notifications parse and just 2 receive a departure date. Past stays cannot be
   enriched from iCal at all.

`DTSTART` is confirmed as the arrival date: `20260901` matches reservation
`6100000001`, whose subject line says 1 September 2026, and `20270723` matches
`5100000004` at 23 July 2027.

`DTEND` is **confirmed** to be the departure date, checked by the owner against
both reservations in the feed:

| Reservation | DTSTART | DTEND | Actual departure |
|---|---|---|---|
| `6100000001` | 2026-09-01 | 2026-09-15 | 15 September |
| `5100000004` | 2027-07-23 | 2027-08-06 | 6 August |

Both match `DTEND` exactly, so a DATE-valued `DTEND` is the guest's checkout day
rather than their last night — the standard exclusive-end reading. This was the
last unverified assumption in the matcher. It remains isolated to the constant
`DTEND_IS_DEPARTURE` in `ical_matcher.py`.

The one assumption still untested is whether contiguous stays merge into a
single event (point 3 above). It needs two adjacent future bookings to appear in
the feed, which has not happened yet. Until then the matcher reports such cases
as ambiguous rather than guessing.


## Subject headers are folded, and the fold is not semantic

Long subjects wrap across lines in the raw header:

```
Subject: Booking.com - Nieuwe boeking! (5100000005, woensdag 10 september\r\n 2025)
```

Python's `make_header(decode_header(...))` preserves that CRLF. The parser never
noticed, because its regexes use `\s+` — but storing the raw subject in Notion
did: Notion normalises CRLF to LF, so the value read back never equalled the
value written, and five pages were rewritten on **every** run. The sync reported
"5 updated" forever instead of converging.

`_decode_subject()` now collapses all whitespace runs to single spaces, which is
correct per RFC 5322 — folding whitespace carries no meaning. After the fix a
repeat run reports 0 created, 0 updated, 57 unchanged.

Worth remembering as a general shape: anything used as a change-detection key
has to be normalised on the way in, or the first system that normalises it
differently turns every run into a write.
