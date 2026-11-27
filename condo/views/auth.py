from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import User
from ..i18n import gettext as _

bp = Blueprint("auth", __name__)


def _safe_next(target):
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/"):
        return None
    return target


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    from .. import central
    if central.enabled() and request.method == "GET" and not request.args.get("local"):
        return redirect(current_app.config["ACCOUNTS_URL"].rstrip("/") + "/sign-in")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.session.scalar(db.select(User).where(User.email == email))
        if user and user.check_password(password):
            if not user.active:
                flash(_("This account has been deactivated. Contact the management office."), "error")
            else:
                login_user(user, remember=bool(request.form.get("remember")))
                if not user.language:  # remember the language they signed in with
                    from ..i18n import current_language
                    user.language = current_language()
                    db.session.commit()
                if user.must_change_password:
                    flash(_("Welcome! Please choose your own password to continue."), "info")
                    return redirect(url_for("auth.account"))
                return redirect(_safe_next(request.args.get("next")) or url_for("dashboard.index"))
        else:
            flash(_("Invalid email or password."), "error")
    return render_template("auth/login.html")


@bp.route("/demo/sign-in", methods=["POST"])
def demo_sign_in():
    """Demo only: sign in as the role the visitor picked, without typing a password."""
    from ..demo import DEMO_ROLES
    if not current_app.config.get("DEMO"):
        abort(404)
    email = dict(DEMO_ROLES).get(request.form.get("role", ""))
    user = db.session.scalar(db.select(User).where(User.email == email)) if email else None
    if user is None or not user.active:
        flash(_("That demo account isn't available right now."), "error")
        return redirect(url_for("auth.login"))
    if current_user.is_authenticated:
        logout_user()
    from .. import sandbox
    sandbox.start()
    login_user(user)
    return redirect(url_for("dashboard.index"))


@bp.route("/auth/handoff")
def handoff():
    """Arriving from belcentra.com with a one-time pass: signed for this condo, under a minute old, unused."""
    from itsdangerous import BadSignature, URLSafeTimedSerializer
    from sqlalchemy.exc import IntegrityError
    from .. import central
    from ..models import HandoffNonce
    if not central.configured():
        abort(404)
    try:
        data = URLSafeTimedSerializer(current_app.config["BELCENTRA_KEY"], salt=central.PASS_SALT).loads(
            request.args.get("pass", ""), max_age=60)
    except BadSignature:
        data = None
    if not data or data.get("aud") != current_app.config["CONDO_SLUG"] or not data.get("nonce"):
        flash(_("That sign-in link has expired. Please sign in again."), "error")
        return redirect(current_app.config["ACCOUNTS_URL"].rstrip("/") + "/sign-in")
    try:
        db.session.add(HandoffNonce(nonce=str(data["nonce"])[:64]))
        db.session.commit()
    except IntegrityError:  # already used
        db.session.rollback()
        abort(403)
    email = (data.get("email") or "").strip().lower()
    user = db.session.scalar(db.select(User).where(db.func.lower(User.email) == email))
    if user is None and data.get("admin"):
        # A Belcentra administrator: gets an administrator account here on first visit.
        user = User(email=email, name=data.get("name") or email, role="admin")
        user.password_hash = "!central"  # never signs in with a password here
        db.session.add(user)
        db.session.commit()
    if user is None or not user.active:
        abort(403)
    if current_user.is_authenticated:
        logout_user()
    login_user(user)
    session["via_central"] = True  # signing out here also signs out of belcentra.com
    session["two_step_on"] = bool(data.get("two_step"))
    if not user.language and data.get("lang") in ("en", "fr"):
        user.language = data["lang"]
        db.session.commit()
    return redirect(url_for("dashboard.index"))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    via_central = session.pop("via_central", False)
    logout_user()
    from .. import central
    if central.enabled() or via_central:
        return redirect(current_app.config["ACCOUNTS_URL"].rstrip("/") + "/signed-out")
    if current_app.config.get("DEMO"):
        from .. import sandbox
        sandbox.end()
        flash(_("You left the demo. Everything you changed has been reset."), "info")
        return redirect(url_for("auth.login"))
    flash(_("You have been signed out."), "info")
    return redirect(url_for("dashboard.index"))


@bp.before_app_request
def require_new_password():
    """Someone whose password was set by the board must choose their own before going on."""
    from .. import central
    if central.enabled():
        return None  # belcentra.com makes them choose their own password
    if (current_user.is_authenticated and getattr(current_user, "must_change_password", False)
            and request.endpoint not in ("auth.account", "auth.logout", "static")):
        flash(_("Please choose your own password to continue."), "info")
        return redirect(url_for("auth.account"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        phone = request.form.get("phone", "").strip()
        from .. import central
        if (new or current) and central.enabled():
            return redirect(current_app.config["ACCOUNTS_URL"].rstrip("/") + "/account/password")
        if (new or current) and current_app.config.get("DEMO"):
            flash(_("Passwords can't be changed in the demo."), "error")
            return redirect(url_for("auth.account"))
        if new or current:
            if not current_user.check_password(current):
                flash(_("Current password is incorrect."), "error")
                return render_template("auth/account.html"), 400
            if len(new) < 8:
                flash(_("New password must be at least 8 characters."), "error")
                return render_template("auth/account.html"), 400
            if new != confirm:
                flash(_("New passwords do not match."), "error")
                return render_template("auth/account.html"), 400
            if current_user.must_change_password and new == current:
                flash(_("Choose a new password, different from the one you were given."), "error")
                return render_template("auth/account.html"), 400
            current_user.set_password(new)
            current_user.must_change_password = False
        current_user.phone = phone or None
        db.session.commit()
        flash(_("Account updated."), "success")
        return redirect(url_for("auth.account"))
    return render_template("auth/account.html")


@bp.route("/language/<code>", methods=["GET", "POST"])
def set_language(code):
    """English / Français switch: remembered on the account and in a cookie (for the sign-in page)."""
    from ..i18n import LANGUAGES
    from ..navigation import safe_back
    if code not in LANGUAGES:
        abort(404)
    if current_user.is_authenticated:
        current_user.language = code
        db.session.commit()
    target = safe_back() or (request.referrer if (request.referrer or "").startswith(request.host_url) else None) \
        or url_for("dashboard.index")
    resp = redirect(target)
    resp.set_cookie("lang", code, max_age=3600 * 24 * 365 * 2, samesite="Lax", secure=request.is_secure)
    return resp
