import calendar
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (CHARGE_KINDS, COST_CATEGORIES, PAYMENT_METHOD_LABELS, PAYMENT_METHODS, SYSTEM_CHARGE_KINDS, Charge, Expense, Payment,
                      BankBalance, Setting, Unit, bank_summary, guess_cost_category)
from ..utils import admin_required, can_access_unit, permission_required, parse_date, parse_money
from ..i18n import fdate, gettext as _

# Shown as "Accounting"; the blueprint keeps its old name so existing links keep working.
bp = Blueprint("billing", __name__, url_prefix="/accounting")
legacy = Blueprint("billing_legacy", __name__)


@legacy.route("/billing/", defaults={"rest": ""})
@legacy.route("/billing/<path:rest>")
def old_address(rest):
    """Billing was renamed Accounting; keep old links working."""
    query = request.query_string.decode()
    return redirect(f"/accounting/{rest}" + (f"?{query}" if query else ""), code=301)


def resolve_period(args, today=None):
    """Read ?view=year|month&year=&month= into a period dict with start/end dates and neighbours."""
    today = today or date.today()
    view = "month" if args.get("view") == "month" else "year"
    try:
        year = int(args.get("year", today.year))
        month = int(args.get("month", today.month))
        if not (1900 < year < 2200 and 1 <= month <= 12):
            raise ValueError
    except ValueError:
        year, month = today.year, today.month
    if view == "year":
        start, end = date(year, 1, 1), date(year, 12, 31)
        label = str(year)
        prev, nxt = {"view": "year", "year": year - 1}, {"view": "year", "year": year + 1}
    else:
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        label = fdate(start, "monthyear").capitalize()
        py, pm = (year, month - 1) if month > 1 else (year - 1, 12)
        ny, nm = (year, month + 1) if month < 12 else (year + 1, 1)
        prev, nxt = {"view": "month", "year": py, "month": pm}, {"view": "month", "year": ny, "month": nm}
    return {"view": view, "year": year, "month": month, "start": start, "end": end, "label": label,
            "prev": prev, "next": nxt}


class OwnerPaidCost:
    """A purchase credit shown in the Costs panel: an owner paid a building cost themselves."""
    origin = "credit"
    approved_by = None

    def __init__(self, payment):
        self.payment = payment
        self.spent_on = payment.paid_on
        self.sort_id = -payment.id
        text = payment.notes or "Purchase"
        self.description = text.split(" — ", 1)[1] if " — " in text else text
        self.cost_cents = payment.amount_cents
        self.category_key = guess_cost_category(self.description)
        if self.category_key == "other":
            self.category_key = "maintenance"
        self.category_label = COST_CATEGORIES[self.category_key]
        self.paid_by = f"Paid by owner of unit {payment.unit.number}, credited to their account"
        self.summary = self.description.split(",", 1)[0]
        self.id = payment.id


def _sum(column, *where):
    return db.session.scalar(db.select(db.func.coalesce(db.func.sum(column), 0)).where(*where))


def generate_dues(year, month, due_day=1):
    """Create a monthly dues charge for every unit with a fee. Idempotent per period.

    Returns (created_count, skipped_count).
    """
    period = f"{year:04d}-{month:02d}"
    due = date(year, month, min(due_day, calendar.monthrange(year, month)[1]))
    label = f"Monthly dues — {calendar.month_name[month]} {year}"
    existing = set(db.session.scalars(
        db.select(Charge.unit_id).where(Charge.kind == "dues", Charge.period == period)
    ))
    created = skipped = 0
    for unit in db.session.scalars(db.select(Unit).where(Unit.monthly_fee_cents > 0)):
        if unit.id in existing:
            skipped += 1
            continue
        db.session.add(Charge(unit_id=unit.id, kind="dues", period=period, description=label,
                              amount_cents=unit.monthly_fee_cents, due_date=due))
        created += 1
    db.session.commit()
    return created, skipped


def accounting_scope(user=None):
    """'all' for managers and the board, 'own' for other owners (their unit only), else None."""
    from ..utils import board_positions_of
    user = user or current_user
    if user.is_admin or board_positions_of(user) or user.can("accounting"):
        return "all"
    if user.role == "owner" and user.unit_id is not None:
        return "own"
    return None


