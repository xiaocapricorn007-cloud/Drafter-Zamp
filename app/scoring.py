"""Turns the AI's ratings into a ranked list of openings. The maths and the rules are code."""
import re
from datetime import date, datetime

from .config import HOOK_THRESHOLD

WEIGHTS = {"personal": 25, "pain": 25, "fit": 25, "unexpected": 10}
FRESH_MAX = 15
PERSONAL_CAP = {"person": 5, "team": 4, "company": 3, "market": 2}
SENSITIVE_WORDS = re.compile(
    r"\b(lay-?offs?|laid off|job cuts?|redundanc\w+|lawsuits?|sued|data breach|breach(ed)?|"
    r"bankrupt\w*|fraud|investigation|died|death|passed away|illness|divorce)\b",
    re.I,
)


def parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text[:40], fmt).date()
        except ValueError:
            continue
    return None


def freshness(when: str, today: date) -> int:
    day = parse_date(when)
    if day is None or day > today:
        return 3  # unknown dates get a low score, not a free pass
    age = (today - day).days
    for limit, points in ((30, 15), (90, 11), (180, 7), (365, 3)):
        if age <= limit:
            return points
    return 0


def _clamp(value) -> int:
    return max(1, min(5, int(value)))


def _points(ratings: dict, about: str, fresh: int) -> tuple[int, dict]:
    parts = {k: _clamp(ratings[k]) for k in WEIGHTS}
    parts["personal"] = min(parts["personal"], PERSONAL_CAP.get(about, 2))
    total = sum(WEIGHTS[k] * parts[k] / 5 for k in WEIGHTS) + fresh
    return round(total), {**parts, "fresh": fresh}


def is_sensitive(fact: dict, assessed: dict) -> str:
    """Either the AI or the word list can block a topic. Neither can clear it alone."""
    if assessed.get("sensitive"):
        return assessed.get("sensitive_reason") or "It's a touchy subject."
    hit = SENSITIVE_WORDS.search(f"{fact['what']} {fact['quote']}")
    return f"It mentions “{hit.group(0)}”, which is a touchy way to start." if hit else ""


def rank(facts: list[dict], assessments: list[dict], pairings: list[dict], offerings: set[str], today: date):
    """Returns (openings we can use, best first; openings we set aside, with the reason)."""
    by_id = {f["id"]: f for f in facts}
    usable, set_aside = [], []
    blocked: dict[str, str] = {}

    for item in assessments:
        fact = by_id.get(item["fact_id"])
        if not fact:
            continue
        hook = {
            "id": fact["id"],
            "fact_ids": [fact["id"]],
            "summary": fact["what"],
            "pressure": item["pressure"],
            "why_they_care": item["why_they_care"],
            "offering_id": item["offering_id"],
            "two_facts": False,
        }
        reason = is_sensitive(fact, item)
        if reason:
            blocked[fact["id"]] = reason
            set_aside.append({**hook, "reason": reason, "score": 0, "sensitive": True})
            continue
        hook["score"], hook["parts"] = _points(item, fact["about"], freshness(fact["date"], today))
        if item["offering_id"] not in offerings:
            set_aside.append({**hook, "reason": "Nothing we offer helps with this.", "no_fit": True})
            continue
        usable.append(hook)

    for pair in pairings:
        ids = list(dict.fromkeys(pair["fact_ids"]))
        chosen = [by_id[i] for i in ids if i in by_id]
        if len(chosen) != 2 or len(ids) != 2 or any(i in blocked for i in ids):
            continue
        if pair["offering_id"] not in offerings:
            continue
        about = min((f["about"] for f in chosen), key=lambda a: -PERSONAL_CAP.get(a, 2))
        fresh = max(freshness(f["date"], today) for f in chosen)
        score, parts = _points(pair, about, fresh)
        usable.append({
            "id": "+".join(ids),
            "fact_ids": ids,
            "summary": pair["idea"],
            "pressure": pair["pressure"],
            "why_they_care": pair["why_they_care"],
            "offering_id": pair["offering_id"],
            "two_facts": True,
            "score": score,
            "parts": parts,
        })

    usable.sort(key=lambda h: (-h["score"], not h["two_facts"]))
    ready = [h for h in usable if h["score"] >= HOOK_THRESHOLD]
    for hook in usable:
        if hook["score"] < HOOK_THRESHOLD:
            set_aside.append({**hook, "reason": "Not strong enough to open with."})
    return ready, set_aside


