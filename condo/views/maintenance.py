from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import files, notify
from ..extensions import db
from ..utils import board_positions_of, parse_date, parse_money
from ..models import (ACTIVE_STATUSES, QUOTE_STATUSES, REQUEST_CATEGORIES, REQUEST_PRIORITIES, REQUEST_STATUSES,
                      MaintenanceComment, MaintenanceRequest, Unit, User, utcnow)
from ..i18n import gettext as _, lazy_gettext as _l, lazy_lower, lazy_money

bp = Blueprint("maintenance", __name__, url_prefix="/maintenance")


def is_board(user=None):
    user = user or current_user
    return user.is_admin or bool(board_positions_of(user))


TABS = {"projects": _l("Projects"), "routine": _l("Routine"), "maintenance": _l("Maintenance")}
KIND_OF_TAB = {"projects": "project", "maintenance": "maintenance"}


def sees_all(user=None):
    user = user or current_user
    return is_board(user) or user.can("maintenance")


def can_add_projects(user=None):
    """Projects are building work the board plans; residents follow them but don't create them."""
    return sees_all(user)


def _visible(req):
    """The board and repair staff see everything; residents see their unit's requests, their own,
    and the building's projects."""
    return (sees_all() or req.submitted_by_id == current_user.id
            or (req.unit_id is not None and req.unit_id == current_user.unit_id)
            or (req.kind == "project" and req.unit_id is None))


def _url(req):
    return url_for("maintenance.detail", request_id=req.id)


def _tell_unit_side(req, title, body=None):
    """The person who asked, plus the unit's owner accounts."""
    notify.send([req.submitted_by] + notify.unit_owner_accounts(req.unit_id), title, url=_url(req), body=body,
                kind="maintenance")


def _visible_requests():
    base = db.select(MaintenanceRequest)
    if not sees_all():
        conds = [MaintenanceRequest.submitted_by_id == current_user.id,
                 db.and_(MaintenanceRequest.kind == "project", MaintenanceRequest.unit_id.is_(None))]
        if current_user.unit_id:
            conds.append(MaintenanceRequest.unit_id == current_user.unit_id)
        base = base.where(db.or_(*conds))
    return base


def tab_counts():
    """Active items per tab (for routine: tasks overdue or due within two weeks)."""
    base = _visible_requests()
    counts = {key: db.session.scalar(db.select(db.func.count()).select_from(
        base.where(MaintenanceRequest.kind == kind, MaintenanceRequest.status.in_(ACTIVE_STATUSES)).subquery()))
        for key, kind in KIND_OF_TAB.items()}
    counts["routine"] = routine_attention_count()
    return counts


@bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "projects")
    if tab not in TABS:
        tab = "projects"
    if tab == "routine":
        return routine_index()
    status = request.args.get("status", "active")
    base = _visible_requests()
    counts = tab_counts()
    stmt = base.where(MaintenanceRequest.kind == KIND_OF_TAB[tab]).order_by(MaintenanceRequest.created_at.desc())
    sub = stmt.order_by(None).subquery()
    in_tab = db.session.execute(db.select(sub.c.status, db.func.count()).group_by(sub.c.status)).all()
    groups = {"active": sum(n for s_, n in in_tab if s_ in ACTIVE_STATUSES),
              "done": sum(n for s_, n in in_tab if s_ not in ACTIVE_STATUSES), "all": sum(n for _, n in in_tab)}
    if status == "active":
        stmt = stmt.where(MaintenanceRequest.status.in_(ACTIVE_STATUSES))
    elif status == "done":
        stmt = stmt.where(MaintenanceRequest.status.not_in(ACTIVE_STATUSES))
    elif status in REQUEST_STATUSES:
        stmt = stmt.where(MaintenanceRequest.status == status)
    requests_ = db.session.scalars(stmt).all()
    return render_template("maintenance/index.html", requests=requests_, status=status, tab=tab, tabs=TABS,
                           counts=counts, groups=groups, can_add_projects=can_add_projects(),
                           statuses=REQUEST_STATUSES, priorities=REQUEST_PRIORITIES)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    picks_unit = current_user.is_admin or current_user.is_staff
    units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all() if picks_unit else []
    values = request.form if request.method == "POST" else request.args
    kind = "project" if values.get("kind") == "project" and can_add_projects() else "maintenance"
    if request.method == "POST":
        errors = []
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "Other")
        priority = request.form.get("priority", "normal")
        location = request.form.get("location", "unit")
        if not title:
            errors.append(_("A short title is required."))
        if not description:
            errors.append(_("Please describe the problem."))
        if category not in REQUEST_CATEGORIES:
            errors.append(_("Choose a category."))
        if priority not in REQUEST_PRIORITIES:
            errors.append(_("Choose a priority."))
        budget = target = None
        if kind == "project":
            if request.form.get("budget", "").strip():
                try:
                    budget = parse_money(request.form.get("budget"))
                except ValueError as e:
                    errors.append(_("Budget: %(e)s", e=e))
            if request.form.get("target_date", "").strip():
                try:
                    target = parse_date(request.form.get("target_date"), "Target date")
                except ValueError as e:
                    errors.append(str(e))

        unit_id = None
        if location == "unit":
            if picks_unit:
                raw = request.form.get("unit_id", "")
                unit = db.session.get(Unit, int(raw)) if raw.isdigit() else None
                if unit is None:
                    errors.append(_("Select a unit, or mark this as a common-area issue."))
                else:
                    unit_id = unit.id
            elif current_user.unit_id is None:
                errors.append(_("Your account is not linked to a unit; report it as a common-area issue."))
            else:
                unit_id = current_user.unit_id

        if not errors:
            req = MaintenanceRequest(title=title, description=description, category=category, kind=kind,
                                     priority=priority, unit_id=unit_id, submitted_by_id=current_user.id,
                                     budget_cents=budget, target_date=target)
            needs_owner = kind == "maintenance" and needs_owner_approval(current_user, unit_id)
            if needs_owner:
                req.status, req.owner_approval = "awaiting_owner", "pending"
            db.session.add(req)
            db.session.flush()
            where = f"unit {req.unit.number}" if req.unit_id else "common area"
            if needs_owner:
                notify.send(notify.unit_owner_accounts(unit_id), _l("Your tenant asked for a repair: %(title)s", title=title),
                            url=_url(req), body=_l("%(name)s: %(description)s", name=current_user.name, description=description), kind="maintenance")
            elif kind == "project":
                notify.send([u for u in notify.admins() if u.id != current_user.id],
                            _l("New project (%(where)s): %(title)s", where=where, title=title), url=_url(req),
                            body=_l("%(name)s: %(description)s", name=current_user.name, description=description), kind="maintenance")
            else:
                notify.send(notify.admins(), _l("New repair request (%(where)s): %(title)s", where=where, title=title), url=_url(req),
                            body=_l("%(name)s: %(description)s", name=current_user.name, description=description), kind="maintenance")
            db.session.commit()
            flash(_("Request sent to the unit's owner for approval.") if needs_owner else _("Project added.") if kind == "project" else _("Maintenance request submitted."), "success")
            return redirect(url_for("maintenance.detail", request_id=req.id))
        for e in errors:
            flash(e, "error")
        return render_template("maintenance/form.html", form=request.form, units=units, kind=kind,
                               categories=REQUEST_CATEGORIES, priorities=REQUEST_PRIORITIES), 400
    return render_template("maintenance/form.html", units=units, kind=kind,
                           form={"priority": "normal", "location": "common" if kind == "project" else "unit"},
                           categories=REQUEST_CATEGORIES, priorities=REQUEST_PRIORITIES)


def needs_owner_approval(user, unit_id):
    """Repairs a tenant asks for in their unit need the owner's go-ahead first."""
    return unit_id is not None and not user.is_admin and user.role == "tenant"