@bp.route("/")
@login_required
def index():
    scope = accounting_scope()
    if scope is None:  # tenants keep their unit statement
        if current_user.unit_id:
            return redirect(url_for("billing.ledger", unit_id=current_user.unit_id))
        abort(403)
    period = resolve_period(request.args)
    if scope == "own":
        unit = current_user.unit
    else:
        raw = request.args.get("unit", "")
        unit = db.session.get(Unit, int(raw)) if raw.isdigit() else None
    if unit is not None and scope == "all":
        for key in ("prev", "next"):
            period[key] = {**period[key], "unit": unit.id}
    period["unit_args"] = {"unit": unit.id} if unit is not None and scope == "all" else {}
    if unit is not None:
        return _unit_view(unit, period, scope)
    start, end = period["start"], period["end"]
    payments = db.session.scalars(db.select(Payment).where(Payment.paid_on.between(start, end))
                                  .order_by(Payment.paid_on.desc(), Payment.id.desc())).all()
    costs = db.session.scalars(db.select(Expense).where(Expense.spent_on.between(start, end))
                               .order_by(Expense.spent_on.desc(), Expense.id.desc())).all()
    # An owner who buys something for the building is credited on their unit: it's a payment from the
    # unit *and* a building cost. Listing it in both keeps the net equal to the real cash movement.
    owner_paid = [OwnerPaidCost(p) for p in payments if p.method == "purchase credit"]
    costs = sorted(list(costs) + owner_paid, key=lambda x: (x.spent_on, x.sort_id), reverse=True)
    paid_total = sum(p.amount_cents for p in payments)
    cost_total = sum(x.cost_cents for x in costs)
    by_category = {}
    for x in costs:
        by_category[x.category_key] = by_category.get(x.category_key, 0) + x.cost_cents
    categories = [(COST_CATEGORIES[k], v) for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])]

    months = []
    if period["view"] == "year":
        for m in range(1, 13):
            ms, me = date(period["year"], m, 1), date(period["year"], m, calendar.monthrange(period["year"], m)[1])
            inc = sum(p.amount_cents for p in payments if ms <= p.paid_on <= me)
            out = sum(x.cost_cents for x in costs if ms <= x.spent_on <= me)
            months.append({"month": m, "name": fdate(ms, "monthname").capitalize(), "payments": inc, "costs": out, "net": inc - out})

    units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all()
    rows = []
    for u in units:
        rows.append({"unit": u,
                     "billed": _sum(Charge.amount_cents, Charge.unit_id == u.id, Charge.kind != "opening",
                                    Charge.due_date.between(start, end)),
                     "paid": sum(p.amount_cents for p in payments if p.unit_id == u.id),
                     "balance": u.balance})
    outstanding = sum(r["balance"] for r in rows if r["balance"] > 0)
    sheet = Setting.get("sheet") or {}
    from ..models import Reimbursement
    pending = db.session.scalar(db.select(db.func.count(Reimbursement.id)).where(
        Reimbursement.status.in_(("submitted", "approved"))))
    bank = bank_summary() if current_user.can("accounting") else None
    sheet_clashes = 0
    if current_user.is_admin:
        from .. import sheet_sync
        from .sheet import _latest_plan
        plan = _latest_plan()
        sheet_clashes = len(sheet_sync.clashing_entries(plan)) if plan else 0
    return render_template("billing/index.html", period=period, pending_reimbursements=pending, scope=scope,
                           sheet_clashes=sheet_clashes, bank=bank,
                           all_units=units, unit=None, payments=payments, costs=costs,
                           paid_total=paid_total, cost_total=cost_total, categories=categories, months=months,
                           rows=rows, outstanding=outstanding, today=date.today(), sheet=sheet,
                           last_sync=Setting.get("sheet_last_sync"))


def _unit_view(unit, period, scope):
    """One unit's accounting for the period: what it was billed, what it paid, its balance."""
    start, end = period["start"], period["end"]
    charges = db.session.scalars(db.select(Charge).where(Charge.unit_id == unit.id, Charge.due_date.between(start, end))
                                 .order_by(Charge.due_date.desc(), Charge.id.desc())).all()
    payments = db.session.scalars(db.select(Payment).where(Payment.unit_id == unit.id, Payment.paid_on.between(start, end))
                                  .order_by(Payment.paid_on.desc(), Payment.id.desc())).all()
    billed = sum(c.amount_cents for c in charges if c.kind != "opening")
    opening = sum(c.amount_cents for c in charges if c.kind == "opening")
    paid = sum(p.amount_cents for p in payments)
    months = []
    if period["view"] == "year":
        for m in range(1, 13):
            ms, me = date(period["year"], m, 1), date(period["year"], m, calendar.monthrange(period["year"], m)[1])
            b = sum(c.amount_cents for c in charges if ms <= c.due_date <= me and c.kind != "opening")
            pd = sum(p.amount_cents for p in payments if ms <= p.paid_on <= me)
            months.append({"month": m, "name": fdate(ms, "monthname").capitalize(), "billed": b, "paid": pd})
    all_units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all() if scope == "all" else []
    from .fees import unit_summary
    return render_template("billing/unit_view.html", fees=unit_summary(unit, period["year"]), period=period, unit=unit, scope=scope, all_units=all_units,
                           charges=charges, payments=payments, billed=billed, opening=opening, paid=paid,
                           months=months, today=date.today())


