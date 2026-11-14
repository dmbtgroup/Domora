import io
from datetime import date, timedelta

from condo.extensions import db
from condo.models import (Expense, MaintenanceRequest, Owner, Partner, PartnerDocument, PartnerRating, Poll, Quote,
                          RoutineTask, Unit, User, utcnow)

from .conftest import login

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def user(email):
    return db.session.scalar(db.select(User).where(User.email == email))


def add_partner(client, name="Déneigement Laval", trade="snow", **extra):
    data = {"name": name, "trade": trade, "phone": "514-555-0199", "website": "deneigement.example", "active": "1"}
    data.update(extra)
    client.post("/maintenance/partners/new", data=data)
    return db.session.scalar(db.select(Partner).where(Partner.name == name))


def a_request(title="Replace the roof membrane", kind="project"):
    req = MaintenanceRequest(title=title, description="Old membrane.", category="Other", kind=kind,
                             submitted_by=user("admin@test.com"))
    db.session.add(req)
    db.session.commit()
    return req


def add_quote(client, req, partner, amount):
    client.post("/maintenance/partners/quotes/new", content_type="multipart/form-data", data={
        "request_id": str(req.id), "partner_id": str(partner.id), "amount": amount, "quoted_on": "2026-11-10",
        "file": (io.BytesIO(PDF), "quote.pdf")})
    return db.session.scalar(db.select(Quote).where(Quote.partner_id == partner.id, Quote.request_id == req.id))


def board_of(*emails):
    """Board members with accounts (one per unit 101/102/201)."""
    units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all()
    for i, email in enumerate(emails):
        u = User(email=email, name=email.split("@")[0].title(), role="owner", unit=units[i % len(units)])
        u.set_password("password123")
        o = Owner(unit=units[i % len(units)], name=u.name, role="owner", email=email)
        o.positions = ["president"] if i == 0 else ["director"]
        db.session.add_all([u, o])
    db.session.commit()


def test_two_top_tabs_and_partner_list(admin_client):
    page = admin_client.get("/maintenance/").data.decode()
    switch = page.split('class="segmented section-switch"')[1].split("</nav>")[0]
    assert "Projects &amp; Maintenance" in switch and "Business partners" in switch
    p = add_partner(admin_client)
    page = admin_client.get("/maintenance/partners/").data.decode()
    assert "Déneigement Laval" in page and "Snow removal" in page and 'aria-current="page">Business partners' in page
    assert admin_client.get(f"/maintenance/partners/{p.id}").status_code == 200
    filtered = admin_client.get("/maintenance/partners/?trade=plumbing").data.decode()
    assert "Déneigement Laval" not in filtered


def test_new_partner_is_linked_to_existing_contractor_names(admin_client):
    db.session.add_all([RoutineTask(title="Snow removal", frequency="yearly", next_due=date(2026, 11, 15),
                                    vendor="déneigement laval"),
                        Expense(spent_on=date(2026, 11, 1), description="Snow", amount_cents=-480000,
                                source="Déneigement Laval")])
    db.session.commit()
    p = add_partner(admin_client)
    assert db.session.scalar(db.select(RoutineTask)).partner_id == p.id
    assert db.session.scalar(db.select(Expense)).partner_id == p.id
    assert p.spent_cents(2026) == 480000
    page = admin_client.get(f"/maintenance/partners/{p.id}").data.decode()
    assert "$4,800.00" in page and "Snow removal" in page


def test_documents_ratings_and_cost_link(admin_client):
    p = add_partner(admin_client)
    admin_client.post(f"/maintenance/partners/{p.id}/documents", content_type="multipart/form-data", data={
        "kind": "insurance", "valid_until": (date.today() + timedelta(days=20)).isoformat(),
        "file": (io.BytesIO(PDF), "insurance.pdf")})
    doc = db.session.scalar(db.select(PartnerDocument))
    assert doc.kind == "insurance" and p.expiring_documents()
    assert admin_client.get(f"/maintenance/partners/documents/{doc.id}").status_code == 200
    admin_client.post(f"/maintenance/partners/{p.id}/ratings", data={"stars": "4", "comment": "On time"})
    assert db.session.get(Partner, p.id).rating == 4
    admin_client.post("/accounting/costs/new", data={"kind": "cost", "category": "maintenance", "description": "Snow",
                                                     "amount": "500", "spent_on": "2026-11-12", "partner_id": str(p.id)})
    cost = db.session.scalar(db.select(Expense))
    assert cost.partner_id == p.id and cost.source == "Déneigement Laval"


def test_owners_see_a_short_directory_and_tenants_nothing(app, client):
    login(client, "admin@test.com")
    p = add_partner(client, notes="Slow to answer")
    client.post("/logout")
    login(client, "alice@test.com")  # owner
    page = client.get("/maintenance/partners/").data.decode()
    assert "Déneigement Laval" in page and "deneigement.example" in page and "Slow to answer" not in page
    assert client.get(f"/maintenance/partners/{p.id}").status_code == 403
    assert client.post("/maintenance/partners/new", data={"name": "X", "trade": "other"}).status_code == 403
    client.post("/logout")
    login(client, "bob@test.com")  # tenant
    assert client.get("/maintenance/partners/").status_code == 403
    assert "Business partners" not in client.get("/maintenance/").data.decode()


