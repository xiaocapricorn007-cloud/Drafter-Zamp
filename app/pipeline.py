"""The process, stage by stage. Every stage reports to the event log that drives the live view."""
import json
import logging
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime

from . import answers, art, checks, config, db, profile, prompts, scoring, verify
from .providers import ProviderError

log = logging.getLogger("drafter")

STAGES = [
    ("find", "Finding the person"),
    ("confirm", "Checking with you"),
    ("email", "Looking for their email"),
    ("need", "Working out what to ask"),
    ("read", "Looking for the answers"),
    ("facts", "Keeping what we can prove"),
    ("write", "Writing the message"),
    ("test", "Testing the message"),
    ("art", "Drawing the card"),
]

_pool = ThreadPoolExecutor(max_workers=4)
_ahead_pool = ThreadPoolExecutor(max_workers=4)
_ahead: dict[str, tuple[str, Future]] = {}  # questions being written while the person is looked up
run_inline = False  # tests turn this on


def launch(fn, run_id: str, providers, *args) -> None:
    def guarded():
        try:
            fn(run_id, providers, *args)
        except Exception:
            log.exception("Run %s failed", run_id)
            run = db.get_run(run_id)
            active = [k for k, s in timeline_states(run_id).items() if s == "running"]
            for key in active:
                stage(run_id, key, "stopped")
            if run and run["status"] in db.BUSY:
                db.update_run(run_id, status="failed", problem="Something went wrong on our side. Please try again.")

    if run_inline:
        guarded()
    else:
        _pool.submit(guarded)


def stage(run_id: str, key: str, state: str) -> None:
    db.add_event(run_id, "stage", {"key": key, "state": state})


def note(run_id: str, key: str, text: str) -> None:
    db.add_event(run_id, "note", {"key": key, "text": text})


def timeline_states(run_id: str) -> dict:
    states = {}
    for ev in db.events_since(run_id):
        if ev["kind"] == "stage":
            states[ev["key"]] = ev["state"]
    return states


def timeline(run_id: str) -> list[dict]:
    items = {k: {"key": k, "label": label, "state": "todo", "notes": []} for k, label in STAGES}
    for ev in db.events_since(run_id):
        if ev.get("key") not in items:
            continue
        if ev["kind"] == "stage":
            items[ev["key"]]["state"] = ev["state"]
        elif ev["kind"] == "note":
            items[ev["key"]]["notes"].append(ev["text"])
    return list(items.values())


def _prospect_key(prospect: dict) -> str:
    return f"{prospect['name']}|{verify.company_core(prospect['company'])}"


def _seller_of(run: dict) -> dict:
    """What this draft pitches, fixed when it began. Drafts from before accounts had products pitched Zamp."""
    return run["data"].get("seller") or config.load_seller()


def lookup_keys(run: dict) -> list[str]:
    """Every name this draft's web results were saved under."""
    return list({*run["data"].get("lookup_keys", []), _prospect_key(run["prospect"])})


def sign_old_drafts(user: dict) -> None:
    """Drafts from before accounts existed end with a blank for the name, so fill it in."""
    blank = "[Your name]"
    name = user["sender"] or user["name"]
    for run in db.list_runs(user["id"]):
        changes = {
            part: {**run["data"][part], "body": run["data"][part]["body"].replace(blank, name)}
            for part in ("draft", "final")
            if blank in (run["data"].get(part) or {}).get("body", "")
        }
        if changes:
            db.update_run(run["id"], **changes)


def _signed_by(seller: dict, run: dict) -> dict:
    """The email ends with the name of the person who asked for it."""
    owner = db.get_user(run["user_id"]) if run["user_id"] else None
    return {**seller, "sender": owner["sender"] or owner["name"]} if owner else seller


# ---- 1. find the person ---------------------------------------------------


