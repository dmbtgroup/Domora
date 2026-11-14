"""The "← Back" link shown on every page below the main menu pages.

Each page has a logical parent (used when the page was opened directly, e.g. from an
email link). In the browser, the link goes back to the previous portal page instead when
there is one, so filters, periods and tabs are kept.
"""
from flask import request, url_for
from flask_login import current_user

# Menu pages: no back link.
TOP_LEVEL = {
    "dashboard.index", "units.index", "directory.contacts", "directory.board", "directory.accounts",
    "directory.index", "billing.index", "maintenance.index", "announcements.index", "documents.index",
    "amenities.index", "auth.login", "billing.my_account", "notifications.index", "reimbursements.index", "votes.index", "partners.index",
}

from .i18n import lazy_gettext as _l


def _unit_parent(unit_id):
    return (_l("Unit"), url_for("units.detail", unit_id=unit_id)) if current_user.is_admin else \
        (_l("Dashboard"), url_for("dashboard.index"))


def parent_for(endpoint, args):
    """(label, url) of the page a page belongs under, or None for menu pages."""
    from .extensions import db
    from .models import Owner

    if not endpoint or endpoint in TOP_LEVEL or endpoint == "static":
        return None
    admin = current_user.is_authenticated and current_user.is_admin
    if endpoint in ("units.detail", "units.new"):
        return _l("Units"), url_for("units.index")
    if endpoint == "units.edit":
        return _l("Unit"), url_for("units.detail", unit_id=args["unit_id"])
    if endpoint in ("owners.new", "insurance.new"):
        return _unit_parent(args["unit_id"])
    if endpoint == "owners.edit":
        owner = db.session.get(Owner, args["owner_id"])
        if request.args.get("next") == "directory" or owner is None:
            return _l("Directory"), url_for("directory.index")
        return _l("Unit"), url_for("units.detail", unit_id=owner.unit_id)
    if endpoint == "owners.new_from_directory":
        return _l("Directory"), url_for("directory.contacts", group=args["group"])
    if endpoint.startswith("residents."):
        return _l("Accounts"), url_for("directory.accounts")
    if endpoint.startswith("billing."):
        if admin or current_user.role == "owner":
            return _l("Accounting"), url_for("billing.index")
        return _l("Dashboard"), url_for("dashboard.index")
    if endpoint == "fees.index":
        return _l("Accounting"), url_for("billing.index")
    if endpoint.startswith("fees."):
        return _l("Fees & contributions"), url_for("fees.index")
    if endpoint.startswith("reimbursements."):
        return _l("Reimbursements"), url_for("reimbursements.index")
    if endpoint.startswith("maintenance.routine_") and endpoint != "maintenance.routine_new":
        return _l("Routine"), url_for("maintenance.index", tab="routine")
    if endpoint == "maintenance.routine_new":
        return _l("Routine"), url_for("maintenance.index", tab="routine")
    if endpoint.startswith("maintenance."):
        from .models import MaintenanceRequest
        req = db.session.get(MaintenanceRequest, args["request_id"]) if "request_id" in args else None
        project = req.is_project if req else request.values.get("kind") == "project"
        return (_l("Projects"), url_for("maintenance.index", tab="projects")) if project else \
            (_l("Maintenance"), url_for("maintenance.index", tab="maintenance"))
    if endpoint.startswith("partners."):
        return _l("Business partners"), url_for("partners.index")
    if endpoint.startswith("votes."):
        return _l("Votes"), url_for("votes.index")
    if endpoint.startswith("announcements."):
        return _l("Notices"), url_for("announcements.index")
    if endpoint.startswith("documents."):
        return _l("Documents"), url_for("documents.index")
    if endpoint.startswith("amenities."):
        return _l("Amenities"), url_for("amenities.index")
    if endpoint == "sheet.index":
        return _l("Accounting"), url_for("billing.index")
    if endpoint.startswith("sheet."):
        return _l("Google Sheet"), url_for("sheet.index")
    if endpoint == "importer.upload":
        return _l("Units"), url_for("units.index")
    if endpoint.startswith("importer."):
        return _l("Import"), url_for("importer.upload")
    return _l("Dashboard"), url_for("dashboard.index")


def here():
    """The current page's path and query, to pass as ?back= on links into edit pages."""
    return request.full_path.rstrip("?")


def safe_back():
    """The ?back= page a link came from, if it is a page of this portal (never another site)."""
    target = request.args.get("back", "") or request.form.get("back", "")
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def label_for(path):
    from .extensions import db
    from .models import Unit
    import re

    bare = path.split("?", 1)[0]
    m = re.match(r"^/units/(\d+)$", bare)
    if m:
        unit = db.session.get(Unit, int(m.group(1)))
        return _l("unit %(number)s", number=unit.number) if unit else _l("the unit")
    m = re.match(r"^/maintenance/(\d+)$", bare)
    if m:
        from .models import MaintenanceRequest
        req = db.session.get(MaintenanceRequest, int(m.group(1)))
        return (_l("the project") if req.is_project else _l("the request")) if req else _l("Projects & Maintenance")
    if bare.startswith("/maintenance/partners"):
        return _l("Business partners")
    if bare.startswith("/maintenance"):
        return _l("Maintenance") if "tab=maintenance" in path else _l("Routine") if "tab=routine" in path else _l("Projects")
    for prefix, label in (("/directory", _l("Directory")), ("/accounting", _l("Accounting")), ("/maintenance", _l("Maintenance")),
                          ("/documents", _l("Documents")), ("/reimbursements", _l("Reimbursements")), ("/units", _l("Units")),
                          ("/notifications", _l("Notifications")), ("/votes", _l("Votes")), ("/sheet", _l("Google Sheet"))):
        if bare.startswith(prefix):
            return label
    return _l("the previous page")


def back_link():
    try:
        if not current_user.is_authenticated:
            return None
        came_from = safe_back()
        if came_from and request.endpoint not in TOP_LEVEL:
            return {"label": label_for(came_from), "url": came_from}
        parent = parent_for(request.endpoint, request.view_args or {})
    except Exception:  # never break a page over its back link
        return None
    if parent is None:
        return None
    return {"label": parent[0], "url": parent[1]}
