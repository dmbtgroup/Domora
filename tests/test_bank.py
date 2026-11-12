from datetime import date, timedelta

from condo.extensions import db
from condo.models import BankBalance, Expense, Payment, Unit, bank_summary


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def test_bank_balance_follows_payments_and_costs_after_the_opening(admin_client):
    today = date.today()
    opening_day = today - timedelta(days=10)
    resp = admin_client.post("/accounting/bank", data={"as_of": opening_day.isoformat(), "amount": "25,050.39",
                                                       "note": "RBC"})
    assert resp.status_code == 302
    u = unit("101")
    db.session.add_all([
        Payment(unit=u, amount_cents=66101, paid_on=opening_day, method="other"),  # same day: already in the bank
        Payment(unit=u, amount_cents=29500, paid_on=today - timedelta(days=3), method="bank transfer"),
        Payment(unit=u, amount_cents=12465, paid_on=today - timedelta(days=2), method="purchase credit"),  # no cash
        Expense(spent_on=today - timedelta(days=1), amount_cents=-195458, description="Parking heaters"),
    ])
    db.session.commit()
    s = bank_summary()
    assert s["since"] == 29500 - 195458 and s["today"] == 2505039 + 29500 - 195458
    page = admin_client.get("/accounting/").data.decode()
    assert "Bank account" in page and "$23,390.81" in page and "Bank balance today" in page

    # A later check against the bank shows the gap between what the bank says and what the app expected.
    admin_client.post("/accounting/bank", data={"as_of": today.isoformat(), "amount": "23,300.00"})
    rows = bank_summary()["rows"]
    assert rows[-1]["expected"] == 2339081 and rows[-1]["difference"] == 2330000 - 2339081
    assert bank_summary()["today"] == 2330000
    page = admin_client.get("/accounting/bank").data.decode()
    assert "Opening balance" in page and "-$90.81" in page


def test_bank_validation(admin_client):
    resp = admin_client.post("/accounting/bank", data={"as_of": (date.today() + timedelta(days=1)).isoformat(),
                                                       "amount": "abc"})
    assert resp.status_code == 400 and db.session.scalar(db.select(BankBalance)) is None
    assert "Record the opening balance" in admin_client.get("/accounting/").data.decode()


def test_owners_without_accounting_access_dont_see_the_bank(alice_client):
    assert alice_client.get("/accounting/bank").status_code == 403
    assert "Bank account" not in alice_client.get("/accounting/").data.decode()


def test_delete_a_bank_balance(admin_client):
    admin_client.post("/accounting/bank", data={"as_of": date.today().isoformat(), "amount": "100"})
    rec = db.session.scalar(db.select(BankBalance))
    admin_client.post(f"/accounting/bank/{rec.id}/delete")
    assert db.session.scalar(db.select(BankBalance)) is None
