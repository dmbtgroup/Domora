from datetime import date, datetime, timezone

from flask_login import UserMixin
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager
from .i18n import lazy_gettext as _l


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


ROLES = ("admin", "owner", "tenant")
# Accounts are owners or tenants. Managing the building comes from holding a board position
# (Directory > Board). "admin" only remains for the first, temporary account until its person
# is in the Directory with a board position; it is then converted automatically.
ROLE_LABELS = {"owner": _l("Owner"), "tenant": _l("Tenant"), "staff": _l("Staff"), "admin": _l("Administrator (temporary)")}
ACCOUNT_ROLES = {"owner": _l("Owner"), "tenant": _l("Tenant"), "staff": _l("Staff")}
RESIDENT_ROLES = {"owner": _l("Owner"), "tenant": _l("Tenant")}

# What a hired staff account (not an owner or tenant) may be given access to.
STAFF_PERMISSIONS = {
    "maintenance": _l("Repairs: see and update all repair requests"),
    "accounting": _l("Accounting: view all units, payments and costs"),
    "accounting_edit": _l("Accounting: record payments, charges and costs"),
    "directory": _l("Directory: see owners' and tenants' contact details"),
    "documents": _l("Documents: see all documents, including managers-only ones"),
    "notices": _l("Notices: post and edit notices"),
}


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(40))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="owner")
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"))
    active = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    language = db.Column(db.String(5))  # 'en' or 'fr'; None = follow the browser
    job_title = db.Column(db.String(120))      # staff: e.g. "Cleaning company"
    permissions = db.Column(db.String(300))    # staff: comma-separated STAFF_PERMISSIONS keys
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit", back_populates="residents")

    @property
    def is_staff(self):
        return self.role == "staff"

    @property
    def permission_keys(self):
        keys = {k.strip() for k in (self.permissions or "").split(",")}
        return [k for k in STAFF_PERMISSIONS if k in keys]

    @permission_keys.setter
    def permission_keys(self, keys):
        self.permissions = ",".join(k for k in STAFF_PERMISSIONS if k in set(keys or ())) or None

    def can(self, permission):
        """Board members (managers) can do everything; staff only what they were given."""
        if self.is_admin:
            return True
        if not self.is_staff:
            return False
        keys = set(self.permission_keys)
        return permission in keys or (permission == "accounting" and "accounting_edit" in keys)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def board_positions(self):
        """Board positions of the Directory person with this account's email (cached per request)."""
        from flask import has_request_context, request
        cache = {}
        if has_request_context():  # cache for this request only
            req = request._get_current_object()
            cache = req.__dict__.setdefault("_board_positions", {})
        if self.id not in cache:
            people = db.session.scalars(db.select(Owner).where(
                db.func.lower(Owner.email) == (self.email or "").lower(), Owner.board_roles.is_not(None))).all()
            cache[self.id] = [k for k in BOARD_POSITIONS if any(k in o.positions for o in people)]
        return cache[self.id]

    @property
    def is_board(self):
        return bool(self.board_positions)

    @property
    def is_temp_admin(self):
        return self.role == "admin"

    @property
    def is_admin(self):
        """Can manage the building: board members (and the temporary first account)."""
        return self.role == "admin" or self.is_board

    @property
    def is_active(self):  # Flask-Login hook: deactivated users cannot log in
        return self.active

    @property
    def role_label(self):
        if self.is_board:
            return ", ".join(str(BOARD_POSITIONS[k]) for k in self.board_positions)
        if self.is_staff and self.job_title:
            return self.job_title
        return ROLE_LABELS.get(self.role, self.role)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class Unit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(20), unique=True, nullable=False)
    floor = db.Column(db.Integer)
    bedrooms = db.Column(db.Integer)
    area_sqft = db.Column(db.Integer)
    parking_spot = db.Column(db.String(20))
    monthly_fee_cents = db.Column(db.Integer, nullable=False, default=0)
    owner_name = db.Column(db.String(200))
    ownership_share = db.Column(db.Float)  # fraction of the building, e.g. 0.1161
    notes = db.Column(db.Text)

    residents = db.relationship("User", back_populates="unit", order_by="User.name")
    charges = db.relationship("Charge", back_populates="unit", cascade="all, delete-orphan",
                              order_by="Charge.due_date.desc()")
    payments = db.relationship("Payment", back_populates="unit", cascade="all, delete-orphan",
                               order_by="Payment.paid_on.desc()")
    owners = db.relationship("Owner", back_populates="unit", cascade="all, delete-orphan",
                             order_by="(Owner.is_primary.desc(), Owner.name)")

    @property
    def owner_contacts(self):
        return [o for o in self.owners if o.role != "tenant"]

    @property
    def tenants(self):
        return [o for o in self.owners if o.role == "tenant"]

    def members(self, group):
        """People who need cover in a group: 'owners' (owners and co-owners) or 'tenants'."""
        people = self.tenants if group == "tenants" else self.owner_contacts
        return [o for o in people if o.needs_insurance]

    def policies_for(self, group):
        return db.session.scalars(
            db.select(InsurancePolicy).where(InsurancePolicy.unit_id == self.id, InsurancePolicy.holder_group == group)
            .order_by(InsurancePolicy.valid_until.desc(), InsurancePolicy.id.desc())).all()

    def current_policy(self, group):
        """One policy covers the household: all the unit's owners, or all its tenants."""
        policies = self.policies_for(group)
        return policies[0] if policies else None

    def extra_policies(self, group, today=None):
        """Other policies still in force beside the main one, e.g. a co-owner who also keeps
        their own. Optional: the household is covered by any one valid policy. Renewals of the
        same policy (same policyholder and policy number) aren't listed twice."""
        from datetime import date
        today = today or date.today()
        policies = self.policies_for(group)
        if not policies:
            return []
        main, seen, extra = policies[0], set(), []
        seen.add((main.owner_id, (main.policy_number or "").strip().lower()))
        for p in policies[1:]:
            key = (p.owner_id, (p.policy_number or "").strip().lower())
            if p.valid_until >= today and key not in seen:
                seen.add(key)
                extra.append(p)
        return extra

    def group_insurance(self, group):
        """Insurance state of the owners' or tenants' shared policy; None when nobody is in the group."""
        if not self.members(group):
            return None
        p = self.current_policy(group)
        return insurance_state(p.valid_until if p else None)

    @property
    def owner_display(self):
        """Names of the unit's owner contacts, or the plain owner name when none are recorded."""
        names = [o.name for o in self.owners if o.role in ("owner", "co-owner")]
        return " / ".join(names) if names else (self.owner_name or "")

    @property
    def total_charged(self):
        return db.session.scalar(
            db.select(func.coalesce(func.sum(Charge.amount_cents), 0)).where(Charge.unit_id == self.id)
        )

    @property
    def total_paid(self):
        return db.session.scalar(
            db.select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(Payment.unit_id == self.id)
        )

    @property
    def balance(self):
        """Positive = amount owed by the unit; negative = credit."""
        return self.total_charged - self.total_paid

    def ledger(self):
        """Chronological ledger rows with a running balance."""
        entries = [(c.due_date, 0, c.id, c.description, c.amount_cents, 0, c.origin) for c in self.charges]
        entries += [(p.paid_on, 1, p.id, p.label, 0, p.amount_cents, p.origin) for p in self.payments]
        entries.sort(key=lambda e: (e[0], e[1], e[2]))
        running, rows = 0, []
        for when, kind, entry_id, desc, charge, payment, origin in entries:
            running += charge - payment
            rows.append({"date": when, "type": "payment" if kind else "charge", "id": entry_id, "origin": origin,
                         "description": desc, "charge": charge, "payment": payment, "balance": running})
        return rows