def can_decide(req, user=None):
    """Owner accounts of the unit decide; managers can record a decision made by phone or email."""
    user = user or current_user
    if not req.awaiting_owner:
        return False
    return user.is_admin or (user.role == "owner" and user.unit_id is not None and user.unit_id == req.unit_id)


def owner_accounts(unit_id):
    return db.session.scalars(db.select(User).where(User.unit_id == unit_id, User.role == "owner",
                                                    User.active.is_(True))).all()


def _get_visible(request_id):
    req = db.session.get(MaintenanceRequest, request_id) or abort(404)
    if not _visible(req):
        abort(403)
    return req


@bp.route("/<int:request_id>")
@login_required
def detail(request_id):
    req = _get_visible(request_id)
    owners = owner_accounts(req.unit_id) if req.awaiting_owner and req.unit_id else []
    return render_template("maintenance/detail.html", req=req, statuses=REQUEST_STATUSES,
                           priorities=REQUEST_PRIORITIES, can_decide=can_decide(req), owner_accounts=owners,
                           board=is_board(), unit_owner=is_unit_owner(req), accept=files.ACCEPT,
                           can_projects=can_add_projects(), can_delete=can_delete(req),
                           can_routine=can_manage_routine())


def is_unit_owner(req, user=None):
    user = user or current_user
    return user.is_admin or (user.role == "owner" and req.unit_id is not None and user.unit_id == req.unit_id)


@bp.route("/<int:request_id>/ask-quote", methods=["POST"])
@login_required
def ask_quote(request_id):
    """Board: the owner should get a quote and have the cost approved before doing the repair."""
    req = _get_visible(request_id)
    if not is_board() or req.unit_id is None or req.status not in ("open", "in_progress"):
        abort(403)
    note = request.form.get("note", "").strip()
    req.status, req.quote_note = "quote_requested", note or None
    req.quote_amount_cents = req.quote_file_id = req.quote_approved_cents = None
    db.session.add(MaintenanceComment(request=req, author_id=current_user.id,
                                      body="The board asked the owner to get a quote for board approval."
                                      + (f" {note}" if note else "")))
    _tell_unit_side(req, f"Please get a quote for: {req.title}",
                    "Upload the quote on the request; once the board approves the cost, go ahead with the repair "
                    "and request a reimbursement." + (f" Board's note: {note}" if note else ""))
    db.session.commit()
    flash(_("The owner has been asked for a quote."), "success")
    return redirect(_url(req))


@bp.route("/<int:request_id>/quote", methods=["POST"])
@login_required
def submit_quote(request_id):
    req = _get_visible(request_id)
    if req.status != "quote_requested" or not is_unit_owner(req):
        abort(403)
    errors = []
    try:
        amount = parse_money(request.form.get("amount"))
    except ValueError as e:
        errors.append(str(e))
    stored = files.take_upload(request.files.get("file"), errors, required=True)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(_url(req))
    req.quote_amount_cents, req.quote_file = amount, stored
    req.quote_vendor = request.form.get("vendor", "").strip()[:200] or None
    req.status = "quote_review"
    db.session.add(MaintenanceComment(request=req, author_id=current_user.id,
                                      body=f"Quote submitted: ${amount / 100:,.2f}"
                                      + (f" from {req.quote_vendor}" if req.quote_vendor else "") + "."))
    notify.send(notify.board(), _l("Quote to approve: unit %(number)s, %(amount)s — %(req)s", number=req.unit.number, amount=lazy_money(amount), req=req.title),
                url=_url(req), kind="maintenance")
    db.session.commit()
    flash(_("Quote sent to the board for approval."), "success")
    return redirect(_url(req))


