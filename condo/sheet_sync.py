"""Keep the app's ledger in step with the condo's Google Sheet transaction log.

The sheet is the source of truth for everything it contains. A sync rebuilds
every entry that came from the sheet (origin = "sheet"): opening balances,
monthly fees, payments, purchase credits and building expenses. Entries typed
into the app by hand are never touched.

Expected tabs:
  Transactions  Date | Unit | Owner | Type | Amount | Approved by ... | Source | Notes
  Balances      Unit | Owner | ... | Current balance (owes)      (optional; used as a cross-check)
  Owners        Unit | Name | Email | Phone | Mailing address | Role | Primary | Notes
                (optional; one row per person. When the tab exists, the owners it lists
                replace the ones from the previous sync; owners added in the app are kept.)

Transaction types:
  Opening balance            charge (may be negative: a credit)
  Monthly fee                monthly dues charge for that month
  Fee payment, Special contribution, any "... payment"   payment
  Purchase (reimbursement)   credit to the unit (owner bought something for the building)
  Purchase (no unit)         building expense
"""
import calendar
import hashlib
import io
import re
import urllib.error
import urllib.request
from datetime import date, datetime

from .extensions import db
from .models import OWNER_ROLES, Charge, guess_cost_category, allowed_positions, hand_over_policies, Expense, Owner, Payment, Setting, Unit, utcnow
from .i18n import gettext as _

ORIGIN = "sheet"
UNIT_RE = re.compile(r"^#?\s*(\d+)\s*$")
SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
FETCH_TIMEOUT = 20


class SyncError(ValueError):
    """The sheet couldn't be fetched or doesn't have the expected layout."""


# --- fetching ----------------------------------------------------------------

def export_url(sheet_url):
    """Turn any Google Sheets link into its .xlsx export URL."""
    m = SHEET_ID_RE.search(sheet_url or "")
    if not m:
        raise SyncError(_("That doesn't look like a Google Sheets link (…/spreadsheets/d/…)."))
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"


