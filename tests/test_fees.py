from datetime import date, timedelta

from condo.extensions import db
from condo.models import (Charge, FeeSchedule, Notification, Payment, SpecialContribution, Unit, User,
                          split_by_weight)
from condo.views.fees import unit_summary

from .conftest import login
from .test_staff import staff


PERIOD_2026 = {"basis": "annual", "starts_on": "2026-01-01", "ends_on": "2026-12-31"}


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def set_shares(*shares):
    for number, share in zip(("101", "102", "201"), shares):
        unit(number).ownership_share = share
    db.session.commit()


def test_split_by_weight_adds_up_exactly():
    parts = split_by_weight(100001, {1: 1, 2: 1, 3: 1})
    assert sum(parts.values()) == 100001 and sorted(parts.values()) == [33333, 33334, 33334]
    assert split_by_weight(500, {1: 0, 2: 0}) == {1: 0, 2: 0}


def test_workbook_rate_for_unit_161(app):
    # The 2026 budget of $34,691 split by unit 161's share gives $4,027.37 a year in the workbook.
    s = FeeSchedule(year=2026, amount_cents=3469100)
    u = Unit(id=901, number="161", ownership_share=402737 / 3469100)
    other = Unit(id=902, number="999", ownership_share=1 - 402737 / 3469100)
    rows = {r["unit"].number: r for r in s.unit_fees([u, other])}
    assert rows["161"]["annual"] == 402737
    assert rows["161"]["annual"] + rows["999"]["annual"] == 3469100


def test_save_schedule_with_override_and_apply(admin_client):
    set_shares(0.5, 0.3, 0.2)
    resp = admin_client.post("/accounting/fees/2026", data={**PERIOD_2026, "amount": "12,000", "override_%d" % unit("201").id: "1,200"})
    assert resp.status_code == 302
    s = db.session.scalar(db.select(FeeSchedule).where(FeeSchedule.year == 2026))
    rows = {r["unit"].number: r for r in s.unit_fees([unit("101"), unit("102"), unit("201")])}
    assert rows["201"]["annual"] == 120000 and rows["201"]["override"]
    assert rows["101"]["annual"] == 675000 and rows["102"]["annual"] == 405000  # 10,800 split 5:3
    assert rows["101"]["monthly"] == 56250
    page = admin_client.get("/accounting/fees?year=2026").data.decode()
    assert "$12,000.00" in page and "Special contributions" in page

    admin_client.post("/accounting/fees/2026/apply")
    assert unit("101").monthly_fee_cents == 56250 and unit("201").monthly_fee_cents == 10000
    assert s.applied_at is not None


def test_fixed_amounts_cannot_exceed_budget(admin_client):
    set_shares(0.5, 0.3, 0.2)
    admin_client.post("/accounting/fees/2026", data={**PERIOD_2026, "amount": "1,000", "override_%d" % unit("101").id: "2,000"})
    assert db.session.scalar(db.select(FeeSchedule)) is None


