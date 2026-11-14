"""Business partners: the contractors who quote for the building's work or provide its services.

The board keeps each partner's details, documents (contract, insurance, licence), quotes, the work
they were given, what they were paid and a private rating. Owners see a short directory (name, trade,
phone, website); tenants see nothing.

A contractor is given a project or a repair by accepting one of its quotes: directly by the
President, or by a board vote whose winning quote is accepted when the vote closes.
"""
from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import files, notify
from ..extensions import db
from ..i18n import gettext as _, lazy_gettext as _l
from ..models import (PARTNER_DOC_KINDS, PARTNER_TRADES, PARTNER_QUOTE_STATUSES, REQUEST_STATUSES, Expense, MaintenanceComment,
                      MaintenanceRequest, Partner, PartnerDocument, PartnerRating, Poll, PollOption, PollVoter,
                      Quote, RoutineTask, utcnow)
from ..utils import parse_date, parse_money
from .maintenance import is_board, sees_all
from .reimbursements import is_president

bp = Blueprint("partners", __name__, url_prefix="/maintenance/partners")


def can_manage(user=None):
    """The board and staff who handle repairs or accounting."""
    user = user or current_user
    return sees_all(user) or user.can("accounting")


def can_view(user=None):
    """Owners see the short directory; tenants don't see partners at all."""
    user = user or current_user
    return can_manage(user) or (user.role == "owner")


def _get(partner_id):
    return db.session.get(Partner, partner_id) or abort(404)


def _need_manage():
    if not can_manage():
        abort(403)


def active_partners():
    return db.session.scalars(db.select(Partner).where(Partner.active.is_(True)).order_by(Partner.name)).all()


# --- list and page -----------------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    if not can_view():
        abort(403)
    show = request.args.get("show", "active")
    trade = request.args.get("trade", "")
    q = request.args.get("q", "").strip()
    stmt = db.select(Partner).order_by(Partner.name)
    if show != "all":
        stmt = stmt.where(Partner.active.is_(True))
    if trade in PARTNER_TRADES:
        stmt = stmt.where(Partner.trade == trade)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(db.or_(db.func.lower(Partner.name).like(like), db.func.lower(Partner.contact_name).like(like)))
    partners = db.session.scalars(stmt).all()
    counts = {"active": db.session.scalar(db.select(db.func.count(Partner.id)).where(Partner.active.is_(True))),
              "all": db.session.scalar(db.select(db.func.count(Partner.id)))}
    return render_template("partners/index.html", partners=partners, show=show, trade=trade, q=q, counts=counts,
                           trades=PARTNER_TRADES, manage=can_manage(), year=date.today().year, today=date.today())


@bp.route("/<int:partner_id>")
@login_required
def detail(partner_id):
    _need_manage()
    p = _get(partner_id)
    year = date.today().year
    payments = db.session.scalars(db.select(Expense).where(Expense.partner_id == p.id)
                                  .order_by(Expense.spent_on.desc()).limit(50)).all()
    work = db.session.scalars(db.select(MaintenanceRequest).where(MaintenanceRequest.partner_id == p.id)
                              .order_by(MaintenanceRequest.created_at.desc())).all()
    requests_ = db.session.scalars(db.select(MaintenanceRequest).order_by(MaintenanceRequest.created_at.desc())
                                   .limit(100)).all()
    return render_template("partners/detail.html", p=p, year=year, spent=p.spent_cents(year), payments=payments,
                           work=work, requests=requests_, statuses=REQUEST_STATUSES, doc_kinds=PARTNER_DOC_KINDS, quote_statuses=PARTNER_QUOTE_STATUSES,
                           accept=files.ACCEPT, today=date.today())


# --- add, edit, delete --------------------------------------------------------------------------------

def _form(partner, form):
    errors = []
    name = (form.get("name") or "").strip()
    if not name:
        errors.append(_("Give the partner's name."))
    trade = form.get("trade", "other")
    if trade not in PARTNER_TRADES:
        errors.append(_("Choose a trade."))
    if errors:
        return errors
    partner.name, partner.trade = name[:200], trade
    for field, n in (("contact_name", 200), ("phone", 60), ("email", 255), ("website", 255), ("licence", 60)):
        setattr(partner, field, (form.get(field) or "").strip()[:n] or None)
    partner.address = (form.get("address") or "").strip() or None
    partner.notes = (form.get("notes") or "").strip() or None
    partner.active = bool(form.get("active"))
    return []