def find_person(run_id: str, providers) -> None:
    run = db.get_run(run_id)
    prospect = run["prospect"]
    name, title, company = prospect["name"], prospect["title"], prospect["company"]
    stage(run_id, "find", "running")
    if _use_linkedin(run_id, providers, prospect):
        return
    _ask_ahead(run_id, providers, prospect, _seller_of(run))

    queries = [f'"{name}" "{company}"', f'"{name}" {title} {company} linkedin', f'"{name}" {title}']
    results, seen, failures = [], set(), 0
    for query in queries:
        try:
            found = providers.web_search(query)
        except ProviderError as exc:
            log.warning("Search failed: %s", exc)
            failures += 1
            try:
                found = [
                    {"title": r["title"], "url": r["url"], "snippet": r["text"][:400]}
                    for r in providers.research(query)
                ]
            except ProviderError as exc2:
                log.warning("Backup search failed: %s", exc2)
                found = []
        for item in found:
            if item["url"] not in seen:
                seen.add(item["url"])
                results.append(item)

    key = _prospect_key(prospect)
    db.update_run(run_id, lookup_keys=[key])
    if results:
        db.save("people_results", key, results)
    else:
        saved = db.load("people_results", key)
        if saved:
            results = saved[0]
            db.update_run(run_id, used_saved=_saved_label(saved[1]))
            note(run_id, "find", _saved_label(saved[1]))

    note(run_id, "find", f"Looked at {len(results)} web results")
    if not results:
        _ask_about_typed_person(run_id, prospect, queries)
        return

    listing = "\n".join(
        f"[{i}] {r['title']} — {r['url']}\n{r['snippet']}" for i, r in enumerate(results)
    )
    answer = providers.light_json(
        "people",
        prompts.PEOPLE_SYSTEM,
        f"Looking for: {name}, {title}, {company}\n\n<results>\n{listing}\n</results>",
        prompts.PEOPLE_SCHEMA,
    )
    people = _build_people(answer["people"], results, prospect)
    if not people:
        _ask_about_typed_person(run_id, prospect, queries)
        return
    stage(run_id, "find", "done")
    note(run_id, "find", "Found one possible match" if len(people) == 1 else f"Found {len(people)} possible matches")
    stage(run_id, "confirm", "waiting")
    db.update_run(run_id, status="waiting", people=people, searched=queries)


def _use_linkedin(run_id: str, providers, prospect: dict) -> bool:
    """A link says exactly who this is, so a search by name could only find the wrong person."""
    slug = verify.linkedin_slug(prospect.get("linkedin", ""))
    if not slug:
        return False
    try:
        found = profile.look_up(providers, slug)
    except ProviderError as exc:
        log.warning("LinkedIn lookup failed: %s", exc)
        return False  # a search outage says nothing about the link, so use the normal route

    if found is None:
        _go_ahead(run_id, providers, "Google hasn't listed that LinkedIn page, so we'll go with the details you gave", link_unseen=True)
        return True
    name_ok, company_ok = profile.fits(found, prospect)
    if name_ok and company_ok:
        _go_ahead(run_id, providers, "Your LinkedIn link matches the details you gave", profile=found)
        return True

    # The link and the typed details disagree, and only the rep can say which is right.
    moved = bool(found["company"]) and not company_ok
    stage(run_id, "find", "done")
    note(run_id, "find", "Your LinkedIn link doesn't quite match the details you gave")
    stage(run_id, "confirm", "waiting")
    db.update_run(run_id, status="waiting", profile=found, people=[{
        "name": found["name"] or prospect["name"],
        "title": found["job"] if moved else prospect["title"],
        "company": found["company"] if moved else prospect["company"],
        "about": " ".join(found["snippets"]) or found["title"],
        "sources": [{"title": found["title"] or "LinkedIn", "url": found["url"]}],
        "matches_link": True,
        "different_company": prospect["company"] if moved else "",
    }])
    return True


def _go_ahead(run_id: str, providers, reason: str, **fields) -> None:
    stage(run_id, "find", "done")
    note(run_id, "find", reason)
    stage(run_id, "confirm", "done")
    db.update_run(run_id, status="working", **fields)
    research_and_write(run_id, providers)


