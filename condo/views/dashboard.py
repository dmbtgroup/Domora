from datetime import datetime

from flask import Blueprint, render_template
from flask_login import current_user

from ..extensions import db
from ..models import ACTIVE_STATUSES, Announcement, Booking, MaintenanceRequest, Owner, Unit
from ..models import Reimbursement
from ..i18n import gettext as _
from .directory import board_members
from .reimbursements import is_accounting, is_president
from .documents import current_building_insurance
from .insurance import can_manage_group
from .maintenance import can_decide

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    if not current_user.is_authenticated:  # the public home page, with sign-in
        return render_template("home.html", year=datetime.now().year)
    now = datetime.now()
    announcements = db.session.scalars(
        db.select(Announcement).order_by(Announcement.pinned.desc(), Announcement.created_at.desc()).limit(5)
    ).all()

    if current_user.is_admin:
        units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all()
        balances = [(u, u.balance) for u in units]
        outstanding = sum(b for _, b in balances if b > 0)
        delinquent = sorted([(u, b) for u, b in balances if b > 0], key=lambda x: -x[1])[:5]
        open_requests = db.session.scalars(
            db.select(MaintenanceRequest)
            .where(MaintenanceRequest.status.in_(ACTIVE_STATUSES))
            .order_by(MaintenanceRequest.created_at.desc())
        ).all()
        stats = {
            "units": len(units),
            "occupied": sum(1 for u in units if u.owners or u.residents),
            "people": db.session.scalar(db.select(db.func.count(Owner.id))),
            "outstanding": outstanding,
            "open_requests": len(open_requests),
        }
        upcoming = db.session.scalars(
            db.select(Booking).where(Booking.end >= now).order_by(Booking.start).limit(5)
        ).all()
        awaiting = [r for r in open_requests if r.awaiting_owner]
        insurance_alerts = []
        for u in units:
            for group, label in (("owners", _("Owners")), ("tenants", _("Tenants"))):
                state = u.group_insurance(group)
                if state in ("missing", "expired", "expiring"):
                    insurance_alerts.append((u, label, state))
        return render_template("dashboard_admin.html", stats=stats, delinquent=delinquent,
                               open_requests=[r for r in open_requests if not r.awaiting_owner][:5],
                               awaiting=awaiting, announcements=announcements, upcoming=upcoming,
                               insurance_alerts=insurance_alerts, board=board_members(),
                               reimb_waiting=_reimb_waiting(),
                               building=current_building_insurance(current_user))

    if current_user.is_staff:
        from ..models import STAFF_PERMISSIONS
        active = []
        if current_user.can("maintenance"):
            active = db.session.scalars(db.select(MaintenanceRequest).where(
                MaintenanceRequest.status.in_(ACTIVE_STATUSES)).order_by(MaintenanceRequest.created_at.desc()).limit(10)).all()
        return render_template("dashboard_staff.html", perms=STAFF_PERMISSIONS, active_requests=active,
                               announcements=announcements)

    unit = current_user.unit
    my_requests = []
    if unit:
        my_requests = db.session.scalars(
            db.select(MaintenanceRequest).where(MaintenanceRequest.unit_id == unit.id)
            .order_by(MaintenanceRequest.created_at.desc()).limit(5)
        ).all()
    my_bookings = db.session.scalars(
        db.select(Booking).where(Booking.user_id == current_user.id, Booking.end >= now)
        .order_by(Booking.start)
    ).all()
    to_approve = [r for r in my_requests if can_decide(r)] if unit else []
    if unit and current_user.role == "owner":  # don't miss older ones beyond the latest five
        to_approve = db.session.scalars(db.select(MaintenanceRequest).where(
            MaintenanceRequest.unit_id == unit.id, MaintenanceRequest.status == "awaiting_owner")).all()
    my_insurance = [g for g in ("owners", "tenants")
                    if unit and unit.members(g) and can_manage_group(unit.id, g)]
    return render_template("dashboard_resident.html", unit=unit, my_requests=my_requests,
                           my_bookings=my_bookings, announcements=announcements, to_approve=to_approve,
                           my_insurance=my_insurance, building=current_building_insurance(current_user),
                           board=board_members(), reimb_waiting=_reimb_waiting(),
                           my_reimbursements=db.session.scalars(db.select(Reimbursement).where(
                               Reimbursement.unit_id == unit.id).order_by(Reimbursement.created_at.desc()).limit(5)).all()
                           if unit and current_user.role == "owner" else [])


def _reimb_waiting():
    """Reimbursements waiting for this user's decision or for them to record."""
    statuses = (["submitted"] if is_president() else []) + (["approved"] if is_accounting() else [])
    if not statuses:
        return []
    return db.session.scalars(db.select(Reimbursement).where(Reimbursement.status.in_(statuses))
                              .order_by(Reimbursement.created_at)).all()
