from datetime import date, datetime, timedelta

import click

from .extensions import db
from .models import Amenity, Announcement, Booking, MaintenanceRequest, Payment, Unit, User


def bootstrap():
    """Create tables and, if no manager exists yet, a first manager account
    from the ADMIN_EMAIL / ADMIN_PASSWORD environment variables.

    Safe to run on every start-up: it never changes existing data.
    """
    import os

    from .migrate import upgrade_schema

    db.create_all()
    upgrade_schema()
    from .models import link_admin_accounts, recategorize_costs
    if link_admin_accounts():
        db.session.commit()
    recategorize_costs()
    db.session.commit()
    email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD") or ""
    if not email or len(password) < 8:
        return
    if db.session.scalar(db.select(User).where(User.role == "admin")):
        return
    if db.session.scalar(db.select(User).where(User.email == email)):
        return
    user = User(email=email, name=os.environ.get("ADMIN_NAME", "Administrator"), role="admin")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()


def register_cli(app):
    @app.cli.command("init-db")
    @click.option("--drop", is_flag=True, help="Drop all existing tables first.")
    def init_db(drop):
        """Create database tables."""
        from .migrate import upgrade_schema

        if drop:
            db.drop_all()
        db.create_all()
        upgrade_schema()
        click.echo("Database initialised.")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.password_option()
    def create_admin(email, name, password):
        """Create a manager (admin) account."""
        db.create_all()
        email = email.strip().lower()
        if db.session.scalar(db.select(User).where(User.email == email)):
            raise click.ClickException("A user with that email already exists.")
        user = User(email=email, name=name, role="admin")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Manager account created for {email}.")

    @app.cli.command("seed")
    def seed():
        """Reset the database and load demo data."""
        db.drop_all()
        db.create_all()
        seed_demo_data()
        click.echo("Demo data loaded. Log in as admin@example.com / admin1234 "
                   "or alice@example.com / resident123.")


def seed_demo_data():
    units = []
    for floor in range(1, 5):
        for n in range(1, 5):
            beds = 1 + (n % 3)
            units.append(Unit(number=f"{floor}0{n}", floor=floor, bedrooms=beds,
                              area_sqft=650 + beds * 200, parking_spot=f"P{floor}{n}",
                              monthly_fee_cents=30000 + beds * 7500))
    db.session.add_all(units)

    admin = User(email="admin@example.com", name="Morgan Lee", role="admin", phone="555-0100")
    admin.set_password("admin1234")
    people = [
        ("alice@example.com", "Alice Chen", "owner", 0),
        ("ben@example.com", "Ben Okafor", "owner", 1),
        ("carla@example.com", "Carla Ruiz", "tenant", 2),
        ("dev@example.com", "Dev Patel", "owner", 4),
        ("erin@example.com", "Erin Walsh", "owner", 5),
        ("farid@example.com", "Farid Haddad", "tenant", 8),
        ("grace@example.com", "Grace Kim", "owner", 9),
    ]
    residents = []
    for email, name, role, idx in people:
        u = User(email=email, name=name, role=role, unit=units[idx])
        u.set_password("resident123")
        residents.append(u)
    db.session.add_all([admin, *residents])
    db.session.commit()

    from .views.billing import generate_dues
    today = date.today()
    for back in (2, 1, 0):
        y, m = today.year, today.month - back
        while m < 1:
            y, m = y - 1, m + 12
        generate_dues(y, m)

    # Most units are paid up for earlier months; a couple are behind.
    for i, unit in enumerate(units):
        months_paid = 3 if i % 5 else (1 if i % 2 else 2)
        if i in (1, 9):
            months_paid = 1
        db.session.add(Payment(unit=unit, amount_cents=unit.monthly_fee_cents * months_paid,
                               paid_on=today - timedelta(days=3 + i), method="bank transfer",
                               recorded_by=admin))

    db.session.add_all([
        Announcement(title="Annual general meeting", pinned=True, author=admin,
                     body="The AGM will be held in the community room on the 20th at 7pm. "
                          "Agenda: budget approval, roof replacement reserve, and board elections."),
        Announcement(title="Pool closing for the season", author=admin,
                     body="The outdoor pool closes for winter at the end of this month."),
        Announcement(title="Window cleaning", author=admin,
                     body="Exterior window cleaning is scheduled for next Tuesday and Wednesday. "
                          "Please keep balconies clear."),
    ])

    db.session.add_all([
        MaintenanceRequest(unit=units[0], submitted_by=residents[0], title="Leaking kitchen faucet",
                           description="Constant drip from the kitchen faucet, getting worse.",
                           category="Plumbing", priority="normal", status="open"),
        MaintenanceRequest(unit=units[2], submitted_by=residents[2], title="Heating not working",
                           description="Bedroom radiator stays cold.", category="HVAC",
                           priority="high", status="in_progress"),
        MaintenanceRequest(unit=None, submitted_by=residents[1], title="Lobby light flickering",
                           description="Ceiling light near the mailboxes flickers.",
                           category="Common area", priority="low", status="open"),
        MaintenanceRequest(unit=units[4], submitted_by=residents[3], title="Dishwasher not draining",
                           description="Standing water after each cycle.", category="Appliance",
                           priority="normal", status="resolved"),
    ])

    room = Amenity(name="Community room", description="Seats 40. Kitchenette included.",
                   open_hour=9, close_hour=23, max_hours=5)
    gym = Amenity(name="Fitness centre", description="Treadmills, weights and a rowing machine.",
                  open_hour=6, close_hour=22, max_hours=2)
    guest = Amenity(name="Guest suite", description="One-bedroom suite for visiting guests.",
                    open_hour=0, close_hour=24, max_hours=24)
    db.session.add_all([room, gym, guest])
    tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time())
    db.session.add_all([
        Booking(amenity=room, user=residents[0], start=tomorrow.replace(hour=18),
                end=tomorrow.replace(hour=21), notes="Birthday party"),
        Booking(amenity=gym, user=residents[3], start=tomorrow.replace(hour=7),
                end=tomorrow.replace(hour=8)),
    ])
    db.session.commit()