@bp.route("/<int:request_id>/quote-decision", methods=["POST"])
@login_required
def quote_decision(request_id):
    req = _get_visible(request_id)
    if req.status != "quote_review" or not is_board():
        abort(403)
    decision = request.form.get("decision")
    note = request.form.get("note", "").strip()
    if decision not in ("approve", "decline"):
        abort(400)
    if decision == "decline" and not note:
        flash(_("Tell the owner why, e.g. ask for another quote."), "error")
        return redirect(_url(req))
    req.quote_decided_by, req.quote_decided_at, req.quote_decision_note = current_user.name, utcnow(), note or None
    if decision == "approve":
        raw = request.form.get("approved_amount", "").strip()
        try:
            approved = parse_money(raw) if raw else req.quote_amount_cents
        except ValueError as e:
            flash(str(e), "error")
            return redirect(_url(req))
        req.quote_approved_cents, req.status = approved, "owner_repair"
        msg = f"Quote approved for ${approved / 100:,.2f}. The owner can do the repair and request a reimbursement."
        _tell_unit_side(req, f"Quote approved (${approved / 100:,.2f}): go ahead with {req.title}",
                        "When it's done and paid, open the request and click Request reimbursement."
                        + (f" Board's note: {note}" if note else ""))
    else:
        req.status = "quote_requested"
        msg = f"Quote declined: {note}"
        _tell_unit_side(req, f"Quote not approved: {req.title}", note)
    db.session.add(MaintenanceComment(request=req, author_id=current_user.id, body=f"{msg} ({current_user.name})"))
    db.session.commit()
    flash(_("Decision recorded and the owner notified."), "success")
    return redirect(_url(req))


@bp.route("/<int:request_id>/quote-file")
@login_required
def quote_file(request_id):
    req = _get_visible(request_id)
    if req.quote_file is None:
        abort(404)
    return files.send(req.quote_file)


@bp.route("/<int:request_id>/owner-decision", methods=["POST"])
@login_required
def owner_decision(request_id):
    req = _get_visible(request_id)
    if not can_decide(req):
        abort(403)
    decision = request.form.get("decision")
    if decision not in ("approve", "decline"):
        abort(400)
    note = request.form.get("note", "").strip()
    if decision == "decline" and not note:
        flash(_("Please say why the repair is declined, so the tenant knows."), "error")
        return redirect(url_for("maintenance.detail", request_id=req.id))
    who = current_user.name
    if current_user.is_admin:
        on_behalf = request.form.get("on_behalf", "").strip()
        who = f"{current_user.name} (manager), on behalf of {on_behalf or 'the owner'}"
    req.owner_approval = "approved" if decision == "approve" else "declined"
    req.owner_decision_by, req.owner_decision_at, req.owner_decision_note = who[:200], utcnow(), note or None
    req.status = "open" if decision == "approve" else "closed"
    verb = "approved" if decision == "approve" else "declined"
    db.session.add(MaintenanceComment(request=req, author_id=current_user.id,
                                      body=f"Owner {verb} this repair ({who})." + (f" Note: {note}" if note else "")))
    notify.send([req.submitted_by], (_l("The owner approved your repair request: %(req)s", req=req.title) if decision == "approve"
                                  else _l("The owner declined your repair request: %(req)s", req=req.title)), url=_url(req),
                body=note or None, kind="maintenance")
    if decision == "approve":
        notify.send(notify.admins(), _l("Repair approved by the owner, ready to schedule: %(req)s", req=req.title), url=_url(req),
                    kind="maintenance")
    db.session.commit()
    flash(_("Repair approved. The board can now schedule it.") if decision == "approve" else _("Repair declined."), "success")
    return redirect(url_for("maintenance.detail", request_id=req.id))


@bp.route("/<int:request_id>/project", methods=["POST"])
@login_required
def project_details(request_id):
    """The board files an item under Projects or Maintenance and sets a project's budget and target date."""
    req = _get_visible(request_id)
    if not can_add_projects():
        abort(403)
    kind = request.form.get("kind", req.kind)
    if kind not in ("project", "maintenance"):
        abort(400)
    errors, budget, target = [], None, None
    if kind == "project":
        if request.form.get("budget", "").strip():
            try:
                budget = parse_money(request.form.get("budget"))
            except ValueError as e:
                errors.append(_("Budget: %(e)s", e=e))
        if request.form.get("target_date", "").strip():
            try:
                target = parse_date(request.form.get("target_date"), "Target date")
            except ValueError as e:
                errors.append(str(e))
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(_url(req))
    moved = kind != req.kind
    req.kind = kind
    if kind == "project":
        req.budget_cents, req.target_date = budget, target
    if moved:
        db.session.add(MaintenanceComment(request=req, author_id=current_user.id,
                                          body=f"Moved to {'Projects' if kind == 'project' else 'Maintenance'}."))
    db.session.commit()
    flash(_("Moved to Projects.") if moved and kind == "project" else _("Moved to Maintenance.") if moved else _("Project details saved."), "success")
    return redirect(_url(req))


