"""A French demo condo, "Les Jardins du Parc", for showing the portal.

Everything is made up: names, emails (on .example), phones (555-01xx), amounts.
It is loaded through the portal's own pages (as a board member would enter it), so
fees, special contributions, votes and routine maintenance behave exactly as in real use.

Turned on by the DEMO environment variable: when the database has no units yet, it is
filled at start-up. Demo accounts all use the password DEMO_PASSWORD.
"""
import os
from datetime import date, timedelta

from .extensions import db
from .models import (Amenity, Announcement, BankBalance, Expense, MaintenanceRequest, Owner, Payment, Unit, User,
                     add_months)

DEMO_PASSWORD = "Demo2026!"
BOARD_EMAIL = "conseil@jardinsduparc.example"

UNITS = [  # number, floor, ownership share, owners: (name, email, phone, board positions)
    ("101", 1, 0.115, [("Sophie Lavoie", BOARD_EMAIL, "514-555-0101", ["president"])]),
    ("102", 1, 0.125, [("Marc Gagnon", "marc.gagnon@demo.example", "514-555-0102", ["treasurer"]),
                       ("Julie Gagnon", "julie.gagnon@demo.example", "514-555-0103", [])]),
    ("103", 1, 0.120, [("Amélie Roy", "amelie.roy@demo.example", "514-555-0104", ["secretary"])]),
    ("104", 1, 0.140, [("Karim Benali", "karim.benali@demo.example", "514-555-0105", [])]),
    ("201", 2, 0.115, [("Isabelle Côté", "isabelle.cote@demo.example", "514-555-0106", [])]),
    ("202", 2, 0.125, [("Nguyen Van Minh", "minh.nguyen@demo.example", "514-555-0107", [])]),
    ("203", 2, 0.120, [("Louis Bouchard", "louis.bouchard@demo.example", "514-555-0108", []),
                       ("Chloé Martin", "chloe.martin@demo.example", "514-555-0109", [])]),
    ("204", 2, 0.140, [("Élodie Fortin", "elodie.fortin@demo.example", "514-555-0110", [])]),
]


def enabled():
    return bool(os.environ.get("DEMO"))


def load_if_empty(app):
    """Fill an empty database with the demo condo (start-up hook when DEMO is set)."""
    if db.session.scalar(db.select(db.func.count(Unit.id))):
        return False
    load(app)
    return True


def load(app):
    today = date.today()
    fiscal_start = date(today.year if today.month >= 5 else today.year - 1, 5, 1)

    # --- units, owners and their accounts -----------------------------------------------------
    units = {}
    for number, floor, share, owners in UNITS:
        unit = Unit(number=number, floor=floor, ownership_share=share, bedrooms=2 if share < 0.13 else 3,
                    area_sqft=round(9000 * share), parking_spot=f"P{number}")
        db.session.add(unit)
        units[number] = unit
        for i, (name, email, phone, positions) in enumerate(owners):
            o = Owner(unit=unit, name=name, role="owner", email=email, phone=phone, is_primary=(i == 0))
            o.positions = positions
            db.session.add(o)
            user = User(email=email, name=name, role="owner", unit=unit, phone=phone, language="fr")
            user.set_password(DEMO_PASSWORD)
            db.session.add(user)
    tenant = User(email="locataire@demo.example", name="Thomas Pelletier", role="tenant", unit=units["202"],
                  language="fr")
    tenant.set_password(DEMO_PASSWORD)
    db.session.add(tenant)
    db.session.add(Owner(unit=units["202"], name="Thomas Pelletier", role="tenant", email="locataire@demo.example",
                         phone="514-555-0111", is_primary=True))
    db.session.commit()
    board = db.session.scalar(db.select(User).where(User.email == BOARD_EMAIL))

    # The rest goes through the portal's own pages, signed in as the board president.
    app.config["WTF_CSRF_ENABLED"], csrf_was = False, app.config.get("WTF_CSRF_ENABLED", True)
    try:
        with app.test_client() as c:
            c.post("/login", data={"email": BOARD_EMAIL, "password": DEMO_PASSWORD})
            _fill(c, units, board, today, fiscal_start)
    finally:
        app.config["WTF_CSRF_ENABLED"] = csrf_was