def link_existing(partner):
    """Routine tasks and costs that name this partner as their contractor get linked to it (once)."""
    name = partner.name.strip().lower()
    linked = 0
    for t in db.session.scalars(db.select(RoutineTask).where(RoutineTask.partner_id.is_(None))):
        if (t.vendor or "").strip().lower() == name:
            t.partner_id = partner.id
            linked += 1
    for x in db.session.scalars(db.select(Expense).where(Expense.partner_id.is_(None))):
        if (x.source or "").strip().lower() == name:
            x.partner_id = partner.id
            linked += 1
    return linked


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    _need_manage()
    p = Partner(active=True)
    if request.method == "POST":
        errors = _form(p, request.form)
        if not errors:
            db.session.add(p)
            db.session.flush()
            linked = link_existing(p)
            db.session.commit()
            flash(_("Partner added: %(name)s.", name=p.name) + (" " + _("Linked to %(n)s existing entries with that name.", n=linked)
                                                                  if linked else ""), "success")
            return redirect(url_for("partners.detail", partner_id=p.id))
        for e in errors:
            flash(e, "error")
        return render_template("partners/form.html", p=None, form=request.form, trades=PARTNER_TRADES), 400
    return render_template("partners/form.html", p=None, form={"active": True, "trade": request.args.get("trade", "other"),
                                                                "name": request.args.get("name", "")},
                           trades=PARTNER_TRADES)


@bp.route("/<int:partner_id>/edit", methods=["GET", "POST"])
@login_required
def edit(partner_id):
    _need_manage()
    p = _get(partner_id)
    if request.method == "POST":
        errors = _form(p, request.form)
        if not errors:
            link_existing(p)
            db.session.commit()
            flash(_("Changes saved."), "success")
            return redirect(url_for("partners.detail", partner_id=p.id))
        for e in errors:
            flash(e, "error")
        return render_template("partners/form.html", p=p, form=request.form, trades=PARTNER_TRADES), 400
    form = {k: getattr(p, k) or "" for k in ("name", "trade", "contact_name", "phone", "email", "website", "licence",
                                              "address", "notes")}
    form["active"] = p.active
    return render_template("partners/form.html", p=p, form=form, trades=PARTNER_TRADES)


@bp.route("/<int:partner_id>/delete", methods=["POST"])
@login_required
def delete(partner_id):
    _need_manage()
    p = _get(partner_id)
    db.session.delete(p)
    db.session.commit()
    flash(_("Partner deleted."), "success")
    return redirect(url_for("partners.index"))


# --- documents and ratings ----------------------------------------------------------------------------

@bp.route("/<int:partner_id>/documents", methods=["POST"])
@login_required
def add_document(partner_id):
    _need_manage()
    p = _get(partner_id)
    errors = []
    kind = request.form.get("kind", "other")
    if kind not in PARTNER_DOC_KINDS:
        kind = "other"
    until = None
    if request.form.get("valid_until"):
        try:
            until = parse_date(request.form.get("valid_until"), _("Valid until"))
        except ValueError as e:
            errors.append(str(e))
    stored = files.take_upload(request.files.get("file"), errors, required=True)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("partners.detail", partner_id=p.id))
    db.session.add(PartnerDocument(partner=p, kind=kind, title=(request.form.get("title") or "").strip()[:200] or None,
                                   valid_until=until, file=stored))
    db.session.commit()
    flash(_("Document added."), "success")
    return redirect(url_for("partners.detail", partner_id=p.id))


@bp.route("/documents/<int:doc_id>")
@login_required
def document_file(doc_id):
    _need_manage()
    d = db.session.get(PartnerDocument, doc_id) or abort(404)
    return files.send(d.file) if d.file else abort(404)


@bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    _need_manage()
    d = db.session.get(PartnerDocument, doc_id) or abort(404)
    pid = d.partner_id
    db.session.delete(d)
    db.session.commit()
    flash(_("Document deleted."), "success")
    return redirect(url_for("partners.detail", partner_id=pid))


