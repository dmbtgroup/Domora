"""One-time passes from belcentra.com to a condo, and signatures on the condos' calls back."""
import hashlib
import hmac
import secrets
import time

from flask import current_app
from itsdangerous import URLSafeTimedSerializer

SALT = "belcentra-handoff"
PASS_SECONDS = 60


def condo(slug):
    c = current_app.config["CONDOS"].get(slug)
    return dict(c, slug=slug) if c and c.get("key") and c.get("url") else None


def make_pass(account, slug, role=""):
    """A signed pass for one person, one condo, valid a minute, usable once (the condo keeps the nonce)."""
    c = condo(slug)
    data = {"email": account.email, "name": account.name, "aud": slug, "nonce": secrets.token_urlsafe(16),
            "admin": bool(account.is_platform_admin), "lang": account.language or "",
            "two_step": bool(account.totp_enabled)}
    token = URLSafeTimedSerializer(c["key"], salt=SALT).dumps(data)
    return c["url"].rstrip("/") + "/auth/handoff?pass=" + token


def check_signature(slug, body, timestamp, signature):
    """Calls from a condo: HMAC-SHA256 of '<timestamp>.<body>' with that condo's secret, within 5 minutes."""
    c = condo(slug)
    if not c or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    expected = hmac.new(c["key"].encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