CHARGE_KINDS = {"dues": _l("Monthly dues"), "special": _l("Special contribution"), "fine": _l("Fine"), "other": _l("Other"),
                "opening": _l("Opening balance")}
# Kinds created by the system (dues generation, spreadsheet import), never by the manual charge form.
SYSTEM_CHARGE_KINDS = ("dues", "opening")


class Charge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, default="dues")
    period = db.Column(db.String(7))  # 'YYYY-MM' for monthly dues
    description = db.Column(db.String(255), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    origin = db.Column(db.String(20))  # 'sheet' = synced from the Google Sheet; None = entered in the app
    sheet_key = db.Column(db.String(255), index=True)  # which sheet row it came from (see sheet_sync.entry_keys)
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit", back_populates="charges")

    __table_args__ = (db.UniqueConstraint("unit_id", "kind", "period", name="uq_charge_unit_kind_period"),)


PAYMENT_METHODS = ("check", "bank transfer", "card", "cash", "purchase credit", "other")
PAYMENT_METHOD_LABELS = {"bank transfer": _l("Interac e-Transfer / bank transfer"), "check": _l("Cheque"), "card": _l("Card"),
                         "cash": _l("Cash"), "purchase credit": _l("Credit for a purchase (owner paid a cost)"),
                         "other": _l("Other")}


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False, index=True)
    amount_cents = db.Column(db.Integer, nullable=False)
    paid_on = db.Column(db.Date, nullable=False)
    method = db.Column(db.String(30), nullable=False, default="check")
    reference = db.Column(db.String(80))
    notes = db.Column(db.String(255))
    origin = db.Column(db.String(20))  # 'sheet' = synced from the Google Sheet; None = entered in the app
    sheet_key = db.Column(db.String(255), index=True)  # which sheet row it came from (see sheet_sync.entry_keys)
    special_id = db.Column(db.Integer, db.ForeignKey("special_contribution.id"))  # paying a special contribution
    recorded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    source_data = db.Column(db.JSON)  # the full Google Sheet row it came from (tab, row number and every column)
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit", back_populates="payments")
    recorded_by = db.relationship("User")
    special = db.relationship("SpecialContribution", foreign_keys=[special_id])

    @property
    def summary(self):
        """A short description for lists; the payment's page shows everything."""
        if self.origin == "sheet" and self.notes:
            return self.notes.split(" — ", 1)[0]
        if self.notes:
            return self.notes.split(" — ", 1)[0] if self.method == "purchase credit" else self.notes
        return str(PAYMENT_METHOD_SHORT.get(self.method, PAYMENT_METHOD_LABELS.get(self.method, self.method)))

    @property
    def label(self):
        text = self.notes or str(_l("Payment (%(method)s)", method=PAYMENT_METHOD_LABELS.get(self.method, self.method)))
        return text + (f" #{self.reference}" if self.reference else "")


PAYMENT_METHOD_SHORT = {"bank transfer": _l("e-Transfer"), "check": _l("Cheque"), "card": _l("Card"), "cash": _l("Cash"),
                        "purchase credit": _l("Purchase credit"), "other": _l("Payment")}


REQUEST_STATUSES = {"awaiting_owner": _l("Awaiting owner approval"), "open": _l("Open"),
                    "quote_requested": _l("Owner to get a quote"), "quote_review": _l("Quote awaiting board approval"),
                    "owner_repair": _l("Approved: owner to do the repair"), "in_progress": _l("In progress"),
                    "resolved": _l("Resolved"), "closed": _l("Closed")}
