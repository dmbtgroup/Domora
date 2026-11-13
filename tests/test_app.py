from datetime import date, datetime, timedelta

import pytest

from condo.extensions import db
from condo.models import Booking, Charge, MaintenanceRequest, Payment, Unit, User
from condo.utils import money, parse_money
from condo.views.billing import generate_dues

from .conftest import login


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def user(email):
    return db.session.scalar(db.select(User).where(User.email == email))


# --- utils -----------------------------------------------------------------

def test_money_formatting():
    assert money(0) == "$0.00"
    assert money(123456) == "$1,234.56"
    assert money(-500) == "-$5.00"


@pytest.mark.parametrize("raw,cents", [("12", 1200), ("$1,234.50", 123450), ("0.01", 1)])
def test_parse_money_valid(raw, cents):
    assert parse_money(raw) == cents


@pytest.mark.parametrize("raw", ["", "abc", "-5", "0", "1.234"])
def test_parse_money_invalid(raw):
    with pytest.raises(ValueError):
        parse_money(raw)


# --- auth & permissions ------------------------------------------------------

def test_requires_login(client):
    resp = client.get("/units/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_home_page_for_visitors_with_sign_in(client):
    page = client.get("/").data.decode()
    assert "all in one place." in page and "Everything your building needs" in page
    assert page.count('action="/login"') == 2 and 'name="csrf_token"' in page
    assert "sales@dmbtgroup.com" in page and 'class="rail"' not in page
    resp = client.post("/login", data={"email": "alice@test.com", "password": "password123"})
    assert resp.headers["Location"] == "/"
    assert "all in one place." not in client.get("/").data.decode()  # signed in: the dashboard
    resp = client.post("/logout")
    assert resp.headers["Location"] == "/"


def test_home_page_in_french(client):
    client.get("/language/fr")
    page = client.get("/").data.decode()
    assert '<html lang="fr">' in page and "Se connecter" in page and "Tout ce dont votre immeuble a besoin" in page


def test_bad_login(client):
    resp = login(client, "alice@test.com", "wrong")
    assert b"Invalid email or password" in resp.data


def test_inactive_user_cannot_login(app, client):
    user("bob@test.com").active = False
    db.session.commit()
    resp = login(client, "bob@test.com")
    assert b"deactivated" in resp.data
    assert client.get("/units/").status_code == 302


def test_login_ignores_external_next(client):
    resp = client.post("/login?next=https://evil.example/", data={"email": "alice@test.com",
                                                                 "password": "password123"})
    assert resp.headers["Location"] == "/"


@pytest.mark.parametrize("path", ["/units/", "/residents/", "/announcements/new",
                                  "/amenities/new", "/accounting/payments/new"])
def test_resident_blocked_from_admin_pages(alice_client, path):
    assert alice_client.get(path).status_code == 403


def test_resident_cannot_view_other_units_ledger(alice_client):
    assert alice_client.get(f"/accounting/unit/{unit('102').id}").status_code == 403
    assert alice_client.get(f"/accounting/unit/{unit('101').id}").status_code == 200


def test_all_main_pages_render(admin_client):
    for path in ["/", "/units/", "/directory/owners", "/directory/tenants", "/directory/accounts", "/directory/owners/new", "/accounting/", "/maintenance/", "/announcements/",
                 "/amenities/", "/account", f"/units/{unit('101').id}", "/maintenance/new",
                 "/accounting/charges/new", "/accounting/payments/new", f"/accounting/unit/{unit('101').id}"]:
        assert admin_client.get(path).status_code == 200, path


def test_resident_dashboard(alice_client):
    resp = alice_client.get("/")
    assert resp.status_code == 200 and b"Unit 101" in resp.data


def test_change_password(alice_client, client):
    resp = alice_client.post("/account", data={"current_password": "password123",
                                               "new_password": "newpassword1",
                                               "confirm_password": "newpassword1"})
    assert resp.status_code == 302
    assert user("alice@test.com").check_password("newpassword1")


# --- units & residents -------------------------------------------------------

def test_create_and_edit_unit(admin_client):
    resp = admin_client.post("/units/new", data={"number": "301", "floor": "3", "monthly_fee": "450.00"})
    assert resp.status_code == 302
    u = unit("301")
    assert u.monthly_fee_cents == 45000
    resp = admin_client.post(f"/units/{u.id}/edit", data={"number": "301", "monthly_fee": "500"})
    assert resp.status_code == 302 and unit("301").monthly_fee_cents == 50000


def test_duplicate_unit_rejected(admin_client):
    resp = admin_client.post("/units/new", data={"number": "101"})
    assert resp.status_code == 400 and b"already exists" in resp.data


def test_cannot_delete_occupied_unit(admin_client):
    admin_client.post(f"/units/{unit('101').id}/delete")
    assert unit("101") is not None
    admin_client.post(f"/units/{unit('201').id}/delete")
    assert unit("201") is None


def test_create_resident_requires_unit(admin_client):
    data = {"name": "Carol", "email": "carol@test.com", "role": "owner", "password": "password123"}
    assert admin_client.post("/residents/new", data=data).status_code == 400
    data["unit_id"] = str(unit("201").id)
    assert admin_client.post("/residents/new", data=data).status_code == 302
    assert user("carol@test.com").unit.number == "201"


def test_admin_cannot_demote_self(admin_client):
    admin = user("admin@test.com")
    resp = admin_client.post(f"/residents/{admin.id}/edit",
                             data={"name": "Admin", "email": "admin@test.com", "role": "owner",
                                   "unit_id": str(unit("201").id), "active": "on"})
    assert resp.status_code == 400
    assert user("admin@test.com").role == "admin"


# --- billing -----------------------------------------------------------------

def test_generate_dues_is_idempotent(app):
    assert generate_dues(2026, 3) == (2, 0)  # unit 201 has no fee
    assert generate_dues(2026, 3) == (0, 2)
    assert unit("101").balance == 40000


def test_generate_dues_clamps_due_day(app):
    generate_dues(2026, 2, due_day=31)
    charge = db.session.scalar(db.select(Charge).where(Charge.period == "2026-02"))
    assert charge.due_date == date(2026, 2, 28)


def test_payment_and_ledger(admin_client):
    u = unit("101")
    generate_dues(2026, 1)
    generate_dues(2026, 2)
    resp = admin_client.post("/accounting/payments/new", data={"unit_id": str(u.id), "amount": "500",
                                                            "paid_on": "2026-02-05", "method": "check",
                                                            "reference": "1001"})
    assert resp.status_code == 302
    assert unit("101").balance == 80000 - 50000
    rows = unit("101").ledger()
    assert [r["balance"] for r in rows] == [40000, 80000, 30000]


def test_special_charge(admin_client):
    u = unit("102")
    resp = admin_client.post("/accounting/charges/new", data={"unit_id": str(u.id), "kind": "special",
                                                           "description": "Roof", "amount": "1000",
                                                           "due_date": "2026-06-01"})
    assert resp.status_code == 302 and unit("102").balance == 100000


def test_invalid_payment_rejected(admin_client):
    resp = admin_client.post("/accounting/payments/new", data={"unit_id": str(unit("101").id),
                                                            "amount": "-3", "paid_on": "2026-01-01",
                                                            "method": "check"})
    assert resp.status_code == 400
    assert db.session.scalar(db.select(db.func.count(Payment.id))) == 0


def test_delete_payment(admin_client):
    p = Payment(unit=unit("101"), amount_cents=100, paid_on=date.today(), method="cash")
    db.session.add(p)
    db.session.commit()
    admin_client.post(f"/accounting/payments/{p.id}/delete")
    assert unit("101").balance == 0


# --- maintenance -------------------------------------------------------------

def test_resident_submits_request_for_own_unit(alice_client):
    resp = alice_client.post("/maintenance/new", data={"title": "Leak", "description": "Drip",
                                                       "category": "Plumbing", "priority": "high",
                                                       "location": "unit"})
    assert resp.status_code == 302
    req = db.session.scalar(db.select(MaintenanceRequest))
    assert req.unit.number == "101" and req.status == "open"


def test_request_visibility(app, client):
    bob = user("bob@test.com")
    req = MaintenanceRequest(unit=unit("102"), submitted_by=bob, title="X", description="Y")
    db.session.add(req)
    db.session.commit()
    login(client, "alice@test.com")
    assert client.get(f"/maintenance/{req.id}").status_code == 403
    assert b"X" not in client.get("/maintenance/?status=all").data.split(b"<tbody>")[1]


def test_status_change_permissions(app, client):
    alice = user("alice@test.com")
    req = MaintenanceRequest(unit=unit("101"), submitted_by=alice, title="X", description="Y")
    db.session.add(req)
    db.session.commit()
    login(client, "alice@test.com")
    assert client.post(f"/maintenance/{req.id}/status", data={"status": "resolved"}).status_code == 403
    assert client.post(f"/maintenance/{req.id}/status", data={"status": "closed"}).status_code == 302
    assert db.session.get(MaintenanceRequest, req.id).status == "closed"
    # A status change is logged as a comment.
    assert "Closed" in db.session.get(MaintenanceRequest, req.id).comments[0].body


def test_admin_updates_status(admin_client):
    req = MaintenanceRequest(unit=None, submitted_by=user("bob@test.com"), title="Lobby", description="Y")
    db.session.add(req)
    db.session.commit()
    admin_client.post(f"/maintenance/{req.id}/status", data={"status": "in_progress", "priority": "urgent"})
    req = db.session.get(MaintenanceRequest, req.id)
    assert (req.status, req.priority) == ("in_progress", "urgent")


# --- announcements -----------------------------------------------------------

def test_post_announcement(admin_client):
    admin_client.post("/announcements/new", data={"title": "Water shutoff", "body": "Tuesday 9-11"})
    assert b"Water shutoff" in admin_client.get("/announcements/").data


# --- amenities ---------------------------------------------------------------

def _book(client, day, start, end):
    from condo.models import Amenity
    gym = db.session.scalar(db.select(Amenity))
    return client.post(f"/amenities/{gym.id}", data={"date": day, "start": start, "end": end})


def test_booking_flow(alice_client, client):
    day = (date.today() + timedelta(days=2)).isoformat()
    assert _book(alice_client, day, "07:00", "08:00").status_code == 302
    # overlapping
    resp = _book(alice_client, day, "07:30", "08:30")
    assert resp.status_code == 400 and b"overlaps" in resp.data
    # back-to-back is fine
    assert _book(alice_client, day, "08:00", "09:00").status_code == 302
    assert db.session.scalar(db.select(db.func.count(Booking.id))) == 2


@pytest.mark.parametrize("start,end,msg", [("05:00", "06:00", b"is open"), ("21:30", "22:30", b"is open"),
                                           ("10:00", "13:00", b"at most"), ("10:00", "09:00", b"after start")])
def test_booking_rules(alice_client, start, end, msg):
    day = (date.today() + timedelta(days=2)).isoformat()
    resp = _book(alice_client, day, start, end)
    assert resp.status_code == 400 and msg in resp.data


def test_booking_in_past_rejected(alice_client):
    day = (date.today() - timedelta(days=1)).isoformat()
    assert b"future" in _book(alice_client, day, "10:00", "11:00").data


def test_cannot_cancel_others_booking(app, client):
    from condo.models import Amenity
    start = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).replace(hour=10)
    b = Booking(amenity=db.session.scalar(db.select(Amenity)), user=user("bob@test.com"),
                start=start, end=start + timedelta(hours=1))
    db.session.add(b)
    db.session.commit()
    login(client, "alice@test.com")
    assert client.post(f"/amenities/bookings/{b.id}/cancel").status_code == 403