@bp.route("/costs/new", methods=["GET", "POST"])
@permission_required("accounting_edit")
def new_cost():
    return _cost_form(None)


@bp.route("/costs/<int:cost_id>/edit", methods=["GET", "POST"])
@permission_required("accounting_edit")
def edit_cost(cost_id):
    return _cost_form(db.session.get(Expense, cost_id) or abort(404))


def _cost_form(cost):
    """Add a building cost (or refund), or change one, including one that came from the Google Sheet:
    the sync only adds new rows, so the change stays."""
    if request.method == "POST":
        errors = []
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "other")
        refund = request.form.get("kind") == "refund"
        if not description:
            errors.append(_("Describe the cost."))
        if category not in COST_CATEGORIES:
            errors.append(_("Choose a category."))
        try:
            amount = parse_money(request.form.get("amount"))
        except ValueError as e:
            errors.append(str(e))
        try:
            spent_on = parse_date(request.form.get("spent_on"), "Date")
        except ValueError as e:
            errors.append(str(e))
        if not errors:
            values = dict(spent_on=spent_on, category=category, description=description[:255],
                          amount_cents=amount if refund else -amount,
                          approved_by=request.form.get("approved_by", "").strip()[:255] or None,
                          source=request.form.get("source", "").strip()[:255] or None,
                          notes=request.form.get("notes", "").strip() or None)
            if cost is None:
                db.session.add(Expense(recorded_by_id=current_user.id, **values))
                flash(_("Refund recorded.") if refund else _("Cost recorded."), "success")
            else:
                for k, v in values.items():
                    setattr(cost, k, v)
                flash(_("Changes saved."), "success")
            db.session.commit()
            from ..navigation import safe_back
            return redirect(safe_back() or url_for("billing.index", view="month", year=spent_on.year, month=spent_on.month))
        for e in errors:
            flash(e, "error")
        return render_template("billing/cost_form.html", form=request.form, categories=COST_CATEGORIES, cost=cost), 400
    if cost is None:
        form = {"spent_on": date.today().isoformat(), "category": "maintenance", "kind": "cost"}
    else:
        form = {"spent_on": cost.spent_on.isoformat(), "category": cost.category_key, "kind": "refund" if cost.amount_cents > 0 else "cost",
                "description": cost.description, "amount": f"{abs(cost.amount_cents) / 100:,.2f}",
                "approved_by": cost.approved_by or "", "source": cost.source or "", "notes": cost.notes or ""}
    return render_template("billing/cost_form.html", categories=COST_CATEGORIES, form=form, cost=cost)


@bp.route("/costs/<int:cost_id>/delete", methods=["POST"])
@permission_required("accounting_edit")
def delete_cost(cost_id):
    cost = db.session.get(Expense, cost_id) or abort(404)
    _unlink(expense_id=cost.id)
    from_sheet = cost.origin == "sheet"
    from ..sheet_sync import forget
    forget(cost)
    db.session.delete(cost)
    db.session.commit()
    flash(_("Cost deleted.") + (" " + _("It came from the Google Sheet; the next sync won't add it again.")
                                if from_sheet else ""), "success" if not from_sheet else "info")
    return redirect(_back_after_delete())


def _back_after_delete():
    from ..navigation import safe_back
    back = safe_back()
    if back and ("/payments/" in back or "/costs/" in back):  # the entry's own page no longer exists
        back = None
    return back or url_for("billing.index")


def _unlink(payment_id=None, expense_id=None):
    """Something recorded from a reimbursement or a routine visit was deleted: undo the link.
    A reimbursement goes back to 'approved', so accounting can record it again."""
    from ..models import Reimbursement, RoutineLog
    for r in db.session.scalars(db.select(Reimbursement).where(
            (Reimbursement.payment_id == payment_id) if payment_id else (Reimbursement.expense_id == expense_id))):
        r.payment_id = None if payment_id else r.payment_id
        r.expense_id = None if expense_id else r.expense_id
        if r.status == "settled":
            r.status, r.settled_by, r.settled_at = "approved", None, None
    if expense_id:
        for log in db.session.scalars(db.select(RoutineLog).where(RoutineLog.expense_id == expense_id)):
            log.expense_id = None


