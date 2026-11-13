import os

from flask import Flask

from .extensions import csrf, db, login_manager


def _database_url(instance_path):
    """Pick the database URL.

    Uses DATABASE_URL (or POSTGRES_URL, which the Vercel/Neon integration sets).
    Without one, falls back to a local SQLite file; on Vercel, where the code
    directory is read-only, that file lives in /tmp and is NOT persistent.
    """
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if url:
        # SQLAlchemy needs an explicit driver for psycopg 3.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url
    folder = "/tmp" if os.environ.get("VERCEL") else instance_path
    return "sqlite:///" + os.path.join(folder, "condo.db")


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass  # read-only filesystem (e.g. serverless)

    on_vercel = bool(os.environ.get("VERCEL"))
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
        SQLALCHEMY_DATABASE_URI=_database_url(app.instance_path),
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        CONDO_NAME=os.environ.get("CONDO_NAME", "Maple Court Condominiums"),
        SESSION_COOKIE_SECURE=on_vercel,
        REMEMBER_COOKIE_SECURE=on_vercel,
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,  # Vercel's request body limit is 4.5 MB
        AUTO_INIT_DB=on_vercel or bool(os.environ.get("AUTO_INIT_DB")),
    )
    if test_config:
        app.config.update(test_config)
    app.config["EPHEMERAL_DB"] = on_vercel and app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    from . import i18n
    i18n.init_app(app)

    from . import models  # noqa: F401  (register models)
    from .views import amenities, announcements, auth, billing, dashboard, directory, documents, fees, importer, insurance, notifications, reimbursements, maintenance, owners, residents, sheet, units, votes

    for bp in (auth.bp, dashboard.bp, units.bp, residents.bp, billing.bp,
               maintenance.bp, announcements.bp, amenities.bp, importer.bp, sheet.bp, owners.bp, directory.bp, documents.bp, insurance.bp, billing.legacy, notifications.bp, reimbursements.bp, fees.bp, votes.bp):
        app.register_blueprint(bp)

    from .cli import register_cli
    register_cli(app)

    from .utils import initials, money, one_line, phone_display, phone_href
    from .navigation import here, safe_back
    app.jinja_env.globals.update(here=here, safe_back=safe_back)
    app.jinja_env.filters.update(money=money, phone=phone_display, tel=phone_href, one_line=one_line,
                                 initials=initials)

    def _unread():
        from flask_login import current_user
        from .notify import unread_count
        try:
            return unread_count(current_user)
        except Exception:
            return 0

    def _nav_board():
        from flask_login import current_user
        from .utils import board_positions_of
        try:
            return bool(current_user.is_authenticated and not current_user.is_admin and board_positions_of(current_user))
        except Exception:
            return False

    @app.context_processor
    def inject_globals():
        from datetime import date

        from .navigation import back_link
        return {"condo_name": app.config["CONDO_NAME"], "ephemeral_db": app.config["EPHEMERAL_DB"],
                "demo": app.config.get("DEMO"),
                "back": back_link(), "unread": _unread(),
                "today": date.today(), "nav_board": _nav_board(),
                "STATUS_LABELS": models.REQUEST_STATUSES, "CATEGORY_LABELS": models.CATEGORY_LABELS, "PRIORITY_LABELS": models.REQUEST_PRIORITIES}

    from flask import render_template

    from .i18n import lazy_gettext as _l
    messages = {400: _l("That request could not be processed."),
                413: _l("That file is too large (the limit is 4 MB)."),
                403: _l("You don't have permission to view this page."),
                404: _l("We couldn't find that page.")}

    @app.route("/manifest.webmanifest")
    def manifest():
        """Lets phones and computers install the portal as an app (Add to Home Screen)."""
        from flask import jsonify, url_for
        from .i18n import gettext as _
        name = app.config["CONDO_NAME"]
        resp = jsonify({
            "name": name, "short_name": "Domora", "description": _("A modern platform for smarter condominium management."),
            "start_url": "/", "scope": "/", "display": "standalone", "orientation": "any",
            "background_color": "#FAF7F2", "theme_color": "#E8612C",
            "icons": [
                {"src": url_for("static", filename="icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
                {"src": url_for("static", filename="icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
                {"src": url_for("static", filename="icons/maskable-512.png"), "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
            ]})
        resp.mimetype = "application/manifest+json"
        return resp

    def handle_error(err):
        code = getattr(err, "code", 500)
        return render_template("error.html", code=code,
                               message=messages.get(code) or _l("Something went wrong.")), code

    for code in messages:
        app.register_error_handler(code, handle_error)

    if app.config["AUTO_INIT_DB"]:
        from .cli import bootstrap
        with app.app_context():
            bootstrap()
    from . import demo
    app.config["DEMO"] = demo.enabled()
    if app.config["DEMO"]:
        with app.app_context():
            db.create_all()
            demo.load_if_empty(app)

    return app
