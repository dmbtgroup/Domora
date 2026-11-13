"""Home insurance per household: one policy covers a unit's owners together, and one its tenants.

Each policy names a policyholder (one person in the group) and has a certificate file and
a policy period. Renewals add a new policy; the one ending last counts.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import files
from ..extensions import db
from ..models import InsurancePolicy, Owner, Unit
from ..utils import parse_date
from ..i18n import gettext as _

bp = Blueprint("insurance", __name__)

GROUP_LABELS = {"owners": "Owners", "tenants": "Tenants"}


def can_manage_group(unit_id, group, user=None):
    """Managers: any unit. A unit's owner account: both of that unit's households (so an owner
    can file their tenant's certificate). A tenant account: only the unit's tenant policy."""
    user = user or current_user
    if user.is_admin:
        return True
    if user.unit_id is None or user.unit_id != unit_id:
        return False
    return user.role == "owner" or (group == "tenants" and user.role == "tenant")


def can_manage(person, user=None):
    return can_manage_group(person.unit_id, person.insurance_group, user)


def can_view(policy, user=None):
    user = user or current_user
    return user.is_admin or (user.unit_id is not None and user.unit_id == policy.unit_id)


def _back(unit):
    from ..navigation import safe_back
    if safe_back():
        return safe_back()
    if request.args.get("next") == "dashboard" or not current_user.is_admin:
        return url_for("dashboard.index")
    return url_for("units.detail", unit_id=unit.id)


@bp.route("/units/<int:unit_id>/insurance/<group>/new", methods=["GET", "POST"])
@login_required
def new(unit_id, group):
    if group not in GROUP_LABELS:
        abort(404)
    unit = db.session.get(Unit, unit_id) or abort(404)
    if not can_manage_group(unit.id, group):
        abort(403)
    members = unit.members(group)
    if not members:
        flash(_("Add the unit's %(group)s first; the policy names one of them as policyholder.", group=group), "error")
        return redirect(_back(unit))
    last = unit.current_policy(group)
    if request.method == "POST":
        errors, dates = [], {}
        raw = request.form.get("policyholder_id", "")
        if raw:
            holder = next((m for m in members if str(m.id) == raw), None)
            if holder is None:
                errors.append(_("Choose the policyholder."))
        else:  # not chosen: the household's primary contact (or its only member)
            holder = next((m for m in members if m.is_primary), members[0])
        for field, label in (("valid_from", "Start date"), ("valid_until", "End date")):
            try:
                dates[field] = parse_date(request.form.get(field), label)
            except ValueError as exc:
                errors.append(str(exc))
        if len(dates) == 2 and dates["valid_until"] < dates["valid_from"]:
            errors.append(_("The end date is before the start date."))
        stored = files.take_upload(request.files.get("file"), errors, required=True)
        if not errors:
            db.session.add(InsurancePolicy(
                owner=holder, unit_id=unit.id, holder_group=group, file=stored,
                valid_from=dates["valid_from"], valid_until=dates["valid_until"],
                provider=request.form.get("provider", "").strip()[:200] or None,
                policy_number=request.form.get("policy_number", "").strip()[:100] or None))
            db.session.commit()
            flash(_("Tenant insurance saved for unit %(number)s.", number=unit.number) if group == "tenants"
                  else _("Owners’ insurance saved for unit %(number)s.", number=unit.number), "success")
            return redirect(_back(unit))
        for e in errors:
            flash(e, "error")
        return render_template("insurance/form.html", unit=unit, group=group, members=members, last=last,
                               form=request.form, accept=files.ACCEPT), 400
    preferred = request.args.get("holder", "")
    separate = bool(request.args.get("separate")) and last is not None
    if separate:  # another co-owner's own policy: default to someone who isn't the main policyholder
        default = next((m for m in members if m.id != last.owner_id), members[0])
    else:
        default = next((m for m in members if str(m.id) == preferred), None) \
            or (last.owner if last and last.owner in members else None) \
            or next((m for m in members if m.is_primary), members[0])
    form = {"policyholder_id": str(default.id),
            "provider": "" if separate or not last else last.provider or "",
            "policy_number": "" if separate or not last else last.policy_number or ""}
    return render_template("insurance/form.html", unit=unit, group=group, members=members, last=last,
                           form=form, accept=files.ACCEPT, separate=separate)


@bp.route("/people/<int:owner_id>/insurance/new")
@login_required
def new_for_person(owner_id):
    """Older per-person link: go to the person's household policy with them as policyholder."""
    person = db.session.get(Owner, owner_id) or abort(404)
    return redirect(url_for("insurance.new", unit_id=person.unit_id, group=person.insurance_group,
                            holder=person.id, **({"next": request.args["next"]} if "next" in request.args else {})))


@bp.route("/insurance/<int:policy_id>/file")
@login_required
def download(policy_id):
    policy = db.session.get(InsurancePolicy, policy_id) or abort(404)
    if policy.file is None:
        abort(404)
    if not can_view(policy):
        abort(403)
    return files.send(policy.file)


@bp.route("/insurance/<int:policy_id>/delete", methods=["POST"])
@login_required
def delete(policy_id):
    policy = db.session.get(InsurancePolicy, policy_id) or abort(404)
    if not current_user.is_admin:
        abort(403)
    unit_id = policy.unit_id
    db.session.delete(policy)
    db.session.commit()
    flash(_("Insurance policy removed."), "success")
    return redirect(url_for("units.detail", unit_id=unit_id))
