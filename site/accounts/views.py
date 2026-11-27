import hashlib
import secrets
from datetime import timedelta
from functools import wraps

import pyotp
import segno
from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, session,
                   url_for)
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import t
from .handoff import condo, make_pass
from .mail import send
from .models import Account, db, record, utcnow

bp = Blueprint("views", __name__)
MAX_FAILURES, LOCK_MINUTES = 5, 15
DEVICE_COOKIE, DEVICE_DAYS = "bc_device", 30


# --- plumbing ----------------------------------------------------------------------------------------------

@bp.before_app_request
def _csrf():
    session.setdefault("csrf", secrets.token_urlsafe(24))
    if request.method == "POST" and request.blueprint == "views":
        if not secrets.compare_digest(request.form.get("csrf", ""), session["csrf"]):
            abort(400)


@bp.after_app_request
def _no_store(response):
    """Pages seen while signed in are never kept by the browser (Back after signing out shows nothing)."""
    if (session.get("account_id") or session.get("pending_id")) and request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


@bp.app_context_processor
def _globals():
    return {"csrf": session.get("csrf", ""), "me": _current()}


def _current():
    acc_id = session.get("account_id")
    acc = db.session.get(Account, acc_id) if acc_id else None
    return acc if acc and acc.active else None


def signed_in(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if _current() is None:
            return redirect(url_for("views.sign_in", next=request.path))
        return view(*a, **kw)
    return wrapper


def _pending():
    acc_id = session.get("pending_id")
    return db.session.get(Account, acc_id) if acc_id else None


def _device_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="belcentra-device")


def _device_mark(acc):
    # Tied to the current two-step key: a new key (or two-step turned off) forgets every saved device.
    return hashlib.sha256((acc.totp_secret or "").encode()).hexdigest()[:16]


def _device_saved(acc):
    try:
        data = _device_serializer().loads(request.cookies.get(DEVICE_COOKIE, ""), max_age=DEVICE_DAYS * 86400)
    except BadSignature:
        return False
    return data.get("id") == acc.id and secrets.compare_digest(data.get("k", ""), _device_mark(acc))


def _finish(acc, save_device=False):
    """Password (and code) accepted: open the session, then whatever is still required."""
    remember = session.get("remember", False)
    session.clear()
    session["csrf"] = secrets.token_urlsafe(24)
    session["account_id"] = acc.id
    session.permanent = remember
    acc.last_sign_in = utcnow()
    record("sign-in", acc)
    db.session.commit()
    if acc.needs_two_step and not acc.totp_enabled:
        response = redirect(url_for("views.two_step_setup"))
    elif acc.must_change_password:
        response = redirect(url_for("views.change_password"))
    else:
        response = redirect(url_for("views.condos"))
    if save_device:
        response.set_cookie(DEVICE_COOKIE, _device_serializer().dumps({"id": acc.id, "k": _device_mark(acc)}),
                            max_age=DEVICE_DAYS * 86400, secure=current_app.config["SESSION_COOKIE_SECURE"],
                            httponly=True, samesite="Lax")
    return response


# --- public pages --------------------------------------------------------------------------------------------

@bp.get("/")
def home():
    return render_template("home.html")


@bp.get("/pricing")
def pricing():
    return render_template("pricing.html")


# --- sign-in -------------------------------------------------------------------------------------------------