@bp.route("/<int:partner_id>/ratings", methods=["POST"])
@login_required
def add_rating(partner_id):
    if not is_board():
        abort(403)
    p = _get(partner_id)
    raw = request.form.get("stars", "")
    if not raw.isdigit() or not 1 <= int(raw) <= 5:
        flash(_("Choose 1 to 5 stars."), "error")
        return redirect(url_for("partners.detail", partner_id=p.id))
    rid = request.form.get("request_id", "")
    db.session.add(PartnerRating(partner=p, stars=int(raw), comment=(request.form.get("comment") or "").strip() or None,
                                 request_id=int(rid) if rid.isdigit() else None, author_id=current_user.id))
    db.session.commit()
    flash(_("Rating saved."), "success")
    return redirect(url_for("partners.detail", partner_id=p.id))


@bp.route("/ratings/<int:rating_id>/delete", methods=["POST"])
@login_required
def delete_rating(rating_id):
    if not is_board():
        abort(403)
    r = db.session.get(PartnerRating, rating_id) or abort(404)
    pid = r.partner_id
    db.session.delete(r)
    db.session.commit()
    return redirect(url_for("partners.detail", partner_id=pid))


# --- quotes and assigning the work -------------------------------------------------------------------

def _back(default):
    from ..navigation import safe_back
    return redirect(safe_back() or default)


@bp.route("/quotes/new", methods=["POST"])
@login_required
def add_quote():
    _need_manage()
    errors = []
    rid, pid = request.form.get("request_id", ""), request.form.get("partner_id", "")
    req = db.session.get(MaintenanceRequest, int(rid)) if rid.isdigit() else None
    partner = db.session.get(Partner, int(pid)) if pid.isdigit() else None
    if partner is None:
        errors.append(_("Choose the partner who gave the quote."))
    try:
        amount = parse_money(request.form.get("amount"))
    except ValueError as e:
        errors.append(str(e))
    try:
        quoted_on = parse_date(request.form.get("quoted_on") or date.today().isoformat(), _("Date"))
    except ValueError as e:
        errors.append(str(e))
    stored = files.take_upload(request.files.get("file"), errors, required=False)
    default = url_for("maintenance.detail", request_id=req.id) if req else (
        url_for("partners.detail", partner_id=partner.id) if partner else url_for("partners.index"))
    if errors:
        for e in errors:
            flash(e, "error")
        return _back(default)
    db.session.add(Quote(partner=partner, request=req, amount_cents=amount, quoted_on=quoted_on, file=stored,
                         note=(request.form.get("note") or "").strip() or None, created_by_id=current_user.id))
    db.session.commit()
    flash(_("Quote added."), "success")
    return _back(default)


@bp.route("/quotes/<int:quote_id>")
@login_required
def quote_file(quote_id):
    _need_manage()
    q = db.session.get(Quote, quote_id) or abort(404)
    return files.send(q.file) if q.file else abort(404)


@bp.route("/quotes/<int:quote_id>/delete", methods=["POST"])
@login_required
def delete_quote(quote_id):
    _need_manage()
    q = db.session.get(Quote, quote_id) or abort(404)
    default = url_for("maintenance.detail", request_id=q.request_id) if q.request_id else url_for(
        "partners.detail", partner_id=q.partner_id)
    if q.request and q.status == "accepted" and q.request.partner_id == q.partner_id:
        q.request.partner_id = None
    db.session.delete(q)
    db.session.commit()
    flash(_("Quote deleted."), "success")
    return _back(default)


def assign(quote, how):
    """Accept this quote: its partner gets the work; the request's other pending quotes are declined."""
    req = quote.request
    quote.status = "accepted"
    if req is not None:
        for other in req.quotes:
            if other.id != quote.id and other.status == "pending":
                other.status = "declined"
        req.partner_id = quote.partner_id
        db.session.add(MaintenanceComment(request=req, author_id=getattr(current_user, "id", None) or req.submitted_by_id,
                                          body=f"Assigned to {quote.partner.name} ({quote.amount_cents / 100:,.2f} $) — {how}"))


