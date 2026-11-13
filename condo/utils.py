from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import abort
from flask_login import current_user, login_required
from .i18n import gettext as _


def money(cents):
    """Format integer cents as money in the current language, e.g. -1250 -> '-$12.50' (or '-12,50 $')."""
    try:
        from .i18n import money as localized
        return localized(cents)
    except RuntimeError:  # outside a request/app context
        cents = int(cents or 0)
        return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def parse_money(value):
    """Parse a user-entered amount ('1,234.50', '$20') into integer cents.

    Raises ValueError for empty, malformed or non-positive input.
    """
    cleaned = (value or "").strip().replace("$", "").replace("\u00a0", " ").replace("\u202f", " ").strip()
    if "," in cleaned and "." not in cleaned and len(cleaned.rsplit(",", 1)[1]) <= 2:
        cleaned = cleaned.replace(" ", "").replace(",", ".")  # French: 1 234,50
    cleaned = cleaned.replace(",", "").replace(" ", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(_("Enter a valid amount."))
    if amount <= 0:
        raise ValueError(_("Amount must be greater than zero."))
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError(_("Amount can have at most two decimal places."))
    return int(amount * 100)


def parse_date(value, field="Date"):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise ValueError(_("%(field)s must be a valid date (YYYY-MM-DD).", field=field))


def parse_datetime(value, field="Time"):
    try:
        return datetime.fromisoformat((value or "").strip())
    except ValueError:
        raise ValueError(_("%(field)s must be a valid date and time.", field=field))


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def board_positions_of(user):
    """Board positions held by the person whose contact email matches this account."""
    if not user or not getattr(user, "is_authenticated", False) or not user.email:
        return []
    return list(user.board_positions)


def board_has_accounts():
    """True once at least one board member can sign in (has an active account)."""
    from .extensions import db
    from .models import Owner, User
    emails = [e.lower() for e in db.session.scalars(
        db.select(Owner.email).where(Owner.board_roles.is_not(None), Owner.email.is_not(None)))]
    if not emails:
        return False
    return db.session.scalar(db.select(db.func.count(User.id)).where(
        db.func.lower(User.email).in_(emails), User.active.is_(True))) > 0


def can_manage_accounts(user=None):
    """Website accounts are managed by the board. Until a board member has an account,
    managers keep access so the first board accounts can be created."""
    user = user or current_user
    if not user.is_authenticated:
        return False
    if board_positions_of(user):
        return True
    return user.is_temp_admin and not board_has_accounts()


def board_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not can_manage_accounts():
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def permission_required(permission):
    """Board members, or staff given this permission."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not current_user.can(permission):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def can_access_unit(unit_id):
    return current_user.is_admin or (unit_id is not None and current_user.unit_id == unit_id)


def can_see_unit_finances(unit_id):
    """A unit's statement, balance and payments: the board, accounting staff, and the unit's owners.
    Tenants never see the unit's finances."""
    if current_user.is_admin or current_user.can("accounting"):
        return True
    return current_user.role != "tenant" and unit_id is not None and current_user.unit_id == unit_id


def phone_display(value):
    """Format North American numbers as (514) 566-4383; leave anything else as typed."""
    import re
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return value


def phone_href(value):
    import re
    digits = re.sub(r"[^\d+]", "", value or "")
    return f"tel:{digits}"


def one_line(value):
    """Join a multi-line address into one line: '1000 Rue Levert, Apt. 715, H3E 0G3 QC, Canada'."""
    if not value:
        return ""
    parts = [p.strip().rstrip(",") for p in str(value).replace("\r", "").split("\n")]
    return ", ".join(p for p in parts if p)


def initials(name):
    words = [w for w in (name or "").replace("/", " ").split() if w[:1].isalpha()]
    if not words:
        return "?"
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()