ACTIVE_STATUSES = ("awaiting_owner", "open", "quote_requested", "quote_review", "owner_repair", "in_progress")
QUOTE_STATUSES = ("quote_requested", "quote_review", "owner_repair")
REQUEST_PRIORITIES = {"low": _l("Low"), "normal": _l("Normal"), "high": _l("High"), "urgent": _l("Urgent")}
REQUEST_CATEGORIES = ("Plumbing", "Electrical", "HVAC", "Appliance", "Common area", "Security", "Other")
# Stored in English; shown in the reader's language.
CATEGORY_LABELS = {"Plumbing": _l("Plumbing"), "Electrical": _l("Electrical"), "HVAC": _l("HVAC"), "Appliance": _l("Appliance"),
                   "Common area": _l("Common area"), "Security": _l("Security"), "Other": _l("Other")}


class MaintenanceRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), index=True)  # null = common area
    submitted_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(40), nullable=False, default="Other")
    priority = db.Column(db.String(20), nullable=False, default="normal")
    status = db.Column(db.String(20), nullable=False, default="open")
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    # Tenant requests for their unit need the owner's go-ahead before work starts.
    owner_approval = db.Column(db.String(20))  # None (not needed), 'pending', 'approved', 'declined'
    owner_decision_by = db.Column(db.String(200))
    owner_decision_at = db.Column(db.DateTime)
    owner_decision_note = db.Column(db.Text)
    # Quote step: the board asks the owner to get a quote, then approves the cost before work.
    quote_note = db.Column(db.Text)               # board's instructions to the owner
    quote_vendor = db.Column(db.String(200))
    quote_amount_cents = db.Column(db.Integer)
    quote_file_id = db.Column(db.Integer, db.ForeignKey("stored_file.id"))
    quote_approved_cents = db.Column(db.Integer)
    quote_decided_by = db.Column(db.String(200))
    quote_decided_at = db.Column(db.DateTime)
    quote_decision_note = db.Column(db.Text)
    # 'maintenance' (a repair or upkeep request) or 'project' (planned building work, e.g. a roof).
    kind = db.Column(db.String(20), nullable=False, default="maintenance", server_default="maintenance")
    budget_cents = db.Column(db.Integer)   # projects: expected cost
    target_date = db.Column(db.Date)       # projects: when it should be done

    unit = db.relationship("Unit")
    submitted_by = db.relationship("User")
    quote_file = db.relationship("StoredFile", foreign_keys=[quote_file_id])
    comments = db.relationship("MaintenanceComment", back_populates="request",
                               cascade="all, delete-orphan", order_by="MaintenanceComment.created_at")

    @property
    def is_active(self):
        return self.status in ACTIVE_STATUSES

    @property
    def awaiting_owner(self):
        return self.status == "awaiting_owner"

    @property
    def is_project(self):
        return self.kind == "project"


class MaintenanceComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("maintenance_request.id"), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    request = db.relationship("MaintenanceRequest", back_populates="comments")
    author = db.relationship("User")


class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    pinned = db.Column(db.Boolean, nullable=False, default=False)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    author = db.relationship("User")


class Amenity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.Text)
    open_hour = db.Column(db.Integer, nullable=False, default=8)    # 0-23
    close_hour = db.Column(db.Integer, nullable=False, default=22)  # 1-24
    max_hours = db.Column(db.Integer, nullable=False, default=4)
    active = db.Column(db.Boolean, nullable=False, default=True)

    bookings = db.relationship("Booking", back_populates="amenity", cascade="all, delete-orphan")


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    amenity_id = db.Column(db.Integer, db.ForeignKey("amenity.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    start = db.Column(db.DateTime, nullable=False)
    end = db.Column(db.DateTime, nullable=False)
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow)

    amenity = db.relationship("Amenity", back_populates="bookings")
    user = db.relationship("User")

    @staticmethod
    def conflicts(amenity_id, start, end):
        return db.session.scalars(
            db.select(Booking).where(Booking.amenity_id == amenity_id,
                                     Booking.start < end, Booking.end > start)
        ).all()


class ImportBatch(db.Model):
    """An uploaded spreadsheet, parsed and waiting for the manager to load each dataset."""
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255))
    payload = db.Column(db.JSON, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    created_by = db.relationship("User")


COST_CATEGORIES = {
    "utilities": _l("Utilities"),
    "insurance": _l("Insurance"),
    "maintenance": _l("Maintenance"),
    "repairs": _l("Repairs & projects"),
    "administration": _l("Administration"),
    "other": _l("Other"),
}
_COST_KEYWORDS = [
    ("insurance", ("insurance", "assurance")),
    ("utilities", ("hydro", "electric bill", "électricité", "gas", "water bill")),
    ("maintenance", ("snow", "lawn", "landscap", "garden", "gardening", "cleaning", "déneigement", "leaf", "compost")),
    ("administration", ("bank", "fee", "frais", "interac", "cheque", "check order", "accounting", "tax")),
    ("repairs", ("repair", "heater", "plumb", "roof", "window", "door", "brick", "garage", "contractor",
                 "electric", "paint", "invoice")),
]


def guess_cost_category(text):
    t = (text or "").lower()
    for key, words in _COST_KEYWORDS:
        if any(w in t for w in words):
            return key
    return "other"


class BankBalance(db.Model):
    """What the condo's bank account held at the end of a day, as the board read it from the bank.
    The first one is the opening balance; later ones are checks against the app's own running total."""
    id = db.Column(db.Integer, primary_key=True)
    as_of = db.Column(db.Date, nullable=False, index=True)
    amount_cents = db.Column(db.Integer, nullable=False)
    note = db.Column(db.String(255))
    recorded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    recorded_by = db.relationship("User")


def bank_movement(after, until=None):
    """Cash in minus cash out of the bank after a day (and up to `until`, inclusive): payments received
    (not purchase credits, where the owner paid the supplier directly) and building costs or refunds."""
    pay = db.select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
        Payment.paid_on > after, Payment.method != "purchase credit")
    exp = db.select(func.coalesce(func.sum(Expense.amount_cents), 0)).where(Expense.spent_on > after)
    if until is not None:
        pay, exp = pay.where(Payment.paid_on <= until), exp.where(Expense.spent_on <= until)
    return db.session.scalar(pay) + db.session.scalar(exp)


