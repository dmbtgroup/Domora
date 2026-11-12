"""Minimal additive schema upgrades.

`db.create_all()` creates missing tables but never adds columns to tables that
already exist. Each entry below is a column added after the first release; it is
added with ALTER TABLE when absent. Only nullable columns (or ones with a DEFAULT) belong here, so the
upgrade is safe on tables that already hold rows.
"""
from sqlalchemy import inspect, text

from .extensions import db

ADDED_COLUMNS = [
    ("unit", "owner_name", "VARCHAR(200)"),
    ("unit", "ownership_share", "FLOAT"),
    ("charge", "origin", "VARCHAR(20)"),
    ("charge", "sheet_key", "VARCHAR(255)"),
    ("special_share", "prepaid_cents", "INTEGER"),
    ("special_share", "prepaid_on", "DATE"),
    ("payment", "sheet_key", "VARCHAR(255)"),
    ("expense", "sheet_key", "VARCHAR(255)"),
    ("payment", "origin", "VARCHAR(20)"),
    ("payment", "notes", "VARCHAR(255)"),
    ("maintenance_request", "owner_approval", "VARCHAR(20)"),
    ("maintenance_request", "owner_decision_by", "VARCHAR(200)"),
    ("maintenance_request", "owner_decision_at", "TIMESTAMP"),
    ("maintenance_request", "owner_decision_note", "TEXT"),
    ("owner", "board_roles", "VARCHAR(200)"),
    ("insurance_policy", "unit_id", "INTEGER"),
    ("insurance_policy", "holder_group", "VARCHAR(10)"),
    ("expense", "category", "VARCHAR(20)"),
    ("user", "must_change_password", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("user", "language", "VARCHAR(5)"),
    ("user", "job_title", "VARCHAR(120)"),
    ("payment", "special_id", "INTEGER"),
    ("payment", "source_data", "JSON"),
    ("expense", "source_data", "JSON"),
    ("expense", "recorded_by_id", "INTEGER"),
    ("fee_schedule", "basis", "VARCHAR(10) NOT NULL DEFAULT 'annual'"),
    ("fee_schedule", "starts_on", "DATE"),
    ("fee_schedule", "ends_on", "DATE"),
    ("fee_schedule", "renewal_notified_at", "TIMESTAMP"),
    ("maintenance_request", "kind", "VARCHAR(20) NOT NULL DEFAULT 'maintenance'"),
    ("maintenance_request", "budget_cents", "INTEGER"),
    ("maintenance_request", "target_date", "DATE"),
    ("user", "permissions", "VARCHAR(300)"),
    ("maintenance_request", "quote_note", "TEXT"),
    ("maintenance_request", "quote_vendor", "VARCHAR(200)"),
    ("maintenance_request", "quote_amount_cents", "INTEGER"),
    ("maintenance_request", "quote_file_id", "INTEGER"),
    ("maintenance_request", "quote_approved_cents", "INTEGER"),
    ("maintenance_request", "quote_decided_by", "VARCHAR(200)"),
    ("maintenance_request", "quote_decided_at", "TIMESTAMP"),
    ("maintenance_request", "quote_decision_note", "TEXT"),
]

# Data fixes that run after the columns exist. Each must be safe to repeat.
BACKFILLS = [
    # Policies used to belong to one person; now they cover the person's unit and group.
    """UPDATE insurance_policy SET
         unit_id = (SELECT owner.unit_id FROM owner WHERE owner.id = insurance_policy.owner_id),
         holder_group = CASE WHEN (SELECT owner.role FROM owner WHERE owner.id = insurance_policy.owner_id) = 'tenant'
                             THEN 'tenants' ELSE 'owners' END
       WHERE unit_id IS NULL OR holder_group IS NULL""",
]


def upgrade_schema():
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    preparer = db.engine.dialect.identifier_preparer
    with db.engine.begin() as conn:
        for table, column, ddl_type in ADDED_COLUMNS:
            if table not in tables:
                continue  # create_all() builds it with every column
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {preparer.quote(table)} ADD COLUMN {preparer.quote(column)} {ddl_type}"))
    tables = set(inspect(db.engine).get_table_names())
    with db.engine.begin() as conn:
        for sql in BACKFILLS:
            table = sql.split()[1]
            if table in tables:
                conn.execute(text(sql))
