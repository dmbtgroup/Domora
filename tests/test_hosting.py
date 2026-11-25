from condo import _engine_options, create_app


def test_postgres_is_encrypted_and_pooler_safe():
    opts = _engine_options("postgresql+psycopg://u:p@db.example.ca:6543/postgres", serverless=True)
    assert opts["connect_args"] == {"prepare_threshold": None, "sslmode": "require"}
    assert opts["poolclass"].__name__ == "NullPool"
    kept = _engine_options("postgresql+psycopg://u:p@db.example.ca/postgres?sslmode=verify-full", serverless=False)
    assert "sslmode" not in kept["connect_args"] and kept["pool_pre_ping"]


def test_sqlite_is_untouched():
    assert _engine_options("sqlite:////tmp/x.db", serverless=True) == {"pool_pre_ping": True}


def test_only_database_url_is_read(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://u:p@old.example/db")
    from condo import _database_url
    assert _database_url(str(tmp_path)).startswith("sqlite:///")


def test_read_only_window_blocks_saving_and_the_cron(monkeypatch):
    monkeypatch.setenv("READ_ONLY", "1")
    monkeypatch.setenv("CRON_SECRET", "s")
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "WTF_CSRF_ENABLED": False})
    from condo.extensions import db
    with app.app_context():
        db.create_all()
    c = app.test_client()
    assert c.get("/login").status_code == 200
    assert "Maintenance in progress" in c.get("/login").data.decode()
    assert c.post("/login", data={"email": "a@b.c", "password": "x"}).status_code == 503
    assert c.get("/sheet/cron", headers={"Authorization": "Bearer s"}).status_code == 503