def bank_summary(today=None):
    """The bank records with what the app expected at each one, and the balance worked out for today."""
    today = today or date.today()
    records = db.session.scalars(db.select(BankBalance).order_by(BankBalance.as_of, BankBalance.id)).all()
    rows, prev = [], None
    for r in records:
        expected = None if prev is None else prev.amount_cents + bank_movement(prev.as_of, r.as_of)
        rows.append({"record": r, "expected": expected,
                     "difference": None if expected is None else r.amount_cents - expected})
        prev = r
    if prev is None:
        return {"rows": [], "last": None, "today": None, "since": 0}
    since = bank_movement(prev.as_of)
    return {"rows": rows, "last": prev, "since": since, "today": prev.amount_cents + since}


class Expense(db.Model):
    """A building-level cost (or refund) not tied to a unit, e.g. a contractor invoice.
    Negative amounts are money paid out of the condo account; positive ones are money back."""
    id = db.Column(db.Integer, primary_key=True)
    spent_on = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(20))
    description = db.Column(db.String(255), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    approved_by = db.Column(db.String(255))
    source = db.Column(db.String(255))
    notes = db.Column(db.Text)
    origin = db.Column(db.String(20))
    sheet_key = db.Column(db.String(255), index=True)  # which sheet row it came from (see sheet_sync.entry_keys)
    recorded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    source_data = db.Column(db.JSON)  # the full Google Sheet row it came from
    created_at = db.Column(db.DateTime, default=utcnow)

    recorded_by = db.relationship("User")

    @property
    def sort_id(self):
        return self.id

    paid_by = None

    @property
    def cost_cents(self):
        """Money spent as a positive number (a refund shows as negative)."""
        return -self.amount_cents

    @property
    def summary(self):
        """A short description for lists; the cost's page shows everything."""
        return self.description.split(" — ", 1)[0]

    @property
    def category_key(self):
        return self.category if self.category in COST_CATEGORIES else guess_cost_category(
            f"{self.description} {self.notes or ''}")

    @property
    def category_label(self):
        return COST_CATEGORIES[self.category_key]


class Setting(db.Model):
    """Small key/value store for app-wide settings (e.g. the linked Google Sheet)."""
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.JSON)

    @staticmethod
    def get(key, default=None):
        row = db.session.get(Setting, key)
        return row.value if row is not None else default

    @staticmethod
    def put(key, value):
        row = db.session.get(Setting, key)
        if row is None:
            db.session.add(Setting(key=key, value=value))
        else:
            row.value = value


OWNER_ROLES = {"owner": _l("Owner"), "co-owner": _l("Co-owner"), "tenant": _l("Tenant"), "contact": _l("Other contact")}

# Board of directors of the syndicate. Order = display order.
BOARD_POSITIONS = {
    "president": _l("President"),
    "vice-president": _l("Vice-president"),
    "treasurer": _l("Treasurer"),
    "secretary": _l("Secretary"),
    "accounting": _l("Accounting"),
    "director": _l("Director"),
}
SINGLE_HOLDER_POSITIONS = ("president", "vice-president", "treasurer", "secretary")
OPEN_TO_CONTACTS = ("accounting",)  # positions an outside contact (not a co-owner) may hold


def allowed_positions(role):
    if role in ("owner", "co-owner"):
        return tuple(BOARD_POSITIONS)
    if role == "contact":
        return OPEN_TO_CONTACTS
    return ()


class Owner(db.Model):
    """A person connected to a unit (usually one of several co-owners), with contact details.
    Separate from login accounts: an owner doesn't need to use the website."""
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="owner")
    email = db.Column(db.String(255))
    phone = db.Column(db.String(60))
    mailing_address = db.Column(db.Text)  # empty = lives in the unit
    is_primary = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.Text)
    board_roles = db.Column(db.String(200))  # comma-separated BOARD_POSITIONS keys
    origin = db.Column(db.String(20))  # 'sheet' = synced from the Google Sheet's Owners tab
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit", back_populates="owners")
    # Policies where this person is the named policyholder (each covers their whole group).
    policies = db.relationship("InsurancePolicy", back_populates="owner", cascade="all, delete-orphan",
                               passive_deletes=True,
                               order_by="(InsurancePolicy.valid_until.desc(), InsurancePolicy.id.desc())")

    @property
    def role_label(self):
        return OWNER_ROLES.get(self.role, self.role)

    @property
    def positions(self):
        keys = [k.strip() for k in (self.board_roles or "").split(",") if k.strip() in BOARD_POSITIONS]
        return sorted(set(keys), key=list(BOARD_POSITIONS).index)

    @positions.setter
    def positions(self, keys):
        keys = [k for k in BOARD_POSITIONS if k in set(keys or ())]
        self.board_roles = ",".join(keys) or None

    @property
    def position_labels(self):
        return [str(BOARD_POSITIONS[k]) for k in self.positions]

    @property
    def insurance_group(self):
        return "tenants" if self.role == "tenant" else "owners"

    @property
    def current_policy(self):
        """The household policy that covers this person (shared with co-owners or co-tenants)."""
        return self.unit.current_policy(self.insurance_group) if self.needs_insurance and self.unit else None

    @property
    def insurance_state(self):
        p = self.current_policy
        return insurance_state(p.valid_until if p else None)

    @property
    def needs_insurance(self):
        return self.role in ("owner", "co-owner", "tenant")


