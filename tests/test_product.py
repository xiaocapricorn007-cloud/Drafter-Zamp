"""First-time setup: naming the product, confirming it, and pitching it in every draft."""
import json

import pytest
from fastapi.testclient import TestClient

from app import db, main, pipeline, product
from tests.fakes import Fake

PERSON = {"name": "Asha Rao", "title": "CFO", "company": "Northwind"}
PASSWORD = "a long password"


@pytest.fixture
def setup(tmp_path):
    db.init(str(tmp_path / "t.db"))
    pipeline.run_inline = True
    fake = Fake()
    main.app.state.providers = fake
    client = TestClient(main.app)
    client.post("/api/signup", json={"name": "Rep One", "password": PASSWORD})
    yield client, fake
    main.app.state.providers = None
    pipeline.run_inline = False


def begin(client, **extra):
    return client.post("/api/setup", json={"company": "Acme", "product": "Widget Cloud", **extra})


def status(client):
    return client.get("/api/setup").json()


def finish(client):
    begin(client)
    assert client.post("/api/setup/confirm", json={"choice": 0}).status_code == 200


def test_a_new_account_has_no_product_and_cannot_draft_yet(setup):
    client, fake = setup
    assert client.get("/api/me").json()["pitching"] is None and status(client) == {"status": "none"}
    assert client.post("/api/runs", json=PERSON).status_code == 409
    assert not fake.calls


def test_it_stops_for_confirmation_before_any_research(setup):
    client, fake = setup
    begin(client)
    now = status(client)
    assert now["status"] == "choosing" and len(now["candidates"]) == 2 and now["candidates"][0]["company"] == "Acme"
    assert not [c for c in fake.calls if c[0] in ("research", "search_answers", "deep")]
    assert client.get("/api/me").json()["pitching"] is None


def test_confirming_researches_and_stores_the_product(setup):
    client, fake = setup
    finish(client)
    assert status(client)["status"] == "ready"
    calls = [c[1] for c in fake.calls if c[0] in ("light", "deep")]
    assert calls == ["products", "product_questions", "product_profile"]
    asked = len([c for c in fake.calls if c[0] == "search_answers"])
    assert asked == len([c for c in fake.calls if c[0] == "research"]) == 3
    pitching = client.get("/api/me").json()["pitching"]
    assert pitching["company"] == "Acme" and pitching["product"] == "Widget Cloud" and pitching["offerings"][0]["name"] == "Faster close"


def test_claims_without_a_source_are_thrown_out(setup):
    client, _ = setup
    finish(client)
    assert client.get("/api/me").json()["pitching"]["offerings"][0]["proof"] == ["Northwind hired four analysts before switching."]
    assert any("Threw out 2" in n for s in status(client)["steps"] for n in s["notes"])


def test_drafts_pitch_the_stored_product(setup):
    client, fake = setup
    finish(client)
    run_id = client.post("/api/runs", json=PERSON).json()["id"]
    client.post(f"/api/runs/{run_id}/confirm", json={"person": 0})
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "ready" and run["data"]["seller_name"] == "Acme"
    assert "Best,\nRep One\nAcme" in run["data"]["draft"]["body"] and run["data"]["hooks"][0]["offering_id"] == "o1"


def test_changing_product_only_affects_new_drafts(setup):
    client, _ = setup
    finish(client)
    first = client.post("/api/runs", json=PERSON).json()["id"]
    client.post(f"/api/runs/{first}/confirm", json={"person": 0})
    user = db.find_user("Rep One")
    db.set_seller(user["id"], json.dumps({**json.loads(user["seller"]), "seller": "Newco"}))
    assert client.post(f"/api/runs/{first}/review", json={"action": "next"}).status_code == 200
    assert client.get(f"/api/runs/{first}").json()["data"]["seller_name"] == "Acme"
    second = client.post("/api/runs", json=PERSON).json()["id"]
    client.post(f"/api/runs/{second}/confirm", json={"person": 0})
    assert client.get(f"/api/runs/{second}").json()["data"]["seller_name"] == "Newco"


def test_old_product_stays_until_the_new_one_is_finished(setup):
    client, _ = setup
    finish(client)
    begin(client, product="Second Thing")
    assert status(client)["status"] == "choosing"
    assert client.get("/api/me").json()["pitching"]["product"] == "Widget Cloud"


def test_none_of_these_needs_a_description(setup):
    client, _ = setup
    begin(client)
    assert client.post("/api/setup/confirm", json={"choice": None, "description": "too short"}).status_code == 400
    assert status(client)["status"] == "choosing"
    assert client.post("/api/setup/confirm", json={"choice": None, "description": "Cloud software that closes the books faster."}).status_code == 200
    now = status(client)
    assert now["status"] == "ready" and now["chosen"]["company"] == "Acme" and now["chosen"]["sources"] == []


