"""Demo sandboxes: each demo visitor works on a private copy of the demo condo.

Picking a role copies the demo database into a file of its own; every request from that visitor then
reads and writes that copy. Signing out deletes it, and so does an hour without activity, after which the
visitor is asked to pick a role again. Nobody else ever sees what a visitor changed. Only used when the
demo runs on a SQLite file (DEMO set, no DATABASE_URL).
"""
import os
import sqlite3
import threading
import time
import uuid

from flask import current_app, flash, g, redirect, request, session, url_for
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from .extensions import db
from .i18n import gettext as _

IDLE_SECONDS = 3600
_KEY, _SEEN = "demo_sandbox", "demo_seen"
_engines = {}
_lock = threading.Lock()


def _template():
    """Path of the demo database every copy starts from, or None when sandboxes don't apply."""
    if not (current_app.config.get("DEMO") and current_app.config.get("DEMO_SANDBOXES")):
        return None
    url = db.engine.url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return url.database


def _folder():
    folder = os.path.join(os.path.dirname(os.path.abspath(_template())), "demo-sandboxes")
    os.makedirs(folder, exist_ok=True)
    return folder


def _path(sandbox_id):
    return os.path.join(_folder(), f"{sandbox_id}.db")


def _copy_template(path):
    src, dst = sqlite3.connect(_template()), sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def _engine(sandbox_id):
    with _lock:
        if sandbox_id not in _engines:
            _engines[sandbox_id] = create_engine(f"sqlite:///{_path(sandbox_id)}", poolclass=NullPool)
        return _engines[sandbox_id]


def _drop(sandbox_id):
    with _lock:
        engine = _engines.pop(sandbox_id, None)
    if engine is not None:
        engine.dispose()
    try:
        os.remove(_path(sandbox_id))
    except OSError:
        pass


def _sweep():
    """Delete copies nobody has used for over an hour (visitors who just closed the tab)."""
    cutoff = time.time() - IDLE_SECONDS - 60
    for name in os.listdir(_folder()):
        if name.endswith(".db") and os.path.getmtime(os.path.join(_folder(), name)) < cutoff:
            _drop(name[:-3])


def start():
    """A visitor picked a role: give them a fresh copy and use it for the rest of this request."""
    if not _template():
        return
    _sweep()
    old = session.get(_KEY)
    if old:
        _drop(old)
    sandbox_id = uuid.uuid4().hex
    _copy_template(_path(sandbox_id))
    session[_KEY], session[_SEEN] = sandbox_id, int(time.time())
    db.session.remove()
    g.sandbox_engine = _engine(sandbox_id)


def end():
    """The visitor signed out: their changes go with their copy."""
    sandbox_id = session.pop(_KEY, None)
    session.pop(_SEEN, None)
    g.pop("sandbox_engine", None)
    if sandbox_id:
        db.session.remove()
        _drop(sandbox_id)


def _before_request():
    g.pop("sandbox_engine", None)
    if request.endpoint == "static" or not _template():
        return None
    sandbox_id = session.get(_KEY)
    if not sandbox_id:
        if "_user_id" in session or request.cookies.get("remember_token"):
            session.clear()  # signed in without a copy of their own (e.g. from before): start over
            session["_remember"] = "clear"
        return None
    valid = len(sandbox_id) == 32 and all(c in "0123456789abcdef" for c in sandbox_id)
    if not valid or time.time() - session.get(_SEEN, 0) > IDLE_SECONDS:
        if valid:
            _drop(sandbox_id)
        session.clear()
        session["_remember"] = "clear"
        flash(_("Your demo session ended after an hour without activity, and your changes were reset. "
                "Choose a role to start again."), "info")
        return redirect(url_for("auth.login"))
    path = _path(sandbox_id)
    if not os.path.exists(path):  # served by another server, or cleaned up: start again from the demo data
        _copy_template(path)
    else:
        os.utime(path)
    session[_SEEN] = int(time.time())
    db.session.remove()
    g.sandbox_engine = _engine(sandbox_id)
    return None


def _teardown(_exc=None):
    if g.pop("sandbox_engine", None) is not None:
        db.session.remove()


def init_app(app):
    """Register before anything that reads the signed-in user."""
    app.before_request(_before_request)
    app.teardown_request(_teardown)