def test_seed_command(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["seed"])
    assert result.exit_code == 0, result.output
    assert db.session.scalar(db.select(db.func.count(Unit.id))) == 16


def test_installable_as_an_app_and_branded_dmbt(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.mimetype == "application/manifest+json"
    data = m.get_json()
    assert data["display"] == "standalone" and any(i["sizes"] == "512x512" for i in data["icons"])
    page = client.get("/login").data.decode()
    assert 'rel="manifest"' in page and "apple-touch-icon" in page
    assert "Domora" in page and "by DMBT Group" in page and "support@dmbtgroup.com" in page


def test_french_demo_condo_loads_through_the_portal(app):
    from condo import demo
    from condo.models import Payment, Poll, PollVoter, RoutineTask, SpecialContribution
    db.session.remove()
    db.drop_all()
    db.create_all()
    assert demo.load_if_empty(app) and not demo.load_if_empty(app)
    assert db.session.scalar(db.select(db.func.count(Unit.id))) >= 8
    assert db.session.scalar(db.select(SpecialContribution)).billed_at is not None
    poll = db.session.scalar(db.select(Poll))
    assert poll.status == "open" and db.session.scalar(db.select(db.func.count(PollVoter.id)).where(
        PollVoter.option_id.is_not(None))) == 4
    assert db.session.scalar(db.select(db.func.count(RoutineTask.id))) == 3
    assert db.session.scalar(db.select(db.func.count(Payment.id))) > 20
    board = db.session.scalar(db.select(User).where(User.email == demo.BOARD_EMAIL))
    assert board.is_board and board.check_password(demo.DEMO_PASSWORD)