def can_delete(req, user=None):
    """The board and repair staff can delete anything; the person who asked can delete their own request
    while nothing has happened on it yet."""
    user = user or current_user
    if sees_all(user):
        return True
    untouched = req.status in ("open", "awaiting_owner") and not any(c.author_id != user.id for c in req.comments)
    return req.submitted_by_id == user.id and req.kind == "maintenance" and untouched


@bp.route("/<int:request_id>/delete", methods=["POST"])
@login_required
def delete(request_id):
    req = _get_visible(request_id)
    if not can_delete(req):
        abort(403)
    from ..models import Reimbursement
    for r in db.session.scalars(db.select(Reimbursement).where(Reimbursement.request_id == req.id)):
        r.request_id = None  # reimbursements are money records: keep them, just unlink
    tab = "projects" if req.is_project else "maintenance"
    title, quote = req.title, req.quote_file
    db.session.delete(req)
    if quote is not None:
        db.session.delete(quote)
    db.session.commit()
    flash(_("Project deleted: %(title)s.", title=title) if tab == "projects"
          else _("Request deleted: %(title)s.", title=title), "success")
    return redirect(url_for("maintenance.index", tab=tab))


@bp.route("/<int:request_id>/comment", methods=["POST"])
@login_required
def comment(request_id):
    req = _get_visible(request_id)
    body = request.form.get("body", "").strip()
    if not body:
        flash(_("Comment cannot be empty."), "error")
    else:
        db.session.add(MaintenanceComment(request=req, author_id=current_user.id, body=body))
        req.updated_at = utcnow()
        others = [req.submitted_by] + ([] if current_user.is_admin else notify.admins())
        notify.send(others, _l("New comment on “%(req)s” from %(name)s", req=req.title, name=current_user.name), url=_url(req), body=body,
                    kind="maintenance")
        db.session.commit()
        flash(_("Comment added."), "success")
    return redirect(url_for("maintenance.detail", request_id=req.id))


@bp.route("/<int:request_id>/status", methods=["POST"])
@login_required
def update_status(request_id):
    req = _get_visible(request_id)
    new_status = request.form.get("status")
    new_priority = request.form.get("priority", req.priority)
    if current_user.can("maintenance"):
        if new_status not in REQUEST_STATUSES or new_priority not in REQUEST_PRIORITIES:
            abort(400)
        if req.awaiting_owner and new_status not in ("awaiting_owner", "closed"):
            flash(_("This repair is waiting for the owner. Record the owner's approval first."), "error")
            return redirect(url_for("maintenance.detail", request_id=req.id))
        if new_status == "awaiting_owner" and not req.awaiting_owner:
            abort(400)
        if new_status in QUOTE_STATUSES and new_status != req.status:
            flash(_("Use the quote buttons to move a request through the quote step."), "error")
            return redirect(url_for("maintenance.detail", request_id=req.id))
    else:
        # Residents may only close their own request (e.g. "fixed itself") or reopen a resolved one.
        allowed = {"closed"} if req.is_active else {"open"} if req.status == "resolved" else set()
        if req.submitted_by_id != current_user.id or new_status not in allowed:
            abort(403)
        new_priority = req.priority
    old = req.status
    req.status = new_status
    req.priority = new_priority
    if old != new_status:
        db.session.add(MaintenanceComment(
            request=req, author_id=current_user.id,
            body=f"Status changed from {REQUEST_STATUSES[old]} to {REQUEST_STATUSES[new_status]}."))
        notify.send([req.submitted_by], _l("Your repair request is now %(new_status)s: %(req)s", new_status=lazy_lower(REQUEST_STATUSES[new_status]), req=req.title),
                    url=_url(req), kind="maintenance")
    db.session.commit()
    flash(_("Request updated."), "success")
    return redirect(url_for("maintenance.detail", request_id=req.id))


