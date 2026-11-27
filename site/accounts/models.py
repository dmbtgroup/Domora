"""Belcentra accounts: who a person is and which condos they belong to. No condo data lives here."""
import secrets
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False, default="")
    password_hash = db.Column(db.String(400))
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    is_platform_admin = db.Column(db.Boolean, nullable=False, default=False)  # opens every condo
    totp_secret = db.Column(db.String(64))
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False)
    recovery_codes = db.Column(db.Text)  # hashed, one per line
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    language = db.Column(db.String(2))
    created_at = db.Column(db.DateTime, default=utcnow)
    last_sign_in = db.Column(db.DateTime)
    memberships = db.relationship("Membership", backref="account", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    @property
    def needs_two_step(self):
        """Required for anyone who manages a building: board members and Belcentra administrators."""
        return self.is_platform_admin or any(m.active and m.board for m in self.memberships)

    def active_memberships(self):
        return sorted((m for m in self.memberships if m.active), key=lambda m: m.condo)

    def new_recovery_codes(self, count=10):
        codes = ["-".join(secrets.token_hex(2) for _ in range(2)) for _ in range(count)]
        self.recovery_codes = "\n".join(generate_password_hash(c) for c in codes)
        return codes

    def use_recovery_code(self, code):
        code = (code or "").strip().lower()
        hashes = [h for h in (self.recovery_codes or "").splitlines() if h]
        for h in hashes:
            if check_password_hash(h, code):
                hashes.remove(h)
                self.recovery_codes = "\n".join(hashes)
                return True
        return False


class Membership(db.Model):
    """One person's place in one condo, as that condo reports it."""
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False)
    condo = db.Column(db.String(60), nullable=False)  # slug, e.g. condo5077
    role = db.Column(db.String(80), nullable=False, default="")  # shown in the picker: President, Owner…
    board = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (db.UniqueConstraint("account_id", "condo"),)


class Event(db.Model):
    """Sign-ins, handovers and changes, for the record."""
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=utcnow, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"))
    kind = db.Column(db.String(40), nullable=False)
    detail = db.Column(db.String(300))
    ip = db.Column(db.String(64))


def record(kind, account=None, detail=None):
    from flask import has_request_context, request
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
          if has_request_context() else None)
    db.session.add(Event(kind=kind, account_id=account.id if account else None, detail=(detail or "")[:300], ip=ip))