def test_nothing_found_online_still_lets_you_describe_it(setup):
    client, fake = setup
    fake.no_products = True
    begin(client)
    assert status(client)["status"] == "choosing" and status(client)["candidates"] == []
    assert client.post("/api/setup/confirm", json={"description": "Cloud software that closes the books faster."}).status_code == 200
    assert status(client)["status"] == "ready"


def test_a_search_outage_still_lets_you_describe_it(setup):
    client, fake = setup
    fake.fail_search = True
    begin(client)
    assert status(client)["status"] == "choosing" and status(client)["candidates"] == []


def test_a_bad_choice_and_a_double_confirm_are_refused(setup):
    client, _ = setup
    begin(client)
    assert client.post("/api/setup/confirm", json={"choice": 9}).status_code == 400
    assert client.post("/api/setup/confirm", json={"choice": 0}).status_code == 200
    assert client.post("/api/setup/confirm", json={"choice": 0}).status_code == 409


def test_website_is_searched_first(setup):
    client, fake = setup
    begin(client, website="https://www.acme.co/widget")
    assert [c[1] for c in fake.calls if c[0] == "search"][0] == "site:acme.co Widget Cloud"


def test_required_fields(setup):
    client, _ = setup
    assert client.post("/api/setup", json={"company": " ", "product": "Thing"}).status_code == 422


def test_each_account_has_its_own_product(setup):
    client, _ = setup
    finish(client)
    other = TestClient(main.app)
    other.post("/api/signup", json={"name": "Rep Two", "password": PASSWORD})
    assert other.get("/api/me").json()["pitching"] is None and other.get("/api/setup").json() == {"status": "none"}


def test_setup_needs_a_login(setup):
    assert TestClient(main.app).post("/api/setup", json={"company": "Acme", "product": "Thing"}).status_code == 401


def test_a_crash_is_reported_not_stuck(setup, monkeypatch):
    client, fake = setup
    begin(client)
    monkeypatch.setattr(fake, "_product_profile", lambda user: 1 / 0)
    client.post("/api/setup/confirm", json={"choice": 0})
    assert status(client)["status"] == "failed" and client.get("/api/me").json()["pitching"] is None
    assert client.post("/api/setup/reset", json={}).status_code == 200 and status(client) == {"status": "none"}


def test_a_restart_during_setup_is_reported_not_stuck(setup, tmp_path):
    client, _ = setup
    db.set_setup(db.find_user("Rep One")["id"], {"status": "researching"})
    db.init(str(tmp_path / "t.db"))
    assert status(client)["status"] == "failed"


def test_a_touchy_word_is_fine_when_it_describes_the_product():
    from datetime import date
    from app import answers

    hit = {"kind": "snippet", "ask": "", "title": "Acme", "url": "https://a.example/x", "date": "", "text": "Acme stops payment fraud before money leaves the account for its customers."}
    who = {"name": "Shield", "company": "Acme"}
    assert answers.build([hit], who, date(2026, 11, 9))[0] == []
    assert answers.build([hit], who, date(2026, 11, 9), avoid_touchy=False)[0]


@pytest.mark.parametrize("given,domain", [("https://www.acme.co/x", "acme.co"), ("acme.co", "acme.co"), ("", "")])
def test_websites_are_read(given, domain):
    assert product._domain(given) == domain


def test_official_sites_keep_cards_and_quotes_with_their_author():
    from datetime import date
    from app import answers

    text = """![](/img/logo.png)
## Hi, I'm Acme
\u201cAcme lets our teams focus on what matters most\u201d
Yatin Parab
CPO at Wio Bank
I'm running at 99%+ accuracy on every invoice
Privacy policy and cookie settings for this website
"""
    page = {"kind": "page", "ask": "", "title": "Acme", "url": "https://acme.co", "date": "", "text": text, "trusted": True}
    facts, _ = answers.build([page], {"name": "Thing", "company": "Acme"}, date(2026, 11, 9), strict=True, avoid_touchy=False)
    quotes = {f["quote"]: f for f in facts}
    praise = "\u201cAcme lets our teams focus on what matters most\u201d"
    assert praise in quotes and "Wio Bank" in quotes[praise]["context"]
    assert "I'm running at 99%+ accuracy on every invoice" in quotes
    assert not [q for q in quotes if "cookie" in q or "img" in q]


def test_a_claim_can_use_the_author_named_beside_a_quote():
    facts = {"f1": {"quote": "We process invoices fast", "context": "Finance lead, 40 stores"}}
    kept, dropped = product._proof_from([{"claim": "A finance lead with 40 stores praised it.", "fact_ids": ["f1"]}], facts)
    assert kept and not dropped