@bp.route("/generate", methods=["POST"])
@permission_required("accounting_edit")
def generate():
    try:
        year, month = (int(x) for x in request.form.get("period", "").split("-"))
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        flash(_("Choose a valid billing month."), "error")
        return redirect(url_for("billing.index"))
    created, skipped = generate_dues(year, month)
    msg = f"Generated dues for {created} unit(s)."
    if skipped:
        msg += f" {skipped} unit(s) were already billed for that month."
    flash(msg, "success")
    return redirect(url_for("billing.index"))


def _special_from(values):
    from ..models import SpecialContribution
    raw = values.get("special_id", "")
    return db.session.get(SpecialContribution, int(raw)) if str(raw).isdigit() else None


def _manual_kinds():
    return {k: v for k, v in CHARGE_KINDS.items() if k not in SYSTEM_CHARGE_KINDS}


def _units():
    return db.session.scalars(db.select(Unit).order_by(Unit.number)).all()


def _unit_from_form(errors):
    raw = request.form.get("unit_id", "")
    unit = db.session.get(Unit, int(raw)) if raw.isdigit() else None
    if unit is None:
        errors.append(_("Select a unit."))
    return unit


@bp.route("/charges/new", methods=["GET", "POST"])
@permission_required("accounting_edit")
def new_charge():
    if request.method == "POST":
        errors = []
        unit = _unit_from_form(errors)
        kind = request.form.get("kind", "other")
        if kind not in CHARGE_KINDS or kind in SYSTEM_CHARGE_KINDS:
            errors.append(_("Choose a charge type."))
        description = request.form.get("description", "").strip()
        if not description:
            errors.append(_("Description is required."))
        try:
            amount = parse_money(request.form.get("amount"))
        except ValueError as e:
            errors.append(str(e))
        try:
            due = parse_date(request.form.get("due_date"), "Due date")
        except ValueError as e:
            errors.append(str(e))
        if not errors:
            db.session.add(Charge(unit=unit, kind=kind, description=description,
                                  amount_cents=amount, due_date=due))
            db.session.commit()
            flash(_("Charge added to unit %(number)s.", number=unit.number), "success")
            return redirect(url_for("billing.index", unit=unit.id, year=due.year))
        for e in errors:
            flash(e, "error")
        return render_template("billing/charge_form.html", units=_units(), kinds=_manual_kinds(),
                               form=request.form), 400
    form = {"unit_id": request.args.get("unit_id", ""), "due_date": date.today().isoformat()}
    return render_template("billing/charge_form.html", units=_units(), kinds=_manual_kinds(), form=form)


@bp.route("/payments/new", methods=["GET", "POST"])
@permission_required("accounting_edit")
def new_payment():
    if request.method == "POST":
        errors = []
        unit = _unit_from_form(errors)
        method = request.form.get("method", "check")
        if method not in PAYMENT_METHODS:
            errors.append(_("Choose a payment method."))
        try:
            amount = parse_money(request.form.get("amount"))
        except ValueError as e:
            errors.append(str(e))
        try:
            paid_on = parse_date(request.form.get("paid_on"), "Payment date")
        except ValueError as e:
            errors.append(str(e))
        if not errors:
            special = _special_from(request.form)
            notes = request.form.get("notes", "").strip()[:255] or None
            if special is not None and not notes:
                notes = f"Special contribution — {special.title}"[:255]
            db.session.add(Payment(unit=unit, amount_cents=amount, paid_on=paid_on, method=method,
                                   reference=request.form.get("reference", "").strip()[:80] or None,
                                   notes=notes, special_id=special.id if special else None,
                                   recorded_by_id=current_user.id))
            db.session.commit()
            flash(_("Payment recorded for unit %(number)s.", number=unit.number), "success")
            if special is not None:
                return redirect(url_for("fees.special", special_id=special.id))
            return redirect(url_for("billing.ledger", unit_id=unit.id))
        for e in errors:
            flash(e, "error")
        return render_template("billing/payment_form.html", units=_units(), methods=PAYMENT_METHOD_LABELS,
                               form=request.form, special=_special_from(request.form)), 400
    form = {"unit_id": request.args.get("unit_id", ""), "paid_on": date.today().isoformat(), "method": "bank transfer",
            "amount": request.args.get("amount", ""), "special_id": request.args.get("special_id", "")}
    return render_template("billing/payment_form.html", units=_units(), methods=PAYMENT_METHOD_LABELS, form=form,
                           special=_special_from(request.args))


