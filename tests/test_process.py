"""End-to-end runs of the process through the real API, with outside services replaced."""
import pytest
from fastapi.testclient import TestClient

from app import answers, checks, db, main, pipeline, profile, scoring, verify
from tests.fakes import Fake, ready_to_pitch

PERSON = {"name": "Asha Rao", "title": "CFO", "company": "Northwind"}


@pytest.fixture
def app_client(tmp_path):
    db.init(str(tmp_path / "t.db"))
    pipeline.run_inline = True
    fake = Fake()
    main.app.state.providers = fake
    client = TestClient(main.app)
    assert client.post("/api/signup", json={"name": "Rep One", "password": "a long password"}).status_code == 200
    ready_to_pitch("Rep One")
    yield client, fake
    main.app.state.providers = None
    pipeline.run_inline = False


def start(client, **extra):
    r = client.post("/api/runs", json={**PERSON, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def get(client, run_id):
    return client.get(f"/api/runs/{run_id}").json()


def confirm(client, run_id, person=0):
    return client.post(f"/api/runs/{run_id}/confirm", json={"person": person})


def test_waits_for_person_before_researching(app_client):
    client, fake = app_client
    run = get(client, start(client))
    assert run["status"] == "waiting"
    assert not [c for c in fake.calls if c[0] in ("research", "search_answers", "hunter", "deep")]


def test_happy_path(app_client):
    client, fake = app_client
    run_id = start(client)
    assert confirm(client, run_id).status_code == 200
    run = get(client, run_id)
    assert run["status"] == "ready", run["data"].get("problems")
    assert run["data"]["email"]["address"] == "asha@example.com"
    assert run["data"]["tests"]["only_them"] is True
    assert all(f["quote"] != "These words appear nowhere on the page at all" for f in run["data"]["facts"])
    states = {t["key"]: t["state"] for t in run["timeline"]}
    assert states["write"] == "done" and states["test"] == "done"
    body = run["data"]["draft"]["body"]
    assert body.startswith("Hi Asha,") and "Best,\nRep One\nZamp" in body
    assert run["data"]["confidence"]["score"] >= 75 and run["data"]["confidence"]["label"] == "High"
    r = client.post(f"/api/runs/{run_id}/review", json={"action": "approve", "subject": "hi", "body": "edited", "card_headline": "Big words"})
    assert r.status_code == 200
    run = get(client, run_id)
    assert run["status"] == "approved" and run["data"]["final"]["edited"] is True
    assert run["data"]["final"]["card_headline"] == "Big words"


def test_double_confirm_is_refused(app_client):
    client, _ = app_client
    run_id = start(client)
    confirm(client, run_id)
    assert confirm(client, run_id).status_code == 409


def test_common_name_shows_both_people(app_client):
    client, _ = app_client
    r = client.post("/api/runs", json={"name": "Sam Lee", "title": "CFO", "company": "Northwind"})
    run = get(client, r.json()["id"])
    assert run["status"] == "waiting" and len(run["data"]["people"]) == 2


def test_none_of_these_still_writes_without_research(app_client):
    client, fake = app_client
    run_id = start(client)
    client.post(f"/api/runs/{run_id}/confirm", json={"person": None})
    run = get(client, run_id)
    assert run["status"] == "ready" and run["data"]["confidence"]["score"] <= 25
    assert not [c for c in fake.calls if c[0] == "research"]


def test_moved_company_updates_prospect(app_client):
    client, fake = app_client
    fake.scenario = "moved"
    run_id = start(client)
    assert get(client, run_id)["data"]["people"][0]["different_company"] == "Northwind"
    confirm(client, run_id)
    assert get(client, run_id)["prospect"]["company"] == "Newco"


def test_sensitive_fact_is_not_used(app_client):
    client, fake = app_client
    fake.scenario = "sensitive"
    run_id = start(client)
    confirm(client, run_id)
    run = get(client, run_id)
    assert run["status"] == "ready"
    assert all("layoffs" not in h["summary"] for h in run["data"]["hooks"])
    assert any("layoffs" in s["summary"] for s in run["data"]["set_aside"])


def test_nothing_found_still_gives_a_general_draft_with_low_confidence(app_client):
    client, fake = app_client
    fake.scenario = "quiet"
    run_id = start(client)
    confirm(client, run_id)
    run = get(client, run_id)
    assert run["status"] == "ready" and run["data"]["draft"]["body"]
    assert run["data"]["confidence"]["score"] <= 35 and run["data"]["confidence"]["label"] == "Low"
    assert run["data"]["hooks"][0]["general"] is True


def test_no_fit_still_writes_but_says_so(app_client):
    client, fake = app_client
    fake.scenario = "weak"
    run_id = start(client)
    confirm(client, run_id)
    run = get(client, run_id)
    assert run["status"] == "ready" and run["data"]["confidence"]["score"] <= 45
    assert any("little to do" in r for r in run["data"]["confidence"]["bad"])


def test_nobody_found_online_can_still_go_ahead(app_client):
    client, fake = app_client
    fake.fail_search = fake.fail_research = True
    run_id = start(client)
    run = get(client, run_id)
    assert run["status"] == "waiting" and run["data"]["people"][0]["name"] == "Asha Rao"
    confirm(client, run_id)
    run = get(client, run_id)
    assert run["status"] == "ready" and run["data"]["unconfirmed"] is True
    assert run["data"]["confidence"]["score"] <= 25


def test_touchy_fact_is_never_the_fallback():
    fact_item = {"summary": "x", "score": 0, "sensitive": True, "reason": "r"}
    hooks, aside = scoring.fallback_hooks([fact_item], "close")
    assert hooks == [] and aside == [fact_item]


def test_weak_opening_can_never_be_high_confidence():
    readers = {"only_them": True, "same_idea": True, "would_open": True, "kind": True}
    sure = scoring.confidence({"score": 59, "weak": True}, readers, True, False, False)
    assert sure["score"] <= 65 and sure["label"] == "Medium"


def test_card_rules():
    good = {"sticky_line": "Stop hiring heroes", "card_line": "We take on the repeat work."}
    assert checks.check_card(good) == []
    assert checks.check_card({**good, "sticky_line": "This line is far too long for a good card headline"})
    assert checks.check_card({**good, "card_line": "Saved 4 days"})


def test_invented_quote_is_flagged_after_one_rewrite(app_client):
    client, fake = app_client
    bad = fake.good_draft()
    bad["lines"][0]["text"] = 'Asha, you said "we love spreadsheets more than people".'
    fake.submissions = [bad]
    run_id = start(client)
    confirm(client, run_id)
    run = get(client, run_id)
    assert run["status"] == "flagged"
    assert [c for c in fake.calls if c in (("deep", "plan_write"), ("deep", "writer"))] == [("deep", "plan_write"), ("deep", "writer")]
    assert not [c for c in fake.calls if c == ("light", "reader")]  # no AI spent on a failing draft


def test_writer_can_recover_after_a_failure(app_client):
    client, fake = app_client
    bad = fake.good_draft()
    bad["lines"][0]["kind"] = "ask"
    fake.submissions = [bad, fake.good_draft()]
    run_id = start(client)
    confirm(client, run_id)
    assert get(client, run_id)["status"] == "ready"


def test_search_failure_uses_backup(app_client):
    client, fake = app_client
    fake.fail_search = True
    assert get(client, start(client))["status"] == "waiting"


def test_research_failure_uses_saved_and_says_so(app_client):
    client, fake = app_client
    first = start(client)
    confirm(client, first)
    fake.fail_research = True
    second = start(client)
    confirm(client, second)
    run = get(client, second)
    assert run["status"] == "ready" and "used_saved" in run["data"]


def test_research_failure_with_nothing_saved_still_writes(app_client):
    client, fake = app_client
    fake.fail_research = True
    run_id = start(client)
    confirm(client, run_id)
    run = get(client, run_id)
    assert run["status"] == "ready" and run["data"]["hooks"][0]["general"] is True


def test_try_another_opening(app_client):
    client, fake = app_client
    run_id = start(client)
    confirm(client, run_id)
    assert client.post(f"/api/runs/{run_id}/review", json={"action": "next"}).status_code == 200
    run = get(client, run_id)
    assert run["status"] == "ready" and run["data"]["hook_pos"] == 1


def test_bad_input_is_refused(app_client):
    client, _ = app_client
    assert client.post("/api/runs", json={**PERSON, "name": "  "}).status_code == 422
    assert client.post("/api/runs", json={**PERSON, "linkedin": "https://x.com/a"}).status_code == 422


def test_quote_must_be_on_page():
    assert verify.quote_on_page("runs on “heroics” and long  nights", 'it runs on "heroics" and long nights!')
    assert not verify.quote_on_page("runs on magic and long nights", "it runs on heroics and long nights")


def test_same_company():
    assert verify.same_company("Acme Pvt Ltd", "Acme Private Limited")
    assert not verify.same_company("Acme", "Globex")


def test_masking_hides_names():
    prospect = {"name": "Asha Rao", "company": "Northwind Ltd", "title": "CFO"}
    assert checks._hide("Asha Rao at Northwind and asha", prospect) == "[person] at [company] and [person]"


def test_numbers_must_match_source():
    facts = {"f1": {"quote": "hiring four analysts", "what": "Hiring."}}
    good = [{"text": "You have 4 roles open.", "kind": "fact", "facts": ["f1"]}]
    bad = [{"text": "You have 9 roles open.", "kind": "fact", "facts": ["f1"]}]
    assert checks.check_facts(good, facts) == []
    assert checks.check_facts(bad, facts)


def test_style_rules():
    lines = [{"text": "I hope this finds you well.", "kind": "creative"}]
    problems = " ".join(checks.check_style("hello", lines))
    assert "overused phrase" in problems and "exactly one ask" in problems


def test_customer_results_must_match_our_list():
    proof = ["Reduced approval times from 2 weeks to 2 days"]
    good = [{"text": "One team went from 2 weeks to 2 days.", "kind": "proof", "facts": []}]
    bad = [{"text": "One team went from 9 weeks to 1 day.", "kind": "proof", "facts": []}]
    assert not [p for p in checks.check_facts(good, {}, proof) if "customer" in p]
    assert [p for p in checks.check_facts(bad, {}, proof) if "customer" in p]


def test_same_quote_on_two_sites_counts_once(app_client):
    client, _ = app_client
    run_id = start(client)
    confirm(client, run_id)
    quotes = [f["quote"] for f in get(client, run_id)["data"]["facts"]]
    assert len(quotes) == len(set(quotes))


def test_company_news_cannot_score_as_personal():
    from datetime import date
    fact = {"id": "f1", "what": "x", "quote": "y", "date": "2026-11-01", "about": "market"}
    item = {"fact_id": "f1", "offering_id": "close", "pressure": "", "why_they_care": "", "personal": 5, "pain": 3, "fit": 3, "unexpected": 1, "sensitive": False}
    ready, aside = scoring.rank([fact], [item], [], {"close"}, date(2026, 11, 9))
    assert not ready and aside[0]["parts"]["personal"] == 2


def test_claude_only_asks_and_writes(app_client):
    client, fake = app_client
    run_id = start(client)
    confirm(client, run_id)
    assert [c[1] for c in fake.calls if c[0] in ("light", "deep") and c[1] not in ("people", "reader", "judge", "card_art")] == ["questions", "plan_write"]
    assert get(client, run_id)["data"]["questions"]


def test_every_question_goes_to_both_searches(app_client):
    client, fake = app_client
    run_id = start(client)
    confirm(client, run_id)
    asked = len(get(client, run_id)["data"]["questions"])
    assert len([c for c in fake.calls if c[0] == "search_answers"]) == asked
    assert len([c for c in fake.calls if c[0] == "research"]) == asked


def test_questions_failing_falls_back_to_our_own(app_client, monkeypatch):
    client, fake = app_client
    real = fake.light_json

    def broken(purpose, *rest):
        if purpose == "questions":
            raise pipeline.ProviderError("busy")
        return real(purpose, *rest)

    monkeypatch.setattr(fake, "light_json", broken)
    run_id = start(client)
    confirm(client, run_id)
    run = get(client, run_id)
    assert run["status"] in ("ready", "flagged") and run["data"]["draft"]["body"] and len(run["data"]["questions"]) == 6


def test_serper_snippets_become_dated_facts(app_client):
    client, fake = app_client
    fake.snippets = [{"title": "Asha Rao on finance", "url": "https://news.example/a", "date": "2 weeks ago",
                      "snippet": "Asha Rao told the panel that invoice approvals slow her team down every single month ... Unrelated later text from elsewhere on the page."}]
    run_id = start(client)
    confirm(client, run_id)
    fact = next(f for f in get(client, run_id)["data"]["facts"] if "invoice approvals" in f["quote"])
    assert fact["about"] == "person" and fact["date"] and "Unrelated" not in fact["quote"]


def test_code_overrules_a_touchy_choice_and_writes_again(app_client, monkeypatch):
    client, fake = app_client
    real = fake._plan_write

    def touchy(user):
        plan = real(user)
        plan["openings"][0]["sensitive"], plan["openings"][0]["sensitive_reason"] = True, "Prying."
        return plan

    monkeypatch.setattr(fake, "_plan_write", touchy)
    run_id = start(client)
    confirm(client, run_id)
    assert ("deep", "writer") in fake.calls and get(client, run_id)["status"] == "ready"


def test_email_is_ready_before_the_card(app_client, monkeypatch):
    client, fake = app_client
    seen = {}
    real = fake._card_art

    def watch(user):
        run = db.list_runs(db.find_user("Rep One")["id"])[0]
        seen.update(status=run["status"], art=run["data"]["draft"]["art"])
        return real(user)

    monkeypatch.setattr(fake, "_card_art", watch)
    run_id = start(client)
    confirm(client, run_id)
    assert seen == {"status": "ready", "art": None}
    assert "<circle" in get(client, run_id)["data"]["draft"]["art"]


def test_a_late_picture_never_touches_a_locked_or_replaced_draft(app_client):
    client, _ = app_client
    run_id = start(client)
    confirm(client, run_id)
    token = get(client, run_id)["data"]["draft"]["token"]
    assert db.set_art(run_id, "an old draft", "<svg/>") is False
    client.post(f"/api/runs/{run_id}/lock", json={"code": "long-code-1234"})
    assert db.set_art(run_id, token, "<svg/>") is False
    stored = db.get_run(run_id)
    assert stored["data"] == {} and stored["sealed"]


def test_words_across_a_google_gap_are_not_one_quote():
    hit = {"kind": "snippet", "ask": "", "title": "T", "url": "https://a.example/x", "date": "",
           "text": "Asha Rao said the close is hard for everyone here ... and in other news Northwind bought a boat"}
    facts, _ = answers.build([hit], PERSON, __import__("datetime").date(2026, 11, 9))
    assert len(facts) == 1 and "..." not in facts[0]["quote"] and "boat" not in facts[0]["quote"]


def test_results_about_someone_else_are_dropped():
    hit = {"kind": "snippet", "ask": "", "title": "Other", "url": "https://a.example/x", "date": "", "text": "A completely different person said something long enough to count here."}
    assert answers.build([hit], PERSON, __import__("datetime").date(2026, 11, 9)) == ([], [])


def test_page_sentences_keep_exact_words_and_context():
    text = "Asha Rao joined Northwind as finance chief last spring this year. She said the month-end close still depends on heroics every single time. Buy our cookie policy newsletter today please now."
    hit = {"kind": "page", "ask": "month-end close", "title": "T", "url": "https://a.example/x", "date": "", "text": text}
    facts, _ = answers.build([hit], PERSON, __import__("datetime").date(2026, 11, 9))
    close = next(f for f in facts if "heroics" in f["quote"])
    assert close["quote"] in text and "Asha Rao" in close["context"] and close["about"] == "person"
    assert not [f for f in facts if "cookie" in f["quote"]]


def test_dates_from_search_engines_are_understood():
    today = __import__("datetime").date(2026, 11, 9)
    assert answers.normal_date("2 weeks ago", today) == "2026-10-26"
    assert answers.normal_date("Sep 23, 2026", today) == "2026-09-23"
    assert answers.normal_date("whenever", today) == ""


LINK = "https://www.linkedin.com/in/asha-rao"


def listed(title="Asha Rao - CFO - Northwind | LinkedIn", snippet="Finance leader at Northwind who has spent ten years rebuilding the month-end close."):
    return [{"title": title, "url": "https://in.linkedin.com/in/asha-rao/", "snippet": snippet}]


def name_searches(fake):
    return [c for c in fake.calls if c[0] == "search" and "linkedin.com/in/" not in c[1]]


def test_matching_link_skips_the_name_search_and_the_question(app_client):
    client, fake = app_client
    fake.profile = listed()
    run = get(client, start(client, linkedin=LINK))
    assert run["status"] == "ready" and not name_searches(fake) and ("light", "people") not in fake.calls
    profile_fact = next(f for f in run["data"]["facts"] if "rebuilding" in f["quote"])
    assert profile_fact["about"] == "person"
    assert {s["key"]: s["state"] for s in run["timeline"]}["confirm"] == "done"


def test_link_showing_a_different_name_stops_and_asks(app_client):
    client, fake = app_client
    fake.profile = listed(title="Maya Lindqvist - CFO - Northwind | LinkedIn")
    run = get(client, start(client, linkedin=LINK))
    assert run["status"] == "waiting" and len(run["data"]["people"]) == 1 and run["data"]["people"][0]["name"] == "Maya Lindqvist"
    assert not name_searches(fake) and not [c for c in fake.calls if c[0] == "research"]


def test_link_showing_another_company_stops_and_says_so(app_client):
    client, fake = app_client
    fake.profile = listed(title="Asha Rao - Partner at Globex", snippet="Advising companies on finance strategy across several markets today.")
    run = get(client, start(client, linkedin=LINK))
    person = run["data"]["people"][0]
    assert run["status"] == "waiting" and person["company"] == "Globex" and person["different_company"] == "Northwind"


def test_link_with_no_company_on_it_asks_instead_of_guessing(app_client):
    client, fake = app_client
    fake.profile = listed(title="Asha Rao - Leading teams building ...", snippet="Managing engineering teams and delivering scalable software products.")
    run = get(client, start(client, linkedin=LINK))
    assert run["status"] == "waiting" and run["data"]["people"][0]["company"] == "Northwind"


def test_unlisted_link_trusts_the_details_without_a_name_search(app_client):
    client, fake = app_client
    run = get(client, start(client, linkedin=LINK))
    assert run["status"] == "ready" and not name_searches(fake) and run["data"]["link_unseen"] is True
    assert run["data"]["confidence"]["score"] <= 60 and any("LinkedIn" in r for r in run["data"]["confidence"]["bad"])


def test_search_outage_falls_back_to_the_normal_route(app_client):
    client, fake = app_client
    fake.fail_search = True
    run = get(client, start(client, linkedin=LINK))
    assert run["status"] == "waiting" and name_searches(fake)


def test_a_company_page_link_is_not_treated_as_a_person(app_client):
    client, fake = app_client
    run = get(client, start(client, linkedin="https://www.linkedin.com/company/northwind"))
    assert run["status"] == "waiting" and name_searches(fake)


def test_with_a_link_pages_must_name_their_company():
    same_name = {"kind": "snippet", "ask": "", "title": "Asha Rao", "url": "https://a.example/x", "date": "", "text": "Asha Rao the painter opened a new gallery in Leeds this very spring."}
    day = __import__("datetime").date(2026, 11, 9)
    assert answers.build([same_name], PERSON, day)[0]
    assert answers.build([same_name], PERSON, day, strict=True)[0] == []


@pytest.mark.parametrize("title,expected", [
    ("Satya Nadella - Chairman and CEO at Microsoft", ("Satya Nadella", "Chairman and CEO", "Microsoft")),
    ("Asha Rao - CFO - Northwind | LinkedIn", ("Asha Rao", "CFO", "Northwind")),
    ("Nikhil Kamath - Leading Engineering teams building ...", ("Nikhil Kamath", "Leading Engineering teams building", "")),
    ("", ("", "", "")),
])
def test_linkedin_titles_are_read(title, expected):
    assert tuple(profile.parse_title(title).values()) == expected


@pytest.mark.parametrize("typed,shown,same", [
    ("Asha Rao", "Asha Rao", True), ("Purnima Sanwal", "Purnima S.", True), ("Asha", "Asha Rao", True),
    ("Asha Rao", "Maya Rao", False), ("Asha Rao", "Asha Kapoor", False), ("Asha Rao", "", False),
])
def test_names_are_compared_fairly(typed, shown, same):
    assert profile.names_match(typed, shown) is same


def test_style_failure_does_not_blame_the_facts():
    sure = scoring.confidence({"score": 80}, None, False, True, False, facts_ok=True)
    assert not any("matched" in r for r in sure["bad"]) and any("writing rules" in r for r in sure["bad"])
    assert any("matches where we found it" in r for r in sure["good"])


def test_wrong_facts_are_still_called_out():
    sure = scoring.confidence({"score": 80}, None, False, True, False, facts_ok=False)
    assert any("matched" in r for r in sure["bad"])


def test_unlisted_link_with_nothing_personal_scores_lower():
    readers = {"only_them": True, "same_idea": True, "would_open": True, "kind": True}
    some = scoring.confidence({"score": 80}, readers, True, False, False, link_unseen=True)
    none = scoring.confidence({"score": 80}, readers, True, False, False, link_unseen=True, nothing_personal=True)
    assert some["score"] == 60 and none["score"] == 40 and any("personally" in r for r in none["bad"])


@pytest.mark.parametrize("text,allowed", [
    ("Owning a payments operation and processing it are two different jobs.", True),
    ("Three things tend to go wrong at month-end.", True),
    ("You have 2 teams doing this.", False),
    ("Your nine offices all do this differently.", False),
])
def test_small_number_words_are_figures_of_speech(text, allowed):
    problems = [x for x in checks.check_facts([{"text": text, "kind": "creative", "facts": []}], {}, need_fact=False) if "number" in x]
    assert (not problems) is allowed


def test_the_writer_gets_only_the_best_facts():
    facts = [{"id": f"f{n}", "quote": "q", "context": "", "date": "", "about": "company", "answers": ""} for n in range(36)]
    packet = pipeline._choosing_packet(PERSON, __import__("app.config", fromlist=["load_seller"]).load_seller(), facts)
    assert len(packet["facts"]) == 15 and packet["facts"][0]["id"] == "f0"


def test_questions_are_written_while_finding_the_person(app_client):
    client, fake = app_client
    run_id = start(client)
    assert ("light", "questions") in fake.calls and not [c for c in fake.calls if c[0] == "research"]
    confirm(client, run_id)
    assert fake.calls.count(("light", "questions")) == 1


def test_questions_are_rewritten_if_the_person_works_elsewhere(app_client):
    client, fake = app_client
    fake.scenario = "moved"
    confirm(client, start(client))
    assert fake.calls.count(("light", "questions")) == 2


def test_the_writer_is_told_the_style_rules():
    from app import prompts

    for text in (prompts.WRITER_SYSTEM, prompts.PLAN_WRITE_SYSTEM):
        assert "one to seven words" in text and "i came across" in text


def test_a_reader_failure_lowers_the_score_without_a_rewrite(app_client, monkeypatch):
    client, fake = app_client
    monkeypatch.setattr(fake, "_judge", lambda user: {"same_idea": False, "would_open": True, "kind": True, "adds_outside_fact": False, "note": ""})
    run_id = start(client)
    confirm(client, run_id)
    run = get(client, run_id)
    assert run["status"] == "flagged" and run["data"]["problems"] and run["data"]["confidence"]["score"] <= 50
    assert ("deep", "writer") not in fake.calls and run["data"]["tests_pending"] is False


def test_email_appears_before_the_readers_finish(app_client, monkeypatch):
    client, fake = app_client
    seen = {}
    real = fake._reader

    def watch(user):
        run = db.list_runs(db.find_user("Rep One")["id"])[0]
        seen.update(status=run["status"], pending=run["data"]["tests_pending"], sure=run["data"]["confidence"])
        return real(user)

    monkeypatch.setattr(fake, "_reader", watch)
    confirm(client, start(client))
    assert seen == {"status": "ready", "pending": True, "sure": None}


def test_reader_outage_still_leaves_a_usable_email(app_client, monkeypatch):
    client, fake = app_client

    def down(user):
        raise pipeline.ProviderError("busy")

    monkeypatch.setattr(fake, "_reader", down)
    run_id = start(client)
    confirm(client, run_id)
    run = get(client, run_id)
    assert run["status"] == "ready" and run["data"]["tests"] is None and run["data"]["tests_pending"] is False and run["data"]["confidence"]


def test_late_results_never_touch_finished_locked_or_replaced_drafts(app_client):
    client, _ = app_client
    run_id = start(client)
    confirm(client, run_id)
    token = get(client, run_id)["data"]["draft"]["token"]
    assert db.finish_tests(run_id, "an old draft", True, problems=["x"]) is False
    client.post(f"/api/runs/{run_id}/review", json={"action": "approve", "subject": "s", "body": "b", "card_headline": "c"})
    assert db.finish_tests(run_id, token, True, problems=["x"]) is True and get(client, run_id)["status"] == "approved"
    other = start(client)
    confirm(client, other)
    other_token = get(client, other)["data"]["draft"]["token"]
    client.post(f"/api/runs/{other}/lock", json={"code": "long-code-1234"})
    assert db.finish_tests(other, other_token, True, problems=["x"]) is False and db.get_run(other)["data"] == {}


def test_a_restart_while_checking_does_not_stay_checking(app_client, tmp_path):
    client, _ = app_client
    run_id = start(client)
    confirm(client, run_id)
    db.update_run(run_id, tests_pending=True)
    db.init(str(tmp_path / "t.db"))
    assert get(client, run_id)["data"]["tests_pending"] is False
