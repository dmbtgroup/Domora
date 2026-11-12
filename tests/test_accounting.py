from datetime import date

from condo.extensions import db
from condo.models import Expense, Payment, Unit, guess_cost_category
from condo.views.billing import resolve_period


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def seed():
    u1, u2 = unit("101"), unit("102")
    db.session.add_all([
        Payment(unit=u1, amount_cents=40000, paid_on=date(2026, 10, 5), method="bank transfer", notes="Fee payment"),
        Payment(unit=u2, amount_cents=35000, paid_on=date(2026, 11, 2), method="bank transfer"),
        Payment(unit=u1, amount_cents=10000, paid_on=date(2025, 12, 30), method="cash"),
        Expense(spent_on=date(2026, 11, 5), description="Hydra Electric invoice — parking heaters", amount_cents=-195458,
                origin="sheet"),
        Expense(spent_on=date(2026, 10, 20), description="Snow removal contract", category="maintenance", amount_cents=-60000),
    ])
    db.session.commit()


def test_resolve_period():
    today = date(2026, 11, 6)
    y = resolve_period({}, today)
    assert (y["view"], y["start"], y["end"], y["label"]) == ("year", date(2026, 1, 1), date(2026, 12, 31), "2026")
    m = resolve_period({"view": "month", "year": "2026", "month": "1"}, today)
    assert (m["start"], m["end"], m["label"]) == (date(2026, 1, 1), date(2026, 1, 31), "January 2026")
    assert m["prev"] == {"view": "month", "year": 2025, "month": 12}
    assert resolve_period({"view": "month", "year": "x"}, today)["label"] == "November 2026"


def test_year_view_has_separate_panels_and_monthly_table(admin_client):
    seed()
    page = admin_client.get("/accounting/?view=year&year=2026").data.decode()
    assert "<h1>Accounting</h1>" in page and "<title>Accounting · 2026" in page
    payments_panel = page.split("<h2>Payments")[1].split("</section>")[0]
    costs_panel = page.split("<h2>Costs")[1].split("</section>")[0]
    assert "$750.00" in payments_panel and "$100.00" not in payments_panel  # 2025 payment excluded
    assert "Hydra Electric" in costs_panel and "Snow removal" in costs_panel and "$2,554.58" in costs_panel
    assert "Repairs &amp; projects" in costs_panel and "Maintenance" in costs_panel
    assert "2026 by month" in page and "November" in page
    assert "−" in page  # net label


def test_month_view(admin_client):
    seed()
    page = admin_client.get("/accounting/?view=month&year=2026&month=10").data.decode()
    assert "October 2026" in page and "2026 by month" not in page
    payments_panel = page.split("<h2>Payments")[1].split("</section>")[0]
    assert "$400.00" in payments_panel and "$350.00" not in payments_panel
    assert "Hydra" not in page.split("<h2>Costs")[1].split("</section>")[0]


def test_add_and_remove_manual_cost(admin_client):
    resp = admin_client.post("/accounting/costs/new", data={"kind": "cost", "category": "insurance",
                                                            "description": "Building insurance premium",
                                                            "amount": "7,154.00", "spent_on": "2026-09-01"})
    assert resp.status_code == 302 and "view=month" in resp.headers["Location"]
    cost = db.session.scalar(db.select(Expense))
    assert cost.amount_cents == -715400 and cost.cost_cents == 715400 and cost.category_label == "Insurance"
    admin_client.post("/accounting/costs/new", data={"kind": "refund", "category": "repairs",
                                                     "description": "Contractor refund", "amount": "100",
                                                     "spent_on": "2026-09-15"})
    refund = db.session.scalar(db.select(Expense).where(Expense.description == "Contractor refund"))
    assert refund.cost_cents == -10000
    admin_client.post(f"/accounting/costs/{cost.id}/delete")
    assert db.session.get(Expense, cost.id) is None


