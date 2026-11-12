from condo.extensions import db
from condo.models import Document, MaintenanceRequest, Unit, User

from .conftest import login


def staff(perms, email="staff@x.com"):
    u = User(email=email, name="Hired Help", role="staff", job_title="Handyman")
    u.set_password("password123")
    u.permission_keys = perms
    db.session.add(u)
    db.session.commit()
    return u


def test_create_staff_account_from_accounts_tab(admin_client):
    form = admin_client.get("/residents/new").data.decode()
    assert "New staff account" in form and 'name="perms"' in form and 'name="unit_id"' not in form
    resp = admin_client.post("/residents/new", data={"role": "staff", "name": "Sparkle Cleaning", "email": "c@x.com",
                                                     "job_title": "Cleaning company", "perms": ["maintenance", "notices"],
                                                     "password": "password123"})
    assert resp.status_code == 302
    u = db.session.scalar(db.select(User).where(User.email == "c@x.com"))
    assert (u.role, u.unit_id, u.permission_keys, u.role_label) == ("staff", None, ["maintenance", "notices"],
                                                                     "Cleaning company")


def test_staff_only_reach_what_they_were_given(app, client):
    staff(["maintenance"])
    req = MaintenanceRequest(unit=db.session.scalar(db.select(Unit).where(Unit.number == "101")),
                             submitted_by=db.session.scalar(db.select(User).where(User.email == "alice@test.com")),
                             title="Leak", description="Drip")
    db.session.add(req)
    db.session.commit()
    login(client, "staff@x.com")
    nav = client.get("/").data.decode()
    assert ">Projects &amp; Maintenance<" in nav and ">Accounting<" not in nav and ">Directory<" not in nav
    assert "Leak" in client.get("/maintenance/?tab=maintenance").data.decode()
    client.post(f"/maintenance/{req.id}/status", data={"status": "in_progress", "priority": "high"})
    assert db.session.get(MaintenanceRequest, req.id).status == "in_progress"
    for path in ("/accounting/", "/directory/owners", "/units/", "/announcements/new", "/accounting/payments/new"):
        assert client.get(path).status_code in (302, 403), path


def test_staff_accounting_view_vs_record(app, client):
    staff(["accounting"])
    login(client, "staff@x.com")
    page = client.get("/accounting/").data.decode()
    assert "By unit" in page and "Add payment" not in page
    assert client.get("/accounting/payments/new").status_code == 403
    client.post("/logout")
    staff(["accounting_edit"], email="book@x.com")
    login(client, "book@x.com")
    assert "Add payment" in client.get("/accounting/").data.decode()
    assert client.get("/accounting/payments/new").status_code == 200


def test_staff_directory_is_read_only(app, client):
    staff(["directory"])
    login(client, "staff@x.com")
    page = client.get("/directory/owners").data.decode()
    assert page.count('class="tabs"') == 1 and "Board" not in page.split('class="tabs"')[1].split("</nav>")[0]
    assert "Add owner" not in page and "Create login" not in page
    assert client.get("/directory/accounts").status_code == 403


def test_staff_documents_and_notices(app, client):
    from io import BytesIO
    from condo.models import StoredFile
    doc = Document(title="Board notes", category="other", visibility="managers",
                   file=StoredFile(filename="n.pdf", content_type="application/pdf", size=3, data=b"pdf"))
    db.session.add(doc)
    db.session.commit()
    staff(["documents", "notices"])
    login(client, "staff@x.com")
    assert "Board notes" in client.get("/documents/").data.decode()
    assert client.get("/announcements/new").status_code == 200
    client.post("/announcements/new", data={"title": "Cleaning Tuesday", "body": "Hallways"})
    assert "Cleaning Tuesday" in client.get("/announcements/").data.decode()


def test_staff_without_permissions_sees_little(app, client):
    staff([])
    login(client, "staff@x.com")
    dash = client.get("/").data.decode()
    assert "Hired Help" in dash and "No unit linked" not in dash
    assert client.get("/maintenance/").status_code == 200  # only their own requests