def _ask_about_typed_person(run_id: str, prospect: dict, queries: list[str]) -> None:
    """Nobody found online. The rep can still go ahead with what they typed."""
    stage(run_id, "find", "done")
    note(run_id, "find", "We couldn't find them online")
    typed = {
        "name": prospect["name"],
        "title": prospect["title"],
        "company": prospect["company"],
        "about": "We couldn't find them online, but we can still write from what you gave us.",
        "sources": [],
        "matches_link": False,
        "different_company": "",
    }
    stage(run_id, "confirm", "waiting")
    db.update_run(run_id, status="waiting", people=[typed], searched=queries)


def _build_people(raw: list[dict], results: list[dict], prospect: dict) -> list[dict]:
    slug = verify.linkedin_slug(prospect.get("linkedin", ""))
    people = []
    for person in raw:
        sources = [results[i] for i in person["source_ids"] if 0 <= i < len(results)]
        if not sources or not person["name"].strip():
            continue  # no evidence, so we don't show it
        moved = bool(person["company"]) and not verify.same_company(person["company"], prospect["company"])
        people.append({
            "name": person["name"],
            "title": person["title"],
            "company": person["company"],
            "about": person["about"],
            "sources": [{"title": s["title"], "url": s["url"]} for s in sources[:3]],
            "matches_link": bool(slug) and any(verify.linkedin_slug(s["url"]) == slug for s in sources),
            "different_company": prospect["company"] if moved else "",
        })
    people.sort(key=lambda p: (not p["matches_link"], bool(p["different_company"])))
    return people[:6]


# ---- 2. after the rep confirms --------------------------------------------


def research_and_write(run_id: str, providers) -> None:
    """Claude asks the questions and writes the email. Everything between is search and plain code."""
    run = db.get_run(run_id)
    seller = _seller_of(run)
    prospect = run["prospect"]
    _find_email(run_id, providers, prospect)
    found = run["data"].get("profile")
    questions = _plan_questions(run_id, providers, prospect, seller, found)
    hits = _search_for_answers(run_id, providers, prospect, questions)
    if found:
        hits += [{"kind": "snippet", "ask": "Their LinkedIn profile", "title": found["title"], "url": found["url"], "text": text, "date": "", "trusted": True} for text in found["snippets"]]
    facts, left_out = _keep_facts(run_id, prospect, hits)
    db.update_run(run_id, facts=facts, questions=questions)
    if not facts:
        write_from_details(run_id, providers, "We didn't find anything specific, so this one will be general")
        return
    stage(run_id, "write", "running")
    plan = providers.deep_json("plan_write", prompts.PLAN_WRITE_SYSTEM, json.dumps(_choosing_packet(prospect, seller, facts), indent=1), prompts.PLAN_WRITE_SCHEMA)
    hooks, set_aside = _rank_openings(run_id, facts, plan["openings"], seller, prospect, left_out)
    db.update_run(run_id, hooks=hooks, set_aside=set_aside, hook_pos=0)
    first = plan["openings"][0]["fact_id"] if plan["openings"] else None
    # Code has the last word: if it turned down Claude's choice, the email is written again for one it accepts.
    write(run_id, providers, 0, plan if hooks[0]["id"] == first else None)


def write_from_details(run_id: str, providers, reason: str) -> None:
    """Always possible: write from the job and the offer alone, and say so."""
    _ahead.pop(run_id, None)
    run = db.get_run(run_id)
    seller = _seller_of(run)
    for key in ("need", "read", "facts"):
        if timeline_states(run_id).get(key) != "done":
            stage(run_id, key, "skipped")
    note(run_id, "write", reason)
    hook = scoring.general_hook(role_focus(run["prospect"]["title"], seller), seller["offerings"][0]["id"])
    db.update_run(run_id, facts=[], hooks=[hook], set_aside=[], hook_pos=0)
    write(run_id, providers, 0)


def _saved_label(when: float) -> str:
    return f"Live search wasn't working, so we used what we saved on {datetime.fromtimestamp(when):%d %b %Y}"


