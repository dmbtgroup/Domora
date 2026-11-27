import hashlib
import hmac
import json
import re
import time

import pyotp
import pytest
from itsdangerous import URLSafeTimedSerializer

from accounts import create_app
from accounts.models import Account, Membership, db

CONDOS = {"condo5077": {"name": "Condo 5077", "url": "https://condo5077.example", "key": "k-5077"},
          "jardins": {"name": "Les Jardins du Parc", "url": "https://jardins.example", "key": "k-jardins"}}


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "test", "CONDOS": CONDOS,
                      "MAIL_ENABLED": True})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()


def add(email, password="correct horse 1", memberships=(), admin=False):
    acc = Account(email=email, name=email.split("@")[0].title(), is_platform_admin=admin)
    acc.set_password(password)
    for condo, role, board in memberships:
        acc.memberships.append(Membership(condo=condo, role=role, board=board))
    db.session.add(acc)
    db.session.commit()
    return acc


def csrf(c):
    with c.session_transaction() as s:
        return s.setdefault("csrf", "tok")


def post(c, path, **data):
    return c.post(path, data=dict(data, csrf=csrf(c)))


def sign_in(c, email, password="correct horse 1"):
    c.get("/sign-in")
    return post(c, "/sign-in", email=email, password=password)


def read_pass(location, key):
    token = location.split("pass=", 1)[1]
    return URLSafeTimedSerializer(key, salt="belcentra-handoff").loads(token, max_age=60)


def test_public_pages(app):
    c = app.test_client()
    assert c.get("/").status_code == 200 and 'href="/sign-in"' in c.get("/").data.decode()
    assert c.get("/pricing").status_code == 200 and c.get("/site.css").status_code == 200


def test_one_condo_goes_straight_in_with_a_one_time_pass(app):
    add("owner@x.example", memberships=[("condo5077", "Owner", False)])
    c = app.test_client()
    assert sign_in(c, "owner@x.example").headers["Location"].endswith("/condos")
    resp = c.get("/condos")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/go/condo5077")
    resp = c.get("/go/condo5077")
    assert resp.headers["Location"].startswith("https://condo5077.example/auth/handoff?pass=")
    data = read_pass(resp.headers["Location"], "k-5077")
    assert data["email"] == "owner@x.example" and data["aud"] == "condo5077" and data["nonce"] and not data["admin"]
    with pytest.raises(Exception):  # another condo's key can't read it
        read_pass(resp.headers["Location"], "k-jardins")


def test_several_condos_get_a_picker_and_only_their_own(app):
    add("two@x.example", memberships=[("condo5077", "Owner", False), ("jardins", "Tenant", False)])
    c = app.test_client()
    sign_in(c, "two@x.example")
    page = c.get("/condos").data.decode()
    assert "Condo 5077 · Owner" in page and "Les Jardins du Parc · Tenant" in page
    add("one@x.example", memberships=[("condo5077", "Owner", False)])
    c2 = app.test_client()
    sign_in(c2, "one@x.example")
    assert c2.get("/go/jardins").status_code == 403  # not theirs


def test_platform_admin_opens_every_condo(app):
    add("admin@belcentra.example", admin=True)
    acc = db.session.scalar(db.select(Account))
    acc.totp_secret, acc.totp_enabled = pyotp.random_base32(), True
    db.session.commit()
    c = app.test_client()
    sign_in(c, "admin@belcentra.example")
    post(c, "/sign-in/code", code=pyotp.TOTP(acc.totp_secret).now())
    page = c.get("/condos").data.decode()
    assert "Condo 5077" in page and "Les Jardins du Parc" in page
    assert read_pass(c.get("/go/jardins").headers["Location"], "k-jardins")["admin"] is True


def test_wrong_password_and_lockout(app):
    add("p@x.example")
    c = app.test_client()
    for _ in range(5):
        assert sign_in(c, "p@x.example", "nope").status_code == 400
    assert sign_in(c, "p@x.example").status_code == 429  # locked even with the right password
    assert sign_in(c, "nobody@x.example", "nope").status_code == 400  # same answer for unknown emails


def test_board_members_must_set_up_two_step_then_use_it(app):
    add("pres@x.example", memberships=[("condo5077", "President", True)])
    c = app.test_client()
    assert sign_in(c, "pres@x.example").headers["Location"].endswith("/two-step/setup")
    assert c.get("/go/condo5077").headers["Location"].endswith("/two-step/setup")  # can't skip it
    c.get("/two-step/setup")
    with c.session_transaction() as s:
        secret = s["totp_setup"]
    page = post(c, "/two-step/setup", code=pyotp.TOTP(secret).now()).data.decode()
    codes = re.findall(r"<code>([0-9a-f]{4}-[0-9a-f]{4})</code>", page)
    assert len(codes) == 10
    post(c, "/sign-out")
    assert sign_in(c, "pres@x.example").headers["Location"].endswith("/sign-in/code")
    assert c.get("/condos").status_code == 302  # not signed in until the code
    assert post(c, "/sign-in/code", code="000000").status_code == 200
    assert post(c, "/sign-in/code", code=codes[0]).headers["Location"].endswith("/condos")  # a backup code
    post(c, "/sign-out")
    sign_in(c, "pres@x.example")
    assert post(c, "/sign-in/code", code=codes[0]).status_code == 200  # each backup code works once