def test_special_contribution_lifecycle(admin_client):
    set_shares(0.5, 0.3, 0.2)
    deadline = date.today() + timedelta(days=30)
    resp = admin_client.post("/accounting/specials/new", data={"title": "Window replacement", "method": "share",
                                                               "total": "10,000", "deadline": deadline.isoformat()})
    sc = db.session.scalar(db.select(SpecialContribution))
    assert resp.headers["Location"].endswith(f"/accounting/specials/{sc.id}")
    assert {sh.unit.number: sh.amount_cents for sh in sc.shares} == {"101": 500000, "102": 300000, "201": 200000}
    assert sc.billed_at is None and not db.session.scalars(db.select(Charge)).all()

    admin_client.post(f"/accounting/specials/{sc.id}/bill")
    charges = db.session.scalars(db.select(Charge).where(Charge.kind == "special")).all()
    assert len(charges) == 3 and all(c.due_date == deadline for c in charges)
    alice = db.session.scalar(db.select(User).where(User.email == "alice@test.com"))
    note = db.session.scalar(db.select(Notification).where(Notification.user_id == alice.id))
    assert "$5,000.00" in note.title

    admin_client.post("/accounting/payments/new", data={"unit_id": unit("101").id, "method": "check", "amount": "2,000",
                                                        "paid_on": date.today().isoformat(), "special_id": sc.id})
    p = db.session.scalar(db.select(Payment))
    assert p.special_id == sc.id and "Window replacement" in p.notes
    rows = {r["unit"].number: r for r in sc.status_rows()}
    assert (rows["101"]["status"], rows["101"]["left"]) == ("partial", 300000)
    assert rows["102"]["status"] == "unpaid"
    assert {r["unit"].number: r["status"] for r in sc.status_rows(deadline + timedelta(days=1))}["102"] == "overdue"

    new = deadline + timedelta(days=14)
    admin_client.post(f"/accounting/specials/{sc.id}/deadline", data={"deadline": new.isoformat()})
    assert sc.deadline == new and all(c.due_date == new for c in charges)

    admin_client.post(f"/accounting/specials/{sc.id}/delete")
    assert db.session.get(SpecialContribution, sc.id) is not None  # billed: kept

    page = admin_client.get(f"/accounting/specials/{sc.id}").data.decode()
    assert "Window replacement" in page and "Partly paid" in page


def test_equal_and_custom_splits(admin_client):
    deadline = (date.today() + timedelta(days=10)).isoformat()
    admin_client.post("/accounting/specials/new", data={"title": "Roof", "method": "equal", "total": "100", "deadline": deadline})
    admin_client.post("/accounting/specials/new", data={"title": "Balcony", "method": "custom", "deadline": deadline,
                                                        "amount_%d" % unit("102").id: "750"})
    roof, balcony = db.session.scalars(db.select(SpecialContribution).order_by(SpecialContribution.id)).all()
    assert sorted(sh.amount_cents for sh in roof.shares) == [3333, 3333, 3334]
    assert [(sh.unit.number, sh.amount_cents) for sh in balcony.shares] == [("102", 75000)]
    # share split without shares set is refused
    resp = admin_client.post("/accounting/specials/new", data={"title": "X", "method": "share", "total": "100",
                                                               "deadline": deadline})
    assert resp.status_code == 400


def test_owner_sees_own_fee_and_specials(app, admin_client):
    set_shares(0.5, 0.3, 0.2)
    y = date.today().year
    admin_client.post("/accounting/fees/%d" % y, data={"amount": "12,000", "basis": "annual",
                                                        "starts_on": f"{y}-01-01", "ends_on": f"{y}-12-31"})
    admin_client.post("/accounting/specials/new", data={"title": "Elevator", "method": "share", "total": "1,000",
                                                        "deadline": (date.today() + timedelta(days=5)).isoformat()})
    sc = db.session.scalar(db.select(SpecialContribution))
    admin_client.post(f"/accounting/specials/{sc.id}/bill")
    summary = unit_summary(unit("101"))
    assert summary["fee"]["annual"] == 600000 and summary["specials"][0][1]["amount"] == 50000
    admin_client.post("/logout")

    login(admin_client, "alice@test.com")
    page = admin_client.get("/accounting/").data.decode()
    assert "Elevator" in page and "$6,000.00" in page
    assert admin_client.post("/accounting/fees/2026", data={**PERIOD_2026, "amount": "1"}).status_code in (302, 403)
    assert db.session.scalar(db.select(FeeSchedule)).annual_total_cents == 1200000


def test_permissions(app, client):
    login(client, "bob@test.com")  # tenant
    assert client.get("/accounting/fees").status_code in (302, 403)
    client.post("/logout")
    staff(["accounting"])
    login(client, "staff@x.com")
    page = client.get("/accounting/fees")
    assert page.status_code == 200 and "+ Special contribution" not in page.data.decode()
    assert client.post("/accounting/fees/2026", data={**PERIOD_2026, "amount": "5"}).status_code in (302, 403)
    assert db.session.scalar(db.select(FeeSchedule)) is None
    assert client.get("/accounting/specials/new").status_code in (302, 403)


