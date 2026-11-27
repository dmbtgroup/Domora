"""Central sign-in at belcentra.com.

When CENTRAL_LOGIN is on, people sign in once at belcentra.com and arrive here with a one-time pass
(/auth/handoff). This condo keeps belcentra.com current: after any change to accounts or board positions,
and from the regular cron, it sends its complete list of sign-ins (email, name, role, board, active) in one
signed call. belcentra.com never sees condo data. Settings: CENTRAL_LOGIN, CONDO_SLUG, BELCENTRA_KEY
(shared with belcentra.com), ACCOUNTS_URL.
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

from flask import current_app, g, has_app_context
from sqlalchemy import event

from .extensions import db

PASS_SALT = "belcentra-handoff"


def configured(app=None):
    cfg = (app or current_app).config
    return bool(cfg.get("BELCENTRA_KEY") and cfg.get("CONDO_SLUG") and not cfg.get("DEMO"))


def enabled(app=None):
    """Sign-in goes through belcentra.com (otherwise the condo's own sign-in page is used)."""
    return configured(app) and bool((app or current_app).config.get("CENTRAL_LOGIN"))


def _call(path, payload):
    cfg = current_app.config
    body, ts = json.dumps(payload).encode(), str(int(time.time()))
    sig = hmac.new(cfg["BELCENTRA_KEY"].encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(cfg["ACCOUNTS_URL"].rstrip("/") + path, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Belcentra-Condo": cfg["CONDO_SLUG"],
        "X-Belcentra-Time": ts, "X-Belcentra-Signature": sig})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:
        current_app.logger.exception("Call to belcentra.com %s failed", path)
        return None, {}


def members(with_passwords=()):
    """Everyone with a sign-in here, as belcentra.com needs them."""
    from .models import User
    out = []
    for u in db.session.scalars(db.select(User).order_by(User.id)):
        item = {"email": u.email.lower(), "name": u.name, "role": str(u.role_label), "board": bool(u.is_admin),
                "active": bool(u.active)}
        if with_passwords == "all" or u.email.lower() in with_passwords:
            item.update(password_hash=u.password_hash, must_change=bool(u.must_change_password))
        out.append(item)
    return out


def push_members(with_passwords=()):
    """Send the full list. Returns how many belcentra.com recorded, or None if it couldn't be reached."""
    if not configured():
        return None
    status, data = _call("/api/members/sync", {"members": members(with_passwords)})
    return data.get("members") if status == 200 else None


def set_password(user):
    """The board (re)set someone's password here: pass it on. False when belcentra.com refuses because the
    person also uses other condos (they reset it themselves at belcentra.com)."""
    if not configured():
        return True
    status, _ = _call("/api/password", {"email": user.email.lower(), "password_hash": user.password_hash,
                                        "must_change": bool(user.must_change_password)})
    return status != 409


# --- push after any change to accounts or board positions ---------------------------------------------------

def _before_flush(session, _ctx, _instances):
    if not has_app_context():
        return
    from .models import Owner, User
    touched = [o for o in (*session.new, *session.dirty, *session.deleted) if isinstance(o, (User, Owner))]
    if touched:
        g.central_changed = True
        g.setdefault("central_new_users", set()).update(
            o.email.lower() for o in session.new if isinstance(o, User) and o.email)


def _after_request(response):
    if g.pop("central_changed", False) and configured():
        push_members(with_passwords=g.pop("central_new_users", set()))
    return response


def init_app(app):
    if not event.contains(db.session, "before_flush", _before_flush):
        event.listen(db.session, "before_flush", _before_flush)
    app.after_request(_after_request)