def fetch(sheet_url):
    req = urllib.request.Request(export_url(sheet_url), headers={"User-Agent": "condo-management-sync"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise SyncError(_("Google refused access to the sheet. In the sheet, click Share and set "
                            "General access to \"Anyone with the link\" (Viewer).")) from exc
        raise SyncError(_("Google returned an error (%(code)s) for the sheet.", code=exc.code)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SyncError(_("Couldn't reach Google Sheets. Try again in a minute.")) from exc
    if "html" in ctype or not data.startswith(b"PK"):
        raise SyncError(_("Google sent a sign-in page instead of the sheet. Set General access to "
                        "\"Anyone with the link\" (Viewer)."))
    return data


# --- parsing -----------------------------------------------------------------

def _cents(value):
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip() or 0
    return int(round(float(value) * 100))


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    return None


def _text(value):
    return str(value).strip() if value is not None else ""


def _unit(value):
    m = UNIT_RE.match(_text(value))
    return m.group(1) if m else None


def _header_index(header, *names):
    """Find a column by (prefix of) its header text, case-insensitively."""
    lowered = [_text(h).lower() for h in header]
    for name in names:
        for i, h in enumerate(lowered):
            if h == name or h.startswith(name):
                return i
    return None


def classify(kind, unit):
    k = kind.lower()
    if k.startswith("opening"):
        return "opening"
    if k.startswith("monthly fee") or k in ("fee", "condo fee", "dues"):
        return "dues"
    if "reimbursement" in k or ("purchase" in k and unit):
        return "credit"
    if "purchase" in k or "expense" in k:
        return "expense"
    if "payment" in k or "contribution" in k or k in ("deposit", "e-transfer"):
        return "payment"
    return None


def parse(data):
    """Parse the workbook bytes into a plan dict (JSON-serialisable)."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise SyncError(_("The sheet couldn't be read as a spreadsheet.")) from exc
    names = {n.lower(): n for n in wb.sheetnames}
    if "transactions" not in names:
        raise SyncError(_('The sheet has no "Transactions" tab.'))
    rows = [list(r) for r in wb[names["transactions"]].iter_rows(values_only=True)]
    balances_rows = ([list(r) for r in wb[names["balances"]].iter_rows(values_only=True)]
                     if "balances" in names else [])
    owner_rows = ([list(r) for r in wb[names["owners"]].iter_rows(values_only=True)]
                  if "owners" in names else None)
    wb.close()
    if not rows:
        raise SyncError(_('The "Transactions" tab is empty.'))

    header = rows[0]
    col = {key: _header_index(header, *names_) for key, names_ in {
        "date": ("date",), "unit": ("unit",), "owner": ("owner",), "type": ("type",),
        "amount": ("amount",), "approved": ("approved",), "source": ("source",), "notes": ("notes", "note"),
    }.items()}
    missing = [k for k in ("date", "unit", "type", "amount") if col[k] is None]
    if missing:
        raise SyncError(_('The "Transactions" tab is missing column(s): %(missing)s.', missing=", ".join(missing)))

    def get(row, key):
        i = col[key]
        return row[i] if i is not None and i < len(row) else None

    entries, warnings, owners = [], [], {}
    for n, row in enumerate(rows[1:], start=2):
        if not any(v not in (None, "") for v in row):
            continue
        when, unit, kind = _date(get(row, "date")), _unit(get(row, "unit")), _text(get(row, "type"))
        category = classify(kind, unit)
        if when is None or not kind:
            warnings.append(f"Row {n}: skipped (missing date or type).")
            continue
        if category is None:
            warnings.append(f'Row {n}: skipped, unknown type "{kind}".')
            continue
        if category != "expense" and unit is None:
            warnings.append(f'Row {n}: skipped, "{kind}" needs a unit.')
            continue
        try:
            amount = _cents(get(row, "amount"))
        except ValueError:
            warnings.append(f"Row {n}: skipped, amount isn't a number.")
            continue
        owner = _text(get(row, "owner"))
        if unit and owner and category in ("opening", "dues"):
            owners[unit] = owner  # the latest fee/opening row names the current owner
        entries.append({"row": n, "date": when.isoformat(), "unit": unit, "kind": kind, "category": category,
                        "amount_cents": amount, "owner": owner, "approved": _text(get(row, "approved")),
                        "source": _text(get(row, "source")), "notes": _text(get(row, "notes"))})

    expected = {}
    if balances_rows:
        bh = balances_rows[0]
        ui, oi = _header_index(bh, "unit"), _header_index(bh, "owner")
        bi = _header_index(bh, "current balance", "balance")
        for row in balances_rows[1:]:
            unit = _unit(row[ui]) if ui is not None and ui < len(row) else None
            if unit and bi is not None and bi < len(row) and row[bi] not in (None, ""):
                expected[unit] = _cents(row[bi])
                if oi is not None and oi < len(row) and _text(row[oi]):
                    owners[unit] = _text(row[oi])  # the Balances tab is the cleanest list of owners

    contacts = _parse_owners(owner_rows, warnings) if owner_rows is not None else None

    units = sorted({e["unit"] for e in entries if e["unit"]} | set(expected)
                   | {c["unit"] for c in contacts or []}, key=int)
    fees = {}
    for e in sorted(entries, key=lambda e: e["date"]):
        if e["category"] == "dues":
            fees[e["unit"]] = e["amount_cents"]

    computed = {u: 0 for u in units}
    for e in entries:
        if e["unit"]:
            sign = 1 if e["category"] in ("opening", "dues") else -1
            computed[e["unit"]] += sign * e["amount_cents"]
    for u, cents in expected.items():
        if computed.get(u) != cents:
            warnings.append(f"Unit {u}: transactions add up to {cents_str(computed.get(u, 0))} "
                            f"but the Balances tab says {cents_str(cents)}.")

    counts = {c: sum(1 for e in entries if e["category"] == c)
              for c in ("opening", "dues", "payment", "credit", "expense")}
    counts["owners"] = len(contacts) if contacts is not None else None
    return {"kind": "sheet", "hash": hashlib.sha256(data).hexdigest(), "entries": entries, "owners": contacts,
            "units": [{"number": u, "owner": owners.get(u, ""), "fee_cents": fees.get(u),
                       "balance_cents": computed[u], "sheet_balance_cents": expected.get(u)} for u in units],
            "counts": counts, "warnings": warnings}


TRUTHY = {"yes", "y", "true", "x", "1", "primary", "✓", "oui"}


def _owner_role(text):
    t = text.lower().replace(" ", "-")
    if t in OWNER_ROLES:
        return t
    if "tenant" in t or "locataire" in t:
        return "tenant"
    if "co" in t and "owner" in t:
        return "co-owner"
    if t and "owner" not in t and "propri" not in t:
        return "contact"
    return "owner"


BOARD_ALIASES = {"president": "president", "présidente": "president", "président": "president",
                 "vice-president": "vice-president", "vice president": "vice-president", "vp": "vice-president",
                 "treasurer": "treasurer", "treasury": "treasurer", "trésorier": "treasurer", "trésorière": "treasurer",
                 "secretary": "secretary", "secrétaire": "secretary", "accounting": "accounting",
                 "accountant": "accounting", "comptable": "accounting", "director": "director",
                 "board member": "director", "administrateur": "director", "administratrice": "director"}


def _board_positions(text, row, warnings):
    keys = []
    for part in re.split(r"[,;/&]| and ", text or ""):
        part = part.strip().lower()
        if not part:
            continue
        key = BOARD_ALIASES.get(part)
        if key:
            keys.append(key)
        else:
            warnings.append(f'Owners tab row {row}: unknown board role "{part}"; ignored.')
    return keys


def _parse_owners(rows, warnings):
    """One person per row. Returns a list of contact dicts."""
    if not rows:
        return []
    header = rows[0]
    col = {key: _header_index(header, *names_) for key, names_ in {
        "unit": ("unit",), "name": ("name", "owner"), "email": ("email", "e-mail", "courriel"),
        "phone": ("phone", "tel", "mobile", "cell"), "address": ("mailing address", "address", "adresse"),
        "role": ("role",), "primary": ("primary", "main contact"), "notes": ("notes", "note"),
        "board": ("board role", "board", "position"),
    }.items()}
    if col["unit"] is None or col["name"] is None:
        warnings.append('Owners tab skipped: it needs at least "Unit" and "Name" columns.')
        return []

    def get(row, key):
        i = col[key]
        return _text(row[i]) if i is not None and i < len(row) else ""

    contacts = []
    for n, row in enumerate(rows[1:], start=2):
        if not any(v not in (None, "") for v in row):
            continue
        unit, name = _unit(get(row, "unit")), get(row, "name")
        if not unit or not name:
            warnings.append(f"Owners tab row {n}: skipped (needs a unit and a name).")
            continue
        email = get(row, "email").lower()
        if email and "@" not in email:
            warnings.append(f"Owners tab row {n}: \"{email}\" isn't an email address; left blank.")
            email = ""
        contacts.append({"row": n, "unit": unit, "name": name, "email": email, "phone": get(row, "phone"),
                         "address": get(row, "address"), "role": _owner_role(get(row, "role")),
                         "primary": get(row, "primary").lower() in TRUTHY, "notes": get(row, "notes"),
                         "board": _board_positions(get(row, "board"), n, warnings) if col["board"] is not None else None})
    _resolve_same_as(contacts, warnings)
    return contacts


SAME_AS_RE = re.compile(r"^\s*(?:same\s+(?:address\s+)?as|idem|=)\s*(.+?)\s*$", re.IGNORECASE)


def _resolve_same_as(contacts, warnings):
    """Let an address cell say "Same as <name>" to reuse another owner's address.
    Names are matched in the same unit first, then anywhere in the sheet; chains are followed."""
    def find(name, unit):
        key = name.strip().lower()
        matches = [c for c in contacts if c["name"].strip().lower() == key]
        return next((c for c in matches if c["unit"] == unit), matches[0] if matches else None)

    for c in contacts:
        seen = set()
        target, m = c, SAME_AS_RE.match(c["address"])
        while m and id(target) not in seen:
            seen.add(id(target))
            target = find(m.group(1), target["unit"])
            if target is None:
                break
            m = SAME_AS_RE.match(target["address"])
        if not SAME_AS_RE.match(c["address"]):
            continue
        if target is None or m:
            warnings.append(f"Owners tab row {c['row']}: couldn't find an address for \"{c['address']}\"; left blank.")
            c["address"] = ""
        else:
            c["address"] = target["address"]


def cents_str(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


# --- applying ----------------------------------------------------------------

def _truncate(text, n):
    return text if len(text) <= n else text[: n - 1] + "…"


def apply(plan):
    """Replace all sheet-sourced entries with the plan's. Returns a summary dict."""
    existing = {u.number: u for u in db.session.scalars(db.select(Unit))}
    created = []
    for u in plan["units"]:
        unit = existing.get(u["number"])
        if unit is None:
            unit = Unit(number=u["number"], monthly_fee_cents=u["fee_cents"] or 0)
            db.session.add(unit)
            existing[u["number"]] = unit
            created.append(u["number"])
        if u["owner"]:
            unit.owner_name = _truncate(u["owner"], 200)
        if u["fee_cents"]:
            unit.monthly_fee_cents = u["fee_cents"]
    db.session.flush()

    for model in (Charge, Payment, Expense):
        db.session.execute(db.delete(model).where(model.origin == ORIGIN))
    db.session.flush()
    db.session.expire_all()  # bulk deletes bypass the session; drop stale relationship caches

    if plan.get("owners") is not None:
        _sync_owners(plan["owners"], existing)

    # Charges that were entered in the app for the same unit, kind and month would clash.
    taken = set(db.session.execute(
        db.select(Charge.unit_id, Charge.kind, Charge.period).where(Charge.period.is_not(None))).all())
    skipped = []
    for e in plan["entries"]:
        when = date.fromisoformat(e["date"])
        note = " — ".join(x for x in (e["kind"], e["notes"]) if x)
        row = {"tab": "Transactions", "row": e.get("row"), "date": e["date"], "type": e["kind"], "unit": e["unit"],
               "amount_cents": e["amount_cents"], "owner": e.get("owner") or None, "approved": e.get("approved") or None,
               "source": e.get("source") or None, "notes": e.get("notes") or None}
        if e["category"] == "expense":
            db.session.add(Expense(spent_on=when, amount_cents=e["amount_cents"], origin=ORIGIN,
                                   category=guess_cost_category(f"{e['notes']} {e['kind']}"),
                                   description=_truncate(e["notes"] or e["kind"], 255),
                                   approved_by=_truncate(e["approved"], 255) or None,
                                   source=_truncate(e["source"], 255) or None, source_data=row,
                                   notes=e["notes"] if e["notes"] and len(e["notes"]) > 255 else None))
            continue
        unit = existing[e["unit"]]
        if e["category"] in ("opening", "dues"):
            if e["amount_cents"] == 0:
                continue
            kind = "opening" if e["category"] == "opening" else "dues"
            period = when.strftime("%Y-%m")
            if (unit.id, kind, period) in taken:
                skipped.append(f"Row {e['row']}: unit {unit.number} already has a {kind} entry for {period}; skipped.")
                continue
            taken.add((unit.id, kind, period))
            db.session.add(Charge(unit=unit, kind=kind, period=period, due_date=when, origin=ORIGIN,
                                  amount_cents=e["amount_cents"], description=_truncate(note, 255)))
        else:
            method = "purchase credit" if e["category"] == "credit" else (
                "bank transfer" if "interac" in e["source"].lower() else "other")
            db.session.add(Payment(unit=unit, amount_cents=e["amount_cents"], paid_on=when, method=method,
                                   origin=ORIGIN, notes=_truncate(note, 255), source_data=row))
    db.session.flush()

    mismatches = []
    for u in plan["units"]:
        expected = u["sheet_balance_cents"]
        if expected is not None and existing[u["number"]].balance != expected:
            mismatches.append(f"Unit {u['number']}: the app shows {cents_str(existing[u['number']].balance)} "
                              f"but the sheet's Balances tab says {cents_str(expected)}.")
    summary = {"at": utcnow().isoformat(timespec="seconds"), "hash": plan["hash"], "counts": plan["counts"],
               "units_created": created, "skipped": skipped, "mismatches": mismatches,
               "warnings": plan["warnings"]}
    Setting.put("sheet_last_sync", summary)
    db.session.commit()
    return summary


def _sync_owners(contacts, units):
    """Update the sheet's people in place (matched by unit and name) so anything attached
    to them in the app, such as insurance policies, survives a sync. People no longer in
    the sheet are removed; people added in the app are never touched."""
    current = {(o.unit.number, o.name.strip().lower()): o
               for o in db.session.scalars(db.select(Owner).where(Owner.origin == ORIGIN))}
    keep, primary_set = set(), set()
    for c in contacts:
        key = (c["unit"], c["name"].strip().lower())
        if key in keep:
            continue  # duplicate row for the same person
        keep.add(key)
        owner = current.get(key)
        if owner is None:
            owner = Owner(unit=units[c["unit"]], origin=ORIGIN)
            db.session.add(owner)
        is_primary = c["primary"] and c["unit"] not in primary_set
        if is_primary:
            primary_set.add(c["unit"])
        owner.name = _truncate(c["name"], 200)
        owner.email = c["email"][:255] or None
        owner.phone = c["phone"][:60] or None
        owner.mailing_address = c["address"] or None
        owner.role = c["role"]
        owner.is_primary = is_primary
        owner.notes = c["notes"] or None
        if c.get("board") is not None:  # the sheet has a Board role column: it decides
            owner.positions = [p for p in c["board"] if p in allowed_positions(c["role"])]
        else:  # positions are assigned in the Directory; keep them unless the role no longer allows them
            owner.positions = [p for p in owner.positions if p in allowed_positions(c["role"])]
    db.session.flush()
    for key, owner in current.items():
        if key not in keep:
            if owner.policies:
                hand_over_policies(owner)
            db.session.delete(owner)


def config():
    return Setting.get("sheet", {}) or {}


def run_auto_sync():
    """Fetch and apply if auto-sync is on and the sheet changed. Returns a short status string."""
    cfg = config()
    if not cfg.get("url") or not cfg.get("auto"):
        return "auto-sync is off"
    data = fetch(cfg["url"])
    last = Setting.get("sheet_last_sync") or {}
    if last.get("hash") == hashlib.sha256(data).hexdigest():
        Setting.put("sheet_last_check", utcnow().isoformat(timespec="seconds"))
        db.session.commit()
        return "unchanged"
    apply(parse(data))
    return "synced"


def month_name(period):
    y, m = (int(x) for x in period.split("-"))
    return f"{calendar.month_name[m]} {y}"


def clashing_entries(plan):
    """Charges entered in the app (e.g. by the Excel import) that block the sheet's own
    opening balance or monthly fee for the same unit and month.

    The sheet is the book of record: once it gives a unit's opening balance, any opening balance
    entered in the app for that unit up to that month is superseded, even when the sheet's is $0
    (the sheet skips zero lines, so the old one would otherwise stay and be counted on top)."""
    wanted, opening_month = set(), {}
    for e in plan.get("entries", []):
        if e["category"] == "dues" and e["amount_cents"]:
            wanted.add((e["unit"], "dues", e["date"][:7]))
        elif e["category"] == "opening":
            opening_month[e["unit"]] = max(opening_month.get(e["unit"], ""), e["date"][:7])
    for u in plan.get("units", []):  # units the sheet lists with no opening line start from $0
        if u.get("sheet_balance_cents") is not None and opening_month:
            opening_month.setdefault(u["number"], min(opening_month.values()))
    if not wanted and not opening_month:
        return []
    rows = db.session.scalars(db.select(Charge).join(Unit).where(Charge.origin.is_(None), Charge.period.is_not(None),
                                                                Charge.kind.in_(("opening", "dues")))).all()
    return [c for c in rows if (c.unit.number, c.kind, c.period) in wanted
            or (c.kind == "opening" and c.period <= opening_month.get(c.unit.number, ""))]