def fallback_hooks(set_aside: list[dict], default_offering: str):
    """When nothing is strong, use the best weak openings. Touchy topics are never used."""
    pool = [h for h in set_aside if not h.get("sensitive")]
    pool.sort(key=lambda h: (bool(h.get("no_fit")), -h["score"]))
    picks = pool[:3]
    hooks = [
        {**h, "weak": True, "offering_id": default_offering if h.get("no_fit") else h["offering_id"]}
        for h in picks
    ]
    return hooks, [h for h in set_aside if h not in picks]


def general_hook(focus: dict, default_offering: str) -> dict:
    """The last resort: nothing specific is known, so the message is about the job, not the person."""
    cares = ", ".join(focus["cares_about"]).lower()
    return {
        "id": "general",
        "fact_ids": [],
        "summary": "A general note about what people in this job usually care about",
        "pressure": f"People in this job usually care about {cares}.",
        "why_they_care": "We found nothing specific about them, so this is about their job in general.",
        "offering_id": default_offering,
        "two_facts": False,
        "score": 0,
        "parts": {},
        "general": True,
    }


def confidence(
    hook: dict, readers: dict | None, rules_ok: bool, flagged: bool, unconfirmed: bool, link_unseen: bool = False,
    facts_ok: bool | None = None, nothing_personal: bool = False,
) -> dict:
    """How much to trust a draft. Every point here is plain arithmetic the rep can see explained."""
    readers = readers or {}
    facts_ok = rules_ok if facts_ok is None else facts_ok
    points = hook["score"] * 0.45
    points += 15 * bool(rules_ok)
    points += 15 * bool(readers.get("only_them"))
    points += 10 * bool(readers.get("same_idea"))
    points += 10 * bool(readers.get("would_open"))
    points += 5 * bool(readers.get("kind"))

    good, bad = [], []
    limits = [100]
    if hook.get("general"):
        bad.append("We found nothing specific about them, so this message is general.")
        limits.append(35)
    elif hook.get("no_fit"):
        bad.append("What we found about them has little to do with what we offer.")
        limits.append(45)
    elif hook.get("weak"):
        bad.append("The link between what we found and what we offer is weak.")
        limits.append(65)
    else:
        good.append("It opens with something real that matters to them.")
    if unconfirmed:
        bad.append("You gave us these details. We didn't confirm them online.")
        limits.append(25)
    if link_unseen:
        bad.append("We couldn't see their LinkedIn page, so we relied on the details you gave.")
        limits.append(40 if nothing_personal and not hook.get("general") else 60)
        if nothing_personal and not hook.get("general"):
            bad.append("Nothing we found was about them personally, only about where they work.")
    if flagged:
        bad.append("It didn't pass all of our checks.")
        limits.append(50)
    if not facts_ok:
        bad.append("Some facts couldn't be matched to a source.")
    elif not hook.get("general"):
        good.append("Every fact matches where we found it.")
    if facts_ok and not rules_ok:
        bad.append("It broke one of our writing rules, so a reader never tested it.")
    if readers and not hook.get("general"):
        (good if readers["only_them"] else bad).append("A reader could tell it was written for them." if readers["only_them"] else "It could fit other people too.")
    if readers:
        (good if readers["same_idea"] else bad).append("A reader remembered the main idea." if readers["same_idea"] else "A reader missed the main idea.")

    score = round(min(points, *limits))
    label = "High" if score >= 75 else "Medium" if score >= 50 else "Low"
    return {"score": score, "label": label, "good": good, "bad": bad}