@bp.route("/quotes/<int:quote_id>/assign", methods=["POST"])
@login_required
def assign_quote(quote_id):
    """The President gives the work to this quote's partner without a board vote."""
    if not is_president():
        abort(403)
    q = db.session.get(Quote, quote_id) or abort(404)
    assign(q, f"by the President ({current_user.name})")
    db.session.commit()
    flash(_("%(name)s is assigned to this work.", name=q.partner.name), "success")
    return _back(url_for("maintenance.detail", request_id=q.request_id) if q.request_id else
                 url_for("partners.detail", partner_id=q.partner_id))


@bp.route("/requests/<int:request_id>/board-vote", methods=["POST"])
@login_required
def board_vote(request_id):
    """Ask the board to choose between the request's pending quotes: one vote per board member."""
    if not is_board():
        abort(403)
    req = db.session.get(MaintenanceRequest, request_id) or abort(404)
    pending = [q for q in req.quotes if q.status == "pending"]
    back = url_for("maintenance.detail", request_id=req.id)
    if not pending:
        flash(_("Add at least one quote before asking the board."), "error")
        return redirect(back)
    from .votes import eligible_voters, from_local
    from datetime import datetime
    try:
        deadline = from_local(datetime.strptime(request.form.get("deadline", ""), "%Y-%m-%dT%H:%M"))
    except ValueError:
        flash(_("Choose the deadline's date and time."), "error")
        return redirect(back)
    if deadline <= utcnow():
        flash(_("The deadline must be in the future."), "error")
        return redirect(back)
    poll = Poll(title=_("Contractor for “%(title)s”", title=req.title)[:200], audience="board", weighting="person",
                description=(request.form.get("note") or "").strip() or None, deadline=deadline, status="open",
                published_at=utcnow(), request_id=req.id, created_by_id=current_user.id)
    for i, q in enumerate(pending):
        poll.options.append(PollOption(text=f"{q.partner.name} — {q.amount_cents / 100:,.2f} $"[:200], position=i, quote_id=q.id))
    poll.options.append(PollOption(text=_("None of these"), position=len(pending)))
    db.session.add(poll)
    db.session.flush()
    voters = eligible_voters(poll)
    for user, unit, weight in voters:
        db.session.add(PollVoter(poll=poll, user_id=user.id, unit_id=unit.id if unit else None, weight=weight))
    notify.send([u for u, _unit, _w in voters], _l("Board vote: %(title)s", title=poll.title),
                url=url_for("votes.detail", poll_id=poll.id), kind="vote")
    db.session.commit()
    flash(_("Board vote started. Board members have been notified."), "success")
    return redirect(back)


def apply_vote_outcome(poll):
    """When a board vote on quotes closes: the choice with the most votes wins, as long as more than half
    of the board voted. A tie, too few votes or "None of these" assigns nobody; the board is told."""
    if poll.request_id is None or poll.outcome:
        return
    voters = list(poll.voters)
    voted = [v for v in voters if v.option_id]
    tally = {}
    for v in voted:
        tally[v.option_id] = tally.get(v.option_id, 0) + 1
    req = poll.request
    best = max(tally.values()) if tally else 0
    leaders = [oid for oid, n in tally.items() if n == best]
    option = db.session.get(PollOption, leaders[0]) if len(leaders) == 1 else None
    if not voted or len(voted) * 2 <= len(voters):
        poll.outcome = "no quorum"
        message = _l("Board vote on “%(title)s”: not enough votes (more than half the board must vote). Nobody was assigned.",
                     title=req.title if req else poll.title)
    elif option is None:
        poll.outcome = "tie"
        message = _l("Board vote on “%(title)s”: tie. Nobody was assigned; decide with a new vote or by the President.",
                     title=req.title if req else poll.title)
    elif option.quote is None:
        poll.outcome = "none"
        message = _l("Board vote on “%(title)s”: none of the quotes was chosen.", title=req.title if req else poll.title)
    else:
        poll.outcome = f"assigned:{option.quote.partner_id}"
        if req is not None:
            assign(option.quote, f"by board vote, {best} of {len(voters)}")
        message = _l("Board vote on “%(title)s”: %(name)s is assigned.", title=req.title if req else poll.title,
                     name=option.quote.partner.name)
    notify.send(notify.board(), message, url=url_for("maintenance.detail", request_id=req.id) if req else None,
                kind="vote", include_actor=True)