def test_sheet_costs_can_be_deleted_with_a_warning(admin_client):
    seed()
    sheet_cost = db.session.scalar(db.select(Expense).where(Expense.origin == "sheet"))
    page = admin_client.post(f"/accounting/costs/{sheet_cost.id}/delete", follow_redirects=True).data.decode()
    assert db.session.get(Expense, sheet_cost.id) is None
    assert "the next sync won&#39;t add it again" in page or "the next sync won't add it again" in page


def test_cost_validation(admin_client):
    resp = admin_client.post("/accounting/costs/new", data={"kind": "cost", "category": "nope", "description": "",
                                                            "amount": "-5", "spent_on": "bad"})
    assert resp.status_code == 400 and db.session.scalar(db.select(db.func.count(Expense.id))) == 0


def test_old_billing_links_redirect(admin_client):
    resp = admin_client.get("/billing/?view=month&year=2026&month=3")
    assert resp.status_code == 301 and resp.headers["Location"] == "/accounting/?view=month&year=2026&month=3"
    assert admin_client.get("/billing/unit/1").headers["Location"] == "/accounting/unit/1"


def test_nav_says_accounting(admin_client):
    page = admin_client.get("/").data.decode()
    assert '<span class="label">Accounting</span>' in page and "Billing" not in page


def test_owner_sees_only_their_unit(alice_client):
    seed()
    page = alice_client.get("/accounting/").data.decode()
    assert "Accounting <span class=\"muted\">· Unit 101" in page
    assert 'name="unit"' not in page and "Hydra" not in page  # no unit picker, no building costs
    assert "$400.00" in page and "$350.00" not in page  # unit 102's payment hidden
    # Asking for another unit still shows their own.
    other = alice_client.get(f"/accounting/?unit={unit('102').id}").data.decode()
    assert "· Unit 101" in other and "· Unit 102" not in other
    assert alice_client.get("/accounting/costs/new").status_code == 403
    assert alice_client.get("/accounting/payments/new").status_code == 403


def test_tenant_goes_to_statement(client):
    from .conftest import login
    login(client, "bob@test.com")
    resp = client.get("/accounting/")
    assert resp.status_code == 302 and "/accounting/unit/" in resp.headers["Location"]


def test_manager_unit_filter(admin_client):
    seed()
    u = unit("101")
    page = admin_client.get(f"/accounting/?view=year&year=2026&unit={u.id}").data.decode()
    assert "· Unit 101" in page and "<h2>Charges" in page and "Hydra" not in page
    assert f'value="{u.id}" selected' in page
    assert f"unit={u.id}" in page.split('aria-label="Previous"')[0].split("period-nav")[1]  # arrows keep the unit
    allpage = admin_client.get("/accounting/?view=year&year=2026").data.decode()
    assert "All units" in allpage and "Hydra" in allpage


def test_board_member_manages_accounting(app, client):
    from condo.models import Owner, User
    from .conftest import login
    o = Owner(unit=unit("101"), name="Alice Chen", role="owner", email="alice@test.com")
    o.positions = ["treasurer"]
    db.session.add(o)
    db.session.commit()
    seed()
    login(client, "alice@test.com")
    page = client.get("/accounting/").data.decode()
    assert "All units" in page and "Hydra" in page and "Add cost" in page and "Add charge" in page
    assert client.get("/accounting/costs/new").status_code == 200


def test_sync_banner_removed(admin_client):
    from condo.models import Setting
    Setting.put("sheet", {"url": "https://docs.google.com/spreadsheets/d/" + "x" * 30, "auto": True})
    db.session.commit()
    assert b"come from the Google Sheet" not in admin_client.get("/accounting/").data


def test_guess_cost_category():
    assert guess_cost_category("Electricity_Hydro-Quebec") == "utilities"
    assert guess_cost_category("Snow Removal") == "maintenance"
    assert guess_cost_category("Insurance_Unique Cie Ass.") == "insurance"
    assert guess_cost_category("Bank charges") == "administration"
    assert guess_cost_category("parking heaters, labour & materials") == "repairs"
    assert guess_cost_category("Mystery") == "other"


