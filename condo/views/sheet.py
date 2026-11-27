"""Link the app to the condo's Google Sheet transaction log.

A manager saves the sheet's link, previews what a sync would load, and confirms
it. Once a sync has been confirmed, automatic sync can be switched on: a Vercel
cron job calls /sheet/cron, which re-syncs whenever the sheet's contents change.
"""
import hmac
import os

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .. import sheet_sync
from ..extensions import csrf, db
from ..models import ImportBatch, Setting, utcnow
from ..utils import admin_required
from ..i18n import gettext as _, ngettext

bp = Blueprint("sheet", __name__, url_prefix="/sheet")


@bp.route("/", methods=["GET", "POST"])
@admin_required
def index():
    cfg = sheet_sync.config()
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        try:
            sheet_sync.export_url(url)
        except sheet_sync.SyncError as exc:
            flash(str(exc), "error")
            return redirect(url_for("sheet.index"))
        if url != cfg.get("url"):
            cfg = {"url": url, "auto": False}  # a new sheet must be confirmed again
        Setting.put("sheet", cfg)
        db.session.commit()
        flash(_("Sheet link saved. Preview a sync to check what it will load."), "success")
        return redirect(url_for("sheet.index"))
    plan = _latest_plan()
    clashes = sheet_sync.clashing_entries(plan) if plan else []
    return render_template("sheet/index.html", cfg=cfg, last=Setting.get("sheet_last_sync"), clashes=clashes,
                           last_check=Setting.get("sheet_last_check"), last_error=Setting.get("sheet_last_error"),
                           cron_ready=bool(os.environ.get("CRON_SECRET")))


@bp.route("/preview", methods=["POST"])
@admin_required
def preview():
    """Build a preview from the linked sheet, or from an uploaded copy of it."""
    file = request.files.get("workbook")
    try:
        if file and file.filename:
            data, source = file.read(), file.filename
        else:
            cfg = sheet_sync.config()
            if not cfg.get("url"):
                flash(_("Save the sheet link first, or upload a copy of the sheet."), "error")
                return redirect(url_for("sheet.index"))
            data, source = sheet_sync.fetch(cfg["url"]), "Google Sheet"
        plan = sheet_sync.parse(data)
    except sheet_sync.SyncError as exc:
        flash(str(exc), "error")
        return redirect(url_for("sheet.index"))
    plan["loaded"] = None
    batch = ImportBatch(filename=source[:255], payload=plan, created_by_id=current_user.id)
    db.session.add(batch)
    db.session.commit()
    return redirect(url_for("sheet.review", batch_id=batch.id))


def _plan_batch(batch_id):
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None or batch.payload.get("kind") != "sheet":
        abort(404)
    return batch


@bp.route("/preview/<int:batch_id>")
@admin_required
def review(batch_id):
    batch = _plan_batch(batch_id)
    return render_template("sheet/preview.html", batch=batch, plan=batch.payload)


@bp.route("/preview/<int:batch_id>/apply", methods=["POST"])
@admin_required
def apply(batch_id):
    batch = _plan_batch(batch_id)
    if batch.payload.get("loaded"):
        flash(_("This preview was already loaded."), "info")
        return redirect(url_for("sheet.review", batch_id=batch.id))
    summary = sheet_sync.apply(batch.payload)
    payload = dict(batch.payload)
    payload["loaded"] = {"at": summary["at"], "by": current_user.name}
    batch.payload = payload
    cfg = sheet_sync.config()
    cfg["confirmed"] = True
    Setting.put("sheet", cfg)
    Setting.put("sheet_last_error", None)
    db.session.commit()
    problems = len(summary["mismatches"]) + len(summary["skipped"])
    flash("Sheet loaded." + (f" {problems} item(s) need a look — see below." if problems else
                             " Every unit's balance matches the sheet."), "success" if not problems else "info")
    return redirect(url_for("sheet.index"))


def _latest_plan():
    batches = db.session.scalars(db.select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(20)).all()
    return next((b.payload for b in batches if b.payload.get("kind") == "sheet" and b.payload.get("loaded")), None)


@bp.route("/resolve-clashes", methods=["POST"])
@admin_required
def resolve_clashes():
    """Remove app-entered charges that block the sheet's entries, then load the sheet again."""
    plan = _latest_plan()
    if not plan:
        flash(_("Load the sheet once first."), "error")
        return redirect(url_for("sheet.index"))
    clashes = sheet_sync.clashing_entries(plan)
    for c in clashes:
        db.session.delete(c)
    db.session.flush()
    summary = sheet_sync.apply(plan)
    problems = len(summary["mismatches"]) + len(summary["skipped"])
    flash(ngettext("Removed %(num)s old entry and re-applied the sheet.",
                   "Removed %(num)s old entries and re-applied the sheet.", len(clashes)), "success" if not problems else "info")
    flash(_("Every unit's balance now matches the sheet.") if not problems
          else _("%(problems)s item(s) still need a look.", problems=problems), "success" if not problems else "info")
    return redirect(url_for("sheet.index"))


@bp.route("/auto", methods=["POST"])
@admin_required
def auto():
    cfg = sheet_sync.config()
    turn_on = request.form.get("auto") == "on"
    if turn_on and not (cfg.get("url") and cfg.get("confirmed")):
        flash(_("Load the sheet once from a preview before turning on automatic sync."), "error")
        return redirect(url_for("sheet.index"))
    cfg["auto"] = turn_on
    Setting.put("sheet", cfg)
    db.session.commit()
    flash("Automatic sync is " + ("on." if turn_on else "off."), "success")
    return redirect(url_for("sheet.index"))


@bp.route("/cron")
@csrf.exempt
def cron():
    """Called by Vercel Cron with `Authorization: Bearer $CRON_SECRET`."""
    secret = os.environ.get("CRON_SECRET", "")
    given = request.headers.get("Authorization", "")
    if not secret or not hmac.compare_digest(given, f"Bearer {secret}"):
        abort(403)
    try:
        from .fees import check_renewals
        check_renewals()  # reminds the board two months before the condo-fee period ends
        from .maintenance import check_routine_reminders
        check_routine_reminders()  # routine maintenance due soon, contracts ending
        from .votes import check_polls
        check_polls()  # vote reminders a day before the deadline, results when it closes
    except Exception:  # never let the reminder stop the sheet sync
        db.session.rollback()
        current_app.logger.exception("Fee renewal check failed")
    try:
        from .. import central
        central.push_members()  # keeps belcentra.com's list of sign-ins current, whatever changed it
    except Exception:
        current_app.logger.exception("Sending sign-ins to belcentra.com failed")
    try:
        status = sheet_sync.run_auto_sync()
        Setting.put("sheet_last_error", None)
        db.session.commit()
    except sheet_sync.SyncError as exc:
        db.session.rollback()
        Setting.put("sheet_last_error", {"at": utcnow().isoformat(timespec="seconds"), "message": str(exc)})
        db.session.commit()
        current_app.logger.warning("Sheet sync failed: %s", exc)
        return {"status": "error", "message": str(exc)}, 200
    return {"status": status}