# --- Routine maintenance ------------------------------------------------------------------------------

from datetime import date as _date, timedelta as _timedelta  # noqa: E402

from ..models import (CATEGORY_LABELS, ROUTINE_FREQUENCIES, ROUTINE_STATE_LABELS, Expense, RoutineLog,  # noqa: E402
                      RoutineTask)
from ..i18n import fdate, lazy_date  # noqa: E402

# Common upkeep for a Quebec condo, offered as one-click starting points.
ROUTINE_SUGGESTIONS = [
    {"title": _l("Snow removal"), "frequency": "yearly", "season_from": 11, "season_to": 4, "month": 11, "category": "Common area"},
    {"title": _l("Gutter cleaning"), "frequency": "twice_yearly", "month": 5, "category": "Common area"},
    {"title": _l("Fire alarm and extinguisher inspection"), "frequency": "yearly", "month": 3, "category": "Security"},
    {"title": _l("Dryer vent cleaning"), "frequency": "yearly", "month": 9, "category": "Other"},
    {"title": _l("Heating / HVAC servicing"), "frequency": "yearly", "month": 10, "category": "HVAC"},
    {"title": _l("Roof inspection"), "frequency": "yearly", "month": 5, "category": "Common area"},
    {"title": _l("Lawn care and landscaping"), "frequency": "yearly", "season_from": 5, "season_to": 10, "month": 5, "category": "Common area"},
    {"title": _l("Window washing"), "frequency": "yearly", "month": 6, "category": "Common area"},
]


def can_manage_routine(user=None):
    return sees_all(user)


def routine_attention_count(today=None):
    today = today or _date.today()
    return db.session.scalar(db.select(db.func.count(RoutineTask.id)).where(
        RoutineTask.active.is_(True), RoutineTask.next_due <= today + _timedelta(days=14))) or 0


def check_routine_reminders(today=None):
    """Tell the board once when a routine task is due within two weeks, and when a contract ends within 60 days."""
    from ..models import CONTRACT_NOTICE_DAYS, ROUTINE_DUE_SOON_DAYS
    today = today or _date.today()
    sent = 0
    for t in db.session.scalars(db.select(RoutineTask).where(RoutineTask.active.is_(True))):
        url = url_for("maintenance.routine_detail", task_id=t.id)
        if t.next_due <= today + _timedelta(days=ROUTINE_DUE_SOON_DAYS) and t.reminded_due != t.next_due:
            notify.send(notify.board(), _l("Routine maintenance due %(when)s: %(title)s", when=lazy_date(t.next_due),
                                           title=t.title), url=url, kind="maintenance", include_actor=True,
                        body=_l("Contractor: %(vendor)s", vendor=t.vendor) if t.vendor else None)
            t.reminded_due = t.next_due
            sent += 1
        if t.contract_ends and t.contract_ends <= today + _timedelta(days=CONTRACT_NOTICE_DAYS) \
                and t.contract_ends >= today - _timedelta(days=31) and t.reminded_contract != t.contract_ends:
            notify.send(notify.board(), _l("Contract ends %(when)s: %(title)s — time to renew or find a new contractor",
                                           when=lazy_date(t.contract_ends), title=t.title),
                        url=url, kind="maintenance", include_actor=True)
            t.reminded_contract = t.contract_ends
            sent += 1
    if sent:
        db.session.commit()
    return sent