def test_purchase_credit_is_also_a_cost_and_opening_not_billed(admin_client):
    from condo.models import Charge
    u = unit("102")
    db.session.add_all([
        Charge(unit=u, kind="opening", period="2026-09", due_date=date(2026, 9, 23), amount_cents=-72216,
               description="Opening balance"),
        Charge(unit=u, kind="dues", period="2026-10", due_date=date(2026, 10, 1), amount_cents=32700,
               description="Monthly fee"),
        Payment(unit=u, amount_cents=11431, paid_on=date(2026, 10, 9), method="purchase credit",
                notes="Purchase (reimbursement) — Leaf blower for condo yard"),
        Payment(unit=u, amount_cents=21126, paid_on=date(2026, 10, 10), method="bank transfer"),
    ])
    db.session.commit()
    page = admin_client.get("/accounting/?view=month&year=2026&month=10").data.decode()
    costs_panel = page.split("<h2>Costs")[1].split("</section>")[0]
    assert "Leaf blower for condo yard" in costs_panel
    assert "$114.31" in costs_panel and "Maintenance $114.31" in costs_panel
    # Net = cash actually received (211.26): payments 325.57 − owner-paid cost 114.31.
    assert "$211.26</span>Net" in page
    units = page.split("<h2>Units</h2>")[1]
    row = units.split(">102<")[1].split("</tr>")[0]
    assert "$327.00" in row  # billed excludes the opening balance


def test_payment_form_defaults_and_note(admin_client):
    page = admin_client.get("/accounting/payments/new").data.decode()
    assert 'value="bank transfer" selected' in page and "Interac e-Transfer" in page
    assert 'class="money-input"' in page and "data-balance=" in page
    resp = admin_client.post("/accounting/payments/new", data={"unit_id": str(unit("101").id), "amount": "341.03",
                                                               "paid_on": "2026-11-03", "method": "bank transfer",
                                                               "notes": "November condo fee"})
    assert resp.status_code == 302
    p = db.session.scalar(db.select(Payment).where(Payment.notes == "November condo fee"))
    assert p.amount_cents == 34103 and p.label == "November condo fee"


def test_panels_are_short_and_dates_open_the_full_details(admin_client):
    from condo import sheet_sync
    from condo.models import Reimbursement, User
    u = unit("101")
    long_note = ("Fee payment — JULIO QUIROZ — memo: 167 Terry Fox Novembre 2026; Interac reference 8812; "
                 "sent from the joint account")
    sheet_payment = Payment(unit=u, amount_cents=29500, paid_on=date(2026, 11, 6), method="bank transfer",
                            origin="sheet", notes=long_note[:255],
                            source_data={"tab": "Transactions", "row": 42, "date": "2026-11-06", "type": "Fee payment",
                                         "unit": "101", "amount_cents": 29500, "owner": "JULIO QUIROZ",
                                         "approved": None, "source": "Interac e-Transfer from JULIO QUIROZ",
                                         "notes": "memo: 167 Terry Fox Novembre 2026"})
    admin = db.session.scalar(db.select(User).where(User.email == "admin@test.com"))
    manual = Payment(unit=u, amount_cents=10000, paid_on=date(2026, 11, 3), method="check", reference="1043",
                     notes="November fee", recorded_by_id=admin.id)
    cost = Expense(spent_on=date(2026, 11, 2), description="Snow removal — first visit of the season, driveway and walk",
                   amount_cents=-45000, category="maintenance", source="Déneigement Laval", approved_by="Board vote",
                   recorded_by_id=admin.id)
    db.session.add_all([sheet_payment, manual, cost])
    db.session.commit()

    page = admin_client.get("/accounting/?view=month&year=2026&month=11").data.decode()
    payments_panel = page.split("<h2>Payments")[1].split("</section>")[0]
    assert "JULIO" not in payments_panel and "Fee payment" in payments_panel
    assert f"/accounting/payments/{sheet_payment.id}?back=" in payments_panel
    assert "/accounting/unit/" not in payments_panel  # the unit is plain text now
    costs_panel = page.split("<h2>Costs")[1].split("</section>")[0]
    assert f"/accounting/costs/{cost.id}?back=" in costs_panel and "Board vote" not in costs_panel

    detail = admin_client.get(f"/accounting/payments/{sheet_payment.id}").data.decode()
    assert "JULIO QUIROZ" in detail and "row 42" in detail and "Interac e-Transfer from JULIO QUIROZ" in detail
    assert "Google Sheet" in detail
    detail = admin_client.get(f"/accounting/payments/{manual.id}").data.decode()
    assert "Entered in the app" in detail and "By Admin on" in detail and "1043" in detail
    detail = admin_client.get(f"/accounting/costs/{cost.id}").data.decode()
    assert "Déneigement Laval" in detail and "Board vote" in detail and "By Admin on" in detail