@bp.route("/charges/<int:charge_id>/delete", methods=["POST"])
@permission_required("accounting_edit")
def delete_charge(charge_id):
    charge = db.session.get(Charge, charge_id) or abort(404)
    unit_id = charge.unit_id
    from ..sheet_sync import forget
    forget(charge)
    db.session.delete(charge)
    db.session.commit()
    flash(_("Charge removed."), "success")
    from ..navigation import safe_back
    return redirect(safe_back() or url_for("billing.ledger", unit_id=unit_id))


@bp.route("/payments/<int:payment_id>/delete", methods=["POST"])
@permission_required("accounting_edit")
def delete_payment(payment_id):
    payment = db.session.get(Payment, payment_id) or abort(404)
    _unlink(payment_id=payment.id)
    from_sheet = payment.origin == "sheet"
    from ..sheet_sync import forget
    forget(payment)
    db.session.delete(payment)
    db.session.commit()
    flash(_("Payment deleted.") + (" " + _("It came from the Google Sheet; the next sync won't add it again.")
                                   if from_sheet else ""), "success" if not from_sheet else "info")
    return redirect(_back_after_delete())


def _sheet_info():
    return {"sheet": Setting.get("sheet") or {}, "last_sync": Setting.get("sheet_last_sync")}


@bp.route("/payments/<int:payment_id>")
@login_required
def payment_detail(payment_id):
    """Everything about one payment, including where the entry came from."""
    payment = db.session.get(Payment, payment_id) or abort(404)
    if not (can_access_unit(payment.unit_id) or current_user.can("accounting")):
        abort(403)
    from ..models import Reimbursement
    reimbursement = db.session.scalar(db.select(Reimbursement).where(Reimbursement.payment_id == payment.id))
    return render_template("billing/payment_detail.html", p=payment, reimbursement=reimbursement,
                           methods=PAYMENT_METHOD_LABELS, **_sheet_info())


@bp.route("/costs/<int:cost_id>")
@login_required
def cost_detail(cost_id):
    cost = db.session.get(Expense, cost_id) or abort(404)
    if accounting_scope() != "all":
        abort(403)
    from ..models import Reimbursement
    reimbursement = db.session.scalar(db.select(Reimbursement).where(Reimbursement.expense_id == cost.id))
    return render_template("billing/cost_detail.html", x=cost, reimbursement=reimbursement, **_sheet_info())


@bp.route("/unit/<int:unit_id>")
@login_required
def ledger(unit_id):
    if not (can_access_unit(unit_id) or current_user.can("accounting")):
        abort(403)
    unit = db.session.get(Unit, unit_id) or abort(404)
    rows = unit.ledger()
    rows.reverse()  # newest first
    return render_template("billing/ledger.html", unit=unit, rows=rows)


@bp.route("/my")
@login_required
def my_account():
    if current_user.unit_id is None:
        flash(_("Your account is not linked to a unit."), "info")
        return redirect(url_for("dashboard.index"))
    return redirect(url_for("billing.ledger", unit_id=current_user.unit_id))


@bp.route("/bank", methods=["GET", "POST"])
@permission_required("accounting")
def bank():
    """The condo's bank account: balances read from the bank, and the running balance worked out since."""
    form, errors = {"as_of": date.today().isoformat()}, []
    if request.method == "POST":
        if not current_user.can("accounting_edit"):
            abort(403)
        form = request.form
        try:
            as_of = parse_date(form.get("as_of"), _("Date"))
            if as_of > date.today():
                errors.append(_("The date can't be in the future."))
        except ValueError as exc:
            errors.append(str(exc))
        raw = (form.get("amount") or "").strip()
        negative = raw.startswith("-")
        try:
            amount = parse_money(raw.lstrip("-")) * (-1 if negative else 1)
        except ValueError:
            amount = 0 if raw.lstrip("-").strip("$ ") in ("0", "0.00", "0,00") else None
            if amount is None:
                errors.append(_("Enter the balance shown by the bank, e.g. 25,050.39."))
        if not errors:
            db.session.add(BankBalance(as_of=as_of, amount_cents=amount, note=(form.get("note") or "").strip()[:255] or None,
                                       recorded_by_id=current_user.id))
            db.session.commit()
            flash(_("Bank balance recorded."), "success")
            return redirect(url_for("billing.bank"))
    return render_template("billing/bank.html", bank=bank_summary(), form=form, errors=errors), 400 if errors else 200


@bp.route("/bank/<int:record_id>/delete", methods=["POST"])
@permission_required("accounting_edit")
def delete_bank_balance(record_id):
    record = db.session.get(BankBalance, record_id) or abort(404)
    db.session.delete(record)
    db.session.commit()
    flash(_("Bank balance deleted."), "success")
    return redirect(url_for("billing.bank"))
