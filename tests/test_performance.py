from datetime import date

from condo.extensions import db
from condo.models import Expense, Payment, Setting, Unit, recategorize_costs

from .conftest import login


def seed():
    unit = db.session.scalar(db.select(Unit).order_by(Unit.number))
    db.session.add_all([
        Expense(spent_on=date(2026, 3, 1), description="Building insurance", amount_cents=-865000, category="insurance"),
        Expense(spent_on=date(2026, 1, 15), description="Snow removal January", amount_cents=-240000, category="snow"),
        Expense(spent_on=date(2026, 12, 15), description="Snow removal December", amount_cents=-240000, category="snow"),
        Expense(spent_on=date(2025, 1, 15), description="Snow removal", amount_cents=-400000, category="snow"),
        Payment(unit=unit, paid_on=date(2026, 2, 1), amount_cents=1500000, method="cheque"),
        Payment(unit=unit, paid_on=date(2026, 5, 3), amount_cents=12000, method="purchase credit",
                notes="Purchase — Garden soil and lawn seed"),
    ])
    db.session.commit()


def test_board_sees_costs_by_category_against_last_year(admin_client):
    seed()
    page = admin_client.get("/accounting/performance?year=2026").data.decode()
    assert "Performance" in page and "Where the money went" in page
    assert "$15,120" in page  # income: payments, purchase credit included
    assert "$13,570" in page  # costs: 8,650 + 2 × 2,400 + the owner’s 120 purchase
    assert "Snow removal" in page and "Landscaping" in page and "$4,800" in page and "$4,000" in page
    assert "▲" in page  # snow went up against 2025


def test_category_drill_down_lists_its_costs(admin_client):
    seed()
    page = admin_client.get("/accounting/performance?year=2026&category=snow").data.decode()
    detail = page.split('id="detail"')[1]
    assert "Snow removal January" in detail and "Snow removal December" in detail and "Building insurance" not in detail
    page = admin_client.get("/accounting/performance?year=2026&category=landscaping").data.decode()
    assert "Garden soil" in page.split('id="detail"')[1]


def test_rolling_twelve_months_and_empty_year(admin_client):
    seed()
    assert admin_client.get("/accounting/performance?span=12m").status_code == 200
    assert "No costs recorded for 2019" in admin_client.get("/accounting/performance?year=2019").data.decode()


def test_owners_and_tenants_cannot_see_it(client):
    login(client, "alice@test.com")
    assert client.get("/accounting/performance").status_code == 403
    assert "Performance" not in client.get("/accounting/").data.decode()
    client.post("/logout")
    login(client, "bob@test.com")
    assert client.get("/accounting/performance").status_code == 403


def test_tab_shows_for_the_board(admin_client):
    assert "/accounting/performance" in admin_client.get("/accounting/").data.decode()


def test_existing_costs_move_to_the_new_categories_once(app):
    db.session.add_all([Expense(spent_on=date(2026, 1, 5), description="Déneigement", amount_cents=-1000, category="maintenance"),
                        Expense(spent_on=date(2026, 1, 6), description="Window washing", amount_cents=-1000, category="other"),
                        Expense(spent_on=date(2026, 1, 7), description="Snow shovels", amount_cents=-1000, category="repairs")])
    db.session.commit()
    recategorize_costs()
    db.session.commit()
    cats = [e.category for e in db.session.scalars(db.select(Expense).order_by(Expense.spent_on))]
    assert cats == ["snow", "cleaning", "repairs"]  # a category picked on purpose stays
    assert db.session.get(Setting, "cost_categories_v2") is not None