@bp.route("/sign-in", methods=["GET", "POST"])
def sign_in():
    if _current():
        return redirect(url_for("views.condos"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        acc = db.session.scalar(db.select(Account).where(Account.email == email))
        now = utcnow()
        if acc and acc.locked_until and acc.locked_until > now:
            flash(t("Too many attempts. Try again in a few minutes.", "Trop de tentatives. Réessayez dans quelques minutes."), "error")
            return render_template("sign_in.html", email=email), 429
        if not acc or not acc.active or not acc.check_password(request.form.get("password", "")):
            if acc:
                acc.failed_logins += 1
                if acc.failed_logins >= MAX_FAILURES:
                    acc.locked_until, acc.failed_logins = now + timedelta(minutes=LOCK_MINUTES), 0
                    record("locked", acc)
                db.session.commit()
            flash(t("Invalid email or password.", "Courriel ou mot de passe invalide."), "error")
            return render_template("sign_in.html", email=email), 400
        acc.failed_logins, acc.locked_until = 0, None
        db.session.commit()
        session.clear()
        session["csrf"] = secrets.token_urlsafe(24)
        session["remember"] = bool(request.form.get("remember"))
        if acc.totp_enabled and not _device_saved(acc):
            session["pending_id"] = acc.id
            return redirect(url_for("views.sign_in_code"))
        return _finish(acc)
    return render_template("sign_in.html", email="")


@bp.route("/sign-in/code", methods=["GET", "POST"])
def sign_in_code():
    acc = _pending()
    if acc is None:
        return redirect(url_for("views.sign_in"))
    if request.method == "POST":
        code = request.form.get("code", "").replace(" ", "")
        ok = (code.isdigit() and pyotp.TOTP(acc.totp_secret).verify(code, valid_window=1)) or acc.use_recovery_code(code)
        if ok:
            return _finish(acc, save_device=bool(request.form.get("save_device")))
        acc.failed_logins += 1
        if acc.failed_logins >= MAX_FAILURES:
            acc.locked_until, acc.failed_logins = utcnow() + timedelta(minutes=LOCK_MINUTES), 0
            session.clear()
            record("locked", acc)
            db.session.commit()
            flash(t("Too many attempts. Try again in a few minutes.", "Trop de tentatives. Réessayez dans quelques minutes."), "error")
            return redirect(url_for("views.sign_in"))
        db.session.commit()
        flash(t("That code didn't work. Try the newest one.", "Ce code n’a pas fonctionné. Essayez le plus récent."), "error")
    return render_template("code.html")


@bp.route("/two-step/setup", methods=["GET", "POST"])
@signed_in
def two_step_setup():
    acc = _current()
    if request.method == "GET" or "totp_setup" not in session:
        session["totp_setup"] = pyotp.random_base32()  # a fresh key each time the page opens
    secret = session["totp_setup"]
    if request.method == "POST":
        if pyotp.TOTP(secret).verify(request.form.get("code", "").replace(" ", ""), valid_window=1):
            acc.totp_secret, acc.totp_enabled = secret, True
            codes = acc.new_recovery_codes()
            session.pop("totp_setup", None)
            record("two-step-on", acc)
            db.session.commit()
            return render_template("recovery_codes.html", codes=codes)
        flash(t("That code didn't work. Check your phone's time and try the newest code.",
                "Ce code n’a pas fonctionné. Vérifiez l’heure de votre téléphone et essayez le plus récent."), "error")
    uri = pyotp.TOTP(secret).provisioning_uri(name=acc.email, issuer_name="Belcentra")
    qr = segno.make(uri, error="m").svg_inline(scale=5, dark="#2A211B", light="#ffffff", omitsize=True)
    key = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)).lower()  # lowercase: no O/0 mix-up
    return render_template("two_step_setup.html", qr=qr, secret=key, required=acc.needs_two_step)


@bp.post("/sign-out")
def sign_out():
    acc = _current()
    if acc:
        record("sign-out", acc)
        db.session.commit()
    session.clear()
    flash(t("You are signed out.", "Vous êtes déconnecté."), "info")
    return redirect(url_for("views.sign_in"))


@bp.get("/signed-out")
def signed_out():
    """Where a condo sends people when they sign out there: end the Belcentra session too."""
    acc = _current()
    if acc:
        record("sign-out", acc, "from a condo")
        db.session.commit()
    session.clear()
    flash(t("You are signed out.", "Vous êtes déconnecté."), "info")
    return redirect(url_for("views.sign_in"))


# --- condos ----------------------------------------------------------------------------------------------------

def _gates(acc):
    """What must happen before opening a condo."""
    if acc.needs_two_step and not acc.totp_enabled:
        return redirect(url_for("views.two_step_setup"))
    if acc.must_change_password:
        return redirect(url_for("views.change_password"))
    return None


def _choices(acc):
    """(slug, name, role) of the condos this person may open."""
    configured = current_app.config["CONDOS"]
    if acc.is_platform_admin:
        mine = {m.condo: m.role for m in acc.active_memberships()}
        return [(slug, c["name"], mine.get(slug) or "Belcentra") for slug, c in sorted(configured.items())]
    return [(m.condo, configured[m.condo]["name"], m.role) for m in acc.active_memberships() if m.condo in configured]


@bp.get("/condos")
@signed_in
def condos():
    acc = _current()
    gate = _gates(acc)
    if gate:
        return gate
    choices = _choices(acc)
    if len(choices) == 1 and not request.args.get("all"):
        return redirect(url_for("views.go", slug=choices[0][0]))
    return render_template("condos.html", choices=choices)