# --- files, documents and insurance ---------------------------------------------

class StoredFile(db.Model):
    """An uploaded file kept in the database (serverless hosting has no lasting disk).
    Served only through views that check who may see it."""
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(100), nullable=False)
    size = db.Column(db.Integer, nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    uploaded_by = db.relationship("User")


DOCUMENT_CATEGORIES = {
    "syndicate": _l("Syndicate documents"),
    "insurance": _l("Building insurance"),
    "minutes": _l("Annual meeting minutes"),
    "other": _l("Other"),
}
DOCUMENT_VISIBILITY = {"everyone": _l("Everyone signed in"), "owners": _l("Owners and managers"), "managers": _l("Managers only")}


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(20), nullable=False, default="other")
    description = db.Column(db.Text)
    document_date = db.Column(db.Date)   # meeting date, adoption date, issue date
    valid_from = db.Column(db.Date)      # insurance policy period
    valid_until = db.Column(db.Date)
    provider = db.Column(db.String(200))  # insurer
    policy_number = db.Column(db.String(100))
    visibility = db.Column(db.String(20), nullable=False, default="everyone")
    file_id = db.Column(db.Integer, db.ForeignKey("stored_file.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    file = db.relationship("StoredFile", cascade="all, delete-orphan", single_parent=True)

    def visible_to(self, user):
        if user.can("documents") or self.visibility == "everyone":
            return True
        return self.visibility == "owners" and user.role == "owner"


EXPIRING_SOON_DAYS = 30


def insurance_state(valid_until, today=None):
    """'valid', 'expiring', 'expired' or 'missing' for a policy end date."""
    from datetime import date, timedelta
    today = today or date.today()
    if valid_until is None:
        return "missing"
    if valid_until < today:
        return "expired"
    if valid_until <= today + timedelta(days=EXPIRING_SOON_DAYS):
        return "expiring"
    return "valid"


INSURANCE_STATE_LABELS = {"valid": _l("Insured"), "expiring": _l("Expiring soon"), "expired": _l("Expired"), "missing": _l("No insurance")}


class InsurancePolicy(db.Model):
    """A home-insurance policy covering a unit's owners together, or its tenants together.
    `owner` is the named policyholder. Renewals add a new row; the latest end date counts."""
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), index=True)
    holder_group = db.Column(db.String(10))  # 'owners' or 'tenants'
    provider = db.Column(db.String(200))
    policy_number = db.Column(db.String(100))
    valid_from = db.Column(db.Date, nullable=False)
    valid_until = db.Column(db.Date, nullable=False)
    file_id = db.Column(db.Integer, db.ForeignKey("stored_file.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    owner = db.relationship("Owner", back_populates="policies")
    unit = db.relationship("Unit")
    file = db.relationship("StoredFile", cascade="all, delete-orphan", single_parent=True)

    @property
    def state(self):
        return insurance_state(self.valid_until)


def hand_over_policies(person):
    """Before removing a person, pass the household policies they hold to someone else in
    the same group, so removing one co-owner doesn't drop the household's insurance."""
    others = [o for o in person.unit.members(person.insurance_group) if o.id != person.id] if person.unit else []
    if not others:
        return False
    heir = next((o for o in others if o.is_primary), others[0])
    for policy in list(person.policies):
        policy.owner = heir
    return True


class Notification(db.Model):
    """Something a user should know about, shown under the bell until they read it."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text)
    url = db.Column(db.String(500))
    kind = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    read_at = db.Column(db.DateTime)

    user = db.relationship("User")


REIMBURSEMENT_STATUSES = {"submitted": _l("Waiting for the president"), "approved": _l("Approved: waiting for accounting"),
                          "declined": _l("Declined"), "settled": _l("Settled")}
SETTLE_METHODS = {"credit": _l("Credit on my next condo payment"), "transfer": _l("Pay me back (e-transfer or cheque)")}


class Reimbursement(db.Model):
    """An owner paid a building cost and asks for it back, or for a credit on their dues."""
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False, index=True)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    request_id = db.Column(db.Integer, db.ForeignKey("maintenance_request.id"))  # repair it came from, if any
    description = db.Column(db.String(255), nullable=False)
    vendor = db.Column(db.String(200))
    amount_cents = db.Column(db.Integer, nullable=False)
    spent_on = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(20))
    settle_method = db.Column(db.String(20), nullable=False, default="credit")
    status = db.Column(db.String(20), nullable=False, default="submitted")
    decided_by = db.Column(db.String(200))
    decided_at = db.Column(db.DateTime)
    decision_note = db.Column(db.Text)
    settled_by = db.Column(db.String(200))
    settled_at = db.Column(db.DateTime)
    settle_reference = db.Column(db.String(120))
    payment_id = db.Column(db.Integer, db.ForeignKey("payment.id"))
    expense_id = db.Column(db.Integer, db.ForeignKey("expense.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit")
    requested_by = db.relationship("User")
    request = db.relationship("MaintenanceRequest")
    files = db.relationship("ReimbursementFile", back_populates="reimbursement", cascade="all, delete-orphan")

    @property
    def status_label(self):
        return REIMBURSEMENT_STATUSES.get(self.status, self.status)


class ReimbursementFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reimbursement_id = db.Column(db.Integer, db.ForeignKey("reimbursement.id", ondelete="CASCADE"), nullable=False)
    file_id = db.Column(db.Integer, db.ForeignKey("stored_file.id"), nullable=False)

    reimbursement = db.relationship("Reimbursement", back_populates="files")
    file = db.relationship("StoredFile", cascade="all, delete-orphan", single_parent=True)


def link_admin_accounts():
    """Turn temporary administrator accounts into the person's own account once that person is
    in the Directory (same email) with a board position. Returns the converted accounts."""
    from flask import has_request_context, request
    if has_request_context():
        request._get_current_object().__dict__.pop("_board_positions", None)
    converted = []
    for user in db.session.scalars(db.select(User).where(User.role == "admin")).all():
        person = db.session.scalar(db.select(Owner).where(db.func.lower(Owner.email) == user.email.lower(),
                                                         Owner.board_roles.is_not(None)))
        if person is None or not person.positions:
            continue
        user.role = "tenant" if person.role == "tenant" else "owner"
        user.unit_id = person.unit_id
        if user.name.strip().lower() in ("building manager", "manager", "administrator", "admin", ""):
            user.name = person.name[:120]
        converted.append(user)
    return converted


# --- annual condo fees and special contributions ---------------------------------

def split_by_weight(total_cents, weights):
    """Split an amount in proportion to weights so the parts add up exactly to the total
    (largest-remainder rounding). `weights` is {key: weight}."""
    weight_sum = sum(weights.values())
    if not weights or weight_sum <= 0:
        return {k: 0 for k in weights}
    exact = {k: total_cents * w / weight_sum for k, w in weights.items()}
    parts = {k: int(v) for k, v in exact.items()}
    leftover = total_cents - sum(parts.values())
    for k in sorted(exact, key=lambda k: exact[k] - parts[k], reverse=True)[:leftover]:
        parts[k] += 1
    return parts


def add_months(d, months):
    """The same day `months` later (or earlier), clamped to the month's last day."""
    import calendar
    from datetime import date
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


FEE_BASES = {"annual": _l("Annual total"), "monthly": _l("Monthly total")}
RENEWAL_NOTICE_MONTHS = 2


class FeeSchedule(db.Model):
    """The building's condo fees for one period (normally a year), split between units by ownership
    share. The board enters either the total for the whole period or the total per month."""
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, unique=True)  # the year the period starts in
    amount_cents = db.Column("annual_total_cents", db.Integer, nullable=False)  # in `basis` terms
    basis = db.Column(db.String(10), nullable=False, default="annual", server_default="annual")
    starts_on = db.Column(db.Date)
    ends_on = db.Column(db.Date)
    notes = db.Column(db.Text)
    applied_at = db.Column(db.DateTime)
    renewal_notified_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)

    overrides = db.relationship("FeeOverride", back_populates="schedule", cascade="all, delete-orphan")

    @property
    def start(self):
        from datetime import date
        return self.starts_on or date(self.year, 1, 1)

    @property
    def end(self):
        from datetime import date
        return self.ends_on or date(self.year, 12, 31)

    @property
    def months(self):
        """Whole months in the period (Jan 1 – Dec 31 is 12)."""
        return max(round(((self.end - self.start).days + 1) / 30.4375), 1)

    @property
    def renewal_date(self):
        return add_months(self.end, -RENEWAL_NOTICE_MONTHS)

    def _to_annual(self, cents):
        return cents * self.months if self.basis == "monthly" else cents

    def _to_monthly(self, cents):
        return cents if self.basis == "monthly" else round(cents / self.months)

    @property
    def annual_total_cents(self):
        """Total for the whole period."""
        return self._to_annual(self.amount_cents)

    @property
    def monthly_total_cents(self):
        return self._to_monthly(self.amount_cents)

    def unit_fees(self, units):
        """[{unit, share, annual, monthly, amount, override}] for every unit. Fixed amounts are kept
        as set; the rest of the total is split by ownership share. `amount` is in the schedule's basis."""
        fixed = {o.unit_id: o.amount_cents for o in self.overrides}
        rest = [u for u in units if u.id not in fixed]
        remaining = self.amount_cents - sum(v for k, v in fixed.items() if any(u.id == k for u in units))
        split = split_by_weight(max(remaining, 0), {u.id: (u.ownership_share or 0) for u in rest})
        rows = []
        for u in units:
            amount = fixed.get(u.id, split.get(u.id, 0))
            rows.append({"unit": u, "share": u.ownership_share, "amount": amount, "annual": self._to_annual(amount),
                         "monthly": self._to_monthly(amount), "override": u.id in fixed})
        return rows


