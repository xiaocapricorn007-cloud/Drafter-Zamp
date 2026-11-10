"""First-time setup: work out what this account sells, so every draft can pitch it.

It follows the same steps as a draft: search, let the person confirm, ask questions,
search for the answers, and keep only what can be traced back to a source.
"""
import json
import logging
import re
from datetime import date
from urllib.parse import urlparse

from . import answers, db, pipeline, prompts, verify
from .providers import ProviderError

log = logging.getLogger("drafter")

MAX_QUESTIONS = 8
MAX_FACTS = 50
MAX_OFFERINGS = 5
MAX_ROLES = 5
MAX_CANDIDATES = 4
DEFAULT_TONE = "Plain, warm and direct. Short sentences. Sounds like one thoughtful person writing to another."
DEFAULT_ASK = "a 15-minute call"
STEPS = [
    ("ask", "Working out what to ask"),
    ("read", "Looking for the answers"),
    ("keep", "Keeping what we can prove"),
    ("write", "Writing up your product"),
]
BUSY = ("finding", "researching")


def _domain(url: str) -> str:
    url = (url or "").strip()
    if url and "//" not in url:
        url = "https://" + url
    return re.sub(r"^www\.", "", urlparse(url).netloc.lower())


def _launch(fn, user_id: str, providers, *args) -> None:
    def guarded():
        try:
            fn(user_id, providers, *args)
        except Exception:
            log.exception("Product setup failed for %s", user_id)
            db.update_setup(user_id, status="failed", problem="Something went wrong on our side. Please try again.")

    if pipeline.run_inline:
        guarded()
    else:
        pipeline._pool.submit(guarded)


def begin(user_id: str, providers, company: str, product: str, website: str) -> None:
    db.set_setup(user_id, {"status": "finding", "company": company, "product": product, "website": website, "candidates": [], "steps": [], "problem": ""})
    _launch(_find, user_id, providers)


def _find(user_id: str, providers) -> None:
    setup = db.get_setup(user_id)
    company, product = setup["company"], setup["product"]
    queries = [f'"{product}" "{company}"', f'"{product}" {company} product', f'"{company}" what does {product} do']
    site = _domain(setup["website"])
    if site:
        queries.insert(0, f"site:{site} {product}")
    results, seen = [], set()
    for query in queries:
        try:
            found = providers.web_search(query)
        except ProviderError as exc:
            log.warning("Product search failed: %s", exc)
            continue
        for item in found:
            if item["url"] not in seen:
                seen.add(item["url"])
                results.append(item)

    candidates = []
    if results:
        listing = "\n".join(f"[{i}] {r['title']} — {r['url']}\n{r['snippet']}" for i, r in enumerate(results))
        try:
            answer = providers.light_json(
                "products", prompts.PRODUCTS_SYSTEM,
                f"Looking for: {product}, sold by {company}\n\n<results>\n{listing}\n</results>", prompts.PRODUCTS_SCHEMA,
            )
        except ProviderError as exc:
            log.warning("Product matching failed: %s", exc)
            answer = {"products": []}
        for item in answer["products"]:
            sources = [results[i] for i in item["source_ids"] if 0 <= i < len(results)]
            if sources and item["product"].strip():
                candidates.append({
                    "product": item["product"].strip(), "company": item["company"].strip() or company, "about": item["about"],
                    "sources": [{"title": s["title"], "url": s["url"]} for s in sources[:3]],
                })
    if site:
        candidates.sort(key=lambda c: not any(_domain(s["url"]).endswith(site) for s in c["sources"]))
    db.update_setup(user_id, status="choosing", candidates=candidates[:MAX_CANDIDATES])


def choose(user_id: str, providers, index: int | None, description: str) -> None:
    """Raises ValueError with a plain reason if this answer can't be used."""
    setup = db.get_setup(user_id)
    candidates = setup.get("candidates", [])
    if index is not None:
        if not 0 <= index < len(candidates):
            raise ValueError("Please pick one of the products shown.")
        chosen = candidates[index]
    else:
        if len(description.strip()) < 15:
            raise ValueError("Please tell us a little more about what it does.")
        chosen = {"product": setup["product"], "company": setup["company"], "about": description.strip(), "sources": []}
    steps = [{"key": k, "label": label, "state": "todo", "notes": []} for k, label in STEPS]
    if not db.move_setup(user_id, "choosing", "researching", chosen=chosen, steps=steps):
        raise LookupError
    _launch(_research, user_id, providers)


def _step(user_id: str, key: str, state: str, note: str | None = None) -> None:
    steps = db.get_setup(user_id)["steps"]
    for step in steps:
        if step["key"] == key:
            step["state"] = state
            if note:
                step["notes"].append(note)
    db.update_setup(user_id, steps=steps)


