# rental-pms

Reads Booking.com notification emails from Gmail, extracts reservation details,
and keeps a Notion database in sync. Runs on a schedule in GitHub Actions.

Single property. The notification emails are in Dutch.

Guest names, reservation numbers and account identifiers in the tests and docs
are anonymised — this repo is public. Don't commit real reservation data.

**Nor log it.** Actions logs on a public repo are world-readable, so the default
run output is aggregate counts only:

```
7 notification(s): 1 cancelled, 2 modified, 4 new.
0 with a departure date, 1 with a guest name.
```

The per-reservation table needs `--details`, and every log line naming a
reservation or guest is at DEBUG, below the default level. The workflow passes
neither `--details` nor `--verbose`. `tests/test_report_privacy.py` asserts all
of this, because it was got wrong once already: the first Actions run published
real guest names to a public log.

## Status

| Step | Module | State |
|---|---|---|
| 1. Project scaffold | — | done |
| 2. Config & secrets | `config.py` | done |
| 3. Subject-line parser | `email_parser.py` | done, verified against the live mailbox |
| 4. Guest name from cancellations | `email_parser.py` | done |
| 5. iCal matching | `ical_matcher.py` | done, departure dates confirmed by owner |
| 5. Daily-digest parser | `digest_parser.py` | stub |
| 5. Notion writer | `notion_writer.py` | done, backfilled 57 reservations |

`digest_parser.py` is the one remaining stub; it raises `NotImplementedError`
and carries design notes in its docstring.

The database was backfilled with 57 reservations over a 400-day lookback, and
re-running is a verified no-op (0 created, 0 updated, 57 unchanged).

### A limit worth knowing about the iCal feed

It carries only current and future dates. Over a 400-day lookback, 84
notifications parse but only **2** get a departure date — the two stays still in
the future. Departure dates for past stays are not recoverable from iCal at all;
the daily digest is the only source that prints them, and only for stays that
happened while digests were arriving.

## The verified assumption

The date in the subject line is the **guest's arrival date**, not the send date.
This was the riskiest assumption in the design, and it holds — proven three
ways against real mail in [docs/subject-line-analysis.md](docs/subject-line-analysis.md).

That document is worth reading before touching the parser. It also covers why
the Gmail labels must not be used for filtering, the two different cancellation
body shapes, and the duplicate emails Booking.com sends.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # then fill it in
```

`GMAIL_APP_PASSWORD` is a Google [App Password](https://myaccount.google.com/apppasswords),
not the account password; two-factor auth must be on for that page to appear.

## Usage

Parse the mailbox and print what was found, without writing anywhere:

```bash
.venv/bin/python -m rental_pms.main --parse-only --lookback-days 120
```

Check that no notification is falling through the labels:

```bash
.venv/bin/python -m rental_pms.main --audit-labels --lookback-days 120
```

See what would be written to Notion without writing anything:

```bash
.venv/bin/python -m rental_pms.main --dry-run
```

Run the tests:

```bash
.venv/bin/python -m pytest tests/ -q
```

## Configuration

Set in `.env` locally, or as GitHub Actions repository secrets under the same
names. Real environment variables always win over `.env`.

| Variable | Required | Notes |
|---|---|---|
| `GMAIL_ADDRESS` | yes | Mailbox receiving the notifications |
| `GMAIL_APP_PASSWORD` | yes | 16-character App Password |
| `NOTION_API_TOKEN` | yes | Integration must be shared with the database |
| `NOTION_DATABASE_ID` | yes | |
| `ICAL_URL` | yes | Booking.com calendar export |
| `IMAP_SELECTION` | no | `subject` (default) or `labels` |
| `IMAP_LABELS` | no | Comma-separated; only used in `labels` mode |
| `IMAP_FOLDER` | no | Defaults to auto-discovering All Mail |
| `LOOKBACK_DAYS` | no | Default 30 |
| `DRY_RUN` | no | |

`.env` is git-ignored. `Config.redacted()` is what gets logged, so secrets stay
out of CI output.

## Scheduling

`.github/workflows/sync.yml` runs daily at 06:15 UTC and can be triggered
manually with a custom lookback and a parse-only toggle. The five secrets above
must be set in the repository's Actions secrets.

Note the cron is UTC, so the local run time shifts by an hour across DST.

## Reading the mailbox

Selection defaults to matching the **subject line** — every notification subject
begins `Booking.com -`. Set `IMAP_SELECTION=labels` to read the Gmail filter
labels instead (`IMAP_LABELS` overrides which ones).

Subject is the default because a Gmail filter is mutable config outside this
repo: untestable in CI, and it fails silently — a filter that stops matching
produces a smaller run, not an error. Filters also never apply retroactively, so
label mode cannot see mail that predates a filter change. Over the last 120 days
that gap was 11 notifications, 8 of them cancellations, and a missed
cancellation is the expensive one — it leaves a stale booking in Notion.

If you do use label mode, audit the gap after any filter change:

```bash
.venv/bin/python -m rental_pms.main --audit-labels --lookback-days 120
```

It lists what no label covers, prints the Gmail searches that fix it, and exits
non-zero when it finds anything.

The IMAP session is opened read-only; the script never marks, moves, or deletes
mail.


## What the Notion writer will and will not touch

Keyed on the reservation number, which is the database's title property.

**Written by the script:** `Reservation #`, `Booking status`, `Arrival`,
`Departure`, `Guest name`, `Email received`, `Raw source`.

**Never written:** `Cleaning status`, `Cleaning notes`, `Amount paid`. These are
yours, and the "Action items" view depends on them. `Nights` is a formula and is
read-only by definition. A test asserts none of these ever reach a write
payload.

Two further rules, both about not destroying information:

- **An absent value never clears an existing one.** New and modified booking
  emails carry no guest name, so writing an empty one over a name the digest
  filled in would lose it. Same for departure dates.
- **A cancellation updates `Booking status`, it does not delete the page**, so
  history and your cleaning notes survive.

Runs are idempotent. Events are folded per reservation in send order before any
write, so the three emails a booking typically generates become one page in its
final state, and a page is only patched when a field actually differs.
