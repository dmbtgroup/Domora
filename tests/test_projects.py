from condo.extensions import db
from condo.models import MaintenanceRequest, Unit, User

from .conftest import login


def test_side_menu_has_projects_and_maintenance(admin_client):
    page = admin_client.get("/").data.decode()
    assert '<span class="label">Projects &amp; Maintenance</span>' in page and 'class="rail"' in page


def test_board_adds_project_with_budget_and_target(admin_client):
    form = admin_client.get("/maintenance/new?kind=project").data.decode()
    assert "New project" in form and 'name="budget"' in form
    admin_client.post("/maintenance/new", data={"kind": "project", "title": "Roof replacement", "description": "Whole roof",
                                                "category": "Other", "priority": "normal", "location": "common",
                                                "budget": "85,000", "target_date": "2027-09-30"})
    p = db.session.scalar(db.select(MaintenanceRequest))
    assert (p.kind, p.budget_cents, str(p.target_date), p.unit_id) == ("project", 8500000, "2027-09-30", None)
    projects = admin_client.get("/maintenance/").data.decode()
    assert "Roof replacement" in projects and "$85,000.00" in projects and 'class="active">Projects' in projects
    repairs = admin_client.get("/maintenance/?tab=maintenance").data.decode()
    assert "Roof replacement" not in repairs
    detail = admin_client.get(f"/maintenance/{p.id}").data.decode()
    assert "← Back to Projects" in detail and "Move to Maintenance" in detail


def test_residents_see_building_projects_but_cannot_create_them(app, admin_client):
    admin_client.post("/maintenance/new", data={"kind": "project", "title": "Window replacement", "description": "All windows",
                                                "category": "Other", "priority": "normal", "location": "common"})
    admin_client.post("/logout")
    login(admin_client, "alice@test.com")
    page = admin_client.get("/maintenance/").data.decode()
    assert "Window replacement" in page and "New project" not in page
    p = db.session.scalar(db.select(MaintenanceRequest))
    assert admin_client.get(f"/maintenance/{p.id}").status_code == 200
    # a resident's "project" is filed as an ordinary maintenance request
    admin_client.post("/maintenance/new", data={"kind": "project", "title": "Dripping tap", "description": "Kitchen",
                                                "category": "Other", "priority": "normal", "location": "unit"})
    tap = db.session.scalar(db.select(MaintenanceRequest).where(MaintenanceRequest.title == "Dripping tap"))
    assert tap.kind == "maintenance"
    assert admin_client.post(f"/maintenance/{tap.id}/project", data={"kind": "project"}).status_code == 403


def test_board_moves_request_to_projects(app, admin_client):
    admin_client.post("/maintenance/new", data={"title": "Cracked foundation", "description": "North wall",
                                                "category": "Other", "priority": "high", "location": "common"})
    r = db.session.scalar(db.select(MaintenanceRequest))
    assert r.kind == "maintenance"
    admin_client.post(f"/maintenance/{r.id}/project", data={"kind": "project"})
    assert r.kind == "project"
    admin_client.post(f"/maintenance/{r.id}/project", data={"kind": "project", "budget": "12,500", "target_date": ""})
    assert r.budget_cents == 1250000
    assert any("Moved to Projects" in c.body for c in r.comments)


def test_board_deletes_projects_and_requests(app, admin_client):
    from condo.models import MaintenanceComment, Reimbursement
    admin_client.post("/maintenance/new", data={"kind": "project", "title": "Roof", "description": "Whole roof",
                                                "category": "Other", "priority": "normal", "location": "common"})
    admin_client.post("/maintenance/new", data={"title": "Leak", "description": "Drip", "category": "Plumbing",
                                                "priority": "normal", "location": "common"})
    project, leak = db.session.scalars(db.select(MaintenanceRequest).order_by(MaintenanceRequest.id)).all()
    admin_client.post(f"/maintenance/{leak.id}/comment", data={"body": "On it"})
    assert "maintenance/%d/delete" % project.id in admin_client.get("/maintenance/").data.decode()
    resp = admin_client.post(f"/maintenance/{project.id}/delete")
    assert resp.headers["Location"].endswith("/maintenance/?tab=projects")
    admin_client.post(f"/maintenance/{leak.id}/delete")
    assert db.session.scalar(db.select(db.func.count(MaintenanceRequest.id))) == 0
    assert db.session.scalar(db.select(db.func.count(MaintenanceComment.id))) == 0