def _plan_questions(user_id: str, providers, chosen: dict) -> list[dict]:
    _step(user_id, "ask", "running")
    packet = {k: chosen[k] for k in ("product", "company", "about")}
    try:
        asked = providers.light_json("product_questions", prompts.PRODUCT_QUESTIONS_SYSTEM, json.dumps(packet, indent=1), prompts.QUESTIONS_SCHEMA)["questions"]
    except ProviderError as exc:
        log.warning("Couldn't plan product questions: %s", exc)
        asked = []
    anchors = (verify.normalise(chosen["product"]), verify.company_core(chosen["company"]))
    questions = []
    for item in asked[:MAX_QUESTIONS]:
        query = item["query"].strip()
        if query and not any(a in verify.normalise(query) for a in anchors):
            query = f'{query} "{chosen["company"]}"'  # a search with no name in it would find anyone
        if query:
            questions.append({"ask": item["ask"], "query": query, "news": bool(item["news"])})
    if not questions:
        name = f'"{chosen["product"]}" "{chosen["company"]}"'
        questions = [{"ask": ask, "query": f"{name} {words}", "news": False} for ask, words in (
            ("What does it do?", "features how it works"), ("Who buys it?", "customers use cases"),
            ("What results have customers had?", "case study results"), ("How is it different?", "compared to alternatives"),
        )]
    for question in questions:
        _step(user_id, "ask", "running", question["ask"])
    _step(user_id, "ask", "done")
    return questions


def _proof_from(claims: list[dict], facts: dict[str, dict]) -> tuple[list[str], int]:
    """A customer result goes in only if a real source says so. These end up in emails."""
    kept, dropped = [], 0
    for item in claims:
        cited = [facts[i] for i in item["fact_ids"] if i in facts]
        claim = " ".join(item["claim"].split())
        source = " ".join(f"{f['quote']} {f['context']}" for f in cited)
        if claim and cited and verify.numbers_in(claim) <= verify.numbers_in(source):
            kept.append(claim[:300])
        else:
            dropped += 1
    return kept, dropped


def _strings(items: list[str], most: int = 6) -> list[str]:
    return [" ".join(i.split())[:200] for i in items if i.strip()][:most]


def _research(user_id: str, providers) -> None:
    setup = db.get_setup(user_id)
    chosen = setup["chosen"]
    questions = _plan_questions(user_id, providers, chosen)

    _step(user_id, "read", "running")
    hits, pages, _ = pipeline.gather(providers, questions)
    _step(user_id, "read", "done", f"Read {len(pages)} pages and {len(hits)} search results")

    _step(user_id, "keep", "running")
    official = {d for d in ({_domain(s["url"]) for s in chosen["sources"]} | {_domain(setup["website"])}) if d}
    everything = hits + pages
    for hit in everything:
        hit["trusted"] = _domain(hit["url"]) in official
    who = {"name": chosen["product"], "company": chosen["company"]}
    facts, _ = answers.build(everything, who, date.today(), strict=True, avoid_touchy=False, limit=MAX_FACTS)
    _step(user_id, "keep", "done", f"Kept {len(facts)} facts we can point to")

    _step(user_id, "write", "running")
    packet = {
        "product": chosen["product"], "company": chosen["company"], "about": chosen["about"],
        "facts": [{k: f[k] for k in ("id", "quote", "context", "answers")} for f in facts],
    }
    written = providers.deep_json("product_profile", prompts.PRODUCT_PROFILE_SYSTEM, json.dumps(packet, indent=1), prompts.PRODUCT_PROFILE_SCHEMA)
    by_id = {f["id"]: f for f in facts}
    offerings, thrown_out = [], 0
    for n, item in enumerate(written["offerings"][:MAX_OFFERINGS], 1):
        if not (item["name"].strip() and item["plain"].strip()):
            continue
        proof, dropped = _proof_from(item["proof"], by_id)
        thrown_out += dropped
        offerings.append({
            "id": f"o{n}", "name": item["name"].strip()[:120], "plain": " ".join(item["plain"].split())[:300],
            "problems": _strings(item["problems"]), "buying_signals": _strings(item["buying_signals"]),
            "search_terms": _strings(item["search_terms"], 3), "proof": proof,
        })
    if not offerings:
        db.update_setup(user_id, status="failed", problem="We couldn't work out what this product does. Please try again and add its website.")
        return

    seller = {
        "seller": chosen["company"],
        "product": chosen["product"],
        "about": " ".join(written["about"].split())[:500],
        "tone": DEFAULT_TONE,
        "default_ask": DEFAULT_ASK,
        "offerings": offerings,
        "role_focus": [
            {"match": [m.lower() for m in _strings(r["match"], 8)], "cares_about": _strings(r["cares_about"]), "lead_with": r["lead_with"][:200], "ask": r["ask"][:120] or DEFAULT_ASK}
            for r in written["role_focus"][:MAX_ROLES] if r["match"]
        ],
        "default_role_focus": {
            "cares_about": _strings(written["default_role_focus"]["cares_about"]) or ["Saving time", "Cost"],
            "lead_with": written["default_role_focus"]["lead_with"][:200], "ask": written["default_role_focus"]["ask"][:120] or DEFAULT_ASK,
        },
        "sources": [{"title": p["title"], "url": p["url"]} for p in pages[:8]],
    }
    db.set_seller(user_id, json.dumps(seller))
    if thrown_out:
        _step(user_id, "write", "running", f"Threw out {thrown_out} claim{'s' if thrown_out > 1 else ''} we couldn't trace to a source")
    _step(user_id, "write", "done")
    db.update_setup(user_id, status="ready")
