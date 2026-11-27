"""belcentra.com: the public pages, and one sign-in for every condo.

Condos are configured in the CONDOS environment variable (JSON), each with its own secret:
  {"condo5077": {"name": "Condo 5077", "url": "https://condo5077.belcentra.com", "key": "<shared secret>"}}
The same secret is set in that condo's app (BELCENTRA_KEY); it signs one-time passes and API calls.
"""
import json
import os
from datetime import timedelta

from flask import Flask
from markupsafe import Markup, escape

from .models import db


def _database_url():
    url = (os.environ.get("ACCOUNTS_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        folder = "/tmp" if os.environ.get("VERCEL") else os.path.dirname(os.path.abspath(__file__))
        return "sqlite:///" + os.path.join(folder, "accounts.db")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    return url


def _engine_options(url, serverless):
    if not url.startswith("postgresql"):
        return {"pool_pre_ping": True}
    args = {"prepare_threshold": None}
    if "sslmode=" not in url and "@localhost" not in url:
        args["sslmode"] = "require"
    if serverless:
        from sqlalchemy.pool import NullPool
        return {"poolclass": NullPool, "connect_args": args}
    return {"pool_pre_ping": True, "connect_args": args}


def t(en, fr):
    """Both languages; the page shows the visitor's (see static/site.js)."""
    return Markup(f"<span data-en>{escape(en)}</span><span data-fr>{escape(fr)}</span>")


def create_app(test_config=None):
    app = Flask(__name__, static_folder="../static", static_url_path="")
    on_vercel = bool(os.environ.get("VERCEL"))
    url = _database_url()
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
        SQLALCHEMY_DATABASE_URI=url,
        SQLALCHEMY_ENGINE_OPTIONS=_engine_options(url, on_vercel),
        SESSION_COOKIE_SECURE=on_vercel, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        CONDOS=json.loads(os.environ.get("CONDOS") or "{}"),
        MAIL_ENABLED=os.environ.get("MAIL_ENABLED") == "1",
        RESEND_API_KEY=os.environ.get("RESEND_API_KEY", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", "Belcentra <no-reply@belcentra.com>"),
        PUBLIC_URL=os.environ.get("PUBLIC_URL", "https://belcentra.com"),
    )
    if test_config:
        app.config.update(test_config)
    db.init_app(app)
    app.jinja_env.globals.update(t=t)

    from . import api, views
    app.register_blueprint(views.bp)
    app.register_blueprint(api.bp)

    with app.app_context():
        db.create_all()
        _ensure_platform_admin()
    return app


def _ensure_platform_admin():
    """PLATFORM_ADMIN_EMAIL / PLATFORM_ADMIN_PASSWORD: create the Belcentra administrator once, if missing."""
    from .models import Account
    email = (os.environ.get("PLATFORM_ADMIN_EMAIL") or "").strip().lower()
    password = os.environ.get("PLATFORM_ADMIN_PASSWORD") or ""
    if not email or len(password) < 10:
        return
    acc = db.session.scalar(db.select(Account).where(Account.email == email))
    if acc is None:
        acc = Account(email=email, name="Belcentra", is_platform_admin=True, must_change_password=True)
        acc.set_password(password)
        db.session.add(acc)
    acc.is_platform_admin = True
    db.session.commit()