def _find_email(run_id: str, providers, prospect: dict) -> None:
    stage(run_id, "email", "running")
    if prospect.get("email"):
        db.update_run(run_id, email={"address": prospect["email"], "from": "you"})
        note(run_id, "email", "You already gave us this")
        stage(run_id, "email", "skipped")
        return
    parts = prospect["name"].split()
    if len(parts) < 2:
        note(run_id, "email", "We need a first and last name to look this up")
        stage(run_id, "email", "skipped")
        return
    key = _prospect_key(prospect)
    saved = db.load("email", key)
    try:
        found = saved[0] if saved else providers.find_email(parts[0], parts[-1], prospect["company"])
    except ProviderError as exc:
        log.warning("Email lookup failed: %s", exc)
        note(run_id, "email", "We couldn't look this up, so we'll carry on without it")
        stage(run_id, "email", "skipped")
        return
    if not saved:
        db.save("email", key, found)
    if found and found["score"] >= config.HUNTER_MIN_SCORE:
        db.update_run(run_id, email={"address": found["email"], "from": "found", "score": found["score"], "sources": found["sources"]})
        note(run_id, "email", f"Found one, and we're {found['score']}% sure of it")
        stage(run_id, "email", "done")
    else:
        note(run_id, "email", "No email we could trust, so we left it blank")
        stage(run_id, "email", "skipped")


def _queries(prospect: dict, seller: dict) -> list[tuple[str, str]]:
    name, company = prospect["name"], prospect["company"]
    queries = [
        (f'"{name}" "{company}" interview OR podcast', "general"),
        (f'"{name}" {company} said OR wrote OR talk', "general"),
        (f'"{company}" funding OR acquisition OR expansion', "news"),
        (f'"{company}" news', "news"),
    ]
    terms = [term for offering in seller["offerings"] for term in offering["search_terms"][:1]]
    queries += [(f'"{company}" {term}', "general") for term in terms[:4]]
    return queries


def _write_questions(providers, prospect: dict, seller: dict, found: dict | None = None) -> list[dict]:
    """Empty if it fails. Needs only what was typed, so it can run before the person is confirmed."""
    packet = {
        "person": {k: prospect[k] for k in ("name", "title", "company")},
        "what_this_job_cares_about": role_focus(prospect["title"], seller)["cares_about"],
        "seller": seller["seller"],
        "offerings": [{k: o[k] for k in ("name", "plain", "problems", "buying_signals")} for o in seller["offerings"]],
    }
    if found:
        packet["their_linkedin"] = {"headline": found["title"], "profile": " ".join(found["snippets"])}
    try:
        asked = providers.light_json("questions", prompts.QUESTIONS_SYSTEM, json.dumps(packet, indent=1), prompts.QUESTIONS_SCHEMA)["questions"]
    except ProviderError as exc:
        log.warning("Couldn't plan the questions: %s", exc)
        return []
    anchors = (verify.normalise(prospect["name"].split()[-1]), verify.company_core(prospect["company"]))
    questions = []
    for item in asked[: config.MAX_QUESTIONS]:
        query = item["query"].strip()
        if query and not any(a in verify.normalise(query) for a in anchors):
            query = f'{query} "{prospect["company"]}"'  # a search with no name in it would find anyone
        if query:
            questions.append({"ask": item["ask"], "query": query, "news": bool(item["news"])})
    return questions


def _ask_ahead(run_id: str, providers, prospect: dict, seller: dict) -> None:
    key = _prospect_key(prospect)
    if run_inline:
        ready: Future = Future()
        ready.set_result(_write_questions(providers, prospect, seller))
        _ahead[run_id] = (key, ready)
    else:
        _ahead[run_id] = (key, _ahead_pool.submit(_write_questions, providers, prospect, seller))


def _questions_from_ahead(run_id: str, prospect: dict) -> list[dict] | None:
    """None means they have to be written now, such as when the person turned out to work elsewhere."""
    key, job = _ahead.pop(run_id, (None, None))
    if job is None or key != _prospect_key(prospect):
        return None
    try:
        return job.result(timeout=30)
    except Exception as exc:
        log.warning("Questions written ahead weren't usable: %s", exc)
        return None


