"""Votes: the board drafts a poll, publishes it to an audience with a deadline, and owners vote.

Nobody is told about a draft. Publishing fixes who may vote and how much each vote counts:
by ownership share (a unit's owners split its share), one vote per unit, or one per person.
Only the board sees who voted for what; voters see the results once the deadline has passed.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import notify
from ..extensions import db
from ..i18n import gettext as _, lazy_datetime, lazy_gettext as _l
from ..models import (POLL_AUDIENCES, POLL_STATUS_LABELS, POLL_WEIGHTINGS, Poll, PollOption, PollVoter, Unit, User,
                      utcnow)

bp = Blueprint("votes", __name__, url_prefix="/votes")
LOCAL = ZoneInfo("America/Montreal")
REMIND_BEFORE = timedelta(hours=24)


def is_board(user=None):
    return (user or current_user).is_admin


def to_local(dt):
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(LOCAL).replace(tzinfo=None) if dt else None


def from_local(dt):
    return dt.replace(tzinfo=LOCAL).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _units():
    return db.session.scalars(db.select(Unit).order_by(Unit.number)).all()


def eligible_voters(poll):
    """[(user, unit, weight)] for the poll's audience and weighting. Weights add up to 1."""
    if poll.audience == "board":
        users = [u for u in notify.board() if u.active]
        return [(u, u.unit, 1 / len(users)) for u in users] if users else []
    owners = db.session.scalars(db.select(User).where(User.role == "owner", User.active.is_(True),
                                                      User.unit_id.is_not(None))).all()
    if poll.audience == "units":
        wanted = set(poll.selected_unit_ids)
        owners = [u for u in owners if u.unit_id in wanted]
    by_unit = {}
    for u in owners:
        by_unit.setdefault(u.unit_id, []).append(u)
    if not by_unit:
        return []
    out = []
    if poll.weighting == "person":
        n = sum(len(v) for v in by_unit.values())
        return [(u, u.unit, 1 / n) for users in by_unit.values() for u in users]
    units = {uid: users[0].unit for uid, users in by_unit.items()}
    if poll.weighting == "share":
        total = sum(units[uid].ownership_share or 0 for uid in by_unit)
        for uid, users in by_unit.items():
            for u in users:
                out.append((u, units[uid], (units[uid].ownership_share or 0) / total / len(users) if total else 0))
    else:  # one per unit
        for uid, users in by_unit.items():
            for u in users:
                out.append((u, units[uid], 1 / len(by_unit) / len(users)))
    return out


def check_polls(now=None):
    """Remind people who haven't voted a day before the deadline; tell voters when results are ready."""
    now = now or utcnow()
    sent = 0
    for p in db.session.scalars(db.select(Poll).where(Poll.status != "draft")):
        url = url_for("votes.detail", poll_id=p.id)
        if p.state == "open" and not p.reminded_at and p.deadline - now <= REMIND_BEFORE:
            waiting = [v.user for v in p.voters if not v.option_id]
            if waiting:
                notify.send(waiting, _l("Reminder: vote on “%(title)s” before %(when)s", title=p.title,
                                        when=lazy_datetime(to_local(p.deadline))), url=url, kind="vote", include_actor=True)
            p.reminded_at = now
            sent += 1
        if p.state == "closed" and not p.results_notified_at:
            notify.send([v.user for v in p.voters] + notify.board(), _l("Results are in: %(title)s", title=p.title),
                        url=url, kind="vote", include_actor=True)
            p.results_notified_at = now
            if p.request_id:  # a board vote on quotes: give the work to the winner
                from .partners import apply_vote_outcome
                apply_vote_outcome(p)
            sent += 1
    if sent:
        db.session.commit()
    return sent


@bp.route("/")
@login_required
def index():
    check_polls()
    board = is_board()
    stmt = db.select(Poll).order_by(Poll.created_at.desc())
    polls = db.session.scalars(stmt).all()
    if not board:
        mine = {v.poll_id for v in db.session.scalars(db.select(PollVoter).where(PollVoter.user_id == current_user.id))}
        polls = [p for p in polls if p.id in mine and p.status != "draft"]
    return render_template("votes/index.html", polls=polls, board=board, labels=POLL_STATUS_LABELS,
                           to_local=to_local)


