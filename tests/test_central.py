import pytest
from itsdangerous import URLSafeTimedSerializer

from condo import central
from condo.extensions import db
from condo.models import User

from .conftest import login

KEY = "shared-secret"


@pytest.fixture
def central_app(app, monkeypatch):
    app.config.update(CENTRAL_LOGIN=True, CONDO_SLUG="condo5077", BELCENTRA_KEY=KEY, ACCOUNTS_URL="https://acc.example")
    calls = []

    def fake_call(path, payload):
        calls.append((path, payload))
        if path == "/api/password" and payload["email"] == "multi@test.com":
            return 409, {}
        return 200, {"ok": True, "members": len(payload.get("members", []))}
    monkeypatch.setattr(central, "_call", fake_call)
    app.central_calls = calls
    return app


def make_pass(**over):
    data = {"email": "alice@test.com", "name": "Alice", "aud": "condo5077", "nonce": "n-1", "admin": False, "lang": "fr"}
    data.update(over)
    return URLSafeTimedSerializer(KEY, salt="belcentra-handoff").dumps(data)


def test_one_time_pass_signs_in_once(central_app, client):
    resp = client.get(f"/auth/handoff?pass={make_pass()}")
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    assert client.get("/units/").status_code in (200, 403)  # signed in (owners may not see every page)
    client.post("/logout")
    assert client.get(f"/auth/handoff?pass={make_pass()}").status_code == 403  # same pass again


def test_passes_for_another_condo_or_forged_are_refused(central_app, client):
    assert "acc.example/sign-in" in client.get(f"/auth/handoff?pass={make_pass(aud='other', nonce='n-2')}").headers["Location"]
    forged = URLSafeTimedSerializer("wrong-key", salt="belcentra-handoff").dumps({"email": "admin@test.com", "aud": "condo5077", "nonce": "n-3"})
    assert "acc.example/sign-in" in client.get(f"/auth/handoff?pass={forged}").headers["Location"]
    assert client.get("/units/").status_code == 302  # still signed out
    assert client.get(f"/auth/handoff?pass={make_pass(email='stranger@x.com', nonce='n-4')}").status_code == 403


def test_belcentra_admin_gets_an_admin_account_here(central_app, client):
    client.get(f"/auth/handoff?pass={make_pass(email='admin@belcentra.example', admin=True, nonce='n-5')}")
    user = db.session.scalar(db.select(User).where(User.email == "admin@belcentra.example"))
    assert user.role == "admin" and not user.check_password("")
    assert client.get("/units/").status_code == 200


def test_sign_in_and_out_go_through_belcentra(central_app, client):
    assert client.get("/login").headers["Location"] == "https://acc.example/sign-in"
    assert client.get("/login?local=1").status_code == 200  # emergency fallback
    login(client, "alice@test.com")
    assert client.post("/logout").headers["Location"] == "https://acc.example/signed-out"


def test_account_changes_are_sent_with_new_passwords_only(central_app, admin_client):
    calls = central_app.central_calls
    admin_client.post("/residents/new", data={"name": "Newcomer", "email": "new@test.com", "role": "owner",
                                              "unit_id": "1", "password": "password123"})
    path, payload = calls[-1]
    assert path == "/api/members/sync"
    by_email = {m["email"]: m for m in payload["members"]}
    assert by_email["new@test.com"]["password_hash"] and "password_hash" not in by_email["alice@test.com"]
    assert by_email["admin@test.com"]["board"] is True and by_email["alice@test.com"]["board"] is False


def test_board_reset_refused_for_people_in_other_condos(central_app, admin_client):
    u = User(email="multi@test.com", name="Multi", role="owner")
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    resp = admin_client.post(f"/residents/{u.id}/reset-password")
    assert resp.status_code == 302 and "Forgot password" in admin_client.get(resp.headers["Location"]).data.decode()


def test_off_by_default(app, client):
    assert client.get("/login").status_code == 200
    assert client.get(f"/auth/handoff?pass={make_pass()}").status_code == 404


def test_missing_passwords_are_sent_once_when_asked(app, monkeypatch):
    app.config.update(CONDO_SLUG="condo5077", BELCENTRA_KEY=KEY)
    sent = []

    def fake_call(path, payload):
        sent.append(payload)
        need = [] if any("password_hash" in m for m in payload["members"]) else ["alice@test.com"]
        return 200, {"members": len(payload["members"]), "need_passwords": need}
    monkeypatch.setattr(central, "_call", fake_call)
    assert central.push_members() == 3
    assert len(sent) == 2 and [m["email"] for m in sent[1]["members"] if "password_hash" in m] == ["alice@test.com"]