def test_payment_details_follow_unit_access(app, client):
    from .conftest import login
    p1 = Payment(unit=unit("101"), amount_cents=100, paid_on=date(2026, 1, 1), method="cash")
    p2 = Payment(unit=unit("102"), amount_cents=100, paid_on=date(2026, 1, 1), method="cash")
    c = Expense(spent_on=date(2026, 1, 1), description="Bulbs", amount_cents=-500)
    db.session.add_all([p1, p2, c])
    db.session.commit()
    login(client, "alice@test.com")  # owner of 101
    assert client.get(f"/accounting/payments/{p1.id}").status_code == 200
    assert client.get(f"/accounting/payments/{p2.id}").status_code == 403
    assert client.get(f"/accounting/costs/{c.id}").status_code == 403



def test_every_row_has_delete_and_costs_are_short(admin_client):
    from condo.models import Reimbursement, User
    seed()
    u = unit("101")
    admin = db.session.scalar(db.select(User).where(User.email == "admin@test.com"))
    credit = Payment(unit=u, amount_cents=1034, paid_on=date(2026, 11, 4), method="purchase credit",
                     notes="Purchase (reimbursement) — Compost bags for the building, Canadian Tire")
    db.session.add(credit)
    db.session.add(Expense(spent_on=date(2026, 11, 5), category="repairs", origin="sheet", amount_cents=-195458,
                           description="Hydra Electric invoice 41126274 — parking heaters November"))
    db.session.commit()
    page = admin_client.get("/accounting/?view=month&year=2026&month=11").data.decode()
    payments_panel = page.split("<h2>Payments")[1].split("</section>")[0]
    costs_panel = page.split("<h2>Costs")[1].split("</section>")[0]
    assert payments_panel.count("/delete") == 2 and costs_panel.count("/delete") == 3
    assert "Hydra Electric invoice 41126274" in costs_panel and "parking heaters November" not in costs_panel
    assert ">sheet<" not in costs_panel and "Repairs &amp; projects</span></td>" not in costs_panel

    # A reimbursement recorded as a credit goes back to "approved" if the credit is deleted.
    r = Reimbursement(unit=u, requested_by_id=admin.id, description="Compost bags", amount_cents=1034,
                      settle_method="credit", status="settled", payment_id=credit.id, settled_by="Admin",
                      spent_on=date(2026, 11, 4))
    db.session.add(r)
    db.session.commit()
    resp = admin_client.post(f"/accounting/payments/{credit.id}/delete?back=/accounting/payments/{credit.id}")
    assert resp.headers["Location"].endswith("/accounting/")
    assert db.session.get(Payment, credit.id) is None and r.status == "approved" and r.payment_id is None


def test_only_bookkeepers_see_delete(alice_client):
    seed()
    page = alice_client.get("/accounting/").data.decode()
    assert "/delete" not in page
