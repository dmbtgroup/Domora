"""Condos keep belcentra.com up to date: who has a sign-in there, their role, and password changes."""
import json

from flask import Blueprint, abort, jsonify, request

from .handoff import check_signature
from .models import Account, Membership, db, record

bp = Blueprint("api", __name__, url_prefix="/api")


def _payload():
    slug = request.headers.get("X-Belcentra-Condo", "")
    if not check_signature(slug, request.get_data(), request.headers.get("X-Belcentra-Time", ""),
                           request.headers.get("X-Belcentra-Signature", "")):
        abort(403)
    try:
        return slug, json.loads(request.get_data() or b"{}")
    except ValueError:
        abort(400)


def _account(email, name=""):
    email = (email or "").strip().lower()
    if "@" not in email:
        abort(400)
    acc = db.session.scalar(db.select(Account).where(Account.email == email))
    if acc is None:
        acc = Account(email=email, name=name or email)
        db.session.add(acc)
    return acc


@bp.post("/members")
def members():
    """Add or update one person's place in the calling condo (and create their account if new)."""
    slug, data = _payload()
    acc = _account(data.get("email"), data.get("name"))
    if data.get("name"):
        acc.name = data["name"]
    if data.get("password_hash") and not acc.password_hash:
        acc.password_hash = data["password_hash"]  # first sign-in account: keeps the condo's password
        acc.must_change_password = bool(data.get("must_change"))
    m = next((m for m in acc.memberships if m.condo == slug), None)
    if m is None:
        m = Membership(condo=slug)
        acc.memberships.append(m)
    m.role, m.board, m.active = (data.get("role") or "")[:80], bool(data.get("board")), bool(data.get("active", True))
    record("member-sync", acc, f"{slug}: {m.role} active={m.active}")
    db.session.commit()
    return jsonify(ok=True)


@bp.post("/password")
def password():
    """A condo's board (re)sets someone's password. Refused for people who also use other condos:
    one condo's board must not control a sign-in that opens other buildings. People change their own
    password on belcentra.com."""
    slug, data = _payload()
    acc = db.session.scalar(db.select(Account).where(Account.email == (data.get("email") or "").strip().lower()))
    if acc is None or not any(m.condo == slug for m in acc.memberships):
        abort(404)
    if any(m.condo != slug and m.active for m in acc.memberships):
        return jsonify(ok=False, reason="other_condos"), 409
    if not data.get("password_hash"):
        abort(400)
    acc.password_hash = data["password_hash"]
    acc.must_change_password = bool(data.get("must_change"))
    acc.failed_logins, acc.locked_until = 0, None
    record("password-set", acc, f"by {slug}")
    db.session.commit()
    return jsonify(ok=True)
