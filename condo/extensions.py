from flask import g, has_app_context
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_sqlalchemy.session import Session as _Session
from flask_wtf import CSRFProtect


class Session(_Session):
    """Uses the current demo visitor's private database when there is one (see sandbox.py)."""

    def get_bind(self, mapper=None, clause=None, bind=None, **kwargs):
        engine = g.get("sandbox_engine") if has_app_context() else None
        if engine is not None and bind is None:
            return engine
        return super().get_bind(mapper=mapper, clause=clause, bind=bind, **kwargs)


db = SQLAlchemy(session_options={"class_": Session})
csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "info"