def _get(poll_id):
    return db.session.get(Poll, poll_id) or abort(404)


def _form_values(poll=None):
    if poll is None:
        local = (datetime.now(LOCAL) + timedelta(days=14)).replace(hour=17, minute=0, second=0, microsecond=0)
        return {"title": "", "description": "", "audience": "owners", "weighting": "share",
                "deadline": local.strftime("%Y-%m-%dT%H:%M"), "options": [_("Yes"), _("No"), _("Abstain")],
                "unit_ids": []}
    return {"title": poll.title, "description": poll.description or "", "audience": poll.audience,
            "weighting": poll.weighting, "deadline": to_local(poll.deadline).strftime("%Y-%m-%dT%H:%M"),
            "options": [o.text for o in poll.options], "unit_ids": poll.selected_unit_ids}


def _apply(poll, form):
    errors = []
    title = form.get("title", "").strip()
    if not title:
        errors.append(_("Give the vote a question or title."))
    audience = form.get("audience", "owners")
    if audience not in POLL_AUDIENCES:
        errors.append(_("Choose who votes."))
    weighting = "person" if audience == "board" else form.get("weighting", "share")
    if weighting not in POLL_WEIGHTINGS:
        errors.append(_("Choose how votes are counted."))
    unit_ids = [x for x in form.getlist("unit_ids") if x.isdigit()]
    if audience == "units" and not unit_ids:
        errors.append(_("Choose at least one unit."))
    options = [o.strip()[:200] for o in form.getlist("options") if o.strip()]
    if len(options) < 2:
        errors.append(_("Give at least two choices."))
    try:
        deadline = from_local(datetime.strptime(form.get("deadline", ""), "%Y-%m-%dT%H:%M"))
        if deadline <= utcnow():
            errors.append(_("The deadline must be in the future."))
    except ValueError:
        errors.append(_("Enter the deadline date and time."))
        deadline = None
    if errors:
        return errors
    poll.title, poll.description = title[:200], form.get("description", "").strip() or None
    poll.audience, poll.weighting, poll.deadline = audience, weighting, deadline
    poll.unit_ids = ",".join(unit_ids) if audience == "units" else None
    poll.options.clear()
    db.session.flush()
    for i, text in enumerate(options):
        poll.options.append(PollOption(text=text, position=i))
    return []


def _render_form(poll, values, status=200):
    return render_template("votes/form.html", poll=poll, form=values, audiences=POLL_AUDIENCES,
                           weightings=POLL_WEIGHTINGS, units=_units()), status


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if not is_board():
        abort(403)
    poll = Poll()
    if request.method == "POST":
        errors = _apply(poll, request.form)
        if not errors:
            poll.created_by_id = current_user.id
            db.session.add(poll)
            db.session.commit()
            flash(_("Draft saved. Nobody has been told yet: check it, then publish it."), "success")
            return redirect(url_for("votes.detail", poll_id=poll.id))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return _render_form(poll, _posted(), 400)
    return _render_form(poll, _form_values())


def _posted():
    f = request.form
    return {"title": f.get("title", ""), "description": f.get("description", ""), "audience": f.get("audience"),
            "weighting": f.get("weighting"), "deadline": f.get("deadline", ""),
            "options": [o for o in f.getlist("options")] or ["", ""],
            "unit_ids": [int(x) for x in f.getlist("unit_ids") if x.isdigit()]}


@bp.route("/<int:poll_id>/edit", methods=["GET", "POST"])
@login_required
def edit(poll_id):
    poll = _get(poll_id)
    if not is_board():
        abort(403)
    if poll.status != "draft":
        flash(_("A published vote can't be changed."), "error")
        return redirect(url_for("votes.detail", poll_id=poll.id))
    if request.method == "POST":
        errors = _apply(poll, request.form)
        if not errors:
            db.session.commit()
            flash(_("Draft updated."), "success")
            return redirect(url_for("votes.detail", poll_id=poll.id))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return _render_form(poll, _posted(), 400)
    return _render_form(poll, _form_values(poll))