class FeeOverride(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("fee_schedule.id", ondelete="CASCADE"), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False)
    amount_cents = db.Column("annual_cents", db.Integer, nullable=False)  # in the schedule's basis

    schedule = db.relationship("FeeSchedule", back_populates="overrides")
    __table_args__ = (db.UniqueConstraint("schedule_id", "unit_id", name="uq_fee_override_unit"),)


SPLIT_METHODS = {"share": _l("By ownership share"), "equal": _l("Equally between units"), "custom": _l("Amounts I enter per unit")}


class SpecialContribution(db.Model):
    """A one-off amount the owners must pay by a deadline (e.g. roof or window replacement)."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    total_cents = db.Column(db.Integer, nullable=False)
    deadline = db.Column(db.Date, nullable=False)
    method = db.Column(db.String(10), nullable=False, default="share")
    billed_at = db.Column(db.DateTime)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    shares = db.relationship("SpecialShare", back_populates="special", cascade="all, delete-orphan",
                             order_by="SpecialShare.id")
    created_by = db.relationship("User")

    def paid_by_unit(self):
        """What each unit has paid towards this contribution: payments recorded for it, plus its part of
        the unit's "Special contribution" payments that aren't linked to one (e.g. from the Google Sheet),
        which go to the contributions in deadline order."""
        return special_allocations().get(self.id, {})

    def status_rows(self, today=None):
        from datetime import date
        today = today or date.today()
        paid = self.paid_by_unit()
        rows = []
        for sh in sorted(self.shares, key=lambda s: s.unit.number):
            p = paid.get(sh.unit_id, 0)
            left = max(sh.amount_cents - p, 0)
            if sh.amount_cents == 0 or left == 0:
                status = "paid"
            elif self.billed_at is None and not p:
                status = "draft"
            elif today > self.deadline:
                status = "overdue"
            else:
                status = "partial" if p else "unpaid"
            rows.append({"share": sh, "unit": sh.unit, "amount": sh.amount_cents, "paid": p, "left": left,
                         "status": status})
        return rows

    @property
    def collected_cents(self):
        return min(sum(self.paid_by_unit().values()), self.total_cents)


def special_allocations():
    """{contribution id: {unit id: cents paid}} for every special contribution.

    Payments recorded for a contribution count for it. A unit's other payments described as a special
    contribution (the Google Sheet's "Special contribution" type) fill its shares in deadline order;
    whatever is left over after the last one counts for the last one."""
    specials = db.session.scalars(db.select(SpecialContribution).order_by(SpecialContribution.deadline,
                                                                          SpecialContribution.id)).all()
    result = {sc.id: {sh.unit_id: sh.prepaid_cents for sh in sc.shares if sh.prepaid_cents} for sc in specials}
    for unit_id, special_id, total in db.session.execute(
            db.select(Payment.unit_id, Payment.special_id, func.sum(Payment.amount_cents))
            .where(Payment.special_id.is_not(None)).group_by(Payment.unit_id, Payment.special_id)):
        if special_id in result:
            result[special_id][unit_id] = result[special_id].get(unit_id, 0) + total
    loose = {}
    for p in db.session.scalars(db.select(Payment).where(Payment.special_id.is_(None),
                                                         func.lower(Payment.notes).like("special contribution%"))):
        loose[p.unit_id] = loose.get(p.unit_id, 0) + p.amount_cents
    for unit_id, cents in loose.items():
        mine = [(sc, sh) for sc in specials for sh in sc.shares if sh.unit_id == unit_id]
        for i, (sc, sh) in enumerate(mine):
            if cents <= 0:
                break
            have = result[sc.id].get(unit_id, 0)
            take = cents if i == len(mine) - 1 else min(cents, max(sh.amount_cents - have, 0))
            result[sc.id][unit_id] = have + take
            cents -= take
    return result


SPECIAL_STATUS_LABELS = {"paid": _l("Paid"), "partial": _l("Partly paid"), "unpaid": _l("Not paid yet"), "overdue": _l("Overdue"),
                         "draft": _l("Not billed yet")}


class SpecialShare(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    special_id = db.Column(db.Integer, db.ForeignKey("special_contribution.id", ondelete="CASCADE"), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    charge_id = db.Column(db.Integer, db.ForeignKey("charge.id"))
    # Paid before the portal was in use (e.g. in August, before the Google Sheet log starts): it counts
    # towards the contribution but isn't a payment in the app, so balances and the bank aren't changed.
    prepaid_cents = db.Column(db.Integer)
    prepaid_on = db.Column(db.Date)

    special = db.relationship("SpecialContribution", back_populates="shares")
    unit = db.relationship("Unit")


# --- Routine maintenance: work that comes back on a schedule (snow removal, gutters, inspections) -------

ROUTINE_FREQUENCIES = {  # months between visits
    "monthly": (1, _l("Every month")),
    "quarterly": (3, _l("Every 3 months")),
    "twice_yearly": (6, _l("Twice a year")),
    "yearly": (12, _l("Every year")),
    "every_2_years": (24, _l("Every 2 years")),
    "every_5_years": (60, _l("Every 5 years")),
}
ROUTINE_DUE_SOON_DAYS = 14
CONTRACT_NOTICE_DAYS = 60


class RoutineTask(db.Model):
    """Recurring building upkeep. Marking it done logs the visit and moves the next due date."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(40), nullable=False, default="Other")
    frequency = db.Column(db.String(20), nullable=False, default="yearly")
    next_due = db.Column(db.Date, nullable=False)
    season_from = db.Column(db.Integer)  # month the season starts, e.g. 11 for snow removal (optional)
    season_to = db.Column(db.Integer)    # month it ends, e.g. 4
    vendor = db.Column(db.String(200))
    vendor_contact = db.Column(db.String(200))
    estimated_cents = db.Column(db.Integer)
    contract_ends = db.Column(db.Date)
    active = db.Column(db.Boolean, nullable=False, default=True)
    reminded_due = db.Column(db.Date)          # the due date the board was last reminded about
    reminded_contract = db.Column(db.Date)     # the contract end date the board was last reminded about
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    logs = db.relationship("RoutineLog", back_populates="task", cascade="all, delete-orphan",
                           order_by="RoutineLog.done_on.desc()")

    @property
    def interval_months(self):
        return ROUTINE_FREQUENCIES.get(self.frequency, (12, None))[0]

    @property
    def frequency_label(self):
        return ROUTINE_FREQUENCIES.get(self.frequency, (12, self.frequency))[1]

    @property
    def last_done(self):
        return self.logs[0] if self.logs else None

    def state(self, today=None):
        from datetime import date, timedelta
        today = today or date.today()
        if not self.active:
            return "paused"
        if self.next_due < today:
            return "overdue"
        if self.next_due <= today + timedelta(days=ROUTINE_DUE_SOON_DAYS):
            return "due_soon"
        return "scheduled"

    def contract_ending(self, today=None):
        from datetime import date, timedelta
        today = today or date.today()
        return bool(self.contract_ends and today <= self.contract_ends <= today + timedelta(days=CONTRACT_NOTICE_DAYS))

    def advance(self, done_on):
        """Next due date after a visit on `done_on`: step from the scheduled date until it's after the visit."""
        nxt = self.next_due
        while nxt <= done_on:
            nxt = add_months(nxt, self.interval_months)
        self.next_due = nxt


ROUTINE_STATE_LABELS = {"overdue": _l("Overdue"), "due_soon": _l("Due soon"), "scheduled": _l("Scheduled"),
                        "paused": _l("Paused")}


class RoutineLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("routine_task.id", ondelete="CASCADE"), nullable=False, index=True)
    done_on = db.Column(db.Date, nullable=False)
    cost_cents = db.Column(db.Integer)
    note = db.Column(db.Text)
    expense_id = db.Column(db.Integer, db.ForeignKey("expense.id"))
    done_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    task = db.relationship("RoutineTask", back_populates="logs")
    done_by = db.relationship("User")