def _fill(c, units, board, today, fiscal_start):
    y = fiscal_start.year
    # Condo fees for the period, applied to every unit by ownership share.
    c.post(f"/accounting/fees/{y}", data={"basis": "annual", "amount": "48,000.00",
                                          "starts_on": fiscal_start.isoformat(),
                                          "ends_on": (add_months(fiscal_start, 12) - timedelta(days=1)).isoformat()})
    c.post(f"/accounting/fees/{y}/apply")

    # Monthly dues since the start of the period, and most of them paid.
    from .views.billing import generate_dues
    months, m = [], fiscal_start
    while m <= today:
        months.append(m)
        m = add_months(m, 1)
    for month in months:
        generate_dues(month.year, month.month)
    late = {"104": 2, "203": 1}  # months behind
    for number, unit in units.items():
        for month in months[:len(months) - late.get(number, 0)]:
            paid_on = min(month + timedelta(days=3 + int(number[-1])), today)
            db.session.add(Payment(unit=unit, amount_cents=unit.monthly_fee_cents, paid_on=paid_on,
                                   method="bank transfer", recorded_by=board, notes="Frais de condo"))
    db.session.commit()

    # Building costs.
    costs = [
        (25, "repairs", "Réparation de la porte du garage", 84250, "Portes Laval inc."),
        (60, "utilities", "Électricité — parties communes", 41275, "Fournisseur d'électricité"),
        (30, "utilities", "Électricité — parties communes", 39860, "Fournisseur d'électricité"),
        (45, "insurance", "Assurance de l'immeuble — versement", 310000, "Assurances Démo"),
        (12, "maintenance", "Entretien paysager — automne", 65000, "Paysagiste Démo"),
        (5, "maintenance", "Ménage des aires communes", 28000, "Entretien Démo"),
    ]
    for days, category, description, cents, source in costs:
        db.session.add(Expense(spent_on=today - timedelta(days=days), category=category, description=description,
                               amount_cents=-cents, source=source, recorded_by_id=board.id))
    db.session.commit()

    # A special contribution, billed, with some units paid.
    c.post("/accounting/specials/new", data={"title": "Réfection de la toiture", "method": "share", "total": "24,000",
                                            "deadline": (today + timedelta(days=45)).isoformat(),
                                            "description": "Remplacement de la membrane du toit, voté en assemblée."})
    from .models import SpecialContribution
    sc = db.session.scalar(db.select(SpecialContribution))
    if sc:
        c.post(f"/accounting/specials/{sc.id}/bill")
        for number in ("101", "102", "201", "204"):
            sh = next(s for s in sc.shares if s.unit.number == number)
            db.session.add(Payment(unit=units[number], amount_cents=sh.amount_cents, paid_on=today - timedelta(days=4),
                                   method="bank transfer", special_id=sc.id, recorded_by=board,
                                   notes=f"Special contribution — {sc.title}"))
        db.session.commit()

    # The bank account: what the bank showed a month ago.
    db.session.add(BankBalance(as_of=today - timedelta(days=31), amount_cents=6842015, note="Relevé bancaire",
                               recorded_by_id=board.id))
    db.session.commit()

    # Projects and repairs.
    c.post("/maintenance/new", data={"kind": "project", "title": "Réfection de la toiture", "category": "Other",
                                     "location": "Toit", "budget": "24,000", "priority": "high",
                                     "target_date": (today + timedelta(days=120)).isoformat(),
                                     "description": "Remplacement complet de la membrane et des solins."})
    c.post("/maintenance/new", data={"kind": "project", "title": "Rafraîchissement du hall d'entrée",
                                     "category": "Common area", "location": "Hall", "budget": "6,500",
                                     "priority": "normal", "description": "Peinture, éclairage DEL et boîtes aux lettres."})
    for unit, title, cat, prio, desc in (
            (None, "Lumière du stationnement défectueuse", "Electrical", "normal", "Le néon près de la place P104 clignote."),
            ("203", "Infiltration d'eau sous la fenêtre", "Plumbing", "high", "De l'eau s'accumule sous la fenêtre du salon."),
            (None, "Porte d'entrée qui ne se referme pas", "Common area", "normal", "Le ferme-porte doit être ajusté.")):
        c.post("/maintenance/new", data={"title": title, "category": cat, "priority": prio, "description": desc,
                                         "unit_id": str(units[unit].id) if unit else "", "location": ""})
    db.session.commit()

    # Routine maintenance.
    for title, freq, cat, due, vendor, est, sf, st in (
            ("Déneigement", "yearly", "Common area", date(today.year, 11, 15), "Déneigement Démo", "4,800", "11", "4"),
            ("Nettoyage des gouttières", "twice_yearly", "Common area", today + timedelta(days=20), "Gouttières Démo", "450", "", ""),
            ("Inspection des extincteurs", "yearly", "Security", today + timedelta(days=75), "Sécurité Démo", "300", "", "")):
        c.post("/maintenance/routine/new", data={"title": title, "frequency": freq, "category": cat,
                                                "next_due": due.isoformat(), "vendor": vendor, "estimated": est,
                                                "season_from": sf, "season_to": st, "vendor_contact": "514-555-0150"})

    # Notices.
    db.session.add_all([
        Announcement(title="Assemblée générale annuelle", pinned=True, author=board,
                     body="L'assemblée aura lieu le 25 au sous-sol, à 19 h. À l'ordre du jour : budget, toiture et élection du conseil."),
        Announcement(title="Déneigement", author=board,
                     body="Merci de libérer le stationnement les jours de déneigement annoncés."),
        Announcement(title="Nouveau portail des copropriétaires", author=board,
                     body="Vos relevés, les avis et les votes sont maintenant en ligne. Bienvenue !"),
    ])
    db.session.add_all([
        Amenity(name="Salle commune", description="20 places, cuisinette.", open_hour=9, close_hour=23, max_hours=5),
        Amenity(name="Salle d'exercice", description="Tapis roulant, vélo et poids libres.", open_hour=6, close_hour=22, max_hours=2),
    ])
    db.session.commit()

    # A vote in progress, weighted by ownership share.
    from .views.votes import to_local
    from .models import utcnow
    deadline = to_local(utcnow() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M")
    c.post("/votes/new", data={"title": "Approuvez-vous la soumission de 24 000 $ pour la toiture ?",
                               "audience": "owners", "weighting": "share", "deadline": deadline,
                               "options": ["Pour", "Contre", "Abstention"]})
    from .models import Poll
    poll = db.session.scalar(db.select(Poll))
    if poll:
        c.post(f"/votes/{poll.id}/publish")
        options = {o.text: o.id for o in poll.options}
        for email, choice in ((BOARD_EMAIL, "Pour"), ("marc.gagnon@demo.example", "Pour"),
                              ("isabelle.cote@demo.example", "Contre"), ("minh.nguyen@demo.example", "Pour")):
            c.post("/logout")
            c.post("/login", data={"email": email, "password": DEMO_PASSWORD})
            c.post(f"/votes/{poll.id}/vote", data={"option_id": options[choice]})
    db.session.commit()
