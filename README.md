# Domora

Property management, reimagined. Domora by DMBT Group: a modern platform for smarter condominium management.

Source: https://github.com/dmbtgroup/Domora

A web app for running a condominium. Managers use it to look after units, residents, billing and maintenance. Owners and tenants use it to check their account, report problems, read notices and book amenities.

Built with Flask, SQLAlchemy and SQLite. It renders pages on the server and needs no JavaScript build step.

## Features

| Area | Board (managers) | Owners / tenants |
|---|---|---|
| **Dashboard** | Occupancy, outstanding dues, units that owe money, active maintenance, upcoming bookings | Their unit's balance, their requests and bookings, latest notices |
| **Units** | Create, edit and delete units (floor, bedrooms, area, parking, monthly dues) | — |
| **Directory** | One menu with four tabs. **Board**: the syndicate's board of directors (President, Vice-president, Treasurer, Secretary, Accounting, Directors), with vacant positions shown and "email the board". **Owners**: owners, co-owners and other contacts. **Tenants**: tenants. **Accounts**: people the syndicate hires (cleaners, contractors, an outside accountant), who aren't owners or tenants, each given only the access they need: repairs, accounting (view or record), directory, documents, notices. Owners' and tenants' logins are created and managed from the Owners and Tenants tabs. Accounts are **managed by the board only**. A board member is an account whose email matches a person holding a board position; until one exists, managers can set accounts up. Board positions are assigned in the Board tab. Each tab has search, "email all" (BCC) and CSV export. Owners and tenants can have several people per unit, each with email, phone, mailing address (copyable from another person) and a primary-contact flag. | Edit their own phone number and password |
| **Accounting** | Year or month view with separate **Payments** and **Costs** panels, totals, net and a month-by-month table; costs by category (utilities, insurance, maintenance, repairs, administration); add costs and refunds by hand. Bill monthly dues for every unit in one step (running it twice for the same month bills nobody twice), add special assessments or fines, record payments, see a statement with a running balance | Read their own unit's statement |
| **Fees & contributions** (in Accounting) | **Condo fees**: set each period's start and end date and enter either the annual total or the monthly total (the other is calculated); two months before the end date the board is notified to renew; it is split between units by ownership share (largest-remainder rounding, so the parts add up to the cent), with optional fixed amounts per unit and a comparison with the previous year; "Apply" sets every unit's monthly fee. **Special contributions**: a title, total, deadline and split (by share, equally, or amounts per unit); bill it to create a charge per unit and notify owners; record payments against it and follow each unit's status (paid, partly paid, not paid, overdue); moving the deadline notifies owners | See their unit's annual fee and their special contributions with amount, paid, left and deadline on their accounting page |
| **Projects & Maintenance** | One menu with two tabs. **Projects**: planned building work (roof, windows, renovations) with an optional budget and target date, added by the board and visible to every resident; any request can be moved between the tabs. **Maintenance**: triage requests by status and priority, comment on them, record an owner's approval given by phone or email | Submit requests for their unit or a common area, comment, close or reopen their own. **A tenant's request for their unit waits for the owner's approval** (owner accounts approve or decline from their dashboard); common-area requests don't. |
| **Notifications** | A bell with an unread count; each person sees only what concerns them: repairs to approve, decisions on their requests, status changes and comments, quotes, reimbursements and new notices | Same |
| **Quotes** | The board can ask an owner to get a quote for a repair; the owner uploads it, the board approves the cost (or asks for another quote), and the owner then does the repair and requests a reimbursement | Upload quotes; request reimbursement once approved |
| **Reimbursements** | The President approves or declines; Accounting (or the Treasurer) then records it as a credit on the unit or a payback. Managers stand in while no one in those positions has an account | Owners request with receipts, choosing a credit on their next payment or a payback |
| **Insurance** | One home-insurance policy per household: the unit's owners share one and its tenants share one, each naming a policyholder, with certificate and start and end dates; renewals keep history; units are flagged when insurance is missing, expired or expiring within 30 days | Owners upload policies for anyone in their unit (including their tenant); tenants upload their own |
| **Documents** | Upload syndicate documents (declaration, by-laws), building insurance (with policy dates) and annual meeting minutes; choose who sees each (everyone, owners, managers) | View and download what they're allowed to see; the current building insurance is shown on every dashboard |
| **Votes** | Draft a vote (question, choices, audience, deadline in Montreal time); nobody is notified until it is published. Audience: all owners, owners of selected units, or the board. Counting: by ownership share (a unit's owners split its share, so two owners count half each), one vote per unit, or one per person. The board sees live results and every ballot; reminders go out a day before the deadline; the board can close early or delete | Vote and change their vote until the deadline; see the results once it has passed (never who voted what) |
| **Notices** | Post, pin, edit and delete notices | Read them |
| **Amenities** | Set up amenities with opening hours and a maximum booking length | Book time slots (overlaps, past times, out-of-hours and over-long bookings are refused) and cancel their own |

Money is stored in integer cents. Uploaded files (up to 4 MB) are stored in the database and only served to people allowed to see them. Every form has CSRF protection, and after login the app only redirects to addresses on its own site.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Load demo data (16 units, residents, dues, requests, bookings)
.venv/bin/flask --app condo seed
.venv/bin/flask --app condo run
```

Managing the building comes from holding a board position (Directory → Board): an account whose email matches a board member gets full management access. Accounts themselves are Owner or Tenant. The first account created from `ADMIN_EMAIL` is a temporary administrator; once its person is added in the Directory with that email and given a board position, it becomes their own account.

Then open http://127.0.0.1:5000 and sign in with a demo account:

- Manager: `admin@example.com` / `admin1234`
- Resident: `alice@example.com` / `resident123`

### Starting with an empty database

```bash
.venv/bin/flask --app condo init-db
.venv/bin/flask --app condo create-admin
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | `dev-change-me` | Session signing key. **Set this in production.** |
| `DATABASE_URL` | `sqlite:///instance/condo.db` | Any SQLAlchemy database URL |
| `CONDO_NAME` | `Maple Court Condominiums` | Name shown in the header |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME` | — | When no manager account exists yet, one is created from these at start-up |
| `CRON_SECRET` | — | Bearer token Vercel Cron sends to `/sheet/cron` |
| `AUTO_INIT_DB` | on when running on Vercel | Create tables (and the first manager) at start-up |

`postgres://` and `postgresql://` URLs, including the `POSTGRES_URL` that the Neon integration sets, are used automatically with the psycopg 3 driver.

## Google Sheet sync

Managers can link the condo's transaction log in Google Sheets under **Billing → Google Sheet**. The sheet needs a **Transactions** tab with the columns Date, Unit, Owner, Type, Amount, Approved by, Source and Notes. An optional **Balances** tab (Unit, Owner, …, Current balance (owes)) is used to cross-check the totals.

| Type in the sheet | Loaded as |
|---|---|
| Opening balance | opening-balance charge (negative = credit) |
| Monthly fee | that month's dues |
| Fee payment, Special contribution | payment |
| Purchase (reimbursement), with a unit | purchase credit to that unit |
| Purchase, no unit | building expense |

**Owner contacts:** an optional **Owners** tab lists one person per row, with the columns Unit, Name, Email, Phone, Mailing address, Role and Primary. Role is Owner, Co-owner, Tenant or Contact. An optional **Board role** column takes positions such as `President, Treasurer` (French names such as `Trésorier` and `Comptable` also work). When the tab exists, its people replace the contacts from the previous sync; contacts added in the app are kept.

To reuse another owner's address, write `Same as <name>` (or `= <name>`) in the Mailing address cell. A match in the same unit is used first, then one anywhere in the sheet. On the website, the owner form's **Copy address from** list does the same.

How a sync works:
- A sync is previewed and confirmed before anything is written.
- Each sync replaces every entry that came from the sheet. Entries typed into the app by hand are kept.
- Entries that came from the sheet can't be removed in the app; change them in the sheet.

**Automatic sync:** once a sync has been confirmed, a manager can turn this on. Vercel Cron (`vercel.json`) calls `/sheet/cron` every 10 minutes, authenticated with `CRON_SECRET`. The app re-syncs only when the sheet's contents have changed.

The website reads the sheet through its `.xlsx` export, so the sheet must be shared as *Anyone with the link → Viewer*. You can also preview a sync from a downloaded copy of the sheet.

## English and French

Every page, message and notification is available in English and French (Quebec usage). People switch with the
**EN | FR** button at the top of each page (also on the sign-in page). The choice is saved on their account; before
they choose, the browser's language is used. Notifications are written in each recipient's own language, and money,
dates and percentages follow the language (`$34,691.00` / `34 691,00 $`, `Nov 6, 2026` / `6 nov. 2026`).
Amounts can be typed either way (`1,234.50` or `1 234,50`).

Texts are marked with `_()` / `{% trans %}`; the French is in `condo/translations/fr/LC_MESSAGES/messages.po`.
After adding or changing texts:

```bash
pybabel extract -F babel.cfg -k _l -k lazy_gettext --no-location --sort-output -o condo/translations/messages.pot condo
pybabel update -i condo/translations/messages.pot -d condo/translations --no-fuzzy-matching
# translate the new entries in messages.po, then:
pybabel compile -d condo/translations
```

The compiled `messages.mo` is committed, so the server needs no build step.

## Deploying to Vercel

`app.py` exposes the Flask app; Vercel detects it and runs it with its Flask runtime.

1. Import the GitHub repository as a Vercel project.
2. Under **Storage**, connect a Neon Postgres database to the project. This sets `DATABASE_URL`. Without a database the site still runs, but on a temporary SQLite file in `/tmp` that gets wiped, and a warning banner is shown.
3. Set `SECRET_KEY`, `CONDO_NAME`, `ADMIN_EMAIL` and `ADMIN_PASSWORD`, then redeploy.
4. Add the custom domain. If its DNS is hosted elsewhere, create a `CNAME` record pointing to `cname.vercel-dns.com`.
5. Sign in with the admin account and change its password under **My profile**.

## Tests

To run the suite against Postgres, set `TEST_DATABASE_URL=postgresql+psycopg://…`.


```bash
.venv/bin/pytest
```

## Project layout

```
condo/
  __init__.py      app factory, error handlers
  models.py        User, Unit, Charge, Payment, MaintenanceRequest, Announcement, Amenity, Booking
  cli.py           init-db, create-admin, seed
  views/           one blueprint per area
  templates/       Jinja templates
  static/style.css
tests/
```