@bp.route("/<int:poll_id>")
@login_required
def detail(poll_id):
    check_polls()
    poll = _get(poll_id)
    board = is_board()
    me = poll.voter_for(current_user)
    if not board and (me is None or poll.status == "draft"):
        abort(403)
    preview = eligible_voters(poll) if poll.status == "draft" and board else None
    state = poll.state
    show_results = board or state == "closed"
    return render_template("votes/detail.html", poll=poll, board=board, me=me, state=state, labels=POLL_STATUS_LABELS,
                           audiences=POLL_AUDIENCES, weightings=POLL_WEIGHTINGS, preview=preview, to_local=to_local,
                           results=poll.results() if show_results and poll.status != "draft" else None,
                           unit_numbers=[u.number for u in _units() if u.id in poll.selected_unit_ids])


@bp.route("/<int:poll_id>/publish", methods=["POST"])
@login_required
def publish(poll_id):
    poll = _get(poll_id)
    if not is_board():
        abort(403)
    if poll.status != "draft":
        return redirect(url_for("votes.detail", poll_id=poll.id))
    if poll.deadline <= utcnow():
        flash(_("The deadline has already passed. Edit the draft and choose a later one."), "error")
        return redirect(url_for("votes.detail", poll_id=poll.id))
    voters = eligible_voters(poll)
    if not voters:
        flash(_("Nobody can vote yet: the chosen owners need sign-in accounts."), "error")
        return redirect(url_for("votes.detail", poll_id=poll.id))
    if poll.weighting == "share" and any(not unit.ownership_share for _u, unit, _w in voters):
        flash(_("Some units have no ownership share. Set their share on the unit page, or count one vote per unit."),
              "error")
        return redirect(url_for("votes.detail", poll_id=poll.id))
    for user, unit, weight in voters:
        poll.voters.append(PollVoter(user_id=user.id, unit_id=unit.id if unit else None, weight=weight))
    poll.status, poll.published_at = "open", utcnow()
    notify.send([u for u, _unit, _w in voters], _l("New vote: %(title)s", title=poll.title),
                url=url_for("votes.detail", poll_id=poll.id), kind="vote", include_actor=True,
                body=_l("Vote before %(when)s.", when=lazy_datetime(to_local(poll.deadline))))
    db.session.commit()
    flash(_("Published: %(n)s people can vote and have been notified.", n=len(voters)), "success")
    return redirect(url_for("votes.detail", poll_id=poll.id))


@bp.route("/<int:poll_id>/vote", methods=["POST"])
@login_required
def vote(poll_id):
    poll = _get(poll_id)
    me = poll.voter_for(current_user)
    if me is None:
        abort(403)
    if poll.state != "open":
        flash(_("Voting is closed."), "error")
        return redirect(url_for("votes.detail", poll_id=poll.id))
    raw = request.form.get("option_id", "")
    option = next((o for o in poll.options if str(o.id) == raw), None)
    if option is None:
        flash(_("Choose one of the answers."), "error")
        return redirect(url_for("votes.detail", poll_id=poll.id))
    changed = me.option_id is not None
    me.option_id, me.voted_at = option.id, utcnow()
    db.session.commit()
    flash(_("Your vote was changed.") if changed else _("Thank you, your vote was recorded."), "success")
    return redirect(url_for("votes.detail", poll_id=poll.id))


@bp.route("/<int:poll_id>/close", methods=["POST"])
@login_required
def close(poll_id):
    poll = _get(poll_id)
    if not is_board():
        abort(403)
    if poll.state == "open":
        poll.status, poll.closed_at = "closed", utcnow()
        db.session.commit()
        check_polls()
        flash(_("Voting closed. Voters can now see the results."), "success")
    return redirect(url_for("votes.detail", poll_id=poll.id))


@bp.route("/<int:poll_id>/delete", methods=["POST"])
@login_required
def delete(poll_id):
    poll = _get(poll_id)
    if not is_board():
        abort(403)
    db.session.delete(poll)
    db.session.commit()
    flash(_("Vote deleted."), "success")
    return redirect(url_for("votes.index"))