def routine_index():
    if can_manage_routine():
        check_routine_reminders()
    show = request.args.get("show", "active")
    stmt = db.select(RoutineTask).order_by(RoutineTask.active.desc(), RoutineTask.next_due)
    if show != "all":
        stmt = stmt.where(RoutineTask.active.is_(True))
    tasks = db.session.scalars(stmt).all()
    counts = tab_counts()
    groups = {"all": db.session.scalar(db.select(db.func.count(RoutineTask.id))),
              "active": db.session.scalar(db.select(db.func.count(RoutineTask.id)).where(RoutineTask.active.is_(True)))}
    return render_template("maintenance/routine_index.html", tasks=tasks, tab="routine", tabs=TABS, counts=counts,
                           groups=groups,
                           show=show, can_manage=can_manage_routine(), states=ROUTINE_STATE_LABELS,
                           suggestions=ROUTINE_SUGGESTIONS if can_manage_routine() and not db.session.scalar(
                               db.select(db.func.count(RoutineTask.id))) else [],
                           today=_date.today(), can_add_projects=can_add_projects())


def _routine(task_id):
    return db.session.get(RoutineTask, task_id) or abort(404)


def _routine_form(task, form):
    errors = []
    title = form.get("title", "").strip()
    if not title:
        errors.append(_("Give the task a name, e.g. “Snow removal”."))
    frequency = form.get("frequency", "yearly")
    if frequency not in ROUTINE_FREQUENCIES:
        errors.append(_("Choose how often it repeats."))
    category = form.get("category", "Other")
    if category not in REQUEST_CATEGORIES:
        category = "Other"
    try:
        next_due = parse_date(form.get("next_due"), _("Next due"))
    except ValueError as e:
        errors.append(str(e))
        next_due = None
    contract_ends = None
    if form.get("contract_ends", "").strip():
        try:
            contract_ends = parse_date(form.get("contract_ends"), _("Contract ends"))
        except ValueError as e:
            errors.append(str(e))
    estimated = None
    if form.get("estimated", "").strip():
        try:
            estimated = parse_money(form.get("estimated"))
        except ValueError as e:
            errors.append(_("Estimated cost: %(e)s", e=e))
    months = []
    for field in ("season_from", "season_to"):
        raw = form.get(field, "")
        months.append(int(raw) if raw.isdigit() and 1 <= int(raw) <= 12 else None)
    if bool(months[0]) != bool(months[1]):
        errors.append(_("Choose both the first and the last month of the season, or neither."))
    if errors:
        return errors
    task.title, task.frequency, task.category, task.next_due = title[:200], frequency, category, next_due
    task.description = form.get("description", "").strip() or None
    task.season_from, task.season_to = months
    task.vendor = form.get("vendor", "").strip()[:200] or None
    task.vendor_contact = form.get("vendor_contact", "").strip()[:200] or None
    task.estimated_cents, task.contract_ends = estimated, contract_ends
    task.active = bool(form.get("active", "1" if task.id is None else ""))
    return []


def _render_routine_form(task, form, status=200):
    return render_template("maintenance/routine_form.html", task=task, form=form, frequencies=ROUTINE_FREQUENCIES,
                           categories=REQUEST_CATEGORIES), status


@bp.route("/routine/new", methods=["GET", "POST"])
@login_required
def routine_new():
    if not can_manage_routine():
        abort(403)
    task = RoutineTask()
    # "Move to Routine" from a maintenance request: the form starts from the request, which is closed once saved.
    raw = request.values.get("from_request", "")
    source = db.session.get(MaintenanceRequest, int(raw)) if raw.isdigit() else None
    if request.method == "POST":
        errors = _routine_form(task, request.form)
        if not errors:
            task.created_by_id = current_user.id
            db.session.add(task)
            if source is not None:
                db.session.flush()
                source.status = "closed"
                db.session.add(MaintenanceComment(request=source, author_id=current_user.id,
                                                  body=f"Moved to routine maintenance: {task.title} ({current_user.name})"))
            db.session.commit()
            flash(_("Routine task added: %(title)s.", title=task.title)
                  + (" " + _("The request was closed and points to it.") if source is not None else ""), "success")
            return redirect(url_for("maintenance.routine_detail", task_id=task.id))
        for e in errors:
            flash(e, "error")
        return _render_routine_form(task, request.form, 400)
    today = _date.today()
    month = request.args.get("month", "")
    due = today
    if month.isdigit() and 1 <= int(month) <= 12:
        due = _date(today.year if int(month) >= today.month else today.year + 1, int(month), 1)
    form = {"title": request.args.get("title", ""), "frequency": request.args.get("frequency", "yearly"),
            "category": request.args.get("category", "Other"), "next_due": due.isoformat(),
            "season_from": request.args.get("season_from", ""), "season_to": request.args.get("season_to", ""),
            "active": True}
    if source is not None:
        form.update(title=source.title, category=source.category if source.category in REQUEST_CATEGORIES else "Other",
                    description=source.description, from_request=source.id)
    return _render_routine_form(task, form)