# --- Votes: the board drafts a poll, publishes it to an audience, owners vote until the deadline ----------

POLL_AUDIENCES = {"owners": _l("All owners"), "units": _l("Owners of selected units"), "board": _l("Board members")}
POLL_WEIGHTINGS = {
    "share": _l("By ownership share (a unit's owners split its share)"),
    "unit": _l("One vote per unit (a unit's owners split it)"),
    "person": _l("One vote per person"),
}
POLL_STATUS_LABELS = {"draft": _l("Draft"), "open": _l("Open"), "closed": _l("Closed")}


class Poll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    audience = db.Column(db.String(10), nullable=False, default="owners")
    unit_ids = db.Column(db.String(500))  # for audience "units": comma-separated unit ids
    weighting = db.Column(db.String(10), nullable=False, default="share")
    deadline = db.Column(db.DateTime, nullable=False)  # UTC
    status = db.Column(db.String(10), nullable=False, default="draft")  # draft, open, closed (early)
    published_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)
    reminded_at = db.Column(db.DateTime)
    results_notified_at = db.Column(db.DateTime)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    options = db.relationship("PollOption", back_populates="poll", cascade="all, delete-orphan",
                              order_by="PollOption.position")
    voters = db.relationship("PollVoter", back_populates="poll", cascade="all, delete-orphan")
    created_by = db.relationship("User")

    @property
    def selected_unit_ids(self):
        return [int(x) for x in (self.unit_ids or "").split(",") if x.strip().isdigit()]

    @property
    def state(self):
        """draft, open, or closed (by the board or because the deadline passed)."""
        if self.status == "draft":
            return "draft"
        if self.status == "closed" or utcnow() >= self.deadline:
            return "closed"
        return "open"

    def voter_for(self, user):
        return next((v for v in self.voters if v.user_id == getattr(user, "id", None)), None)

    def results(self):
        """[{option, weight, votes, pct_of_cast, pct_of_all}], plus totals. Weights are shares of the whole vote."""
        total = sum(v.weight for v in self.voters) or 0
        cast = sum(v.weight for v in self.voters if v.option_id)
        rows = []
        for o in self.options:
            w = sum(v.weight for v in self.voters if v.option_id == o.id)
            rows.append({"option": o, "weight": w, "votes": sum(1 for v in self.voters if v.option_id == o.id),
                         "pct_of_cast": (w * 100 / cast) if cast else 0, "pct_of_all": (w * 100 / total) if total else 0})
        return {"rows": rows, "total_weight": total, "cast_weight": cast,
                "turnout": (cast * 100 / total) if total else 0,
                "voted": sum(1 for v in self.voters if v.option_id), "eligible": len(self.voters)}


class PollOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey("poll.id", ondelete="CASCADE"), nullable=False, index=True)
    text = db.Column(db.String(200), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)

    poll = db.relationship("Poll", back_populates="options")


class PollVoter(db.Model):
    """Who may vote and how much their vote counts, fixed when the poll is published. Holds their choice."""
    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey("poll.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"))
    weight = db.Column(db.Float, nullable=False, default=0)
    option_id = db.Column(db.Integer, db.ForeignKey("poll_option.id", ondelete="SET NULL"))
    voted_at = db.Column(db.DateTime)

    poll = db.relationship("Poll", back_populates="voters")
    user = db.relationship("User")
    unit = db.relationship("Unit")
    option = db.relationship("PollOption")
    __table_args__ = (db.UniqueConstraint("poll_id", "user_id", name="uq_poll_voter"),)