def test_quotes_side_by_side_and_president_assigns(admin_client):
    req = a_request()
    a, b = add_partner(admin_client, "Toitures ABC", "roofing"), add_partner(admin_client, "Toit Pro", "roofing")
    qa, qb = add_quote(admin_client, req, a, "24,000"), add_quote(admin_client, req, b, "21,500")
    page = admin_client.get(f"/maintenance/{req.id}").data.decode()
    assert "Toitures ABC" in page and "$21,500.00" in page and "Board vote" in page
    admin_client.post(f"/maintenance/partners/quotes/{qb.id}/assign")  # admin stands in for the President
    req = db.session.get(MaintenanceRequest, req.id)
    assert req.partner_id == b.id
    assert db.session.get(Quote, qb.id).status == "accepted" and db.session.get(Quote, qa.id).status == "declined"
    assert any("Assigned to Toit Pro" in c.body for c in req.comments)


def test_only_the_president_assigns_directly(app, client):
    board_of("pres@x.com", "dir@x.com")
    login(client, "admin@test.com")
    req = a_request()
    q = add_quote(client, req, add_partner(client, "Toit Pro", "roofing"), "21,500")
    client.post("/logout")
    login(client, "dir@x.com")
    assert client.post(f"/maintenance/partners/quotes/{q.id}/assign").status_code == 403
    client.post("/logout")
    login(client, "pres@x.com")
    client.post(f"/maintenance/partners/quotes/{q.id}/assign")
    assert db.session.get(MaintenanceRequest, req.id).partner_id == q.partner_id


def _vote(client, email, poll, text):
    client.post("/logout")
    login(client, email)
    option = next(o for o in poll.options if o.text.startswith(text))
    client.post(f"/votes/{poll.id}/vote", data={"option_id": option.id})


def _start_vote(client, req):
    from condo.views.votes import to_local
    deadline = to_local(utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    client.post(f"/maintenance/partners/requests/{req.id}/board-vote", data={"deadline": deadline})
    return db.session.scalar(db.select(Poll).where(Poll.request_id == req.id).order_by(Poll.id.desc()))


def _close(app, client, poll):
    client.post("/logout")
    login(client, "pres@x.com")
    client.post(f"/votes/{poll.id}/close")


def test_board_vote_assigns_the_winning_quote(app, client):
    board_of("pres@x.com", "dir@x.com", "sec@x.com")
    login(client, "pres@x.com")
    req = a_request()
    a, b = add_partner(client, "Toitures ABC", "roofing"), add_partner(client, "Toit Pro", "roofing")
    add_quote(client, req, a, "24,000")
    add_quote(client, req, b, "21,500")
    poll = _start_vote(client, req)
    assert poll.audience == "board" and len(poll.voters) == 3 and [o.text for o in poll.options][-1] == "None of these"
    assert "Board vote in progress: 0 of 3 voted" in client.get(f"/maintenance/{req.id}").data.decode()
    _vote(client, "pres@x.com", poll, "Toit Pro")
    _vote(client, "dir@x.com", poll, "Toit Pro")
    _close(app, client, poll)
    req = db.session.get(MaintenanceRequest, req.id)
    assert req.partner_id == b.id and db.session.get(Poll, poll.id).outcome == f"assigned:{b.id}"
    assert any("by board vote, 2 of 3" in c.body for c in req.comments)


def test_board_vote_tie_or_too_few_votes_assigns_nobody(app, client):
    board_of("pres@x.com", "dir@x.com", "sec@x.com", "tre@x.com")
    login(client, "pres@x.com")
    req = a_request()
    a, b = add_partner(client, "Toitures ABC", "roofing"), add_partner(client, "Toit Pro", "roofing")
    add_quote(client, req, a, "24,000")
    add_quote(client, req, b, "21,500")
    poll = _start_vote(client, req)
    _vote(client, "pres@x.com", poll, "Toit Pro")
    _vote(client, "dir@x.com", poll, "Toitures ABC")
    _vote(client, "sec@x.com", poll, "Toit Pro")
    _vote(client, "tre@x.com", poll, "Toitures ABC")
    _close(app, client, poll)
    assert db.session.get(Poll, poll.id).outcome == "tie" and db.session.get(MaintenanceRequest, req.id).partner_id is None

    poll = _start_vote(client, req)  # only 2 of 4 vote: not more than half
    _vote(client, "pres@x.com", poll, "Toit Pro")
    _vote(client, "dir@x.com", poll, "Toit Pro")
    _close(app, client, poll)
    assert db.session.get(Poll, poll.id).outcome == "no quorum"
    assert db.session.get(MaintenanceRequest, req.id).partner_id is None


def test_none_of_these_wins(app, client):
    board_of("pres@x.com", "dir@x.com")
    login(client, "pres@x.com")
    req = a_request()
    add_quote(client, req, add_partner(client, "Toit Pro", "roofing"), "21,500")
    poll = _start_vote(client, req)
    _vote(client, "pres@x.com", poll, "None of these")
    _vote(client, "dir@x.com", poll, "None of these")
    _close(app, client, poll)
    assert db.session.get(Poll, poll.id).outcome == "none"
    assert db.session.scalar(db.select(Quote)).status == "pending"


def test_routine_task_takes_its_contractor_from_a_partner(admin_client):
    p = add_partner(admin_client)
    admin_client.post("/maintenance/routine/new", data={"title": "Snow removal", "frequency": "yearly", "category": "Common area",
                                                        "next_due": "2026-11-15", "partner_id": str(p.id)})
    t = db.session.scalar(db.select(RoutineTask))
    assert t.partner_id == p.id and t.vendor == "Déneigement Laval" and t.vendor_contact == "514-555-0199"