def test_monthly_basis_and_custom_period(admin_client):
    set_shares(0.5, 0.3, 0.2)
    admin_client.post("/accounting/fees/2026", data={"basis": "monthly", "amount": "3,000", "starts_on": "2026-04-01",
                                                     "ends_on": "2027-03-31", "override_%d" % unit("201").id: "500"})
    s = db.session.scalar(db.select(FeeSchedule))
    assert (s.basis, s.months, s.monthly_total_cents, s.annual_total_cents) == ("monthly", 12, 300000, 3600000)
    rows = {r["unit"].number: r for r in s.unit_fees([unit("101"), unit("102"), unit("201")])}
    assert rows["201"]["monthly"] == 50000 and rows["201"]["annual"] == 600000
    assert rows["101"]["monthly"] == 156250 and rows["102"]["monthly"] == 93750  # 2,500 split 5:3
    page = admin_client.get("/accounting/fees?year=2026").data.decode()
    assert "$36,000.00" in page and "$3,000.00" in page and "Jan 31, 2027" in page  # reminder date
    # a half-year period entered as an annual figure
    admin_client.post("/accounting/fees/2027", data={"basis": "annual", "amount": "6,000", "starts_on": "2027-04-01",
                                                     "ends_on": "2027-09-30"})
    half = db.session.scalar(db.select(FeeSchedule).where(FeeSchedule.year == 2027))
    assert (half.months, half.monthly_total_cents) == (6, 100000)


def test_period_validation(admin_client):
    admin_client.post("/accounting/fees/2026", data={**PERIOD_2026, "amount": "1,200"})
    resp = admin_client.post("/accounting/fees/2027", data={"basis": "annual", "amount": "1", "starts_on": "2026-06-01",
                                                            "ends_on": "2027-05-31"}, follow_redirects=True)
    assert "already fees for a period starting in 2026" in resp.data.decode() and db.session.scalar(db.select(db.func.count(FeeSchedule.id))) == 1
    resp = admin_client.post("/accounting/fees/2028", data={"basis": "annual", "amount": "1", "starts_on": "2028-06-01",
                                                            "ends_on": "2028-01-01"}, follow_redirects=True)
    assert "end date must be after" in resp.data.decode()


def test_board_reminded_two_months_before_end(app, admin_client):
    from condo.views.fees import check_renewals
    admin_client.post("/accounting/fees/2026", data={**PERIOD_2026, "amount": "12,000"})
    s = db.session.scalar(db.select(FeeSchedule))
    assert s.renewal_date == date(2026, 10, 31)
    with app.test_request_context():
        assert check_renewals(date(2026, 10, 30)) == 0
        assert check_renewals(date(2026, 10, 31)) == 1
        assert check_renewals(date(2026, 11, 15)) == 0  # only once
    admin = db.session.scalar(db.select(User).where(User.email == "admin@test.com"))
    note = db.session.scalar(db.select(Notification).where(Notification.user_id == admin.id, Notification.kind == "fees"))
    assert "Dec 31, 2026" in note.title and "/accounting/fees?year=2027" in note.url
    assert s.renewal_notified_at is not None


def test_no_reminder_when_next_period_is_set(app, admin_client):
    from condo.views.fees import check_renewals
    admin_client.post("/accounting/fees/2026", data={**PERIOD_2026, "amount": "12,000"})
    admin_client.post("/accounting/fees/2027", data={"basis": "annual", "amount": "12,500", "starts_on": "2027-01-01",
                                                     "ends_on": "2027-12-31"})
    with app.test_request_context():
        assert check_renewals(date(2026, 11, 15)) == 0