def _plan_questions(run_id: str, providers, prospect: dict, seller: dict, found: dict | None = None) -> list[dict]:
    stage(run_id, "need", "running")
    questions = _questions_from_ahead(run_id, prospect)
    if questions is None:
        questions = _write_questions(providers, prospect, seller, found)
    if not questions:
        questions = [{"ask": q, "query": q, "news": topic == "news"} for q, topic in _queries(prospect, seller)[: config.MAX_QUESTIONS]]
    for question in questions:
        note(run_id, "need", question["ask"])
    stage(run_id, "need", "done")
    return questions


def gather(providers, questions: list[dict]) -> tuple[list[dict], list[dict], list]:
    """Each question goes to both: Serper is quick and dated, and Tavily brings the full pages."""

    def serper(question):
        try:
            return providers.search_answers(question["query"], question["news"])
        except ProviderError as exc:
            log.warning("Search failed: %s", exc)
            return None

    def tavily(question):
        try:
            return providers.research(question["query"], "news" if question["news"] else "general")
        except ProviderError as exc:
            log.warning("Research failed: %s", exc)
            return None

    if run_inline:
        short = [serper(q) for q in questions]
        full = [tavily(q) for q in questions]
    else:
        with ThreadPoolExecutor(max_workers=8) as pool:
            short_jobs = [pool.submit(serper, q) for q in questions]
            full_jobs = [pool.submit(tavily, q) for q in questions]
            short = [j.result() for j in short_jobs]
            full = [j.result() for j in full_jobs]

    hits = [
        {"kind": "snippet", "ask": q["ask"], "title": r["title"], "url": r["url"], "text": r["snippet"], "date": r["date"]}
        for q, batch in zip(questions, short) for r in batch or []
    ]
    pages, seen = [], set()
    for q, batch in zip(questions, full):
        for page in batch or []:
            if page["url"] not in seen and page["text"].strip():
                seen.add(page["url"])
                pages.append({"kind": "page", "ask": q["ask"], "title": page["title"], "url": page["url"],
                              "text": page["text"][: config.MAX_PAGE_CHARS], "date": page.get("published", "")})
    return hits, pages[: config.MAX_PAGES], full


def _search_for_answers(run_id: str, providers, prospect: dict, questions: list[dict]) -> list[dict]:
    stage(run_id, "read", "running")
    hits, pages, full = gather(providers, questions)

    key = _prospect_key(prospect)
    if all(b is None for b in full):
        saved = db.load("pages", key)
        if saved:
            pages = saved[0]
            db.update_run(run_id, used_saved=_saved_label(saved[1]))
            note(run_id, "read", _saved_label(saved[1]))
        else:
            note(run_id, "read", "We couldn't read full pages just now, so we'll use the search results")
    elif pages:
        db.save("pages", key, pages)

    db.update_run(run_id, searched=[q["query"] for q in questions], pages=[{"title": p["title"], "url": p["url"]} for p in pages])
    note(run_id, "read", f"Read {len(pages)} pages and {len(hits)} search results")
    stage(run_id, "read", "done")
    return hits + pages


def _keep_facts(run_id: str, prospect: dict, hits: list[dict]) -> tuple[list[dict], list[dict]]:
    stage(run_id, "facts", "running")
    facts, left_out = answers.build(hits, prospect, date.today(), strict=bool(prospect.get("linkedin")))
    note(run_id, "facts", f"Kept {len(facts)} facts we can point to")
    for item in left_out[:3]:
        note(run_id, "facts", f"Left out: {item['summary'][:120]} {item['reason']}")
    stage(run_id, "facts", "done")
    return facts, left_out


def role_focus(title: str, seller: dict) -> dict:
    lowered = title.lower()
    for focus in seller["role_focus"]:
        if any(word in lowered for word in focus["match"]):
            return focus
    return seller["default_role_focus"]