@bp.route("/routine/<int:task_id>")
@login_required
def routine_detail(task_id):
    task = _routine(task_id)
    return render_template("maintenance/routine_detail.html", task=task, states=ROUTINE_STATE_LABELS,
                           can_manage=can_manage_routine(), today=_date.today())


@bp.route("/routine/<int:task_id>/edit", methods=["GET", "POST"])
@login_required
def routine_edit(task_id):
    if not can_manage_routine():
        abort(403)
    task = _routine(task_id)
    if request.method == "POST":
        errors = _routine_form(task, request.form)
        if not errors:
            db.session.commit()
            flash(_("Routine task updated."), "success")
            return redirect(url_for("maintenance.routine_detail", task_id=task.id))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return _render_routine_form(task, request.form, 400)
    form = {"title": task.title, "description": task.description or "", "frequency": task.frequency,
            "category": task.category, "next_due": task.next_due.isoformat(),
            "season_from": str(task.season_from or ""), "season_to": str(task.season_to or ""),
            "vendor": task.vendor or "", "vendor_contact": task.vendor_contact or "",
            "estimated": f"{task.estimated_cents / 100:.2f}" if task.estimated_cents else "",
            "contract_ends": task.contract_ends.isoformat() if task.contract_ends else "", "active": task.active}
    return _render_routine_form(task, form)


@bp.route("/routine/<int:task_id>/done", methods=["POST"])
@login_required
def routine_done(task_id):
    """Log a visit, move the next due date, and optionally record the cost in Accounting."""
    if not can_manage_routine():
        abort(403)
    task = _routine(task_id)
    try:
        done_on = parse_date(request.form.get("done_on"), _("Date done"))
        cost = parse_money(request.form.get("cost")) if request.form.get("cost", "").strip() else None
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("maintenance.routine_detail", task_id=task.id))
    note = request.form.get("note", "").strip() or None
    log = RoutineLog(task=task, done_on=done_on, cost_cents=cost, note=note, done_by_id=current_user.id)
    if cost and request.form.get("record_cost") and current_user.can("accounting_edit"):
        expense = Expense(spent_on=done_on, category="maintenance", amount_cents=-cost,
                          description=(f"{task.title}" + (f" — {task.vendor}" if task.vendor else ""))[:255],
                          source=task.vendor, notes=note, recorded_by_id=current_user.id)
        db.session.add(expense)
        db.session.flush()
        log.expense_id = expense.id
    db.session.add(log)
    task.advance(done_on)
    db.session.commit()
    flash(_("Marked as done. Next due %(when)s.", when=fdate(task.next_due, "long"))
          + (" " + _("The cost was added to Accounting.") if log.expense_id else ""), "success")
    return redirect(url_for("maintenance.routine_detail", task_id=task.id))


@bp.route("/routine/<int:task_id>/delete", methods=["POST"])
@login_required
def routine_delete(task_id):
    if not can_manage_routine():
        abort(403)
    task = _routine(task_id)
    db.session.delete(task)
    db.session.commit()
    flash(_("Routine task deleted."), "success")
    return redirect(url_for("maintenance.index", tab="routine"))
