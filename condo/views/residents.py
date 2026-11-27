import re
import secrets

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import ACCOUNT_ROLES, RESIDENT_ROLES, ROLE_LABELS, ROLES, STAFF_PERMISSIONS, Unit, User
from ..navigation import safe_back
from ..utils import admin_required, board_required
from ..i18n import gettext as _

bp = Blueprint("residents", __name__, url_prefix="/residents")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _apply_form(user, form, creating):
    errors = []
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    role = form.get("role", "owner")
    unit_id = form.get("unit_id") or None
    password = form.get("password", "")
    if current_app.config.get("DEMO"):  # passwords are fixed in the demo
        from ..demo import DEMO_PASSWORD
        password = DEMO_PASSWORD if creating else ""

    if not name:
        errors.append(_("Name is required."))
    if not EMAIL_RE.match(email):
        errors.append(_("A valid email is required."))
    else:
        existing = db.session.scalar(db.select(User).where(User.email == email))
        if existing and existing.id != user.id:
            errors.append(_("Another account already uses that email."))
    allowed_roles = set(ACCOUNT_ROLES) | ({"admin"} if user.role == "admin" else set())
    if role not in allowed_roles:
        errors.append(_("Choose Owner or Tenant. Managing the building comes from a board position."))
    unit = None
    if role in RESIDENT_ROLES:
        unit = db.session.get(Unit, int(unit_id)) if str(unit_id or "").isdigit() else None
        if unit is None:
            errors.append(_("Choose the unit this person owns or rents."))
    if creating and len(password) < 8:
        errors.append(_("Password must be at least 8 characters."))
    elif password and len(password) < 8:
        errors.append(_("New password must be at least 8 characters."))
    if not creating and user.id == current_user.id:
        if user.role == "admin" and role != "admin" and not user.is_board:
            errors.append(_("Assign yourself your board position in Directory > Board first, or you'd lose access."))
        if not form.get("active"):
            errors.append(_("You cannot deactivate your own account."))
    if errors:
        return errors

    user.name = name
    user.email = email
    user.phone = form.get("phone", "").strip() or None
    user.role = role
    user.unit = unit
    if role == "staff":
        user.job_title = form.get("job_title", "").strip()[:120] or None
        user.permission_keys = form.getlist("perms") if hasattr(form, "getlist") else form.get("perms", [])
    else:
        user.job_title, user.permissions = None, None
    user.active = bool(form.get("active")) if not creating else True
    if password:
        user.set_password(password)
        user.must_change_password = bool(form.get("must_change")) and user.id != current_user.id
    return []


# Unambiguous characters only (no 0/O, 1/l/I), grouped for reading aloud or typing from a phone.
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _demo_locked():
    if current_app.config.get("DEMO"):
        flash(_("Passwords can't be changed in the demo."), "error")
        return True
    return False


def generate_password():
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))


@bp.route("/<int:user_id>/password", methods=["GET", "POST"])
@board_required
def set_password(user_id):
    """The board sets (or generates) a password for one account."""
    user = db.session.get(User, user_id) or abort(404)
    if _demo_locked():
        return redirect(url_for("residents.edit", user_id=user.id))
    if user.id == current_user.id:
        flash(_("Change your own password from My profile."), "info")
        return redirect(url_for("auth.account"))
    if request.method == "POST":
        generated = request.form.get("action") == "generate"
        password = generate_password() if generated else request.form.get("password", "")
        if not generated:
            if len(password) < 8:
                flash(_("The password must be at least 8 characters."), "error")
                return render_template("residents/password.html", user=user, form=request.form), 400
            if password != request.form.get("confirm", ""):
                flash(_("The two passwords don't match."), "error")
                return render_template("residents/password.html", user=user, form=request.form), 400
        user.set_password(password)
        user.must_change_password = bool(request.form.get("must_change"))
        db.session.commit()
        if not _central_password(user):
            return redirect(url_for("residents.edit", user_id=user.id))
        return render_template("residents/password_done.html", user=user,
                               password=password if generated else None)
    return render_template("residents/password.html", user=user, form={"must_change": True})


@bp.route("/<int:user_id>/reset-password", methods=["POST"])
@board_required
def reset_password(user_id):
    """One click: a temporary password, shown once, that the person must replace at their next sign-in."""
    user = db.session.get(User, user_id) or abort(404)
    if _demo_locked():
        return redirect(url_for("residents.edit", user_id=user.id))
    if user.id == current_user.id:
        flash(_("Change your own password from My profile."), "info")
        return redirect(url_for("auth.account"))
    password = generate_password()
    user.set_password(password)
    user.must_change_password = True
    db.session.commit()
    if not _central_password(user):
        return redirect(url_for("residents.edit", user_id=user.id))
    return render_template("residents/password_done.html", user=user, password=password, reset=True)


def _central_password(user):
    """Pass the new password on to belcentra.com; refused when this person's sign-in also opens other condos."""
    from .. import central
    if central.set_password(user):
        return True
    flash(_("%(name)s also uses Belcentra for other condos, so only they can change their password: "
            "“Forgot password?” on the Belcentra sign-in page.", name=user.name), "error")
    return False


def _role_choices(user):
    choices = dict(RESIDENT_ROLES)
    if getattr(user, "role", None) == "admin":
        choices["admin"] = ROLE_LABELS["admin"]
    return choices


def _render(user, form, creating, status=200):
    role = form.get("role") or user.role
    mode = "staff" if role == "staff" else "resident"
    return render_template("residents/form.html", user=user, form=form, units=_units(), roles=_role_choices(user),
                           creating=creating, mode=mode, perms=STAFF_PERMISSIONS), status


def _units():
    return db.session.scalars(db.select(Unit).order_by(Unit.number)).all()


@bp.route("/")
@board_required
def index():
    """Old address of the accounts list; it now lives in the Directory."""
    return redirect(url_for("directory.accounts"), code=301)


@bp.route("/new", methods=["GET", "POST"])
@board_required
def new():
    user = User()
    if request.method == "POST":
        errors = _apply_form(user, request.form, creating=True)
        if not errors:
            db.session.add(user)
            db.session.commit()
            flash(_("Account for %(name)s created.", name=user.name), "success")
            return redirect(safe_back() or url_for("directory.accounts"))
        for e in errors:
            flash(e, "error")
        return _render(user, request.form, True, 400)
    role = request.args.get("role", "staff")  # Accounts tab adds staff; Owners/Tenants tabs pass owner/tenant
    form = {"unit_id": request.args.get("unit_id", ""), "role": role if role in ACCOUNT_ROLES else "staff",
            "name": request.args.get("name", ""), "email": request.args.get("email", ""),
            "phone": request.args.get("phone", ""), "must_change": True, "perms": []}
    return _render(user, form, True)


@bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@board_required
def edit(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if request.method == "POST":
        errors = _apply_form(user, request.form, creating=False)
        if not errors:
            db.session.commit()
            flash(_("%(name)s updated.", name=user.name), "success")
            return redirect(safe_back() or url_for("directory.accounts"))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return _render(user, request.form, False, 400)
    form = {"name": user.name, "email": user.email, "phone": user.phone or "", "role": user.role,
            "unit_id": str(user.unit_id or ""), "active": user.active, "job_title": user.job_title or "",
            "perms": user.permission_keys}
    return _render(user, form, False)