def test_forms_need_the_csrf_token(app):
    add("p@x.example")
    c = app.test_client()
    c.get("/sign-in")
    assert c.post("/sign-in", data={"email": "p@x.example", "password": "correct horse 1"}).status_code == 400


def test_forced_password_change_and_reset_by_email(app):
    acc = add("p@x.example", memberships=[("condo5077", "Owner", False)])
    acc.must_change_password = True
    db.session.commit()
    c = app.test_client()
    sign_in(c, "p@x.example")
    assert c.get("/condos").headers["Location"].endswith("/account/password")
    post(c, "/account/password", current="correct horse 1", new="a brand new one", confirm="a brand new one")
    assert c.get("/condos").status_code == 302 and "/go/condo5077" in c.get("/condos").headers["Location"]
    post(c, "/sign-out")
    post(c, "/password/forgot", email="p@x.example")
    mail = app.extensions["sent_mail"][-1]
    link = re.search(r"(/password/reset/\S+)", mail["text"]).group(1)
    post(c, link, new="reset password 1", confirm="reset password 1")
    assert sign_in(c, "p@x.example", "reset password 1").headers["Location"].endswith("/condos")
    assert post(c, link, new="again again 1", confirm="again again 1").headers["Location"].endswith("/password/forgot")  # used


def _api(c, path, slug, key, body):
    raw, ts = json.dumps(body).encode(), str(int(time.time()))
    sig = hmac.new(key.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return c.post(path, data=raw, content_type="application/json",
                  headers={"X-Belcentra-Condo": slug, "X-Belcentra-Time": ts, "X-Belcentra-Signature": sig})


def test_condos_sync_members_with_signed_calls(app):
    c = app.test_client()
    from werkzeug.security import generate_password_hash
    h = generate_password_hash("from the condo 1")
    member = {"email": "New@x.example", "name": "New Owner", "role": "Owner", "board": False, "password_hash": h}
    assert _api(c, "/api/members/sync", "condo5077", "k-5077", {"members": [member]}).status_code == 200
    assert sign_in(c, "new@x.example", "from the condo 1").status_code == 302  # kept the condo's password
    assert _api(c, "/api/members/sync", "condo5077", "wrong-key", {"members": []}).status_code == 403
    assert _api(c, "/api/members/sync", "jardins", "k-5077", {"members": []}).status_code == 403
    _api(c, "/api/members/sync", "jardins", "k-jardins", {"members": [{"email": "new@x.example", "role": "Tenant"}]})
    # condo5077's board can't reset a password that also opens another condo
    resp = _api(c, "/api/password", "condo5077", "k-5077", {"email": "new@x.example", "password_hash": h, "must_change": True})
    assert resp.status_code == 409
    _api(c, "/api/members/sync", "jardins", "k-jardins", {"members": []})  # jardins no longer lists them
    m = db.session.scalar(db.select(Membership).where(Membership.condo == "jardins"))
    assert m.active is False
    assert _api(c, "/api/password", "condo5077", "k-5077", {"email": "new@x.example", "password_hash": h, "must_change": True}).status_code == 200


def test_signing_out_of_a_condo_ends_the_belcentra_session(app):
    add("o@x.example", memberships=[("condo5077", "Owner", False)])
    c = app.test_client()
    sign_in(c, "o@x.example")
    assert c.get("/signed-out").headers["Location"].endswith("/sign-in")
    assert c.get("/condos").headers["Location"].startswith("/sign-in")


def test_sync_asks_for_missing_passwords_once(app):
    c = app.test_client()
    r = _api(c, "/api/members/sync", "condo5077", "k-5077", {"members": [{"email": "a@x.example", "role": "Owner"}]})
    assert r.get_json()["need_passwords"] == ["a@x.example"]
    from werkzeug.security import generate_password_hash
    m = {"email": "a@x.example", "role": "Owner", "password_hash": generate_password_hash("their own pw 1")}
    assert _api(c, "/api/members/sync", "condo5077", "k-5077", {"members": [m]}).get_json()["need_passwords"] == []


def test_platform_admin_from_settings(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAIL", "Admin@Belcentra.example")
    monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", "first password 1")
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "t", "CONDOS": CONDOS})
    with app.app_context():
        acc = db.session.scalar(db.select(Account))
        assert acc.email == "admin@belcentra.example" and acc.is_platform_admin and acc.must_change_password


def test_email_off_sends_nothing(app):
    app.config["MAIL_ENABLED"] = False
    app.config["RESEND_API_KEY"] = "re_test"
    add("p@x.example")
    c = app.test_client()
    page = c.get("/password/forgot").get_data(as_text=True)
    assert "board member" in page and 'name="email"' not in page
    post(c, "/password/forgot", email="p@x.example")
    from accounts.mail import send
    with app.test_request_context():
        assert send("p@x.example", "s", "t") is False
    assert not app.extensions.get("sent_mail") or all(m["subject"] == "s" for m in app.extensions["sent_mail"])
