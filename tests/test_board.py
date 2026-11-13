import io

from openpyxl import load_workbook

from condo import sheet_sync
from condo.extensions import db
from condo.models import Owner, Unit, User

from .conftest import login
from .test_sheet_sync import add_owners_tab, make_sheet


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def person(name):
    return db.session.scalar(db.select(Owner).where(Owner.name == name))


def contact(unit_number, name, role="owner", email=None):
    o = Owner(unit=unit(unit_number), name=name, role=role, email=email or name.split()[0].lower() + "@x.com")
    db.session.add(o)
    db.session.commit()
    return o


def assign(client, o, key):
    return client.post("/directory/board/assign", data={"position": key, "owner_id": str(o.id)})


def test_owner_form_has_no_board_positions(admin_client):
    assert b"Board of directors" not in admin_client.get(f"/units/{unit('101').id}/owners/new").data


def test_assign_several_positions_in_directory(admin_client):
    raquel = contact("101", "Raquel Silva")
    assign(admin_client, raquel, "treasurer")
    assign(admin_client, raquel, "accounting")
    assert person("Raquel Silva").position_labels == ["Treasurer", "Accounting"]
    page = admin_client.get("/directory/board").data.decode()
    assert "Raquel Silva" in page and "Vacant" in page and "Choose a person" in page
    assert 'class="badge board">Treasurer' in admin_client.get(f"/units/{unit('101').id}").data.decode()


def test_single_holder_replaced_and_unassign(admin_client):
    ann, ben, cat = contact("101", "Ann First"), contact("102", "Ben Second"), contact("101", "Cat Third")
    assign(admin_client, ann, "president")
    resp = assign(admin_client, ben, "president")
    assert person("Ann First").positions == [] and person("Ben Second").positions == ["president"]
    assert b"President moved from Ann First to Ben Second." in admin_client.get(resp.headers["Location"]).data
    assign(admin_client, ann, "director")
    assign(admin_client, cat, "director")  # several directors allowed
    assert person("Ann First").positions == ["director"] and person("Cat Third").positions == ["director"]
    admin_client.post("/directory/board/unassign", data={"position": "director", "owner_id": str(cat.id)})
    assert person("Cat Third").positions == []


def test_eligibility(admin_client):
    tom = contact("101", "Tom Tenant", role="tenant")
    cpa = contact("101", "Outside Cpa", role="contact")
    assign(admin_client, tom, "president")
    assign(admin_client, cpa, "treasurer")
    assign(admin_client, cpa, "accounting")
    assert person("Tom Tenant").positions == [] and person("Outside Cpa").positions == ["accounting"]
    page = admin_client.get("/directory/board").data.decode()
    president_row = page.split("<strong>President</strong>")[1].split("</tr>")[0]
    assert "Tom Tenant" not in president_row and "Outside Cpa" not in president_row


def test_editing_owner_keeps_positions_and_role_change_drops_them(admin_client):
    ann = contact("101", "Ann First")
    assign(admin_client, ann, "secretary")
    admin_client.post(f"/owners/{ann.id}/edit", data={"name": "Ann First", "role": "co-owner"})
    assert person("Ann First").positions == ["secretary"]
    admin_client.post(f"/owners/{ann.id}/edit", data={"name": "Ann First", "role": "tenant"})
    assert person("Ann First").positions == []


def test_unit_page_has_no_accounts_card(admin_client):
    assert b"Website accounts" not in admin_client.get(f"/units/{unit('101').id}").data


def test_accounts_are_board_only_once_a_board_member_has_an_account(app, client):
    # Bootstrap: no board accounts yet, so the manager can still manage accounts.
    login(client, "admin@test.com")
    page = client.get("/directory/accounts").data
    assert b"No board member has an account yet" in page
    # Alice (an owner with an account) becomes treasurer.
    alice_c = contact("101", "Alice Chen", email="alice@test.com")
    assign(client, alice_c, "treasurer")
    client.post("/logout")

    # Now the manager, who isn't on the board, loses access to accounts.
    login(client, "admin@test.com")
    assert client.get("/directory/accounts").status_code == 403
    assert client.get("/residents/new").status_code == 403
    assert client.get("/directory/accounts/export.csv").status_code == 403
    assert b'href="/directory/accounts"' not in client.get("/directory/owners").data
    client.post("/logout")

    # Alice (board, not a manager) gets the Directory with Board and Accounts tabs.
    login(client, "alice@test.com")
    dash = client.get("/").data.decode()
    assert 'href="/directory/board"' in dash
    assert client.get("/directory/accounts").status_code == 200
    assert client.get("/directory/owners").status_code == 200  # board members manage the building
    resp = client.post("/residents/new", data={"name": "New Person", "email": "new@x.com", "role": "tenant",
                                               "unit_id": str(unit("102").id), "password": "password123"})
    assert resp.status_code == 302
    assert db.session.scalar(db.select(User).where(User.email == "new@x.com")) is not None
    # Board members can also assign positions.
    bob_c = contact("102", "Bob Owner")
    assert assign(client, bob_c, "secretary").status_code == 302
    assert person("Bob Owner").positions == ["secretary"]


def test_non_board_resident_has_no_access(alice_client):
    for path in ("/directory/board", "/directory/accounts", "/residents/new"):
        assert alice_client.get(path).status_code == 403, path
    assert alice_client.post("/directory/board/assign", data={"position": "president", "owner_id": "1"}).status_code == 403


def test_board_tab_and_dashboards(admin_client, client):
    ann, ben = contact("101", "Ann First"), contact("102", "Ben Second")
    assign(admin_client, ann, "president")
    assign(admin_client, ben, "treasurer")
    page = admin_client.get("/directory/board").data.decode()
    assert "mailto:?bcc=ann@x.com,ben@x.com" in page
    assert "https://mail.google.com/mail/?view=cm&amp;fs=1&amp;bcc=ann%40x.com%2Cben%40x.com" in page  # Gmail, new tab
    assert "Board role" in admin_client.get("/directory/owners/export.csv").data.decode()
    admin_client.post("/logout")
    login(client, "bob@test.com")  # tenant sees whom to contact
    dash = client.get("/").data.decode()
    assert "Board of directors" in dash and "President" in dash and "Ann First" in dash


def _with_board_column(values):
    data = add_owners_tab(make_sheet(), [
        ["#101", "Alice Chen", "", "", "", "Owner", "yes", ""],
        ["#102", "Bob Okafor", "", "", "", "Owner", "", ""],
    ])
    if values is None:
        return data
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Owners"]
    ws.cell(1, 9, "Board role")
    for i, v in enumerate(values, start=2):
        ws.cell(i, 9, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_sheet_board_column_sets_positions(app):
    plan = sheet_sync.parse(_with_board_column(["Président, Treasury", "Accountant / chef"]))
    assert any('unknown board role "chef"' in w for w in plan["warnings"])
    sheet_sync.apply(plan)
    assert person("Alice Chen").positions == ["president", "treasurer"]
    assert person("Bob Okafor").positions == ["accounting"]


def test_sheet_without_board_column_keeps_directory_assignments(app):
    sheet_sync.apply(sheet_sync.parse(_with_board_column(None)))
    alice = person("Alice Chen")
    alice.positions = ["president"]
    db.session.commit()
    sheet_sync.apply(sheet_sync.parse(_with_board_column(None)))
    assert person("Alice Chen").positions == ["president"]