def test_cron_runs_renewal_check(app, client, monkeypatch):
    from condo import sheet_sync
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr(sheet_sync, "run_auto_sync", lambda: "not configured")
    calls = []
    monkeypatch.setattr("condo.views.fees.check_renewals", lambda: calls.append(1) or 0)
    assert client.get("/sheet/cron", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert calls == [1]


def test_unit_page_shows_this_years_contributions_billed_or_not(admin_client):
    """Two contributions this year, one billed and one not; the unit's sheet payment marked
    "Special contribution" isn't linked to either, and counts towards the earliest first."""
    set_shares(0.5, 0.3, 0.2)
    y = date.today().year
    soon, later = date.today() + timedelta(days=5), date.today() + timedelta(days=40)
    admin_client.post("/accounting/specials/new", data={"title": "Parking heaters", "method": "share", "total": "1,000",
                                                        "deadline": soon.isoformat()})
    admin_client.post("/accounting/specials/new", data={"title": "Roof repair", "method": "share", "total": "2,000",
                                                        "deadline": later.isoformat()})
    heaters, roof = db.session.scalars(db.select(SpecialContribution).order_by(SpecialContribution.id)).all()
    admin_client.post(f"/accounting/specials/{heaters.id}/bill")
    u = unit("101")  # 50%: owes 500 + 1,000
    db.session.add(Payment(unit=u, amount_cents=70000, paid_on=date.today(), method="other", origin="sheet",
                           notes="Special contribution"))
    db.session.commit()
    rows = {sc.title: r for sc, r in unit_summary(u, y)["specials"]}
    assert rows["Parking heaters"]["paid"] == 50000 and rows["Parking heaters"]["status"] == "paid"
    assert rows["Roof repair"]["amount"] == 100000 and rows["Roof repair"]["paid"] == 20000
    assert rows["Roof repair"]["left"] == 80000 and rows["Roof repair"]["status"] == "partial"
    page = admin_client.get(f"/accounting/?unit={u.id}&year={y}").data.decode()
    assert "Parking heaters" in page and "Roof repair" in page and "$1,000.00" in page and "$800.00" in page
    # A unit that paid nothing yet towards a contribution that isn't billed yet.
    rows = {sc.title: r for sc, r in unit_summary(unit("102"), y)["specials"]}
    assert rows["Roof repair"]["status"] == "draft" and rows["Parking heaters"]["status"] == "unpaid"
    assert "Not billed yet" in admin_client.get(f"/accounting/?unit={unit('102').id}&year={y}").data.decode()


def test_units_that_paid_before_the_portal(admin_client):
    set_shares(0.5, 0.3, 0.2)
    admin_client.post("/accounting/specials/new", data={"title": "Parking heaters", "method": "share", "total": "1,000",
                                                        "deadline": (date.today() + timedelta(days=5)).isoformat()})
    sc = db.session.scalar(db.select(SpecialContribution))
    balance_before = unit("101").balance
    page = admin_client.get(f"/accounting/specials/{sc.id}").data.decode()
    assert "Paid before the portal" in page, re.findall(r"<title>.*?</title>|flash[^<]*<", page)
    admin_client.post(f"/accounting/specials/{sc.id}/paid-before",
                      data={"paid_on": "2026-08-15", "unit_id": [str(unit("101").id), str(unit("102").id)]})
    rows = {r["unit"].number: r for r in sc.status_rows()}
    assert rows["101"]["paid"] == 50000 and rows["101"]["status"] == "paid"
    assert rows["102"]["status"] == "paid" and rows["201"]["left"] == 20000
    assert unit("101").balance == balance_before  # not a payment: the balance is untouched
    # A later sheet payment marked "Special contribution" isn't counted twice for a prepaid unit.
    db.session.add(Payment(unit=unit("101"), amount_cents=10000, paid_on=date.today(), method="other",
                           notes="Special contribution"))
    db.session.commit()
    assert {r["unit"].number: r for r in sc.status_rows()}["101"]["paid"] == 60000
    # Unticking a unit takes it off again.
    admin_client.post(f"/accounting/specials/{sc.id}/paid-before", data={"paid_on": "2026-08-15",
                                                                         "unit_id": [str(unit("102").id)]})
    rows = {r["unit"].number: r for r in sc.status_rows()}
    assert rows["101"]["paid"] == 10000 and rows["102"]["status"] == "paid"
