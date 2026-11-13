from datetime import date, timedelta

from condo.extensions import db
from condo.models import Expense, Notification, RoutineLog, RoutineTask, User

from .conftest import login


def make(admin_client, **extra):
    data = {"title": "Snow removal", "frequency": "yearly", "category": "Common area",
            "next_due": "2026-11-15", "season_from": "11", "season_to": "4", "vendor": "Déneigement Laval",
            "vendor_contact": "514-555-0199", "estimated": "4,800", "contract_ends": "2027-04-30"}
    data.update(extra)
    return admin_client.post("/maintenance/routine/new", data=data)


def test_routine_is_the_second_tab(admin_client):
    page = admin_client.get("/maintenance/").data.decode()
    tabs = page.split('class="section-tabs"')[1].split("</nav>")[0]
    assert tabs.index("Projects") < tabs.index("Routine") < tabs.index("Maintenance")
    empty = admin_client.get("/maintenance/?tab=routine").data.decode()
    assert "No routine maintenance yet." in empty and "+ Snow removal" in empty and "+ Gutter cleaning" in empty
    form = admin_client.get("/maintenance/routine/new?title=Snow+removal&frequency=yearly&month=11&season_from=11&season_to=4").data.decode()
    assert 'value="Snow removal"' in form and '<option value="11" selected>' in form


def test_add_and_mark_done_moves_the_due_date_and_records_cost(admin_client):
    resp = make(admin_client)
    t = db.session.scalar(db.select(RoutineTask))
    assert resp.headers["Location"].endswith(f"/maintenance/routine/{t.id}")
    assert (t.season_from, t.season_to, t.estimated_cents, t.vendor) == (11, 4, 480000, "Déneigement Laval")
    page = admin_client.get("/maintenance/?tab=routine").data.decode()
    assert "Snow removal" in page and "Every year" in page and "Season: November to April" in page

    admin_client.post(f"/maintenance/routine/{t.id}/done", data={"done_on": "2026-11-20", "cost": "4,800",
                                                                 "record_cost": "1", "note": "Contract signed"})
    assert t.next_due == date(2027, 11, 15)
    log = db.session.scalar(db.select(RoutineLog))
    assert log.cost_cents == 480000 and log.expense_id
    exp = db.session.get(Expense, log.expense_id)
    assert exp.amount_cents == -480000 and exp.category == "maintenance" and "Snow removal" in exp.description
    detail = admin_client.get(f"/maintenance/routine/{t.id}").data.decode()
    assert "Contract signed" in detail and "November 15, 2027" in detail


def test_frequencies_and_late_visits():
    t = RoutineTask(title="Gutters", frequency="twice_yearly", next_due=date(2026, 5, 1))
    t.advance(date(2026, 5, 3))
    assert t.next_due == date(2026, 11, 1)
    t.advance(date(2027, 6, 1))  # skipped a visit: lands on the next one after the visit
    assert t.next_due == date(2027, 11, 1)
    m = RoutineTask(title="Filters", frequency="monthly", next_due=date(2026, 1, 31))
    m.advance(date(2026, 1, 31))
    assert m.next_due == date(2026, 2, 28)


def test_board_reminded_once_before_due_and_before_contract_ends(app, admin_client):
    from condo.views.maintenance import check_routine_reminders
    soon = date.today() + timedelta(days=10)
    make(admin_client, next_due=soon.isoformat(), contract_ends=(date.today() + timedelta(days=45)).isoformat())
    with app.test_request_context():
        assert check_routine_reminders() == 2
        assert check_routine_reminders() == 0
    admin = db.session.scalar(db.select(User).where(User.email == "admin@test.com"))
    titles = [n.title for n in db.session.scalars(db.select(Notification).where(Notification.user_id == admin.id))]
    assert any(t.startswith("Routine maintenance due") for t in titles)
    assert any(t.startswith("Contract ends") for t in titles)
    page = admin_client.get("/maintenance/?tab=routine").data.decode()
    assert "Due soon" in page and "Contract ending" in page


def test_residents_see_the_schedule_but_cannot_change_it(app, admin_client):
    make(admin_client)
    t = db.session.scalar(db.select(RoutineTask))
    admin_client.post("/logout")
    login(admin_client, "alice@test.com")
    page = admin_client.get("/maintenance/?tab=routine").data.decode()
    assert "Snow removal" in page and "New routine task" not in page
    assert "Mark as done" not in admin_client.get(f"/maintenance/routine/{t.id}").data.decode()
    assert admin_client.post(f"/maintenance/routine/{t.id}/done", data={"done_on": "2026-11-20"}).status_code == 403
    assert admin_client.get("/maintenance/routine/new").status_code == 403


def test_pause_and_delete(admin_client):
    make(admin_client)
    t = db.session.scalar(db.select(RoutineTask))
    admin_client.post(f"/maintenance/routine/{t.id}/edit", data={"title": "Snow removal", "frequency": "yearly",
                                                                 "category": "Common area", "next_due": "2026-11-15"})
    assert t.active is False
    assert "Snow removal" not in admin_client.get("/maintenance/?tab=routine").data.decode().split("<tbody>")[1]
    page = admin_client.get("/maintenance/?tab=routine&show=all").data.decode()
    assert "Snow removal" in page and 'Active <span class="count">0</span>' in page and 'All <span class="count">1</span>' in page
    admin_client.post(f"/maintenance/routine/{t.id}/delete")
    assert db.session.scalar(db.select(RoutineTask)) is None


def test_routine_in_french(admin_client):
    make(admin_client)
    admin_client.get("/language/fr")
    page = admin_client.get("/maintenance/?tab=routine").data.decode()
    assert "Entretien courant" in page and "Chaque année" in page and "novembre" in page


def test_back_link_returns_to_routine(admin_client):
    make(admin_client)
    t = db.session.scalar(db.select(RoutineTask))
    assert "← Back to Routine" in admin_client.get(f"/maintenance/routine/{t.id}").data.decode()


def test_move_a_maintenance_request_to_routine(admin_client):
    from condo.models import MaintenanceComment, MaintenanceRequest
    admin = db.session.scalar(db.select(User).where(User.email == "admin@test.com"))
    req = MaintenanceRequest(title="Clean the dryer vents", description="Lint builds up every year.", category="Appliance",
                             submitted_by=admin, kind="maintenance")
    db.session.add(req)
    db.session.commit()
    page = admin_client.get(f"/maintenance/{req.id}").data.decode()
    assert "Move to Routine" in page and f"/maintenance/routine/new?from_request={req.id}" in page
    form = admin_client.get(f"/maintenance/routine/new?from_request={req.id}").data.decode()
    assert 'value="Clean the dryer vents"' in form and "Lint builds up" in form and "Saving closes the request" in form
    resp = admin_client.post(f"/maintenance/routine/new?from_request={req.id}",
                             data={"title": "Clean the dryer vents", "frequency": "yearly", "category": "Appliance",
                                   "next_due": "2027-03-01", "from_request": str(req.id)})
    task = db.session.scalar(db.select(RoutineTask))
    assert resp.status_code == 302 and task.title == "Clean the dryer vents"
    req = db.session.get(MaintenanceRequest, req.id)
    assert req.status == "closed"
    assert any("Moved to routine maintenance" in c.body for c in req.comments)
