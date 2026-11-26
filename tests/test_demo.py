import os
import sqlite3
import time

import pytest
from werkzeug.security import check_password_hash

from condo import create_app, demo, sandbox
from condo.extensions import db


@pytest.fixture
def demo_app(tmp_path):
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SECRET_KEY": "test",
                      "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'demo.db'}"})
    with app.app_context():
        db.create_all()
        demo.load_if_empty(app)
        db.session.remove()
    app.config.update(DEMO=True, DEMO_SANDBOXES=True)
    return app


def _sandbox_file(client):
    with client.session_transaction() as s:
        sid = s.get("demo_sandbox")
    return sid and os.path.join(os.path.dirname(client.application.config["SQLALCHEMY_DATABASE_URI"][10:]),
                                "demo-sandboxes", f"{sid}.db")


def _rows(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_demo_sign_in_by_picking_a_role(demo_app):
    c = demo_app.test_client()
    page = c.get("/login").data.decode()
    assert 'action="/demo/sign-in"' in page and 'name="password"' not in page
    assert '<option value="owner" selected>' in page
    for role, _email in demo.DEMO_ROLES:
        resp = c.post("/demo/sign-in", data={"role": role})
        assert resp.status_code == 302 and resp.headers["Location"] == "/"
        assert c.get("/").status_code == 200
        c.post("/logout")
    assert c.post("/demo/sign-in", data={"role": "nobody"}).headers["Location"] == "/login"


def test_role_sign_in_is_off_outside_the_demo(app, client):
    assert client.post("/demo/sign-in", data={"role": "president"}).status_code == 404
    assert 'name="password"' in client.get("/login").data.decode()


def test_each_visitor_gets_a_private_copy_reset_when_they_leave(demo_app):
    template = demo_app.config["SQLALCHEMY_DATABASE_URI"][10:]
    before = _rows(template, "select count(*) from announcement")[0][0]
    a, b = demo_app.test_client(), demo_app.test_client()
    a.post("/demo/sign-in", data={"role": "president"})
    b.post("/demo/sign-in", data={"role": "president"})
    a.post("/announcements/new", data={"title": "Only visitor A sees this", "body": "Private"})
    assert "Only visitor A sees this" in a.get("/announcements/").data.decode()
    assert "Only visitor A sees this" not in b.get("/announcements/").data.decode()
    assert _rows(template, "select count(*) from announcement")[0][0] == before  # demo data untouched
    path = _sandbox_file(a)
    assert os.path.exists(path)
    a.post("/logout")
    assert not os.path.exists(path)  # leaving deletes their copy
    a.post("/demo/sign-in", data={"role": "president"})
    assert "Only visitor A sees this" not in a.get("/announcements/").data.decode()


def test_an_hour_without_activity_resets_and_asks_for_a_role(demo_app):
    c = demo_app.test_client()
    c.post("/demo/sign-in", data={"role": "owner"})
    path = _sandbox_file(c)
    with c.session_transaction() as s:
        s["demo_seen"] = int(time.time()) - sandbox.IDLE_SECONDS - 5
    resp = c.get("/")
    assert resp.status_code == 302 and resp.headers["Location"] == "/login"
    assert not os.path.exists(path)
    assert c.get("/units/").status_code == 302  # signed out
    assert "demo_sandbox" not in c.get("/login").data.decode()


def test_passwords_cannot_change_in_the_demo(demo_app):
    c = demo_app.test_client()
    c.post("/demo/sign-in", data={"role": "president"})
    page = c.get("/account").data.decode()
    assert "demo-locked" in page and "demo-note" in page
    c.post("/account", data={"current_password": demo.DEMO_PASSWORD, "new_password": "something-new",
                             "confirm_password": "something-new", "phone": ""})
    path = _sandbox_file(c)
    owner_id = _rows(path, "select id from user where email = 'isabelle.cote@demo.example'")[0][0]
    assert "demo-locked" in c.get(f"/residents/{owner_id}/edit").data.decode()
    c.post(f"/residents/{owner_id}/reset-password")
    c.post(f"/residents/{owner_id}/password", data={"password": "another-one-1", "confirm": "another-one-1"})
    for email in (demo.BOARD_EMAIL, "isabelle.cote@demo.example"):
        pw_hash, must_change = _rows(path, f"select password_hash, must_change_password from user where email = '{email}'")[0]
        assert check_password_hash(pw_hash, demo.DEMO_PASSWORD) and not must_change
