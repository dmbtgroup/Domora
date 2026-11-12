import io
from datetime import date, datetime

import pytest
from openpyxl import Workbook

from condo import sheet_sync
from condo.extensions import db
from condo.models import Charge, Expense, Payment, Setting, Unit

HEADER = ["Date", "Unit", "Owner", "Type", "Amount", "Approved by Mostafa", "Source", "Notes"]


def make_sheet(extra_rows=(), balances=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    rows = [
        [datetime(2026, 9, 23), "#101", "Alice Chen", "Opening balance", 100.0, "n/a", "Update", "Fee balance"],
        [datetime(2026, 9, 23), "#102", "Bob Okafor", "Opening balance", -20.0, "n/a", "Update", ""],
        [datetime(2026, 10, 1), "#101", "Alice Chen", "Monthly fee", 400.0, "n/a", "Simulator", "October"],
        [datetime(2026, 10, 1), "#102", "Bob Okafor", "Monthly fee", 350.0, "n/a", "Simulator", "October"],
        [datetime(2026, 10, 5), "#101", "Alice Chen", "Fee payment", 400.0, "n/a", "Interac to condo account", None],
        [datetime(2026, 10, 9), "#102", "Bob", "Purchase (reimbursement)", 30.0, "Implicit", "Email", "Leaf blower"],
        [datetime(2026, 10, 12), "#101", "Alice Chen", "Special contribution", 50.0, "n/a", "Interac", None],
        [datetime(2026, 11, 5), None, None, "Purchase", -1954.58, "Yes", "RBC", "Parking heaters"],
        *extra_rows,
    ]
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    bal = wb.create_sheet("Balances")
    bal.append(["Unit", "Owner", "Opening balance", "Fees charged", "Payments received", "Purchase credits",
                "Current balance (owes)"])
    for unit, owner, cur in balances or [("#101", "Alice Chen", 50.0), ("#102", "Bob Okafor", 300.0)]:
        bal.append([unit, owner, 0, 0, 0, 0, cur])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_export_url():
    url = "https://docs.google.com/spreadsheets/d/1ZJ8LTTXgBlY2OprOdyLo8LMyejYC0eG8fQCFc35h8kY/edit?usp=drivesdk"
    assert sheet_sync.export_url(url).endswith("/d/1ZJ8LTTXgBlY2OprOdyLo8LMyejYC0eG8fQCFc35h8kY/export?format=xlsx")
    with pytest.raises(sheet_sync.SyncError):
        sheet_sync.export_url("https://example.com/sheet")


def test_parse_classifies_and_cross_checks():
    plan = sheet_sync.parse(make_sheet())
    assert plan["counts"] == {"opening": 2, "dues": 2, "payment": 2, "credit": 1, "expense": 1, "owners": None}
    assert plan["warnings"] == []
    u101, u102 = plan["units"]
    assert (u101["balance_cents"], u101["sheet_balance_cents"], u101["fee_cents"]) == (5000, 5000, 40000)
    assert u102["owner"] == "Bob Okafor"  # Balances tab wins over the transaction row's "Bob"


def test_parse_reports_problems():
    plan = sheet_sync.parse(make_sheet(
        extra_rows=[[datetime(2026, 11, 1), "#101", "A", "Mystery", 5, "", "", ""],
                    [datetime(2026, 11, 1), None, None, "Fee payment", 5, "", "", ""]],
        balances=[("#101", "Alice Chen", 99.0), ("#102", "Bob Okafor", 300.0)]))
    text = " ".join(plan["warnings"])
    assert 'unknown type "Mystery"' in text and "needs a unit" in text and "Balances tab says $99.00" in text


def test_apply_rebuilds_sheet_entries_and_keeps_manual_ones(app):
    manual = Payment(unit=db.session.scalar(db.select(Unit).where(Unit.number == "101")),
                     amount_cents=1000, paid_on=date(2026, 10, 20), method="cash")
    db.session.add(manual)
    db.session.commit()

    summary = sheet_sync.apply(sheet_sync.parse(make_sheet()))
    u101 = db.session.scalar(db.select(Unit).where(Unit.number == "101"))
    assert u101.owner_name == "Alice Chen" and u101.monthly_fee_cents == 40000
    assert u101.balance == 5000 - 1000  # sheet balance minus the manual cash payment
    assert summary["mismatches"] == ["Unit 101: the app shows $40.00 but the sheet's Balances tab says $50.00."]
    credit = db.session.scalar(db.select(Payment).where(Payment.method == "purchase credit"))
    assert credit.amount_cents == 3000 and "Leaf blower" in credit.notes
    expense = db.session.scalar(db.select(Expense))
    assert expense.amount_cents == -195458 and expense.description == "Parking heaters"
    # The full sheet row is kept so the entry's page can show where it came from.
    assert credit.source_data["tab"] == "Transactions" and credit.source_data["row"] >= 2
    assert credit.source_data["unit"] and credit.source_data["amount_cents"] == 3000
    assert expense.source_data["type"] and expense.source_data["row"] >= 2

    # Re-sync: same result, nothing duplicated, manual payment still there.
    sheet_sync.apply(sheet_sync.parse(make_sheet()))
    assert db.session.scalar(db.select(db.func.count(Charge.id)).where(Charge.origin == "sheet")) == 4
    assert db.session.scalar(db.select(db.func.count(Expense.id))) == 1
    assert db.session.get(Payment, manual.id) is not None


def test_apply_skips_clash_with_manual_dues(app):
    from condo.views.billing import generate_dues
    generate_dues(2026, 10)  # app-generated dues for October
    summary = sheet_sync.apply(sheet_sync.parse(make_sheet()))
    assert len(summary["skipped"]) == 2 and "already has a dues entry for 2026-10" in summary["skipped"][0]


def test_flow_preview_confirm_auto(admin_client, monkeypatch):
    monkeypatch.setattr(sheet_sync, "fetch", lambda url: make_sheet())
    admin_client.post("/sheet/", data={"url": "https://docs.google.com/spreadsheets/d/" + "x" * 30 + "/edit"})
    # Auto-sync can't be switched on before a confirmed sync.
    admin_client.post("/sheet/auto", data={"auto": "on"})
    assert not sheet_sync.config().get("auto")

    resp = admin_client.post("/sheet/preview")
    assert resp.status_code == 302
    page = admin_client.get(resp.headers["Location"])
    assert b"Confirm and load" in page.data and b"Parking heaters" in page.data
    assert db.session.scalar(db.select(db.func.count(Expense.id))) == 0  # preview writes nothing

    admin_client.post(resp.headers["Location"] + "/apply")
    assert db.session.scalar(db.select(db.func.count(Expense.id))) == 1
    admin_client.post("/sheet/auto", data={"auto": "on"})
    assert sheet_sync.config()["auto"] is True
    assert b"Generate" not in admin_client.get("/accounting/").data  # dues come from the sheet


def test_preview_from_uploaded_file(admin_client):
    resp = admin_client.post("/sheet/preview", data={"workbook": (io.BytesIO(make_sheet()), "log.xlsx")},
                             content_type="multipart/form-data")
    assert b"log.xlsx" in admin_client.get(resp.headers["Location"]).data


def test_sheet_pages_admin_only(alice_client):
    assert alice_client.get("/sheet/").status_code == 403


def test_cron_requires_secret_and_syncs_only_changes(app, client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    data = {"sheet": make_sheet()}
    monkeypatch.setattr(sheet_sync, "fetch", lambda url: data["sheet"])
    assert client.get("/sheet/cron").status_code == 403
    assert client.get("/sheet/cron", headers={"Authorization": "Bearer wrong"}).status_code == 403
    auth = {"Authorization": "Bearer s3cret"}
    assert client.get("/sheet/cron", headers=auth).json == {"status": "auto-sync is off"}

    Setting.put("sheet", {"url": "https://docs.google.com/spreadsheets/d/" + "x" * 30, "auto": True,
                          "confirmed": True})
    db.session.commit()
    assert client.get("/sheet/cron", headers=auth).json == {"status": "synced"}
    assert client.get("/sheet/cron", headers=auth).json == {"status": "unchanged"}
    data["sheet"] = make_sheet(extra_rows=[[datetime(2026, 11, 6), "#102", "Bob", "Fee payment", 300.0,
                                            "n/a", "Interac", None]],
                               balances=[("#101", "Alice Chen", 50.0), ("#102", "Bob Okafor", 0.0)])
    assert client.get("/sheet/cron", headers=auth).json == {"status": "synced"}
    assert db.session.scalar(db.select(Unit).where(Unit.number == "102")).balance == 0


def test_cron_records_fetch_errors(app, client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")

    def boom(url):
        raise sheet_sync.SyncError("Google refused access to the sheet.")
    monkeypatch.setattr(sheet_sync, "fetch", boom)
    Setting.put("sheet", {"url": "https://docs.google.com/spreadsheets/d/" + "x" * 30, "auto": True})
    db.session.commit()
    resp = client.get("/sheet/cron", headers={"Authorization": "Bearer s3cret"})
    assert resp.json["status"] == "error"
    assert "refused" in Setting.get("sheet_last_error")["message"]


def test_sheet_entries_can_be_deleted_but_come_back_at_the_next_sync(admin_client):
    sheet_sync.apply(sheet_sync.parse(make_sheet()))
    pay = db.session.scalar(db.select(Payment).where(Payment.origin == "sheet"))
    pay_id, amount = pay.id, pay.amount_cents
    assert b"from sheet" in admin_client.get(f"/accounting/unit/{pay.unit_id}").data
    admin_client.post(f"/accounting/payments/{pay_id}/delete")
    assert db.session.get(Payment, pay_id) is None
    sheet_sync.apply(sheet_sync.parse(make_sheet()))
    assert db.session.scalar(db.select(Payment).where(Payment.origin == "sheet", Payment.amount_cents == amount))


def add_owners_tab(data, rows):
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    ws = wb.create_sheet("Owners")
    ws.append(["Unit", "Name", "Email", "Phone", "Mailing address", "Role", "Primary", "Notes"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_owners_tab_syncs_multiple_owners_per_unit(app):
    from condo.models import Owner
    manual = Owner(unit=db.session.scalar(db.select(Unit).where(Unit.number == "101")), name="Property Manager",
                   role="contact")
    db.session.add(manual)
    db.session.commit()
    data = add_owners_tab(make_sheet(), [
        ["#101", "Alice Chen", "ALICE@example.com", "514-555-0101", "", "Owner", "yes", ""],
        ["#101", "Wei Chen", "wei@example.com", "514-555-0102", "12 Elm St\nLaval QC", "Co-owner", "", "Lives abroad"],
        ["#102", "Bob Okafor", "not-an-email", "", "", "", "x", ""],
        ["#103", "Nobody", "", "", "", "tenant", "", ""],
    ])
    plan = sheet_sync.parse(data)
    assert plan["counts"]["owners"] == 4
    assert any("isn't an email address" in w for w in plan["warnings"])
    sheet_sync.apply(plan)
    u101 = db.session.scalar(db.select(Unit).where(Unit.number == "101"))
    names = [(o.name, o.role, o.is_primary, o.email) for o in u101.owners]
    assert ("Alice Chen", "owner", True, "alice@example.com") in names
    assert ("Wei Chen", "co-owner", False, "wei@example.com") in names
    assert ("Property Manager", "contact", False, None) in names  # manual contact kept
    assert u101.owner_display == "Alice Chen / Wei Chen"  # other contacts aren't listed as owners
    assert db.session.scalar(db.select(Unit).where(Unit.number == "103")) is not None  # unit created

    # Re-sync replaces only the sheet's owners.
    sheet_sync.apply(sheet_sync.parse(add_owners_tab(make_sheet(), [["#101", "Alice Chen", "", "", "", "", "", ""]])))
    assert sorted(o.name for o in db.session.get(Unit, u101.id).owners) == ["Alice Chen", "Property Manager"]


def test_no_owners_tab_leaves_contacts_alone(app):
    from condo.models import Owner
    sheet_sync.apply(sheet_sync.parse(add_owners_tab(make_sheet(), [["#101", "Alice Chen", "", "", "", "", "", ""]])))
    sheet_sync.apply(sheet_sync.parse(make_sheet()))  # tab removed
    assert db.session.scalar(db.select(db.func.count(Owner.id))) == 1


def test_owners_tab_same_as_address():
    data = add_owners_tab(make_sheet(), [
        ["#101", "Alice Chen", "", "", "12 Elm St, Laval", "Owner", "yes", ""],
        ["#101", "Wei Chen", "", "", "Same as Alice Chen", "Co-owner", "", ""],
        ["#102", "Bob Okafor", "", "", "same address as wei chen", "", "", ""],  # chain, other unit
        ["#102", "Eve", "", "", "Same as Nobody", "", "", ""],
    ])
    plan = sheet_sync.parse(data)
    addr = {c["name"]: c["address"] for c in plan["owners"]}
    assert addr["Wei Chen"] == addr["Bob Okafor"] == "12 Elm St, Laval"
    assert addr["Eve"] == "" and any('"Same as Nobody"' in w for w in plan["warnings"])


def test_resolve_clashes_with_imported_openings(admin_client):
    from condo.models import ImportBatch
    u101 = db.session.scalar(db.select(Unit).where(Unit.number == "101"))
    # The Excel import loaded an opening balance first...
    db.session.add(Charge(unit=u101, kind="opening", period="2026-09", due_date=date(2026, 9, 30), amount_cents=224766,
                          description="Opening balance (owed) from spreadsheet"))
    db.session.commit()
    resp = admin_client.post("/sheet/preview", data={"workbook": (io.BytesIO(make_sheet()), "log.xlsx")},
                             content_type="multipart/form-data")
    admin_client.post(resp.headers["Location"] + "/apply")
    assert db.session.get(Unit, u101.id).balance != 5000  # ...so the sheet's opening was skipped
    page = admin_client.get("/sheet/").data.decode()
    assert "1 old entry blocks the sheet" in page and "$2,247.66" in page
    admin_client.post("/sheet/resolve-clashes")
    assert db.session.get(Unit, u101.id).balance == 5000  # now matches the Balances tab
    assert "block the sheet" not in admin_client.get("/sheet/").data.decode()


def test_imported_opening_is_replaced_even_when_the_sheet_opens_at_zero(admin_client):
    """Unit 169's case: the sheet opens the unit at $0, the old Excel import had $2,247.66 owed."""
    u101 = db.session.scalar(db.select(Unit).where(Unit.number == "101"))
    u102 = db.session.scalar(db.select(Unit).where(Unit.number == "102"))
    for unit, cents in ((u101, 88421), (u102, 1083)):
        db.session.add(Charge(unit=unit, kind="opening", period="2026-09", due_date=date(2026, 9, 30), amount_cents=cents,
                              description="Opening balance (owed) from spreadsheet, end of September 2026"))
    db.session.commit()
    rows = [[datetime(2026, 9, 23), "#101", "Alice Chen", "Opening balance", 0, "n/a", "Update", ""]]
    data = make_sheet(rows)
    # Drop the sheet's own non-zero opening for 101, so 101 opens at $0 like 169.
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Transactions"]
    ws.delete_rows(2)
    buf = io.BytesIO()
    wb.save(buf)
    resp = admin_client.post("/sheet/preview", data={"workbook": (io.BytesIO(buf.getvalue()), "log.xlsx")},
                             content_type="multipart/form-data")
    admin_client.post(resp.headers["Location"] + "/apply")
    assert "2 old entries from before the Google Sheet" in admin_client.get("/accounting/").data.decode()
    page = admin_client.get("/sheet/").data.decode()
    assert "2 old entries block the sheet" in page and "$884.21" in page and "$10.83" in page
    admin_client.post("/sheet/resolve-clashes")
    assert not db.session.scalars(db.select(Charge).where(Charge.kind == "opening", Charge.origin.is_(None))).all()
    assert db.session.get(Unit, u101.id).balance == 40000 - 40000 - 5000  # fee 400 − paid 400 − contribution 50
    assert "old entries from before" not in admin_client.get("/accounting/").data.decode()


def test_board_can_delete_a_charge_from_the_unit_page(admin_client):
    u101 = db.session.scalar(db.select(Unit).where(Unit.number == "101"))
    c = Charge(unit=u101, kind="other", due_date=date(2026, 10, 2), amount_cents=1234, description="Old entry")
    db.session.add(c)
    db.session.commit()
    page = admin_client.get(f"/accounting/?unit={u101.id}&year=2026").data.decode()
    assert f"/accounting/charges/{c.id}/delete" in page
    admin_client.post(f"/accounting/charges/{c.id}/delete")
    assert db.session.get(Charge, c.id) is None