@bp.get("/go/<slug>")
@signed_in
def go(slug):
    acc = _current()
    gate = _gates(acc)
    if gate:
        return gate
    allowed = {c[0]: c[2] for c in _choices(acc)}
    if slug not in allowed or condo(slug) is None:
        abort(403)
    record("handover", acc, slug)
    db.session.commit()
    return redirect(make_pass(acc, slug, allowed[slug]))


# --- passwords -------------------------------------------------------------------------------------------------

@bp.route("/account/password", methods=["GET", "POST"])
@signed_in
def change_password():
    acc = _current()
    if request.method == "POST":
        current, new, confirm = (request.form.get(k, "") for k in ("current", "new", "confirm"))
        if not acc.check_password(current):
            flash(t("Your current password is incorrect.", "Votre mot de passe actuel est incorrect."), "error")
        elif len(new) < 10:
            flash(t("Choose at least 10 characters.", "Choisissez au moins 10 caractères."), "error")
        elif new != confirm:
            flash(t("The two new passwords don't match.", "Les deux nouveaux mots de passe ne correspondent pas."), "error")
        elif new == current:
            flash(t("Choose a password different from the current one.", "Choisissez un mot de passe différent de l’actuel."), "error")
        else:
            acc.set_password(new)
            acc.must_change_password = False
            record("password-changed", acc)
            db.session.commit()
            send(acc.email, "Belcentra: your password was changed / votre mot de passe a été modifié",
                 "Your Belcentra password was just changed. If it wasn't you, contact support@belcentra.com.\n\n"
                 "Votre mot de passe Belcentra vient d’être modifié. Si ce n’était pas vous, écrivez à support@belcentra.com.")
            flash(t("Password changed.", "Mot de passe modifié."), "success")
            return redirect(url_for("views.condos"))
    return render_template("change_password.html", forced=acc.must_change_password)


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="belcentra-reset")


@bp.route("/password/forgot", methods=["GET", "POST"])
def forgot():
    if not current_app.config.get("MAIL_ENABLED"):
        return render_template("forgot.html", mail_off=True)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        acc = db.session.scalar(db.select(Account).where(Account.email == email))
        if acc and acc.active:
            # The token carries part of the current hash, so it stops working once used.
            token = _reset_serializer().dumps({"id": acc.id, "h": (acc.password_hash or "")[-12:]})
            link = current_app.config["PUBLIC_URL"].rstrip("/") + url_for("views.reset", token=token)
            send(acc.email, "Belcentra: reset your password / réinitialiser votre mot de passe",
                 f"To choose a new password, open this link within an hour:\n{link}\n\n"
                 f"Pour choisir un nouveau mot de passe, ouvrez ce lien dans l’heure :\n{link}\n\n"
                 "If you didn't ask for this, ignore this email. / Si vous n’avez rien demandé, ignorez ce courriel.")
            record("reset-requested", acc)
            db.session.commit()
        flash(t("If that email has a Belcentra account, we've sent a link to reset the password.",
                "Si ce courriel a un compte Belcentra, nous avons envoyé un lien pour réinitialiser le mot de passe."), "info")
        return redirect(url_for("views.sign_in"))
    return render_template("forgot.html")


@bp.route("/password/reset/<token>", methods=["GET", "POST"])
def reset(token):
    try:
        data = _reset_serializer().loads(token, max_age=3600)
    except BadSignature:
        data = None
    acc = db.session.get(Account, data["id"]) if data else None
    if acc is None or (acc.password_hash or "")[-12:] != data["h"]:
        flash(t("That link has expired. Ask for a new one.", "Ce lien a expiré. Demandez-en un nouveau."), "error")
        return redirect(url_for("views.forgot"))
    if request.method == "POST":
        new, confirm = request.form.get("new", ""), request.form.get("confirm", "")
        if len(new) < 10:
            flash(t("Choose at least 10 characters.", "Choisissez au moins 10 caractères."), "error")
        elif new != confirm:
            flash(t("The two passwords don't match.", "Les deux mots de passe ne correspondent pas."), "error")
        else:
            acc.set_password(new)
            acc.must_change_password, acc.failed_logins, acc.locked_until = False, 0, None
            record("password-reset", acc)
            db.session.commit()
            flash(t("Password changed. You can sign in now.", "Mot de passe modifié. Vous pouvez vous connecter."), "success")
            return redirect(url_for("views.sign_in"))
    return render_template("reset.html")