def _choosing_packet(prospect: dict, seller: dict, facts: list[dict]) -> dict:
    focus = role_focus(prospect["title"], seller)
    return {
        "to": {k: prospect[k] for k in ("name", "title", "company")},
        "what_this_job_cares_about": focus["cares_about"],
        "lead_with": focus["lead_with"],
        "suggested_ask": focus["ask"],
        "seller": {"name": seller["seller"], "product": seller.get("product", ""), "about": seller["about"], "tone": seller["tone"]},
        "offerings": [{k: o[k] for k in ("id", "name", "plain", "proof")} for o in seller["offerings"]],
        "facts": [{k: f[k] for k in ("id", "quote", "context", "date", "about", "answers")} for f in facts[: config.WRITER_FACTS]],
    }


def _rank_openings(run_id: str, facts: list[dict], openings: list[dict], seller: dict, prospect: dict, left_out: list[dict]):
    """Claude rates the openings. The maths, the limits and the touchy-topic block are still code."""
    offerings = {o["id"] for o in seller["offerings"]}
    default = seller["offerings"][0]["id"]
    hooks, set_aside = scoring.rank(facts, openings, [], offerings, date.today())
    if not hooks:
        hooks, set_aside = scoring.fallback_hooks(set_aside, default)
        if hooks:
            note(run_id, "write", "Nothing strong came up, so we're using the best we have")
        else:
            hooks = [scoring.general_hook(role_focus(prospect["title"], seller), default)]
            note(run_id, "write", "Nothing safe to mention, so this one will be general")
    summaries = {o["fact_id"]: o["summary"] for o in openings}
    for item in hooks + set_aside:
        item["summary"] = summaries.get(item["id"], item["summary"])
    first = openings[0]["fact_id"] if openings else None
    hooks.sort(key=lambda h: h["id"] != first)  # among those code accepted, keep Claude's favourite first
    note(run_id, "write", f"Best opening: {hooks[0]['summary']}")
    return hooks, set_aside + left_out


# ---- 3. write, test, and let the rep decide ------------------------------


def _writing_packet(run: dict, hook: dict, seller: dict, offering: dict) -> dict:
    facts = {f["id"]: f for f in run["data"]["facts"]}
    prospect = run["prospect"]
    focus = role_focus(prospect["title"], seller)
    ordered = hook["fact_ids"] + [i for i in facts if i not in hook["fact_ids"]]
    return {
        "to": {k: prospect[k] for k in ("name", "title", "company")},
        "what_this_job_cares_about": focus["cares_about"],
        "lead_with": focus["lead_with"],
        "suggested_ask": focus["ask"],
        "seller": {"name": seller["seller"], "product": seller.get("product", ""), "about": seller["about"], "tone": seller["tone"]},
        "offering": {k: offering[k] for k in ("name", "plain", "proof")},
        "opening": {
            **{k: hook[k] for k in ("summary", "pressure", "why_they_care", "fact_ids")},
            "strength": "none" if hook.get("general") else "weak" if hook.get("weak") else "strong",
        },
        "facts": [{"id": i, **{k: facts[i].get(k, "") for k in ("what", "quote", "context", "date")}} for i in ordered[:8]],
    }


def _draw_card(run_id: str, providers, brief: dict, token: str) -> None:
    """A nice extra, never a blocker: the email is already on screen, and without a picture the card is words only."""
    idea = {k: brief[k] for k in ("one_idea", "tension", "sticky_line")}
    try:
        drawn = providers.deep_json("card_art", prompts.CARD_ART_SYSTEM, json.dumps(idea), prompts.CARD_ART_SCHEMA)
    except ProviderError as exc:
        log.warning("Card drawing failed: %s", exc)
        drawn = {}
    picture = art.clean_svg(drawn.get("svg", ""))
    if db.set_art(run_id, token, picture):
        note(run_id, "art", f"Drew {drawn['shows']}" if picture and drawn.get("shows") else "Used a card with words only" if not picture else "Drew the picture")
        stage(run_id, "art", "done")