def test_residents_delete_only_their_own_untouched_request(app, admin_client):
    admin_client.post("/logout")
    login(admin_client, "alice@test.com")
    admin_client.post("/maintenance/new", data={"title": "Squeaky door", "description": "Hinge", "category": "Other",
                                                "priority": "low", "location": "unit"})
    mine = db.session.scalar(db.select(MaintenanceRequest))
    assert "maintenance/%d/delete" % mine.id in admin_client.get(f"/maintenance/{mine.id}").data.decode()
    mine.status = "in_progress"
    db.session.commit()
    assert admin_client.post(f"/maintenance/{mine.id}/delete").status_code == 403
    mine.status = "open"
    db.session.commit()
    admin_client.post(f"/maintenance/{mine.id}/delete")
    assert db.session.scalar(db.select(MaintenanceRequest)) is None


def test_list_filter_is_active_done_all_with_a_status_dropdown(admin_client):
    for title, status in (("Leak", "open"), ("Door", "in_progress"), ("Light", "resolved")):
        db.session.add(MaintenanceRequest(title=title, description="x", category="Other", kind="maintenance", status=status,
                                          submitted_by=db.session.scalar(db.select(User).where(User.email == "admin@test.com"))))
    db.session.commit()
    page = admin_client.get("/maintenance/?tab=maintenance").data.decode()
    seg = page.split('class="segmented"')[1].split("</nav>")[0]
    from condo.models import ACTIVE_STATUSES
    reqs = db.session.scalars(db.select(MaintenanceRequest).where(MaintenanceRequest.kind == "maintenance")).all()
    active = sum(r.status in ACTIVE_STATUSES for r in reqs)
    assert f'Active <span class="count">{active}</span>' in seg and f'Done <span class="count">{len(reqs) - active}</span>' in seg \
        and f'All <span class="count">{len(reqs)}</span>' in seg
    assert '<option value="quote_review">' in page and 'class="tabs"' not in page.split("section-tabs")[-1]
    body = lambda url: admin_client.get(url).data.decode().split("<tbody>")[1]
    assert "Light" in body("/maintenance/?tab=maintenance&status=done") and "Leak" not in body("/maintenance/?tab=maintenance&status=done")
    picked = admin_client.get("/maintenance/?tab=maintenance&status=in_progress").data.decode()
    assert '<option value="in_progress" selected>' in picked and "Door" in picked.split("<tbody>")[1] and "Leak" not in picked.split("<tbody>")[1]


def test_resolve_a_repair_with_invoice_and_photos_owners_see_only_photos(app, admin_client):
    import io
    PDF = b"%PDF-1.4\n%%EOF"
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    alice = db.session.scalar(db.select(User).where(User.email == "alice@test.com"))
    req = MaintenanceRequest(title="Leaking faucet", description="Drips", category="Plumbing", unit=alice.unit,
                             submitted_by=alice)
    db.session.add(req)
    db.session.commit()
    admin_client.post(f"/maintenance/{req.id}/status", content_type="multipart/form-data", data={
        "status": "resolved", "priority": "normal", "invoice_files": [(io.BytesIO(PDF), "plumber.pdf")],
        "after_files": [(io.BytesIO(PNG), "fixed.png")]})
    req = db.session.get(MaintenanceRequest, req.id)
    assert req.status == "resolved" and sorted(f.kind for f in req.work_files) == ["after", "invoice"]
    admin_client.post(f"/maintenance/{req.id}/files", content_type="multipart/form-data",
                      data={"before_files": [(io.BytesIO(PNG), "before.png")]})
    assert len(db.session.get(MaintenanceRequest, req.id).work_files) == 3
    admin_client.post("/logout")
    login(admin_client, "alice@test.com")
    page = admin_client.get(f"/maintenance/{req.id}").data.decode()
    assert "fixed.png" in page and "plumber.pdf" not in page
    invoice = next(f for f in req.work_files if f.kind == "invoice")
    photo = next(f for f in req.work_files if f.kind == "after")
    assert admin_client.get(f"/maintenance/files/{invoice.id}").status_code == 403
    assert admin_client.get(f"/maintenance/files/{photo.id}").status_code == 200
