import pytest

from condo import demo
from condo.extensions import db
from condo.models import User


@pytest.fixture
def demo_app(app):
    db.session.remove()
    db.drop_all()
    db.create_all()
    demo.load_if_empty(app)
    from flask import g
    g.pop("_login_user", None)  # the loader signs in through the shared test context
    app.config["DEMO"] = True
    return app


def test_demo_sign_in_by_picking_a_role(demo_app):
    c = demo_app.test_client()
    page = c.get("/login").data.decode()
    assert 'action="/demo/sign-in"' in page and 'name="password"' not in page
    for role, email in demo.DEMO_ROLES:
        resp = c.post("/demo/sign-in", data={"role": role})
        assert resp.status_code == 302 and resp.headers["Location"] == "/"
        assert c.get("/").status_code == 200
        c.post("/logout")
    assert c.post("/demo/sign-in", data={"role": "nobody"}).headers["Location"] == "/login"


def test_role_sign_in_is_off_outside_the_demo(app, client):
    assert client.post("/demo/sign-in", data={"role": "president"}).status_code == 404
    assert 'name="password"' in client.get("/login").data.decode()


def test_passwords_cannot_change_in_the_demo(demo_app):
    c = demo_app.test_client()
    c.post("/demo/sign-in", data={"role": "president"})
    page = c.get("/account").data.decode()
    assert "demo-locked" in page and "Passwords can" in page
    c.post("/account", data={"current_password": demo.DEMO_PASSWORD, "new_password": "something-new",
                             "confirm_password": "something-new", "phone": ""})
    board = db.session.scalar(db.select(User).where(User.email == demo.BOARD_EMAIL))
    assert board.check_password(demo.DEMO_PASSWORD)
    owner = db.session.scalar(db.select(User).where(User.email == "isabelle.cote@demo.example"))
    assert "demo-locked" in c.get(f"/residents/{owner.id}/edit").data.decode()
    c.post(f"/residents/{owner.id}/reset-password")
    c.post(f"/residents/{owner.id}/password", data={"password": "another-one-1", "confirm": "another-one-1"})
    db.session.expire_all()
    assert db.session.get(User, owner.id).check_password(demo.DEMO_PASSWORD) and not db.session.get(User, owner.id).must_change_password