def _read_the_draft(run_id: str, providers, run: dict, hook: dict, facts: list[dict], draft: dict, token: str) -> None:
    """The independent readers, while the rep is already reading. A poor result lowers the score and doesn't rewrite."""
    try:
        readers = checks.run_readers(providers, run["prospect"], draft["subject"], draft["lines"], draft["brief"], facts, config.load_peers(), f"{run_id}-{token[:6]}")
    except ProviderError as exc:
        log.warning("Reader tests failed: %s", exc)
        readers = None
    problems = checks.reader_problems(readers, bool(hook.get("general"))) if readers else []
    sure = scoring.confidence(
        hook, readers, True, bool(problems), bool(run["data"].get("unconfirmed")), bool(run["data"].get("link_unseen")),
        facts_ok=True, nothing_personal=not any(f["about"] == "person" for f in facts),
    )
    if db.finish_tests(run_id, token, bool(problems), tests=readers, problems=problems, confidence=sure, tests_pending=False):
        note(run_id, "test", "We couldn't finish the reader test" if readers is None else "It passed every test" if not problems else f"Readers found {len(problems)} thing{'s' if len(problems) > 1 else ''} to look at")
        stage(run_id, "test", "done")


def write(run_id: str, providers, hook_pos: int, first_draft: dict | None = None) -> None:
    """Write, let code check it, and give the writer one chance to fix what code found."""
    stage(run_id, "write", "running")
    stage(run_id, "test", "todo")
    run = db.get_run(run_id)
    seller = _seller_of(run)
    hook = run["data"]["hooks"][hook_pos]
    offering = next(o for o in seller["offerings"] if o["id"] == hook["offering_id"])
    facts = {f["id"]: f for f in run["data"]["facts"]}
    ordered = [facts[i] for i in hook["fact_ids"]] + [f for i, f in facts.items() if i not in hook["fact_ids"]]
    packet = _writing_packet(run, hook, seller, offering)
    draft = first_draft
    for attempt in range(1, config.MAX_SUBMITS + 1):
        if draft is None:
            draft = providers.deep_json("writer", prompts.WRITER_SYSTEM, json.dumps(packet, indent=1), prompts.WRITER_SCHEMA)
        subject, lines, brief = draft["subject"], draft["lines"], draft["brief"]
        wrong_facts = checks.check_facts(lines, facts, offering["proof"], need_fact=bool(hook["fact_ids"]))
        rules = wrong_facts + checks.check_style(subject, lines) + checks.check_card(brief)
        if not rules:
            break
        note(run_id, "test", f"Try {attempt}: found {len(rules)} thing{'s' if len(rules) > 1 else ''} to fix")
        if attempt < config.MAX_SUBMITS:
            packet = {**packet, "your_last_draft": {"subject": subject, "lines": lines, "brief": brief}, "problems": rules}
            draft = None
    stage(run_id, "write", "done")

    first_name = run["prospect"]["name"].split()[0]
    body = checks.compose_body(lines, first_name, _signed_by(seller, run))
    token = uuid.uuid4().hex
    checking = not rules
    stage(run_id, "test", "running" if checking else "done")
    stage(run_id, "art", "running")  # before the email appears, so the page knows more is coming
    sure = None if checking else scoring.confidence(
        hook, None, False, True, bool(run["data"].get("unconfirmed")), bool(run["data"].get("link_unseen")),
        facts_ok=not wrong_facts, nothing_personal=not any(f["about"] == "person" for f in facts.values()),
    )
    finished = {"subject": subject, "lines": lines, "brief": brief, "body": body, "art": None, "token": token}
    db.update_run(
        run_id,
        status="flagged" if rules else "ready",
        draft=finished,
        seller_name=seller["seller"],
        confidence=sure,
        tests=None,
        tests_pending=checking,
        problems=rules,
        rules_ok=not rules,
        hook_pos=hook_pos,
    )

    jobs = [lambda: _draw_card(run_id, providers, brief, token)]
    if checking:
        jobs.append(lambda: _read_the_draft(run_id, providers, run, hook, ordered, finished, token))
    if run_inline:
        for job in jobs:
            job()
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for result in [pool.submit(job) for job in jobs]:
                result.result()


def launch_next_hook(run_id: str, providers, pos: int) -> None:
    launch(write, run_id, providers, pos)
